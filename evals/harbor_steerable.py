"""Harbor BaseInstalledAgent for the Steerable/DeepPath product loop.

Installs workspace Python packages into a venv in the trial container and
runs ``python -m steerable_sidecar.headless`` against the task instruction.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

try:
    from typing import override
except ImportError:  # Python < 3.12 — evals unit tests still collect.
    def override(f):  # type: ignore[misc]
        return f

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.model_connection import ModelConnectionSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from evals.harbor_helpers import (
    _APT_PYTHON_INSTALL,
    _NO_PROXY_ENV,
    _REMOTE_SRC,
    _REPO_ROOT,
    _UV_PIP_INSTALL,
    _UV_SEED,
    ensure_github_no_proxy as _ensure_github_no_proxy,
    merge_trial_path as _merge_trial_path,
    pip_install_command as _pip_install_command,
    python_tag_supported as _python_tag_supported,
    rewrite_forwarded_env_value as _rewrite_forwarded_env_value,
    rewrite_loopback_host as _rewrite_loopback_host,
    spec_as_json as _spec_as_json,
    venv_tarball as _venv_tarball,
)

_PACKAGE_DIRS = (
    _REPO_ROOT / "packages" / "agent-protocol" / "py",
    _REPO_ROOT / "packages" / "agent-harness" / "py",
    _REPO_ROOT / "packages" / "agent-runtime" / "py",
    _REPO_ROOT / "packages" / "sidecar" / "py",
)
_VENV_PYTHON = f"{_REMOTE_SRC}/venv/bin/python"
_INSTRUCTION_REMOTE = "/tmp/steerable-instruction.md"
# The trial container installs only the workspace wheels — PyYAML is
# deliberately not a runtime dependency, so the spec crosses the boundary
# as JSON (a YAML subset the runtime loader parses with stdlib json).
_HARNESS_REMOTE = "/tmp/steerable-harness.json"
_REMOTE_VENV_TAR = "/tmp/steerable-venv.tgz"
# The agent packages require Python >= 3.10; task images stuck on older
# interpreters (qemu-alpine-ssh's Debian bullseye ships 3.9) get a pinned
# python-build-standalone instead of a cryptic pip failure. The desktop app
# bundles its own runtime for the same reason. Pinned for reproducibility;
# no CN mirror carries astral-sh/python-build-standalone, so proxied local
# runs may stall — GHA (the eval home) reaches GitHub directly.
_STANDALONE_PY_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    "20250818/cpython-3.11.13%2B20250818-x86_64-unknown-linux-gnu-install_only.tar.gz"
)
_STANDALONE_PY_HOME = "/opt/steerable-py311"
_STANDALONE_PY = f"{_STANDALONE_PY_HOME}/bin/python3.11"
_CREDENTIAL_KEYS = (
    "STEERABLE_API_KEY",
    "STEERABLE_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
)
_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class SteerableHarborAgent(BaseInstalledAgent):
    """Headless CoreLoop with in-process bash/file tools."""

    MODEL_CONNECTION = ModelConnectionSpec(
        api_key_envs=("STEERABLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
        base_url_envs=("STEERABLE_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"),
        passthrough=True,
    )

    @staticmethod
    @override
    def name() -> str:
        return "steerable"

    def __init__(self, *args, harness: str | None = None, **kwargs) -> None:
        # Harbor's base classes swallow unknown kwargs; capture the harness
        # dimension (W1.3.1) explicitly so `--agent-kwarg harness=...` takes
        # effect instead of vanishing.
        super().__init__(*args, **kwargs)
        self._harness_spec = harness

    @override
    def get_version_command(self) -> str | None:
        return f"{shlex.quote(_VENV_PYTHON)} -m steerable_sidecar.headless --version"

    @override
    def parse_version(self, stdout: str) -> str:
        lines = stdout.strip().splitlines()
        return lines[-1].strip() if lines else "unknown"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        pip_check = await environment.exec(
            command="python3 -m pip --version", user="root"
        )
        proxy_env = self._forwarded_env(_PROXY_KEYS)
        # Harbor scopes extra_env to agent setup/run only. TB hidden tests
        # download uv from GitHub inside the same container; without a
        # rewritten host proxy that step hangs (VerifierTimeoutError).
        if proxy_env:
            environment._persistent_env.update(proxy_env)
        if pip_check.return_code != 0:
            apt_env = {"DEBIAN_FRONTEND": "noninteractive", **proxy_env}
            await self._ensure_python_apt(environment, apt_env)
        await self.exec_as_root(
            environment, command=f"mkdir -p {shlex.quote(_REMOTE_SRC)}"
        )
        py_tag = await self._python_tag(environment)
        python_bin = "python3"
        if py_tag and not _python_tag_supported(py_tag):
            python_bin = await self._install_standalone_python(environment, proxy_env)
            py_tag = await self._python_tag(environment, python=python_bin)
        cached = _venv_tarball(py_tag) if py_tag else None
        restored = False
        if cached is not None and cached.is_file():
            restored = await self._restore_venv(environment, cached)
        if restored:
            # Cached venv is third-party wheels; overlay workspace sources so
            # later trials pick up headless/runtime fixes without a full pip.
            await self._overlay_source(environment, proxy_env)
        else:
            await self._pip_install_packages(environment, proxy_env, python=python_bin)
            if py_tag:
                await self._save_venv(environment, _venv_tarball(py_tag))
        await self._seed_uv(environment)
        environment._persistent_env["PATH"] = _merge_trial_path(
            environment._persistent_env.get("PATH", "")
        )
        agent_user = str(environment.default_user or "root")
        await self.exec_as_root(
            environment,
            command=(
                f"chown -R {shlex.quote(agent_user)}:{shlex.quote(agent_user)} "
                f"{shlex.quote(_REMOTE_SRC)}"
            ),
        )

    async def _python_tag(self, environment: BaseEnvironment, python: str = "python3") -> str:
        result = await environment.exec(
            command=(
                f"{shlex.quote(python)} -c 'import sys; print(\"%s%s\" % "
                "(sys.version_info.major, sys.version_info.minor))'"
            ),
            user="root",
        )
        if result.return_code != 0:
            return ""
        return (result.stdout or "").strip()

    async def _install_standalone_python(
        self, environment: BaseEnvironment, proxy_env: dict[str, str]
    ) -> str:
        """Fetch the pinned python-build-standalone into the environment.

        Fails loud: without it the agent cannot install at all, and a clear
        download error beats pip's ``requires a different Python`` wall.
        """
        result = await self.exec_as_root(
            environment,
            command=(
                f"curl -fsSL --retry 3 --max-time 600 "
                f"{shlex.quote(_STANDALONE_PY_URL)} -o /tmp/steerable-py.tgz "
                f"&& mkdir -p {shlex.quote(_STANDALONE_PY_HOME)} "
                f"&& tar -xzf /tmp/steerable-py.tgz --strip-components=1 "
                f"-C {shlex.quote(_STANDALONE_PY_HOME)} "
                f"&& rm /tmp/steerable-py.tgz "
                f"&& {shlex.quote(_STANDALONE_PY)} --version"
            ),
            env=proxy_env or None,
            timeout_sec=900,
        )
        if result.return_code != 0:
            raise RuntimeError(
                "environment python3 is below the agent's >=3.10 requirement "
                "and the standalone-python fallback failed to install: "
                f"{(result.stderr or result.stdout or '').strip()[-400:]}"
            )
        return _STANDALONE_PY

    async def _ensure_python_apt(
        self, environment: BaseEnvironment, apt_env: dict[str, str]
    ) -> None:
        """Install pip/venv after Ubuntu's boot apt releases the dpkg lock.

        Slim images have no ``fuser``. A Harbor timeout also leaves apt-get
        running, so a second install must wait on ``/proc/*/fd`` and only then
        kill a stuck lock holder.
        """
        try:
            await self.exec_as_root(
                environment,
                command=_APT_PYTHON_INSTALL,
                env=apt_env or None,
                timeout_sec=900,
            )
        except Exception:
            await self.exec_as_root(
                environment,
                command=_APT_PYTHON_INSTALL,
                env=apt_env or None,
                timeout_sec=900,
            )
        pip_check = await environment.exec(
            command="python3 -m pip --version", user="root"
        )
        if pip_check.return_code != 0:
            await self.ensure_system_dependencies(
                environment, ("python3", "python_pip")
            )

    async def _seed_uv(self, environment: BaseEnvironment) -> None:
        """Install uv from a PyPI mirror so TB ``test.sh`` need not hit GitHub.

        Clash (and even noproxy GitHub GETs from Docker Desktop) stall the
        official uv tarball around 2 MB, which becomes VerifierTimeoutError.
        Tsinghua's manylinux wheel completed in ~7s. Failure here is ignored
        so GHA can still rely on ``test.sh`` downloading uv itself.
        """
        try:
            await self.exec_as_root(
                environment,
                command=_UV_PIP_INSTALL,
                env=_NO_PROXY_ENV,
                timeout_sec=180,
            )
            await self.exec_as_root(environment, command=_UV_SEED)
        except Exception:
            return
        environment._persistent_env["PATH"] = _merge_trial_path(
            environment._persistent_env.get("PATH", "")
        )

    async def _restore_venv(
        self, environment: BaseEnvironment, tarball: Path
    ) -> bool:
        await environment.upload_file(tarball, _REMOTE_VENV_TAR)
        await self.exec_as_root(
            environment,
            command=(
                f"tar -C {shlex.quote(_REMOTE_SRC)} -xzf {shlex.quote(_REMOTE_VENV_TAR)}"
            ),
        )
        check = await environment.exec(
            command=f"{shlex.quote(_VENV_PYTHON)} -m steerable_sidecar.headless --version",
            user="root",
        )
        if check.return_code == 0:
            return True
        await self.exec_as_root(
            environment, command=f"rm -rf {shlex.quote(_REMOTE_SRC)}/venv"
        )
        return False

    async def _upload_packages(self, environment: BaseEnvironment) -> list[str]:
        remote_pkgs: list[str] = []
        for src in _PACKAGE_DIRS:
            dest = f"{_REMOTE_SRC}/{src.parent.name}"
            await self.exec_as_root(
                environment, command=f"mkdir -p {shlex.quote(dest)}"
            )
            await environment.upload_dir(src, dest)
            remote_pkgs.append(dest)
        return remote_pkgs

    async def _overlay_source(
        self, environment: BaseEnvironment, proxy_env: dict[str, str]
    ) -> None:
        remote_pkgs = await self._upload_packages(environment)
        await self._pip_install(
            environment,
            remote_pkgs,
            proxy_env,
            extra_args=("--no-deps", "--upgrade", "--force-reinstall"),
        )

    async def _pip_install_packages(
        self,
        environment: BaseEnvironment,
        proxy_env: dict[str, str],
        python: str = "python3",
    ) -> None:
        remote_pkgs = await self._upload_packages(environment)
        venv = f"{_REMOTE_SRC}/venv"
        venv_check = await environment.exec(
            command=f"{shlex.quote(python)} -m venv {shlex.quote(venv)}", user="root"
        )
        if venv_check.return_code != 0:
            await self.ensure_system_dependencies(environment, ("python_venv",))
            await self.exec_as_root(
                environment, command=f"{shlex.quote(python)} -m venv {shlex.quote(venv)}"
            )
        await self._pip_install(environment, remote_pkgs, proxy_env)

    async def _pip_install(
        self,
        environment: BaseEnvironment,
        remote_pkgs: list[str],
        proxy_env: dict[str, str],
        extra_args: tuple[str, ...] = (),
    ) -> None:
        await self.exec_as_root(
            environment,
            command=_pip_install_command(remote_pkgs, extra_args=extra_args),
            env=proxy_env or None,
            timeout_sec=600,
        )

    async def _save_venv(self, environment: BaseEnvironment, tarball: Path) -> None:
        try:
            tarball.parent.mkdir(parents=True, exist_ok=True)
            await self.exec_as_root(
                environment,
                command=(
                    f"tar -C {shlex.quote(_REMOTE_SRC)} -czf "
                    f"{shlex.quote(_REMOTE_VENV_TAR)} venv"
                ),
            )
            await environment.download_file(_REMOTE_VENV_TAR, tarball)
        except Exception:
            # Optional host cache; a snapshot failure must not fail the trial.
            return

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="steerable-instruction-") as tmp:
            local = Path(tmp) / "instruction.md"
            local.write_text(instruction, encoding="utf-8")
            await environment.upload_file(local, _INSTRUCTION_REMOTE)
        harness_flag = ""
        if self._harness_spec:
            # The spec is a host-side path; the container needs the file.
            # headless.py must understand --harness (W1.2.2) — until it does,
            # argparse rejects the flag and the trial errors loudly rather
            # than silently rerunning the default harness under a new label.
            await environment.upload_file(_spec_as_json(self._harness_spec), _HARNESS_REMOTE)
            harness_flag = f"--harness {shlex.quote(_HARNESS_REMOTE)} "
        provider = (self._parsed_model_provider or "openai").strip().lower()
        kind = "anthropic" if provider in {"anthropic", "claude"} else "openai_compat"
        env = self._forwarded_env((*_CREDENTIAL_KEYS, *_PROXY_KEYS))
        env["STEERABLE_PROVIDER"] = kind
        env["STEERABLE_MODEL"] = self._parsed_model_name or ""
        env["PYTHONUNBUFFERED"] = "1"
        # GLM-5.3-Flash thinking defaults to max and can sit silent for many
        # minutes before the first tool call. Harbor wants cheap/fast rounds.
        env.setdefault("STEERABLE_REASONING_EFFORT", "low")
        log = f"{self.environment_logs_dir.as_posix()}/headless.log"
        await self.exec_as_agent(
            environment,
            command=(
                f"{shlex.quote(_VENV_PYTHON)} -u -m steerable_sidecar.headless "
                f"--instruction-file {shlex.quote(_INSTRUCTION_REMOTE)} --cwd . "
                f"{harness_flag}"
                f"> {shlex.quote(log)} 2>&1"
            ),
            env=env,
        )

    def _forwarded_env(self, keys: tuple[str, ...]) -> dict[str, str]:
        env: dict[str, str] = {}
        proxy_keys = set(_PROXY_KEYS)
        for key in keys:
            value = self._get_env(key) or os.environ.get(key)
            if not value:
                continue
            if key in proxy_keys:
                value = _rewrite_forwarded_env_value(key, value)
            env[key] = value
        host_proxy = os.environ.get("STEERABLE_HOST_PROXY")
        if host_proxy and not (
            env.keys()
            & {
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "http_proxy",
                "https_proxy",
                "ALL_PROXY",
                "all_proxy",
            }
        ):
            rewritten = _rewrite_loopback_host(host_proxy)
            env["HTTP_PROXY"] = rewritten
            env["HTTPS_PROXY"] = rewritten
            env["http_proxy"] = rewritten
            env["https_proxy"] = rewritten
        if env.keys() & {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        }:
            _ensure_github_no_proxy(env)
        return env
