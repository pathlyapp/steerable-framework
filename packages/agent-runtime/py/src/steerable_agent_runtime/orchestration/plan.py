"""Structured schema, validation, and topological sorting for multi-agent plans.

This module houses the pure data layer of the orchestration engine. It defines
the Pydantic models for orchestration plans, validates plans against structural
and semantic invariants, and sorts tasks topologically to determine execution
layers.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


def _maybe_json_decode_list(value: Any) -> Any:
    """Safely coerce a double-stringified or JSON-encoded field back to a list.

    Under Anthropic gateways, nested lists (like `tasks` or `dependsOn`) are
    frequently delivered as JSON-encoded strings rather than native lists/arrays.
    This helper decodes them defensively.
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    if stripped[0] == '"' and stripped[-1] == '"' and len(stripped) >= 2:
        try:
            stripped = json.loads(stripped)
            if not isinstance(stripped, str):
                return stripped
            stripped = stripped.strip()
        except (ValueError, TypeError) as exc:
            logger.warning(
                "orchestration_json_decode_outer_unwrap_failed err=%s value=%r",
                exc, value[:200],
            )
            return value
    if not stripped or stripped[0] != "[":
        return value
    try:
        return json.loads(stripped)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "orchestration_json_decode_failed err=%s len=%d preview=%r",
            exc, len(value), value[:300],
        )
        return value


class OrchestrationTask(BaseModel):
    """A single worker invocation node in the plan DAG."""

    id: str = Field(min_length=1, max_length=32, description="Unique within this plan, e.g. 't1'.")
    agentId: str = Field(min_length=1, description="The ID of the worker agent to route to.")
    prompt: str = Field(min_length=1, max_length=2000, description="Concrete sub-instruction for the worker.")
    dependsOn: list[str] = Field(default_factory=list, description="List of task IDs that must complete first.")
    readOutputsFrom: list[str] = Field(default_factory=list, description="Subset of dependsOn to inject as reference context.")

    @model_validator(mode="before")
    @classmethod
    def _decode_string_lists(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ("dependsOn", "readOutputsFrom"):
                if key in data:
                    data[key] = _maybe_json_decode_list(data[key])
        return data


class OrchestrationPlan(BaseModel):
    """The complete multi-agent plan returned by the Coordinator."""

    rationale: str = Field(default="", max_length=600, description="A rationale explaining this division of labor.")
    mode: str = Field(default="dag", description='"parallel" | "sequential" | "dag" -- UI hint.')
    tasks: list[OrchestrationTask] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _decode_stringified_tasks(cls, data: Any) -> Any:
        if isinstance(data, dict) and "tasks" in data:
            data["tasks"] = _maybe_json_decode_list(data["tasks"])
        return data


class PlanValidationError(ValueError):
    """Raised when a plan fails structural or semantic validation checks."""

    def __init__(self, message: str, *, code: str = "invalid_plan") -> None:
        super().__init__(message)
        self.code = code
        self.user_message = message


def validate_plan(
    raw: Any,
    *,
    allowed_agent_ids: set[str],
    require_full_coverage: bool,
    max_tasks: int = 12,
    max_layer_depth: int = 6,
) -> OrchestrationPlan:
    """Coerce and validate a raw plan dictionary against DAG invariants.

    Raises PlanValidationError if any invariant is violated.
    """
    if not isinstance(raw, dict):
        raise PlanValidationError("plan 不是 JSON 对象")

    try:
        plan = OrchestrationPlan.model_validate(raw)
    except Exception as e:
        raise PlanValidationError(f"plan schema 不合法: {e}") from e

    if len(plan.tasks) > max_tasks:
        raise PlanValidationError(f"plan 包含 {len(plan.tasks)} 个 task，超过上限 {max_tasks}")

    if not plan.tasks and require_full_coverage:
        raise PlanValidationError("不能输出空 tasks（必须分配工作）")

    seen_ids: set[str] = set()
    for t in plan.tasks:
        if t.id in seen_ids:
            raise PlanValidationError(f"task id 重复: {t.id}")
        seen_ids.add(t.id)

    for t in plan.tasks:
        if t.agentId not in allowed_agent_ids:
            raise PlanValidationError(
                f"task {t.id} 指向了不允许的 agentId={t.agentId}"
            )
        bad_deps = [d for d in t.dependsOn if d not in seen_ids]
        if bad_deps:
            raise PlanValidationError(
                f"task {t.id} 的 dependsOn 包含未知 id: {bad_deps}"
            )
        bad_reads = [r for r in t.readOutputsFrom if r not in t.dependsOn]
        if bad_reads:
            raise PlanValidationError(
                f"task {t.id} 的 readOutputsFrom 必须是 dependsOn 的子集，多了: {bad_reads}"
            )
        if t.id in t.dependsOn:
            raise PlanValidationError(f"task {t.id} 不能依赖自己")

    return plan


def topological_layers(
    plan: OrchestrationPlan,
    *,
    max_layer_depth: int = 6,
) -> list[list[str]]:
    """Determine the parallel execution layers using topological sort.

    Raises PlanValidationError if a dependency cycle is detected or depth is exceeded.
    """
    task_by_id = {t.id: t for t in plan.tasks}
    in_degree = {t.id: len(t.dependsOn) for t in plan.tasks}
    adj_list: dict[str, list[str]] = {t.id: [] for t in plan.tasks}
    for t in plan.tasks:
        for dep in t.dependsOn:
            adj_list[dep].append(t.id)

    current_layer = [tid for tid, deg in in_degree.items() if deg == 0]
    layers: list[list[str]] = []
    processed_count = 0

    while current_layer:
        if len(layers) >= max_layer_depth:
            raise PlanValidationError(
                f"plan 依赖层数超过上限 {max_layer_depth} 层，可能存在过于冗长的依赖关系"
            )
        layers.append(sorted(current_layer))
        processed_count += len(current_layer)

        next_layer: list[str] = []
        for tid in current_layer:
            for neighbor in adj_list[tid]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_layer.append(neighbor)
        current_layer = next_layer

    if processed_count < len(plan.tasks):
        raise PlanValidationError("plan 存在循环依赖，无法执行")

    return layers
