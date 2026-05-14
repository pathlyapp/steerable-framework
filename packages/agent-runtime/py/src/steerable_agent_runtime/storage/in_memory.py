"""Reference in-memory StorageAdapter (default for sidecar / dev)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from copy import deepcopy

from steerable_agent_protocol.generated import (
    AgentSession,
    ChatAgent,
    ChatMessage,
    HarnessTrace,
    TraceEvent,
    TraceSpan,
)

from ..errors import StorageError


class InMemoryStorage:
    """Thread-safe in-memory storage. All mutations happen under an asyncio
    lock so concurrent dispatch from a single event loop is safe."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, AgentSession] = {}
        self._agents: dict[str, ChatAgent] = {}
        self._messages: dict[str, list[ChatMessage]] = {}
        self._traces: dict[str, HarnessTrace] = {}
        self._spans: dict[str, list[TraceSpan]] = {}
        self._events: dict[str, list[TraceEvent]] = {}

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def upsert_session(self, session: AgentSession) -> AgentSession:
        async with self._lock:
            self._sessions[session.sessionId] = deepcopy(session)
            return deepcopy(session)

    async def get_session(self, session_id: str) -> AgentSession | None:
        async with self._lock:
            value = self._sessions.get(session_id)
            return deepcopy(value) if value else None

    async def list_sessions(
        self,
        *,
        user_id: str | None = None,
        chat_id: str | None = None,
        active_only: bool = False,
    ) -> list[AgentSession]:
        async with self._lock:
            sessions = list(self._sessions.values())
        if user_id is not None:
            sessions = [s for s in sessions if s.userId == user_id]
        if chat_id is not None:
            sessions = [s for s in sessions if s.chatId == chat_id]
        if active_only:
            sessions = [s for s in sessions if s.isActive]
        sessions.sort(key=lambda s: s.updatedAt, reverse=True)
        return [deepcopy(s) for s in sessions]

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    async def upsert_agent(self, agent: ChatAgent) -> ChatAgent:
        async with self._lock:
            self._agents[agent.id] = deepcopy(agent)
            return deepcopy(agent)

    async def get_agent(self, agent_id: str) -> ChatAgent | None:
        async with self._lock:
            value = self._agents.get(agent_id)
            return deepcopy(value) if value else None

    async def list_agents(self, *, include_archived: bool = False) -> list[ChatAgent]:
        async with self._lock:
            agents = list(self._agents.values())
        if not include_archived:
            agents = [a for a in agents if not a.isArchived]
        agents.sort(key=lambda a: (a.sortOrder, a.createdAt))
        return [deepcopy(a) for a in agents]

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def append_message(self, message: ChatMessage) -> ChatMessage:
        if not message.chatId:
            raise StorageError("ChatMessage.chatId is required for append_message")
        async with self._lock:
            bucket = self._messages.setdefault(message.chatId, [])
            bucket.append(deepcopy(message))
            return deepcopy(message)

    async def list_messages(
        self, chat_id: str, *, limit: int | None = None
    ) -> list[ChatMessage]:
        async with self._lock:
            bucket = list(self._messages.get(chat_id, []))
        bucket.sort(key=lambda m: m.createdAt)
        if limit is not None:
            bucket = bucket[-limit:]
        return [deepcopy(m) for m in bucket]

    # ------------------------------------------------------------------
    # Traces / spans / events
    # ------------------------------------------------------------------

    async def upsert_trace(self, trace: HarnessTrace) -> HarnessTrace:
        async with self._lock:
            self._traces[trace.traceId] = deepcopy(trace)
            return deepcopy(trace)

    async def get_trace(self, trace_id: str) -> HarnessTrace | None:
        async with self._lock:
            value = self._traces.get(trace_id)
            return deepcopy(value) if value else None

    async def append_spans(self, trace_id: str, spans: Iterable[TraceSpan]) -> None:
        async with self._lock:
            bucket = self._spans.setdefault(trace_id, [])
            for span in spans:
                bucket.append(deepcopy(span))
            trace = self._traces.get(trace_id)
            if trace is not None:
                trace.spanCount = len(bucket)

    async def list_spans(self, trace_id: str) -> list[TraceSpan]:
        async with self._lock:
            return [deepcopy(span) for span in self._spans.get(trace_id, [])]

    async def append_events(self, trace_id: str, events: Iterable[TraceEvent]) -> None:
        async with self._lock:
            bucket = self._events.setdefault(trace_id, [])
            for event in events:
                bucket.append(deepcopy(event))
            trace = self._traces.get(trace_id)
            if trace is not None:
                trace.eventCount = len(bucket)

    async def list_events(self, trace_id: str) -> list[TraceEvent]:
        async with self._lock:
            return sorted(
                [deepcopy(event) for event in self._events.get(trace_id, [])],
                key=lambda event: event.sequence,
            )
