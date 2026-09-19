"""Pure-Python Steerable plugin authoring and plugin-host RPC SDK."""

from .rpc import PLUGIN_HOST_PROTOCOL_VERSION, PluginHostRpcClient, RpcRouterProxy
from .schema import derive_schema
from .tools import (
    STEERABLE_TOOLS_ENTRY_POINT_GROUP,
    LocalPluginRouter,
    RegisteredTool,
    ToolDescriptor,
    ToolExposure,
    ToolRegistrationError,
    tool,
)

__all__ = [
    "PLUGIN_HOST_PROTOCOL_VERSION",
    "STEERABLE_TOOLS_ENTRY_POINT_GROUP",
    "LocalPluginRouter",
    "PluginHostRpcClient",
    "RegisteredTool",
    "RpcRouterProxy",
    "ToolDescriptor",
    "ToolExposure",
    "ToolRegistrationError",
    "derive_schema",
    "tool",
]
