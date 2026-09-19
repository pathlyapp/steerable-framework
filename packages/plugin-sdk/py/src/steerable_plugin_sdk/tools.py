"""Plugin tool declaration and local plugin-host dispatch."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from steerable_agent_harness.policy import ToolMode, decide_tool_mode
from steerable_agent_protocol.generated import ToolResult

from .schema import derive_schema

ToolHandler = Callable[..., Any] | Callable[..., Awaitable[Any]]
ToolExposure = Literal["direct", "deferred", "hidden"]
STEERABLE_TOOLS_ENTRY_POINT_GROUP = "steerable.tools"


class ToolRegistrationError(RuntimeError):
    """A plugin attempted an invalid or duplicate tool registration."""


class RegistrationRouter(Protocol):
    """Router methods consumed by the SDK's decorator."""

    def register(self, handler: ToolHandler, **kwargs: Any) -> Any:
        """Register one handler and return the host's registration record."""


@dataclass(slots=True)
class ToolDescriptor:
    """Wire-safe metadata advertised by a plugin host."""

    name: str
    description: str
    schema: dict[str, Any]
    mode: ToolMode
    exposure: ToolExposure
    require_consent: bool
    concurrency_safe: bool
    plugin: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Return the camelCase plugin-host representation."""
        return {
            "name": self.name,
            "description": self.description,
            "schema": self.schema,
            "mode": self.mode,
            "exposure": self.exposure,
            "requireConsent": self.require_consent,
            "concurrencySafe": self.concurrency_safe,
            "plugin": self.plugin,
        }

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> ToolDescriptor:
        """Decode one plugin-host descriptor."""
        return cls(
            name=str(value["name"]),
            description=str(value.get("description") or ""),
            schema=dict(value.get("schema") or {}),
            mode=value.get("mode") or decide_tool_mode(str(value["name"])),
            exposure=value.get("exposure") or "direct",
            require_consent=bool(value.get("requireConsent", False)),
            concurrency_safe=bool(value.get("concurrencySafe", False)),
            plugin=str(value["plugin"]) if value.get("plugin") else None,
        )


@dataclass(slots=True)
class RegisteredTool:
    """One plugin-host registration and its local handler."""

    descriptor: ToolDescriptor
    handler: ToolHandler = field(repr=False)


class LocalPluginRouter:
    """Pure-Python registry used inside the future plugin-host process."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        handler: ToolHandler,
        *,
        name: str | None = None,
        mode: ToolMode | None = None,
        description: str | None = None,
        schema: dict[str, Any] | None = None,
        require_consent: bool | None = None,
        concurrency_safe: bool | None = None,
        exposure: ToolExposure = "direct",
        plugin: str | None = None,
        **_ignored: Any,
    ) -> RegisteredTool:
        """Register one plugin handler."""
        resolved_name = name or getattr(handler, "__name__", None)
        if not resolved_name:
            raise ToolRegistrationError("tool handler must have a name")
        if resolved_name in self._tools:
            raise ToolRegistrationError(f"tool already registered: {resolved_name}")
        resolved_mode = mode or decide_tool_mode(resolved_name)
        descriptor = ToolDescriptor(
            name=resolved_name,
            description=description or (inspect.getdoc(handler) or "").strip(),
            schema=schema if schema is not None else derive_schema(handler),
            mode=resolved_mode,
            exposure=exposure,
            require_consent=(
                require_consent
                if require_consent is not None
                else resolved_mode == "destructive"
            ),
            concurrency_safe=(
                concurrency_safe
                if concurrency_safe is not None
                else resolved_mode == "read"
            ),
            plugin=plugin,
        )
        registered = RegisteredTool(descriptor=descriptor, handler=handler)
        self._tools[resolved_name] = registered
        return registered

    def unregister(self, name: str) -> None:
        """Remove a handler if present."""
        self._tools.pop(name, None)

    def descriptors(self) -> list[ToolDescriptor]:
        """Return registrations in insertion order."""
        return [registered.descriptor for registered in self._tools.values()]

    async def dispatch(
        self,
        name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Invoke one local plugin handler and normalize its result."""
        registered = self._tools.get(name)
        if registered is None:
            return ToolResult(
                success=False,
                error=f"unknown plugin tool: {name}",
                terminal=False,
                needsFollowup=True,
            )
        handler = registered.handler
        signature = inspect.signature(handler)
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        kwargs = {
            parameter.name: (
                (context or {})
                if parameter.name == "context"
                else arguments[parameter.name]
            )
            for parameter in signature.parameters.values()
            if parameter.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            and (parameter.name == "context" or parameter.name in arguments)
        }
        if accepts_kwargs:
            for key, value in arguments.items():
                kwargs.setdefault(key, value)
        try:
            result = handler(**kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                terminal=False,
                needsFollowup=True,
            )
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, dict) and "success" in result:
            return ToolResult(**result)
        return ToolResult(success=True, data={"value": result})


def tool(
    *,
    name: str | None = None,
    mode: ToolMode | None = None,
    description: str | None = None,
    schema: dict[str, Any] | None = None,
    require_consent: bool | None = None,
    concurrency_safe: bool | None = None,
    exposure: ToolExposure = "direct",
    router: RegistrationRouter | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorate and optionally register a plugin handler."""

    def decorate(handler: ToolHandler) -> ToolHandler:
        metadata = {
            "name": name,
            "mode": mode,
            "description": description,
            "schema": schema,
            "require_consent": require_consent,
            "concurrency_safe": concurrency_safe,
            "exposure": exposure,
        }
        handler.__steerable_tool_meta__ = metadata
        if router is not None:
            router.register(handler, **metadata)
        return handler

    return decorate
