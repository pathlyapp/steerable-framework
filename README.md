# Steerable Framework

Open-source agent framework extracted from DeepPath ecosystem.

## Monorepo packages

| Package | Language | Tier | Purpose |
| --- | --- | --- | --- |
| `@steerable/agent-protocol` | TypeScript | 1 (protocol) | Wire types — SSE events, tool calls, chat messages, agent sessions, sidecar JSON-RPC |
| `steerable-agent-protocol` | Python | 1 (protocol) | Same wire types as Pydantic models |
| `@steerable/agent-harness` | TypeScript | 2 (harness facade) | Thin TS facade over the canonical Python harness — kept for cross-lang conformance tests |
| `steerable-agent-harness` | Python | 2 (harness) | Policy / Budget / Retry / Completion / Tracing primitives; single source of truth |
| `steerable-agent-runtime` | Python | 3 (runtime) | LLMProvider, ToolRouter, StorageAdapter, TransportAdapter (SSE / stdio JSON-RPC) |
| `steerable-sidecar` | Python | 3 (executable) | Portable Python sidecar binary; speaks JSON-RPC over stdio to a UI shell |
| `@steerable/agent-ui` | TypeScript | 4 (UI) | React hooks + headless components + Tailwind preset |

## Principles

- Spec-first: all cross-language contracts come from `spec/`.
- Lock-step release for protocol and harness npm/PyPI packages.
- Conformance tests verify TypeScript and Python behavior parity.

## Development

```bash
pnpm install
uv sync
pnpm gen
pnpm test
```

## End-to-end validation

The framework's UI layer is dogfooded inside the DeepPath monorepo via a
**dev-only preview page** that mounts `@steerable/agent-ui`'s `ChatPanel` +
`useChatStream` against the production SSE endpoint
(`/api/v2/chats/:id/send`). Open it at:

```
http://localhost:3000/dev/framework-preview
```

(only mounted when `NODE_ENV !== 'production'`). The page splits into a chat
panel on the left and a live `SSEStreamView` log on the right so you can watch
every `content` / `tool_call` / `tool_result` / `error` / `done` /
`budget_exhausted` event the framework receives. This is the canonical
"is the framework wired correctly?" check before we mirror more production
components into `@steerable/agent-ui` (P7+).
