"""SQLAlchemy-backed StorageAdapter (optional)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from steerable_agent_protocol.generated import (
    AgentSession,
    ChatAgent,
    ChatMessage,
    HarnessTrace,
    TraceEvent,
    TraceSpan,
)

try:
    from sqlalchemy import (
        JSON,
        Boolean,
        Column,
        DateTime,
        Integer,
        MetaData,
        String,
        Table,
        Text,
        UniqueConstraint,
        select,
    )
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
except Exception as exc:  # pragma: no cover - optional dep
    raise ImportError(
        "SqlAlchemyStorage requires sqlalchemy>=2.0. Install with "
        "`pip install steerable-agent-runtime[sqlalchemy]`."
    ) from exc

from ..errors import StorageError

metadata = MetaData()


sessions_table = Table(
    "steerable_session",
    metadata,
    Column("sessionId", String(191), primary_key=True),
    Column("userId", String(191), nullable=False, index=True),
    Column("projectId", String(191), nullable=True),
    Column("chatId", String(191), nullable=False, index=True),
    Column("currentStage", String(191), nullable=False),
    Column("nextStage", String(191), nullable=True),
    Column("scenario", String(191), nullable=False, default="agent-entry"),
    Column("stageData", JSON, nullable=True),
    Column("isActive", Boolean, nullable=False, default=True),
    Column("createdAt", DateTime, nullable=False),
    Column("updatedAt", DateTime, nullable=False),
)

agents_table = Table(
    "steerable_agent",
    metadata,
    Column("id", String(191), primary_key=True),
    Column("slug", String(191), nullable=True),
    Column("name", String(191), nullable=False),
    Column("icon", String(191), nullable=True),
    Column("color", String(191), nullable=True),
    Column("description", Text, nullable=True),
    Column("rolePrompt", Text, nullable=True),
    Column("forbiddenPrompt", Text, nullable=True),
    Column("skillIds", JSON, nullable=False, default=list),
    Column("allowExternalSkills", Boolean, nullable=False, default=True),
    Column("isBuiltin", Boolean, nullable=False, default=False),
    Column("isArchived", Boolean, nullable=False, default=False),
    Column("sortOrder", Integer, nullable=False, default=0),
    Column("createdAt", DateTime, nullable=False),
    Column("updatedAt", DateTime, nullable=False),
)

messages_table = Table(
    "steerable_message",
    metadata,
    Column("id", String(191), primary_key=True),
    Column("chatId", String(191), nullable=False, index=True),
    Column("role", String(32), nullable=False),
    Column("content", Text, nullable=False),
    Column("agentId", String(191), nullable=True),
    Column("toolCalls", JSON, nullable=True),
    Column("toolResult", JSON, nullable=True),
    Column("createdAt", DateTime, nullable=False, index=True),
    Column("updatedAt", DateTime, nullable=True),
)

traces_table = Table(
    "steerable_trace",
    metadata,
    Column("traceId", String(191), primary_key=True),
    Column("userId", String(191), nullable=True, index=True),
    Column("chatId", String(191), nullable=True, index=True),
    Column("sessionId", String(191), nullable=True, index=True),
    Column("assistantMessageId", String(191), nullable=True),
    Column("status", String(32), nullable=False, default="running"),
    Column("durationMs", Integer, nullable=True),
    Column("hadError", Boolean, nullable=False, default=False),
    Column("errorMessage", String(2048), nullable=True),
    Column("eventCount", Integer, nullable=False, default=0),
    Column("spanCount", Integer, nullable=False, default=0),
    Column("totalTokens", Integer, nullable=True),
    Column("modelId", String(191), nullable=True),
    Column("startedAtMs", Integer, nullable=True),
    Column("createdAt", DateTime, nullable=False),
    Column("updatedAt", DateTime, nullable=False),
)

spans_table = Table(
    "steerable_trace_span",
    metadata,
    Column("spanId", String(191), primary_key=True),
    Column("traceId", String(191), nullable=False, index=True),
    Column("parentSpanId", String(191), nullable=True),
    Column("name", String(191), nullable=False),
    Column("kind", String(32), nullable=False, default="custom"),
    Column("startMs", Integer, nullable=False),
    Column("endMs", Integer, nullable=True),
    Column("durationMs", Integer, nullable=True),
    Column("status", String(32), nullable=False, default="running"),
    Column("attrs", JSON, nullable=False, default=dict),
)

events_table = Table(
    "steerable_trace_event",
    metadata,
    Column("id", String(191), primary_key=True),
    Column("traceId", String(191), nullable=False, index=True),
    Column("kind", String(32), nullable=False),
    Column("name", String(191), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("timestampMs", Integer, nullable=False),
    Column("durationMs", Integer, nullable=True),
    Column("status", String(32), nullable=True),
    Column("payload", JSON, nullable=True),
    Column("createdAt", DateTime, nullable=True),
    UniqueConstraint("traceId", "sequence", name="steerable_trace_event_seq_uniq"),
)


def _model_to_row(model: Any) -> dict[str, Any]:
    return model.model_dump()


class SqlAlchemyStorage:
    """SQLAlchemy 2.0 async StorageAdapter."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def upsert_session(self, session: AgentSession) -> AgentSession:
        await self._upsert(sessions_table, "sessionId", session.sessionId, _model_to_row(session))
        return session

    async def get_session(self, session_id: str) -> AgentSession | None:
        row = await self._get_one(sessions_table, sessions_table.c.sessionId == session_id)
        return AgentSession(**row) if row else None

    async def list_sessions(
        self,
        *,
        user_id: str | None = None,
        chat_id: str | None = None,
        active_only: bool = False,
    ) -> list[AgentSession]:
        clauses = []
        if user_id is not None:
            clauses.append(sessions_table.c.userId == user_id)
        if chat_id is not None:
            clauses.append(sessions_table.c.chatId == chat_id)
        if active_only:
            clauses.append(sessions_table.c.isActive.is_(True))
        rows = await self._select_many(
            sessions_table,
            clauses=clauses,
            order_by=[sessions_table.c.updatedAt.desc()],
        )
        return [AgentSession(**row) for row in rows]

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    async def upsert_agent(self, agent: ChatAgent) -> ChatAgent:
        await self._upsert(agents_table, "id", agent.id, _model_to_row(agent))
        return agent

    async def get_agent(self, agent_id: str) -> ChatAgent | None:
        row = await self._get_one(agents_table, agents_table.c.id == agent_id)
        return ChatAgent(**row) if row else None

    async def list_agents(self, *, include_archived: bool = False) -> list[ChatAgent]:
        clauses = []
        if not include_archived:
            clauses.append(agents_table.c.isArchived.is_(False))
        rows = await self._select_many(
            agents_table,
            clauses=clauses,
            order_by=[agents_table.c.sortOrder.asc(), agents_table.c.createdAt.asc()],
        )
        return [ChatAgent(**row) for row in rows]

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def append_message(self, message: ChatMessage) -> ChatMessage:
        if not message.chatId:
            raise StorageError("ChatMessage.chatId is required")
        await self._insert(messages_table, _model_to_row(message))
        return message

    async def list_messages(self, chat_id: str, *, limit: int | None = None) -> list[ChatMessage]:
        rows = await self._select_many(
            messages_table,
            clauses=[messages_table.c.chatId == chat_id],
            order_by=[messages_table.c.createdAt.asc()],
            limit=limit,
        )
        return [ChatMessage(**row) for row in rows]

    # ------------------------------------------------------------------
    # Traces / spans / events
    # ------------------------------------------------------------------

    async def upsert_trace(self, trace: HarnessTrace) -> HarnessTrace:
        await self._upsert(traces_table, "traceId", trace.traceId, _model_to_row(trace))
        return trace

    async def get_trace(self, trace_id: str) -> HarnessTrace | None:
        row = await self._get_one(traces_table, traces_table.c.traceId == trace_id)
        return HarnessTrace(**row) if row else None

    async def append_spans(self, trace_id: str, spans: Iterable[TraceSpan]) -> None:
        rows = []
        for span in spans:
            data = _model_to_row(span)
            data["traceId"] = trace_id
            rows.append(data)
        if rows:
            await self._insert_many(spans_table, rows)

    async def list_spans(self, trace_id: str) -> list[TraceSpan]:
        rows = await self._select_many(
            spans_table,
            clauses=[spans_table.c.traceId == trace_id],
            order_by=[spans_table.c.startMs.asc()],
        )
        return [TraceSpan(**row) for row in rows]

    async def append_events(self, trace_id: str, events: Iterable[TraceEvent]) -> None:
        rows = []
        for event in events:
            data = _model_to_row(event)
            data["traceId"] = trace_id
            rows.append(data)
        if rows:
            await self._insert_many(events_table, rows)

    async def list_events(self, trace_id: str) -> list[TraceEvent]:
        rows = await self._select_many(
            events_table,
            clauses=[events_table.c.traceId == trace_id],
            order_by=[events_table.c.sequence.asc()],
        )
        return [TraceEvent(**row) for row in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _upsert(
        self,
        table: Table,
        key_column: str,
        key_value: Any,
        row: dict[str, Any],
    ) -> None:
        async with self._sessionmaker() as session:
            existing = await session.execute(
                select(table).where(getattr(table.c, key_column) == key_value)
            )
            if existing.first():
                await session.execute(
                    table.update()
                    .where(getattr(table.c, key_column) == key_value)
                    .values(**row)
                )
            else:
                await session.execute(table.insert().values(**row))
            await session.commit()

    async def _insert(self, table: Table, row: dict[str, Any]) -> None:
        async with self._sessionmaker() as session:
            await session.execute(table.insert().values(**row))
            await session.commit()

    async def _insert_many(self, table: Table, rows: list[dict[str, Any]]) -> None:
        async with self._sessionmaker() as session:
            await session.execute(table.insert(), rows)
            await session.commit()

    async def _select_many(
        self,
        table: Table,
        *,
        clauses: list[Any] | None = None,
        order_by: list[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(table)
        for clause in clauses or []:
            stmt = stmt.where(clause)
        for ordering in order_by or []:
            stmt = stmt.order_by(ordering)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._sessionmaker() as session:
            result = await session.execute(stmt)
            return [dict(row._mapping) for row in result.fetchall()]

    async def _get_one(self, table: Table, clause: Any) -> dict[str, Any] | None:
        async with self._sessionmaker() as session:
            result = await session.execute(select(table).where(clause))
            row = result.first()
            return dict(row._mapping) if row else None
