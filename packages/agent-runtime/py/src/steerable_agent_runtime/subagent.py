"""Sub-agent delegation seam — answer one tool call with a child CoreLoop.

codex and dsh both ship a delegation primitive (codex's agent tool, dsh's
subagent capability); this is the framework's equivalent: a ``ToolExecutor``
decorator that intercepts one well-known tool name and answers it by
running a bounded child ``CoreLoop`` on the same provider with a fresh
transcript.

Design:
- Depth-1 by construction: the child dispatches to the *inner* executor,
  so a child cannot spawn further agents.
- The child runs storage-free; in the parent trace the whole delegation is
  a single tool span — child internals stay out of the parent's event
  stream (a product that wants child traces wraps the child run in its own
  TraceRecorder via ``hooks``/composition, not this seam).
- The child's answer is its accumulated assistant text at completion.
- Opt-in: the host advertises ``subagent_tool_descriptor`` in the tools
  list and wraps its executor; products that don't want delegation simply
  do neither.
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

from dataclasses import dataclass
from typing import Any

from steerable_agent_protocol.generated import ToolCall, ToolResult

from .llm import LLMMessage, LLMProvider
from .loop import CoreLoop, LoopConfig, LoopContext, LoopHooks, ToolExecutor


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
    is an independent loop over the shared inner executor, which is exactly
    what the loop's parallel tool execution already does.
    """

    tool_name: str = "delegate_subagent"
    max_rounds: int = 8
    allow_tools: bool = True
    tool_filter: frozenset[str] | None = None
    model: str | None = None
    concurrent: bool = False
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


class SubagentExecutor:
    """ToolExecutor decorator: ``config.tool_name`` calls run a child loop.

    ``provider_factory`` maps a model name onto a provider for children
    whose profile sets ``model`` (CC's per-subagent model parity); without
    one, a ``model``-bearing profile fails closed at dispatch. ``registry``
    carries the named profiles the tool schema advertises.
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
    ) -> None:
        self._inner = inner
        self._provider = provider
        self._config = config or SubagentConfig()
        self._hooks = hooks
        self._registry = registry
        self._provider_factory = provider_factory

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
        provider = self._provider
        if config.model is not None:
            if self._provider_factory is None:
                return ToolResult(
                    success=False,
                    error=(
                        f"sub-agent profile requests model {config.model!r} "
                        "but this host has no provider factory"
                    ),
                )
            provider = self._provider_factory(config.model)
        child = CoreLoop(
            provider,
            self._child_executor(config),
            LoopConfig(max_rounds=config.max_rounds),
            hooks=self._hooks,
        )
        answer_parts: list[str] = []
        status = "completed"
        async for event in child.run([LLMMessage.text_of("user", task)]):
            if event.kind == "content_delta":
                answer_parts.append(str(event.data.get("delta") or ""))
            elif event.kind == "completion":
                status = str(event.data.get("status") or "completed")
        answer = "".join(answer_parts).strip()
        if status != "completed":
            return ToolResult(
                success=False,
                error=f"sub-agent ended with status: {status}",
                message=answer or None,
            )
        return ToolResult(
            success=True,
            message=answer or "(sub-agent returned no text)",
        )

    def _child_executor(self, config: SubagentConfig) -> ToolExecutor:
        if not config.allow_tools:
            return _NoTools()
        if config.tool_filter is not None:
            return FilteredToolsExecutor(self._inner, config.tool_filter)
        return self._inner

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
