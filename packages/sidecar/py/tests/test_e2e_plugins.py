"""E2E: the plugin.* lifecycle RPCs across a real spawned sidecar process.

The unit tests in ``test_sidecar_plugins.py`` drive the handlers in-process;
here the whole chain runs over stdio JSON-RPC: boot loads a directory plugin
(``STEERABLE_PLUGIN_DIR``), the registry backs the four management RPCs, and
state transitions are visible in both ``plugin.list`` and the model-facing
``tool.list``.
"""

from __future__ import annotations

from e2e_harness import model_tool_names

_PLUGIN_SOURCE = '''
async def plugin_echo() -> dict:
    return {"echoed": True}


def register(router) -> None:
    router.register(plugin_echo, name="plugin_echo")
'''


def _write_plugin(tmp_path, source: str = _PLUGIN_SOURCE):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "echo.py").write_text(source)
    return plugin_dir


async def test_plugin_lifecycle_over_real_process(sidecar_factory, tmp_path) -> None:
    plugin_dir = _write_plugin(tmp_path)
    client = await sidecar_factory(
        env_overrides={"STEERABLE_PLUGIN_DIR": str(plugin_dir)}
    )

    # list: the directory plugin loaded at boot, its tool on the router.
    result = await client.request("plugin.list")
    assert result["plugins"] == [
        {
            "name": "echo",
            "origin": f"directory:{plugin_dir / 'echo.py'}",
            "tools": ["plugin_echo"],
            "enabled": True,
            "reloadable": True,
        }
    ]
    tools = await client.request("tool.list")
    assert "plugin_echo" in model_tool_names(tools)

    # disable: the record flips and the tool leaves the dispatch surface.
    disabled = await client.request("plugin.disable", {"name": "echo"})
    assert disabled["plugin"]["enabled"] is False
    tools = await client.request("tool.list")
    assert "plugin_echo" not in model_tool_names(tools)

    # enable: back on, tool dispatchable again.
    enabled = await client.request("plugin.enable", {"name": "echo"})
    assert enabled["plugin"]["enabled"] is True
    tools = await client.request("tool.list")
    assert "plugin_echo" in model_tool_names(tools)

    # reload: directory plugins are reloadable; the record survives.
    reloaded = await client.request("plugin.reload", {"name": "echo"})
    assert reloaded["plugin"]["name"] == "echo"
    assert reloaded["plugin"]["enabled"] is True


async def test_plugin_rpcs_reject_unknown_names_over_real_process(
    sidecar_factory, tmp_path
) -> None:
    client = await sidecar_factory(
        env_overrides={"STEERABLE_PLUGIN_DIR": str(_write_plugin(tmp_path))}
    )
    for method in ("plugin.enable", "plugin.disable", "plugin.reload"):
        response = await client.request_raw(method, {"name": "ghost"})
        assert "error" in response, method
        assert "ghost" in response["error"]["message"]


async def test_plugin_list_empty_without_sources_over_real_process(
    sidecar_factory,
) -> None:
    # No STEERABLE_PLUGIN_DIR and no installed entry-point packages in the
    # scrubbed child env: the registry is wired but empty — the RPC answers
    # rather than erroring, so hosts can render the empty state.
    client = await sidecar_factory()
    result = await client.request("plugin.list")
    assert result == {"plugins": []}
