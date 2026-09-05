"""Sandboxed tool execution: the ``SandboxedToolExecutor`` port (Wave 3).

Layer 1 of the safety model confines the sidecar *process* (the Seatbelt
profile tooling in ``steerable_sidecar.sandbox``). This module is layer 2:
confining what a shell/subprocess *tool call* runs. The executor is a
``ToolExecutor`` decorator — it rewrites the call's command argument into a
sandboxed invocation and delegates, so it works in front of any dispatch
path. For the desktop that means the rewritten command travels over the
reverse channel and the host's shell spawns it confined — the per-exec
Seatbelt story without the host learning any sandbox mechanics.

The backend is pluggable (``SandboxBackend``): Seatbelt on macOS today,
E2B-class remote sandboxes on a server tomorrow. Enforcement is reported as
a *value* — ``data["_sandbox"]["enforcement"]`` on the result — not a log
line (dsh's ``SandboxEnforcement`` lesson). Two independent refuse knobs:

- ``require_backend`` denies only ``enforcement == "none"`` (no OS
  backend; the command would run unsandboxed). ``partial`` still runs —
  the desktop's ``network: true`` makes every current backend honest-partial.
- ``require_full`` denies anything weaker than ``full`` (including
  ``partial``). Do not send this from a product that also asks for egress.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from steerable_agent_protocol.generated import ToolCall, ToolResult

if TYPE_CHECKING:
    from collections.abc import Collection

    from .loop import LoopContext, ToolExecutor

__all__ = [
    "DEFAULT_SHELL_TOOLS",
    "SandboxBackend",
    "SandboxEnforcement",
    "SandboxedToolExecutor",
]

#: Reported confinement strength: ``full`` = OS-enforced deny-by-default
#: boundary; ``partial`` = confined but with a documented gap (e.g. egress
#: open or port-only); ``none`` = no backend, the call ran unsandboxed.
SandboxEnforcement = Literal["full", "partial", "none"]

#: Tool names treated as shell/subprocess calls unless configured otherwise.
#: Covers the framework convention (``bash``/``shell``) and the desktop
#: host's ``local_exec_shell``; all take a ``command`` string argument.
DEFAULT_SHELL_TOOLS = frozenset({"bash", "shell", "local_exec_shell"})


@runtime_checkable
class SandboxBackend(Protocol):
    """Confinement mechanism: rewrite a shell command into a sandboxed one.

    The returned string is parsed by the same shell that would have run the
    original command, so the wrapper must be a valid shell invocation (quote
    every interpolated part — see the Seatbelt backend for the pattern).
    """

    @property
    def name(self) -> str:
        """Backend identifier reported in the result marker."""
        ...

    @property
    def enforcement(self) -> SandboxEnforcement:
        """``full`` or ``partial`` — never ``none`` (a missing backend is
        represented by ``None``, not by a backend reporting none)."""
        ...

    def wrap_command(self, command: str) -> str:
        """Return a shell string that runs ``command`` confined."""
        ...


class SandboxedToolExecutor:
    """``ToolExecutor`` decorator confining shell/subprocess calls.

    Non-shell calls pass through untouched. Shell calls (``shell_tools``,
    carrying the command in ``command_arg``) are rewritten by the backend
    and then delegated; the result gains a ``data["_sandbox"]`` marker with
    the backend name and the enforcement strength actually applied.

    Fail-closed options, checked before execution:

    - ``require_backend``: refuse ``none`` (no backend). Partial backends
      still run.
    - ``require_full``: refuse anything other than ``full``. Stricter;
      takes precedence when both are set.
    """

    def __init__(
        self,
        inner: ToolExecutor,
        backend: SandboxBackend | None,
        *,
        shell_tools: Collection[str] = DEFAULT_SHELL_TOOLS,
        command_arg: str = "command",
        require_full: bool = False,
        require_backend: bool = False,
    ) -> None:
        self._inner = inner
        self._backend = backend
        self._shell_tools = frozenset(shell_tools)
        self._command_arg = command_arg
        self._require_full = require_full
        self._require_backend = require_backend

    def concurrency_safe(self, call: ToolCall) -> bool:
        check = getattr(self._inner, "concurrency_safe", None)
        return bool(check is not None and check(call))

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        if call.name not in self._shell_tools:
            return await self._inner.execute(call, ctx)
        arguments = call.arguments or {}
        command = arguments.get(self._command_arg)
        if not isinstance(command, str) or not command.strip():
            # Not a command-shaped call — let the inner executor's own
            # validation produce the error.
            return await self._inner.execute(call, ctx)

        backend = self._backend
        enforcement: SandboxEnforcement = (
            backend.enforcement if backend is not None else "none"
        )
        if self._require_full and enforcement != "full":
            return ToolResult(
                success=False,
                error="sandbox_unavailable",
                needsFollowup=False,
                data={
                    "_sandbox": self._marker(enforcement, backend),
                    "message": (
                        f"Refused to run '{call.name}': this deployment "
                        f"requires full sandbox enforcement, got "
                        f"'{enforcement}'."
                    ),
                },
            )
        if self._require_backend and enforcement == "none":
            return ToolResult(
                success=False,
                error="sandbox_unavailable",
                needsFollowup=False,
                data={
                    "_sandbox": self._marker(enforcement, backend),
                    "message": (
                        f"Refused to run '{call.name}': this deployment "
                        "requires a sandbox backend, got 'none'."
                    ),
                },
            )
        if backend is None:
            return self._mark(
                await self._inner.execute(call, ctx), enforcement, backend
            )
        rewritten = ToolCall(
            id=call.id,
            name=call.name,
            arguments={**arguments, self._command_arg: backend.wrap_command(command)},
        )
        return self._mark(
            await self._inner.execute(rewritten, ctx), enforcement, backend
        )

    @staticmethod
    def _marker(
        enforcement: SandboxEnforcement, backend: SandboxBackend | None
    ) -> dict[str, Any]:
        marker: dict[str, Any] = {"enforcement": enforcement}
        if backend is not None:
            marker["backend"] = backend.name
        return marker

    @classmethod
    def _mark(
        cls,
        result: ToolResult,
        enforcement: SandboxEnforcement,
        backend: SandboxBackend | None,
    ) -> ToolResult:
        data = result.data if isinstance(result.data, dict) else {}
        return result.model_copy(
            update={"data": {**data, "_sandbox": cls._marker(enforcement, backend)}}
        )
