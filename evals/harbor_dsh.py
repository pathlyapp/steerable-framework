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
_PIN_PROXY_REMOTE = "/tmp/or-pin-proxy.py"
_PIN_PROXY_PORT = 8787
# Catalog route. A made-up "gateway" id plus compat.openRouterRouting is
# INVALID_CONFIG: dsh-llm-pi-ai withholds that field (cheap-12 34684499625).
_PROVIDER = "openrouter"
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


def thinking_format_for(model_id: str) -> str:
    """Pi-ai thinking wire format. Same split as pi-glm."""
    return "qwen" if "qwen" in model_id.lower() else "openai"


def pin_proxy_listen_url(port: int = _PIN_PROXY_PORT) -> str:
    """Loopback OpenAI-compat base URL the pin proxy listens on."""
    return f"http://127.0.0.1:{port}/v1"


def pin_proxy_target(upstream: str, request_path: str) -> str:
    """Map loopback ``/v1/...`` onto the real OpenRouter ``/api/v1/...`` URL.

    ``urljoin('https://openrouter.ai/api/v1/', '/v1/chat/completions')``
    becomes ``https://openrouter.ai/v1/chat/completions`` and 404s. cheap-12
    34837175358 returned that as HTTP 502 from the pin proxy.
    """
    path, _, query = request_path.partition("?")
    if path.startswith("/v1/"):
        suffix = path[3:]
    elif path == "/v1":
        suffix = "/"
    else:
        suffix = path if path.startswith("/") else f"/{path}"
    target = upstream.rstrip("/") + suffix
    if query:
        target = f"{target}?{query}"
    return target


PIN_PROXY_SOURCE = r'''#!/usr/bin/env python3
"""Forward OpenAI-compat POSTs and inject OpenRouter provider pin.

dsh-llm-pi-ai withholds compat.openRouterRouting (discussion 5349). The
Harbor adapter therefore cannot pin z-ai/alibaba in settings.yaml; this
process is the pin.
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, Request, build_opener

parser = argparse.ArgumentParser()
parser.add_argument("--upstream", required=True)
parser.add_argument("--order", action="append", default=[])
parser.add_argument("--listen", default="127.0.0.1:8787")
args = parser.parse_args()
upstream = args.upstream.rstrip("/")
order = [item for item in args.order if item]
if not order:
    sys.exit("pin proxy needs --order")
routing = {"order": order, "allow_fallbacks": False}
host, port_s = args.listen.rsplit(":", 1)
opener = build_opener(ProxyHandler({}))


def pin_proxy_target(upstream, request_path):
    path, _, query = request_path.partition("?")
    if path.startswith("/v1/"):
        suffix = path[3:]
    elif path == "/v1":
        suffix = "/"
    else:
        suffix = path if path.startswith("/") else "/" + path
    target = upstream.rstrip("/") + suffix
    if query:
        target = target + "?" + query
    return target


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        sys.stderr.write("%s\n" % (fmt % a))

    def do_GET(self):
        self._forward(b"", "GET")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and "model" in payload:
            existing = payload.get("provider")
            if isinstance(existing, dict):
                existing.setdefault("order", order)
                existing["allow_fallbacks"] = False
            else:
                payload["provider"] = dict(routing)
            body = json.dumps(payload).encode("utf-8")
        self._forward(body, "POST")

    def _forward(self, body: bytes, method: str) -> None:
        target = pin_proxy_target(upstream, self.path)
        req = Request(target, data=body or None, method=method)
        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered in ("host", "content-length"):
                continue
            req.add_header(key, value)
        if body:
            req.add_header("Content-Length", str(len(body)))
        try:
            with opener.open(req, timeout=600) as resp:
                self.send_response(resp.status)
                for key, value in resp.headers.items():
                    if key.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(key, value)
                self.end_headers()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as exc:
            sys.stderr.write("pin-proxy upstream error: %s %s\n" % (target, exc))
            self.send_error(502, str(exc)[:200])


http_host = host
port = int(port_s)
sys.stderr.write("pin-proxy listen %s:%s -> %s\n" % (http_host, port, upstream))
ThreadingHTTPServer((http_host, port), Handler).serve_forever()
'''


def dsh_settings_yaml(
    *,
    base_url: str,
    model_id: str,
    api_key_env: str,
    effort: str,
    context_window: int,
    max_tokens: int = _MAX_TOKENS,
    thinking_format: str | None = None,
) -> str:
    """User-layer ``settings.yaml`` for a headless eval session.

    ``permission.defaultPreset: danger-full-access`` is required: the
    factory default is ``workspace-write`` + ``ask``, which hangs a
    Harbor trial. ``DSH_PERMISSION_MODE`` is also set at process start.

    Do not put ``openRouterRouting`` under ``compat``: dsh-llm-pi-ai
    withholds it and refuses the whole document (INVALID_CONFIG). The
    OpenRouter pin is the loopback proxy in ``PIN_PROXY_SOURCE``.
    """
    effort_line = f"  reasoningEffort: {_yaml_str(effort)}\n" if effort else ""
    fmt = thinking_format or thinking_format_for(model_id)
    effort_block = ""
    if effort:
        effort_block = (
            f"          reasoningEfforts:\n"
            f"            {_yaml_str('off')}:\n"
            f"            {effort}: {_yaml_str(effort)}\n"
        )
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
        f"        thinkingFormat: {_yaml_str(fmt)}\n"
        f"        supportsReasoningEffort: true\n"
        f"      models:\n"
        f"        - id: {_yaml_str(model_id)}\n"
        f"          contextWindow: {context_window}\n"
        f"          maxTokens: {max_tokens}\n"
        f"{effort_block}"
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
        dsh_base = base_url
        with tempfile.TemporaryDirectory(prefix="dsh-eval-") as tmp:
            tmp_path = Path(tmp)
            instruction_file = tmp_path / "instruction.md"
            settings_file = tmp_path / "settings.yaml"
            instruction_file.write_text(instruction, encoding="utf-8")
            await environment.upload_file(instruction_file, _INSTRUCTION_REMOTE)
            await self.exec_as_agent(
                environment,
                command=f"mkdir -p {shlex.quote(_REMOTE_DSH_HOME.as_posix())}",
            )
            if order:
                proxy_file = tmp_path / "or-pin-proxy.py"
                proxy_file.write_text(PIN_PROXY_SOURCE, encoding="utf-8")
                await environment.upload_file(proxy_file, _PIN_PROXY_REMOTE)
                dsh_base = pin_proxy_listen_url()
            settings_file.write_text(
                dsh_settings_yaml(
                    base_url=dsh_base,
                    model_id=model_id,
                    api_key_env=api_key_env,
                    effort=effort,
                    context_window=context_window_for(model_id),
                ),
                encoding="utf-8",
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
        if order:
            env["NO_PROXY"] = ",".join(
                part
                for part in (
                    env.get("NO_PROXY"),
                    "127.0.0.1",
                    "localhost",
                )
                if part
            )
        log = f"{self.environment_logs_dir.as_posix()}/dsh.txt"
        start_pin = ""
        if order:
            order_flags = " ".join(
                f"--order {shlex.quote(item)}" for item in order
            )
            start_pin = (
                "PY3=$(command -v python3); "
                'if [ -z "$PY3" ]; then echo python3 missing >&2; exit 1; fi; '
                "nohup \"$PY3\" -u "
                f"{shlex.quote(_PIN_PROXY_REMOTE)} "
                f"--upstream {shlex.quote(base_url)} "
                f"{order_flags} "
                f"--listen 127.0.0.1:{_PIN_PROXY_PORT} "
                ">/tmp/or-pin-proxy.log 2>&1 & "
                "echo $! >/tmp/or-pin-proxy.pid; "
                "ok=0; "
                "for i in $(seq 1 100); do "
                f"\"$PY3\" -c 'import socket; socket.create_connection((\"127.0.0.1\", {_PIN_PROXY_PORT}), 1).close()' "
                "&& ok=1 && break; sleep 0.1; done; "
                'if [ "$ok" != 1 ]; then '
                "echo pin-proxy did not listen PY3=$PY3 >&2; "
                "cat /tmp/or-pin-proxy.log >&2; "
                "exit 1; "
                "fi; "
            )
        await self.exec_as_agent(
            environment,
            command=(
                f"{start_pin}"
                "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                f"dsh --profile headless "
                f'"$(cat {shlex.quote(_INSTRUCTION_REMOTE)})" '
                f"> {shlex.quote(log)} 2>&1"
            ),
            env=env,
        )
