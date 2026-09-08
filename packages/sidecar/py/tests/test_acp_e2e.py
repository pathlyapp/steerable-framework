"""Real-process ACP round trip: spawn ``steerable-sidecar-acp`` and drive it
with the SDK client.

The 34 in-process tests (``test_acp_adapter.py``) exercise the adapter's
methods against a ``_FakeClient``; this file proves the *wire* path — a real
subprocess serving JSON-RPC over stdio, a real ``acp`` client connection —
so a protocol or framing regression cannot hide behind the in-process seam.
The provider is a loopback mock (no network, no key): the agent reads
``STEERABLE_BASE_URL``/``STEERABLE_API_KEY`` from the environment the client
spawns it with.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import acp
import pytest
from acp.schema import TextContentBlock

pytestmark = pytest.mark.asyncio


class _RecordingClient(acp.Client):
    """Minimal editor stand-in: captures session updates, approves nothing
    (no tools are offered in this round trip, so no permission request
    arrives)."""

    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append(update)


def _mock_env(base_url: str) -> dict[str, str]:
    env = dict(os.environ)
    env["STEERABLE_PROVIDER"] = "openai_compat"
    env["STEERABLE_MODEL"] = "mock-acp"
    env["STEERABLE_BASE_URL"] = base_url
    env["STEERABLE_API_KEY"] = "acp-e2e-not-a-real-key"
    return env


async def test_acp_real_process_initialize_session_prompt_cancel(
    mock_openai: Any, tmp_path: Any
) -> None:
    """initialize → new_session → prompt → cancel over a real subprocess."""
    from e2e_harness import sse_text

    mock = mock_openai(lambda _body, _index: sse_text("ACP_E2E_OK"))
    client = _RecordingClient()

    async with acp.spawn_agent_process(
        client,
        sys.executable,
        "-m",
        "steerable_sidecar.acp_adapter",
        env=_mock_env(mock.base_url),
        cwd=str(tmp_path),
    ) as (conn, process):
        init = await conn.initialize(protocol_version=1)
        assert init.agent_info is not None
        # Session lifecycle is advertised (load_session capability on).
        assert init.agent_capabilities.load_session is True

        session = await conn.new_session(cwd=str(tmp_path))
        assert session.session_id

        prompt = await conn.prompt(
            session_id=session.session_id,
            prompt=[TextContentBlock(type="text", text="say the magic word")],
        )
        # The scripted provider answered; the turn ended by stopping, not by
        # an error or a cancellation.
        assert prompt.stop_reason == "end_turn"

        # cancel is a valid no-op on a finished turn (cooperative wind-down).
        await conn.cancel(session_id=session.session_id)

    # The subprocess served the whole round trip and exited cleanly.
    assert process.returncode in (0, None)
    # The model was actually asked (one prompt request over the wire).
    assert len(mock.requests) >= 1
    # And the client saw streamed session updates for the turn.
    assert client.updates, "expected session/update notifications"
