"""Steerable agent runtime — Tier 3 adapter package."""

from .antihallucination import (
    AntiHallucinationConfig,
    AntiHallucinationHooks,
    detect_claimed_execution,
    detect_deferred_execution,
    detect_deferred_execution_eager,
    detect_execution_intent_in_user_message,
    parse_grounding_verdict,
    parse_turn_route,
    should_run_grounding_judge,
)
from .calibration import CalibratingProvider, ModelCalibration, UsageCalibration
from .compaction import CompactionHooks
from .errors import (
    BudgetExhaustedError,
    PolicyDeniedError,
    StorageError,
    ToolDispatchError,
    TransportError,
)
from .errors import (
    RuntimeError as SteerableRuntimeError,
)
from .hooks import (
    ChainHooks,
    CompletionAction,
    CompletionDraft,
    LoopHooks,
    NoopHooks,
    PreStepAction,
    RetryAction,
)
from .llm import LLMMessage, LLMProvider, LLMStreamChunk, LLMUsage
from .loop import (
    CompletionDecision,
    CoreLoop,
    LoopConfig,
    LoopContext,
    LoopEvent,
    RouterToolExecutor,
    ToolExecutor,
)
from .pseudo import extract_inline_tool_calls
from .replay import (
    ExecutionBudget,
    HarnessExecutionState,
    HarnessTrajectoryEvent,
    build_step_decision_event,
    reduce_execution_state,
)
from .otel import export_otlp_http, to_otlp_json
from .resume import load_transcript, project_transcript
from .retry import RetryHooks
from .spill import FilesystemSpillStore, InMemorySpillStore, SpillHooks, SpillStore
from .storage import StorageAdapter
from .subagent import SubagentConfig, SubagentExecutor, subagent_tool_descriptor
from .tokens import (
    MODEL_TOKEN_FACTORS,
    estimate_text_tokens,
    estimate_tokens,
    factor_for_model,
    register_model_factor,
)
from .tools import RegisteredTool, ToolRouter, tool
from .tracing import TraceRecorder
from .transport import TransportAdapter

__all__ = [
    "AntiHallucinationConfig",
    "AntiHallucinationHooks",
    "BudgetExhaustedError",
    "CalibratingProvider",
    "ChainHooks",
    "CompactionHooks",
    "CompletionAction",
    "CompletionDecision",
    "CompletionDraft",
    "CoreLoop",
    "ExecutionBudget",
    "FilesystemSpillStore",
    "HarnessExecutionState",
    "HarnessTrajectoryEvent",
    "InMemorySpillStore",
    "LLMMessage",
    "LLMProvider",
    "LLMStreamChunk",
    "LLMUsage",
    "LoopConfig",
    "LoopContext",
    "LoopEvent",
    "LoopHooks",
    "NoopHooks",
    "PolicyDeniedError",
    "PreStepAction",
    "RegisteredTool",
    "RetryAction",
    "RetryHooks",
    "RouterToolExecutor",
    "SpillHooks",
    "SpillStore",
    "SteerableRuntimeError",
    "StorageAdapter",
    "StorageError",
    "SubagentConfig",
    "SubagentExecutor",
    "ToolDispatchError",
    "ToolExecutor",
    "MODEL_TOKEN_FACTORS",
    "ModelCalibration",
    "ToolRouter",
    "TraceRecorder",
    "estimate_text_tokens",
    "estimate_tokens",
    "export_otlp_http",
    "factor_for_model",
    "register_model_factor",
    "to_otlp_json",
    "TransportAdapter",
    "TransportError",
    "UsageCalibration",
    "build_step_decision_event",
    "detect_claimed_execution",
    "detect_deferred_execution",
    "detect_deferred_execution_eager",
    "detect_execution_intent_in_user_message",
    "extract_inline_tool_calls",
    "parse_grounding_verdict",
    "parse_turn_route",
    "load_transcript",
    "project_transcript",
    "reduce_execution_state",
    "should_run_grounding_judge",
    "subagent_tool_descriptor",
    "tool",
]

__version__ = "0.1.0"
