from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Field, SQLModel


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentSessionBase(SQLModel):
    """Base class for AgentSession table."""
    id: str = Field(default_factory=lambda: f"sess_rec_{uuid.uuid4().hex[:12]}", primary_key=True)
    sessionId: str = Field(index=True)
    userId: str = Field(index=True)
    projectId: Optional[str] = Field(default=None, index=True)
    chatId: str = Field(index=True)
    currentStage: str = Field(default="agent-entry")
    nextStage: Optional[str] = Field(default=None)
    scenario: str = Field(default="agent-entry")
    stageData: Optional[Any] = Field(default=None)
    isActive: bool = Field(default=True, index=True)
    createdAt: datetime = Field(default_factory=_now_utc)
    updatedAt: datetime = Field(default_factory=_now_utc)


class ChatMessageBase(SQLModel):
    """Base class for ChatMessage table."""
    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:12]}", primary_key=True)
    chatId: str = Field(index=True)
    agentId: Optional[str] = Field(default=None, index=True)
    content: str
    role: str = Field(index=True)
    isPlaceholder: bool = Field(default=False, index=True)
    thinkingProcess: Optional[str] = Field(default=None)
    messageMetadata: Optional[str] = Field(default=None)
    feedback: Optional[str] = Field(default=None)
    createdAt: datetime = Field(default_factory=_now_utc, index=True)
    sessionId: Optional[str] = Field(default=None, index=True)
    activeVariantId: Optional[str] = Field(default=None)
    variantsCount: int = Field(default=1)
    variantsLocked: bool = Field(default=False)


class HarnessTraceBase(SQLModel):
    """Base class for HarnessTrace table."""
    traceId: str = Field(primary_key=True)
    userId: Optional[str] = Field(default=None, index=True)
    chatId: Optional[str] = Field(default=None, index=True)
    sessionId: Optional[str] = Field(default=None, index=True)
    assistantMessageId: Optional[str] = Field(default=None)
    status: str = Field(default="running", index=True)
    durationMs: Optional[int] = Field(default=None)
    hadError: bool = Field(default=False, index=True)
    errorMessage: Optional[str] = Field(default=None)
    eventCount: int = Field(default=0)
    spanCount: int = Field(default=0)
    totalTokens: Optional[int] = Field(default=None)
    modelId: Optional[str] = Field(default=None)
    startedAtMs: Optional[int] = Field(default=None)
    createdAt: datetime = Field(default_factory=_now_utc)
    updatedAt: datetime = Field(default_factory=_now_utc)


class HarnessTraceEventBase(SQLModel):
    """Base class for HarnessTraceEvent table."""
    id: str = Field(default_factory=lambda: f"ev_{uuid.uuid4().hex[:12]}", primary_key=True)
    traceId: str = Field(index=True)
    kind: str = Field(index=True)
    name: str = Field(index=True)
    sequence: int = Field(index=True)
    timestampMs: Optional[int] = Field(default=None)
    durationMs: Optional[int] = Field(default=None)
    status: Optional[str] = Field(default=None, index=True)
    payload: Optional[Any] = Field(default=None)
    createdAt: datetime = Field(default_factory=_now_utc)
