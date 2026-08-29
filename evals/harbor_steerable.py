"""Harbor BaseInstalledAgent for the Steerable/DeepPath product loop.

Installs workspace Python packages into a venv in the trial container and
runs ``python -m steerable_sidecar.headless`` against the task instruction.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path
from typing import override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.model_connection import ModelConnectionSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_DIRS = (
    _REPO_ROOT / "packages" / "agent-protocol" / "py",
    _REPO_ROOT / "packages" / "agent-harness" / "py",
    _REPO_ROOT / "packages" / "agent-runtime" / "py",
    _REPO_ROOT / "packages" / "sidecar" / "py",
)
_REMOTE_SRC = "/installed-agent/steerable"
_VENV_PYTHON = f"{_REMOTE_SRC}/venv/bin/python"
_INSTRUCTION_REMOTE = "/tmp/steerable-instruction.md"
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
        if pip_check.return_code != 0:
            apt_env = {"DEBIAN_FRONTEND": "noninteractive", **proxy_env}
            try:
                await self.exec_as_root(
                    environment,
                    command="apt-get update && apt-get install -y python3 python3-pip python3-venv",
                    env=apt_env or None,
                    timeout_sec=300,
                )
            except Exception:
                await self.ensure_system_dependencies(
                    environment, ("python3", "python_pip")
                )
        await self.exec_as_root(
            environment, command=f"mkdir -p {shlex.quote(_REMOTE_SRC)}"
        )
        remote_pkgs: list[str] = []
        for src in _PACKAGE_DIRS:
            dest = f"{_REMOTE_SRC}/{src.parent.name}"
            await self.exec_as_root(
                environment, command=f"mkdir -p {shlex.quote(dest)}"
            )
            await environment.upload_dir(src, dest)
            remote_pkgs.append(dest)
        venv = f"{_REMOTE_SRC}/venv"
        venv_check = await environment.exec(
            command=f"python3 -m venv {shlex.quote(venv)}", user="root"
        )
        if venv_check.return_code != 0:
            await self.ensure_system_dependencies(environment, ("python_venv",))
            await self.exec_as_root(
                environment, command=f"python3 -m venv {shlex.quote(venv)}"
            )
        quoted = " ".join(shlex.quote(p) for p in remote_pkgs)
        pip = f"{venv}/bin/pip"
        await self.exec_as_root(
            environment,
            command=f"{shlex.quote(pip)} install --quiet {quoted}",
            env=proxy_env or None,
            timeout_sec=600,
        )
        agent_user = str(environment.default_user or "root")
        await self.exec_as_root(
            environment,
            command=(
                f"chown -R {shlex.quote(agent_user)}:{shlex.quote(agent_user)} "
                f"{shlex.quote(_REMOTE_SRC)}"
            ),
        )

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
        await self.exec_as_agent(
            environment,
            command=(
                f"{shlex.quote(_VENV_PYTHON)} -m steerable_sidecar.headless "
                f"--instruction-file {shlex.quote(_INSTRUCTION_REMOTE)} --cwd ."
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
                value = _rewrite_loopback_host(value)
            env[key] = value
        return env


def _rewrite_loopback_host(value: str) -> str:
    """Docker Desktop: host Clash on 127.0.0.1 is not the container loopback."""
    return value.replace("127.0.0.1", "host.docker.internal").replace(
        "localhost", "host.docker.internal"
    )
