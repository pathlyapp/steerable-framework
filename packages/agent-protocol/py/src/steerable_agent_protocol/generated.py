from __future__ import annotations

from typing import Any
from pydantic import BaseModel

class ActionSegmentPayload(BaseModel):
    segments: list[Any]

class AnalysisDocumentPayload(BaseModel):
    title: Any | None = None
    body: str
    createdAt: Any | None = None
    modelId: Any | None = None

class AskUserQuestionsPayload(BaseModel):
    intro: str
    outro: Any | None = None
    answers: Any | None = None
    questions: list[Any]

class CoverageReportPayload(BaseModel):
    reportId: str
    title: str
    overallCoverage: float
    overallMastery: float
    summary: Any | None = None
    sections: list[Any]
    weakPoints: list[Any]
    actions: dict[str, Any]

class OrchestrationPlanPayload(BaseModel):
    rationale: str | None = None
    mode: str | None = None
    tasks: list[Any]
    coordinator: dict[str, Any] | None = None

class PlanSelectorPayload(BaseModel):
    comparison: str
    selectedPlan: Any | None = None
    goalAttribution: dict[str, Any]
    plans: list[Any]

class PlanStepsPayload(BaseModel):
    steps: list[Any]

class QuizPayload(BaseModel):
    quizId: str
    title: str
    description: Any | None = None
    submitActionLabel: str
    submittedAnswers: Any | None = None
    questions: list[Any]

class ResearchPlanPayload(BaseModel):
    topic: str
    round: int
    final: bool
    subQuestions: list[Any]
    decision: dict[str, Any]

class SearchSourcesPayload(BaseModel):
    sources: list[Any]

class SuggestedRepliesPayload(BaseModel):
    suggestions: list[Any]

class SummaryMessagePayload(BaseModel):
    body: str
    summarizedCount: Any | None = None
    status: Any | None = None
    type: Any | None = None

class ThinkingProcessPayload(BaseModel):
    body: str
    defaultExpanded: bool | None = None

class ToolExecutionPayload(BaseModel):
    id: str
    name: str
    status: str
    summary: Any | None = None
    args: Any | None = None
    output: Any | None = None
    error: Any | None = None
    durationMs: Any | None = None
    icon: Any | None = None
    expandable: bool | None = None

class ChatAgent(BaseModel):
    id: str
    slug: str | None = None
    name: str
    icon: str | None = None
    color: str | None = None
    description: str | None = None
    rolePrompt: str | None = None
    forbiddenPrompt: str | None = None
    skillIds: list[Any] | None = None
    allowExternalSkills: bool | None = None
    isBuiltin: bool | None = None
    isArchived: bool | None = None
    sortOrder: int | None = None
    createdAt: str
    updatedAt: str

class ChatMessage(BaseModel):
    id: str
    chatId: str | None = None
    role: str
    content: str
    agentId: str | None = None
    toolCalls: list[Any] | None = None
    toolResult: Any | None = None
    createdAt: str
    updatedAt: str | None = None

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

class AgentSession(BaseModel):
    id: str | None = None
    sessionId: str
    userId: str
    projectId: Any | None = None
    chatId: str
    currentStage: str
    nextStage: Any | None = None
    scenario: str | None = None
    stageData: Any | None = None
    isActive: bool
    createdAt: str
    updatedAt: str

class HarnessTrace(BaseModel):
    traceId: str
    userId: Any | None = None
    chatId: Any | None = None
    sessionId: Any | None = None
    assistantMessageId: Any | None = None
    status: str
    durationMs: Any | None = None
    hadError: bool
    errorMessage: Any | None = None
    eventCount: int
    spanCount: int
    totalTokens: Any | None = None
    modelId: Any | None = None
    startedAtMs: Any | None = None
    createdAt: str
    updatedAt: str

class TraceEvent(BaseModel):
    id: str | None = None
    traceId: str
    kind: str
    name: str
    sequence: int
    timestampMs: int
    durationMs: Any | None = None
    status: Any | None = None
    payload: Any | None = None
    createdAt: str | None = None

class TraceSpan(BaseModel):
    spanId: str
    traceId: Any | None = None
    parentSpanId: Any | None = None
    name: str
    kind: str | None = None
    startMs: int
    endMs: Any | None = None
    durationMs: Any | None = None
    status: str
    attrs: dict[str, Any] | None = None

class CommandSafetyPattern(BaseModel):
    id: str
    label: str
    description: str
    pattern: str
    category: str
    severity: str
    platform: str

class SidecarError(BaseModel):
    code: int
    message: str
    data: Any | None = None
    kind: str | None = None

class SidecarHealth(BaseModel):
    status: str
    version: str
    protocolVersion: str | None = None
    uptimeMs: int
    pid: int | None = None
    pythonVersion: str | None = None
    platform: str | None = None
    loadedProviders: list[Any] | None = None
    loadedTools: int | None = None
    activeTraces: int | None = None
    checks: dict[str, Any] | None = None

class SidecarNotification(BaseModel):
    jsonrpc: str
    method: str
    params: Any | None = None

class SidecarRequest(BaseModel):
    jsonrpc: str
    id: Any
    method: str
    params: Any | None = None

class SidecarResponse(BaseModel):
    jsonrpc: str
    id: Any
    result: Any | None = None
    error: Any | None = None

class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]

class ToolResult(BaseModel):
    success: bool
    terminal: bool | None = None
    needsFollowup: bool | None = None
    nextAction: str | None = None
    message: str | None = None
    error: str | None = None
    data: dict[str, Any] | None = None
