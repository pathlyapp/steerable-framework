"""Multi-agent orchestration — a parent CoreLoop driving parallel children.

Extends the depth-1 delegation seam (``subagent.py``) into the framework's
orchestration layer (P3.1): a pool of concurrent child ``CoreLoop`` instances
with coordination primitives the parent model drives through four tools —
``agent_spawn`` / ``agent_send`` / ``agent_wait`` / ``agent_close``.

Design:

- **Lineage as data.** Child ids are ``<lineage>.<seq>`` (root ``0``), so
  ``0.2`` is the root's second child and ``0.2.1`` its first grandchild.
  Every tool result carries the id as structured JSON — the parent record
  alone rebuilds who was spawned and how they ended.
- **Budgets fail closed.** ``max_parallel`` and ``max_depth`` refuse at the
  boundary (``orchestration_budget_exceeded`` / no orchestration tools in
  the child), never silently queue. Depth is structural: a child only has
  orchestration tools when its own executor nests another
  ``OrchestrationExecutor``, which happens iff ``depth + 1 < max_depth``.
- **Coordination reuses loop primitives.** ``agent_send`` is ``loop.steer``;
  ``agent_close`` is the cooperative ``loop.cancel()`` with a hard-cancel
  backstop — a closed child winds down with a consistent record, same as a
  user-cancelled turn.
- **Children stay out of the parent record.** A child's transcript is its
  own loop's; the parent sees spawn/wait/close results. Hosts that want
  child traces subscribe via ``event_sink`` (lifecycle events with lineage)
  or wrap children in their own hooks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from steerable_agent_protocol.generated import ToolCall, ToolResult

from .llm import LLMMessage, LLMProvider
from .loop import CoreLoop, LoopConfig, LoopContext, LoopHooks, ToolExecutor
from .subagent import FilteredToolsExecutor

logger = logging.getLogger(__name__)

#: Grace between a cooperative close (``loop.cancel()``) and the hard-cancel
#: backstop for a child that fails to wind down.
_CLOSE_GRACE_S = 2.0


class OrchestrationBudgetExceeded(Exception):
    """Raised by ``AgentPool.spawn`` at the parallel cap — fail closed."""


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    """Tunables for the orchestration tool family.

    ``max_depth`` counts loop nesting: 1 means the parent spawns children
    that cannot spawn themselves (today's depth-1 delegation). ``max_parallel``
    caps concurrently running children per pool. ``wait_timeout_s`` bounds a
    ``agent_wait`` without an explicit ``timeoutMs`` — kept under the loop's
    per-tool backstop (default 5 min) so a hung child surfaces as a
    ``running`` outcome, not a tool timeout.
    """

    max_depth: int = 1
    max_parallel: int = 4
    child_max_rounds: int = 8
    wait_timeout_s: float = 120.0
    spawn_tool: str = "agent_spawn"
    send_tool: str = "agent_send"
    wait_tool: str = "agent_wait"
    close_tool: str = "agent_close"

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(
            {self.spawn_tool, self.send_tool, self.wait_tool, self.close_tool}
        )


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """Terminal (or snapshot) state of one child, returned by ``wait``."""

    child_id: str
    #: completed | error | cancelled | running (wait timed out, child alive)
    status: str
    answer: str


def orchestration_tool_descriptors(
    config: OrchestrationConfig | None = None,
) -> list[dict[str, Any]]:
    """OpenAI tool schemas for the four orchestration tools."""
    config = config or OrchestrationConfig()

    def fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    return [
        fn(
            config.spawn_tool,
            "Spawn a sub-agent with its own reasoning loop to work on a "
            "self-contained task concurrently. Returns immediately with a "
            "childId; use agent_wait to collect its answer.",
            {
                "task": {
                    "type": "string",
                    "description": (
                        "Complete, self-contained instructions for the "
                        "sub-agent — it sees none of this conversation."
                    ),
                },
                "toolFilter": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional allow-list of tool names the child may "
                        "call; omitted delegates the full tool domain."
                    ),
                },
            },
            ["task"],
        ),
        fn(
            config.send_tool,
            "Send a message into a running sub-agent; it is delivered at "
            "the child's next round boundary.",
            {
                "childId": {"type": "string"},
                "message": {"type": "string"},
            },
            ["childId", "message"],
        ),
        fn(
            config.wait_tool,
            "Wait for a sub-agent to finish and return its outcome. A child "
            "still running at the timeout returns status 'running'.",
            {
                "childId": {"type": "string"},
                "timeoutMs": {"type": "integer"},
            },
            ["childId"],
        ),
        fn(
            config.close_tool,
            "Close a sub-agent: cooperative cancel with a hard-cancel "
            "backstop. The child's partial work is discarded.",
            {"childId": {"type": "string"}},
            ["childId"],
        ),
    ]


class _ChildHandle:
    def __init__(self, child_id: str, loop: CoreLoop) -> None:
        self.child_id = child_id
        self.loop = loop
        self.task: asyncio.Task[None] | None = None
        self.outcome: ChildOutcome | None = None


class AgentPool:
    """Concurrent child CoreLoops for one parent, budgeted and lineage-tracked.

    ``loop_factory`` builds the child's loop and advertised tool schemas for
    a given child id and tool filter — owned by ``OrchestrationExecutor`` so
    depth narrowing and tool-domain narrowing stay in one place.
    """

    def __init__(
        self,
        *,
        config: OrchestrationConfig,
        depth: int,
        lineage: str,
        loop_factory: Callable[
            [str, frozenset[str] | None], tuple[CoreLoop, list[dict[str, Any]] | None]
        ],
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

    def spawn(self, task: str, tool_filter: frozenset[str] | None) -> _ChildHandle:
        live = [h for h in self._children.values() if h.outcome is None]
        if len(live) >= self._config.max_parallel:
            raise OrchestrationBudgetExceeded(
                f"parallel cap {self._config.max_parallel} reached "
                f"({len(live)} children running); wait for one to finish "
                "or close one before spawning again"
            )
        self._seq += 1
        child_id = f"{self._lineage}.{self._seq}"
        loop, schemas = self._loop_factory(child_id, tool_filter)
        handle = _ChildHandle(child_id, loop)
        self._children[child_id] = handle
        handle.task = asyncio.ensure_future(self._run_child(handle, task, schemas))
        self._emit(
            "child_spawned",
            {"childId": child_id, "depth": self._depth + 1, "task": task},
        )
        return handle

    async def _run_child(
        self, handle: _ChildHandle, task: str, schemas: list[dict[str, Any]] | None
    ) -> None:
        answer_parts: list[str] = []
        status = "completed"
        try:
            async for event in handle.loop.run(
                [LLMMessage.text_of("user", task)], tools=schemas
            ):
                if event.kind == "content_delta":
                    answer_parts.append(str(event.data.get("delta") or ""))
                elif event.kind == "completion":
                    status = str(event.data.get("status") or "completed")
        except asyncio.CancelledError:
            # Hard-cancel backstop path (close/shutdown grace expired).
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

    def send(self, child_id: str, message: str) -> bool:
        handle = self._children.get(child_id)
        if handle is None or handle.outcome is not None:
            return False
        handle.loop.steer(message)
        return True

    async def close(self, child_id: str) -> bool:
        handle = self._children.get(child_id)
        if handle is None:
            return False
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
        """Cooperatively cancel every live child; hard-cancel stragglers."""
        live = [h for h in self._children.values() if h.outcome is None]
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


class OrchestrationExecutor:
    """ToolExecutor decorator: the four orchestration tools drive an AgentPool.

    ``tools`` is the parent loop's advertised schemas — children advertise
    the subset their ``toolFilter`` delegates (minus the orchestration
    family, re-added only when the depth budget allows the child its own
    pool). Without it children run reasoning-only, matching the legacy
    subagent seam.
    """

    def __init__(
        self,
        inner: ToolExecutor,
        provider: LLMProvider,
        config: OrchestrationConfig | None = None,
        *,
        tools: list[dict[str, Any]] | None = None,
        hooks: LoopHooks | None = None,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
        _depth: int = 0,
        _lineage: str = "0",
    ) -> None:
        self._inner = inner
        self._provider = provider
        self._config = config or OrchestrationConfig()
        self._parent_tools = list(tools or [])
        self._hooks = hooks
        self._event_sink = event_sink
        self._depth = _depth
        self._lineage = _lineage
        self._pool = AgentPool(
            config=self._config,
            depth=_depth,
            lineage=_lineage,
            loop_factory=self._child_loop,
            event_sink=event_sink,
        )

    # ------------------------------------------------------------------
    # child construction
    # ------------------------------------------------------------------

    def _child_loop(
        self, child_id: str, tool_filter: frozenset[str] | None
    ) -> tuple[CoreLoop, list[dict[str, Any]] | None]:
        base: ToolExecutor = self._inner
        if tool_filter is not None:
            base = FilteredToolsExecutor(base, tool_filter)
        orch_names = self._config.tool_names
        schemas = [
            schema
            for schema in self._parent_tools
            if _schema_name(schema) not in orch_names
            and (tool_filter is None or _schema_name(schema) in tool_filter)
        ]
        if self._depth + 1 < self._config.max_depth:
            base = OrchestrationExecutor(
                base,
                self._provider,
                self._config,
                tools=schemas,
                hooks=self._hooks,
                event_sink=self._event_sink,
                _depth=self._depth + 1,
                _lineage=child_id,
            )
            schemas = [*schemas, *orchestration_tool_descriptors(self._config)]
        loop = CoreLoop(
            self._provider,
            base,
            LoopConfig(max_rounds=self._config.child_max_rounds),
            hooks=self._hooks,
        )
        return loop, schemas or None

    # ------------------------------------------------------------------
    # ToolExecutor
    # ------------------------------------------------------------------

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        if call.name == self._config.spawn_tool:
            return await self._spawn(call)
        if call.name == self._config.send_tool:
            return await self._send(call)
        if call.name == self._config.wait_tool:
            return await self._wait(call)
        if call.name == self._config.close_tool:
            return await self._close(call)
        return await self._inner.execute(call, ctx)

    async def _spawn(self, call: ToolCall) -> ToolResult:
        task = str(call.arguments.get("task") or "").strip()
        if not task:
            return ToolResult(success=False, error="empty task")
        raw_filter = call.arguments.get("toolFilter")
        tool_filter = (
            frozenset(str(t) for t in raw_filter)
            if isinstance(raw_filter, list)
            else None
        )
        try:
            handle = self._pool.spawn(task, tool_filter)
        except OrchestrationBudgetExceeded as exc:
            return ToolResult(
                success=False,
                error=f"orchestration_budget_exceeded: {exc}",
                needsFollowup=True,
            )
        return ToolResult(
            success=True,
            message=json.dumps(
                {
                    "childId": handle.child_id,
                    "depth": self._depth + 1,
                    "status": "running",
                }
            ),
            data={"childId": handle.child_id},
        )

    async def _send(self, call: ToolCall) -> ToolResult:
        child_id = str(call.arguments.get("childId") or "")
        message = str(call.arguments.get("message") or "").strip()
        if not message:
            return ToolResult(success=False, error="empty message")
        if not self._pool.send(child_id, message):
            return ToolResult(
                success=False,
                error=f"unknown_child: {child_id!r} is not a running child",
                needsFollowup=True,
            )
        return ToolResult(
            success=True, message=json.dumps({"childId": child_id, "sent": True})
        )

    async def _wait(self, call: ToolCall) -> ToolResult:
        child_id = str(call.arguments.get("childId") or "")
        raw_timeout = call.arguments.get("timeoutMs")
        timeout_s = (
            float(raw_timeout) / 1000.0
            if isinstance(raw_timeout, (int, float))
            else None
        )
        outcome = await self._pool.wait(child_id, timeout_s)
        if outcome is None:
            return ToolResult(
                success=False,
                error=f"unknown_child: {child_id!r}",
                needsFollowup=True,
            )
        return ToolResult(
            success=True,
            message=json.dumps(
                {
                    "childId": outcome.child_id,
                    "status": outcome.status,
                    "answer": outcome.answer,
                }
            ),
            data={"childId": outcome.child_id, "status": outcome.status},
        )

    async def _close(self, call: ToolCall) -> ToolResult:
        child_id = str(call.arguments.get("childId") or "")
        if not await self._pool.close(child_id):
            return ToolResult(
                success=False,
                error=f"unknown_child: {child_id!r}",
                needsFollowup=True,
            )
        return ToolResult(
            success=True, message=json.dumps({"childId": child_id, "closed": True})
        )

    async def shutdown(self) -> None:
        """Wind down every live child — hosts call this when the parent run ends."""
        await self._pool.shutdown()

    def concurrency_safe(self, call: ToolCall) -> bool:
        # spawn and wait mutate nothing themselves (the pool task does the
        # work), so they may batch; send/close are serialized with siblings.
        if call.name in {self._config.spawn_tool, self._config.wait_tool}:
            return True
        if call.name in {self._config.send_tool, self._config.close_tool}:
            return False
        inner_safe = getattr(self._inner, "concurrency_safe", None)
        return bool(inner_safe and inner_safe(call))


def _schema_name(schema: dict[str, Any]) -> str:
    function = schema.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return str(schema.get("name") or "")
