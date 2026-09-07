"""``run_code`` — model program that calls tools in one CoreLoop round.

The model-visible tool takes ``{code, description}``. The program runs in a
**child** process under the same layer-2 backend as bash (Seatbelt / bwrap /
Landlock). The sidecar never ``exec``s model Python in the key-holding
process. Nested ``tools.call`` frames travel JSON-over-stdio and go through
the live ToolExecutor (approval + sandbox + host reverse channel).

Default off: set ``STEERABLE_RUN_CODE=1`` (or a harness flag) to register.
No backend → ``sandbox_unavailable``, same as P0 layer-2.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime.tools import ToolRouter

from .sandbox import select_exec_backend

if TYPE_CHECKING:
    from steerable_agent_runtime.loop import LoopContext, ToolExecutor

__all__ = [
    "RunCodeBoundExecutor",
    "register_run_code",
    "run_code_enabled",
    "run_code_tool_descriptor",
]

_ENV = "STEERABLE_RUN_CODE"
_TIMEOUT_ENV = "STEERABLE_RUN_CODE_TIMEOUT_MS"
_CONFINED_ENV = "STEERABLE_SIDECAR_CONFINED"
_MAX_CALLS = 32
_MAX_SOURCE = 100_000
_DEFAULT_TIMEOUT_MS = 60_000

#: The child interpreter inherits only these variables from the sidecar's
#: environment. An allowlist — not a denylist — so a credential introduced
#: tomorrow (a new ``*_API_KEY`` / ``*_TOKEN``) is excluded by default. The
#: sidecar itself may hold ``STEERABLE_API_KEY`` and friends; without this
#: scrub the model-written child could read them via ``os.environ`` (the
#: import guard blocks ``import os`` but not the ``__subclasses__`` route).
_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "PYTHONPATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
)


def _child_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """The scrubbed environment the ``run_code`` child runs with."""
    child = {
        key: value
        for key, value in environ.items()
        if key in _CHILD_ENV_ALLOWLIST or key.startswith("LC_")
    }
    # The driver resolves the relay script relative to its own file; it must
    # not re-read the parent's bytecode cache or write one into a confined
    # filesystem.
    child["PYTHONDONTWRITEBYTECODE"] = "1"
    return child

_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": (
                "Body of a Python function (indentation as the body). "
                "`return` is the tool result. Call other tools with "
                "`tools.call(\"bash\", command=\"ls\")` or "
                "`tools.bash(command=\"ls\")`. Nested run_code is refused. "
                "`os` / `subprocess` / `socket` cannot be imported."
            ),
        },
        "description": {
            "type": "string",
            "description": "Short summary of what the program does.",
        },
    },
    "required": ["code", "description"],
    "additionalProperties": False,
}

_DESCRIPTION = (
    "Run a short Python program that can call other tools in this "
    "turn (tools.call / tools.<name>). Use it to chain several tool "
    "calls without extra model rounds. Native tools remain available."
)


def run_code_tool_descriptor() -> dict[str, Any]:
    """OpenAI tool schema to append to the model's tools list.

    The sidecar registers ``run_code`` on its router (so the RPC fallback can
    dispatch it) but the model only sees it when the descriptor is appended to
    the ``tools`` array — mirroring how subagent/skills advertise themselves.
    """
    return {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": _DESCRIPTION,
            "parameters": _SCHEMA,
        },
    }


class _Dispatch:
    __slots__ = ("executor", "ctx")

    def __init__(self, executor: ToolExecutor, ctx: LoopContext) -> None:
        self.executor = executor
        self.ctx = ctx


_dispatch: ContextVar[_Dispatch | None] = ContextVar(
    "run_code_dispatch", default=None
)
_router_for_rpc: ContextVar[ToolRouter | None] = ContextVar(
    "run_code_router", default=None
)


class RunCodeBoundExecutor:
    """Bind the live executor so nested ``run_code`` calls reuse it.

    ``run_code`` itself is answered locally from the sidecar's router — under
    ``toolsViaHost`` the inner executor forwards every call to the host, which
    has no ``run_code`` (it is a sidecar tool), so without the interception the
    desktop path fails with ``Unknown tool: run_code``. Nested calls inside the
    program still go through ``self._inner`` (the host) via ``_dispatch``.
    """

    def __init__(self, inner: ToolExecutor, router: ToolRouter | None = None) -> None:
        self._inner = inner
        self._router = router

    def concurrency_safe(self, call: ToolCall) -> bool:
        check = getattr(self._inner, "concurrency_safe", None)
        return bool(check is not None and check(call))

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        token = _dispatch.set(_Dispatch(self._inner, ctx))
        try:
            if call.name == "run_code" and self._router is not None:
                return await self._router.dispatch(call)
            return await self._inner.execute(call, ctx)
        finally:
            _dispatch.reset(token)


def run_code_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(_ENV) or "").strip() in {"1", "true", "yes", "on"}


def _timeout_s(environ: Mapping[str, str]) -> float:
    raw = (environ.get(_TIMEOUT_ENV) or "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_MS / 1000.0
    try:
        ms = int(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_MS / 1000.0
    return max(1.0, ms / 1000.0)


def _sidecar_confined(environ: Mapping[str, str]) -> bool:
    """True when the sidecar itself already runs under an OS sandbox.

    The desktop sets ``STEERABLE_SIDECAR_CONFINED=1`` when it wraps the
    sidecar in layer-1 confinement (Seatbelt/bwrap/Landlock). A confined
    sidecar cannot apply a *second* sandbox to its own child — macOS denies a
    nested ``sandbox_apply`` once the outer profile allows outbound network —
    so ``run_code`` lets the child inherit the layer-1 boundary instead of
    wrapping it again.
    """
    return (environ.get(_CONFINED_ENV) or "").strip() in {"1", "true", "yes", "on"}


def _result_payload(result: ToolResult) -> dict[str, Any]:
    dumped = result.model_dump(exclude_none=True)
    return dumped


async def _invoke_nested(
    name: str, arguments: dict[str, Any], call_id: str
) -> ToolResult:
    if name == "run_code":
        return ToolResult(
            success=False,
            error="nested run_code is not allowed",
            needsFollowup=False,
        )
    bound = _dispatch.get()
    call = ToolCall(id=call_id, name=name, arguments=arguments)
    if bound is not None:
        return await bound.executor.execute(call, bound.ctx)
    router = _router_for_rpc.get()
    if router is None:
        return ToolResult(
            success=False,
            error="run_code has no tool executor",
            needsFollowup=False,
        )
    return await router.dispatch(call)


async def _drive_child(
    *,
    source: str,
    description: str,
    environ: Mapping[str, str],
) -> ToolResult:
    if len(source) > _MAX_SOURCE:
        return ToolResult(
            success=False,
            error="run_code source exceeds the size cap",
            needsFollowup=True,
        )
    tmpdir = tempfile.mkdtemp(prefix="steerable-run-code-")
    program_path = Path(tmpdir) / "program.py"
    try:
        program_path.write_text(source, encoding="utf-8")
        inherited = _sidecar_confined(environ)
        if inherited:
            # The sidecar is already layer-1 confined; a nested wrap would fail
            # (macOS denies a second ``sandbox_apply`` under an outer profile
            # that allows outbound network). The child inherits the outer
            # boundary — a documented ``partial`` posture, not a dedicated one.
            backend = None
        else:
            backend = select_exec_backend(writable_roots=[tmpdir], network=False)
            if backend is None:
                return ToolResult(
                    success=False,
                    error="sandbox_unavailable",
                    needsFollowup=False,
                    data={
                        "_sandbox": {"backend": "none", "enforcement": "none"},
                        "message": (
                            "Refused to run run_code: no OS sandbox backend to "
                            "confine the child interpreter."
                        ),
                    },
                )
        driver_argv = [
            sys.executable,
            "-m",
            "steerable_sidecar.run_code_driver",
            "--program",
            str(program_path),
        ]
        argv = driver_argv if inherited else backend.argv_for_exec(driver_argv)
        sandbox_marker = (
            {"backend": "inherited", "enforcement": "partial", "via": "layer1"}
            if inherited
            else {
                "backend": getattr(backend, "name", "unknown"),
                "enforcement": getattr(backend, "enforcement", "partial"),
            }
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_environ(environ),
        )
        calls: list[dict[str, Any]] = []
        logs: list[str] = []

        async def _run() -> ToolResult:
            assert proc.stdout is not None and proc.stdin is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    stderr = b""
                    if proc.stderr is not None:
                        stderr = await proc.stderr.read()
                    err = stderr.decode("utf-8", errors="replace").strip()
                    return ToolResult(
                        success=False,
                        error=err or "run_code child exited without a result",
                        needsFollowup=True,
                        data={"description": description, "calls": calls, "logs": logs},
                    )
                try:
                    frame = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    logs.append(line.decode("utf-8", errors="replace").rstrip())
                    continue
                kind = frame.get("type")
                if kind == "log":
                    text = str(frame.get("text") or "")
                    if text:
                        logs.append(text)
                    continue
                if kind == "done":
                    ok = bool(frame.get("ok"))
                    if not ok:
                        return ToolResult(
                            success=False,
                            error=str(frame.get("error") or "run_code failed"),
                            needsFollowup=True,
                            data={
                                "description": description,
                                "calls": calls,
                                "logs": logs,
                            },
                        )
                    return ToolResult(
                        success=True,
                        data={
                            "description": description,
                            "value": frame.get("value"),
                            "calls": calls,
                            "logs": logs,
                            "_sandbox": dict(sandbox_marker),
                        },
                    )
                if kind != "call":
                    continue
                if len(calls) >= _MAX_CALLS:
                    return ToolResult(
                        success=False,
                        error=f"run_code exceeded {_MAX_CALLS} nested tool calls",
                        needsFollowup=True,
                        data={"description": description, "calls": calls, "logs": logs},
                    )
                tool_name = str(frame.get("tool") or "")
                arguments = frame.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = {}
                nested_id = f"run_code-{frame.get('id')}"
                result = await _invoke_nested(tool_name, arguments, nested_id)
                calls.append(
                    {
                        "tool": tool_name,
                        "arguments": arguments,
                        "result": _result_payload(result),
                    }
                )
                reply = {
                    "v": 1,
                    "id": frame.get("id"),
                    "ok": result.success,
                    "result": _result_payload(result) if result.success else None,
                    "error": None if result.success else (result.error or "tool failed"),
                }
                proc.stdin.write(
                    (json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8")
                )
                await proc.stdin.drain()

        try:
            return await asyncio.wait_for(_run(), timeout=_timeout_s(environ))
        except TimeoutError:
            return ToolResult(
                success=False,
                error="run_code timed out",
                needsFollowup=True,
                data={"description": description, "calls": calls, "logs": logs},
            )
        finally:
            if proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except (TimeoutError, ProcessLookupError):
                    pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def register_run_code(
    router: ToolRouter,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Register ``run_code`` on ``router``. Returns the tool name."""

    env = os.environ if environ is None else environ
    token = _router_for_rpc.set(router)

    async def run_code(code: str = "", description: str = "") -> ToolResult:
        source = (code or "").strip()
        if not source:
            return ToolResult(success=False, error="code is empty", needsFollowup=True)
        summary = (description or "").strip() or "run_code"
        return await _drive_child(source=source, description=summary, environ=env)

    router.register(
        run_code,
        name="run_code",
        mode="local",
        description=_DESCRIPTION,
        schema=_SCHEMA,
        require_consent=False,
        concurrency_safe=False,
    )
    # token kept: RPC fallback dispatch uses this router for the process life.
    del token
    return "run_code"
