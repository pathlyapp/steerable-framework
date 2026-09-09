"""Wire-format tests for the OpenAI Responses provider."""

from __future__ import annotations

import pytest

from steerable_agent_runtime import LLMMessage, LLMUsage
from steerable_agent_runtime.llm.openai_responses import (
    OpenAIResponsesProvider,
    _decode_response,
    _encode_input,
    _parse_response_event,
    _parse_usage,
    _ResponsesToolCallAssembler,
)


def _provider(**kwargs) -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider(
        name="openai",
        model="gpt-5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        **kwargs,
    )


class TestBuildBody:
    def test_system_messages_become_instructions(self) -> None:
        body = _provider()._build_body(
            messages=[
                LLMMessage.text_of("system", "be terse"),
                LLMMessage.text_of("system", "be correct"),
                LLMMessage.text_of("user", "hi"),
            ],
            tools=None,
            temperature=None,
            max_tokens=None,
            stream=True,
            extra={},
        )
        assert body["instructions"] == "be terse\n\nbe correct"
        assert [item["role"] for item in body["input"]] == ["user"]

    def test_user_message_uses_input_text_parts(self) -> None:
        body = _provider()._build_body(
            messages=[LLMMessage.text_of("user", "hi")],
            tools=None,
            temperature=None,
            max_tokens=None,
            stream=False,
            extra={},
        )
        assert body["input"] == [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }
        ]

    def test_assistant_tool_calls_become_function_call_items(self) -> None:
        from steerable_agent_protocol.generated import ToolCall

        body = _provider()._build_body(
            messages=[
                LLMMessage.text_of(
                    "assistant",
                    "checking",
                    tool_calls=[ToolCall(id="call_1", name="exec", arguments={"cmd": "ls"})],
                ),
                LLMMessage.text_of(
                    "tool", "file.txt", name="exec", tool_call_id="call_1"
                ),
            ],
            tools=None,
            temperature=None,
            max_tokens=None,
            stream=False,
            extra={},
        )
        assert body["input"] == [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "checking"}],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "exec",
                "arguments": '{"cmd": "ls"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "file.txt",
            },
        ]

    def test_reasoning_details_round_trip_unmodified(self) -> None:
        reasoning_item = {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": "blob",
        }
        body = _provider()._build_body(
            messages=[
                LLMMessage.text_of(
                    "assistant", "done", reasoning_details=[reasoning_item]
                ),
            ],
            tools=None,
            temperature=None,
            max_tokens=None,
            stream=False,
            extra={},
        )
        assert body["input"][0] is reasoning_item

    def test_tools_are_flattened(self) -> None:
        body = _provider()._build_body(
            messages=[LLMMessage.text_of("user", "hi")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "description": "run",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            temperature=None,
            max_tokens=None,
            stream=False,
            extra={},
        )
        assert body["tools"] == [
            {
                "type": "function",
                "name": "exec",
                "description": "run",
                "parameters": {"type": "object"},
            }
        ]

    def test_store_disabled_and_encrypted_reasoning_included(self) -> None:
        body = _provider()._build_body(
            messages=[LLMMessage.text_of("user", "hi")],
            tools=None,
            temperature=None,
            max_tokens=None,
            stream=True,
            extra={},
        )
        assert body["store"] is False
        assert body["include"] == ["reasoning.encrypted_content"]

    def test_reasoning_effort_maps_to_reasoning_object(self) -> None:
        body = _provider()._build_body(
            messages=[LLMMessage.text_of("user", "hi")],
            tools=None,
            temperature=None,
            max_tokens=1024,
            stream=False,
            extra={},
        )
        assert body["max_output_tokens"] == 1024
        # gpt-5 supports reasoning; the clamped env/preset effort lands here.
        assert "max_tokens" not in body

    def test_extra_kwargs_win_over_presets(self) -> None:
        body = _provider()._build_body(
            messages=[LLMMessage.text_of("user", "hi")],
            tools=None,
            temperature=0.3,
            max_tokens=None,
            stream=False,
            extra={"top_p": 0.5},
        )
        assert body["temperature"] == 0.3
        assert body["top_p"] == 0.5


class TestToolCallAssembler:
    def test_arguments_split_across_deltas(self) -> None:
        assembler = _ResponsesToolCallAssembler()
        assembler.observe(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "exec",
                    "arguments": "",
                },
            }
        )
        assembler.observe(
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "delta": '{"cmd": "ec',
            }
        )
        assembler.observe(
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "delta": 'ho hi"}',
            }
        )
        calls = assembler.flush()
        assert len(calls) == 1
        assert calls[0].id == "call_1"
        assert calls[0].name == "exec"
        assert calls[0].arguments == {"cmd": "echo hi"}

    def test_done_event_supplies_the_full_arguments(self) -> None:
        assembler = _ResponsesToolCallAssembler()
        assembler.observe(
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"type": "function_call", "call_id": "c", "name": "read"},
            }
        )
        assembler.observe(
            {
                "type": "response.function_call_arguments.done",
                "output_index": 1,
                "arguments": '{"path": "/tmp"}',
            }
        )
        calls = assembler.flush()
        assert calls[0].arguments == {"path": "/tmp"}

    def test_non_function_items_are_ignored(self) -> None:
        assembler = _ResponsesToolCallAssembler()
        assembler.observe(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "message", "role": "assistant", "content": []},
            }
        )
        assert assembler.flush() == []


class TestParseResponseEvent:
    def test_text_delta(self) -> None:
        chunk = _parse_response_event(
            {"type": "response.output_text.delta", "delta": "hel"}
        )
        assert chunk is not None
        assert chunk.content_delta == "hel"

    def test_reasoning_delta(self) -> None:
        chunk = _parse_response_event(
            {"type": "response.reasoning_text.delta", "delta": "thinking"}
        )
        assert chunk is not None
        assert chunk.reasoning_delta == "thinking"

    def test_reasoning_item_done_surfaces_details(self) -> None:
        item = {"type": "reasoning", "id": "rs_1", "encrypted_content": "x"}
        chunk = _parse_response_event(
            {"type": "response.output_item.done", "item": item}
        )
        assert chunk is not None
        assert chunk.reasoning_details == [item]

    def test_completed_carries_usage_and_finish(self) -> None:
        chunk = _parse_response_event(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                        "input_tokens_details": {"cached_tokens": 4},
                    },
                },
            }
        )
        assert chunk is not None
        assert chunk.finish_reason == "stop"
        assert chunk.usage == LLMUsage(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cached_prompt_tokens=4,
        )

    def test_incomplete_max_output_maps_to_length(self) -> None:
        chunk = _parse_response_event(
            {
                "type": "response.completed",
                "response": {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            }
        )
        assert chunk is not None
        assert chunk.finish_reason == "length"

    def test_unrelated_events_are_skipped(self) -> None:
        assert _parse_response_event({"type": "response.created"}) is None
        assert (
            _parse_response_event(
                {
                    "type": "response.output_item.done",
                    "item": {"type": "message", "content": []},
                }
            )
            is None
        )


class TestDecodeResponse:
    def test_full_payload(self) -> None:
        message, usage = _decode_response(
            {
                "status": "completed",
                "output": [
                    {"type": "reasoning", "id": "rs_1"},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "hello "},
                            {"type": "output_text", "text": "world"},
                        ],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_9",
                        "name": "exec",
                        "arguments": '{"cmd": "ls"}',
                    },
                ],
                "usage": {"input_tokens": 3, "output_tokens": 7, "total_tokens": 10},
            }
        )
        assert message.role == "assistant"
        assert message.content_text == "hello world"
        assert message.reasoning_details == [{"type": "reasoning", "id": "rs_1"}]
        assert len(message.tool_calls or []) == 1
        assert (message.tool_calls or [])[0].name == "exec"
        assert usage.total_tokens == 10


class TestEncodeInput:
    def test_empty_transcript(self) -> None:
        instructions, items = _encode_input([])
        assert instructions == ""
        assert items == []


@pytest.mark.asyncio
async def test_provider_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        OpenAIResponsesProvider(name="x", model="m", base_url="")
