"""Harbor BaseInstalledAgent for the Steerable/DeepPath product loop.

Installs workspace Python packages into a venv in the trial container and
runs ``python -m steerable_sidecar.headless`` against the task instruction.
"""

from __future__ import annotations

import os
import shlex
import shutil
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
    _ENSURE_PYTHON_310,
    _NO_PROXY_ENV,
    _REMOTE_SRC,
    _REPO_ROOT,
    _UV_PIP_INSTALL,
    _UV_SEED,
    ensure_github_no_proxy as _ensure_github_no_proxy,
    merge_trial_path as _merge_trial_path,
    musl_uv_binary as _musl_uv_binary,
    linux_cpython_tarball as _linux_cpython_tarball,
    pip_install_command as _pip_install_command,
    rewrite_forwarded_env_value as _rewrite_forwarded_env_value,
    rewrite_loopback_host as _rewrite_loopback_host,
    trial_python_ok as _trial_python_ok,
    trial_python_tag as _trial_python_tag,
    trial_python_venv as _trial_python_venv,
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
        await self._inject_host_uv(environment)
        await self._inject_host_python(environment)
        await self._ensure_python_310(environment, proxy_env)
        # The 3.10+ interpreter is /usr/local/bin/python3. Apply that PATH
        # before `python3 -m venv`, not only after install().
        environment._persistent_env["PATH"] = _merge_trial_path(
            environment._persistent_env.get("PATH", "")
        )
        await self.exec_as_root(
            environment, command=f"mkdir -p {shlex.quote(_REMOTE_SRC)}"
        )
        py_tag = await self._python_tag(environment)
        if py_tag and int(py_tag) < 310:
            raise RuntimeError(
                f"trial python cp{py_tag} is still <3.10 before venv"
            )
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

    async def _python_tag(self, environment: BaseEnvironment) -> str:
        result = await environment.exec(
            command=_trial_python_tag(),
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

    async def _inject_host_uv(self, environment: BaseEnvironment) -> None:
        """Put a Linux musl ``uv`` in the trial before Python upgrade.

        Debian 11 / Alpine 3.9 cannot pip-install a recent uv. A musl-static
        binary from GitHub (downloaded on the Harbor host) runs in those
        images; copying a macOS ``uv`` does not. Fall back to ``which uv``.
        """
        src = _musl_uv_binary(fetch=True)
        if src is None:
            host = shutil.which("uv")
            if not host:
                return
            src = Path(host)
        try:
            await environment.upload_file(src, "/tmp/steerable-host-uv")
            await self.exec_as_root(
                environment,
                command=(
                    "cp /tmp/steerable-host-uv /usr/local/bin/uv && "
                    "chmod 0755 /usr/local/bin/uv && "
                    "/usr/local/bin/uv --version"
                ),
                timeout_sec=30,
            )
        except Exception:
            return

    async def _inject_host_python(self, environment: BaseEnvironment) -> None:
        """Install a host-packed Linux 3.12 before ``uv python install`` in-trial.

        qemu-alpine-ssh / qemu-startup are Debian 11 (3.9.2). GitHub GETs of
        python-build-standalone from inside those images fail; the GHA host
        already has that tarball from setup-harbor.
        """
        src = _linux_cpython_tarball(fetch=True)
        if src is None:
            return
        try:
            await environment.upload_file(src, "/tmp/steerable-cpython.tgz")
            await self.exec_as_root(
                environment,
                command=(
                    "mkdir -p /opt/steerable-python && "
                    "tar -C /opt/steerable-python -xzf /tmp/steerable-cpython.tgz && "
                    "for b in /opt/steerable-python/bin/python3 "
                    "/opt/steerable-python/bin/python3.12 "
                    "/opt/steerable-python/bin/python; do "
                    '[ -x "$b" ] && ln -sf "$b" /usr/local/bin/python3 && break; '
                    "done && "
                    "/usr/local/bin/python3 -c "
                    "'import ssl, zlib, sys; raise SystemExit("
                    "0 if sys.version_info >= (3, 10) else 1)'"
                ),
                timeout_sec=120,
            )
        except Exception:
            try:
                await self.exec_as_root(
                    environment,
                    command="rm -f /usr/local/bin/python3",
                    timeout_sec=15,
                )
            except Exception:
                return
            return

    async def _ensure_python_310(
        self, environment: BaseEnvironment, proxy_env: dict[str, str]
    ) -> None:
        """Raise the trial interpreter to >=3.10 before creating the agent venv."""
        check = await environment.exec(
            command=_trial_python_ok(),
            user="root",
        )
        if check.return_code == 0:
            return
        await self.exec_as_root(
            environment,
            command=_ENSURE_PYTHON_310,
            env=proxy_env or None,
            timeout_sec=900,
        )
        environment._persistent_env["PATH"] = _merge_trial_path(
            environment._persistent_env.get("PATH", "")
        )
        again = await environment.exec(
            command=_trial_python_ok(),
            user="root",
        )
        if again.return_code != 0:
            raise RuntimeError(
                "trial python is still <3.10 after _ENSURE_PYTHON_310 "
                f"(stdout={again.stdout!r} stderr={again.stderr!r})"
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
            await self.exec_as_root(
                environment, command=_UV_SEED, timeout_sec=600
            )
        except Exception:
            return
        environment._persistent_env["PATH"] = _merge_trial_path(
            environment._persistent_env.get("PATH", "")
        )
        environment._persistent_env["UV_PYTHON_INSTALL_DIR"] = "/opt/uv-python"

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
        venv_cmd = _trial_python_venv(venv)
        venv_check = await environment.exec(command=venv_cmd, user="root")
        if venv_check.return_code != 0:
            await self.ensure_system_dependencies(environment, ("python_venv",))
            await self.exec_as_root(environment, command=venv_cmd)
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
        # Z.AI GLM-5.3 coding default is reasoning_effort=max (high is weaker).
        # Claude Code TB 84.3 used temperature=1.0, max_new_tokens=65536, 6h.
        env.setdefault("STEERABLE_REASONING_EFFORT", "max")
        env.setdefault("STEERABLE_TEMPERATURE", "1.0")
        env.setdefault("STEERABLE_MAX_TOKENS", "65536")
        # Catalog/failed-prev Harbor ×12 on 900s tasks is 180 min; wrap at
        # 150 min so keep-tools wrap-up has ~30 min before the kill
        # (regex-chess drafted /app/re.json in chat, then wrap-up had ~10 min).
        env.setdefault("STEERABLE_SOFT_TIMEOUT_MS", "9000000")
        # High GLM thinking can emit no SSE bytes for many minutes
        # (regex-chess thought ~48 min). Idle read must cover the wrap.
        env.setdefault("STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", "10200")
        # OpenRouter 429s during 16-shard GHA; default 3×200ms dies immediately.
        env.setdefault("STEERABLE_RETRY_MAX_ATTEMPTS", "12")
        env.setdefault("STEERABLE_RETRY_BASE_DELAY_MS", "2000")
        env.setdefault("STEERABLE_RETRY_MAX_DELAY_MS", "120000")
        # OpenRouter cheapest route is Relace, not Z.ai. GLM's 83+ TB score
        # is the official endpoint; pin so catalog quality matches.
        # Do not set require_parameters: Harbor streams with
        # stream_options.include_usage, which no GLM endpoint advertises —
        # require_parameters then 404s "No endpoints found".
        env.setdefault("STEERABLE_OPENROUTER_PROVIDER", "z-ai")
        env.setdefault("STEERABLE_OPENROUTER_ALLOW_FALLBACKS", "0")
        env.setdefault("STEERABLE_OPENROUTER_REQUIRE_PARAMETERS", "0")
        env.setdefault(
            "STEERABLE_HTTP_REFERER",
            "https://github.com/pathlyapp/steerable-framework",
        )
        env.setdefault("STEERABLE_HTTP_TITLE", "Steerable Harbor TB")
        log = f"{self.environment_logs_dir.as_posix()}/headless.log"
        await self.exec_as_agent(
            environment,
            command=(
                f"{shlex.quote(_VENV_PYTHON)} -u -m steerable_sidecar.headless "
                f"--instruction-file {shlex.quote(_INSTRUCTION_REMOTE)} --cwd . "
                f"--max-rounds 250 "
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
