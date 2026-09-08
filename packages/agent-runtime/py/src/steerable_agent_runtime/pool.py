"""AgentPool — concurrent child CoreLoops for one parent, budgeted and
lineage-tracked.

The pool is the framework's single child-execution engine, shared by both
multi-agent surfaces:

- the six-tool orchestration family (``orchestration.py`` — the parent
  model drives ``agent_spawn``/``agent_send``/``agent_wait``/...), and
- the single-tool delegation seam (``subagent.py`` — ``delegate_subagent``
  spawns into a pool and awaits the terminal outcome synchronously).

Design:
- **Lineage as data.** Child ids are ``<lineage>.<seq>`` (root ``0``), so
  ``0.2`` is the root's second child and ``0.2.1`` its first grandchild.
- **Budgets fail closed.** ``max_parallel`` refuses at the boundary
  (``OrchestrationBudgetExceeded``), never silently queues.
- **One pool, several drivers.** The constructor's ``loop_factory`` is the
  default child builder; a caller whose children differ per spawn (the
  delegation seam resolves a named profile per call) passes a per-spawn
  ``loop_factory`` override instead. Sharing one pool instance between the
  two surfaces keeps one ``max_parallel`` budget and one lineage space.
- **Children stay out of the parent record.** A child's transcript is its
  own loop's; hosts that want child traces subscribe via ``event_sink``
  (lifecycle events with lineage) or wrap children in their own hooks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .llm import LLMMessage
from .loop import CoreLoop

logger = logging.getLogger(__name__)

#: Grace between a cooperative close (``loop.cancel()``) and the hard-cancel
#: backstop for a child that fails to wind down.
_CLOSE_GRACE_S = 2.0


class OrchestrationBudgetExceeded(Exception):
    """Raised by ``AgentPool.spawn`` at the parallel cap — fail closed."""


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    """Tunables for the orchestration layer (pool budgets + tool family).

    ``max_depth`` counts loop nesting: 1 means the parent spawns children
    that cannot spawn themselves (today's depth-1 delegation). ``max_parallel``
    caps concurrently running children per pool. ``wait_timeout_s`` bounds a
    ``agent_wait`` without an explicit ``timeoutMs`` — kept under the loop's
    per-tool backstop (default 5 min) so a hung child surfaces as a
    ``running`` outcome, not a tool timeout. The six tool names configure
    the orchestration family the ``OrchestrationExecutor`` exposes.
    """

    max_depth: int = 1
    max_parallel: int = 4
    child_max_rounds: int = 8
    wait_timeout_s: float = 120.0
    spawn_tool: str = "agent_spawn"
    send_tool: str = "agent_send"
    wait_tool: str = "agent_wait"
    close_tool: str = "agent_close"
    list_tool: str = "agent_list"
    interrupt_tool: str = "agent_interrupt"

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(
            {
                self.spawn_tool,
                self.send_tool,
                self.wait_tool,
                self.close_tool,
                self.list_tool,
                self.interrupt_tool,
            }
        )


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """Terminal (or snapshot) state of one child, returned by ``wait``."""

    child_id: str
    #: completed | error | cancelled | running (wait timed out, child alive)
    status: str
    answer: str


class _ChildHandle:
    def __init__(self, child_id: str, loop: CoreLoop) -> None:
        self.child_id = child_id
        self.loop = loop
        self.task: asyncio.Task[None] | None = None
        self.outcome: ChildOutcome | None = None
        #: Original spawn task (for list display and budget accounting).
        self.task_desc = ""
        #: Advertised tool schemas of the current/last run — reused on resume.
        self.schemas: list[dict[str, Any]] | None = None
        #: Terminal flag set by close()/shutdown(): a closed child rejects
        #: sends even after its run has finished.
        self.closed = False
        #: Model-visible record snapshot at the end of the last cleanly
        #: finished run (cooperative cancel included — the loop guarantees
        #: no dangling tool_calls). ``None`` before the first clean finish;
        #: the hard-cancel backstop path leaves no resumable record.
        self.resume_messages: list[LLMMessage] | None = None


#: Builds a child's loop and advertised tool schemas for a child id and tool
#: filter. Owned by the driving executor (``OrchestrationExecutor`` keeps
#: depth narrowing and tool-domain narrowing in one place; the delegation
#: seam resolves the named profile's provider and round bound).
LoopFactory = Callable[
    [str, frozenset[str] | None], tuple[CoreLoop, list[dict[str, Any]] | None]
]


class AgentPool:
    """Concurrent child CoreLoops for one parent, budgeted and lineage-tracked.

    ``loop_factory`` is the default child builder; individual spawns may
    pass their own (see the module docstring for why the delegation seam
    does). ``event_sink`` receives lifecycle events (``child_spawned`` …)
    as ``(kind, data)`` — synchronous, fired inside tool execution.
    """

    def __init__(
        self,
        *,
        config: OrchestrationConfig,
        depth: int,
        lineage: str,
        loop_factory: LoopFactory | None = None,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._config = config
        self._depth = depth
        self._lineage = lineage
        self._loop_factory = loop_factory
        self._event_sink = event_sink
        self._children: dict[str, _ChildHandle] = {}
        self._seq = 0

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self._event_sink is not None:
            self._event_sink(kind, data)

    def spawn(
        self,
        task: str,
        tool_filter: frozenset[str] | None,
        *,
        loop_factory: LoopFactory | None = None,
        event_extra: dict[str, Any] | None = None,
    ) -> _ChildHandle:
        """Start a child loop immediately; returns its handle.

        ``loop_factory`` overrides the constructor's default for this spawn;
        ``event_extra`` merges into the ``child_spawned`` payload (the
        delegation seam adds the resolved ``profile`` name there).
        """
        factory = loop_factory or self._loop_factory
        if factory is None:
            raise RuntimeError(
                "AgentPool.spawn needs a loop factory — none was given at "
                "construction or per spawn"
            )
        live = [h for h in self._children.values() if h.outcome is None]
        if len(live) >= self._config.max_parallel:
            raise OrchestrationBudgetExceeded(
                f"parallel cap {self._config.max_parallel} reached "
                f"({len(live)} children running); wait for one to finish "
                "or close one before spawning again"
            )
        self._seq += 1
        child_id = f"{self._lineage}.{self._seq}"
        loop, schemas = factory(child_id, tool_filter)
        handle = _ChildHandle(child_id, loop)
        handle.task_desc = task
        handle.schemas = schemas
        self._children[child_id] = handle
        handle.task = asyncio.ensure_future(
            self._run_child(handle, [LLMMessage.text_of("user", task)])
        )
        self._emit(
            "child_spawned",
            {
                "childId": child_id,
                "depth": self._depth + 1,
                "task": task,
                **(event_extra or {}),
            },
        )
        return handle

    async def run(
        self,
        task: str,
        tool_filter: frozenset[str] | None,
        *,
        loop_factory: LoopFactory | None = None,
        event_extra: dict[str, Any] | None = None,
    ) -> ChildOutcome:
        """Synchronous delegation: spawn a child and await its terminal
        outcome. The caller's cancellation propagates into the child run
        (a parent turn cancel winds the child down with it)."""
        handle = self.spawn(
            task, tool_filter, loop_factory=loop_factory, event_extra=event_extra
        )
        assert handle.task is not None
        await handle.task
        # _run_child sets the outcome on every non-cancelled exit path.
        assert handle.outcome is not None
        return handle.outcome

    async def _run_child(
        self, handle: _ChildHandle, seed: list[LLMMessage]
    ) -> None:
        answer_parts: list[str] = []
        status = "completed"
        try:
            async for event in handle.loop.run(seed, tools=handle.schemas):
                if event.kind == "content_delta":
                    answer_parts.append(str(event.data.get("delta") or ""))
                elif event.kind == "completion":
                    status = str(event.data.get("status") or "completed")
        except asyncio.CancelledError:
            # Hard-cancel backstop path (close/shutdown grace expired). No
            # resume snapshot: the record may hold dangling tool_calls.
            handle.outcome = ChildOutcome(handle.child_id, "cancelled", "")
            self._emit("child_cancelled", {"childId": handle.child_id})
            raise
        except Exception as exc:  # noqa: BLE001 — a child run can raise anything
            # (provider, executor, bugs); the pool must survive with the
            # failure recorded as the child's outcome, never an escape that
            # leaves wait() hanging on an unset outcome.
            logger.warning("child %s failed: %s", handle.child_id, exc)
            handle.outcome = ChildOutcome(handle.child_id, "error", str(exc))
            self._emit("child_failed", {"childId": handle.child_id, "error": str(exc)})
            return
        # Clean finish (completion or cooperative cancel): the record has no
        # dangling tool_calls, so it is a valid resume seed for follow-ups.
        handle.resume_messages = list(handle.loop.history.projection)
        answer = "".join(answer_parts).strip()
        handle.outcome = ChildOutcome(handle.child_id, status, answer)
        kind = "child_completed" if status == "completed" else "child_failed"
        self._emit(kind, {"childId": handle.child_id, "status": status})

    def get(self, child_id: str) -> _ChildHandle | None:
        return self._children.get(child_id)

    async def wait(self, child_id: str, timeout_s: float | None) -> ChildOutcome | None:
        handle = self._children.get(child_id)
        if handle is None:
            return None
        if handle.outcome is not None:
            return handle.outcome
        assert handle.task is not None
        try:
            # Shielded: a wait timeout must not cancel the child.
            await asyncio.wait_for(
                asyncio.shield(handle.task),
                timeout=timeout_s or self._config.wait_timeout_s,
            )
        except asyncio.TimeoutError:
            return ChildOutcome(child_id, "running", "")
        return handle.outcome or ChildOutcome(child_id, "cancelled", "")

    def send(self, child_id: str, message: str) -> str:
        """Deliver a message: steer a running child, resume a finished one.

        Returns ``"steered"`` / ``"resumed"`` / an ``error:*`` string the
        executor maps onto a failed ToolResult — the model needs the
        distinction to recover (e.g. spawn a fresh child after closing one).
        """
        handle = self._children.get(child_id)
        if handle is None:
            return f"error:unknown_child: {child_id!r} is not a child of this pool"
        if handle.closed:
            return f"error:child_closed: {child_id!r} is closed; spawn a new child"
        if handle.outcome is None:
            handle.loop.steer(message)
            return "steered"
        # Follow-up turn on a finished/interrupted child. The resume seed is
        # the clean-finish record snapshot plus the new user message.
        if handle.resume_messages is None:
            return (
                f"error:child_not_resumable: {child_id!r} ended without a "
                "clean record (hard-cancelled); spawn a new child"
            )
        live = [h for h in self._children.values() if h.outcome is None]
        if len(live) >= self._config.max_parallel:
            return (
                f"error:orchestration_budget_exceeded: parallel cap "
                f"{self._config.max_parallel} reached; wait for a child to "
                "finish before resuming this one"
            )
        seed = [*handle.resume_messages, LLMMessage.text_of("user", message)]
        handle.outcome = None
        handle.loop.reset_cancel()
        handle.task = asyncio.ensure_future(self._run_child(handle, seed))
        self._emit("child_resumed", {"childId": child_id})
        return "resumed"

    def interrupt(self, child_id: str) -> bool:
        """Cooperatively cancel the child's current turn, keeping it
        addressable: the clean-finish snapshot preserves its record and a
        later send() resumes it. False when the child is unknown, closed, or
        not running."""
        handle = self._children.get(child_id)
        if handle is None or handle.closed or handle.outcome is not None:
            return False
        handle.loop.cancel()
        self._emit("child_interrupted", {"childId": child_id})
        return True

    def list(self) -> list[dict[str, Any]]:
        """Snapshot of every child: id, status, task, and answer preview."""
        entries: list[dict[str, Any]] = []
        for handle in self._children.values():
            if handle.closed:
                status = "closed"
            elif handle.outcome is None:
                status = "running"
            elif handle.outcome.status == "cancelled":
                # Cooperative cancel via interrupt() — resumable, distinct
                # from a hard-cancelled (closed) child.
                status = "interrupted"
            else:
                status = handle.outcome.status
            entry: dict[str, Any] = {
                "childId": handle.child_id,
                "status": status,
                "task": handle.task_desc,
            }
            if handle.outcome is not None and handle.outcome.answer:
                entry["answerPreview"] = handle.outcome.answer[:200]
            entries.append(entry)
        return entries

    async def close(self, child_id: str) -> bool:
        handle = self._children.get(child_id)
        if handle is None:
            return False
        handle.closed = True
        if handle.outcome is not None:
            return True
        handle.loop.cancel()
        assert handle.task is not None
        try:
            await asyncio.wait_for(asyncio.shield(handle.task), timeout=_CLOSE_GRACE_S)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if not handle.task.done():
                handle.task.cancel()
        return True

    async def shutdown(self) -> None:
        """Cooperatively cancel every live child; hard-cancel stragglers.

        Terminal for the whole pool: every child is marked closed, so no
        sends or resumes are accepted afterwards.
        """
        live = [h for h in self._children.values() if h.outcome is None]
        for handle in self._children.values():
            handle.closed = True
        for handle in live:
            handle.loop.cancel()
        for handle in live:
            assert handle.task is not None
            try:
                await asyncio.wait_for(
                    asyncio.shield(handle.task), timeout=_CLOSE_GRACE_S
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not handle.task.done():
                    handle.task.cancel()
