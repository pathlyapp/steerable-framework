"""In-process tool registry.

Tools are registered with `@tool` (or `ToolRouter.register`). Each tool gets:

* ``name``  — unique identifier (used in ``ToolCall.name``)
* ``handler`` — callable that takes a dict of arguments and returns a value
* ``mode`` — `ToolMode` (auto-classified from name unless overridden)
* ``schema`` — optional JSON Schema for ``arguments`` (surfaced to the LLM)
* ``description`` — natural-language description for the LLM

The router accepts a `ToolCall` and returns a `ToolResult`. Errors raised by
handlers are wrapped into `ToolResult.error` so the loop can decide whether to
self-heal.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from steerable_agent_harness.policy import ToolMode, decide_tool_mode
from steerable_agent_protocol.generated import ToolCall, ToolResult

from .errors import PolicyDeniedError, ToolDispatchError

logger = logging.getLogger(__name__)


ToolHandler = Callable[..., Any] | Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class RegisteredTool:
    name: str
    handler: ToolHandler
    mode: ToolMode
    description: str = ""
    schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    require_consent: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_openai_function(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


class ToolRouter:
    """Async-safe in-process tool dispatch."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        handler: ToolHandler,
        *,
        name: str | None = None,
        mode: ToolMode | None = None,
        description: str | None = None,
        schema: dict[str, Any] | None = None,
        require_consent: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RegisteredTool:
        resolved_name = name or getattr(handler, "__name__", None)
        if not resolved_name:
            raise ToolDispatchError("Tool handler must have a name")
        if resolved_name in self._tools:
            raise ToolDispatchError(f"Tool already registered: {resolved_name}")
        resolved_mode: ToolMode = mode or decide_tool_mode(resolved_name)
        resolved_consent = (
            require_consent if require_consent is not None else resolved_mode == "destructive"
        )
        tool_meta = RegisteredTool(
            name=resolved_name,
            handler=handler,
            mode=resolved_mode,
            description=description or (inspect.getdoc(handler) or "").strip(),
            schema=schema or {"type": "object", "properties": {}},
            require_consent=resolved_consent,
            metadata=dict(metadata or {}),
        )
        self._tools[resolved_name] = tool_meta
        return tool_meta

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def list_tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def describe(self) -> list[dict[str, Any]]:
        return [t.to_openai_function() for t in self._tools.values()]

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        call: ToolCall,
        *,
        consent_granted: bool = False,
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {call.name}",
                terminal=False,
                needsFollowup=False,
            )
        if tool.require_consent and not consent_granted:
            raise PolicyDeniedError(
                f"Tool '{tool.name}' requires explicit consent",
                data={"tool": tool.name, "mode": tool.mode},
            )
        started = time.monotonic()
        try:
            result = await self._invoke(tool, call.arguments or {}, context or {})
        except Exception as exc:  # noqa: BLE001 — wrap for the loop
            logger.exception("Tool %s failed", tool.name)
            return ToolResult(
                success=False,
                error=str(exc),
                terminal=False,
                needsFollowup=True,
                data={"durationMs": int((time.monotonic() - started) * 1000)},
            )
        return _coerce_to_tool_result(result, duration_ms=int((time.monotonic() - started) * 1000))

    async def _invoke(
        self,
        tool: RegisteredTool,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> Any:
        signature = inspect.signature(tool.handler)
        kwargs: dict[str, Any] = {}
        for parameter in signature.parameters.values():
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            name = parameter.name
            if name in arguments:
                kwargs[name] = arguments[name]
            elif name == "context":
                kwargs[name] = context
        result = tool.handler(**kwargs)
        if inspect.isawaitable(result):
            return await result
        if asyncio.iscoroutine(result):  # pragma: no cover - defensive
            return await result
        return result


# ---------------------------------------------------------------------------
# Decorator helper
# ---------------------------------------------------------------------------


def tool(
    *,
    name: str | None = None,
    mode: ToolMode | None = None,
    description: str | None = None,
    schema: dict[str, Any] | None = None,
    require_consent: bool | None = None,
    router: ToolRouter | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator form of `ToolRouter.register()`.

    Usage::

        router = ToolRouter()

        @tool(router=router, description="List events for the user")
        async def list_events(limit: int = 20) -> list[dict]:
            ...

    If `router` is omitted the decorator stores registration metadata on the
    function as ``__steerable_tool_meta__`` so it can be batch-registered
    later via ``router.register_decorated(handler)``.
    """

    def _decorator(handler: ToolHandler) -> ToolHandler:
        meta = {
            "name": name,
            "mode": mode,
            "description": description,
            "schema": schema,
            "require_consent": require_consent,
        }
        setattr(handler, "__steerable_tool_meta__", meta)
        if router is not None:
            router.register(
                handler,
                name=name,
                mode=mode,
                description=description,
                schema=schema,
                require_consent=require_consent,
            )
        return handler

    return _decorator


# ---------------------------------------------------------------------------
# Result coercion
# ---------------------------------------------------------------------------


def _coerce_to_tool_result(value: Any, *, duration_ms: int) -> ToolResult:
    """Convert handler return value into a `ToolResult`."""

    if isinstance(value, ToolResult):
        if value.data is None:
            value.data = {}
        value.data.setdefault("durationMs", duration_ms)
        return value
    if isinstance(value, dict) and "success" in value:
        result = ToolResult(**value)
        if result.data is None:
            result.data = {}
        result.data.setdefault("durationMs", duration_ms)
        return result
    return ToolResult(
        success=True,
        data={"value": value, "durationMs": duration_ms},
    )
