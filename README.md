# Steerable

**The agent plumbing you'd otherwise rewrite.** Typed wire protocol, pluggable LLM runtime, embeddable Python sidecar, and ready-to-mount React chat UI — pick any subset, skip the rest.

→ Docs: <https://pathlyapp.github.io/steerable-framework/> · Storybook: <https://pathlyapp.github.io/steerable-framework/storybook/>

---

## Why you'd reach for it

Building an LLM agent product means rewriting the same five things every time:

| Problem | Steerable's answer |
|---|---|
| **"What shape is this SSE stream?"** Every team invents their own envelope; FE and BE drift. | One JSON Schema → generated **TypeScript types + Pydantic models**, kept in lockstep release. `content`, `tool_call`, `tool_result`, `error`, `done`, `budget_exhausted` all standardised. |
| **Tool dispatch / budgets / retries / safety regex** | `agent-harness` (Py): `decide_tool_mode`, `consume_budget`, `next_retry_delay_ms`, `is_terminal_result`, command-safety patterns. Pure functions, zero I/O coupling — drop into FastAPI / Celery / a notebook. |
| **LLM provider abstraction** | `agent-runtime` (Py): one `LLMProvider` interface, adapters for **Ollama / OpenAI-compatible / Anthropic**, `@tool` decorator, `ToolRouter`, SSE/stdio transport. |
| **Shipping LLMs in a desktop app without an internet round-trip** | `steerable-sidecar`: a portable, signed CPython binary that speaks JSON-RPC over stdio. Bundle with Electron/Tauri/anything. |
| **Building a chat UI that doesn't look like 2003** | `@steerable/agent-ui`: headless React hooks (`useChatStream`, `useAgentSession`, `useToolCallStatus`) + opinionated components (`ChatPanel`, `MessageList`, `OrchestrationPlanCard`, `ToolCallRenderer`, `SSEStreamView`) + Tailwind preset. |

Every layer is independently published. You can use just the protocol types, just the UI, just the sidecar — there's no monolith to swallow.

---

## Pick your path (5 minutes)

### "I'm building a Python agent backend"

```bash
uv add steerable-agent-protocol steerable-agent-harness steerable-agent-runtime
```

```python
from steerable_agent_runtime import ToolRouter, tool
from steerable_agent_protocol import ToolCall

router = ToolRouter()

@tool(router=router, description="Read a file by path")
async def read_file(path: str) -> dict:
    return {"path": path, "content": open(path).read()}

result = await router.dispatch(ToolCall(id="c1", name="read_file", arguments={"path": "README.md"}))
```

Full runnable: [`examples/py-minimal`](./examples/py-minimal).

### "I'm building a React chat UI on top of someone else's SSE endpoint"

```bash
pnpm add @steerable/agent-protocol @steerable/agent-ui
```

```tsx
import { ChatPanel, useChatStream } from '@steerable/agent-ui';

export function Chat() {
  const { messages, send, isStreaming } = useChatStream({
    endpoint: '/api/chats/123/send',
  });
  return <ChatPanel messages={messages} onSubmit={send} isStreaming={isStreaming} />;
}
```

`useChatStream` parses every standard `SSEEvent` shape into typed messages — you don't write a parser. See live components at the [Storybook](https://pathlyapp.github.io/steerable-framework/storybook/).

### "I'm shipping an Electron app and want LLMs to run locally"

```bash
pnpm add @steerable/agent-protocol @steerable/agent-ui
# Then bundle the sidecar binary into resources/python-runtime/<platform>/
# (build script: packages/sidecar/build/build_sidecar.py)
```

```ts
import { spawn } from 'node:child_process';

const proc = spawn(sidecarPath, [], { stdio: ['pipe', 'pipe', 'inherit'] });
proc.stdin.write(JSON.stringify({
  jsonrpc: '2.0', id: 1, method: 'agent.chat.stream',
  params: { messages: [{ role: 'user', content: 'hi' }] },
}) + '\n');
// SSE-over-JSON-RPC events stream back on stdout.
```

Full runnable: [`examples/sidecar-roundtrip`](./examples/sidecar-roundtrip). Real-world embedder: [`deeppath-agent`](https://github.com/deeppath/deeppath-agent).

---

## Who's using it in production

- **[DeepPath](https://deeppath.cc)** — web (`@steerable/agent-protocol` + `@steerable/agent-ui`), API (all 3 Py packages), Electron desktop (sidecar + UI). The framework was extracted from this codebase and is dogfooded back into it on every release.

(If you're using Steerable in production, send a PR adding your project here.)

---

## Packages reference

| Need | Install | Tier |
|---|---|---|
| Just typed wire events for FE/BE alignment | `pnpm add @steerable/agent-protocol` *or* `uv add steerable-agent-protocol` | 1 — protocol |
| Add policy/budget/retry/tool-classification primitives | `uv add steerable-agent-harness` | 2 — harness |
| Add LLM providers + tool router + SSE/stdio transport | `uv add steerable-agent-runtime` | 3 — runtime |
| Embed a portable Python LLM runtime in a desktop app | `uv add steerable-sidecar` (Python side) — bundle the built binary on the host side | 3 — sidecar |
| React chat UI components + hooks | `pnpm add @steerable/agent-ui` | 4 — UI |

All TS packages also have a Python twin where it makes sense (`agent-protocol` is a 1:1 codegen pair). Lockstep versioning across all 7 packages — every release is `X.Y.Z` everywhere, enforced by CI.

---

## Design principles

- **Spec-first.** All cross-language types are generated from `spec/*.schema.json`. The drift checker fails CI if hand-edits sneak into generated files.
- **No layer leak.** Tier 1 (protocol) doesn't import Tier 2; Tier 2 (harness) doesn't import Tier 3; Tier 3 (runtime) doesn't import Tier 4 (UI). You can adopt any layer without inheriting the ones above it.
- **Pre-1.0, breaking changes go in the minor.** `0.X` is the breaking-change axis until the API is stable for at least one full minor cycle.

---

## Learn more

- **[Getting Started](./docs/getting-started.md)** — full walkthrough, ~5 minutes
- **[Specs](./docs/spec/)** — wire-level reference for every event shape
- **[Examples](./examples)** — `py-minimal`, `ts-minimal`, `sidecar-roundtrip` — all run via one command
- **[Storybook](https://pathlyapp.github.io/steerable-framework/storybook/)** — every UI component, live, with a11y + visual regression baselines

---

## Contributing / framework dev

```bash
pnpm install
uv sync --all-packages
pnpm gen          # regenerate TS+Py types from spec/
pnpm test
uv run pytest
```

Working on Steerable alongside a downstream consumer (deeppath, deeppath-api, deeppath-agent)? Read [`INTEGRATION-TESTING.md`](./INTEGRATION-TESTING.md) — covers the local toggle scripts that flip each repo between published-registry mode and sibling-source mode.

Cutting a release? Read [`RELEASING.md`](./RELEASING.md) — `bump_to.sh X.Y.Z → tag → push` is the entire flow.

License: Apache-2.0.
