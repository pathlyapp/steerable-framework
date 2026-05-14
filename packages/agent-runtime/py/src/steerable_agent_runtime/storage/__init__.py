"""StorageAdapter interface + reference implementations."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Protocol, runtime_checkable

from steerable_agent_protocol.generated import (
    AgentSession,
    ChatAgent,
    ChatMessage,
    HarnessTrace,
    TraceEvent,
    TraceSpan,
)


@runtime_checkable
class StorageAdapter(Protocol):
    """Persistence interface for the runtime.

    Implementations must be **safe under concurrent ``await``** but are not
    required to be process-safe. The reference SQLAlchemy adapter delegates
    isolation to the underlying database.
    """

    # -- AgentSession ---------------------------------------------------

    async def upsert_session(self, session: AgentSession) -> AgentSession: ...

    async def get_session(self, session_id: str) -> AgentSession | None: ...

    async def list_sessions(
        self,
        *,
        user_id: str | None = None,
        chat_id: str | None = None,
        active_only: bool = False,
    ) -> list[AgentSession]: ...

    # -- ChatAgent ------------------------------------------------------

    async def upsert_agent(self, agent: ChatAgent) -> ChatAgent: ...

    async def get_agent(self, agent_id: str) -> ChatAgent | None: ...

    async def list_agents(self, *, include_archived: bool = False) -> list[ChatAgent]: ...

    # -- ChatMessage ----------------------------------------------------

    async def append_message(self, message: ChatMessage) -> ChatMessage: ...

    async def list_messages(self, chat_id: str, *, limit: int | None = None) -> list[ChatMessage]: ...

    # -- HarnessTrace + spans + events ---------------------------------

    async def upsert_trace(self, trace: HarnessTrace) -> HarnessTrace: ...

    async def get_trace(self, trace_id: str) -> HarnessTrace | None: ...

    async def append_spans(self, trace_id: str, spans: Iterable[TraceSpan]) -> None: ...

    async def list_spans(self, trace_id: str) -> list[TraceSpan]: ...

    async def append_events(self, trace_id: str, events: Iterable[TraceEvent]) -> None: ...

    async def list_events(self, trace_id: str) -> list[TraceEvent]: ...


from .in_memory import InMemoryStorage  # noqa: E402

try:
    from .sqlalchemy_store import SqlAlchemyStorage  # noqa: F401
except Exception:  # pragma: no cover - optional dep
    SqlAlchemyStorage = None  # type: ignore[assignment]


__all__ = [
    "StorageAdapter",
    "InMemoryStorage",
    "SqlAlchemyStorage",
]
