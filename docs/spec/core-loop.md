# CoreLoop Spec

Status: **implemented.** `CoreLoop` lives in `steerable-agent-runtime`
(`loop.py`) and is the production loop for the sidecar, the desktop agent,
and the eval harness. This page records the seam design it implements. The
design was written **against deeppath-api's requirements** (the larger
surface) so the interface is right before the server adopts the loop. See
`CORELOOP_TODO.md` for the migration plan.

The two traps this spec exists to avoid:

1. **Do not write CoreLoop in TypeScript.** Tier 2/3 are Python-only; the TS
   harness is a parity-test surface. A TS CoreLoop would be a fourth
   implementation deeppath-api can never adopt.
2. **Do not size the event taxonomy to deeppath-agent's 8 SSE shapes.**
   deeppath-api emits ~100 structured payloads; the `LoopEvent` taxonomy below
   is derived from *that* superset so the server can adopt the loop later.

## What CoreLoop is / is not

CoreLoop is the **single-agent step loop**: the think → act → observe cycle
that runs one agent turn (and its tool rounds) to a completion decision.

It is **not** orchestration. Multi-agent planning, DAG workers, groupchat, and
collaboration stay above it. Proof the seam exists: deeppath-api's
`orchestrator.py` already drives `HarnessLoop` as a black-box worker — it
constructs it, drains its stream, and re-frames the output, and the loop has
no "I am a worker" flag. CoreLoop formalizes that boundary.

## Layering

```
┌ product (deeppath-api / deeppath-agent) ────────────────────────┐
│  DeepPathHarnessLoop / desktop adapter                          │
│  wires: ToolExecutor handlers, MessageBuilder, GoalVerifier,    │
│  BillingSink, product emitters (dp-action, UI tools, citations) │
├─────────────────────────────────────────────────────────────────┤
│  steerable-agent-runtime · CoreLoop                             │
│  - inner tool-round loop + outer verifier/compact loop          │
│  - LLM stream consume (via LLMProvider)                         │
│  - pseudo / inline tool-call recovery                           │
│  - budget counters, soft-timeout, compaction-continue           │
│  - yields LoopEvent (structured, NOT bytes)                     │
├─────────────────────────────────────────────────────────────────┤
│  orchestration (unchanged, above the loop)                      │
└─────────────────────────────────────────────────────────────────┘
```

## LoopEvent taxonomy

The loop yields **structured events**, never encoded bytes. A
`TransportAdapter` (e.g. `FastAPISseTransport`, or the sidecar's stdio
JSON-RPC) encodes them for the wire. Derived from deeppath-api's ~100 emit
sites, grouped into five categories:

| Category | Representative events | Product-specific? |
| -------- | --------------------- | ----------------- |
| **Lifecycle** | `stage_start`, `stage_complete`, `retry` (stream resume hint), `error` | No — generic |
| **Content stream** | `content_delta`, `reasoning_delta`, `response_open`, `response_close` | Envelope tags (`<response>`, `<think title>`) are product-rendered; the *deltas* are generic |
| **Tool side** | `tool_call_start`, `tool_call_result`, `tool_error`, `tool_proposal`, `executed_actions` | `tool_proposal` payload (dp-action) is product; the *event kind* is generic |
| **Grounding / evidence** | `search_status`, `citations`, `web_search_sources` | Payload is product; kinds generic |
| **Budget / control** | `budget_note`, `budget_exhausted`, `completion` (status + reason) | No — generic |

Design rule: **the event *kind* and its envelope are framework-owned; the
*payload* may carry product fields** (extra keys allowed, consumers ignore
unknowns — same `additionalProperties` stance as the protocol envelopes).

deeppath-agent's 8 wire shapes map onto this taxonomy as a subset
(`content_delta`, `user_message`, `executed_actions`, `budget_exhausted`,
`completion`, `message_id`, `error`, `done`).

## ToolExecutor port

Today deeppath-api's `_run_tool_calls` is a ~750-line if/elif over ten
branches. CoreLoop replaces dispatch with a single port; cross-cutting
concerns (dedup, policy gate, budget) stay *in* the loop, and each branch
becomes a registered handler.

```python
class ToolExecutor(Protocol):
    async def execute(
        self, call: ToolCall, ctx: LoopContext
    ) -> AsyncIterator[LoopEvent | ToolResult]: ...
```

The ten branches map to handlers the product registers:

| Branch (deeppath-api) | Handler kind | Stays where |
| --------------------- | ------------ | ----------- |
| duplicate-call guard | loop-internal | CoreLoop |
| unknown-tool error + suggestions | loop-internal | CoreLoop |
| policy gate (`decide_tool_permission`) | loop-internal (uses Tier 2) | CoreLoop |
| collaboration tool handler | injected handler | product (orchestration) |
| UI tools (`ask_user`, …) | injected handler | product |
| synthetic tools (`web_search`, …) | injected handler | product |
| read tools (MCP) | injected handler | product |
| external tools (MCP / desktop relay) | injected handler | product |
| auto-local tools (desktop relay) | injected handler | product |
| local / approval-gated → proposal | injected handler | product |
| write tools (guardrails + MCP) | injected handler | product |

For deeppath-agent the same port is how **remote** tools plug in: a handler
that forwards `local_exec_shell` / file ops back to Electron over the sidecar
reverse channel (see `A1` in `CORELOOP_TODO.md`).

## Product hooks (NOT sunk into the framework)

These stay in the product and are injected via `LoopConfig` / ports:

- `<dp-action>` proposal formatting and `guardrails` validation
- UI tools and the `<response>` / `<think title>` frontend contract
- `context_system` tier assembly and Tier-2 citation registration
- goal verifier (inner + outer) — CoreLoop exposes a `GoalVerifier` hook,
  default no-op
- skill-based budget overrides
- token billing, user-timezone conversion, entity-title DB lookup
- desktop-agent relay, Redis context-cache invalidation

## Sunk into the framework (generic mechanics)

- inner/outer loop state machine and round control
- LLM stream consumption, UTF-16 surrogate fix, reasoning extraction
- pseudo / inline / markdown tool-call recovery
- budget counters, soft-timeout, compaction-continue, round extension
- per-tool timeout (`LoopConfig.tool_timeout_ms`): a hung tool returns a
  failed `ToolResult` (`tool_timeout`) instead of hanging the turn —
  `soft_timeout_ms` only gates round boundaries, so without this a dead
  remote executor hangs forever
- large-result externalization to artifacts
- tool dedup, unknown-tool suggestion, arg schema coercion
- deeppath-agent's anti-hallucination layer (data-need routing, grounding
  judge, deferred/claimed retry, narration round) — **generic loop mechanics
  the server currently lacks; sinking them is a net gain, not a wash**

## Known semantic divergences to resolve at adoption time

Recorded here so they are a decision, not a surprise:

- **`maxToolErrors` semantics differ.** deeppath-api counts *cumulative* tool
  errors (completion threshold 2, budget breaker default 3 — internally
  inconsistent). deeppath-agent counts *consecutive* errors, reset on success
  (threshold 3), deliberately — cumulative punished tasks that recovered.
  CoreLoop must pick one semantics (recommend consecutive) and make the
  threshold configurable.
- **Token budget defaults differ by design.** api 120k (server models, large
  context), agent 60k (local models, small context). Keep configurable; do
  not force one number.
