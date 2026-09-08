"""``run_js`` / ``wait_js`` — conversational JS PTC against a persistent Node worker.

The JavaScript counterpart of ``run_code`` (and the steerable counterpart of
codex's CodeModeHost): the model runs JS *cells* that call tools through a
Promise bridge, and a chat-scoped session keeps a JSON KV (``store`` /
``load``) plus still-running cells alive across model rounds.

Process model: the sidecar spawns **one persistent Node worker**
(``ptc_js_worker.cjs``) on first use and speaks the same line-framed JSON
protocol as ``run_code``'s driver, multiplexed by ``cellId``/``callId``. Each
cell runs in its own ``node:vm`` context (async-function body semantics:
top-level ``await``, ``return`` is the cell's value) with only injected
globals — no ``process``/``require``/``fetch``/``Buffer``. The worker process
itself is confined by the same layer-2 backend as ``run_code``
(``network=False``) and gets the same credential-scrubbed environment;
node:vm is defense in depth, not the security boundary.

Model surface:

- ``run_js`` starts a cell. When the cell finishes before ``yieldTimeMs``
  (default 10s) the result is final (``status: "completed"``); otherwise the
  call returns early with ``status: "running"`` and a ``cellId`` — the cell
  keeps executing in the worker.
- ``wait_js`` returns the output a running cell accumulated since the last
  response, its final result, or (with ``terminate: true``) stops it.
- A cell's nested ``tools.<name>(args)`` calls cross back over stdio into the
  live ToolExecutor — approval, sandbox, and the host reverse channel apply,
  exactly like ``run_code``'s nested calls.

Default off: set ``STEERABLE_PTC_JS=1`` to register. Node resolution:
``STEERABLE_PTC_NODE`` → ``node`` on PATH. The desktop points
``STEERABLE_PTC_NODE`` at its bundled runtime (Electron as Node via
``ELECTRON_RUN_AS_NODE``); headless deployments need Node >= 18 on PATH.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from steerable_agent_protocol.generated import ToolResult
from steerable_agent_runtime.tools import ToolRouter

from .run_code import (
    _child_environ,
    _dispatch,
    _result_payload,
    _sidecar_confined,
    invoke_nested_tool,
)
from .sandbox import select_exec_backend

if TYPE_CHECKING:
    from steerable_agent_runtime.loop import LoopContext, ToolExecutor

__all__ = [
    "ptc_js_enabled",
    "register_ptc_js",
    "run_js_tool_descriptor",
    "wait_js_tool_descriptor",
]

_ENV = "STEERABLE_PTC_JS"
_NODE_ENV = "STEERABLE_PTC_NODE"
_CELL_TIMEOUT_ENV = "STEERABLE_PTC_JS_CELL_TIMEOUT_MS"
_MARGIN_ENV = "STEERABLE_PTC_JS_RESPONSE_MARGIN_MS"
_MAX_SOURCE = 100_000
_MAX_CALLS = 32
_DEFAULT_YIELD_MS = 10_000
_DEFAULT_MAX_OUTPUT_CHARS = 40_000
_DEFAULT_CELL_TIMEOUT_MS = 600_000
#: A cell answers every exec/wait within its yield timer; the Python-side
#: budget adds this margin for transport and tool-bridge latency. Exceeding
#: it means the worker's event loop is wedged (a synchronous infinite loop
#: in model JS — the one failure vm contexts cannot interrupt), so the whole
#: worker is killed and every cell fails loud.
_DEFAULT_MARGIN_MS = 60_000
_READY_TIMEOUT_S = 15.0

_WORKER_PATH = Path(__file__).with_name("ptc_js_worker.cjs")

_RUN_JS_DESCRIPTION = (
    "Run JavaScript code to orchestrate/compose tool calls in a persistent "
    "session.\n"
    "- The code runs as the body of an async function: top-level `await` "
    "works and `return <value>` is the cell's result.\n"
    "- Call other tools on the global `tools` object: "
    "`const r = await tools.bash({command: \"ls\"})` or "
    "`await tools.call(\"bash\", {command: \"ls\"})`. A successful call "
    "resolves with the tool's data payload; a failed one rejects the "
    "promise. Nested run_js/wait_js are refused.\n"
    "- Raw JavaScript only — no Node.js: no require/import, no process, no "
    "fs, no network, no Buffer.\n"
    "- `store(key, value)` / `load(key)`: a JSON-serializable KV shared by "
    "every run_js cell of this chat; values persist across cells and turns.\n"
    "- `text(value)` appends output returned with the result; `console.*` "
    "goes to the result's logs. `setTimeout`/`clearTimeout` exist; pending "
    "timers do not keep a cell alive. `exit()` ends the cell successfully "
    "and early.\n"
    "- `yield_control()` returns the accumulated output immediately while "
    "the cell keeps running. If the cell is still running after "
    "yieldTimeMs (default 10000), run_js returns early the same way: "
    "status \"running\" plus a cellId. Call wait_js with that cellId to get "
    "more output or the final result."
)

_WAIT_JS_DESCRIPTION = (
    "Wait on a running run_js cell.\n"
    "- `cellId` identifies the running cell (from a run_js result with "
    "status \"running\").\n"
    "- Returns the output accumulated since the last response, or the "
    "cell's final result once it finishes; a finished cell is closed by "
    "reading its result.\n"
    "- `yieldTimeMs` (default 10000) bounds the wait before answering with "
    "the output so far.\n"
    "- `terminate: true` stops the cell and returns its final state."
)

_RUN_JS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": (
                "JavaScript source, evaluated as the body of an async "
                "function. `return` is the cell result. Call tools with "
                "`await tools.<name>({...})`."
            ),
        },
        "description": {
            "type": "string",
            "description": "Short summary of what the cell does.",
        },
        "yieldTimeMs": {
            "type": "integer",
            "description": (
                "Return early with a cellId if the cell is still running "
                "after this many ms. Defaults to 10000."
            ),
        },
        "maxOutputChars": {
            "type": "integer",
            "description": (
                "Output budget for this call's result, in characters. "
                "Defaults to 40000."
            ),
        },
    },
    "required": ["code"],
    "additionalProperties": False,
}

_WAIT_JS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cellId": {
            "type": "string",
            "description": "Identifier of the running run_js cell.",
        },
        "yieldTimeMs": {
            "type": "integer",
            "description": (
                "Wait at most this many ms for more output before "
                "answering. Defaults to 10000."
            ),
        },
        "maxOutputChars": {
            "type": "integer",
            "description": (
                "Output budget for this call's result, in characters. "
                "Defaults to 40000."
            ),
        },
        "terminate": {
            "type": "boolean",
            "description": (
                "True stops the running cell; false or omitted waits for "
                "output."
            ),
        },
    },
    "required": ["cellId"],
    "additionalProperties": False,
}


def ptc_js_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(_ENV) or "").strip() in {"1", "true", "yes", "on"}


def run_js_tool_descriptor() -> dict[str, Any]:
    """OpenAI tool schema to append to the model's tools list."""
    return {
        "type": "function",
        "function": {
            "name": "run_js",
            "description": _RUN_JS_DESCRIPTION,
            "parameters": _RUN_JS_SCHEMA,
        },
    }


def wait_js_tool_descriptor() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "wait_js",
            "description": _WAIT_JS_DESCRIPTION,
            "parameters": _WAIT_JS_SCHEMA,
        },
    }


def _node_executable(environ: Mapping[str, str]) -> str | None:
    explicit = (environ.get(_NODE_ENV) or "").strip()
    if explicit:
        return explicit
    return shutil.which("node")


def _cell_timeout_ms(environ: Mapping[str, str]) -> int:
    raw = (environ.get(_CELL_TIMEOUT_ENV) or "").strip()
    try:
        return max(1_000, int(raw)) if raw else _DEFAULT_CELL_TIMEOUT_MS
    except ValueError:
        return _DEFAULT_CELL_TIMEOUT_MS


def _response_margin_s(environ: Mapping[str, str]) -> float:
    raw = (environ.get(_MARGIN_ENV) or "").strip()
    try:
        return max(500, int(raw)) / 1000.0 if raw else _DEFAULT_MARGIN_MS / 1000.0
    except ValueError:
        return _DEFAULT_MARGIN_MS / 1000.0


def _worker_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """The scrubbed environment the JS worker runs with.

    Same credential allowlist as ``run_code``'s child, plus the two knobs
    the worker reads and ``ELECTRON_RUN_AS_NODE`` — the desktop points
    ``STEERABLE_PTC_NODE`` at the bundled Electron binary, which only runs
    as plain Node when that variable is set on the child.
    """
    child = _child_environ(environ)
    for passthrough in (
        "ELECTRON_RUN_AS_NODE",
        "STEERABLE_PTC_JS_SESSION_TTL_MS",
        "STEERABLE_PTC_JS_MAX_SESSIONS",
    ):
        value = environ.get(passthrough)
        if value:
            child[passthrough] = value
    return child


def _session_id(context: Mapping[str, Any] | None) -> str:
    """Bind the JS session to the chat: loop dispatch carries ``chat_id``,
    the host's RPC fallback carries ``chatId``."""
    if not context:
        return "default"
    raw = context.get("chat_id") or context.get("chatId")
    return str(raw) if raw else "default"


class _StartError(Exception):
    """Worker startup failed; ``result`` is the model-visible ToolResult."""

    def __init__(self, result: ToolResult) -> None:
        super().__init__(result.error or "run_js worker failed to start")
        self.result = result


class _WorkerDeadError(Exception):
    """The worker process died (or was killed wedged) mid-request."""


class _CellState:
    __slots__ = ("cell_id", "session_id", "description", "queue", "calls", "dispatch")

    def __init__(
        self,
        cell_id: str,
        session_id: str,
        description: str,
        dispatch: Any,
    ) -> None:
        self.cell_id = cell_id
        self.session_id = session_id
        self.description = description
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.calls: list[dict[str, Any]] = []
        # The (executor, LoopContext) pair the cell's nested tool calls run
        # through. Captured at exec, refreshed by every wait_js: a cell
        # outlives the model round that started it, and tool_call frames
        # arrive on the worker's read task where no dispatch ContextVar is
        # set.
        self.dispatch = dispatch


class _Worker:
    """One persistent Node worker process: transport + frame demux.

    Cell frames (``cell_yield``/``cell_done``) are routed to the owning
    cell's queue; ``tool_call`` frames spawn bridge tasks so a slow host
    tool never blocks the pipe. Mirrors ``run_code``'s two transports:
    asyncio subprocess on POSIX, threaded pump on Windows (Proactor named
    pipes are denied under the restricted token).
    """

    def __init__(
        self,
        argv: list[str],
        env: dict[str, str],
        *,
        on_cell_frame: Callable[[dict[str, Any]], None],
        on_tool_call: Callable[[str, str, str, dict[str, Any]], Awaitable[tuple[bool, dict[str, Any]]]],
        on_dead: Callable[[], None],
    ) -> None:
        self._argv = argv
        self._env = env
        self._on_cell_frame = on_cell_frame
        self._on_tool_call = on_tool_call
        self._on_dead = on_dead
        self._proc_async: asyncio.subprocess.Process | None = None
        self._proc_sync: subprocess.Popen[bytes] | None = None
        self._frames: queue.Queue[bytes] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._write_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self.dead = False

    async def start(self) -> None:
        if sys.platform == "win32":
            self._proc_sync = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
            )
            self._frames = queue.Queue()
            threading.Thread(
                target=self._pump_stdout, name="ptc-js-stdout-pump", daemon=True
            ).start()
            threading.Thread(
                target=self._pump_stderr, name="ptc-js-stderr-pump", daemon=True
            ).start()
        else:
            self._proc_async = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )
            asyncio.create_task(self._drain_stderr_async())
        reader = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_READY_TIMEOUT_S)
        except TimeoutError:
            reader.cancel()
            await self.aclose()
            raise _StartError(
                ToolResult(
                    success=False,
                    error="node_unavailable",
                    needsFollowup=False,
                    data={
                        "message": (
                            "The run_js worker did not report ready within "
                            f"{_READY_TIMEOUT_S}s. Check STEERABLE_PTC_NODE "
                            f"({self._argv[0]!r}). stderr: {self.stderr_text()}"
                        )
                    },
                )
            ) from None
        if self.dead:
            await self.aclose()
            raise _StartError(
                ToolResult(
                    success=False,
                    error="node_unavailable",
                    needsFollowup=False,
                    data={
                        "message": (
                            "The run_js worker exited before reporting ready. "
                            f"stderr: {self.stderr_text()}"
                        )
                    },
                )
            )

    # -- transports ------------------------------------------------------

    def _pump_stdout(self) -> None:
        try:
            assert self._proc_sync is not None and self._proc_sync.stdout is not None
            while True:
                chunk = self._proc_sync.stdout.readline()
                if not chunk:
                    break
                assert self._frames is not None
                self._frames.put(chunk)
        except Exception:
            pass
        finally:
            assert self._frames is not None
            self._frames.put(b"")  # EOF sentinel

    def _pump_stderr(self) -> None:
        try:
            assert self._proc_sync is not None and self._proc_sync.stderr is not None
            for raw in iter(self._proc_sync.stderr.readline, b""):
                self._stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())
        except Exception:
            pass

    async def _drain_stderr_async(self) -> None:
        try:
            assert self._proc_async is not None and self._proc_async.stderr is not None
            while True:
                line = await self._proc_async.stderr.readline()
                if not line:
                    return
                self._stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())
        except Exception:
            pass

    async def _readline(self) -> bytes:
        if self._proc_async is not None:
            assert self._proc_async.stdout is not None
            return await self._proc_async.stdout.readline()
        assert self._frames is not None
        return await asyncio.to_thread(self._frames.get)

    async def _write(self, data: bytes) -> None:
        if self._proc_async is not None:
            assert self._proc_async.stdin is not None
            self._proc_async.stdin.write(data)
            await self._proc_async.stdin.drain()
            return
        assert self._proc_sync is not None and self._proc_sync.stdin is not None
        self._proc_sync.stdin.write(data)
        self._proc_sync.stdin.flush()

    # -- protocol --------------------------------------------------------

    async def send(self, frame: dict[str, Any]) -> None:
        if self.dead:
            raise _WorkerDeadError("run_js worker is dead")
        data = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                await self._write(data)
            except (BrokenPipeError, OSError) as exc:
                self.dead = True
                raise _WorkerDeadError("run_js worker pipe is closed") from exc

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._readline()
                if not line:
                    return
                try:
                    frame = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                kind = frame.get("type")
                if kind == "ready":
                    self._ready.set()
                elif kind == "tool_call":
                    asyncio.create_task(self._bridge(frame))
                elif kind in ("cell_yield", "cell_done"):
                    self._on_cell_frame(frame)
        finally:
            self.dead = True
            self._ready.set()  # unblock a start() still waiting
            self._on_dead()

    async def _bridge(self, frame: dict[str, Any]) -> None:
        call_id = str(frame.get("callId") or "")
        arguments = frame.get("arguments")
        ok, payload = await self._on_tool_call(
            str(frame.get("cellId") or ""),
            call_id,
            str(frame.get("tool") or ""),
            arguments if isinstance(arguments, dict) else {},
        )
        reply = {"v": 1, "type": "tool_result", "callId": call_id, "ok": ok, **payload}
        try:
            await self.send(reply)
        except _WorkerDeadError:
            pass

    def stderr_text(self) -> str:
        return "\n".join(self._stderr_tail).strip()[-2000:] or "(empty)"

    async def aclose(self) -> None:
        self.dead = True
        proc_async, proc_sync = self._proc_async, self._proc_sync
        self._proc_async = None
        self._proc_sync = None
        if proc_async is not None and proc_async.returncode is None:
            proc_async.kill()
            try:
                await asyncio.wait_for(proc_async.wait(), timeout=2)
            except (TimeoutError, ProcessLookupError):
                pass
        if proc_sync is not None and proc_sync.poll() is None:
            proc_sync.kill()
            try:
                await asyncio.wait_for(asyncio.to_thread(proc_sync.wait), timeout=2)
            except (TimeoutError, ProcessLookupError):
                pass


class _PtcJsRuntime:
    """Owns the sidecar process's single JS worker and its live cells."""

    def __init__(self, environ: Mapping[str, str], router: ToolRouter) -> None:
        self._environ = dict(environ)
        self._router = router
        self._worker: _Worker | None = None
        self._spawn_lock = asyncio.Lock()
        self._cells: dict[str, _CellState] = {}
        self._sandbox_marker: dict[str, Any] | None = None

    # -- worker lifecycle -------------------------------------------------

    async def _ensure_worker(self) -> _Worker:
        worker = self._worker
        if worker is not None and not worker.dead:
            return worker
        async with self._spawn_lock:
            worker = self._worker
            if worker is not None and not worker.dead:
                return worker
            self._worker = None
            self._sandbox_marker = None
            worker = await self._spawn()
            self._worker = worker
            return worker

    async def _spawn(self) -> _Worker:
        env = self._environ
        node = _node_executable(env)
        explicit = (env.get(_NODE_ENV) or "").strip()
        if node is None:
            raise _StartError(
                ToolResult(
                    success=False,
                    error="node_unavailable",
                    needsFollowup=False,
                    data={
                        "message": (
                            "run_js needs a Node.js runtime: no "
                            "STEERABLE_PTC_NODE set and no `node` on PATH."
                        )
                    },
                )
            )
        if explicit and not Path(node).exists():
            raise _StartError(
                ToolResult(
                    success=False,
                    error="node_unavailable",
                    needsFollowup=False,
                    data={
                        "message": (
                            f"STEERABLE_PTC_NODE points at {node!r}, which "
                            "does not exist."
                        )
                    },
                )
            )
        argv = [node, str(_WORKER_PATH)]
        if _sidecar_confined(env):
            # Same rule as run_code: a layer-1-confined sidecar cannot nest
            # a second OS wrap; the worker inherits the outer boundary.
            marker: dict[str, Any] = {
                "backend": "inherited",
                "enforcement": "partial",
                "via": "layer1",
            }
        else:
            backend = select_exec_backend(writable_roots=[], network=False)
            if backend is None:
                raise _StartError(
                    ToolResult(
                        success=False,
                        error="sandbox_unavailable",
                        needsFollowup=False,
                        data={
                            "_sandbox": {"backend": "none", "enforcement": "none"},
                            "message": (
                                "Refused to start the run_js worker: no OS "
                                "sandbox backend to confine it."
                            ),
                        },
                    )
                )
            argv = backend.argv_for_exec(argv)
            marker = {
                "backend": getattr(backend, "name", "unknown"),
                "enforcement": getattr(backend, "enforcement", "partial"),
            }
        worker = _Worker(
            argv,
            _worker_environ(env),
            on_cell_frame=self._route_cell_frame,
            on_tool_call=self._invoke_for_cell,
            on_dead=self._on_worker_dead,
        )
        try:
            await worker.start()
        except _StartError:
            raise
        except Exception as exc:
            raise _StartError(
                ToolResult(
                    success=False,
                    error="node_unavailable",
                    needsFollowup=False,
                    data={
                        "message": f"Failed to spawn the run_js worker ({node!r}): {exc}"
                    },
                )
            ) from exc
        self._sandbox_marker = marker
        return worker

    def _on_worker_dead(self) -> None:
        # Fail every waiter; the next run_js lazily respawns. Cells are
        # process-local state, so a dead worker loses them all — wait_js on
        # one then answers "unknown cell" from a fresh worker.
        for state in self._cells.values():
            state.queue.put_nowait({"type": "worker_dead"})
        self._cells.clear()

    async def _kill_wedged_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            await worker.aclose()
        self._on_worker_dead()

    def close_sync(self) -> None:
        """atexit hook: kill the worker so it never outlives the sidecar."""
        worker, self._worker = self._worker, None
        if worker is None:
            return
        proc = worker._proc_async or worker._proc_sync
        if proc is None:
            return
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass

    # -- cell registry -----------------------------------------------------

    def _route_cell_frame(self, frame: dict[str, Any]) -> None:
        state = self._cells.get(str(frame.get("cellId") or ""))
        if state is not None:
            state.queue.put_nowait(frame)

    async def _invoke_for_cell(
        self, cell_id: str, call_id: str, tool: str, arguments: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        state = self._cells.get(cell_id)
        result = await invoke_nested_tool(
            tool,
            arguments,
            f"run_js-{call_id}",
            refused=("run_js", "wait_js"),
            dispatch=state.dispatch if state is not None else None,
            router=self._router,
        )
        if state is not None:
            state.calls.append(
                {
                    "tool": tool,
                    "arguments": arguments,
                    "result": _result_payload(result),
                }
            )
        if result.success:
            return True, {"value": json.dumps(result.data, ensure_ascii=False, default=str)}
        return False, {"error": result.error or "tool failed"}

    # -- exec / wait --------------------------------------------------------

    async def exec_cell(
        self,
        *,
        code: str,
        description: str,
        yield_ms: int,
        max_output_chars: int,
        context: Mapping[str, Any] | None,
    ) -> ToolResult:
        worker = await self._ensure_worker()
        cell_id = f"cell-{secrets.token_hex(8)}"
        state = _CellState(cell_id, _session_id(context), description, _dispatch.get())
        self._cells[cell_id] = state
        try:
            await worker.send(
                {
                    "v": 1,
                    "type": "exec",
                    "cellId": cell_id,
                    "sessionId": state.session_id,
                    "code": code,
                    "yieldTimeMs": yield_ms,
                    "maxOutputChars": max_output_chars,
                    "maxCellTimeMs": _cell_timeout_ms(self._environ),
                    "maxToolCalls": _MAX_CALLS,
                    "tools": self._tools_meta(),
                }
            )
        except _WorkerDeadError:
            self._cells.pop(cell_id, None)
            return self._worker_dead_result(state)
        return await self._await_cell(state, worker, yield_ms, terminate_on_cancel=True)

    async def wait_cell(
        self,
        *,
        cell_id: str,
        yield_ms: int,
        max_output_chars: int,
        terminate: bool,
    ) -> ToolResult:
        state = self._cells.get(cell_id)
        if state is None:
            return ToolResult(
                success=False,
                error=(
                    f"unknown cell: {cell_id} (already finished, terminated, "
                    "or lost with a restarted worker)"
                ),
                needsFollowup=True,
            )
        # The wait runs inside its own RunCodeBoundExecutor dispatch — hand
        # the cell the fresh one so nested calls use this turn's context.
        fresh = _dispatch.get()
        if fresh is not None:
            state.dispatch = fresh
        worker = await self._ensure_worker()
        try:
            await worker.send(
                {
                    "v": 1,
                    "type": "wait",
                    "cellId": cell_id,
                    "yieldTimeMs": yield_ms,
                    "maxOutputChars": max_output_chars,
                    "terminate": terminate or None,
                }
            )
        except _WorkerDeadError:
            self._cells.pop(cell_id, None)
            return self._worker_dead_result(state)
        return await self._await_cell(state, worker, yield_ms, terminate_on_cancel=False)

    async def _await_cell(
        self,
        state: _CellState,
        worker: _Worker,
        yield_ms: int,
        *,
        terminate_on_cancel: bool,
    ) -> ToolResult:
        # Drain stale frames first: a cell that finished while no exec/wait
        # was outstanding left its terminal frame queued; stale yields are
        # superseded by the response the worker is about to produce.
        done: dict[str, Any] | None = None
        while not state.queue.empty():
            queued = state.queue.get_nowait()
            if queued.get("type") in ("cell_done", "worker_dead"):
                done = queued
        if done is not None:
            return self._render(state, done)
        timeout_s = max(yield_ms, 1) / 1000.0 + _response_margin_s(self._environ)
        try:
            frame = await asyncio.wait_for(state.queue.get(), timeout=timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            # asyncio.TimeoutError is the builtin TimeoutError only on 3.11+;
            # the sidecar supports 3.10, where the two are unrelated classes.
            # The worker guarantees a response within the yield timer, so a
            # timeout means its event loop is wedged (synchronous infinite
            # loop in model JS — the one thing a vm context cannot
            # interrupt). Kill the process; every cell fails loud.
            await self._kill_wedged_worker()
            return ToolResult(
                success=False,
                error=(
                    "run_js worker stopped responding (likely an "
                    "uninterruptible synchronous loop in the cell); the "
                    "worker was killed and all its cells were terminated"
                ),
                needsFollowup=True,
                data={"cellId": state.cell_id, "calls": state.calls},
            )
        except asyncio.CancelledError:
            if terminate_on_cancel:
                # The turn died before the model learned the cellId — the
                # cell would be orphaned until the idle reaper. Terminate it.
                try:
                    await worker.send(
                        {"v": 1, "type": "wait", "cellId": state.cell_id, "terminate": True}
                    )
                except _WorkerDeadError:
                    pass
                self._cells.pop(state.cell_id, None)
            raise
        return self._render(state, frame)

    # -- rendering ----------------------------------------------------------

    def _tools_meta(self) -> list[dict[str, str]]:
        # ALL_TOOLS is informational: the model's own tools array is the
        # authoritative surface (host tools never appear in the sidecar's
        # router). Listed here are the sidecar-local tools, minus the PTC
        # pair itself (nested run_js/wait_js are refused anyway).
        return [
            {
                "name": descriptor["function"]["name"],
                "description": descriptor["function"].get("description") or "",
            }
            for descriptor in self._router.describe()
            if descriptor["function"]["name"] not in ("run_js", "wait_js")
        ]

    def _worker_dead_result(self, state: _CellState) -> ToolResult:
        return ToolResult(
            success=False,
            error="run_js worker died; the cell was lost with it",
            needsFollowup=True,
            data={"cellId": state.cell_id, "calls": state.calls},
        )

    def _render(self, state: _CellState, frame: dict[str, Any]) -> ToolResult:
        kind = frame.get("type")
        base: dict[str, Any] = {
            "cellId": state.cell_id,
            "description": state.description,
            "output": frame.get("output") or [],
            "logs": frame.get("logs") or [],
            "calls": state.calls,
        }
        if self._sandbox_marker is not None:
            base["_sandbox"] = dict(self._sandbox_marker)
        if frame.get("truncated"):
            base["truncated"] = True
        if kind == "worker_dead":
            self._cells.pop(state.cell_id, None)
            return self._worker_dead_result(state)
        if kind == "cell_yield":
            return ToolResult(
                success=True,
                data={
                    **base,
                    "status": "running",
                    "message": (
                        f"Script running with cell ID {state.cell_id}. Call "
                        "wait_js with this cellId to get more output or the "
                        "final result."
                    ),
                },
            )
        # cell_done is terminal: the worker has already forgotten the cell.
        self._cells.pop(state.cell_id, None)
        if frame.get("ok"):
            raw_value = frame.get("value")
            try:
                value = json.loads(raw_value) if raw_value else None
            except (json.JSONDecodeError, TypeError):
                value = raw_value
            return ToolResult(
                success=True,
                data={**base, "status": "completed", "value": value},
            )
        return ToolResult(
            success=False,
            error=str(frame.get("error") or "run_js cell failed"),
            needsFollowup=True,
            data={
                **base,
                "status": "terminated" if frame.get("terminated") else "failed",
            },
        )


def register_ptc_js(
    router: ToolRouter,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Register ``run_js`` and ``wait_js`` on ``router``. Returns the names."""

    env = os.environ if environ is None else environ
    runtime = _PtcJsRuntime(env, router)
    atexit.register(runtime.close_sync)

    async def run_js(
        code: str = "",
        description: str = "",
        yieldTimeMs: int = 0,
        maxOutputChars: int = 0,
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        source = (code or "").strip()
        if not source:
            return ToolResult(success=False, error="code is empty", needsFollowup=True)
        if len(source) > _MAX_SOURCE:
            return ToolResult(
                success=False,
                error="run_js source exceeds the size cap",
                needsFollowup=True,
            )
        try:
            return await runtime.exec_cell(
                code=source,
                description=(description or "").strip() or "run_js",
                yield_ms=int(yieldTimeMs or 0) or _DEFAULT_YIELD_MS,
                max_output_chars=int(maxOutputChars or 0) or _DEFAULT_MAX_OUTPUT_CHARS,
                context=context,
            )
        except _StartError as exc:
            return exc.result

    async def wait_js(
        cellId: str = "",
        yieldTimeMs: int = 0,
        maxOutputChars: int = 0,
        terminate: bool = False,
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        del context  # session/cell resolution is by cellId, not chat.
        cell_id = (cellId or "").strip()
        if not cell_id:
            return ToolResult(success=False, error="cellId is empty", needsFollowup=True)
        try:
            return await runtime.wait_cell(
                cell_id=cell_id,
                yield_ms=int(yieldTimeMs or 0) or _DEFAULT_YIELD_MS,
                max_output_chars=int(maxOutputChars or 0) or _DEFAULT_MAX_OUTPUT_CHARS,
                terminate=bool(terminate),
            )
        except _StartError as exc:
            return exc.result

    router.register(
        run_js,
        name="run_js",
        mode="local",
        description=_RUN_JS_DESCRIPTION,
        schema=_RUN_JS_SCHEMA,
        require_consent=False,
        concurrency_safe=False,
    )
    router.register(
        wait_js,
        name="wait_js",
        mode="local",
        description=_WAIT_JS_DESCRIPTION,
        schema=_WAIT_JS_SCHEMA,
        require_consent=False,
        concurrency_safe=False,
    )
    return ["run_js", "wait_js"]
