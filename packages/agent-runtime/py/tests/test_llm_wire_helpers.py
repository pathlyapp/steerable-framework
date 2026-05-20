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
# A1.5d.1 — partial-args streaming reassembly
#
# OpenAI streams ``function.arguments`` as a JSON object split across many
# chunks (e.g. ``{"loc``, ``ation":``, ``"NYC"}``). Without a cross-chunk
# accumulator the args dictionary ends up empty (or whatever the last
# parsable fragment was). These tests pin the new contract:
#
# 1. ``tool_buf`` is the cross-chunk state.
# 2. Each parsed chunk carries the **best-effort** parse of *all* args
#    received so far for that ``index``. The accumulator never shrinks.
# 3. ``id`` and ``function.name`` arrive on the first chunk only — every
#    subsequent emit must still carry them.
# 4. The final chunk (where args_str closes the JSON object) yields the
#    complete parse.
# ---------------------------------------------------------------------------


def _tc_delta_chunk(
    *,
    index: int = 0,
    id: str | None = None,
    name: str | None = None,
    args_fragment: str | None = None,
    finish_reason: str | None = None,
) -> dict:
    delta_tc: dict = {"index": index}
    if id is not None:
        delta_tc["id"] = id
    func: dict = {}
    if name is not None:
        func["name"] = name
    if args_fragment is not None:
        func["arguments"] = args_fragment
    if func:
        delta_tc["function"] = func
    return {
        "choices": [
            {
                "delta": {"tool_calls": [delta_tc]},
                "finish_reason": finish_reason,
            }
        ]
    }


def test_partial_args_reassemble_across_three_chunks() -> None:
    """Classic OpenAI bug repro: args JSON split across 3 chunks."""
    tool_buf: dict[int, dict] = {}

    # Chunk 1: id + name + empty args.
    p1 = _parse_stream_chunk(
        _tc_delta_chunk(id="call_x", name="get_weather", args_fragment=""),
        tool_buf,
    )
    # Chunk 2: partial args, not yet parseable.
    p2 = _parse_stream_chunk(
        _tc_delta_chunk(args_fragment='{"loc'),
        tool_buf,
    )
    # Chunk 3: more partial args, still not parseable.
    p3 = _parse_stream_chunk(
        _tc_delta_chunk(args_fragment='ation": "'),
        tool_buf,
    )
    # Chunk 4: final fragment closes the JSON; finish_reason fires.
    p4 = _parse_stream_chunk(
        _tc_delta_chunk(args_fragment='NYC"}', finish_reason="tool_calls"),
        tool_buf,
    )

    assert p1 is not None and p1.tool_call_delta is not None
    assert p4 is not None and p4.tool_call_delta is not None

    # The CRITICAL assertion: by the final chunk the accumulator has the
    # full args dict. Before A1.5d.1 this would have been {} because every
    # intermediate fragment was an isolated json.loads attempt.
    assert p4.tool_call_delta.arguments == {"location": "NYC"}
    # id + name carried through despite only being on chunk 1.
    assert p4.tool_call_delta.id == "call_x"
    assert p4.tool_call_delta.name == "get_weather"
    assert p4.finish_reason == "tool_calls"

    # Intermediate emits never "lose" what they had.
    assert p1.tool_call_delta.arguments == {}
    assert p2.tool_call_delta.arguments == {}  # not parseable yet
    assert p3.tool_call_delta.arguments == {}  # still not parseable


def test_partial_args_accumulator_never_shrinks_on_invalid_fragment() -> None:
    """If a chunk arrives that makes the buffer temporarily invalid, the
    exposed ``arguments`` dict must hold onto the last successful parse
    rather than regressing to ``{}``.

    This guards against the worst-case anti-pattern where a downstream
    consumer relies on ``arguments`` being monotonic and we silently
    leak a transient blank state.
    """
    tool_buf: dict[int, dict] = {}
    # Send a single chunk with a fully-formed JSON object — happens with
    # some providers that don't actually fragment.
    _parse_stream_chunk(
        _tc_delta_chunk(id="c1", name="f", args_fragment='{"x":1}'),
        tool_buf,
    )
    assert tool_buf[0]["last_args"] == {"x": 1}

    # Now simulate a corrupt/extra fragment (defensive — should not
    # happen with real OpenAI but a misbehaving proxy might).
    p_mid = _parse_stream_chunk(_tc_delta_chunk(args_fragment="garbage"), tool_buf)
    assert p_mid is not None and p_mid.tool_call_delta is not None
    # Last good parse preserved — we did NOT regress to {}.
    assert p_mid.tool_call_delta.arguments == {"x": 1}


def test_partial_args_two_tool_calls_with_interleaved_indexes() -> None:
    """Multi-tool streams interleave chunks by ``index``. The accumulator
    must track each index independently."""
    tool_buf: dict[int, dict] = {}

    # Open tool 0 and tool 1.
    _parse_stream_chunk(
        _tc_delta_chunk(index=0, id="c0", name="get_weather", args_fragment=""),
        tool_buf,
    )
    _parse_stream_chunk(
        _tc_delta_chunk(index=1, id="c1", name="list_files", args_fragment=""),
        tool_buf,
    )
    # Interleave fragments.
    _parse_stream_chunk(_tc_delta_chunk(index=0, args_fragment='{"city":'), tool_buf)
    _parse_stream_chunk(_tc_delta_chunk(index=1, args_fragment='{"path":'), tool_buf)
    _parse_stream_chunk(_tc_delta_chunk(index=0, args_fragment='"NYC"}'), tool_buf)
    p_last = _parse_stream_chunk(
        _tc_delta_chunk(index=1, args_fragment='"/tmp"}', finish_reason="tool_calls"),
        tool_buf,
    )

    # Both indices fully assembled in the buffer.
    assert tool_buf[0]["last_args"] == {"city": "NYC"}
    assert tool_buf[1]["last_args"] == {"path": "/tmp"}
    # The last emit reflects index 1 (the chunk we just sent).
    assert p_last is not None and p_last.tool_call_delta is not None
    assert p_last.tool_call_delta.id == "c1"
    assert p_last.tool_call_delta.name == "list_files"
    assert p_last.tool_call_delta.arguments == {"path": "/tmp"}


def test_legacy_mode_without_tool_buf_still_handles_complete_args() -> None:
    """``tool_buf=None`` keeps the pre-A1.5d.1 single-shot behaviour for
    callers that haven't switched yet. Arguments arriving in a single
    chunk parse correctly; spanning chunks is documented as lossy."""
    chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "f", "arguments": '{"x":1}'},
                        }
                    ]
                }
            }
        ]
    }
    parsed = _parse_stream_chunk(chunk)  # no tool_buf
    assert parsed is not None and parsed.tool_call_delta is not None
    assert parsed.tool_call_delta.arguments == {"x": 1}


def test_empty_args_fragment_does_not_break_accumulator() -> None:
    """Many providers send an opening chunk with ``arguments=""``.
    That must not corrupt the buffer or accidentally parse to {}."""
    tool_buf: dict[int, dict] = {}
    p = _parse_stream_chunk(
        _tc_delta_chunk(id="c", name="f", args_fragment=""),
        tool_buf,
    )
    assert p is not None and p.tool_call_delta is not None
    # args_str is "" — empty string is *not* a valid JSON object, so the
    # accumulator stays at its initial empty dict (not "{}" mistakenly
    # accepted as {}).
    assert p.tool_call_delta.arguments == {}
    assert tool_buf[0]["args_str"] == ""


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
