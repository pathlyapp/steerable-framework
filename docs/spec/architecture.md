# Architecture (Tier 1 → Tier 4)

Steerable is intentionally **layered**. Each tier has a single
responsibility and a small surface, so consumers can swap or replace any
layer without leaking concerns into the layers above and below.

## Why four tiers, not "an SDK"

Most agent frameworks ship a monolithic SDK that bundles wire types,
prompt construction, tool execution, storage, and React widgets into one
package. That makes the smallest examples fit on a slide — but couples
upgrades and forces you to take all-or-nothing dependencies. Steerable
splits the four concerns that change at very different rates:

| Tier | Changes when…                                | Versioning impact          |
| ---- | -------------------------------------------- | -------------------------- |
| 1    | Wire protocol evolves (new event type, etc.) | Lock-step npm + PyPI bump  |
| 2    | Harness rule changes (new policy mode, etc.) | PyPI bump (TS facade auto) |
| 3    | Runtime adapter added (new LLM provider)     | Independent PyPI bump      |
| 4    | UI components added / refactored             | Independent npm bump       |

## Tier 1 — Protocol

**Packages:** `@steerable/agent-protocol` · `steerable-agent-protocol`

Pure data types. No I/O, no logic, no LLM client code. Defined in JSON
Schema under `spec/`, codegen produces:

- TypeScript: `interface`s + Zod validators
- Python: `pydantic.BaseModel` classes

Drift between the two SDKs is **detected by CI** (`scripts/check_ts_drift.mjs`
+ `scripts/check_drift.py`) so a change to the schema can never produce
inconsistent types in production.

Lock-step version — both SDKs always release at the same version.

## Tier 2 — Harness

**Packages:** `steerable-agent-harness` (Python — source of truth) ·
`@steerable/agent-harness` (TypeScript — thin parity facade)

Pure functions over Tier 1 types. No I/O, no I/O-adjacent state. Topics
covered:

- `policy.decide_tool_mode(name)` — read / safe_write / destructive / local
- `budget.consume_budget(...)` — token / step / tool-call accounting
- `retry.next_retry_delay_ms(...)` — exponential-backoff helpers
- `completion.is_terminal_result(...)` — should the loop stop?
- `tracing.TraceSpan` — span data structure (storage is Tier 3)
- `safety_patterns.classify_shell_command(...)` — shell command risk grading

Why Python is canonical: the harness is consumed by the FastAPI server
(`deeppath-api`), the sidecar runtime, and the optional in-process Tier 3
runtime. The TypeScript facade exists so cross-language conformance
tests can verify both SDKs answer identically against the
`tests/conformance/cases/` golden inputs — but no production TS code
needs the harness directly today.

## Tier 3 — Runtime

**Package:** `steerable-agent-runtime` (Python only)

Adapter interfaces and reference implementations:

| Interface           | Reference implementations            |
| ------------------- | ------------------------------------ |
| `LLMProvider`       | `OpenAICompatProvider`, `AnthropicProvider` |
| `ToolRouter`        | In-process registry with `@tool`     |
| `StorageAdapter`    | `InMemoryStorage`, `SqlAlchemyStorage` |
| `TransportAdapter`  | `FastAPISseTransport`, `StdioJsonRpcTransport` |

Tier 3 deliberately **does not include** an "AgentLoop" class. Different
products have radically different orchestration semantics
(single-step vs. multi-step, with or without coordinator, plan vs. react,
…). The framework provides the primitives; how you compose them is your
business logic.

## Tier 3 — Sidecar (executable)

**Package:** `steerable-sidecar` (Python executable, packaged as portable
CPython via `python-build-standalone`)

A pre-wired JSON-RPC server that composes Tier 1 + 2 + 3 into a binary
that any UI shell can spawn. Wire format documented at
[Sidecar spec](sidecar.md). Used today by the Electron desktop app
(`deeppath-agent`) so it can share 100% of its agent business logic with
the FastAPI backend without forking.

Bundle size: < 300 MB per platform after stdlib stripping (CI enforced).

## Tier 4 — UI

**Package:** `@steerable/agent-ui`

Headless React hooks + components, Tailwind preset. Designed so a single
React tree can mount either:

- a transport that hits HTTP+SSE (web app)
- a transport that uses Electron IPC bridged to the sidecar (desktop)

without any component-level changes.

## Data flow at runtime

```
        ┌─────────────────┐
        │   User input    │
        └────────┬────────┘
                 │
            ChatMessage[user]
                 │
                 ▼
       ┌──────────────────┐         ┌──────────────────────┐
       │   useChatStream  │ ◀────── │  agent-protocol      │
       │   (Tier 4)       │         │  SSEEvent stream     │
       └──────┬───────────┘         └──────────────────────┘
              │
              │  fetch / IPC / sidecar JSON-RPC
              │
              ▼
       ┌──────────────────┐
       │  TransportAdapter│  ──── ToolCall ────►  ToolRouter (Tier 3)
       │  (Tier 3)        │  ◀─── ToolResult ───
       └──────┬───────────┘
              │
              │  consume_budget / decide_tool_mode (Tier 2)
              │
              ▼
       ┌──────────────────┐
       │   LLMProvider    │  ──► Anthropic / OpenAI / Ollama / …
       │   (Tier 3)       │
       └──────────────────┘
```

## Why Python-only Tier 2 / 3

This was a project pivot decision; see
[Migration guide § all-py-sidecar](../migration/deeppath.md). Short
version:

1. The web app and the FastAPI server already shared the protocol types.
2. The desktop app needed to share **business logic**, not just types.
3. Maintaining two implementations of policy/budget/retry/tools/storage
   in TS+Py guaranteed drift.
4. Embedding a portable Python sidecar in Electron lets the desktop app
   call the same harness + runtime the server uses, with zero drift.

The TS facade for Tier 2 stays only as a parity test surface — your
production TS code should not import from it.
