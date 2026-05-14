"""Spawn `python -m steerable_sidecar`, wait for ready, ping, list tools, shut down.

Mirrors what an Electron / Tauri shell does in production, but in pure
Python so it's easy to read and run anywhere uv is installed.

Run:
    uv run --package steerable-example-sidecar-roundtrip \
        python -m steerable_example_sidecar_roundtrip.main
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

READY_PREFIX = "__SIDECAR_READY__:"
SPAWN_TIMEOUT_S = 30.0


async def _read_until_ready(stream: asyncio.StreamReader) -> dict[str, Any]:
    while True:
        line = await stream.readline()
        if not line:
            raise RuntimeError("sidecar exited before emitting ready marker")
        text = line.decode("utf-8", errors="replace").rstrip()
        if text.startswith(READY_PREFIX):
            return json.loads(text[len(READY_PREFIX) :])
        # Surface stderr logs to the parent for debuggability.
        sys.stderr.write(text + "\n")


async def _send(stdin: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
    payload = (json.dumps(frame) + "\n").encode("utf-8")
    stdin.write(payload)
    await stdin.drain()


async def _recv_response(stdout: asyncio.StreamReader, request_id: int) -> dict[str, Any]:
    """Read frames until we see the response matching ``request_id``.

    The sidecar interleaves notifications (``lifecycle.ready``,
    ``stream.chunk``, etc.) with responses. Notifications carry no ``id``;
    responses carry the ``id`` of the originating request.
    """
    while True:
        line = await stdout.readline()
        if not line:
            raise RuntimeError("sidecar closed stdout unexpectedly")
        frame = json.loads(line.decode("utf-8"))
        if frame.get("id") == request_id:
            return frame
        if "method" in frame:
            print(f"[notif] {frame['method']} {frame.get('params')}")


async def main() -> int:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "steerable_sidecar",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    try:
        ready = await asyncio.wait_for(_read_until_ready(proc.stderr), SPAWN_TIMEOUT_S)
        print(f"[ready] protocolVersion={ready.get('protocolVersion')} version={ready.get('version')}")

        await _send(proc.stdin, {"jsonrpc": "2.0", "id": 1, "method": "system.ping"})
        ping = await _recv_response(proc.stdout, 1)
        print(f"[ping]  {ping.get('result')}")

        await _send(proc.stdin, {"jsonrpc": "2.0", "id": 2, "method": "tool.list"})
        tools = await _recv_response(proc.stdout, 2)
        print(f"[tools] {tools.get('result')}")

        await _send(proc.stdin, {"jsonrpc": "2.0", "id": 3, "method": "system.shutdown"})
        shutdown = await _recv_response(proc.stdout, 3)
        print(f"[bye]   {shutdown.get('result')}")
    finally:
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    return proc.returncode or 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
