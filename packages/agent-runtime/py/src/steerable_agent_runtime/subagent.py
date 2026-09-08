"""Sub-agent delegation seam — answer one tool call with a child CoreLoop.

codex and dsh both ship a delegation primitive (codex's agent tool, dsh's
subagent capability); this is the framework's equivalent: a ``ToolExecutor``
decorator that intercepts one well-known tool name and answers it by
running a bounded child ``CoreLoop`` on the same provider with a fresh
transcript.

Design:
- Depth-1 by construction: the child dispatches to the *inner* executor,
  so a child cannot spawn further agents.
- **Delegate-on-pool.** The child runs inside an ``AgentPool``
  (``pool.py``) — the same engine the six-tool orchestration family
  drives. The tool contract stays synchronous (the call returns the
  child's final answer), but execution is a pooled child run: concurrent
  profiles batch in parallel under the pool's ``max_parallel`` budget, and
  lifecycle lands on the ``event_sink`` as ``child_spawned`` /
  ``child_completed`` / ``child_failed`` events (carrying the resolved
  ``profile`` name) so hosts can render delegation live. When the host
  also enables the orchestration family it attaches the orchestration
  executor's pool (``attach_pool``) — one budget, one lineage space, and
  delegate children show up in ``agent_list``.
- The child runs storage-free; in the parent trace the whole delegation is
  a single tool span — child internals stay out of the parent's event
  stream (a product that wants child traces wraps the child run in its own
  TraceRecorder via ``hooks``/composition, not this seam).
- The child's answer is its accumulated assistant text at completion.
- Opt-out: hosts advertise ``subagent_tool_descriptor`` in the tools list
  and wrap their executor; the sidecar does both by default and
  ``params.subagent: false`` turns delegation off.
- Named profiles (CC ``subagent_type`` parity): a ``SubagentRegistry``
  maps profile names onto per-profile tool domains, round bounds, models,
  and concurrency; the tool schema advertises the names as a
  ``subagent_type`` enum and unknown names fail closed. A profile's
  ``model`` is honored through the host's ``provider_factory`` — without
  one the dispatch fails closed rather than silently running the parent's
  model.
- Privilege boundary: ``SubagentConfig.tool_filter`` narrows the child's
  tool domain (dsh ``toolFilter`` → restrict counterpart); filtered-out
  calls fail closed with ``tool_not_delegated``. Approval narrowing is not
  done here — an ``ApprovalExecutor`` outside this decorator already gates
  child calls (it is wrapped innermost, see the sidecar wiring note on
  ``SubagentExecutor``), and the child cannot spawn further agents.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from steerable_agent_protocol.generated import ToolCall, ToolResult

from .llm import LLMProvider
from .loop import CoreLoop, LoopConfig, LoopContext, LoopHooks, ToolExecutor
from .pool import AgentPool, LoopFactory, OrchestrationBudgetExceeded, OrchestrationConfig

#: ``profile`` label on lifecycle events for a delegation that named no
#: ``subagent_type`` — mirrors the tool schema's "default general-purpose
#: profile" wording.
DEFAULT_PROFILE_LABEL = "general-purpose"


@dataclass(frozen=True, slots=True)
class SubagentConfig:
    """Tunables for the delegation tool exposed by ``SubagentExecutor``.

    ``tool_filter`` narrows the child's tool domain (dsh's
    ``toolFilter`` → ``tools.restrict()`` counterpart): a frozenset of tool
    names the child may call, everything else fails closed with
    ``tool_not_delegated``. ``None`` keeps the legacy whole-domain hand-off
    (and ``allow_tools=False`` still means no tools at all). A read-only
    research sub-agent is ``tool_filter=frozenset({...read tools...})`` —
    it cannot reach the parent's write/shell tools *by construction*, which
    is what breaks the private-data + untrusted-content + egress trifecta.

    ``model`` selects a different model for the child (CC's Agent-tool
    ``model`` parity): the executor must be built with a
    ``provider_factory`` or the request fails closed. ``concurrent`` lifts
    the never-batch rule so several delegations in one round run their
    child loops in parallel (CC's concurrent-subagents parity) — each child
    is a pooled child run, so a same-round batch executes concurrently up
    to the pool's budget and the overflow delegation fails closed with
    ``orchestration_budget_exceeded``. ``max_parallel`` sizes the
    executor's own pool; it is inert once a shared pool is attached (the
    shared pool's config governs).
    """

    tool_name: str = "delegate_subagent"
    max_rounds: int = 8
    allow_tools: bool = True
    tool_filter: frozenset[str] | None = None
    model: str | None = None
    concurrent: bool = False
    max_parallel: int = 4
    description: str = (
        "Delegate a self-contained subtask to a sub-agent with its own "
        "reasoning loop. Good for parallelizable or context-heavy subtasks; "
        "the sub-agent returns only its final answer."
    )


class SubagentRegistry:
    """Named sub-agent profiles (CC ``subagent_type`` / ``.claude/agents``
    parity): a name → ``SubagentConfig`` mapping the model picks from.

    The delegation tool's schema advertises the registered names as the
    ``subagent_type`` enum; a call naming one runs the child with that
    profile's tool domain, model, and round bound. An unknown name fails
    closed listing the registered ones — never a silent fall-back to the
    default profile, which would hide a typo'd delegation.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, SubagentConfig] = {}

    def register(self, name: str, config: SubagentConfig) -> None:
        if not name or not name.strip():
            raise ValueError("subagent profile name must be non-empty")
        self._profiles[name] = config

    def get(self, name: str) -> SubagentConfig | None:
        return self._profiles.get(name)

    def names(self) -> list[str]:
        return sorted(self._profiles)


def subagent_tool_descriptor(
    config: SubagentConfig | None = None,
    *,
    registry: SubagentRegistry | None = None,
) -> dict[str, Any]:
    """OpenAI tool schema to append to the parent loop's tools list.

    With a ``registry``, the schema gains a ``subagent_type`` enum of the
    registered profile names (CC ``subagent_type`` parity); without one the
    tool takes only ``task`` and runs the default profile.
    """
    config = config or SubagentConfig()
    properties: dict[str, Any] = {
        "task": {
            "type": "string",
            "description": (
                "Complete, self-contained instructions for the "
                "sub-agent — it sees none of this conversation."
            ),
        },
    }
    if registry is not None and registry.names():
        properties["subagent_type"] = {
            "type": "string",
            "enum": registry.names(),
            "description": (
                "Named sub-agent profile to run the task with; omit for "
                "the default general-purpose profile."
            ),
        }
    return {
        "type": "function",
        "function": {
            "name": config.tool_name,
            "description": config.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": ["task"],
                "additionalProperties": False,
            },
        },
    }


class _NoTools:
    """Child executor when ``allow_tools=False``: every call fails closed."""

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        return ToolResult(
            success=False,
            error="this sub-agent has no tools; answer from reasoning only",
        )


class FilteredToolsExecutor:
    """Child executor for ``tool_filter``: only the named tools delegate.

    Filtered-out calls fail closed with ``tool_not_delegated`` and the
    delegated set named, so the child can re-issue with a tool it actually
    has instead of concluding the tool is broken. Shared by the depth-1
    delegation seam and the orchestration pool.
    """

    def __init__(self, inner: ToolExecutor, allowed: frozenset[str]) -> None:
        self._inner = inner
        self._allowed = allowed

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        if call.name not in self._allowed:
            return ToolResult(
                success=False,
                error=(
                    f"tool_not_delegated: '{call.name}' is outside this "
                    f"sub-agent's tool domain; delegated tools: "
                    f"{', '.join(sorted(self._allowed)) or '(none)'}"
                ),
                needsFollowup=True,
                data={"toolNotDelegated": call.name},
            )
        return await self._inner.execute(call, ctx)

    def concurrency_safe(self, call: ToolCall) -> bool:
        inner_safe = getattr(self._inner, "concurrency_safe", None)
        return bool(inner_safe and inner_safe(call))


def _schema_name(schema: dict[str, Any]) -> str:
    function = schema.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    return str(schema.get("name") or "")


class SubagentExecutor:
    """ToolExecutor decorator: ``config.tool_name`` calls run a child loop.

    The child runs as a pooled child (``AgentPool.run``): the tool contract
    stays synchronous — the call returns the child's final answer — while
    execution, budgeting, and lifecycle events are the pool's. ``pool``
    injects a shared pool (the orchestration family's, when both surfaces
    are on); absent one the executor runs its own, sized by
    ``config.max_parallel``.

    ``provider_factory`` maps a model name onto a provider for children
    whose profile sets ``model`` (CC's per-subagent model parity); without
    one, a ``model``-bearing profile fails closed at dispatch. ``registry``
    carries the named profiles the tool schema advertises. ``tools`` is the
    parent loop's advertised schemas — children advertise the subset their
    profile delegates (minus the delegation tool itself); without it
    children run reasoning-only.
    """

    def __init__(
        self,
        inner: ToolExecutor,
        provider: LLMProvider,
        config: SubagentConfig | None = None,
        *,
        hooks: LoopHooks | None = None,
        registry: SubagentRegistry | None = None,
        provider_factory: Any = None,
        tools: list[dict[str, Any]] | None = None,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
        pool: AgentPool | None = None,
    ) -> None:
        self._inner = inner
        self._provider = provider
        self._config = config or SubagentConfig()
        self._hooks = hooks
        self._registry = registry
        self._provider_factory = provider_factory
        self._parent_tools = list(tools or [])
        self._pool = pool or AgentPool(
            config=OrchestrationConfig(max_parallel=self._config.max_parallel),
            depth=0,
            lineage="0",
            event_sink=event_sink,
        )

    def attach_pool(self, pool: AgentPool) -> None:
        """Adopt a shared pool — wire-time only, before the first delegation.

        The sidecar calls this with the orchestration executor's pool when
        the six-tool family is also enabled, so both multi-agent surfaces
        share one ``max_parallel`` budget and one lineage space (delegate
        children then appear in ``agent_list`` too).
        """
        self._pool = pool

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        if call.name != self._config.tool_name:
            return await self._inner.execute(call, ctx)
        task = str(call.arguments.get("task") or "").strip()
        if not task:
            return ToolResult(success=False, error="empty task")
        config = self._config
        type_name = str(call.arguments.get("subagent_type") or "").strip()
        if type_name:
            if self._registry is None:
                return ToolResult(
                    success=False,
                    error="subagent_type given but no profiles are registered",
                    needsFollowup=True,
                )
            profile = self._registry.get(type_name)
            if profile is None:
                return ToolResult(
                    success=False,
                    error=(
                        f"unknown subagent_type {type_name!r}; registered: "
                        f"{', '.join(self._registry.names()) or '(none)'}"
                    ),
                    needsFollowup=True,
                )
            config = profile
        if config.model is not None and self._provider_factory is None:
            return ToolResult(
                success=False,
                error=(
                    f"sub-agent profile requests model {config.model!r} "
                    "but this host has no provider factory"
                ),
            )
        try:
            outcome = await self._pool.run(
                task,
                config.tool_filter,
                loop_factory=self._loop_factory_for(config),
                event_extra={"profile": type_name or DEFAULT_PROFILE_LABEL},
            )
        except OrchestrationBudgetExceeded as exc:
            return ToolResult(
                success=False,
                error=f"orchestration_budget_exceeded: {exc}",
                needsFollowup=True,
            )
        if outcome.status != "completed":
            return ToolResult(
                success=False,
                error=f"sub-agent ended with status: {outcome.status}",
                message=outcome.answer or None,
            )
        return ToolResult(
            success=True,
            message=outcome.answer or "(sub-agent returned no text)",
        )

    def _loop_factory_for(self, config: SubagentConfig) -> LoopFactory:
        """Per-spawn child builder resolving the profile's provider, tool
        domain, and round bound (profiles differ per call, so the pool's
        constructor default cannot carry this)."""

        def factory(
            child_id: str, tool_filter: frozenset[str] | None
        ) -> tuple[CoreLoop, list[dict[str, Any]] | None]:
            provider = self._provider
            if config.model is not None:
                # execute() fails closed before the spawn when a
                # model-bearing profile has no provider factory.
                provider = self._provider_factory(config.model)
            schemas: list[dict[str, Any]] | None = None
            if config.allow_tools:
                schemas = [
                    schema
                    for schema in self._parent_tools
                    if _schema_name(schema) != self._config.tool_name
                    and (tool_filter is None or _schema_name(schema) in tool_filter)
                ] or None
            return (
                CoreLoop(
                    provider,
                    self._child_executor(config),
                    LoopConfig(max_rounds=config.max_rounds),
                    hooks=self._hooks,
                ),
                schemas,
            )

        return factory

    def _child_executor(self, config: SubagentConfig) -> ToolExecutor:
        if not config.allow_tools:
            return _NoTools()
        if config.tool_filter is not None:
            return FilteredToolsExecutor(self._inner, config.tool_filter)
        return self._inner

    async def shutdown(self) -> None:
        """Wind down children still running when the parent turn ends —
        hosts call this on stream teardown (a no-op twice, so a shared
        pool may also be shut down via the orchestration executor)."""
        await self._pool.shutdown()

    def concurrency_safe(self, call: ToolCall) -> bool:
        # Delegation spawns a full child loop — batched with siblings only
        # when the resolved profile opts into concurrency; other calls defer
        # to the inner executor's own judgement.
        if call.name == self._config.tool_name:
            config = self._config
            type_name = str(call.arguments.get("subagent_type") or "").strip()
            if type_name and self._registry is not None:
                config = self._registry.get(type_name) or config
            return config.concurrent
        inner_safe = getattr(self._inner, "concurrency_safe", None)
        return bool(inner_safe and inner_safe(call))
