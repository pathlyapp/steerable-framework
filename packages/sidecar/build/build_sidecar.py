#!/usr/bin/env python3
"""Build a portable steerable-sidecar runtime.

Downloads python-build-standalone for the requested target platform(s),
extracts it under ``dist/python-runtime/<target>/``, then installs the sidecar
wheel (and its dependencies) into the embedded site-packages.

Usage::

    ./build_sidecar.py                    # host platform
    ./build_sidecar.py --target all       # all 4 supported platforms
    ./build_sidecar.py --target win32-x64 --python-version 3.12.7

Run from anywhere; paths are resolved relative to the script.

Design notes:
* Dependencies are intentionally limited to the standard library so this can
  bootstrap from a minimal CI runner.
* Nothing is mutated outside ``dist/``; re-running is safe.
* ``--strip-stdlib`` is opt-in and trims modules we do not use, reducing the
  unsigned bundle size by ~40 MB on macOS arm64.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[3]
SIDECAR_PKG_DIR = Path(__file__).resolve().parent.parent
DIST_DIR = SIDECAR_PKG_DIR / "dist" / "python-runtime"

# Pinned for byte-reproducible builds. When this 404s in CI it means the
# release has been deleted upstream — bump both constants in lockstep
# (the asset filename embeds the date), then re-run sidecar-budget locally
# to verify the new pin still fits the 320 MB PR budget. Discover the
# current latest with:
#   gh api repos/astral-sh/python-build-standalone/releases/latest --jq .tag_name
PYTHON_BUILD_STANDALONE_RELEASE = "20260510"
DEFAULT_PYTHON_VERSION = "3.12.13"

# Modules typically unused by the sidecar — safe to strip.
# CONSERVATIVE list: definitely unused by us, no third-party module pulls them in.
STDLIB_STRIP_CANDIDATES = (
    "ensurepip",
    "idlelib",
    "lib2to3",
    "pydoc_data",
    "test",
    "tkinter",
    "turtledemo",
    "unittest/test",
    # GUI / shell helpers we don't ship.
    "tkinter/test",
    # Sample / docs data baked into stdlib.
    "distutils/tests",
    "sqlite3/test",
    "ctypes/test",
    # Old wire formats nobody on the sidecar speaks.
    "xmlrpc",
    "wsgiref",
)

# AGGRESSIVE list: only stripped with --strip-stdlib --aggressive. We have used
# these in the past or they look harmless but a third-party pin (anthropic /
# sqlalchemy / starlette) might reach for them. Keep behind a flag.
STDLIB_STRIP_AGGRESSIVE = (
    "msilib",  # Windows-only installer
    "venv",    # we never spawn nested venvs at runtime
    "ensurepip",
    "config-3.12-darwin",  # build artefacts
)

# Site-packages directories that ship megabytes of test fixtures we don't need.
SITE_PACKAGES_STRIP = (
    "tests",
    "test",
    "testing",
    "examples",
    "docs",
    "doc",
    "samples",
)

# Non-essential metadata under .dist-info; keep METADATA + WHEEL + RECORD only.
DIST_INFO_KEEP = {"METADATA", "WHEEL", "RECORD", "INSTALLER", "entry_points.txt"}


@dataclass(frozen=True)
class Target:
    name: str
    triple: str
    python_executable: str

    @property
    def archive_filename(self) -> str:
        return (
            f"cpython-{DEFAULT_PYTHON_VERSION}+{PYTHON_BUILD_STANDALONE_RELEASE}-"
            f"{self.triple}-install_only.tar.gz"
        )

    def archive_url(self, version: str) -> str:
        return (
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"{PYTHON_BUILD_STANDALONE_RELEASE}/"
            f"cpython-{version}+{PYTHON_BUILD_STANDALONE_RELEASE}-{self.triple}-install_only.tar.gz"
        )


TARGETS: dict[str, Target] = {
    "darwin-arm64": Target("darwin-arm64", "aarch64-apple-darwin", "bin/python3"),
    "darwin-x64": Target("darwin-x64", "x86_64-apple-darwin", "bin/python3"),
    "linux-x64": Target("linux-x64", "x86_64-unknown-linux-gnu", "bin/python3"),
    "win32-x64": Target("win32-x64", "x86_64-pc-windows-msvc", "python.exe"),
}


def host_target() -> Target:
    sys_platform = sys.platform
    machine = platform.machine().lower()
    if sys_platform == "darwin":
        return TARGETS["darwin-arm64"] if machine in {"arm64", "aarch64"} else TARGETS["darwin-x64"]
    if sys_platform.startswith("linux"):
        return TARGETS["linux-x64"]
    if sys_platform == "win32":
        return TARGETS["win32-x64"]
    raise SystemExit(f"Unsupported host platform: {sys_platform}/{machine}")


def build(
    target: Target,
    *,
    python_version: str,
    strip_stdlib: bool,
    aggressive: bool,
    wheels_dir: Path | None = None,
) -> Path:
    out_dir = DIST_DIR / target.name
    if out_dir.exists():
        print(f"[clean] removing existing {out_dir}")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    archive_url = target.archive_url(python_version)
    archive_path = DIST_DIR / "_cache" / target.archive_filename
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if not archive_path.exists():
        print(f"[fetch] {archive_url}")
        urllib.request.urlretrieve(archive_url, archive_path)

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    print(f"[fetch] sha256={digest}")

    print(f"[extract] {archive_path} -> {out_dir}")
    with tarfile.open(archive_path, mode="r:gz") as tar:
        tar.extractall(out_dir)

    install_sidecar(out_dir, target, wheels_dir=wheels_dir)
    initial_size = directory_size(out_dir)
    if strip_stdlib:
        prune_stdlib(out_dir, aggressive=aggressive)
        prune_site_packages(out_dir)
        prune_pycache(out_dir)
        prune_dist_info(out_dir)
        remove_pip_tooling(out_dir, target)
        final_size = directory_size(out_dir)
        saved = initial_size - final_size
        print(
            f"[prune] before={format_bytes(initial_size)} "
            f"after={format_bytes(final_size)} saved={format_bytes(saved)}"
        )
    write_manifest(out_dir, target, digest, python_version)
    return out_dir


def install_sidecar(
    out_dir: Path,
    target: Target,
    *,
    wheels_dir: Path | None = None,
) -> None:
    """Install steerable framework into the embedded interpreter.

    Two modes:
    * **Source** (default): pip-install the four framework packages from their
      sibling source trees. Best during development — picks up local changes.
    * **Wheels**: when ``wheels_dir`` is provided, pip-install the matching
      wheels from there. Use this for reproducible release builds — pair with
      ``framework/dist/py/`` produced by ``scripts/release/build-local-artifacts.sh``.
    """
    py = python_binary(out_dir, target)
    print(f"[pip] using {py}")
    subprocess.run(
        [str(py), "-m", "ensurepip", "--upgrade"],
        check=True,
    )
    subprocess.run(
        [str(py), "-m", "pip", "install", "--upgrade", "pip", "--no-warn-script-location"],
        check=True,
    )
    if wheels_dir is not None:
        wheel_targets: list[Path] = []
        wanted = (
            "steerable_agent_protocol",
            "steerable_agent_harness",
            "steerable_agent_runtime",
            "steerable_sidecar",
        )
        for stem in wanted:
            matches = sorted(wheels_dir.glob(f"{stem}-*.whl"))
            if not matches:
                raise SystemExit(
                    f"--from-wheels {wheels_dir} is missing a wheel for {stem}"
                )
            wheel_targets.append(matches[-1])
        for wheel in wheel_targets:
            print(f"[pip] install (wheel) {wheel.name}")
            subprocess.run(
                [str(py), "-m", "pip", "install", "--no-warn-script-location", str(wheel)],
                check=True,
            )
        return

    pkg_paths = (
        ROOT / "packages" / "agent-protocol" / "py",
        ROOT / "packages" / "agent-harness" / "py",
        ROOT / "packages" / "agent-runtime" / "py",
        ROOT / "packages" / "sidecar" / "py",
    )
    for path in pkg_paths:
        print(f"[pip] install (source) {path}")
        subprocess.run(
            [str(py), "-m", "pip", "install", "--no-warn-script-location", str(path)],
            check=True,
        )


def python_binary(out_dir: Path, target: Target) -> Path:
    candidates = [
        out_dir / "python" / target.python_executable,
        out_dir / target.python_executable,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit(f"Could not locate python binary for {target.name} under {out_dir}")


def prune_stdlib(out_dir: Path, *, aggressive: bool) -> None:
    print(f"[prune] stripping unused stdlib modules (aggressive={aggressive})")
    candidates = list(STDLIB_STRIP_CANDIDATES)
    if aggressive:
        candidates.extend(STDLIB_STRIP_AGGRESSIVE)
    for relative in iter_stdlib_dirs(out_dir):
        for candidate in candidates:
            target = relative / candidate
            if target.exists():
                size = directory_size(target)
                shutil.rmtree(target, ignore_errors=True)
                print(f"  - {target.relative_to(out_dir)} ({format_bytes(size)})")


def prune_site_packages(out_dir: Path) -> None:
    """Strip test/example/docs subtrees from third-party packages."""
    print("[prune] stripping site-packages tests/examples/docs")
    for site_dir in iter_site_packages_dirs(out_dir):
        for entry in site_dir.rglob("*"):
            if not entry.is_dir():
                continue
            if entry.name in SITE_PACKAGES_STRIP:
                # Skip framework's own packages — we want to keep their tests
                # available for in-process debugging.
                if any(p.startswith("steerable_") for p in entry.parts):
                    continue
                size = directory_size(entry)
                shutil.rmtree(entry, ignore_errors=True)
                if size > 1024 * 1024:  # only log >1MB to keep output sane
                    print(f"  - {entry.relative_to(out_dir)} ({format_bytes(size)})")


def prune_pycache(out_dir: Path) -> None:
    """Drop __pycache__ — runtime regenerates as needed."""
    print("[prune] stripping __pycache__ directories")
    for cache in out_dir.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)


def prune_dist_info(out_dir: Path) -> None:
    """Trim .dist-info to only the wheel-spec required files."""
    print("[prune] trimming .dist-info metadata")
    for site_dir in iter_site_packages_dirs(out_dir):
        for dist_info in site_dir.glob("*.dist-info"):
            for entry in dist_info.iterdir():
                if entry.name in DIST_INFO_KEEP:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    try:
                        entry.unlink()
                    except OSError:
                        pass


def remove_pip_tooling(out_dir: Path, target: Target) -> None:
    """Drop pip + setuptools after installation — runtime does not pip install."""
    print("[prune] removing pip / setuptools / wheel from runtime")
    for site_dir in iter_site_packages_dirs(out_dir):
        for name in ("pip", "setuptools", "wheel", "_distutils_hack", "pkg_resources"):
            target_path = site_dir / name
            if target_path.exists():
                shutil.rmtree(target_path, ignore_errors=True)
            for dist_info in site_dir.glob(f"{name}-*.dist-info"):
                shutil.rmtree(dist_info, ignore_errors=True)
    bin_dir = out_dir / "python" / "bin"
    if not bin_dir.exists():
        bin_dir = out_dir / "bin"
    if bin_dir.exists():
        for name in ("pip", "pip3", f"pip{sys.version_info.major}.{sys.version_info.minor}"):
            for ext in ("", ".exe"):
                candidate = bin_dir / f"{name}{ext}"
                if candidate.exists():
                    try:
                        candidate.unlink()
                    except OSError:
                        pass


def iter_site_packages_dirs(out_dir: Path) -> Iterable[Path]:
    """Yield all site-packages roots under the runtime tree."""
    seen: set[Path] = set()
    for candidate in out_dir.rglob("site-packages"):
        if candidate.is_dir() and candidate not in seen:
            seen.add(candidate)
            yield candidate


def iter_stdlib_dirs(out_dir: Path) -> Iterable[Path]:
    for parent in (out_dir / "python" / "lib", out_dir / "lib", out_dir / "Lib"):
        if not parent.exists():
            continue
        for entry in parent.iterdir():
            if entry.is_dir() and entry.name.startswith("python"):
                yield entry
        if parent.name == "Lib":
            yield parent


def write_manifest(out_dir: Path, target: Target, digest: str, python_version: str) -> None:
    payload = {
        "target": target.name,
        "triple": target.triple,
        "pythonVersion": python_version,
        "pythonBuildStandaloneRelease": PYTHON_BUILD_STANDALONE_RELEASE,
        "archiveSha256": digest,
        "totalBytes": directory_size(out_dir),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def directory_size(path: Path) -> int:
    total = 0
    for sub in path.rglob("*"):
        try:
            if sub.is_file():
                total += sub.stat().st_size
        except OSError:
            pass
    return total


def format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    i = 0
    fp = float(value)
    while fp >= 1024 and i < len(units) - 1:
        fp /= 1024
        i += 1
    return f"{fp:.1f} {units[i]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        default="host",
        help="Target name (host|all|<target-name>). See TARGETS dict in source.",
    )
    parser.add_argument(
        "--python-version",
        default=DEFAULT_PYTHON_VERSION,
        help="Embedded CPython version (must match python-build-standalone release)",
    )
    parser.add_argument(
        "--strip-stdlib",
        action="store_true",
        help="Remove unused standard library modules and pip tooling to shrink the bundle",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="Also strip modules from STDLIB_STRIP_AGGRESSIVE (smaller, but slightly riskier)",
    )
    parser.add_argument(
        "--budget-mb",
        type=int,
        default=None,
        help="Fail the build if the per-target bundle exceeds this size in MB",
    )
    parser.add_argument(
        "--from-wheels",
        type=Path,
        default=None,
        metavar="WHEELS_DIR",
        help=(
            "Install the steerable framework into the embedded site-packages "
            "from wheels under WHEELS_DIR (typically <repo>/dist/py produced "
            "by scripts/release/build-local-artifacts.sh) instead of from "
            "the local source trees. Use for reproducible release bundles."
        ),
    )
    args = parser.parse_args()
    wheels_dir = args.from_wheels.resolve() if args.from_wheels is not None else None
    if wheels_dir is not None and not wheels_dir.is_dir():
        raise SystemExit(f"--from-wheels {wheels_dir} is not a directory")

    selected: list[Target]
    if args.target == "host":
        selected = [host_target()]
    elif args.target == "all":
        selected = list(TARGETS.values())
    else:
        target = TARGETS.get(args.target)
        if target is None:
            raise SystemExit(
                f"Unknown target {args.target}. Choose from: host, all, {sorted(TARGETS)}"
            )
        selected = [target]

    over_budget: list[tuple[str, int]] = []
    for target in selected:
        out = build(
            target,
            python_version=args.python_version,
            strip_stdlib=args.strip_stdlib,
            aggressive=args.aggressive,
            wheels_dir=wheels_dir,
        )
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        size_bytes = manifest["totalBytes"]
        print(
            f"[done] {target.name} -> {out} (size={format_bytes(size_bytes)})"
        )
        if args.budget_mb is not None:
            mb = size_bytes / (1024 * 1024)
            if mb > args.budget_mb:
                over_budget.append((target.name, int(mb)))

    if over_budget:
        bullets = "\n".join(f"  - {name}: {mb} MB" for name, mb in over_budget)
        print(
            "\n[budget] FAIL: the following bundles exceed --budget-mb="
            f"{args.budget_mb} MB:\n{bullets}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
