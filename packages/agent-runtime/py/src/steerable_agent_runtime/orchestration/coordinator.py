"""Generic Coordinator runner using framework ChatLoop.

The Coordinator's job is to run a single turn of an invisible agent, forcing
it to call a specific plan tool (or fallback to structured text) to output
a multi-agent plan.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from steerable_agent_runtime.chat_loop import (
    HOOK_SKIP,
    ChatLoop,
    ContentDeltaCtx,
    EmitCtx,
    LLMMessage,
    LoopConfig,
    SendMessagesCtx,
)
from steerable_agent_runtime.llm import LLMProvider
from steerable_agent_runtime.tools import ToolRouter

from steerable_agent_runtime.orchestration.plan import (
    OrchestrationPlan,
    PlanValidationError,
    validate_plan,
    _maybe_json_decode_list,
)

logger = logging.getLogger(__name__)

COORDINATOR_TOOL_NAME = "make_orchestration_plan"

_COORDINATOR_TOOL_DESCRIPTION = (
    "[Coordinator-only] Emit an orchestration plan that routes the "
    "user's message to one or more worker agents. MUST be the only "
    "thing the Coordinator outputs."
)

_COORDINATOR_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rationale": {
            "type": "string",
            "description": "Short rationale explaining this plan.",
        },
        "mode": {
            "type": "string",
            "enum": ["parallel", "sequential", "dag"],
            "description": "Rendering hint / mode.",
        },
        "tasks": {
            "type": "array",
            "description": "List of subtasks.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "agentId": {"type": "string"},
                    "prompt": {"type": "string"},
                    "dependsOn": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "readOutputsFrom": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["id", "agentId", "prompt"],
            },
        },
    },
    "required": ["rationale", "mode", "tasks"],
}


@dataclass
class CoordinatorResult:
    """The outcome of driving a Coordinator loop."""

    plan_dict: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    content_text: str = ""
    captured_plans: list[dict[str, Any]] = field(default_factory=list)


def extract_plan_from_text(text: str) -> Optional[dict[str, Any]]:
    """Defensively parse a plan out of raw text if the tool_choice was ignored.

    Scans for JSON-like blocks or markdown code fences and tries to locate
    the required keys (`rationale`, `mode`, `tasks`).
    """
    if not text:
        return None

    # Fast check for simple code fence
    cleaned = text.strip()
    if "```" in cleaned:
        for block in cleaned.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block.startswith("{") and block.endswith("}"):
                try:
                    obj = json.loads(block)
                    if isinstance(obj, dict) and "tasks" in obj:
                        return obj
                except Exception:
                    pass

    # Fallback to balanced braces scan
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(cleaned[start:end+1])
            if isinstance(obj, dict) and "tasks" in obj:
                return obj
        except Exception:
            pass

    return None


async def run_coordinator(
    *,
    provider: LLMProvider,
    system_prompt: str,
    user_message: str,
    allowed_agent_ids: set[str],
    require_full_coverage: bool,
    max_tasks: int = 12,
    max_layer_depth: int = 6,
) -> CoordinatorResult:
    """Execute a Coordinator loop to construct a multi-agent orchestration plan.

    This executes an invisible, single-round loop, forcing a tool call to
    ``make_orchestration_plan``, capturing its arguments, and returning the
    validated plan.
    """
    captured_plans: list[dict[str, Any]] = []
    content_parts: list[str] = []

    async def make_orchestration_plan(
        rationale: str = "",
        mode: str = "dag",
        tasks: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        plan = {
            "rationale": rationale or "",
            "mode": mode or "dag",
            "tasks": list(tasks or []),
        }
        captured_plans.append(plan)
        return {
            "success": True,
            "message": "plan recorded",
            "data": {"plan": plan},
        }

    router = ToolRouter()
    router.register(
        make_orchestration_plan,
        name=COORDINATOR_TOOL_NAME,
        description=_COORDINATOR_TOOL_DESCRIPTION,
        schema=_COORDINATOR_TOOL_SCHEMA,
        mode="read",
        require_consent=False,
    )

    initial_messages = [
        LLMMessage(role="user", content=user_message),
    ]

    config = LoopConfig(
        provider=provider,
        provider_kind="openai_compat",
        tool_router=router,
        initial_messages=initial_messages,
        max_rounds=1,
        max_elapsed_seconds=30.0,
        tool_choice={
            "type": "function",
            "function": {"name": COORDINATOR_TOOL_NAME},
        },
    )

    loop = ChatLoop(config)

    async def _inject_system(ctx: SendMessagesCtx) -> None:
        if ctx.messages and ctx.messages[0].role == "system":
            return
        ctx.messages.insert(0, LLMMessage(role="system", content=system_prompt))

    async def _release_tool_choice(ctx: SendMessagesCtx) -> None:
        if ctx.round_index > 0:
            ctx.tool_choice = None

    async def _capture_content(ctx: ContentDeltaCtx) -> None:
        if ctx.delta:
            content_parts.append(ctx.delta)

    async def _suppress_emit(_ctx: EmitCtx) -> object:
        # Generic coordinator is silent. No events surface.
        return HOOK_SKIP

    loop.on("before_send_messages", _inject_system)
    loop.on("before_send_messages", _release_tool_choice)
    loop.on("content_delta", _capture_content)
    loop.on("emit", _suppress_emit)

    import json # imported inline for text extraction in exception paths

    try:
        async for _ in loop.run():
            pass
    except Exception as e:
        logger.exception("orchestration_coordinator_run_failed")
        return CoordinatorResult(
            error=f"协调员执行发生异常: {e}",
            content_text="".join(content_parts),
            captured_plans=captured_plans,
        )

    content_text = "".join(content_parts)

    if captured_plans:
        return CoordinatorResult(
            plan_dict=dict(captured_plans[-1]),
            content_text=content_text,
            captured_plans=captured_plans,
        )

    # Fallback to parsing raw text
    if content_text.strip():
        recovered = extract_plan_from_text(content_text)
        if recovered is not None:
            return CoordinatorResult(
                plan_dict=recovered,
                content_text=content_text,
                captured_plans=captured_plans,
            )

    return CoordinatorResult(
        error="协调员未输出 valid plan",
        content_text=content_text,
        captured_plans=captured_plans,
    )
