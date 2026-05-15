"""Unit tests for the build_sidecar prune helpers.

These tests build a fake portable Python tree on disk that mirrors the layout
``python-build-standalone`` produces, then exercise each prune helper in
isolation. Network is never touched — CI can run this on every PR, while the
real ``--target host`` build only runs on tag pushes.

The shape we mock looks like::

    fake-runtime/
      python/
        bin/python3
        bin/pip3
        lib/python3.12/
          ensurepip/...                   <- stripped (conservative)
          tkinter/__init__.py             <- stripped (conservative)
          asyncio/__init__.py             <- KEPT (sidecar uses it)
          xmlrpc/__init__.py              <- stripped (conservative)
          venv/__init__.py                <- stripped (aggressive only)
          site-packages/
            httpx/__init__.py             <- KEPT
            httpx/tests/test_x.py         <- stripped (site-packages tests)
            steerable_agent_runtime/...   <- KEPT (framework package)
            steerable_agent_runtime/tests/...  <- KEPT (we want framework tests)
            httpx-1.0.dist-info/METADATA  <- KEPT
            httpx-1.0.dist-info/LICENSE   <- stripped
            pip/__init__.py               <- stripped (pip tooling)
            pip-25.0.dist-info/METADATA   <- stripped (pip tooling)
            __pycache__/x.pyc             <- stripped
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BUILD_DIR = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "build_sidecar", BUILD_DIR / "build_sidecar.py"
)
assert spec and spec.loader
build_sidecar = importlib.util.module_from_spec(spec)
sys.modules["build_sidecar"] = build_sidecar
spec.loader.exec_module(build_sidecar)


@pytest.fixture()
def fake_runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "fake-runtime"

    bin_dir = runtime / "python" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3").write_text("#!/bin/sh\n")
    (bin_dir / "pip3").write_text("#!/bin/sh\n")

    lib = runtime / "python" / "lib" / "python3.12"
    site = lib / "site-packages"
    site.mkdir(parents=True)

    # Stdlib modules we strip in the conservative pass.
    for mod in ("ensurepip", "tkinter", "xmlrpc", "wsgiref", "test", "idlelib"):
        d = lib / mod
        d.mkdir()
        (d / "__init__.py").write_text("# stub\n")
        (d / "_data.txt").write_bytes(b"x" * 4096)

    # Stdlib modules we keep — sidecar runtime needs them.
    for mod in ("asyncio", "json", "logging"):
        d = lib / mod
        d.mkdir()
        (d / "__init__.py").write_text("# stub\n")

    # Modules removed only with --aggressive.
    for mod in ("venv",):
        d = lib / mod
        d.mkdir()
        (d / "__init__.py").write_text("# stub\n")

    # Third-party package with bundled tests/docs/examples.
    httpx = site / "httpx"
    httpx.mkdir()
    (httpx / "__init__.py").write_text("__version__ = '1.0'\n")
    (httpx / "tests").mkdir()
    (httpx / "tests" / "test_x.py").write_bytes(b"y" * 8192)
    (httpx / "examples").mkdir()
    (httpx / "examples" / "demo.py").write_bytes(b"z" * 4096)
    (httpx / "docs").mkdir()
    (httpx / "docs" / "index.md").write_bytes(b"d" * 2048)

    # dist-info trimming.
    di = site / "httpx-1.0.dist-info"
    di.mkdir()
    (di / "METADATA").write_text("Metadata-Version: 2.1\n")
    (di / "WHEEL").write_text("Wheel-Version: 1.0\n")
    (di / "RECORD").write_text("httpx/__init__.py,sha256=,42\n")
    (di / "LICENSE").write_text("Apache-2.0\n")
    (di / "AUTHORS").write_text("contributors\n")

    # pip tooling we drop.
    pip_pkg = site / "pip"
    pip_pkg.mkdir()
    (pip_pkg / "__init__.py").write_bytes(b"p" * 1024)
    pip_di = site / "pip-25.0.dist-info"
    pip_di.mkdir()
    (pip_di / "METADATA").write_text("Name: pip\n")

    # Framework package — its tests should be PRESERVED.
    fw = site / "steerable_agent_runtime"
    fw.mkdir()
    (fw / "__init__.py").write_text("__version__ = '0.1.0'\n")
    fw_tests = fw / "tests"
    fw_tests.mkdir()
    (fw_tests / "test_runtime.py").write_text("def test_x(): pass\n")

    # __pycache__ scattered around.
    pycache = httpx / "__pycache__"
    pycache.mkdir()
    (pycache / "module.cpython-312.pyc").write_bytes(b"\x00" * 4096)

    return runtime


def test_prune_stdlib_conservative_strips_known_modules(fake_runtime: Path) -> None:
    lib = fake_runtime / "python" / "lib" / "python3.12"
    assert (lib / "tkinter").exists()
    assert (lib / "venv").exists()  # not stripped without aggressive

    build_sidecar.prune_stdlib(fake_runtime, aggressive=False)

    assert not (lib / "tkinter").exists()
    assert not (lib / "ensurepip").exists()
    assert not (lib / "xmlrpc").exists()
    assert not (lib / "wsgiref").exists()
    # KEPT modules untouched.
    assert (lib / "asyncio").exists()
    assert (lib / "json").exists()
    # NOT stripped without aggressive.
    assert (lib / "venv").exists()


def test_prune_stdlib_aggressive_strips_more(fake_runtime: Path) -> None:
    lib = fake_runtime / "python" / "lib" / "python3.12"

    build_sidecar.prune_stdlib(fake_runtime, aggressive=True)

    assert not (lib / "venv").exists()
    # Other conservative ones still gone.
    assert not (lib / "tkinter").exists()
    # Critical KEPT.
    assert (lib / "asyncio").exists()


def test_prune_site_packages_strips_third_party_tests(fake_runtime: Path) -> None:
    site = fake_runtime / "python" / "lib" / "python3.12" / "site-packages"
    httpx = site / "httpx"
    fw_tests = site / "steerable_agent_runtime" / "tests"

    assert (httpx / "tests").exists()
    assert (httpx / "examples").exists()
    assert (httpx / "docs").exists()
    assert fw_tests.exists()

    build_sidecar.prune_site_packages(fake_runtime)

    assert not (httpx / "tests").exists()
    assert not (httpx / "examples").exists()
    assert not (httpx / "docs").exists()
    # Framework tests are PRESERVED on purpose.
    assert fw_tests.exists()
    assert (httpx / "__init__.py").exists()


def test_prune_pycache_removes_all_caches(fake_runtime: Path) -> None:
    cache_dirs = list(fake_runtime.rglob("__pycache__"))
    assert cache_dirs

    build_sidecar.prune_pycache(fake_runtime)

    assert list(fake_runtime.rglob("__pycache__")) == []


def test_prune_dist_info_trims_metadata(fake_runtime: Path) -> None:
    site = fake_runtime / "python" / "lib" / "python3.12" / "site-packages"
    di = site / "httpx-1.0.dist-info"
    assert (di / "LICENSE").exists()
    assert (di / "AUTHORS").exists()

    build_sidecar.prune_dist_info(fake_runtime)

    assert (di / "METADATA").exists()
    assert (di / "WHEEL").exists()
    assert (di / "RECORD").exists()
    assert not (di / "LICENSE").exists()
    assert not (di / "AUTHORS").exists()


def test_remove_pip_tooling_drops_pip_and_binary(fake_runtime: Path) -> None:
    site = fake_runtime / "python" / "lib" / "python3.12" / "site-packages"
    bin_dir = fake_runtime / "python" / "bin"

    target = build_sidecar.TARGETS["darwin-arm64"]
    build_sidecar.remove_pip_tooling(fake_runtime, target)

    assert not (site / "pip").exists()
    assert not (site / "pip-25.0.dist-info").exists()
    assert not (bin_dir / "pip3").exists()
    # python3 itself untouched.
    assert (bin_dir / "python3").exists()


def test_directory_size_returns_total_bytes(fake_runtime: Path) -> None:
    total = build_sidecar.directory_size(fake_runtime)
    assert total > 0
    # Should drop after pruning.
    build_sidecar.prune_pycache(fake_runtime)
    build_sidecar.prune_site_packages(fake_runtime)
    smaller = build_sidecar.directory_size(fake_runtime)
    assert smaller < total


def test_format_bytes_humanises_units() -> None:
    assert build_sidecar.format_bytes(0) == "0.0 B"
    assert build_sidecar.format_bytes(1024) == "1.0 KB"
    assert build_sidecar.format_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert build_sidecar.format_bytes(2 * 1024 * 1024 * 1024) == "2.0 GB"


def test_install_sidecar_from_wheels_validates_inventory(
    fake_runtime: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--from-wheels`` must refuse to bundle a runtime that's missing a wheel."""
    wheels = tmp_path / "dist-py"
    wheels.mkdir()
    # Three of the four required wheels — agent-runtime is intentionally absent.
    for stem in (
        "steerable_agent_protocol",
        "steerable_agent_harness",
        "steerable_sidecar",
    ):
        (wheels / f"{stem}-0.1.0-py3-none-any.whl").write_bytes(b"PK\x03\x04stub\n")

    target = build_sidecar.TARGETS["darwin-arm64"]
    monkeypatch.setattr(build_sidecar.subprocess, "run", lambda *a, **kw: None)

    with pytest.raises(SystemExit) as excinfo:
        build_sidecar.install_sidecar(fake_runtime, target, wheels_dir=wheels)
    assert "steerable_agent_runtime" in str(excinfo.value)


def test_install_sidecar_from_wheels_picks_latest(
    fake_runtime: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When multiple wheel versions exist, the lexicographically last wins."""
    wheels = tmp_path / "dist-py"
    wheels.mkdir()
    for stem in (
        "steerable_agent_protocol",
        "steerable_agent_harness",
        "steerable_agent_runtime",
        "steerable_sidecar",
    ):
        (wheels / f"{stem}-0.1.0-py3-none-any.whl").write_bytes(b"stub")
        (wheels / f"{stem}-0.2.0-py3-none-any.whl").write_bytes(b"stub")

    invocations: list[list[str]] = []

    def fake_run(cmd, **_kw):
        invocations.append(list(cmd))
        class _R:  # minimal CompletedProcess shim
            returncode = 0
        return _R()

    target = build_sidecar.TARGETS["darwin-arm64"]
    monkeypatch.setattr(build_sidecar.subprocess, "run", fake_run)

    build_sidecar.install_sidecar(fake_runtime, target, wheels_dir=wheels)

    pip_install_targets = [
        cmd[-1] for cmd in invocations if "install" in cmd and cmd[-1].endswith(".whl")
    ]
    # Four wheels, all the 0.2.0 variants.
    assert len(pip_install_targets) == 4
    for path in pip_install_targets:
        assert "0.2.0" in path
        assert "0.1.0" not in path
