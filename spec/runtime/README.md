# `spec/runtime/`

Runtime persistence contracts shared between **steerable-agent-runtime** (Tier 3) and any
storage adapter (in-memory, SQLAlchemy, etc.). They are also surfaced over the wire by
TransportAdapters that ship trace data to a UI.

| Schema | Purpose | Lifetime |
| --- | --- | --- |
| `AgentSession.schema.json` | One agentic conversation/FSM instance bound to a chat thread. | Long-lived (days/weeks) |
| `HarnessTrace.schema.json` | One harness execution (one user turn). | Per-turn |
| `TraceSpan.schema.json` | A timed step inside a HarnessTrace (LLM call, tool call, …). | Per-step |
| `TraceEvent.schema.json` | A point-in-time event inside a HarnessTrace. | Per-event |

## Identity rules

- `AgentSession.sessionId` is **visible to clients** and used for resume. `id` is the
  internal storage primary key.
- `HarnessTrace.traceId` is **stable for the lifetime of one run** (`tr_<hex>`).
- `TraceSpan.spanId` is unique **within its parent trace**, formatted `step_<n>` by
  default (1-indexed).
- `TraceEvent.sequence` is a **monotonic per-trace** integer; storage layers must enforce
  the `(traceId, sequence)` unique constraint.

## Secret redaction

Every `payload`, `attrs`, and `stageData` field is required to be **JSON-serializable
and secret-redacted** before persistence. Implementations should reuse
`steerable_agent_harness.tracing.sanitize_for_trace()` (or equivalent) to scrub
`password`, `token`, `secret`, `api_key`, `authorization`, `credential` keys.

## Source of truth

These schemas were extracted from the production models in
`deeppath-api/app/models/{agentsession,harness_trace,harness_trace_event}.py` and the
runtime accumulator in `deeppath-api/app/services/harness/tracing.py`. Once Tier 3 lands,
`steerable-agent-runtime` will own them and `deeppath-api` will become a thin consumer.
