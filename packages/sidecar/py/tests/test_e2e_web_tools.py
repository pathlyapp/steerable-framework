"""E2E: web_search / web_fetch wiring in a real sidecar process.

Layer choice: the hermetic unit tests (``test_web_tools.py``) inject
``httpx.MockTransport`` through the ``client_factory`` / ``resolve_host``
seams — seams that do not cross a process boundary. What only a real
spawned sidecar proves is that the production wiring actually engages those
code paths: that ``__main__`` registers the tools, that the SSRF guard runs
inside the real dispatch path (a loopback target is rejected without the
server ever being contacted), that ``STEERABLE_EGRESS_CONFINED=1`` in the
child's environment fails both tools loud before any network I/O, and that
the headless ``--no-web-tools`` flag removes the pair from the tool list
the model is offered.

No test here makes a real network call: the fetch targets are literal
loopback/private/NAT64-disguised addresses the guard rejects before
connecting, and the search backend URL is pointed at the same loopback
probe server, so even a regressed guard would fail the assertions on
loopback rather than escape to the internet.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from e2e_harness import child_env, sse_text


class _ProbeServer:
    """Loopback HTTP server that counts requests — the "was the network
    touched?" witness for the SSRF and egress-confinement tests."""

    def __init__(self) -> None:
        self.hits = 0
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
                outer.hits += 1
                self.send_response(200)
                self.send_header("content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"probe-ok")

            def do_POST(self) -> None:  # noqa: N802
                outer.hits += 1
                self.send_response(404)
                self.end_headers()

            def log_message(self, *_args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


async def _run_headless(
    args: list[str], env: dict[str, str], *, timeout: float = 90.0
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "steerable_sidecar.headless",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stdout.decode(), stderr.decode()


def _tool_names(tool_list: list[dict[str, Any]]) -> set[str]:
    return {(t.get("function") or {}).get("name") or "" for t in tool_list}


async def test_default_sidecar_registers_web_fetch_only_without_search_key(
    e2e_gate: None, sidecar_factory: Any
) -> None:
    """Default registration: web_fetch always; web_search needs a backend."""
    client = await sidecar_factory()
    names = _tool_names(await client.request("tool.list"))
    assert "web_fetch" in names
    assert "web_search" not in names


async def test_sidecar_registers_web_search_when_key_configured(
    e2e_gate: None, sidecar_factory: Any
) -> None:
    client = await sidecar_factory(
        env_overrides={"STEERABLE_WEB_SEARCH_API_KEY": "e2e-dummy-key"}
    )
    names = _tool_names(await client.request("tool.list"))
    assert {"web_fetch", "web_search"} <= names


async def test_web_fetch_ssrf_rejects_loopback_without_contacting_it(
    e2e_gate: None, sidecar_factory: Any
) -> None:
    """The guard fires inside the real process's dispatch path: the probe
    server bound to the target address never sees a request."""
    probe = _ProbeServer()
    try:
        client = await sidecar_factory()
        result = await client.request(
            "tool.invoke",
            {"name": "web_fetch", "arguments": {"url": f"{probe.base_url}/secret"}},
        )
        assert result["success"] is False
        assert "non-public address" in result["error"]
        assert probe.hits == 0

        # v4-in-v6 disguises of the same loopback/metadata targets are
        # unwrapped before the is_global check — still literal IPs, so no
        # DNS or connection is involved here either.
        for url in (
            "http://[::ffff:127.0.0.1]/",
            "http://[64:ff9b::a9fe:a9fe]/",  # NAT64-wrapped 169.254.169.254
            "http://192.168.0.1/",
        ):
            result = await client.request(
                "tool.invoke", {"name": "web_fetch", "arguments": {"url": url}}
            )
            assert result["success"] is False, url
            assert "non-public address" in result["error"], url
        assert probe.hits == 0
    finally:
        probe.close()


async def test_egress_confined_fails_loud_without_touching_the_network(
    e2e_gate: None, sidecar_factory: Any
) -> None:
    """STEERABLE_EGRESS_CONFINED=1: both tools fail fast with an actionable
    message instead of hanging behind the confining proxy. The search
    backend URL points at the loopback probe so even a regressed guard
    cannot escape to the internet — it would fail these assertions instead.
    """
    probe = _ProbeServer()
    try:
        client = await sidecar_factory(
            env_overrides={
                "STEERABLE_EGRESS_CONFINED": "1",
                "STEERABLE_WEB_SEARCH_API_KEY": "e2e-dummy-key",
                "STEERABLE_WEB_SEARCH_BASE_URL": probe.base_url,
            }
        )

        started = time.monotonic()
        fetch = await client.request(
            "tool.invoke",
            {"name": "web_fetch", "arguments": {"url": f"{probe.base_url}/page"}},
        )
        search = await client.request(
            "tool.invoke",
            {"name": "web_search", "arguments": {"query": "anything"}},
        )
        elapsed = time.monotonic() - started

        for result in (fetch, search):
            assert result["success"] is False
            assert "STEERABLE_EGRESS_CONFINED" in result["error"]
            assert "confined" in result["error"]
            # Actionable: names the remedies, not just the refusal.
            assert "Remedies" in result["error"]
        # Fail-loud means fast: no proxy round-trip, no retry spin.
        assert elapsed < 5.0
        assert probe.hits == 0
    finally:
        probe.close()


async def test_headless_no_web_tools_flag_removes_the_pair_from_the_model_request(
    e2e_gate: None, mock_openai: Any, tmp_path: Path
) -> None:
    """The headless flag is the registration surface for eval contracts:
    the default run offers web_fetch + web_search to the model; with
    ``--no-web-tools`` neither appears in the request's tool list."""
    mock = mock_openai(lambda _body, _index: sse_text("OK"))
    env = child_env(
        tmp_path,
        {
            "STEERABLE_MODEL": "mock-e2e",
            "STEERABLE_BASE_URL": mock.base_url,
            "STEERABLE_API_KEY": "e2e-not-a-real-key",
            "STEERABLE_WEB_SEARCH_API_KEY": "e2e-dummy-key",
        },
    )

    async def run(extra: list[str]) -> tuple[int, str, str]:
        return await _run_headless(
            [
                "--cwd",
                str(tmp_path),
                "--instruction",
                "Reply with exactly: OK",
                *extra,
            ],
            env,
        )

    code, out, err = await run([])
    assert code == 0, err[-2000:]
    assert "OK" in out
    assert "STEERABLE_RUN_SUMMARY" in out
    assert len(mock.requests) == 1
    default_tools = _tool_names(mock.requests[0].get("tools") or [])
    assert {"web_fetch", "web_search"} <= default_tools

    code, out, err = await run(["--no-web-tools"])
    assert code == 0, err[-2000:]
    assert len(mock.requests) == 2
    stripped_tools = _tool_names(mock.requests[1].get("tools") or [])
    assert "web_fetch" not in stripped_tools
    assert "web_search" not in stripped_tools
    # The workspace tools are unaffected by the flag.
    assert {"bash", "read_file", "write_file", "edit_file"} <= stripped_tools
