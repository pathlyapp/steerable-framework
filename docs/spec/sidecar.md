# Sidecar Spec

`steerable-sidecar` is a **portable Python executable** that exposes the
runtime over JSON-RPC 2.0 framed on stdin/stdout. UI shells (Electron,
Tauri, native, …) spawn the sidecar as a subprocess, send method calls
on stdin, receive responses + notifications on stdout, and observe
log + ready markers on stderr.

## Why JSON-RPC over stdio?

- **No port allocation** — works inside sandboxed app containers.
- **No TLS dance** — every byte stays inside the parent process.
- **Native to subprocess supervision** — `child.kill()` is your DR plan.

## Boot sequence

```mermaid
sequenceDiagram
    autonumber
    participant P as parent
    participant S as sidecar<br/>(python -m steerable_sidecar)

    P->>S: spawn(child, ['-m','steerable_sidecar'], stdio=pipe)
    Note over S: bootstrap …
    S-->>P: stderr: __SIDECAR_READY__:{"status":"ok",<br/>"version":"0.1.0","protocolVersion":"0.1.0", …}
    S-->>P: stdout (no id):<br/>{"jsonrpc":"2.0","method":"lifecycle.ready","params":{…}}
    Note over P: Now safe to send JSON-RPC frames.
```

The parent **must wait** for the `__SIDECAR_READY__:` marker on stderr
before sending its first frame. The sidecar **also** emits a
`lifecycle.ready` JSON-RPC notification on stdout immediately after —
parents that use a frame-based reader (rather than peeking stderr) can
key off that instead. Either way, your reader must distinguish
**responses** (carry an `id`) from **notifications** (no `id`).

## Frame format

One JSON object per line, UTF-8, terminated by `\n`. No length-prefix.

### Request

```json
{"jsonrpc":"2.0","id":1,"method":"system.ping"}
```

### Successful response

```json
{"jsonrpc":"2.0","id":1,"result":{"status":"ok","version":"0.1.0","protocolVersion":"0.1.0","uptimeMs":1234,"pid":42,"pythonVersion":"3.12.6","platform":"darwin-arm64","loadedProviders":[],"loadedTools":0,"activeTraces":0,"checks":{}}}
```

### Error response

```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"Method not found","data":{"method":"foo"}}}
```

### Notification (sidecar → parent, no `id`)

```json
{"jsonrpc":"2.0","method":"stream.chunk","params":{"streamId":"s_42","delta":"Hello"}}
```

## Method catalog (v0.1.0)

| Method                  | Direction | Result                                 |
| ----------------------- | --------- | -------------------------------------- |
| `system.ping`           | request   | `SidecarHealth`                        |
| `system.shutdown`       | request   | `null` (graceful drain, then exit)     |
| `system.shutdown_now`   | request   | `null` (immediate exit)                |
| `agent.session.create`  | request   | `AgentSession`                         |
| `agent.session.resume`  | request   | `AgentSession`                         |
| `agent.session.list`    | request   | `AgentSession[]`                       |
| `agent.session.fork`    | request   | `BranchPoint` (fork a record, no turn run) |
| `agent.session.branches`| request   | `{lineage, children}` (branch-family view) |
| `agent.chat.stream`     | request   | `{"streamId": "s_…"}`                  |
| `agent.chat.cancel`     | request   | `null` (cooperative cancel)            |
| `agent.chat.steer`      | request   | `{"accepted": bool}` (mid-turn steer)  |
| `agent.chat.fork`       | request   | fork the running turn's record         |
| `tool.list`             | request   | `ToolDescriptor[]`                     |
| `tool.invoke`           | request   | `ToolResult`                           |
| `workspace.apply_edits` | request   | `{content, diff, applied, matches}` (pure edit algorithm; host owns file I/O) |
| `skills.list`           | request   | `{skills}` (parse + select SKILL.md from host roots) |
| `trace.fetch`           | request   | `{"trace": HarnessTrace, "spans": TraceSpan[], "events": TraceEvent[]}` |
| `trace.export`          | request   | `{status, traceId, privacyMode}` (OTLP/HTTP push) |
| `config.get`            | request   | `Record<string, unknown>`              |
| `config.set`            | request   | `null`                                 |

Notifications emitted by the sidecar:

| Notification         | When                                           | Params                                                  |
| -------------------- | ---------------------------------------------- | ------------------------------------------------------- |
| `lifecycle.ready`    | After boot, before accepting requests          | `{version, protocolVersion, pid, listenInfo}`           |
| `lifecycle.shutdown` | Just before the process exits                  | `{reason}` (`"normal" \| "eof"`)                        |
| `stream.chunk`       | LLM token / tool-call / usage during a stream  | `{streamId, delta?, toolCall?, usage?, finishReason?}`  |
| `stream.done`        | Stream terminated cleanly                      | `{streamId, ok, cancelled?}`                            |
| `stream.error`       | Stream failed (provider error, etc.)           | `{streamId, kind, message}`                             |
| `agent.child`        | Orchestration child lifecycle (spawned/completed/failed/cancelled) | `{streamId, kind, childId, depth?, status?}` |

## `agent.chat.stream` payload

```json
{
  "jsonrpc":"2.0", "id":7, "method":"agent.chat.stream",
  "params": {
    "provider":"openai_compat",
    "model":"gpt-4o-mini",
    "baseUrl":"https://api.openai.com/v1",
    "apiKey":"sk-…",
    "temperature":0.7,
    "messages":[{"role":"user","content":"Say hi"}],
    "tools":[{"type":"function","function":{"name":"echo","parameters":{}}}]
  }
}
```

The sidecar replies with `{"streamId":"s_42"}` immediately, then pushes
`stream.chunk` notifications until `stream.done`.

CoreLoop tunables accepted in `params` (all optional): `maxRounds`,
`maxToolErrors`, `budgetTokens`, `softTimeoutMs`, `toolTimeoutMs`.
`toolTimeoutMs` is the per-tool-execution backstop: a tool that produces
no result within the budget is cancelled and returns a failed
`ToolResult` (`error: "tool_timeout"`) instead of hanging the turn — the
consecutive-error breaker treats it like any other tool failure. It
applies to every executor, in-process or remote (reverse channel, future
MCP). Default 300000 (5 min); the default is a hung-tool backstop, not a
budget — set a tighter value for fast tools.

OpenAI-compatible vendor divergences are data, not provider branches
(`steerable_agent_runtime.llm.compat`). An optional `compat` object in
`params` overrides request/response handling for the OpenAI-compatible
path; keys are camelCase (`supportsUsageInStreaming`, `maxTokensField`,
`supportsReasoningEffort`, `supportsTemperature`, `reasoningDeltaFields`,
`cachedTokensFields`) and unknown keys are rejected. Without `compat`,
the sidecar auto-detects known vendors from the `baseUrl` host
(`PROVIDER_COMPAT_HOSTS`); anything unmatched runs on reference OpenAI
behavior.

`agent.chat.cancel` on a CoreLoop stream is cooperative: the loop winds
down at the next safe point (round boundary, stream chunk, or tool-call
slot), records the partial turn so the chat can continue, and the
terminal `stream.done` carries `status: "cancelled"` with
`cancelled: true`. A 5s watchdog hard-cancels the task only if the
wind-down wedges.

Multi-agent orchestration is opt-in via `orchestration: {maxDepth?,
maxParallel?, childMaxRounds?}` in `params`: the parent model drives
parallel child CoreLoops through four tools — `agent_spawn` (returns a
lineage id like `0.2`, optional `toolFilter` narrows the child's tool
domain), `agent_send` (steers a running child), `agent_wait`
(`timeoutMs`; a live child at timeout returns `status: "running"`),
`agent_close` (cooperative cancel with a hard-cancel backstop). Budgets
fail closed: spawning at the parallel cap returns
`orchestration_budget_exceeded`, and depth is structural — a child only
has orchestration tools when `maxDepth` allows its own pool. Child
lifecycle lands as `agent.child` notifications; every spawn/wait result
carries the child id as structured JSON, so the delegation is
reconstructable from the session record alone. Children still running
when the parent ends are wound down cooperatively.

## Health snapshot

```json
{
  "status": "ok",
  "version": "0.1.0",
  "protocolVersion": "0.1.0",
  "uptimeMs": 12345,
  "pid": 42,
  "pythonVersion": "3.12.6",
  "platform": "darwin-arm64",
  "loadedProviders": [],
  "loadedTools": 0,
  "activeTraces": 0,
  "checks": {}
}
```

## Error codes

The sidecar reuses standard JSON-RPC error codes (`-32700` parse error,
`-32600` invalid request, `-32601` method not found, `-32602` invalid
params, `-32603` internal error) plus framework-specific:

| Code      | Meaning                                  |
| --------- | ---------------------------------------- |
| `-32001`  | `BudgetExhaustedError`                   |
| `-32002`  | `PolicyDeniedError`                      |
| `-32003`  | `ToolDispatchError`                      |
| `-32004`  | `StorageError`                           |
| `-32005`  | `TransportError`                         |

## CLI flags

```
$ python -m steerable_sidecar --help
usage: steerable-sidecar [-h] [--log-level {DEBUG,INFO,WARNING,ERROR}] [--quiet-ready]

options:
  -h, --help                Show help and exit.
  --log-level {DEBUG,INFO,WARNING,ERROR}
                            Sidecar log level (always logged on stderr).
  --quiet-ready             Skip the __SIDECAR_READY__ stderr marker.
                            (Useful for embedded supervisors that prefer to
                            key off the `lifecycle.ready` stdout notification.)
```

## Implementation notes

- The sidecar is single-loop async; concurrent requests interleave on
  the event loop but are ordered by their JSON-RPC `id`.
- `agent.chat.stream` returns immediately and continues to push
  notifications even if the parent processes them slowly. There's no
  back-pressure on the wire — assume your parent can drain stdout.
- `system.shutdown` triggers a graceful drain (in-flight streams cancel,
  pending tool dispatches abort) before returning `null` and exiting.
  `system.shutdown_now` skips the drain.
- Parent processes should also send `SIGTERM` as a backstop in case
  `system.shutdown` hangs; the sidecar installs a `SIGTERM` handler that
  forces an immediate exit.
