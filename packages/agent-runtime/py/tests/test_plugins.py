"""Plugin runtime: entry-point/directory sources, lifecycle, hot reload."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from steerable_agent_runtime import (
    DirectorySource,
    EntryPointSource,
    PluginLoadError,
    PluginRegistry,
    PluginSpec,
    PluginStateError,
    ToolRouter,
    load_tool_entry_points,
    tool,
)


class _FakeEntryPoint:
    """Stands in for importlib.metadata.EntryPoint without installing a
    package: ``load()`` returns the object the entry point points at."""

    def __init__(self, name: str, value: str, target: Any):
        self.name = name
        self.value = value
        self._target = target

    def load(self) -> Any:
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, eps: list[_FakeEntryPoint]) -> None:
    import steerable_agent_runtime.plugins as plugins

    monkeypatch.setattr(
        plugins.metadata, "entry_points", lambda *, group: list(eps)
    )


def _greeter_register(router: ToolRouter) -> None:
    @tool(router=router, description="Greet by name")
    async def greet(name: str) -> str:
        return f"hello {name}"


# ---------------------------------------------------------------------------
# Back-compat wrapper: load_tool_entry_points
# ---------------------------------------------------------------------------


def test_discovers_and_registers_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """A package's register(router) callable runs; its @tool functions land
    on the host's router and are model-visible."""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("greeter", "pkg:register", _greeter_register)])

    router = ToolRouter()
    loaded = load_tool_entry_points(router)

    assert loaded == ["greeter"]
    assert router.get("greet") is not None
    names = [d["function"]["name"] for d in router.describe_model()]
    assert "greet" in names


def test_import_failure_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry point that does not import names the offender, not a silent
    skip."""
    _patch_entry_points(
        monkeypatch,
        [_FakeEntryPoint("broken", "pkg:register", ImportError("no module named 'pkg'"))],
    )
    with pytest.raises(PluginLoadError, match="broken"):
        load_tool_entry_points(ToolRouter())


def test_non_callable_entry_point_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry point resolving to a non-callable is a load error."""
    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("data", "pkg:CONSTANT", 42)]
    )
    with pytest.raises(PluginLoadError, match="not callable"):
        load_tool_entry_points(ToolRouter())


def test_no_entry_points_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(monkeypatch, [])
    router = ToolRouter()
    assert load_tool_entry_points(router) == []
    assert router.describe_model() == []


# ---------------------------------------------------------------------------
# Lifecycle: enable / disable / unload
# ---------------------------------------------------------------------------


def _registry_with_greeter(monkeypatch: pytest.MonkeyPatch) -> tuple[PluginRegistry, ToolRouter]:
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("greeter", "pkg:register", _greeter_register)])
    router = ToolRouter()
    registry = PluginRegistry(router)
    assert registry.load_source(EntryPointSource()) == ["greeter"]
    return registry, router


def test_disable_removes_tools_and_enable_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    registry, router = _registry_with_greeter(monkeypatch)

    registry.disable("greeter")
    assert router.get("greet") is None
    record = registry.get("greeter")
    assert record is not None
    assert record.enabled is False
    assert record.tools == []

    registry.enable("greeter")
    assert router.get("greet") is not None
    record = registry.get("greeter")
    assert record is not None
    assert record.enabled is True
    assert record.tools == ["greet"]


def test_unload_drops_tools_and_record(monkeypatch: pytest.MonkeyPatch) -> None:
    registry, router = _registry_with_greeter(monkeypatch)
    registry.unload("greeter")
    assert router.get("greet") is None
    assert registry.get("greeter") is None
    assert registry.plugins() == []


def test_lifecycle_on_unknown_plugin_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    registry, _ = _registry_with_greeter(monkeypatch)
    for op in (registry.enable, registry.disable, registry.unload, registry.reload):
        with pytest.raises(PluginStateError, match="unknown plugin 'ghost'"):
            op("ghost")


def test_wrong_state_lifecycle_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    registry, _ = _registry_with_greeter(monkeypatch)
    with pytest.raises(PluginStateError, match="already enabled"):
        registry.enable("greeter")
    registry.disable("greeter")
    with pytest.raises(PluginStateError, match="already disabled"):
        registry.disable("greeter")
    with pytest.raises(PluginStateError, match="disabled"):
        registry.reload("greeter")


def test_plugin_name_conflict_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    registry, _ = _registry_with_greeter(monkeypatch)
    with pytest.raises(PluginLoadError, match="already loaded"):
        registry.load(PluginSpec(name="greeter", origin="test", load=lambda: _greeter_register))


# ---------------------------------------------------------------------------
# Tool-name conflicts and registration rollback
# ---------------------------------------------------------------------------


def test_tool_name_conflict_fails_loud_and_first_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    def reg_a(router: ToolRouter) -> None:
        router.register(lambda: "a", name="dup")

    def reg_b(router: ToolRouter) -> None:
        router.register(lambda: "b", name="dup")

    _patch_entry_points(
        monkeypatch,
        [
            _FakeEntryPoint("first", "a:register", reg_a),
            _FakeEntryPoint("second", "b:register", reg_b),
        ],
    )
    router = ToolRouter()
    registry = PluginRegistry(router)
    with pytest.raises(PluginLoadError, match="'second'.*dup"):
        registry.load_source(EntryPointSource())
    # The first registrant wins; the conflicting plugin stays unloaded.
    tool_meta = router.get("dup")
    assert tool_meta is not None
    assert tool_meta.handler() == "a"
    assert registry.get("second") is None


def test_register_failure_rolls_back_partial_registrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reg(router: ToolRouter) -> None:
        router.register(lambda: 1, name="partial")
        raise RuntimeError("boom")

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("broken", "pkg:register", reg)])
    router = ToolRouter()
    with pytest.raises(PluginLoadError, match="'broken'.*boom"):
        PluginRegistry(router).load_source(EntryPointSource())
    assert router.get("partial") is None


# ---------------------------------------------------------------------------
# Directory source
# ---------------------------------------------------------------------------


_V1 = "def register(router):\n    router.register(lambda: 'v1', name='greet')\n"
# Deliberately a different size than _V1 so the bytecode cache invalidates
# on mtime+size even when the two writes land in the same mtime tick.
_V2 = "def register(router):\n    router.register(lambda: 'version-two', name='greet')\n"


def test_directory_source_discovers_and_registers(tmp_path: Path) -> None:
    (tmp_path / "greeter.py").write_text(_V1)
    (tmp_path / "_helper.py").write_text("raise AssertionError('must not load')\n")

    router = ToolRouter()
    registry = PluginRegistry(router)
    assert registry.load_source(DirectorySource(tmp_path)) == ["greeter"]
    tool_meta = router.get("greet")
    assert tool_meta is not None
    assert tool_meta.handler() == "v1"
    record = registry.get("greeter")
    assert record is not None
    assert record.reloadable is True


def test_missing_plugin_directory_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(PluginLoadError, match="does not exist"):
        PluginRegistry(ToolRouter()).load_source(DirectorySource(tmp_path / "nope"))


def test_directory_file_without_register_fails_loud(tmp_path: Path) -> None:
    (tmp_path / "empty.py").write_text("X = 1\n")
    with pytest.raises(PluginLoadError, match="no top-level 'register'"):
        PluginRegistry(ToolRouter()).load_source(DirectorySource(tmp_path))


def test_directory_file_import_error_fails_loud(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("import no_such_module_anywhere\n")
    with pytest.raises(PluginLoadError, match="'broken'.*no_such_module_anywhere"):
        PluginRegistry(ToolRouter()).load_source(DirectorySource(tmp_path))


def test_loads_from_entry_points_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("greeter", "pkg:register", _greeter_register)])
    (tmp_path / "local_add.py").write_text(
        "def register(router):\n    router.register(lambda: 42, name='answer')\n"
    )
    router = ToolRouter()
    registry = PluginRegistry(router)
    assert registry.load_source(EntryPointSource()) == ["greeter"]
    assert registry.load_source(DirectorySource(tmp_path)) == ["local_add"]
    assert router.get("greet") is not None
    assert router.get("answer") is not None
    assert [p.name for p in registry.plugins()] == ["greeter", "local_add"]


# ---------------------------------------------------------------------------
# Hot reload
# ---------------------------------------------------------------------------


def test_directory_reload_picks_up_new_code(tmp_path: Path) -> None:
    plugin_file = tmp_path / "greeter.py"
    plugin_file.write_text(_V1)
    router = ToolRouter()
    registry = PluginRegistry(router)
    registry.load_source(DirectorySource(tmp_path))
    tool_meta = router.get("greet")
    assert tool_meta is not None
    assert tool_meta.handler() == "v1"

    plugin_file.write_text(_V2)
    registry.reload("greeter")

    tool_meta = router.get("greet")
    assert tool_meta is not None
    assert tool_meta.handler() == "version-two"
    record = registry.get("greeter")
    assert record is not None
    assert record.enabled is True
    assert record.tools == ["greet"]


def test_reload_reimport_failure_keeps_current_tools(tmp_path: Path) -> None:
    plugin_file = tmp_path / "greeter.py"
    plugin_file.write_text(_V1)
    router = ToolRouter()
    registry = PluginRegistry(router)
    registry.load_source(DirectorySource(tmp_path))

    plugin_file.write_text("import no_such_module_anywhere\n")
    with pytest.raises(PluginLoadError, match="re-import"):
        registry.reload("greeter")
    # The fresh register callable never resolved, so the old tools stay.
    assert router.get("greet") is not None
    record = registry.get("greeter")
    assert record is not None
    assert record.enabled is True


def test_reload_reregistration_failure_leaves_plugin_disabled(tmp_path: Path) -> None:
    plugin_file = tmp_path / "greeter.py"
    plugin_file.write_text(_V1)
    router = ToolRouter()
    registry = PluginRegistry(router)
    registry.load_source(DirectorySource(tmp_path))
    # A host tool grabs the name between load and reload: re-registration
    # conflicts after the old tools were already unregistered.
    router.register(lambda: "host", name="host_tool")
    plugin_file.write_text(
        "def register(router):\n"
        "    router.register(lambda: 'v2', name='greet')\n"
        "    router.register(lambda: 'x', name='host_tool')\n"
    )
    with pytest.raises(PluginLoadError, match="failed to register"):
        registry.reload("greeter")
    assert router.get("greet") is None
    record = registry.get("greeter")
    assert record is not None
    assert record.enabled is False


def test_reload_non_reloadable_fails_loud() -> None:
    registry = PluginRegistry(ToolRouter())
    registry.load(
        PluginSpec(name="static", origin="test", load=lambda: _greeter_register, reload=None)
    )
    with pytest.raises(PluginStateError, match="not reloadable"):
        registry.reload("static")


def test_entry_point_reload_reimports_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry-point plugin reloads via importlib.reload on the module its
    value points into, then re-resolves the attribute path."""
    mod_file = tmp_path / "ep_plugin_mod.py"
    mod_file.write_text(_V1)
    spec = importlib.util.spec_from_file_location("ep_plugin_mod", mod_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ep_plugin_mod"] = module
    try:
        spec.loader.exec_module(module)
        _patch_entry_points(
            monkeypatch,
            [_FakeEntryPoint("greeter", "ep_plugin_mod:register", module.register)],
        )
        router = ToolRouter()
        registry = PluginRegistry(router)
        registry.load_source(EntryPointSource())
        tool_meta = router.get("greet")
        assert tool_meta is not None
        assert tool_meta.handler() == "v1"

        mod_file.write_text(_V2)
        registry.reload("greeter")

        tool_meta = router.get("greet")
        assert tool_meta is not None
        assert tool_meta.handler() == "version-two"
    finally:
        sys.modules.pop("ep_plugin_mod", None)
