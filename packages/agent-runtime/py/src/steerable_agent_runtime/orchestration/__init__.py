"""Orchestration package — multi-agent coordination, scheduling, and execution.

This module exports standard primitives for running multi-agent workflows:
- Coordinator loop runner and text extractor
- Worker loop runner and peer output context builder
- Orchestration executor with parallel scheduling and event multiplexing
- Validation structures (OrchestrationPlan, OrchestrationTask) and topological sorter
"""

from .coordinator import (
    COORDINATOR_TOOL_NAME,
    CoordinatorResult,
    run_coordinator,
)
from .dispatch import (
    GroupChatStatus,
    OrchestrationDecision,
    decide_orchestration,
)
from .executor import OrchestrationExecutor
from .plan import (
    OrchestrationPlan,
    OrchestrationTask,
    PlanValidationError,
    topological_layers,
    validate_plan,
)
from .sse_reframe import reframe_worker_event
from .worker import (
    WorkerResult,
    build_peer_outputs_block,
    run_worker,
)

__all__ = [
    "COORDINATOR_TOOL_NAME",
    "CoordinatorResult",
    "run_coordinator",
    "GroupChatStatus",
    "OrchestrationDecision",
    "decide_orchestration",
    "OrchestrationExecutor",
    "OrchestrationPlan",
    "OrchestrationTask",
    "PlanValidationError",
    "topological_layers",
    "validate_plan",
    "reframe_worker_event",
    "WorkerResult",
    "build_peer_outputs_block",
    "run_worker",
]
