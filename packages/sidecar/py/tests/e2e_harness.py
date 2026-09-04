"""Shared harness for the sidecar's real-process (e2e) tests.

The ``test_e2e_*.py`` modules spawn the actual sidecar entrypoints as
subprocesses and drive them over stdio JSON-RPC (NDJSON framing), with the
LLM replaced by a loopback mock of ``POST /chat/completions`` speaking
OpenAI SSE. This module provides the shared pieces:

- the environment gate (``check_e2e_gate``). Real-process tests self-skip
  with a visible reason when the Python environment cannot host them and
  honor ``CI_SKIP_SIDECAR_SUBPROCESS=1`` as an explicit opt-out; CI sets
  ``STEERABLE_E2E_REQUIRED=1`` to turn either skip into a hard failure so
  the coverage cannot silently rot.
- ``MockOpenAI`` — a deterministic loopback ``ThreadingHTTPServer``
  scripted per test; every request body is captured so tests assert what
  the model was offered and what it saw.
- ``SidecarClient`` — spawn + ready-marker wait + request/response +
  notification capture + guaranteed teardown for one sidecar process.
- ``child_env`` — the scrubbed child-process environment: no provider keys,
  no deployment knobs, loopback pinned off any HTTP proxy, token
  calibration redirected into the test's tmp dir.

Nothing here touches the network beyond 127.0.0.1, and every spawned
process is terminated in teardown even when the test fails.

This is a plain module (not conftest) so test files import it by name; the
directory's conftest.py puts this directory on ``sys.path`` and exposes the
fixtures.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

E2E_REQUIRED_ENV = "STEERABLE_E2E_REQUIRED"
SUBPROCESS_SKIP_ENV = "CI_SKIP_SIDECAR_SUBPROCESS"

TESTS_DIR = Path(__file__).resolve().parent


def e2e_required() -> bool:
    return os.environ.get(E2E_REQUIRED_ENV) == "1"


def skip_or_fail(reason: str) -> None:
    """Skip visibly, or fail hard when CI marked e2e coverage required."""
    if e2e_required():
        pytest.fail(f"{E2E_REQUIRED_ENV}=1 forbids skipping e2e coverage: {reason}")
    pytest.skip(reason)


def _sidecar_importable() -> bool:
    try:
        probe = subprocess.run(
            [sys.executable, "-c", "import steerable_sidecar"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def check_e2e_gate() -> None:
    """The environment gate every real-process e2e test passes through."""
    if os.environ.get(SUBPROCESS_SKIP_ENV) == "1":
        skip_or_fail(f"explicitly disabled via {SUBPROCESS_SKIP_ENV}")
    if not _sidecar_importable():
        skip_or_fail(
            f"{sys.executable} cannot import steerable_sidecar (run `uv sync`)"
        )


#: Deployment knobs and credentials a developer's shell could leak into a
#: spawned sidecar; every e2e child starts from a known-clean slate and the
#: test re-adds exactly what it varies.
_SCRUBBED_ENV = (
    "STEERABLE_API_KEY",
    "STEERABLE_BASE_URL",
    "STEERABLE_MODEL",
    "STEERABLE_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "DEEPSEEK_API_KEY",
    "TAVILY_API_KEY",
    "STEERABLE_WEB_SEARCH_API_KEY",
    "STEERABLE_WEB_SEARCH_PROVIDER",
    "STEERABLE_WEB_SEARCH_BASE_URL",
    "STEERABLE_WEB_FETCH_TIMEOUT_MS",
    "STEERABLE_WEB_FETCH_MAX_BYTES",
    "STEERABLE_WEB_FETCH_MAX_REDIRECTS",
    "STEERABLE_WEB_SEARCH_TIMEOUT_MS",
    "STEERABLE_WEB_SEARCH_MAX_RESULTS",
    "STEERABLE_EGRESS_CONFINED",
    "STEERABLE_RUN_CODE",
    "STEERABLE_RUN_CODE_TIMEOUT_MS",
    "STEERABLE_EGRESS_PROXY",
    "STEERABLE_SIDECAR_CORELOOP",
    "STEERABLE_SIDECAR_SPILL",
    "STEERABLE_SIDECAR_SUMMARIZER",
    "STEERABLE_RETRY_MAX_ATTEMPTS",
    "STEERABLE_RETRY_BASE_DELAY_MS",
    "STEERABLE_RETRY_MAX_DELAY_MS",
    "STEERABLE_REQUEST_RECORD_PATH",
    "STEERABLE_CACHE_CONTROL",
    "STEERABLE_SOFT_TIMEOUT_MS",
    "STEERABLE_HARD_TIMEOUT_SEC",
    "STEERABLE_IDLE_STREAM_TIMEOUT_MS",
    "STEERABLE_IDLE_STREAM_MAX_CHARS",
    "STEERABLE_REASONING_WITHOUT_PROGRESS_CHARS",
    "STEERABLE_TEMPERATURE",
    "STEERABLE_MAX_TOKENS",
    "STEERABLE_SPILL_DIR",
    "STEERABLE_TOKEN_CALIBRATION",
)


def child_env(
    tmp_path: Path, overrides: dict[str, str | None] | None = None
) -> dict[str, str]:
    """The scrubbed environment for a spawned sidecar (see module docstring)."""
    env = dict(os.environ)
    for name in _SCRUBBED_ENV:
        env.pop(name, None)
    # A machine-wide HTTP proxy must not hijack the loopback mock. The
    # provider's macOS system-proxy bypass (llm/system_proxy.py) covers the
    # no-env-proxy case; NO_PROXY covers a proxy configured via the
    # environment.
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    # Token calibration stays live but writes into the test's tmp dir, not
    # the developer's ~/.steerable.
    env["STEERABLE_TOKEN_CALIBRATION_PATH"] = str(tmp_path / "token-calibration.json")
    for key, value in (overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


# ---------------------------------------------------------------------------
# Mock OpenAI server
# ---------------------------------------------------------------------------

#: Responder contract: (request body, zero-based request index) -> SSE payloads.
MockResponder = Callable[[dict[str, Any], int], list[dict[str, Any]]]


def sse_tool_call(
    name: str, arguments: dict[str, Any], *, call_id: str
) -> list[dict[str, Any]]:
    """One assistant turn that calls a tool (single-chunk arguments)."""
    return [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def sse_text(text: str) -> list[dict[str, Any]]:
    """One assistant turn of plain content with a stop finish."""
    return [
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


class MockOpenAI:
    """Loopback mock of ``POST /chat/completions`` speaking OpenAI SSE.

    Bound to 127.0.0.1 on a kernel-assigned port; every request body lands
    in ``requests`` for post-hoc assertions. The responder is consulted per
    request, so a script can react to what the loop sent (e.g. branch on the
    tool result now present in the transcript).
    """

    def __init__(self, responder: MockResponder) -> None:
        self.requests: list[dict[str, Any]] = []
        self._responder = responder

        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
                if not self.path.endswith("/chat/completions"):
                    self.send_error(404)
                    return
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                payloads = outer._responder(body, len(outer.requests) - 1)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for payload in payloads:
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                # HTTP/1.0 close-delimited: the provider reads to EOF.
                self.close_connection = True

            def log_message(self, *_args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/v1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Sidecar process client
# ---------------------------------------------------------------------------


class SidecarRPCError(Exception):
    """A JSON-RPC error response from the sidecar, with the error dict."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(str(error.get("message") or error))
        self.error = error


class SidecarClient:
    """Async JSON-RPC client driving one spawned sidecar over stdio NDJSON.

    A background reader task routes frames: responses resolve pending
    requests by id, notifications accumulate in ``notifications``, and
    reverse (sidecar -> host) requests get a method_not_found reply so a
    sidecar never wedges awaiting a handler these tests do not provide.
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.notifications: list[dict[str, Any]] = []
        self.stderr_lines: list[str] = []
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._notify_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    @classmethod
    async def spawn(
        cls,
        argv: list[str],
        *,
        env: dict[str, str],
        wait_ready: bool = True,
        ready_timeout: float = 20.0,
    ) -> "SidecarClient":
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            # Mirror the sidecar's own raised reader limit: a big tool.list
            # or record payload must not LimitOverrun the test side either.
            limit=4 * 1024 * 1024,
        )
        client = cls(proc)
        client._reader_task = asyncio.ensure_future(client._read_loop())
        client._stderr_task = asyncio.ensure_future(client._stderr_loop())
        if wait_ready:
            await client.wait_ready(ready_timeout)
        return client

    async def wait_ready(self, timeout: float) -> None:
        """Wait for the stderr ready marker and the lifecycle.ready frame."""
        await asyncio.wait_for(self._ready_event.wait(), timeout)
        await self.wait_for_notification("lifecycle.ready", timeout=timeout)

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> Any:
        response = await self.request_raw(method, params, timeout=timeout)
        if "error" in response:
            raise SidecarRPCError(response["error"])
        return response.get("result")

    async def request_raw(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        await self._write(frame)
        return await asyncio.wait_for(future, timeout)

    async def wait_for_notification(
        self,
        method: str,
        *,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Return the params of the first matching notification."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for payload in self.notifications:
                params = payload.get("params") or {}
                if payload.get("method") == method and (
                    predicate is None or predicate(params)
                ):
                    return params
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"no {method} notification within {timeout}s; "
                    f"seen: {[p.get('method') for p in self.notifications]}; "
                    f"stderr: {''.join(self.stderr_lines)[-2000:]}"
                )
            self._notify_event.clear()
            try:
                await asyncio.wait_for(self._notify_event.wait(), remaining)
            except asyncio.TimeoutError:
                continue

    async def aclose(self) -> None:
        """Terminate the child: stdin EOF, then a kill backstop."""
        try:
            if self.proc.returncode is None:
                try:
                    assert self.proc.stdin is not None
                    self.proc.stdin.close()
                except (BrokenPipeError, ProcessLookupError, AssertionError):
                    pass
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self.proc.kill()
                    await self.proc.wait()
        finally:
            for task in (self._reader_task, self._stderr_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(t for t in (self._reader_task, self._stderr_task) if t is not None),
                return_exceptions=True,
            )

    # ------------------------------------------------------------------

    async def _write(self, frame: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        async with self._write_lock:
            self.proc.stdin.write((json.dumps(frame) + "\n").encode())
            await self.proc.stdin.drain()

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)

    async def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    self._fail_pending(RuntimeError("sidecar closed stdout"))
                    return
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    self.stderr_lines.append(f"[non-json stdout] {text[:200]}\n")
                    continue
                self._route(payload)
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as exc:  # the reader must never die silently
            self._fail_pending(exc)
        finally:
            self._notify_event.set()

    def _route(self, payload: dict[str, Any]) -> None:
        if "method" in payload and "id" in payload:
            # Reverse request: no handler in these tests — answer
            # method_not_found so the sidecar never wedges awaiting one.
            asyncio.ensure_future(
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {
                            "code": -32601,
                            "message": "test client provides no reverse handler",
                        },
                    }
                )
            )
            return
        if "method" in payload:
            self.notifications.append(payload)
            self._notify_event.set()
            return
        if "id" in payload:
            future = self._pending.pop(payload["id"], None)
            if future is not None and not future.done():
                future.set_result(payload)

    async def _stderr_loop(self) -> None:
        assert self.proc.stderr is not None
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace")
                self.stderr_lines.append(text)
                if text.startswith("__SIDECAR_READY__:"):
                    self._ready_event.set()
        finally:
            # EOF before the marker means the child died during boot; either
            # way wait_ready must stop blocking.
            self._ready_event.set()
