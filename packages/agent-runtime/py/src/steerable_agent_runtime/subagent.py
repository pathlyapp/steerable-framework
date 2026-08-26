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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from steerable_agent_protocol.generated import ToolCall, ToolResult

from .llm import LLMMessage, LLMProvider
from .loop import CoreLoop, LoopConfig, LoopContext, LoopHooks, ToolExecutor


@dataclass(frozen=True, slots=True)
class SubagentConfig:
    """Tunables for the delegation tool exposed by ``SubagentExecutor``."""

    tool_name: str = "delegate_subagent"
    max_rounds: int = 8
    allow_tools: bool = True
    description: str = (
        "Delegate a self-contained subtask to a sub-agent with its own "
        "reasoning loop. Good for parallelizable or context-heavy subtasks; "
        "the sub-agent returns only its final answer."
    )


def subagent_tool_descriptor(config: SubagentConfig | None = None) -> dict[str, Any]:
    """OpenAI tool schema to append to the parent loop's tools list."""
    config = config or SubagentConfig()
    return {
        "type": "function",
        "function": {
            "name": config.tool_name,
            "description": config.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "Complete, self-contained instructions for the "
                            "sub-agent — it sees none of this conversation."
                        ),
                    },
                },
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


class SubagentExecutor:
    """ToolExecutor decorator: ``config.tool_name`` calls run a child loop."""

    def __init__(
        self,
        inner: ToolExecutor,
        provider: LLMProvider,
        config: SubagentConfig | None = None,
        *,
        hooks: LoopHooks | None = None,
    ) -> None:
        self._inner = inner
        self._provider = provider
        self._config = config or SubagentConfig()
        self._hooks = hooks

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        if call.name != self._config.tool_name:
            return await self._inner.execute(call, ctx)
        task = str(call.arguments.get("task") or "").strip()
        if not task:
            return ToolResult(success=False, error="empty task")
        child = CoreLoop(
            self._provider,
            self._inner if self._config.allow_tools else _NoTools(),
            LoopConfig(max_rounds=self._config.max_rounds),
            hooks=self._hooks,
        )
        answer_parts: list[str] = []
        status = "completed"
        async for event in child.run([LLMMessage(role="user", content=task)]):
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

    def concurrency_safe(self, call: ToolCall) -> bool:
        # Delegation spawns a full child loop — never batch it with siblings;
        # other calls defer to the inner executor's own judgement.
        if call.name == self._config.tool_name:
            return False
        inner_safe = getattr(self._inner, "concurrency_safe", None)
        return bool(inner_safe and inner_safe(call))
