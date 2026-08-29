"""Wave 3: AG-UI transport — LoopEvent → AG-UI wire format projection."""

from __future__ import annotations

import json

import pytest
from ag_ui.core import EventType
from steerable_agent_runtime import CoreLoop, LoopEvent, ToolRouter, RouterToolExecutor
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk, LLMUsage
from steerable_agent_protocol.generated import ToolCall, ToolResult

from steerable_sidecar.ag_ui import AgUiRenderer, encode_sse


def _types(events) -> list[EventType]:
    return [e.type for e in events]


class TestRenderer:
    def test_text_run_lifecycle(self) -> None:
        r = AgUiRenderer("thread-1", "run-1")
        out = r.begin()
        out += r.render(LoopEvent("content_delta", {"delta": "Hello"}))
        out += r.render(LoopEvent("content_delta", {"delta": " world"}))
        out += r.render(LoopEvent("completion", {"status": "completed"}))

        types = _types(out)
        assert types == [
            EventType.RUN_STARTED,
            EventType.TEXT_MESSAGE_START,
            EventType.TEXT_MESSAGE_CONTENT,
            EventType.TEXT_MESSAGE_CONTENT,
            EventType.TEXT_MESSAGE_END,
            EventType.RUN_FINISHED,
        ]
        assert out[0].thread_id == "thread-1" and out[0].run_id == "run-1"
        # One message id across the segment's content events.
        assert out[1].message_id == out[2].message_id == out[3].message_id
        assert out[2].delta + out[3].delta == "Hello world"

    def test_reasoning_uses_reasoning_family(self) -> None:
        r = AgUiRenderer("t", "r")
        out = r.render(LoopEvent("reasoning_delta", {"delta": "hmm"}))
        out += r.render(LoopEvent("reasoning_delta", {"delta": "…"}))
        out += r.render(LoopEvent("completion", {"status": "completed"}))
        types = _types(out)
        assert types == [
            EventType.REASONING_MESSAGE_START,
            EventType.REASONING_MESSAGE_CONTENT,
            EventType.REASONING_MESSAGE_CONTENT,
            EventType.REASONING_MESSAGE_END,
            EventType.RUN_FINISHED,
        ]

    def test_tool_call_sequence(self) -> None:
        r = AgUiRenderer("t", "r")
        out = r.render(
            LoopEvent(
                "tool_call_start",
                {"id": "c1", "name": "bash", "arguments": {"command": "ls"}},
            )
        )
        out += r.render(
            LoopEvent(
                "tool_call_result",
                {"id": "c1", "name": "bash", "success": True, "resultPreview": "ok"},
            )
        )
        types = _types(out)
        assert types == [
            EventType.TOOL_CALL_START,
            EventType.TOOL_CALL_ARGS,
            EventType.TOOL_CALL_END,
            EventType.TOOL_CALL_RESULT,
        ]
        assert out[0].tool_call_id == out[3].tool_call_id == "c1"
        assert out[0].tool_call_name == "bash"
        assert json.loads(out[1].delta) == {"command": "ls"}
        assert out[3].content == "ok"

    def test_tool_error_rides_in_result_content(self) -> None:
        r = AgUiRenderer("t", "r")
        out = r.render(
            LoopEvent("tool_error", {"id": "c1", "name": "bash", "error": "boom"})
        )
        assert out[0].type == EventType.TOOL_CALL_RESULT
        assert json.loads(out[0].content) == {"success": False, "error": "boom"}

    def test_tool_call_closes_open_message_and_reopens_after(self) -> None:
        r = AgUiRenderer("t", "r")
        out = r.render(LoopEvent("content_delta", {"delta": "before"}))
        out += r.render(
            LoopEvent("tool_call_start", {"id": "c1", "name": "t", "arguments": {}})
        )
        out += r.render(LoopEvent("content_delta", {"delta": "after"}))
        types = _types(out)
        # message END before TOOL_CALL_START; a NEW message START after.
        assert types == [
            EventType.TEXT_MESSAGE_START,
            EventType.TEXT_MESSAGE_CONTENT,
            EventType.TEXT_MESSAGE_END,
            EventType.TOOL_CALL_START,
            EventType.TOOL_CALL_ARGS,
            EventType.TOOL_CALL_END,
            EventType.TEXT_MESSAGE_START,
            EventType.TEXT_MESSAGE_CONTENT,
        ]
        assert out[0].message_id != out[6].message_id

    def test_failed_completion_is_run_error(self) -> None:
        r = AgUiRenderer("t", "r")
        out = r.render(
            LoopEvent("completion", {"status": "failed", "reason": "aborted"})
        )
        assert _types(out) == [EventType.RUN_ERROR]
        assert "aborted" in out[0].message

    def test_budget_exhausted_completion_still_finishes(self) -> None:
        r = AgUiRenderer("t", "r")
        out = r.render(LoopEvent("completion", {"status": "budget_exhausted"}))
        assert _types(out) == [EventType.RUN_FINISHED]

    def test_observability_events_become_custom(self) -> None:
        r = AgUiRenderer("t", "r")
        for kind in ("stage_start", "stage_complete", "hook_action", "steer",
                     "soft_timeout", "budget_exhausted"):
            out = r.render(LoopEvent(kind, {"round": 1}))
            assert out[0].type == EventType.CUSTOM
            assert out[0].name == f"steerable.{kind}"
            assert out[0].value == {"round": 1}

    def test_loop_error_event_is_run_error(self) -> None:
        r = AgUiRenderer("t", "r")
        out = r.render(LoopEvent("error", {"message": "provider down"}))
        assert _types(out) == [EventType.RUN_ERROR]
        assert "provider down" in out[0].message


class TestSseEncoding:
    def test_encode_sse_renders_data_lines(self) -> None:
        r = AgUiRenderer("thread-9", "run-9")
        payload = encode_sse(r.begin())
        assert payload.startswith("data: ")
        assert payload.endswith("\n\n")
        body = json.loads(payload[len("data: "):])
        assert body["type"] == "RUN_STARTED"
        assert body["threadId"] == "thread-9"
        assert body["runId"] == "run-9"


class _ScriptedProvider:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, script):
        self._script = script
        self._round = 0

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        chunks = self._script[min(self._round, len(self._script) - 1)]
        self._round += 1

        async def _gen():
            for chunk in chunks:
                yield chunk

        return _gen()


@pytest.mark.asyncio
async def test_full_loop_projects_to_ag_ui() -> None:
    """End-to-end: a CoreLoop run rendered through AgUiRenderer yields a
    well-formed AG-UI event stream (the taxonomy-neutrality proof)."""
    tools = ToolRouter()

    async def get_time() -> str:
        return "noon"

    tools.register(get_time, name="get_time", mode="read", concurrency_safe=True)
    provider = _ScriptedProvider(
        [
            [
                LLMStreamChunk(
                    tool_call_delta=ToolCall(id="c1", name="get_time", arguments={})
                ),
                LLMStreamChunk(
                    finish_reason="tool_calls",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
            ],
            [
                LLMStreamChunk(content_delta="It is noon."),
                LLMStreamChunk(
                    finish_reason="stop",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
            ],
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(tools))
    renderer = AgUiRenderer("thread-x", "run-x")
    out = renderer.begin()
    async for event in loop.run([LLMMessage.text_of("user", "time?")]):
        out.extend(renderer.render(event))

    types = _types(out)
    assert types[0] == EventType.RUN_STARTED
    assert types[-1] == EventType.RUN_FINISHED
    assert EventType.TOOL_CALL_START in types
    assert EventType.TOOL_CALL_RESULT in types
    # The final assistant text arrived as content of one message.
    text = "".join(
        e.delta for e in out if e.type == EventType.TEXT_MESSAGE_CONTENT
    )
    assert text == "It is noon."
    # Every event is a declared, emittable type.
    from steerable_sidecar.ag_ui import EMITTED_EVENT_TYPES

    assert set(types) <= EMITTED_EVENT_TYPES
