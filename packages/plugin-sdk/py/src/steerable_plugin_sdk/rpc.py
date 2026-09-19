"""Plugin-host JSON-RPC client and sidecar router projection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from steerable_agent_protocol.generated import ToolResult

from .tools import ToolDescriptor

PLUGIN_HOST_PROTOCOL_VERSION = "0.1.0"


class RpcTransport(Protocol):
    """Request transport supplied by the plugin-host process owner."""

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        """Issue one JSON-RPC request and return its result."""


class RemoteRegistrationRouter(Protocol):
    """Runtime router surface needed to project remote plugin tools."""

    def register_remote(
        self,
        name: str,
        invoker: Callable[[str, dict[str, Any]], Awaitable[Any]],
        **kwargs: Any,
    ) -> Any:
        """Register one remotely invoked tool."""

    def unregister(self, name: str) -> None:
        """Remove one projected tool."""


class PluginHostRpcClient:
    """Typed client for the frozen plugin-host RPC methods."""

    def __init__(self, transport: RpcTransport) -> None:
        self._transport = transport

    async def ping(self) -> dict[str, Any]:
        """Negotiate the plugin-host protocol version."""
        return await self._transport.request(
            "plugin.host.ping",
            {"protocolVersion": PLUGIN_HOST_PROTOCOL_VERSION},
        )

    async def describe_tools(self) -> list[ToolDescriptor]:
        """Read every tool currently enabled in the plugin host."""
        result = await self._transport.request("plugin.tools.describe", {})
        return [
            ToolDescriptor.from_wire(value)
            for value in result.get("tools", [])
        ]

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Invoke one plugin tool."""
        result = await self._transport.request(
            "plugin.tool.invoke",
            {"name": name, "arguments": arguments, "context": context or {}},
        )
        return ToolResult(**result)

    async def list_plugins(self) -> dict[str, Any]:
        """Return plugin lifecycle records."""
        return await self._transport.request("plugin.list", {})

    async def enable(self, name: str) -> dict[str, Any]:
        """Enable one plugin."""
        return await self._lifecycle("plugin.enable", name)

    async def disable(self, name: str) -> dict[str, Any]:
        """Disable one plugin."""
        return await self._lifecycle("plugin.disable", name)

    async def reload(self, name: str) -> dict[str, Any]:
        """Reload one plugin."""
        return await self._lifecycle("plugin.reload", name)

    async def _lifecycle(self, method: str, name: str) -> dict[str, Any]:
        return await self._transport.request(method, {"name": name})


class RpcRouterProxy:
    """Project plugin-host descriptors onto a runtime remote-tool router."""

    def __init__(
        self,
        client: PluginHostRpcClient,
        router: RemoteRegistrationRouter,
    ) -> None:
        self._client = client
        self._router = router
        self._names: list[str] = []

    async def sync_tools(self) -> list[str]:
        """Replace projected registrations with the host's current catalog."""
        for name in self._names:
            self._router.unregister(name)
        self._names = []
        for descriptor in await self._client.describe_tools():
            self._router.register_remote(
                descriptor.name,
                self._client.invoke,
                mode=descriptor.mode,
                description=descriptor.description,
                schema=descriptor.schema,
                require_consent=descriptor.require_consent,
                concurrency_safe=descriptor.concurrency_safe,
                exposure=descriptor.exposure,
                metadata={
                    "plugin": descriptor.plugin,
                    "pluginHost": True,
                },
            )
            self._names.append(descriptor.name)
        return list(self._names)
