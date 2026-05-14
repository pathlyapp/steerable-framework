# Steerable Framework

> An open-source, spec-first agent framework extracted from the DeepPath
> ecosystem. Build AI agents in Python (server) and TypeScript (browser/Electron)
> with a single source-of-truth wire protocol, a deterministic harness, a
> pluggable runtime, and a portable sidecar binary that lets desktop apps share
> 100% of their agent business logic with their web backend.

## Why Steerable

Most agent frameworks are either **single-language** (lock you out of the
desktop / browser) or **stitched together at the API level** (your web app and
your Python server drift apart within weeks). Steerable's spec-first design
means:

- One JSON Schema → Python Pydantic + TypeScript types + runtime validators
- Drift between languages is detected by CI, not by users
- Web (TS), API (Py), and desktop (Electron + Py sidecar) all consume the **same** primitives

## Architecture (Tier 1 → Tier 4)

```
┌────────────────────────────────────────────────────────────────────┐
│  Tier 1 — Protocol (TypeScript + Python, lock-step versions)       │
│  @steerable/agent-protocol  ·  steerable-agent-protocol            │
│  SSEEvent · ChatMessage · ToolCall · ToolResult · AgentSession ·   │
│  HarnessTrace · TraceSpan · SidecarRequest/Response/Notification   │
└────────────────────────────────────────────────────────────────────┘
                               ▲
                               │  (shared types)
                               │
┌────────────────────────────────────────────────────────────────────┐
│  Tier 2 — Harness (Python — single source of truth)                │
│  steerable-agent-harness                                           │
│  Policy · Budget · Retry · Completion · Tracing · Safety           │
│  (Thin TS facade @steerable/agent-harness exists for parity tests) │
└────────────────────────────────────────────────────────────────────┘
                               ▲
                               │
┌────────────────────────────────────────────────────────────────────┐
│  Tier 3 — Runtime (Python only)                                    │
│  steerable-agent-runtime                                           │
│  LLMProvider (OpenAI-compat / Anthropic / Ollama)                  │
│  ToolRouter (decorator + dispatch + safety)                        │
│  StorageAdapter (InMemory / SQLAlchemy)                            │
│  TransportAdapter (FastAPI SSE / Stdio JSON-RPC)                   │
└────────────────────────────────────────────────────────────────────┘
                               ▲
                               │
┌────────────────────────────────────────────────────────────────────┐
│  Tier 3 — Sidecar (Python executable, shipped as portable CPython) │
│  steerable-sidecar                                                 │
│  JSON-RPC over stdio · ready marker · graceful shutdown ·          │
│  agent.chat.stream · tool.invoke · agent.session.* · trace.fetch   │
└────────────────────────────────────────────────────────────────────┘
                               ▲
                               │  (spawned by Electron)
                               │
┌────────────────────────────────────────────────────────────────────┐
│  Tier 4 — UI (TypeScript)                                          │
│  @steerable/agent-ui                                               │
│  React hooks: useChatStream · useToolCallStatus · useAgentSession  │
│  Components: ChatPanel · MessageList · OrchestrationPlanCard ·     │
│              ToolCallRenderer · SSEStreamView                      │
│  Tailwind preset (CSS variables, dark-mode aware)                  │
└────────────────────────────────────────────────────────────────────┘
```

The two top-tier consumers (a web app + a desktop Electron app) load **the same
agent-protocol types**, so any change to a wire type either flows through CI
codegen or is rejected by drift checks before it ships.

## What's in v0.1.0

| Package                       | Tier | Status   |
| ----------------------------- | ---- | -------- |
| `@steerable/agent-protocol`   | 1    | Released |
| `steerable-agent-protocol`    | 1    | Released |
| `@steerable/agent-harness`    | 2    | Facade   |
| `steerable-agent-harness`     | 2    | Released |
| `steerable-agent-runtime`     | 3    | Released |
| `steerable-sidecar`           | 3    | Released |
| `@steerable/agent-ui`         | 4    | Released |

## Get started in 5 minutes

→ [**Getting Started**](getting-started.md)

## Specs

- [Spec Overview](spec/overview.md)
- [Architecture (Tier 1–4)](spec/architecture.md)
- [Events](spec/events.md)
- [Tools](spec/tools.md)
- [Chat](spec/chat.md)
- [Safety](spec/safety.md)
- [Runtime: AgentSession + HarnessTrace](spec/runtime.md)
- [Sidecar: JSON-RPC over stdio](spec/sidecar.md)

## Examples

- [`examples/py-minimal/`](https://github.com/steerable-org/steerable-framework/tree/main/examples/py-minimal) — Python protocol + harness + tool dispatch
- [`examples/ts-minimal/`](https://github.com/steerable-org/steerable-framework/tree/main/examples/ts-minimal) — TypeScript protocol consumer
- [`examples/sidecar-roundtrip/`](https://github.com/steerable-org/steerable-framework/tree/main/examples/sidecar-roundtrip) — Spawn sidecar, run a chat-stream end-to-end

## Migrating from DeepPath internals

→ [DeepPath → Steerable migration guide](migration/deeppath.md)
