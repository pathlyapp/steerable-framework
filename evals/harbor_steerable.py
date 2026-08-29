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
    pip_install_command as _pip_install_command,
    rewrite_forwarded_env_value as _rewrite_forwarded_env_value,
    rewrite_loopback_host as _rewrite_loopback_host,
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
_REMOTE_VENV_TAR = "/tmp/steerable-venv.tgz"
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
        cached = _venv_tarball(py_tag) if py_tag else None
        restored = False
        if cached is not None and cached.is_file():
            restored = await self._restore_venv(environment, cached)
        if restored:
            # Cached venv is third-party wheels; overlay workspace sources so
            # later trials pick up headless/runtime fixes without a full pip.
            await self._overlay_source(environment, proxy_env)
        else:
            await self._pip_install_packages(environment, proxy_env)
            if py_tag:
                await self._save_venv(environment, _venv_tarball(py_tag))
        await self._seed_uv(environment)
        agent_user = str(environment.default_user or "root")
        await self.exec_as_root(
            environment,
            command=(
                f"chown -R {shlex.quote(agent_user)}:{shlex.quote(agent_user)} "
                f"{shlex.quote(_REMOTE_SRC)}"
            ),
        )

    async def _python_tag(self, environment: BaseEnvironment) -> str:
        result = await environment.exec(
            command=(
                "python3 -c 'import sys; print(\"%s%s\" % "
                "(sys.version_info.major, sys.version_info.minor))'"
            ),
            user="root",
        )
        if result.return_code != 0:
            return ""
        return (result.stdout or "").strip()

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
        path = environment._persistent_env.get("PATH", "")
        prefix = "/root/.local/bin"
        if prefix not in path.split(":"):
            environment._persistent_env["PATH"] = (
                f"{prefix}:{path}" if path else f"{prefix}:/usr/local/bin:/usr/bin:/bin"
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
        self, environment: BaseEnvironment, proxy_env: dict[str, str]
    ) -> None:
        remote_pkgs = await self._upload_packages(environment)
        venv = f"{_REMOTE_SRC}/venv"
        venv_check = await environment.exec(
            command=f"python3 -m venv {shlex.quote(venv)}", user="root"
        )
        if venv_check.return_code != 0:
            await self.ensure_system_dependencies(environment, ("python_venv",))
            await self.exec_as_root(
                environment, command=f"python3 -m venv {shlex.quote(venv)}"
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
