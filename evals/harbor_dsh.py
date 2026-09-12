"""Harbor installed agent wrapping DeepSeek Harness headless CLI.

Harbor 0.22.0 has no first-party DSH adapter (``dsh-minimal`` is still an
open PR). Stock suite.yaml used to skip this leg. This wrapper installs
``@deepseek-ai/dsh`` into the trial container and points it at the same
OpenAI-compatible gateway the product agent uses, via llm-pi-ai rather
than ``llm-deepseek`` (official api.deepseek.com).

DSH is not on Monday LIVE_AGENTS: that job must not put the gateway on
OPENAI_*, or stock Codex would send the key to api.openai.com.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path, PurePosixPath

try:
    from typing import override
except ImportError:  # Python < 3.12 — evals unit tests still collect.

    def override(f):  # type: ignore[misc]
        return f

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.model_connection import ModelConnectionSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from evals.harbor_helpers import is_zai_glm

_MAX_TOKENS = 65_536
_GLM_OR_DEEPSEEK_WINDOW = 1_048_576
_QWEN38_WINDOW = 262_144
_REMOTE_DSH_HOME = PurePosixPath("/tmp/dsh-home")
_INSTRUCTION_REMOTE = "/tmp/dsh-instruction.md"
_SETTINGS_REMOTE = "/tmp/dsh-home/settings.yaml"
_PROVIDER = "gateway"
_INSTALL_CHECK = (
    "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
    "command -v dsh >/dev/null 2>&1"
)
_KEY_ENVS = ("OPENROUTER_API_KEY", "STEERABLE_API_KEY", "OPENAI_API_KEY")
_URL_ENVS = ("OPENROUTER_BASE_URL", "STEERABLE_BASE_URL", "OPENAI_BASE_URL")
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


def gateway_dsh_model(model_name: str) -> str:
    """Strip Harbor's ``openai/`` prefix. Nested OpenRouter ids stay intact."""
    return model_name.removeprefix("openai/").removeprefix("openrouter/")


def context_window_for(model_id: str) -> int:
    """DSH ``contextWindow`` for a gateway model id. Same table as pi-glm."""
    lowered = model_id.lower()
    if "qwen3.8" in lowered or "qwen3-8-27b" in lowered:
        return _QWEN38_WINDOW
    return _GLM_OR_DEEPSEEK_WINDOW


def openrouter_routing(model_id: str) -> dict[str, object]:
    """Pin the official OpenRouter provider when one is configured."""
    pinned = os.environ.get("STEERABLE_OPENROUTER_PROVIDER", "").strip()
    if not pinned and is_zai_glm(model_id):
        pinned = "z-ai"
    order = [part.strip() for part in pinned.split(",") if part.strip()]
    if not order:
        return {"allow_fallbacks": False}
    return {"order": order, "allow_fallbacks": False}


def resolve_api_key_env(environ: dict[str, str] | None = None) -> str:
    """Name of the env var DSH should read for the gateway key."""
    env = os.environ if environ is None else environ
    for name in _KEY_ENVS:
        if (env.get(name) or "").strip():
            return name
    return "OPENROUTER_API_KEY"


def resolve_base_url(environ: dict[str, str] | None = None) -> str:
    """Gateway URL for llm-pi-ai ``baseURL``."""
    env = os.environ if environ is None else environ
    for name in _URL_ENVS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def _yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def dsh_settings_yaml(
    *,
    base_url: str,
    model_id: str,
    api_key_env: str,
    effort: str,
    provider_order: list[str],
    context_window: int,
    max_tokens: int = _MAX_TOKENS,
) -> str:
    """User-layer ``settings.yaml`` for a headless eval session.

    ``permission.defaultPreset: danger-full-access`` is required: the
    factory default is ``workspace-write`` + ``ask``, which hangs a
    Harbor trial. ``DSH_PERMISSION_MODE`` is also set at process start.
    """
    routing_lines = ["        openRouterRouting:"]
    if provider_order:
        routing_lines.append("          order:")
        routing_lines.extend(
            f"            - {_yaml_str(item)}" for item in provider_order
        )
    routing_lines.append("          allow_fallbacks: false")
    effort_line = f"  reasoningEffort: {_yaml_str(effort)}\n" if effort else ""
    return (
        f"agent-default-model:\n"
        f"  provider: {_yaml_str(_PROVIDER)}\n"
        f"  model: {_yaml_str(model_id)}\n"
        f"{effort_line}"
        f"permission:\n"
        f"  defaultPreset: danger-full-access\n"
        f"llm-pi-ai:\n"
        f"  providers:\n"
        f"    {_PROVIDER}:\n"
        f"      apiKeyEnv: {_yaml_str(api_key_env)}\n"
        f"      api: openai-completions\n"
        f"      baseURL: {_yaml_str(base_url)}\n"
        f"      compat:\n"
        f"        supportsDeveloperRole: false\n"
        f"        maxTokensField: max_tokens\n"
        f"        thinkingFormat: openai\n"
        f"        supportsReasoningEffort: true\n"
        f"{chr(10).join(routing_lines)}\n"
        f"      models:\n"
        f"        - id: {_yaml_str(model_id)}\n"
        f"          reasoning: true\n"
        f"          contextWindow: {context_window}\n"
        f"          maxTokens: {max_tokens}\n"
    )


class DshHarborAgent(BaseInstalledAgent):
    """DeepSeek Harness headless CLI on the product gateway."""

    MODEL_CONNECTION = ModelConnectionSpec(
        api_key_envs=_KEY_ENVS,
        base_url_envs=_URL_ENVS,
        passthrough=True,
    )

    @staticmethod
    @override
    def name() -> str:
        return "dsh"

    def __init__(self, *args, version: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._version = version

    @override
    def get_version_command(self) -> str | None:
        return (
            "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; dsh --version"
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        check = await environment.exec(command=_INSTALL_CHECK)
        if check.return_code == 0:
            return
        await self.ensure_system_dependencies(
            environment, ("curl", "bash", "nodejs", "npm")
        )
        version_spec = f"@{self._version}" if self._version else "@0.1.5-rc.1"
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "if ldd --version 2>&1 | grep -qi musl || [ -f /etc/alpine-release ]; then"
                f" npm install -g @deepseek-ai/dsh{version_spec};"
                " else"
                " curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash &&"
                ' export NVM_DIR="$HOME/.nvm" &&'
                ' \\. "$NVM_DIR/nvm.sh" || true &&'
                " command -v nvm &>/dev/null || { echo 'Error: NVM failed to load' >&2; exit 1; } &&"
                " nvm install 22 && nvm alias default 22 && npm -v &&"
                f" npm install -g @deepseek-ai/dsh{version_spec};"
                " fi && "
                "dsh --version"
            ),
            env={"NVM_NODEJS_ORG_MIRROR": "https://nodejs.org/dist"},
        )
        await self.exec_as_root(
            environment,
            command=(
                "for bin in node dsh; do"
                ' BIN_PATH="$(which "$bin" 2>/dev/null || true)";'
                ' if [ -n "$BIN_PATH" ] && [ "$BIN_PATH" != "/usr/local/bin/$bin" ]; then'
                ' ln -sf "$BIN_PATH" "/usr/local/bin/$bin";'
                " fi;"
                " done"
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
        if not self.model_name:
            raise ValueError("Model name is required")
        base_url = (
            self.model_connection.configured_base_url or resolve_base_url()
        ).rstrip("/")
        if not base_url:
            raise ValueError(
                "dsh needs OPENROUTER_BASE_URL (or STEERABLE_BASE_URL / "
                "OPENAI_BASE_URL) set to the product gateway. Without it the "
                "headless profile would try deepseek-official."
            )
        model_id = gateway_dsh_model(self.model_name)
        api_key_env = resolve_api_key_env()
        effort = os.environ.get("STEERABLE_REASONING_EFFORT", "").strip()
        routing = openrouter_routing(model_id)
        order = [str(item) for item in routing.get("order", [])]  # type: ignore[arg-type]
        settings = dsh_settings_yaml(
            base_url=base_url,
            model_id=model_id,
            api_key_env=api_key_env,
            effort=effort,
            provider_order=order,
            context_window=context_window_for(model_id),
        )
        with tempfile.TemporaryDirectory(prefix="dsh-eval-") as tmp:
            tmp_path = Path(tmp)
            instruction_file = tmp_path / "instruction.md"
            settings_file = tmp_path / "settings.yaml"
            instruction_file.write_text(instruction, encoding="utf-8")
            settings_file.write_text(settings, encoding="utf-8")
            await environment.upload_file(instruction_file, _INSTRUCTION_REMOTE)
            await self.exec_as_agent(
                environment,
                command=f"mkdir -p {shlex.quote(_REMOTE_DSH_HOME.as_posix())}",
            )
            await environment.upload_file(settings_file, _SETTINGS_REMOTE)

        env: dict[str, str] = {
            "DSH_HOME": _REMOTE_DSH_HOME.as_posix(),
            "DSH_PERMISSION_MODE": "danger-full-access",
            "PYTHONUNBUFFERED": "1",
        }
        access = self.model_connection
        if access.api_key:
            env[api_key_env] = access.api_key
        for key in _PROXY_KEYS:
            value = os.environ.get(key)
            if value:
                env[key] = value
        log = f"{self.environment_logs_dir.as_posix()}/dsh.txt"
        await self.exec_as_agent(
            environment,
            command=(
                "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                f"dsh --profile headless "
                f'"$(cat {shlex.quote(_INSTRUCTION_REMOTE)})" '
                f"> {shlex.quote(log)} 2>&1"
            ),
            env=env,
        )
