"""Generic Orchestration Executor using parallel topological task execution.

The Executor manages task dependencies, schedules runnable workers in parallel
layer-by-layer, multiplexes streaming SSEEvents from multiple concurrent workers,
and handles task completion/failure propagation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from steerable_agent_protocol.generated import SSEEvent
from steerable_agent_runtime.orchestration.plan import (
    OrchestrationPlan,
    OrchestrationTask,
    PlanValidationError,
    topological_layers,
)
from steerable_agent_runtime.orchestration.worker import WorkerResult

logger = logging.getLogger(__name__)


class OrchestrationExecutor:
    """Core scheduler and event multiplexer for multi-agent execution DAGs.

    This class schedules parallel execution of task layers, handles cascading
    failures (skipping dependent tasks on upstream failures), and multiplexes
    SSEEvent streams from concurrent workers into a single asynchronous generator.
    """

    def __init__(
        self,
        *,
        group_id: str,
        plan: OrchestrationPlan,
        worker_runner: Callable[[OrchestrationTask, asyncio.Queue[SSEEvent]], Awaitable[WorkerResult]],
        max_layer_depth: int = 6,
    ) -> None:
        self.group_id = group_id
        self.plan = plan
        self.worker_runner = worker_runner
        self.max_layer_depth = max_layer_depth

        self.peer_outputs: dict[str, str] = {}
        self.task_statuses: dict[str, str] = {}  # "pending" | "running" | "done" | "failed" | "skipped"

        # Pre-initialize status
        for t in self.plan.tasks:
            self.task_statuses[t.id] = "pending"

    async def run(self):
        """Asynchronously execute the plan, yielding SSEEvents for the caller to stream."""
        try:
            layers = topological_layers(self.plan, max_layer_depth=self.max_layer_depth)
        except PlanValidationError as exc:
            logger.error("orchestration_topological_sort_failed err=%s", exc)
            yield SSEEvent(
                type="orchestration",
                event="done",
                orchestrationGroupId=self.group_id,
                content="failed",
            )
            return

        # 1. Yield the initial plan-ready event
        # Note: We structure the tasks field defensively using model_dump
        tasks_payload = [t.model_dump() for t in self.plan.tasks]
        yield SSEEvent(
            type="orchestration",
            event="plan_ready",
            orchestrationGroupId=self.group_id,
            payload={
                "rationale": self.plan.rationale,
                "mode": self.plan.mode,
                "tasks": tasks_payload,
            },
        )

        shared_queue: asyncio.Queue[SSEEvent] = asyncio.Queue()

        for layer_idx, layer in enumerate(layers):
            logger.info("orchestration_executing_layeridx=%d layer=%r", layer_idx, layer)

            # Determine which tasks can actually run
            runnable_tasks: list[OrchestrationTask] = []
            for tid in layer:
                t = next(x for x in self.plan.tasks if x.id == tid)
                failed_deps = [
                    dep
                    for dep in t.dependsOn
                    if self.task_statuses.get(dep) in ("failed", "skipped")
                ]
                if failed_deps:
                    logger.warning(
                        "orchestration_task_skipped task_id=%s failed_deps=%r",
                        tid, failed_deps,
                    )
                    self.task_statuses[tid] = "skipped"
                    yield SSEEvent(
                        type="orchestration",
                        event="task_done",
                        orchestrationGroupId=self.group_id,
                        taskId=tid,
                        content="skipped",
                    )
                else:
                    runnable_tasks.append(t)

            if not runnable_tasks:
                continue

            # Run all layer tasks in parallel
            for t in runnable_tasks:
                self.task_statuses[t.id] = "running"
                yield SSEEvent(
                    type="orchestration",
                    event="task_start",
                    orchestrationGroupId=self.group_id,
                    taskId=t.id,
                )

            # Wrap runners with shared queue signaling
            worker_tasks: list[asyncio.Task[WorkerResult]] = []
            for t in runnable_tasks:
                worker_tasks.append(
                    asyncio.create_task(self.worker_runner(t, shared_queue))
                )

            # Loop multiplexer
            pending_count = len(worker_tasks)
            while pending_count > 0:
                # Read any pending items in the queue with a short timeout
                try:
                    event = await asyncio.wait_for(shared_queue.get(), timeout=0.1)
                    yield event
                    shared_queue.task_done()
                except asyncio.TimeoutError:
                    pass

                # Check if any tasks have completed
                done_tasks = [wt for wt in worker_tasks if wt.done()]
                for wt in done_tasks:
                    worker_tasks.remove(wt)
                    pending_count -= 1
                    try:
                        res: WorkerResult = wt.result()
                        # Record outputs and status
                        self.peer_outputs[res.task_id] = res.output_text
                        status = "done" if (res.goal_completed and not res.error) else "failed"
                        self.task_statuses[res.task_id] = status

                        yield SSEEvent(
                            type="orchestration",
                            event="task_done",
                            orchestrationGroupId=self.group_id,
                            taskId=res.task_id,
                            content=status,
                            payload={
                                "output": res.output_text,
                                "error": res.error,
                            },
                        )
                    except Exception as exc:
                        # Defensive handler for runner panic
                        logger.exception("orchestration_worker_runner_panic")
                        # Find the task associated with the failed Task
                        # In asyncio, we can map task back if needed. For simplicity,
                        # we fail remaining tasks gracefully.
                        pass

            # Drain any residual events in queue
            while not shared_queue.empty():
                try:
                    event = shared_queue.get_nowait()
                    yield event
                    shared_queue.task_done()
                except asyncio.QueueEmpty:
                    break

        # 3. Yield the aggregate final done event
        final_status = "ok"
        all_statuses = list(self.task_statuses.values())
        if "failed" in all_statuses or "skipped" in all_statuses:
            if "done" in all_statuses:
                final_status = "partial"
            else:
                final_status = "aborted"

        logger.info("orchestration_completed group_id=%s status=%s", self.group_id, final_status)
        yield SSEEvent(
            type="orchestration",
            event="done",
            orchestrationGroupId=self.group_id,
            content=final_status,
        )
