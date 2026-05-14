# Migrating from DeepPath internals

This page is for engineers migrating an existing DeepPath-internal agent
(in `deeppath/`, `deeppath-api/`, or `deeppath-agent/`) onto the
extracted `steerable-framework`. New users — read [Getting Started](../getting-started.md) instead.

## TL;DR

- Wire types: drop your local copies → import from `@steerable/agent-protocol` (TS) or `steerable-agent-protocol` (Py).
- Harness primitives (policy / budget / retry / completion / safety): drop your local helpers → import from `steerable-agent-harness` (Py canonical) or `@steerable/agent-harness` (TS facade).
- Runtime (LLM provider / tool router / storage / transport): adopt `steerable-agent-runtime` interfaces; in-process implementations stay in your repo.
- Desktop business logic: stop maintaining a TS twin — embed `steerable-sidecar` in Electron and call the Python harness/runtime over JSON-RPC. The TS UI in `deeppath-agent` keeps working unchanged.
- UI: opt in incrementally via `@steerable/agent-ui`. The reference cutover is the dev-only `/dev/framework-preview` page in `deeppath/apps/web`.

## Architectural pivot you should know about

The original extraction plan dual-implemented every tier in TS+Py. We
pivoted to **Python-only Tier 2/3 + TS-only Tier 4** because:

1. The web app (Next.js) and the FastAPI server already shared protocol types — the TS Tier 2 logic was just duplicated effort.
2. The Electron desktop app needed to share **business logic** (not just types) with the FastAPI server.
3. Maintaining two implementations of policy/budget/retry/tools/storage in TS+Py guaranteed drift over time.
4. Embedding a portable Python sidecar in Electron lets the desktop app call the same harness + runtime the server uses, with **zero drift**.

You'll see this reflected throughout the migration — the TS facade for
the harness still exists, but it's a parity-test surface, not a
production code path.

## Package mapping

| What you had (DeepPath-internal)                                       | What you import now                                              |
| ---------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `deeppath/apps/web/src/types/{sse,tool,chat}.ts`                       | `@steerable/agent-protocol` types                                |
| `deeppath-api/app/services/agentic/protocol/*`                         | `steerable_agent_protocol`                                       |
| `deeppath-api/app/services/agentic/harness/{policy,budget,retry,...}.py` | `steerable_agent_harness`                                       |
| `deeppath-api/app/services/agentic/runtime/*`                          | `steerable_agent_runtime`                                        |
| `deeppath-agent/src/harness/*` (TS)                                    | **Replaced** — call into the sidecar via `STEERABLE_USE_SIDECAR=1` |
| `deeppath-agent/src/llm/*` (TS)                                        | **Replaced** — `SidecarLlmProvider` proxies to Python            |
| `deeppath/apps/web/src/components/chat/ChatPanel.tsx`                  | **Future** — `@steerable/agent-ui` `ChatPanel`. Not yet swapped in production. |

## Step-by-step migration order

We did this in 8 phases (P1 → P8) and recommend the same shape for any
downstream consumer.

### 1. Imports only — protocol (1 PR per repo)

Replace every local definition of `SSEEvent`, `ToolCall`, `ToolResult`,
`ChatMessage`, `ChatAgent`, `CommandSafetyPattern`, `AgentSession`,
`HarnessTrace`, `TraceSpan`, `TraceEvent` with imports from the
respective protocol package.

```ts
// before
import type { SSEEvent } from '@/types/sse';
// after
import type { SSEEvent } from '@steerable/agent-protocol';
```

```python
# before
from app.services.agentic.protocol.sse import SSEEvent
# after
from steerable_agent_protocol import SSEEvent
```

### 2. Imports only — harness (Python first)

In `deeppath-api`, replace local `policy.py`, `budget.py`, `retry.py`,
`completion.py`, `safety_patterns.py` imports with the framework's:

```python
# before
from app.services.agentic.harness.policy import decide_tool_mode
# after
from steerable_agent_harness import decide_tool_mode
```

Keep the local file for one release as a re-export shim if you have
many call sites.

### 3. Runtime adapters

`steerable-agent-runtime` ships interfaces (`LLMProvider`,
`ToolRouter`, `StorageAdapter`, `TransportAdapter`) and **reference**
implementations. Your repo can:

- Use the references as-is for green-field code (e.g. `OpenAICompatProvider`).
- Subclass / replace them where you need product-specific behavior (e.g. your custom `SqlAlchemyStorage` schema). The interface is a `typing.Protocol` so duck-typing works.

The harness loop (`run_agent_step` etc.) doesn't live in the runtime —
that's intentionally left to your product. You compose protocol +
harness + runtime however your orchestration needs.

### 4. Desktop: enable the sidecar

In `deeppath-agent`, the cutover is gated behind an env var so we can
bake the change before flipping the default:

```bash
# Enable Python sidecar for LLM + harness business logic.
export STEERABLE_USE_SIDECAR=1
pnpm dev
```

When enabled, Electron's main process spawns
`python -m steerable_sidecar` (or the bundled portable CPython binary in
production builds) and routes:

- `LLMProvider` → `SidecarLlmProvider` → JSON-RPC `agent.chat.stream`
- `ToolRouter` → bridge to sidecar `tool.invoke`
- (Future) `StorageAdapter` → sidecar `agent.session.*`

The legacy in-process TypeScript `OllamaProvider` /
`OpenAICompatProvider` / `LocalToolRouter` remain wired as fallbacks
during the rollout. Remove them once the sidecar has shipped to your
canary users.

### 5. UI: incremental, not big-bang

`deeppath/apps/web/src/components/chat/ChatPanel.tsx` is a heavy
production surface (multi-agent, variants, automation hooks, loader
hints). Don't try to swap it in one PR. The pattern that worked:

1. Add `@steerable/agent-ui` as a `link:` dependency.
2. Add the framework's CSS variables to `globals.css` so its components inherit the existing Tailwind v4 theme.
3. Implement `framework-sse-bridge.ts` to convert your existing wire format to the framework's `SSEEvent`.
4. Ship a **dev-only** `/dev/framework-preview` page that mounts the framework's `ChatPanel` against the real backend SSE.
5. Iterate on the framework's UI surface using the preview as the canary.
6. Plan the production swap for v0.2.x once the framework `ChatPanel` covers the variants/multi-agent feature set.

## Naming differences (TS vs Py)

- TypeScript fields are camelCase (`maxToolCalls`, `needsFollowup`); Python pydantic models keep the same wire field names (no aliasing).
- Python helpers expose snake_case function names (`decide_tool_mode`); TypeScript wrappers use camelCase (`decideToolMode`). Both implementations answer identically against `tests/conformance/cases/`.

## Validation flow after migration

```bash
# Wire-level: regenerate codegen + drift check
pnpm gen
pnpm check:drift
uv run python scripts/generate_py.py
uv run python scripts/check_drift.py

# Tier 1+2 conformance against golden cases
pnpm -r test
uv run pytest tests/conformance/py

# Sidecar smoke test
uv run --package steerable-example-sidecar-roundtrip \
    python -m steerable_example_sidecar_roundtrip.main

# (Desktop) end-to-end against the embedded sidecar
cd ../deeppath-agent
pnpm test --filter sidecar
```

## Common gotchas

- **`additionalProperties` mismatches.** The protocol envelopes
  intentionally leave `additionalProperties: true` so old clients can
  ignore new fields. Don't add a `passthrough` plugin in your
  validators — it'll mask spec violations.
- **Don't import the TS harness facade in production.** It exists for
  parity tests only; production TS code should stay on the protocol
  layer or call the sidecar.
- **Sidecar boot timing.** `__SIDECAR_READY__:` is sent on **stderr**.
  Don't gate readiness on stdout activity — the first stdout byte is
  the response to your first JSON-RPC frame, which can deadlock against
  a parent waiting for a stdout signal.
- **Bundle size.** The Electron-bundled CPython is ~270 MB after
  pruning (CI enforces < 300 MB per platform). If you add Python deps,
  re-run `scripts/sidecar/measure_size.py` and verify the prune
  unit-test still passes.

## Reference deltas

The DeepPath repo's own framework adoption commits are tagged
`framework/p1-protocol`, `framework/p2-harness`, …, `framework/p7-ui`.
They're the canonical worked example for every step above.
