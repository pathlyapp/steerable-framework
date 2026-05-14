"""End-to-end smoke test: spawn the sidecar subprocess and round-trip a frame."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("CI_SKIP_SIDECAR_SUBPROCESS") == "1",
    reason="explicitly disabled via CI_SKIP_SIDECAR_SUBPROCESS",
)


async def _spawn_sidecar() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "steerable_sidecar",
        "--log-level",
        "ERROR",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _read_ready_marker(proc: asyncio.subprocess.Process) -> dict:
    assert proc.stderr is not None
    while True:
        line = await asyncio.wait_for(proc.stderr.readline(), timeout=10.0)
        if not line:
            raise RuntimeError("sidecar exited before ready marker")
        decoded = line.decode("utf-8").rstrip()
        if decoded.startswith("__SIDECAR_READY__:"):
            return json.loads(decoded.removeprefix("__SIDECAR_READY__:"))


async def _read_lifecycle_ready(proc: asyncio.subprocess.Process) -> dict:
    assert proc.stdout is not None
    while True:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        if not line:
            raise RuntimeError("sidecar exited before lifecycle.ready notification")
        payload = json.loads(line)
        if payload.get("method") == "lifecycle.ready":
            return payload


async def _round_trip(proc: asyncio.subprocess.Process, request: dict) -> dict:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
    await proc.stdin.drain()
    while True:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        if not line:
            raise RuntimeError("sidecar closed without responding")
        payload = json.loads(line)
        if payload.get("id") == request.get("id"):
            return payload


async def test_subprocess_full_handshake_and_ping() -> None:
    proc = await _spawn_sidecar()
    try:
        ready = await _read_ready_marker(proc)
        assert ready["status"] in {"ok", "starting"}
        assert ready["protocolVersion"] == "0.1.0"

        lifecycle = await _read_lifecycle_ready(proc)
        assert lifecycle["params"]["protocolVersion"] == "0.1.0"
        assert lifecycle["params"]["pid"] > 0

        response = await _round_trip(
            proc,
            {"jsonrpc": "2.0", "id": 1, "method": "system.ping"},
        )
        assert response["result"]["protocolVersion"] == "0.1.0"

        # graceful shutdown
        await _round_trip(
            proc, {"jsonrpc": "2.0", "id": 2, "method": "system.shutdown"}
        )
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
