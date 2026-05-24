"""Generic Worker agent runner using framework ChatLoop.

Each worker completes a single task from the Coordinator's plan. It runs
in an isolated ChatLoop, with context outputs from prior dependent tasks injected.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

from steerable_agent_protocol.generated import SSEEvent
from steerable_agent_runtime.chat_loop import (
    ChatLoop,
    ContentDeltaCtx,
    EmitCtx,
    LLMMessage,
    LoopConfig,
    SendMessagesCtx,
)
from steerable_agent_runtime.llm import LLMProvider
from steerable_agent_runtime.tools import ToolRouter

from steerable_agent_runtime.orchestration.plan import OrchestrationTask
from steerable_agent_runtime.orchestration.sse_reframe import reframe_worker_event

logger = logging.getLogger(__name__)


def build_peer_outputs_block(
    task: OrchestrationTask,
    peer_outputs: dict[str, str],
) -> str:
    """Format outputs from dependsOn nodes into a clear Chinese markdown reference block."""
    if not task.readOutputsFrom:
        return ""

    blocks: list[str] = []
    for upstream_id in task.readOutputsFrom:
        text = peer_outputs.get(upstream_id, "").strip()
        if not text:
            continue
        blocks.append(
            f"--- 依赖任务结果 (ID: {upstream_id}) ---\n"
            f"{text}\n"
            f"--- 依赖任务结果结束 (ID: {upstream_id}) ---"
        )

    if not blocks:
        return ""

    header = (
        "\n\n请务必参考以下前置任务的执行结果：\n"
        "这些结果已由其他专家智能体产出。你应该将其作为你的主要参考上下文和信息输入。\n"
    )
    return header + "\n\n".join(blocks) + "\n"


@dataclass
class WorkerResult:
    """The final outcomes and state of a worker execution."""

    task_id: str
    agent_id: str
    output_text: str
    error: Optional[str] = None
    goal_completed: bool = True


async def run_worker(
    *,
    group_id: str,
    task: OrchestrationTask,
    provider: LLMProvider,
    tool_router: ToolRouter,
    system_prompt: str,
    user_message: str,
    queue: asyncio.Queue[SSEEvent],
    goal_verifier: Optional[Callable[[str, str], bool]] = None,
) -> WorkerResult:
    """Run an isolated ChatLoop for a single OrchestrationTask, streaming reframed SSEEvents.

    Accepts custom prompt structures, drives the run, and uses reframe_worker_event
    to multiplex its SSE output into the shared queue.
    """
    output_parts: list[str] = []

    async def _inject_system(ctx: SendMessagesCtx) -> None:
        if ctx.messages and ctx.messages[0].role == "system":
            return
        ctx.messages.insert(0, LLMMessage(role="system", content=system_prompt))

    async def _capture_content(ctx: ContentDeltaCtx) -> None:
        if ctx.delta:
            output_parts.append(ctx.delta)

    async def _reframe_and_queue(ctx: EmitCtx) -> None:
        if not ctx.event:
            return
        reframed = reframe_worker_event(
            ctx.event,
            group_id=group_id,
            task_id=task.id,
        )
        if reframed:
            await queue.put(reframed)

    initial_messages = [
        LLMMessage(role="user", content=user_message),
    ]

    config = LoopConfig(
        provider=provider,
        provider_kind="openai_compat",
        tool_router=tool_router,
        initial_messages=initial_messages,
        max_rounds=5,
        max_elapsed_seconds=120.0,
    )

    loop = ChatLoop(config)
    loop.on("before_send_messages", _inject_system)
    loop.on("content_delta", _capture_content)
    loop.on("emit", _reframe_and_queue)

    try:
        async for _ in loop.run():
            pass
    except Exception as e:
        logger.exception("orchestration_worker_run_failed task_id=%s", task.id)
        return WorkerResult(
            task_id=task.id,
            agent_id=task.agentId,
            output_text="".join(output_parts),
            error=str(e),
            goal_completed=False,
        )

    output_text = "".join(output_parts)

    goal_completed = True
    if goal_verifier is not None:
        try:
            goal_completed = goal_verifier(task.prompt, output_text)
        except Exception:
            logger.exception("orchestration_worker_verifier_failed task_id=%s", task.id)

    return WorkerResult(
        task_id=task.id,
        agent_id=task.agentId,
        output_text=output_text,
        goal_completed=goal_completed,
    )
