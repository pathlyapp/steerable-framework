"""params.worldState wiring on the CoreLoop chat path: the host passes
slow-changing context as plain per-section data and the loop injects it as
a world-state fragment (full on first sight, RFC 7386 tail patch on
change) — instead of the host rebuilding its system prompt every turn.
"""

from __future__ import annotations

import json

import pytest
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage

from steerable_sidecar.sidecar import Sidecar


class _ScriptedProvider:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, script: list[list[LLMStreamChunk]]):
        self._script = script
        self._round = 0
        self.seen_messages: list[list] = []

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        self.seen_messages.append(list(messages))
        chunks = self._script[min(self._round, len(self._script) - 1)]

        async def _gen():
            self._round += 1
            for chunk in chunks:
                yield chunk

        return _gen()


def _text_round(text: str) -> list[LLMStreamChunk]:
    return [
        LLMStreamChunk(content_delta=text),
        LLMStreamChunk(
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=5, completion_tokens=1, total_tokens=6),
        ),
    ]


class _CapturingTransport:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit_notification(self, method: str, params: dict | None = None) -> None:
        self.events.append((method, params or {}))

    async def aclose(self) -> None:
        return None


def _make_sidecar(provider: _ScriptedProvider) -> Sidecar:
    sidecar = Sidecar(llm_provider_factory=lambda _params: provider)
    sidecar._transport = _CapturingTransport()  # type: ignore[attr-defined]
    return sidecar


async def _run_stream(sidecar: Sidecar, params: dict) -> list[tuple[str, dict]]:
    response = await sidecar.server.handle_frame(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "agent.chat.stream", "params": params})
    )
    assert "error" not in response, response
    stream_id = response["result"]["streamId"]
    task = sidecar._streams.get(stream_id)
    if task is not None:
        await task
    return sidecar._transport.events  # type: ignore[attr-defined]


def _params(**overrides) -> dict:
    base = {
        "provider": "openai_compat",
        "model": "fake",
        "useCoreLoop": True,
        "messages": [
            {"role": "system", "content": "BASE"},
            {"role": "user", "content": "hi"},
        ],
        "worldState": {
            "current-time": {"iso": "2026-08-29T06:00:00+08:00"},
            "workspace": {"cwd": "/app"},
        },
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_world_state_param_injects_full_state_fragment() -> None:
    provider = _ScriptedProvider([_text_round("done")])
    sidecar = _make_sidecar(provider)
    events = await _run_stream(sidecar, _params())

    # The base system prompt is untouched; the world state is a separate
    # user-role fragment appended after the user message.
    first_request = provider.seen_messages[0]
    assert first_request[0].role == "system"
    assert first_request[0].content_text == "BASE"
    fragments = [m for m in first_request if m.content_text.startswith("<world-state>")]
    assert len(fragments) == 1
    body = fragments[0].content_text
    assert '"current-time"' in body and '"workspace"' in body

    # The injection is observable on the wire as a hook_action notice.
    notices = [
        p["notice"]
        for m, p in events
        if m == "stream.chunk" and p.get("notice", {}).get("kind") == "hook_action"
    ]
    assert any(n.get("action") == "world_state" for n in notices), notices


@pytest.mark.asyncio
async def test_no_world_state_param_means_no_fragment() -> None:
    provider = _ScriptedProvider([_text_round("done")])
    sidecar = _make_sidecar(provider)
    params = _params()
    del params["worldState"]
    await _run_stream(sidecar, params)

    first_request = provider.seen_messages[0]
    assert not any(
        m.content_text.startswith("<world-state") for m in first_request
    )
