from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    success: bool
    terminal: bool | None = None
    needsFollowup: bool | None = None
    nextAction: str | None = None
    message: str | None = None
    error: str | None = None
    data: dict[str, Any] | None = None


class ChatMessage(BaseModel):
    id: str
    chatId: str | None = None
    role: Literal["user", "assistant", "tool", "system"]
    content: str
    agentId: str | None = None
    toolCalls: list[ToolCall] | None = None
    toolResult: ToolResult | None = None
    createdAt: str
    updatedAt: str | None = None


class ChatAgent(BaseModel):
    id: str
    slug: str | None = None
    name: str
    icon: str | None = None
    color: str | None = None
    description: str | None = None
    rolePrompt: str | None = None
    forbiddenPrompt: str | None = None
    skillIds: list[str] = Field(default_factory=list)
    allowExternalSkills: bool = True
    isBuiltin: bool = False
    isArchived: bool = False
    sortOrder: int = 0
    createdAt: str
    updatedAt: str


class CommandSafetyPattern(BaseModel):
    id: str
    label: str
    description: str
    pattern: str
    category: str
    severity: Literal["critical", "warning"]
    platform: Literal["all", "unix", "windows"]


class SSEEvent(BaseModel):
    type: str
    event: str | None = None
    content: str | None = None
    hint: str | None = None
    message: str | None = None
    code: str | None = None
    orchestrationGroupId: str | None = None
    taskId: str | None = None
    messageId: str | None = None
    payload: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Tier 3 runtime models (spec/runtime/)
# ---------------------------------------------------------------------------


class AgentSession(BaseModel):
    id: str | None = None
    sessionId: str
    userId: str
    projectId: str | None = None
    chatId: str
    currentStage: str
    nextStage: str | None = None
    scenario: str = "agent-entry"
    stageData: dict[str, Any] | None = None
    isActive: bool
    createdAt: str
    updatedAt: str


class HarnessTrace(BaseModel):
    traceId: str
    userId: str | None = None
    chatId: str | None = None
    sessionId: str | None = None
    assistantMessageId: str | None = None
    status: Literal["running", "completed", "error", "cancelled", "budget_exhausted"]
    durationMs: int | None = None
    hadError: bool = False
    errorMessage: str | None = None
    eventCount: int = 0
    spanCount: int = 0
    totalTokens: int | None = None
    modelId: str | None = None
    startedAtMs: int | None = None
    createdAt: str
    updatedAt: str


SpanKind = Literal[
    "llm",
    "tool",
    "policy",
    "budget",
    "retry",
    "completion",
    "transport",
    "storage",
    "custom",
]
SpanStatus = Literal["ok", "error", "timeout", "cancelled", "running"]


class TraceSpan(BaseModel):
    spanId: str
    traceId: str | None = None
    parentSpanId: str | None = None
    name: str
    kind: SpanKind = "custom"
    startMs: int
    endMs: int | None = None
    durationMs: int | None = None
    status: SpanStatus = "running"
    attrs: dict[str, Any] = Field(default_factory=dict)


TraceEventKind = Literal[
    "lifecycle",
    "policy",
    "budget",
    "retry",
    "tool_call",
    "tool_result",
    "llm_request",
    "llm_response",
    "completion",
    "error",
    "log",
    "custom",
]


class TraceEvent(BaseModel):
    id: str | None = None
    traceId: str
    kind: TraceEventKind
    name: str
    sequence: int
    timestampMs: int
    durationMs: int | None = None
    status: Literal["ok", "warning", "error"] | None = None
    payload: dict[str, Any] | None = None
    createdAt: str | None = None


# ---------------------------------------------------------------------------
# Tier 3 sidecar wire protocol (spec/sidecar/)
# ---------------------------------------------------------------------------


SidecarErrorKind = Literal[
    "parse",
    "invalid_request",
    "method_not_found",
    "invalid_params",
    "internal",
    "budget_exhausted",
    "policy_denied",
    "tool_failed",
    "transport_closed",
    "timeout",
    "cancelled",
]


class SidecarError(BaseModel):
    code: int
    message: str
    data: Any | None = None
    kind: SidecarErrorKind | None = None


class SidecarRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] | list[Any] | None = None


class SidecarResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: Any | None = None
    error: SidecarError | None = None


class SidecarNotification(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] | list[Any] | None = None


SidecarHealthStatus = Literal["ok", "degraded", "starting", "shutting_down"]


class SidecarHealthCheck(BaseModel):
    status: Literal["ok", "warn", "error"]
    message: str | None = None
    latencyMs: int | None = None


class SidecarHealth(BaseModel):
    status: SidecarHealthStatus
    version: str
    protocolVersion: str = "0.1.0"
    uptimeMs: int
    pid: int | None = None
    pythonVersion: str | None = None
    platform: str | None = None
    loadedProviders: list[str] = Field(default_factory=list)
    loadedTools: int = 0
    activeTraces: int = 0
    checks: dict[str, SidecarHealthCheck] = Field(default_factory=dict)
