# @steerable/agent-runtime

The official TypeScript production runtime for the Steerable framework.

Python is the only production implementation of the framework's harness and
CoreLoop. This package is how a pure-TypeScript product runs it in
production: it owns the embedded Python sidecar process (spawn, ready
handshake, health ping, bounded auto-restart, graceful drain) and exposes
the CoreLoop-level API over the sidecar's JSON-RPC surface. You never write
subprocess management, and because the TS runtime drives the same Python
CoreLoop the server uses, there is no second implementation to drift.

## Install & prerequisites

```sh
npm install @steerable/agent-runtime
```

A Python ≥3.11 environment with `steerable-sidecar` installed must be
reachable. Point at it with `STEERABLE_PYTHON` or the `python` option.

## Quick start

```ts
import { AgentRuntime } from '@steerable/agent-runtime';

const runtime = new AgentRuntime({
  tools: {
    // Host tools the CoreLoop may call back over the reverse channel.
    read_file: async ({ path }) => ({ success: true, result: await readFile(String(path), 'utf8') }),
  },
});
await runtime.start();

const session = await runtime.createSession({ chatId: 'chat-1', userId: 'me' });
const turn = await runtime.chatStream({
  provider: 'openai_compat',
  model: 'deepseek-chat',
  baseUrl: process.env.LLM_BASE_URL,
  apiKey: process.env.LLM_API_KEY,
  messages: [{ role: 'user', content: 'hello' }],
});

for await (const event of turn.events) {
  // content | tool_call | tool_result | usage | orchestration | done | error
}
const { status, cancelled } = await turn.done;

await runtime.close();
```

## API map

| Runtime API | Sidecar RPC |
| --- | --- |
| `start()` / `close()` | process lifecycle + `system.shutdown` |
| `ping()` | `system.ping` |
| `createSession` / `resumeSession` / `listSessions` | `agent.session.*` |
| `forkSession` / `sessionBranches` | `agent.session.fork` / `agent.session.branches` |
| `sessionMessages` | `agent.session.messages` |
| `chatStream` / `cancelChat` / `steerChat` / `forkChat` | `agent.chat.*` |
| `listTools` / `invokeTool` | `tool.list` / `tool.invoke` |
| `listSkills` | `skills.list` |
| `applyEdits` | `workspace.apply_edits` |
| `fetchTrace` / `exportTrace` | `trace.fetch` / `trace.export` |
| `getConfig` / `setConfig` | `config.get` / `config.set` |

The full wire contract is [docs/spec/sidecar.md](../../../docs/spec/sidecar.md).
A CI gate (`test/surface.test.ts`) asserts this surface matches the methods
the Python sidecar registers — the two cannot drift.

`chatStream` defaults `useCoreLoop: true` on the request: the runtime's
API (cooperative cancel, steer, orchestration) only exists on the sidecar's
CoreLoop path. Pass `useCoreLoop: false` to opt into the legacy
direct-stream path explicitly.

## Testing

Unit tests run against `test/fake-sidecar.mjs` (a Node script speaking the
wire protocol). `test/e2e-real-sidecar.test.ts` is a true end-to-end gate:
it spawns the **real** Python sidecar from the repo's uv venv and drives a
full tool-calling turn — CoreLoop → OpenAI-compatible HTTP → local mock
server → reverse-channel `tool.invoke` → second LLM round — plus a
cooperative-cancel run. It self-skips when `.venv` cannot import
`steerable_sidecar` (run `uv sync` at the repo root to enable).

## React UI

`@steerable/agent-ui` hooks consume the runtime directly:

```tsx
import { createChatStreamTransport, createSessionTransport } from '@steerable/agent-runtime';
import { useChatStream, useAgentSession } from '@steerable/agent-ui';

const sessionTransport = createSessionTransport(runtime);
const chatTransport = createChatStreamTransport(runtime, {
  sessionId: () => currentSessionId,
  params: { provider: 'openai_compat', model: 'deepseek-chat' },
});
```

## Lifecycle semantics

- **Ready handshake** — `start()` resolves only after `lifecycle.ready` and a
  `system.ping` round-trip; a half-booted process never answers requests.
- **Auto-restart** — an unexpected exit of a *fully booted* process triggers
  up to `maxRestarts` restarts with exponential backoff; boot failures are
  not retried (they are almost always deterministic). Requests made while
  down reject fast with `SidecarNotReadyError`.
- **Graceful close** — `close()` sends `system.shutdown`, waits, then
  escalates SIGTERM → SIGKILL. A close during a restart cycle suppresses the
  pending restart.
- **Cancellation** — `cancelChat(streamId)` is cooperative: the CoreLoop
  winds down at the next safe point and the terminal `done` event still
  arrives with `status: 'cancelled'`.
