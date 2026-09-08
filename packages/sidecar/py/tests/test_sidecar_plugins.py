"""The plugin.* management RPCs: list / enable / disable / reload."""

from __future__ import annotations

import json

import pytest

from steerable_agent_runtime import PluginRegistry, PluginSpec
from steerable_sidecar import Sidecar


@pytest.fixture
def sidecar() -> Sidecar:
    return Sidecar()


async def _call(sidecar: Sidecar, method: str, params: dict | None = None):
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return await sidecar.server.handle_frame(raw)


def _wire_registry(sidecar: Sidecar) -> PluginRegistry:
    """Load one inline plugin and wire the registry onto the sidecar."""
    registry = PluginRegistry(sidecar.tools)

    def register(router) -> None:
        async def plugin_echo() -> dict:
            return {"echoed": True}

        router.register(plugin_echo, name="plugin_echo")

    registry.load(
        PluginSpec(name="echo-plugin", origin="test", load=lambda: register)
    )
    sidecar.plugin_registry = registry
    return registry


async def test_plugin_rpcs_fail_loud_when_unwired(sidecar: Sidecar) -> None:
    response = await _call(sidecar, "plugin.list")
    assert "error" in response
    assert "not wired" in response["error"]["message"]


async def test_plugin_list_returns_loaded_records(sidecar: Sidecar) -> None:
    _wire_registry(sidecar)
    response = await _call(sidecar, "plugin.list")
    plugins = response["result"]["plugins"]
    assert plugins == [
        {
            "name": "echo-plugin",
            "origin": "test",
            "tools": ["plugin_echo"],
            "enabled": True,
            "reloadable": False,
        }
    ]


async def test_plugin_disable_removes_tools_and_enable_restores(
    sidecar: Sidecar,
) -> None:
    _wire_registry(sidecar)
    assert sidecar.tools.get("plugin_echo") is not None

    disabled = await _call(sidecar, "plugin.disable", {"name": "echo-plugin"})
    assert disabled["result"]["plugin"]["enabled"] is False
    assert disabled["result"]["plugin"]["tools"] == []
    assert sidecar.tools.get("plugin_echo") is None

    enabled = await _call(sidecar, "plugin.enable", {"name": "echo-plugin"})
    assert enabled["result"]["plugin"]["enabled"] is True
    assert sidecar.tools.get("plugin_echo") is not None


async def test_plugin_reload_refuses_a_non_reloadable_plugin(
    sidecar: Sidecar,
) -> None:
    _wire_registry(sidecar)
    response = await _call(sidecar, "plugin.reload", {"name": "echo-plugin"})
    assert "error" in response
    assert "echo-plugin" in response["error"]["message"]


async def test_plugin_lifecycle_on_unknown_name_fails_loud(
    sidecar: Sidecar,
) -> None:
    _wire_registry(sidecar)
    for method in ("plugin.enable", "plugin.disable", "plugin.reload"):
        response = await _call(sidecar, method, {"name": "ghost"})
        assert "error" in response, method
        assert "ghost" in response["error"]["message"], method
