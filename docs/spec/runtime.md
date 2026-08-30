# Runtime Spec

The runtime layer adds the data structures the framework needs to
**persist** an agent run across processes: agent sessions and harness
traces. The wire formats below are protocol-level (Tier 1); the
in-memory dispatch implementations live in `steerable-agent-runtime`
(Tier 3).

## AgentSession

A row that tracks "this user is in the middle of a multi-step run with
this chat / project / scenario". Used by the `agent.session.*` sidecar
methods and by `useAgentSession` on the UI side.

| Field          | Type                       | Required | Notes                                       |
| -------------- | -------------------------- | -------- | ------------------------------------------- |
| `sessionId`    | `string`                   | yes      | Globally unique                             |
| `userId`       | `string`                   | yes      | Owning user                                 |
| `chatId`       | `string`                   | yes      | Owning chat                                 |
| `currentStage` | `string`                   | yes      | Free-form stage tag (e.g. `PLANNING`)       |
| `nextStage`    | `string \| null`           | no       | Pre-computed next stage if any              |
| `scenario`     | `string`                   | no       | Product-defined scenario tag                |
| `stageData`    | `Record<string, unknown>`  | no       | Stage-specific scratch state                |
| `isActive`     | `boolean`                  | yes      | False once the run terminates               |
| `projectId`    | `string \| null`           | no       | Owning project                              |
| `id`           | `string`                   | no       | DB row id (storage-implementation specific) |
| `createdAt` / `updatedAt` | `string` (ISO 8601) | yes  | Audit timestamps                            |

## HarnessTrace

The metadata for one run-of-the-loop. Pairs with N `TraceSpan`s.

| Field            | Type                       | Required | Notes                                    |
| ---------------- | -------------------------- | -------- | ---------------------------------------- |
| `traceId`        | `string`                   | yes      | Unique across the deployment             |
| `sessionId`      | `string`                   | yes      | Owning `AgentSession`                    |
| `chatId`         | `string`                   | yes      |                                          |
| `userId`         | `string`                   | yes      |                                          |
| `startedAt`      | `string`                   | yes      |                                          |
| `endedAt`        | `string`                   | no       | Set when the loop returns                |
| `outcome`        | `'ok' \| 'error' \| 'budget_exhausted' \| 'cancelled'` | no | Run-level summary |

## TraceSpan

One unit of work. Maps 1:1 to OpenTelemetry's notion of a span. The
recorder produces three span kinds (W2.7.2):

| `kind`       | Name            | Brackets                                              |
| ------------ | --------------- | ----------------------------------------------------- |
| `llm`        | `llm.request`   | One provider request — one per attempt, so retries within a round are visible |
| `tool`       | `<tool name>`   | One tool dispatch                                     |
| `approval`   | `approval.wait` | An interactive approval wait, parented to its tool span |

| Field        | Type                       | Required | Notes                                                     |
| ------------ | -------------------------- | -------- | --------------------------------------------------------- |
| `spanId`     | `string`                   | yes      |                                                           |
| `name`       | `string`                   | yes      | e.g. `llm.request`, `read_file`                           |
| `startAt`    | `string`                   | yes      | ISO 8601                                                  |
| `endAt`      | `string`                   | no       |                                                           |
| `parentId`   | `string`                   | no       | Parent span; absent for spans hanging off the root        |
| `attrs`      | `Record<string, unknown>`  | no       | Stage-specific attributes (token counts, tool name, etc.) |

## TraceEvent

A point-in-time annotation **inside** a span (vs. spans which have
duration).

| Field        | Type                       | Required | Notes                                       |
| ------------ | -------------------------- | -------- | ------------------------------------------- |
| `eventId`    | `string`                   | yes      |                                             |
| `spanId`     | `string`                   | yes      | Parent span                                 |
| `name`       | `string`                   | yes      | e.g. `llm.token`, `policy.denied`           |
| `at`         | `string`                   | yes      | ISO 8601                                    |
| `attrs`      | `Record<string, unknown>`  | no       |                                             |

## Observability export decision (W2.7.1, 2026-08-30)

**Decision: keep the zero-dependency hand-rolled OTLP/HTTP exporter; do not
adopt `opentelemetry-sdk`.**

Context: the framework exports traces to OTLP/HTTP collectors via a small
hand-rolled mapping (`otel.py`). The open question was whether to keep
thickening it (span model, sampling) or switch to the real SDK.

Reasons:

1. **Bundle budget.** The sidecar ships embedded in the desktop app under a
   CI-enforced size budget; `opentelemetry-sdk` plus its exporter chain adds
   megabytes of dependency weight for a mapping we already implement in
   ~200 lines.
2. **The wire format is the compatibility surface, not the SDK.** Backends
   (Jaeger, Tempo, Honeycomb, Datadog) ingest OTLP/HTTP JSON regardless of
   what produced it. Our exporter already speaks it; the SDK would not widen
   the backend story.
3. **The real gap was span coverage, which no SDK fixes.** LLM requests and
   approval waits were invisible because the loop did not emit bracketing
   events — closing that (W2.7.2) is loop work, not exporter work.

Consequences: the span model thickens in `tracing.py` (llm/tool/approval
kinds, parentage), head-based sampling lives in `TraceRecorder`
(`sample_rate`, deterministic per trace id), and the OTLP mapping in
`otel.py` stays the single export path. Revisit only if a backend requires
features the hand-rolled mapping cannot express (e.g. exemplars, log
correlation).

## Adapter interfaces (Tier 3)

These are Python-only. The protocol-level types above are what crosses
the wire; the adapters below are what your server / sidecar implements.

### `LLMProvider`

```python
class LLMProvider(Protocol):
    async def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[LLMStreamChunk]: ...
```

Reference implementations: `OpenAICompatProvider`, `AnthropicProvider`.
Ollama is just `OpenAICompatProvider` pointed at a local base URL.

### `ToolRouter`

```python
router = ToolRouter()

@tool(router=router, description="Read a file", mode="read")
async def read_file(path: str) -> dict:
    return {"path": path, "content": "..."}

result = await router.dispatch(
    ToolCall(id="c1", name="read_file", arguments={"path": "README.md"}),
    consent_granted=False,                # required for `local` tools
)
```

### `StorageAdapter`

```python
class StorageAdapter(Protocol):
    async def upsert_session(self, session: AgentSession) -> AgentSession: ...
    async def get_session(self, session_id: str) -> AgentSession | None: ...
    async def list_sessions(
        self, *, user_id: str | None = None, chat_id: str | None = None,
        active_only: bool = False,
    ) -> list[AgentSession]: ...
    # … plus harness trace + chat-message persistence methods

    # The model-visible record channel (Wave 1): a per-chat append-only log
    # of typed history entries (HistoryItem / CompactionBoundary /
    # HistorySeed), written at full fidelity and read back for resume.
    async def append_history(
        self, record_id: str, entries: Iterable[dict[str, Any]]
    ) -> None: ...
    async def list_history(
        self, record_id: str, *, after_seq: int | None = None,
        until_seq: int | None = None, limit: int | None = None,
        reverse: bool = False,
    ) -> list[dict[str, Any]]: ...
```

Reference implementations: `InMemoryStorage` (for tests / sidecar) and
`SqlAlchemyStorage` (for the FastAPI server). Resume reads the record
tail-first (`reverse=True` paging) and stops at the newest compaction
boundary — O(tail), never the superseded prefix.

### `TransportAdapter`

```python
class TransportAdapter(Protocol):
    async def emit(self, event: SSEEvent) -> None: ...
    async def receive(self) -> AsyncIterator[SSEEvent]: ...
```

Reference implementations: `FastAPISseTransport` (HTTP SSE) and
`StdioJsonRpcTransport` (used by the sidecar).
