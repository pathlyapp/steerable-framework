"""Entry-point tool discovery (``steerable.tools`` group)."""

from __future__ import annotations

from typing import Any

import pytest

from steerable_agent_runtime import (
    PluginLoadError,
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


def test_discovers_and_registers_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """A package's register(router) callable runs; its @tool functions land
    on the host's router and are model-visible."""

    def register(router: ToolRouter) -> None:
        @tool(router=router, description="Greet by name")
        async def greet(name: str) -> str:
            return f"hello {name}"

    _patch_entry_points(monkeypatch, [_FakeEntryPoint("greeter", "pkg:register", register)])

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
