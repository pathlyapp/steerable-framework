# Getting Started

> Boot the framework end-to-end in under 5 minutes. By the end you will have
> the protocol types in your project, a running sidecar speaking JSON-RPC, and
> a working `agent.chat.stream` round-trip.

## Prerequisites

| Tool      | Version | Why                                                              |
| --------- | ------- | ---------------------------------------------------------------- |
| Node.js   | ≥ 22    | TypeScript packages, Vitest                                      |
| pnpm      | ≥ 10    | Workspace + dependency resolution                                |
| Python    | ≥ 3.10  | Harness, runtime, sidecar packages                               |
| uv        | latest  | Fast Python dependency manager (replaces pip/virtualenv flow)    |

## 1 · Install the packages

### Python (server-side or sidecar embedder)

```bash
# Either pull from PyPI:
uv add steerable-agent-protocol steerable-agent-harness steerable-agent-runtime

# Or, for sidecar consumers:
uv add steerable-sidecar
```

### TypeScript (web app or Electron renderer)

```bash
pnpm add @steerable/agent-protocol @steerable/agent-ui
```

The TypeScript Tier-2 facade (`@steerable/agent-harness`) is optional — it
exists mainly for cross-language conformance tests. Production callers should
either (a) stay on the protocol layer for browser-only typing, or (b) call the
Python harness via the sidecar.

## 2 · Hello, ToolCall (Python)

The smallest useful program — classify a tool, consume budget, mark done.

```python
from steerable_agent_protocol import ToolCall, ToolResult, SSEEvent
from steerable_agent_harness import (
    BudgetLimit,
    BudgetState,
    consume_budget,
    decide_tool_mode,
    is_terminal_result,
)

call = ToolCall(id="call_1", name="read_file", arguments={"path": "README.md"})
mode = decide_tool_mode(call.name)         # → "read"

state, exhausted = consume_budget(
    BudgetState(),
    BudgetLimit(max_tokens=5_000, max_steps=30, max_tool_calls=10),
    tokens=120, step=True, tool_call=True,
)

result = ToolResult(success=True, message=f"mode={mode}", data={"tokens_used": state.tokens_used})
done = is_terminal_result(result.model_dump())

print(SSEEvent(type="done" if done else "tool_result",
               payload={"callId": call.id, "result": result.model_dump()}))
```

## 3 · Hello, ToolRouter (Python — Tier 3)

Same idea but with a registered tool that actually executes:

```python
import asyncio
from steerable_agent_protocol import ToolCall
from steerable_agent_runtime import ToolRouter, tool

router = ToolRouter()

@tool(router=router, description="Read a file")
async def read_file(path: str) -> dict:
    return {"path": path, "content": "Hello!"}

async def main() -> None:
    result = await router.dispatch(ToolCall(id="c1", name="read_file", arguments={"path": "README.md"}))
    print(result.success, result.data)  # True {'path': 'README.md', 'content': 'Hello!'}

asyncio.run(main())
```

The `@tool` decorator auto-classifies via `decide_tool_mode("read_file")` →
`"read"` (read-only); see [Tools spec](spec/tools.md) for the rules.

## 4 · Hello, Sidecar (Electron / shell embedder)

Spawn the sidecar from any language that can fork a subprocess and speak
[JSON-RPC over stdio](spec/sidecar.md):

```ts
import { SidecarSupervisor } from '@steerable/sidecar-bridge';
// ↑ Or implement the 4 lines yourself: spawn `python -m steerable_sidecar`,
//   wait for `__SIDECAR_READY__:{json}` on stderr, frame JSON-RPC on stdin/stdout.

const supervisor = await SidecarSupervisor.start({
  pythonExecutable: '/path/to/portable-python',
  bootTimeoutMs: 30_000,
});

const health = await supervisor.ping();          // { status: 'ok', version: '0.1.0', protocolVersion: '0.1.0', … }
const tools  = await supervisor.listTools();     // []  (no tools registered yet)

await supervisor.streamChat(
  {
    provider: 'openai_compat',
    model: 'gpt-4o-mini',
    apiKey: process.env.OPENAI_API_KEY!,
    messages: [{ role: 'user', content: 'Say hi' }],
  },
  {
    onChunk: c => process.stdout.write(c.delta ?? ''),
    onDone: () => console.log('\n[done]'),
    onError: e => console.error(e),
  },
);

await supervisor.shutdown();
```

The `agent.chat.stream` notification → `stream.chunk` flow is the canonical way
to pull LLM output back through the sidecar without going through HTTP.

## 5 · Hello, UI (React / browser)

```tsx
import { ChatPanel, useChatStream } from '@steerable/agent-ui';
import '@steerable/agent-ui/tailwind-preset.css'; // or use the preset in tailwind.config.js

function MyChat() {
  const { messages, isStreaming, sendUserMessage, cancel } = useChatStream({
    transport: {
      stream: async (input, onEvent) => {
        const res = await fetch('/api/v2/chats/' + chatId + '/send', {
          method: 'POST',
          body: JSON.stringify({ message: input.content }),
        });
        // Parse SSE → call onEvent({ type: 'content', content: '…' }) etc.
      },
    },
  });

  return (
    <ChatPanel
      messages={messages}
      isStreaming={isStreaming}
      onSubmit={sendUserMessage}
      onCancel={cancel}
    />
  );
}
```

`useChatStream` is intentionally transport-agnostic — fetch+SSE in the browser,
WebSocket in your dev panel, IPC in Electron, sidecar JSON-RPC in a desktop
shell. The hook reduces protocol `SSEEvent`s onto a `ChatMessage[]` regardless.

## 6 · Run the local dev preview

The DeepPath repo (one of the framework's reference consumers) ships a
**dev-only preview page** at `/dev/framework-preview` that mounts the
framework's `ChatPanel` + `useChatStream` + `SSEStreamView` against the real
backend. It's the recommended way to validate framework upgrades before
swapping more of your production UI:

```bash
cd deeppath/apps/web
pnpm dev
open http://localhost:3000/dev/framework-preview
```

## What next?

- Read the [Spec Overview](spec/overview.md) to understand the cross-language contract pipeline
- Read the [Architecture](spec/architecture.md) page to understand the Tier 1–4 boundary
- Browse [`examples/`](https://github.com/steerable-org/steerable-framework/tree/main/examples) for runnable starter projects
- Migrating an existing DeepPath-internal agent? See the [migration guide](migration/deeppath.md)
