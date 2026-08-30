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
    _OpenAIToolCallAssembler,
    _decode_tool_calls,
    _encode_message,
    _parse_stream_chunk,
    _sanitize_tool_name,
    _stream_timeout,
    _timeout_sec,
)


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------


def test_openai_encode_simple_message() -> None:
    encoded = _encode_message(LLMMessage.text_of("user", "hi"))
    assert encoded == {"role": "user", "content": "hi"}


def test_openai_encode_message_with_tool_calls() -> None:
    msg = LLMMessage.text_of(
        "assistant",
        "working on it",
        tool_calls=[ToolCall(id="call_1", name="list_events", arguments={"limit": 5})],
    )
    encoded = _encode_message(msg)
    assert encoded["tool_calls"][0]["id"] == "call_1"
    assert encoded["tool_calls"][0]["type"] == "function"
    assert encoded["tool_calls"][0]["function"]["name"] == "list_events"
    assert json.loads(encoded["tool_calls"][0]["function"]["arguments"]) == {"limit": 5}


def test_openai_encode_tool_response_message() -> None:
    msg = LLMMessage.text_of(
        "tool",
        '{"items": []}',
        tool_call_id="call_1",
        name="list_events",
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


def test_openai_parse_stream_chunk_openrouter_reasoning_field() -> None:
    chunk = {
        "choices": [
            {
                "delta": {"reasoning": "plan the edit"},
                "finish_reason": None,
            }
        ]
    }
    parsed = _parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed.reasoning_delta == "plan the edit"


def test_openai_parse_stream_chunk_usage_only() -> None:
    chunk = {
        "choices": [],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
    }
    parsed = _parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed.usage is not None
    assert parsed.usage.total_tokens == 19


def test_openai_stream_requests_usage_chunk() -> None:
    """Streaming must ask for the final usage chunk — without it budget
    accounting (loop consumes chunk.usage) and usage calibration are blind."""
    from steerable_agent_runtime.llm.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider(name="t", model="m", base_url="http://x/v1")
    stream_body = provider._build_body(
        messages=[LLMMessage.text_of("user", "hi")],
        tools=None,
        temperature=None,
        max_tokens=None,
        stream=True,
        extra={},
    )
    assert stream_body["stream_options"] == {"include_usage": True}
    complete_body = provider._build_body(
        messages=[LLMMessage.text_of("user", "hi")],
        tools=None,
        temperature=None,
        max_tokens=None,
        stream=False,
        extra={},
    )
    assert "stream_options" not in complete_body


def test_openai_build_body_reasoning_effort_from_env(monkeypatch) -> None:
    """W6-8: the env-requested effort is clamped to the model's structured
    reasoning levels — applied when the model has a reasoning knob, omitted
    for models that don't (never an unsupported parameter)."""
    from steerable_agent_runtime.llm.openai_compat import OpenAICompatProvider

    monkeypatch.setenv("STEERABLE_REASONING_EFFORT", "low")

    def build(model: str, extra: dict | None = None) -> dict:
        provider = OpenAICompatProvider(name="t", model=model, base_url="http://x/v1")
        return provider._build_body(
            messages=[LLMMessage.text_of("user", "hi")],
            tools=None,
            temperature=None,
            max_tokens=None,
            stream=True,
            extra=extra or {},
        )

    # A reasoning-capable model gets the (supported) env effort.
    assert build("deepseek-reasoner")["reasoning_effort"] == "low"
    # A model with no reasoning knob gets no reasoning parameter at all.
    assert "reasoning_effort" not in build("deepseek-chat")
    assert "reasoning_effort" not in build("m")  # unknown model → no knob
    # An explicit per-request effort always wins over the env default.
    assert build("deepseek-reasoner", {"reasoning_effort": "max"})["reasoning_effort"] == "max"


def test_openai_build_body_glm_z_ai_thinking_and_max(monkeypatch) -> None:
    from steerable_agent_runtime.llm.openai_compat import OpenAICompatProvider

    monkeypatch.setenv("STEERABLE_REASONING_EFFORT", "max")
    zai = OpenAICompatProvider(
        name="t",
        model="z-ai/glm-5.3-flash",
        base_url="https://api.z.ai/api/coding/paas/v4",
    )
    body = zai._build_body(
        messages=[LLMMessage.text_of("user", "hi")],
        tools=None,
        temperature=None,
        max_tokens=None,
        stream=True,
        extra={},
    )
    assert body["reasoning_effort"] == "max"
    assert body["thinking"] == {"type": "enabled"}
    assert body["tool_stream"] is True
    other = OpenAICompatProvider(
        name="t", model="z-ai/glm-5.3-flash", base_url="https://openrouter.ai/api/v1"
    )
    openrouter = other._build_body(
        messages=[LLMMessage.text_of("user", "hi")],
        tools=None,
        temperature=None,
        max_tokens=None,
        stream=True,
        extra={},
    )
    assert openrouter["reasoning_effort"] == "max"
    assert "thinking" not in openrouter
    assert "tool_stream" not in openrouter


def test_stream_timeout_is_idle_read_not_infinite(monkeypatch) -> None:
    monkeypatch.delenv("STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("STEERABLE_LLM_CONNECT_TIMEOUT_SEC", raising=False)
    timeout = _stream_timeout()
    assert timeout.read == 300.0
    assert timeout.connect == 30.0
    monkeypatch.setenv("STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", "120")
    assert _stream_timeout().read == 120.0
    assert _timeout_sec("STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", 300.0) == 120.0
    monkeypatch.setenv("STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", "0")
    assert _timeout_sec("STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", 300.0) == 300.0
    monkeypatch.setenv("STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", "nope")
    assert _timeout_sec("STEERABLE_LLM_STREAM_READ_TIMEOUT_SEC", 300.0) == 300.0


def test_openai_tool_call_assembler_concatenates_argument_fragments() -> None:
    assembler = _OpenAIToolCallAssembler()
    assembler.observe(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_x",
                                "function": {"name": "bash", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        }
    )
    assembler.observe(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"com'}}
                        ]
                    }
                }
            ]
        }
    )
    assembler.observe(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": 'mand": "git status"}'},
                            }
                        ]
                    }
                }
            ]
        }
    )
    calls = assembler.flush()
    assert calls == [
        ToolCall(id="call_x", name="bash", arguments={"command": "git status"})
    ]
    assert assembler.flush() == []


def test_openai_tool_call_assembler_keeps_parallel_indices() -> None:
    assembler = _OpenAIToolCallAssembler()
    assembler.observe(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "c1",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "b"}',
                                },
                            },
                            {
                                "index": 0,
                                "id": "c0",
                                "function": {
                                    "name": "bash",
                                    "arguments": '{"command": "pwd"}',
                                },
                            },
                        ]
                    }
                }
            ]
        }
    )
    calls = assembler.flush()
    assert [c.name for c in calls] == ["bash", "read_file"]
    assert calls[0].arguments == {"command": "pwd"}
    assert calls[1].arguments == {"path": "b"}


def test_openai_parse_stream_chunk_ignores_string_json_fragments() -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "id": "call_x",
                            "function": {"name": "bash", "arguments": '":"'},
                        }
                    ]
                },
            }
        ]
    }
    parsed = _parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed.tool_call_delta is not None
    assert parsed.tool_call_delta.name == "bash"
    assert parsed.tool_call_delta.arguments == {}


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
# Harmony marker leakage (gpt-oss via OpenAI-compat shims)
# ---------------------------------------------------------------------------


def test_sanitize_tool_name_passes_clean_names_through() -> None:
    assert _sanitize_tool_name("exec_command") == "exec_command"
    assert _sanitize_tool_name("") == ""


def test_sanitize_tool_name_strips_markers_and_recovers_name() -> None:
    assert _sanitize_tool_name("exec_command<|channel|>commentary") == "exec_command"
    assert (
        _sanitize_tool_name("to=functions.exec_command<|channel|>commentary")
        == "exec_command"
    )


def test_sanitize_tool_name_strips_unrecoverable_leak_to_residue() -> None:
    # Production sample: only the <|constrain|> value survived — the real tool
    # name is unrecoverable, but the markers still come out so downstream
    # fuzzy matching / logging see a clean token.
    assert _sanitize_tool_name("json<|channel|>commentary") == "json"


def test_openai_decode_tool_calls_sanitizes_harmony_leak() -> None:
    raw = [
        {
            "id": "call_9",
            "type": "function",
            "function": {
                "name": "to=functions.create_event<|channel|>commentary",
                "arguments": '{"title": "hi"}',
            },
        }
    ]
    decoded = _decode_tool_calls(raw)
    assert decoded is not None
    assert decoded[0].name == "create_event"
    assert decoded[0].arguments == {"title": "hi"}


def test_openai_parse_stream_chunk_sanitizes_harmony_leak() -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "id": "call_y",
                            "function": {
                                "name": "list_events<|channel|>commentary",
                                "arguments": "{}",
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


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_split_system_and_messages_collects_system() -> None:
    messages = [
        LLMMessage.text_of("system", "be concise"),
        LLMMessage.text_of("user", "hi"),
        LLMMessage.text_of("system", "also be friendly"),
    ]
    system, formatted = _split_system_and_messages(messages)
    assert system == "be concise\n\nalso be friendly"
    assert formatted == [{"role": "user", "content": "hi"}]


def test_anthropic_split_handles_tool_response() -> None:
    messages = [
        LLMMessage.text_of(
            "tool",
            '{"ok": true}',
            tool_call_id="call_1",
        )
    ]
    _, formatted = _split_system_and_messages(messages)
    assert formatted[0]["role"] == "user"
    assert formatted[0]["content"][0]["type"] == "tool_result"
    assert formatted[0]["content"][0]["tool_use_id"] == "call_1"


def test_anthropic_split_handles_assistant_with_tool_calls() -> None:
    messages = [
        LLMMessage.text_of(
            "assistant",
            "working",
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
