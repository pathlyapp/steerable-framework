"""Multi-agent orchestration — a parent CoreLoop driving parallel children.

Extends the depth-1 delegation seam (``subagent.py``) into the framework's
orchestration layer (P3.1): a pool of concurrent child ``CoreLoop`` instances
with coordination primitives the parent model drives through six tools —
``agent_spawn`` / ``agent_send`` / ``agent_wait`` / ``agent_close`` /
``agent_list`` / ``agent_interrupt``. The pool itself lives in ``pool.py``
(re-exported here) and also backs the single-tool delegation seam.

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
- **Coordination reuses loop primitives.** ``agent_send`` to a running child
  is ``loop.steer``; to a finished child it is a follow-up turn seeded from
  the clean-finish record snapshot (multi-turn children). ``agent_interrupt``
  is the cooperative ``loop.cancel()`` that keeps the child addressable;
  ``agent_close`` adds a hard-cancel backstop and a terminal closed flag —
  a closed child winds down with a consistent record, same as a
  user-cancelled turn, and rejects further sends.
- **Children stay out of the parent record.** A child's transcript is its
  own loop's; the parent sees spawn/wait/close results. Hosts that want
  child traces subscribe via ``event_sink`` (lifecycle events with lineage)
  or wrap children in their own hooks.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from steerable_agent_protocol.generated import ToolCall, ToolResult

from .llm import LLMProvider
from .loop import CoreLoop, LoopConfig, LoopContext, LoopHooks, ToolExecutor
from .pool import (
    AgentPool,
    ChildOutcome,
    OrchestrationBudgetExceeded,
    OrchestrationConfig,
)
from .subagent import FilteredToolsExecutor, _schema_name

__all__ = [
    "AgentPool",
    "ChildOutcome",
    "OrchestrationBudgetExceeded",
    "OrchestrationConfig",
    "OrchestrationExecutor",
    "orchestration_tool_descriptors",
]


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
            "Send a message to a sub-agent. A running child receives it at "
            "its next round boundary (steer); a finished or interrupted "
            "child is resumed with the message as a follow-up turn, keeping "
            "its prior context. Closed children reject sends.",
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
            "Close a sub-agent terminally: cooperative cancel with a "
            "hard-cancel backstop, and the child rejects further sends. "
            "Use agent_interrupt instead to pause a child you still need.",
            {"childId": {"type": "string"}},
            ["childId"],
        ),
        fn(
            config.list_tool,
            "List this pool's sub-agents with their status (running / "
            "completed / error / cancelled / interrupted / closed), task, "
            "and a preview of finished answers.",
            {},
            [],
        ),
        fn(
            config.interrupt_tool,
            "Interrupt a running sub-agent's current turn (cooperative "
            "cancel) while keeping it addressable: its context is preserved "
            "and a later agent_send resumes it. Unlike agent_close this is "
            "not terminal.",
            {"childId": {"type": "string"}},
            ["childId"],
        ),
    ]


class OrchestrationExecutor:
    """ToolExecutor decorator: the six orchestration tools drive an AgentPool.

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

    @property
    def pool(self) -> AgentPool:
        """This executor's pool — exposed so the delegation seam can share
        it (one ``max_parallel`` budget and one lineage space across both
        multi-agent surfaces; see the sidecar wiring)."""
        return self._pool

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
        if call.name == self._config.list_tool:
            return self._list(call)
        if call.name == self._config.interrupt_tool:
            return await self._interrupt(call)
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
        outcome = self._pool.send(child_id, message)
        if outcome.startswith("error:"):
            return ToolResult(
                success=False,
                error=outcome[len("error:"):],
                needsFollowup=True,
            )
        return ToolResult(
            success=True,
            message=json.dumps({"childId": child_id, "delivery": outcome}),
            data={"childId": child_id, "delivery": outcome},
        )

    def _list(self, call: ToolCall) -> ToolResult:
        entries = self._pool.list()
        return ToolResult(
            success=True,
            message=json.dumps({"children": entries}),
            data={"count": len(entries)},
        )

    async def _interrupt(self, call: ToolCall) -> ToolResult:
        child_id = str(call.arguments.get("childId") or "")
        if not self._pool.interrupt(child_id):
            return ToolResult(
                success=False,
                error=(
                    f"unknown_or_not_running: {child_id!r} — only a running, "
                    "unclosed child can be interrupted"
                ),
                needsFollowup=True,
            )
        return ToolResult(
            success=True,
            message=json.dumps({"childId": child_id, "interrupted": True}),
            data={"childId": child_id},
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

    def dedup_exempt(self, call: ToolCall) -> bool:
        # Every orchestration tool is stateful: identical args yield different
        # results as pool state evolves (a second spawn creates a NEW child,
        # a second wait observes the resumed run). The loop's same-turn
        # duplicate_call guard would break legitimate sequences like
        # wait → send → wait, so the family opts out; inner tools keep the
        # guard unless the inner executor exempts them.
        if call.name in self._config.tool_names:
            return True
        inner_exempt = getattr(self._inner, "dedup_exempt", None)
        return bool(inner_exempt and inner_exempt(call))

    def concurrency_safe(self, call: ToolCall) -> bool:
        # spawn/wait/list mutate nothing themselves (the pool task does the
        # work), so they may batch; send/close/interrupt are serialized with
        # siblings.
        if call.name in {
            self._config.spawn_tool,
            self._config.wait_tool,
            self._config.list_tool,
        }:
            return True
        if call.name in {
            self._config.send_tool,
            self._config.close_tool,
            self._config.interrupt_tool,
        }:
            return False
        inner_safe = getattr(self._inner, "concurrency_safe", None)
        return bool(inner_safe and inner_safe(call))
