# Sidecar binary packaging

The `steerable-sidecar` Python package can be embedded directly via
`pip install steerable-sidecar`, but for shipping inside a desktop application
(e.g. `deeppath-agent`) we ship a **portable, self-contained CPython** runtime
based on [python-build-standalone].

This directory holds the cross-platform packaging pipeline. The output layout is

```
dist/python-runtime/
  darwin-arm64/
    bin/python3
    lib/python3.12/...
    lib/site-packages/steerable_sidecar/...
  darwin-x64/
  linux-x64/
  win32-x64/
```

A desktop host then spawns the platform-specific `bin/python3` (or
`python.exe` on Windows) with the `-m steerable_sidecar` arguments described
in `spec/sidecar/README.md`.

## Why portable Python and not PyInstaller / Nuitka?

* `python-build-standalone` is the same toolchain used by `uv`, `pyoxidizer`,
  and Bazel rules_python. It produces a real CPython that any wheel works
  against — no AOT compilation surprises.
* Builds are reproducible (signed sha256 manifests upstream).
* macOS notarization and Windows authenticode signing both support real
  Mach-O / PE binaries; PyInstaller bundles can be tricky to notarize.

## Targets

| Tag | Platform | Arch | Toolchain |
| --- | --- | --- | --- |
| `darwin-arm64` | macOS 11+ | aarch64 | python-build-standalone `aarch64-apple-darwin` |
| `darwin-x64`   | macOS 11+ | x86_64  | python-build-standalone `x86_64-apple-darwin` |
| `linux-x64`    | glibc 2.31+ | x86_64 | python-build-standalone `x86_64-unknown-linux-gnu` |
| `win32-x64`    | Windows 10+ | x86_64 | python-build-standalone `x86_64-pc-windows-msvc` |

## Local build

```bash
# Single platform (host arch by default)
./build_sidecar.py --target darwin-arm64

# Aggressive shrink to chase the 300 MB budget
./build_sidecar.py --target darwin-arm64 --strip-stdlib --aggressive --budget-mb 300

# Cross-build all
./build_sidecar.py --target all --strip-stdlib --aggressive --budget-mb 300
```

The script downloads the matching python-build-standalone tarball, extracts it
to `dist/python-runtime/<target>/`, then `pip install` the sidecar wheel into
that runtime's `site-packages`.

## Bundle size budget (P5)

Target: **≤ 300 MB per platform**, unsigned. The build keeps that promise by
chaining several prune passes in `build_sidecar.py`:

| Pass | Trigger | What it removes |
| --- | --- | --- |
| `prune_stdlib` (conservative) | `--strip-stdlib` | `ensurepip`, `idlelib`, `lib2to3`, `pydoc_data`, `test`, `tkinter`, `turtledemo`, `unittest/test`, `xmlrpc`, `wsgiref`, `*/test`, `*/tests` |
| `prune_stdlib` (aggressive) | `--strip-stdlib --aggressive` | also `msilib`, `venv`, build-time `config-*` |
| `prune_site_packages` | `--strip-stdlib` | third-party `tests/`, `examples/`, `docs/`, `samples/` (framework's own packages preserved) |
| `prune_pycache` | `--strip-stdlib` | every `__pycache__/` (regenerated lazily at runtime) |
| `prune_dist_info` | `--strip-stdlib` | trims `*.dist-info` to `METADATA`, `WHEEL`, `RECORD`, `INSTALLER`, `entry_points.txt` |
| `remove_pip_tooling` | `--strip-stdlib` | drops `pip`, `setuptools`, `wheel`, `pkg_resources`, plus matching dist-infos and `bin/pip*` shims |

`--budget-mb N` enforces the cap as a hard build failure so size regressions
cannot land silently.

The prune helpers are unit-tested without a network connection in
`tests/test_prune.py` (CI: `.github/workflows/sidecar-prune-tests.yml`); the
real per-target build still runs on tag pushes via
`.github/workflows/sidecar-build.yml`.

## CI integration

GitHub Actions matrix lives at
`.github/workflows/sidecar-build.yml`. Each runner builds its native target,
prints the bundle size as a `::notice` annotation, and uploads the result as an
artifact. A separate `sidecar-sign` job pulls the artifacts, runs
platform-specific signing (codesign / signtool), and stores the signed bundles
for the desktop app to fetch.

[python-build-standalone]: https://github.com/astral-sh/python-build-standalone
