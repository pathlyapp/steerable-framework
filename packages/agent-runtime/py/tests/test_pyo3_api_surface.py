"""Freeze the CoreLoop calling surface deeppath-api already uses.

PyO3 must keep these imports, LoopEvent kinds, LoopHooks methods, and
CoreLoop/LoopConfig constructor fields. A rename here is an API break.
"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from typing import get_args

import pytest
from steerable_agent_runtime.loop import LoopEventKind, ToolExecutor

REPO_ROOT = Path(__file__).resolve().parents[4]
CATALOG_PATH = REPO_ROOT / "docs" / "spec" / "coreloop-rust-test-catalog.json"


def _catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def test_catalog_listed_framework_paths_exist() -> None:
    catalog = _catalog()
    missing: list[str] = []
    for stage in catalog["stages"]:
        for spec in stage.get("tests", []):
            if spec.get("repo") != "steerable-framework":
                continue
            if spec.get("status") == "planned":
                continue
            path = REPO_ROOT / spec["path"]
            if not path.exists():
                missing.append(spec["path"])
    assert missing == [], f"catalog paths missing: {missing}"


def test_pyo3_required_imports_are_exported() -> None:
    catalog = _catalog()
    errors: list[str] = []
    for module_name, names in catalog["pyo3Surface"].items():
        module = importlib.import_module(module_name)
        for name in names:
            if not hasattr(module, name):
                errors.append(f"{module_name}.{name}")
    assert errors == [], f"PyO3 surface missing: {errors}"


def test_loop_event_kinds_match_catalog() -> None:
    catalog = _catalog()
    assert set(get_args(LoopEventKind)) == set(catalog["loopEventKinds"])


def test_loop_hooks_methods_match_catalog() -> None:
    catalog = _catalog()
    from steerable_agent_runtime.hooks import LoopHooks

    for name in catalog["loopHooksMethods"]:
        assert hasattr(LoopHooks, name), f"LoopHooks.{name} missing"


def test_tool_executor_execute_returns_tool_result() -> None:
    signature = inspect.signature(ToolExecutor.execute)
    assert "call" in signature.parameters
    assert "ctx" in signature.parameters
    returned = signature.return_annotation
    name = getattr(returned, "__name__", str(returned))
    assert name.endswith("ToolResult")


def test_coreloop_constructor_and_run_kwargs() -> None:
    catalog = _catalog()
    from steerable_agent_runtime import CoreLoop, LoopConfig

    init_params = list(inspect.signature(CoreLoop.__init__).parameters)
    assert init_params[0] == "self"
    assert init_params[1:] == catalog["coreLoopInitParams"]

    run_params = inspect.signature(CoreLoop.run).parameters
    assert list(run_params)[1] == "messages"
    for name in catalog["coreLoopRunKwargs"]:
        assert name in run_params
    assert hasattr(CoreLoop, "last_run_usage")

    config_fields = {f.name for f in LoopConfig.__dataclass_fields__.values()}
    missing = [name for name in catalog["loopConfigFieldsUsedByApi"] if name not in config_fields]
    assert missing == [], f"LoopConfig missing API fields: {missing}"


def test_native_coreloop_module_exports_run_turn() -> None:
    """PyO3 wheel must be importable so deeppath-api can opt into Rust CoreLoop."""
    native = pytest.importorskip("steerable_agent_runtime_native")
    assert hasattr(native, "run_turn")
    assert native.run_turn is not None
    version = getattr(native, "__version__", "")
    assert version, "native module must export __version__"
