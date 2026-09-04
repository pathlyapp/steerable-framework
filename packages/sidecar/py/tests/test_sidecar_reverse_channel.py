"""End-to-end test of the reverse (sidecar -> host) request channel.

Spawns a test sidecar that, when the host calls ``test.run_host_tool``, issues
a reverse ``tool.invoke`` request back to the host. The host (this test) runs a
fake local tool and replies. Asserts the sidecar received the host's result.

This is the stdio analogue of what deeppath-agent's Electron supervisor does
when a sidecar-hosted loop needs a host-executed tool mid-turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CI_SKIP_SIDECAR_SUBPROCESS") == "1"
    and os.environ.get("STEERABLE_E2E_REQUIRED") != "1",
    reason="explicitly disabled via CI_SKIP_SIDECAR_SUBPROCESS",
)


def test_e2e_gate_configuration() -> None:
    """CI sets STEERABLE_E2E_REQUIRED=1 so real-process coverage cannot
    silently rot; the explicit subprocess opt-out then becomes a hard
    failure instead of a skip."""
    assert not (
        os.environ.get("CI_SKIP_SIDECAR_SUBPROCESS") == "1"
        and os.environ.get("STEERABLE_E2E_REQUIRED") == "1"
    ), (
        "CI_SKIP_SIDECAR_SUBPROCESS=1 conflicts with STEERABLE_E2E_REQUIRED=1: "
        "the subprocess e2e tests are required, remove the opt-out"
    )

_TESTS_DIR = Path(__file__).resolve().parent


async def _spawn() -> asyncio.subprocess.Process:
    # Run the test sidecar as a module from the tests directory so its
    # `reverse_echo_sidecar` import resolves without packaging.
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(_TESTS_DIR / "reverse_echo_sidecar.py"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _read_frame(proc: asyncio.subprocess.Process) -> dict:
    assert proc.stdout is not None
    while True:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        if not line:
            raise RuntimeError("sidecar closed stdout unexpectedly")
        text = line.decode("utf-8").strip()
        if text:
            return json.loads(text)


async def _send(proc: asyncio.subprocess.Process, frame: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(frame) + "\n").encode("utf-8"))
    await proc.stdin.drain()


async def test_sidecar_reverse_tool_invoke_round_trip() -> None:
    proc = await _spawn()
    try:
        # Drain stderr readiness in the background so it doesn't block.
        assert proc.stderr is not None

        async def _drain_stderr() -> None:
            async for _ in proc.stderr:
                pass

        stderr_task = asyncio.ensure_future(_drain_stderr())

        # Host -> sidecar: run a host tool via the reverse channel.
        await _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "test.run_host_tool",
                "params": {"name": "local_exec_shell", "arguments": {"command": "ls"}},
            },
        )

        # Expect: sidecar -> host reverse request (id + method = tool.invoke).
        host_response_id: str | None = None
        final_response: dict | None = None
        while final_response is None:
            frame = await _read_frame(proc)
            has_method = "method" in frame
            has_id = "id" in frame

            if has_method and has_id:
                # Reverse request from the sidecar. Execute the "host tool".
                assert frame["method"] == "tool.invoke"
                assert isinstance(frame["id"], str) and frame["id"].startswith("srv_")
                host_response_id = frame["id"]
                tool_args = frame["params"]["arguments"]
                await _send(
                    proc,
                    {
                        "jsonrpc": "2.0",
                        "id": host_response_id,
                        "result": {
                            "success": True,
                            "data": {"stdout": f"ran:{tool_args['command']}"},
                        },
                    },
                )
            elif has_id and not has_method:
                # Response to one of the host's own requests.
                if frame.get("id") == 1:
                    final_response = frame
            # else: a notification (lifecycle.ready etc.) — ignore.

        assert host_response_id is not None, "sidecar never issued a reverse request"
        assert final_response is not None
        result = final_response["result"]
        assert result["success"] is True
        assert result["data"]["stdout"] == "ran:ls"

        # Graceful shutdown.
        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "system.shutdown"})
        stderr_task.cancel()
    finally:
        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
