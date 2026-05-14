#!/usr/bin/env bash
# Install dist/* artifacts into throwaway envs and run minimal real code.
set -euo pipefail

cd "$(dirname "$0")/../.."
DIST="$(pwd)/dist"

if [[ ! -d "$DIST/py" || ! -d "$DIST/npm" ]]; then
  echo "ERROR: $DIST not populated. Run ./scripts/release/build-local-artifacts.sh first." >&2
  exit 1
fi

VENV=/tmp/steerable-verify-py
NPMDIR=/tmp/steerable-verify-npm

echo "== 1/2  Python wheels into clean venv"
rm -rf "$VENV"
uv venv "$VENV" --python 3.12 >/dev/null
uv pip install --python "$VENV/bin/python" \
    "$DIST"/py/steerable_agent_protocol-*.whl \
    "$DIST"/py/steerable_agent_harness-*.whl \
    "$DIST"/py/steerable_agent_runtime-*.whl \
    "$DIST"/py/steerable_sidecar-*.whl >/dev/null

"$VENV/bin/python" - <<'PY'
import asyncio
from steerable_agent_protocol import ToolCall
from steerable_agent_harness import decide_tool_mode, is_terminal_result
from steerable_agent_runtime import ToolRouter, tool

router = ToolRouter()
@tool(router=router)
async def hello(name: str = "world") -> dict:
    return {"greeting": f"Hello, {name}!"}

async def main():
    assert decide_tool_mode("read_file") == "read"
    r = await router.dispatch(ToolCall(id="c1", name="hello", arguments={"name": "Steerable"}))
    assert r.success and r.data["value"]["greeting"] == "Hello, Steerable!"
    assert not is_terminal_result(r.model_dump())
    print("PY OK:", r.data["value"])

asyncio.run(main())
PY

echo "    Sidecar boot/ping/shutdown..."
timeout 15 "$VENV/bin/python" - <<'PY'
import asyncio, json, sys
async def main():
    p = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "steerable_sidecar",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    while True:
        line = (await p.stderr.readline()).decode().rstrip()
        if line.startswith("__SIDECAR_READY__:"):
            print("    READY:", json.loads(line.split(":",1)[1])["version"])
            break
    p.stdin.write(json.dumps({"jsonrpc":"2.0","id":1,"method":"system.ping"}).encode()+b"\n")
    await p.stdin.drain()
    while True:
        f = json.loads(await p.stdout.readline())
        if f.get("id") == 1:
            print("    PING:", f["result"]["status"]); break
    p.stdin.write(json.dumps({"jsonrpc":"2.0","id":2,"method":"system.shutdown"}).encode()+b"\n")
    await p.stdin.drain()
    await p.wait()
asyncio.run(main())
PY

echo
echo "== 2/2  npm tarballs into clean Node ESM project"
rm -rf "$NPMDIR"
mkdir -p "$NPMDIR"
cat > "$NPMDIR/package.json" <<JSON
{
  "name": "verify",
  "private": true,
  "type": "module",
  "dependencies": {
    "@steerable/agent-protocol": "file:$DIST/npm/steerable-agent-protocol-0.1.0.tgz",
    "@steerable/agent-harness":  "file:$DIST/npm/steerable-agent-harness-0.1.0.tgz"
  },
  "pnpm": {
    "overrides": {
      "@steerable/agent-protocol": "file:$DIST/npm/steerable-agent-protocol-0.1.0.tgz",
      "@steerable/agent-harness":  "file:$DIST/npm/steerable-agent-harness-0.1.0.tgz"
    }
  }
}
JSON
( cd "$NPMDIR" && pnpm install --no-frozen-lockfile >/dev/null )

cat > "$NPMDIR/smoke.mjs" <<'JS'
import { decideToolMode, consumeBudget, isTerminalResult } from '@steerable/agent-harness';
const proto = await import('@steerable/agent-protocol');
const mode = decideToolMode('read_file');
const { state } = consumeBudget(
  { tokensUsed: 0, stepsUsed: 0, toolCallsUsed: 0 },
  { maxTokens: 100, maxSteps: 5, maxToolCalls: 3 },
  { toolCall: true, tokens: 10, step: true },
);
const done = isTerminalResult({ success: true, terminal: true });
if (mode !== 'read' || !done) { console.error('TS FAILED'); process.exit(1); }
console.log('TS OK:', JSON.stringify({ mode, state, done }));
JS
( cd "$NPMDIR" && node smoke.mjs )

echo
echo "All artifact smoke-tests passed."
