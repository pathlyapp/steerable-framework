from __future__ import annotations

import subprocess
import sys
from typing import Literal

import pytest
from steerable_plugin_sdk import (
    LocalPluginRouter,
    PluginHostRpcClient,
    RpcRouterProxy,
    ToolDescriptor,
    derive_schema,
    tool,
)


def test_schema_derives_without_importing_runtime() -> None:
    def search(query: str, limit: int = 8, mode: Literal["fast", "deep"] = "fast"):
        pass

    assert derive_schema(search) == {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "mode": {"enum": ["fast", "deep"], "type": "string"},
        },
        "required": ["query"],
    }
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import steerable_plugin_sdk; "
                "assert 'steerable_agent_runtime' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr


@pytest.mark.asyncio
async def test_author_decorator_and_local_dispatch() -> None:
    router = LocalPluginRouter()

    @tool(router=router, description="Greet by name")
    async def greet(name: str, context: dict) -> str:
        return f"{context['prefix']} {name}"

    descriptors = router.descriptors()
    assert [descriptor.name for descriptor in descriptors] == ["greet"]
    assert descriptors[0].schema["required"] == ["name"]
    result = await router.dispatch("greet", {"name": "Ada"}, {"prefix": "hello"})
    assert result.success is True
    assert result.data == {"value": "hello Ada"}


class _Transport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict):
        self.requests.append((method, params))
        if method == "plugin.tools.describe":
            return {
                "tools": [
                    ToolDescriptor(
                        name="remote_greet",
                        description="greet",
                        schema={
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                        mode="read",
                        exposure="direct",
                        require_consent=False,
                        concurrency_safe=True,
                        plugin="example",
                    ).to_wire()
                ]
            }
        if method == "plugin.tool.invoke":
            return {
                "success": True,
                "data": {"value": f"hello {params['arguments']['name']}"},
            }
        raise AssertionError(method)


class _RuntimeRouter:
    def __init__(self) -> None:
        self.tools: dict[str, tuple] = {}

    def register_remote(self, name, invoker, **kwargs):
        self.tools[name] = (invoker, kwargs)

    def unregister(self, name):
        self.tools.pop(name, None)


@pytest.mark.asyncio
async def test_rpc_proxy_projects_and_invokes_remote_tools() -> None:
    transport = _Transport()
    client = PluginHostRpcClient(transport)
    runtime = _RuntimeRouter()
    proxy = RpcRouterProxy(client, runtime)

    assert await proxy.sync_tools() == ["remote_greet"]
    invoker, metadata = runtime.tools["remote_greet"]
    assert metadata["metadata"] == {
        "plugin": "example",
        "pluginHost": True,
    }
    result = await invoker("remote_greet", {"name": "Ada"})
    assert result.success is True
    assert result.data == {"value": "hello Ada"}
