"""Unit-test the pure wire-format helpers from the LLM providers (no network)."""

from __future__ import annotations

import json

from steerable_agent_protocol.generated import ToolCall

from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.llm.anthropic_native import (
    _openai_tool_to_anthropic,
    _split_system_and_messages,
)
from steerable_agent_runtime.llm.openai_compat import (
    _decode_tool_calls,
    _encode_message,
    _parse_stream_chunk,
)


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------


def test_openai_encode_simple_message() -> None:
    encoded = _encode_message(LLMMessage(role="user", content="hi"))
    assert encoded == {"role": "user", "content": "hi"}


def test_openai_encode_message_with_tool_calls() -> None:
    msg = LLMMessage(
        role="assistant",
        content="working on it",
        tool_calls=[ToolCall(id="call_1", name="list_events", arguments={"limit": 5})],
    )
    encoded = _encode_message(msg)
    assert encoded["tool_calls"][0]["id"] == "call_1"
    assert encoded["tool_calls"][0]["type"] == "function"
    assert encoded["tool_calls"][0]["function"]["name"] == "list_events"
    assert json.loads(encoded["tool_calls"][0]["function"]["arguments"]) == {"limit": 5}


def test_openai_encode_tool_response_message() -> None:
    msg = LLMMessage(
        role="tool",
        tool_call_id="call_1",
        name="list_events",
        content='{"items": []}',
    )
    encoded = _encode_message(msg)
    assert encoded["tool_call_id"] == "call_1"
    assert encoded["name"] == "list_events"


def test_openai_decode_tool_calls_from_completion() -> None:
    raw = [
        {
            "id": "call_42",
            "type": "function",
            "function": {"name": "create_event", "arguments": '{"title": "hi"}'},
        }
    ]
    decoded = _decode_tool_calls(raw)
    assert decoded is not None
    assert decoded[0].id == "call_42"
    assert decoded[0].name == "create_event"
    assert decoded[0].arguments == {"title": "hi"}


def test_openai_decode_tool_calls_handles_invalid_arguments() -> None:
    raw = [
        {
            "id": "x",
            "type": "function",
            "function": {"name": "thing", "arguments": "not-json"},
        }
    ]
    decoded = _decode_tool_calls(raw)
    assert decoded is not None
    assert decoded[0].arguments == {}


def test_openai_parse_stream_chunk_text_delta() -> None:
    chunk = {
        "choices": [
            {
                "delta": {"content": "hello"},
                "finish_reason": None,
            }
        ]
    }
    parsed = _parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed.content_delta == "hello"
    assert parsed.tool_call_delta is None


def test_openai_parse_stream_chunk_reasoning_and_finish() -> None:
    chunk = {
        "choices": [
            {
                "delta": {"reasoning_content": "thinking..."},
                "finish_reason": "stop",
            }
        ]
    }
    parsed = _parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed.reasoning_delta == "thinking..."
    assert parsed.finish_reason == "stop"


def test_openai_parse_stream_chunk_usage_only() -> None:
    chunk = {
        "choices": [],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
    }
    parsed = _parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed.usage is not None
    assert parsed.usage.total_tokens == 19


def test_openai_parse_stream_chunk_tool_call_delta() -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "id": "call_x",
                            "function": {
                                "name": "list_events",
                                "arguments": '{"limit": 3}',
                            },
                        }
                    ]
                },
            }
        ]
    }
    parsed = _parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed.tool_call_delta is not None
    assert parsed.tool_call_delta.name == "list_events"
    assert parsed.tool_call_delta.arguments == {"limit": 3}


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_split_system_and_messages_collects_system() -> None:
    messages = [
        LLMMessage(role="system", content="be concise"),
        LLMMessage(role="user", content="hi"),
        LLMMessage(role="system", content="also be friendly"),
    ]
    system, formatted = _split_system_and_messages(messages)
    assert system == "be concise\n\nalso be friendly"
    assert formatted == [{"role": "user", "content": "hi"}]


def test_anthropic_split_handles_tool_response() -> None:
    messages = [
        LLMMessage(
            role="tool",
            tool_call_id="call_1",
            content='{"ok": true}',
        )
    ]
    _, formatted = _split_system_and_messages(messages)
    assert formatted[0]["role"] == "user"
    assert formatted[0]["content"][0]["type"] == "tool_result"
    assert formatted[0]["content"][0]["tool_use_id"] == "call_1"


def test_anthropic_split_handles_assistant_with_tool_calls() -> None:
    messages = [
        LLMMessage(
            role="assistant",
            content="working",
            tool_calls=[ToolCall(id="call_1", name="list_events", arguments={"limit": 1})],
        )
    ]
    _, formatted = _split_system_and_messages(messages)
    blocks = formatted[0]["content"]
    assert blocks[0] == {"type": "text", "text": "working"}
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["name"] == "list_events"
    assert blocks[1]["input"] == {"limit": 1}


def test_openai_tool_to_anthropic_passes_through_when_already_native() -> None:
    native = {"name": "x", "input_schema": {"type": "object"}}
    assert _openai_tool_to_anthropic(native) == native


def test_openai_tool_to_anthropic_translates_function_form() -> None:
    openai_form = {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "make event",
            "parameters": {"type": "object", "properties": {"title": {"type": "string"}}},
        },
    }
    converted = _openai_tool_to_anthropic(openai_form)
    assert converted["name"] == "create_event"
    assert converted["description"] == "make event"
    assert converted["input_schema"]["type"] == "object"
