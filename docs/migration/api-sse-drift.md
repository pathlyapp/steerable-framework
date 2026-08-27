# DeepPath API SSE Drift Survey (A5, 2026-08-27)

Read-only survey of `deeppath-api`'s agent-stack SSE emission surface, measured
against the [CoreLoop port spec](../spec/core-loop.md) (A2). Inputs to the
"sidecar protocol v1 freeze" decision. No `deeppath-api` code was changed.

## Headline numbers

| Metric | A0 estimate | Measured |
| --- | --- | --- |
| Agent-stack emission sites | ~100 | **114** |
| Distinct wire `type` values (agent chat) | — | **~18** |
| Collaboration event names | — | **~15** (one multiplexer) |
| Live orchestration events | — | **5** (+ 2 dead helpers) |
| Peripheral SSE streams (KG / memories / docs / mgmt news) | — | 35 sites, **out of taxonomy scope** |

Sites by file: `loop.py` 51 · `entrypoint.py` 25 · `orchestration_dispatch.py` 11
· `shared/resumable.py` 11 · `orchestrator.py` 8 · `collaboration_runtime.py` 4
· `shared/compression.py` 3 · `chat_title_stream.py` 1.

The frontend consumer contract (`ChatUIProvider.tsx`) dispatches on ~20 `type`
values. That contract is frozen — adoption must reproduce it byte-compatibly.

## Drift vs the port spec

### 1. Tools have no structured wire events (major)

The spec's "Tool side" category assumed `tool_call_start` / `tool_call_result` /
`tool_proposal` events exist. On the api wire they do not:

- Tools surface as **content tags** (`<dp-action …/>`, `<ask-user>…`,
  `<analysis>…`) inside `{"content": …}` frames, plus `executed_actions` JSON
  frames (full list at end-of-run + incremental pushes for writes).
- `type:"tool-proposal"` is **handled by the frontend but never emitted by the
  backend** — dead wire handling; proposals are `<action>…</action>` content
  wraps rendered at `entrypoint.py`.
- Consequence for adoption: CoreLoop emits structured `tool_call_start` /
  `tool_call_result` internally. The api transport must **render them back to
  content tags** for byte compatibility. Do not add new frontend branches during
  the freeze window.

### 2. Budget / completion: the framework is richer than the wire

- Soft budget stops are **content notes** inside `<response>`; there is no
  `budget_exhausted` frame in single-agent chat (only
  `collaboration/budget_exhausted` with a `TeamBudgetSnapshot`).
- The terminal frame is a raw `data: "[DONE]"`; completion status/reason is not
  on the wire.
- Adoption is a **net gain** here: structured `budget_exhausted` / `completion`
  events exist in CoreLoop today. The transport can keep emitting `[DONE]`
  while persisting the structured reason server-side.

### 3. Evidence trio maps cleanly, naming convention differs

`search-status` / `citations-update` / `web-search-sources` are product-payload
events exactly as the spec predicted — but api uses **kebab-case** type names
while the framework LoopEvent taxonomy uses **snake_case** kinds. The transport
owns the translation; the taxonomy does not bend.

### 4. Lifecycle maps, envelope differs

`agent/stage_start` + `agent/stage_complete` (only `stage:"execute"` is live)
map onto `stage_start` / `stage_complete`. The `retry:` SSE directive is
transport-level, not an event. Compression lifecycle
(`agent/compression_start|done|failed`, carrying `summaryMessageId` /
`summaryContent`) maps onto `hook_action{action:"compact"}` **with a product
payload** — the first concrete case of the spec's "kind is framework-owned,
payload may carry product fields" rule.

### 5. Orchestration / collaboration stay above the loop (confirmed)

- 5 live orchestration events (`plan_ready`, `task_start`, `task_chunk`,
  `task_done`, `done`); `sse_orchestration_planning` / `…_plan_failed` are
  **dead helpers** (planning UX moved to `loader-hint`).
- `orchestrator.py` runs a **passthrough rewriter**: worker frames get
  `orchestrationGroupId` + `taskId` grafted; worker `agent` / `trace` /
  `message_id` / keepalive / `[DONE]` frames are **dropped**; bare `content`
  becomes `orchestration/task_chunk`.
- Collaboration emits ~15 event names through **one multiplexer** with an
  already-versioned envelope (`version:1`, `visibility`, `runId`).
- Neither enters CoreLoop. They need a **product event channel** on the
  transport, not taxonomy additions.

### 6. Content stream is 54 sites, one kind

Every `sse_content_delta` (think/response/wait markers, LLM text, UI tags,
inline dp-action cards, budget notes, prep progress) collapses to
`content_delta`. Reasoning is **not** separated on the api wire (`<think>` is
embedded in content); CoreLoop's `reasoning_delta` is an adoption gain the
transport may re-embed for compatibility.

### 7. Resume infrastructure is api-only

`stream_resumable` (Redis Streams) detaches generation from the HTTP
connection; clients resume via `GET /chats/{id}/stream`. Two adoption-relevant
findings:

- The draft writer **re-parses SSE bytes** (`_extract_content_delta`) to
  persist partial assistant text — a direct symptom of "the loop yields bytes".
  With structured events, drafts derive from the event stream; the re-parsing
  code is deleted.
- The `session` prelude frame (`{type:"session", streamId, sessionId}`) is a
  resume handle, not a loop event. It stays in the api transport.

### 8. Three event vocabularies have drifted apart

| Vocabulary | Status |
| --- | --- |
| api production wire (~18 types, mixed kebab/snake case) | The superset; load-bearing |
| `docs/spec/events.md` `SSEEvent` (10 variants) | **Stale** (P1-era): lists `tool_call` / `tool_result` variants api never emits; lacks orchestration / collaboration / evidence / `loader-hint` types |
| CoreLoop `LoopEvent` (13 kinds) | Current; desktop-validated |

The v1 freeze must re-derive `events.md` from the production wire superset.

## Adoption cost re-estimate

The A0 framing — "rewrite ~100 SSE emission points into structured events" —
overstates the manual work. Decomposed:

| Workstream | Size | Notes |
| --- | --- | --- |
| 1. CoreLoop adoption inside `HarnessLoop` | the real cost | loop.py's 51 sites disappear into the loop; the 10-branch `_run_tool_calls` becomes registered `ToolExecutor` handlers (spec's port table). This was always the A3-for-api cost — unchanged. |
| 2. `FastAPISseTransport` (new) | ~1 mapping table | LoopEvent → the ~18 existing wire types, byte-compatible. Owns: content-tag rendering rules, evidence trio translation, `hook_action` → `compression_*`, structured → `[DONE]` terminal. |
| 3. Orchestration / collaboration passthrough | ~23 sites keep their shapes | Product layer writes directly to the transport; the loop never sees these events. The orchestrator rewriter moves verbatim. |
| 4. Resume layer re-keying | delete + derive | Structured events into Redis directly; delete byte re-parsing. |

Net: the work is **O(20) shape mappings + one transport + one passthrough
channel**, not O(100) site rewrites. The loop-internal sites (51) and most
entrypoint prep UX (13 of 25) convert mechanically.

New risks found by this survey:

- **Tool-event rendering decision** (drift #1) is the only place where adoption
  could silently change user-visible output. Mitigation: golden-file replay of
  the transport against recorded production streams before cutover.
- **Dead code to delete at adoption, not before**: `sse_orchestration_planning`,
  `sse_orchestration_plan_failed`, frontend `tool-proposal` handling.
- **Semantic decisions already recorded in the spec** still gate adoption:
  `maxToolErrors` cumulative-vs-consecutive; token budget defaults (120k vs 60k).

## Sidecar protocol v1 — proposed freeze scope

Two adoption paths exist for api: **in-process import** of
`steerable-agent-runtime` (natural for FastAPI; primary) and **spawned sidecar**
(what the desktop does; optional for api). The freeze therefore covers two
layers, and api adoption binds only layer 1.

### Layer 1 — LoopEvent taxonomy (freeze for api)

- The 13 kinds as implemented: `stage_start`, `stage_complete`, `content_delta`,
  `reasoning_delta`, `tool_call_start`, `tool_call_result`, `tool_error`,
  `error`, `completion`, `budget_exhausted`, `soft_timeout`, `hook_action`,
  `steer`.
- Envelope rule (already in the port spec): kind + envelope are
  framework-owned; payloads are extensible (`additionalProperties` stance).
- **Product events do not enter the loop.** Orchestration / collaboration /
  evidence frames are written by the product layer to the transport directly.
  No generic `product` escape-hatch kind is added — the taxonomy stays closed,
  which keeps the desktop wire small.

### Layer 2 — sidecar JSON-RPC (freeze for desktop; api optional)

- Methods (15 as implemented): `system.ping`, `system.shutdown`,
  `system.shutdown_now`, `agent.session.create`, `agent.session.resume`,
  `agent.session.list`, `agent.chat.stream`, `agent.chat.cancel`,
  `agent.chat.steer`, `agent.chat.fork`, `tool.list`, `tool.invoke`,
  `trace.fetch`, `config.get`, `config.set`. The catalog in
  `spec/sidecar.md` lists 13 — it predates `agent.chat.steer` /
  `agent.chat.fork` and must be regenerated at freeze time.
- Notifications: `lifecycle.ready`, `lifecycle.shutdown`, `stream.chunk`
  (variants: `delta`, `reasoningDelta`, `toolCall`, `toolResult`, `notice`,
  `usage`), `stream.done`, `stream.error`.
- Reverse channel: `tool.invoke` with the `srv_` id-namespace rule.
- `protocolVersion`: bump `0.1.0` → `1.0.0` at freeze; additive-only changes
  afterwards, new fields optional, consumers ignore unknowns.

### Explicitly NOT frozen (still evolving)

- `skills` request param (A6, dogfooding) and the `skill` tool descriptor.
- `antiHallucination` flags (desktop-only need; api lacks the layer entirely —
  sinking it is a separate decision recorded in the port spec).
- `trace.fetch` payload internals (trace schema still grows with hooks).
- `spec/events.md` — to be **rewritten from the production superset** as part
  of the freeze PR, not treated as authoritative today.

### Decision points for the freeze PR (not decided here)

1. Byte-compat strategy for tool events: render `tool_call_*` back to content
   tags (recommended) vs. frontend learns structured tool frames (bigger blast
   radius).
2. Whether api's `agent/chat` HTTP routes ever proxy a spawned sidecar, or
   in-process import is the permanent answer (affects whether layer 2 needs an
   HTTP transport binding at all).
3. `maxToolErrors` semantics + token budget defaults (carried from the port
   spec).
