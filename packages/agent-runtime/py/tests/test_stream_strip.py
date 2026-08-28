"""Streaming display hygiene: pseudo-block stripping + UTF-16 surrogate carry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk
from steerable_agent_runtime.pseudo import (
    PseudoStreamStripper,
    split_trailing_high_surrogate,
    strip_pseudo_fn_final,
)

# ---------------------------------------------------------------------------
# Unit: surrogate carry
# ---------------------------------------------------------------------------


def test_surrogate_carry_holds_trailing_high_half() -> None:
    emit, carry = split_trailing_high_surrogate("hello \ud83d", "")
    assert emit == "hello "
    assert carry == "\ud83d"
    # next chunk completes the pair
    emit, carry = split_trailing_high_surrogate("\ude00 world", carry)
    assert emit == "\ud83d\ude00 world"
    assert carry == ""


def test_surrogate_carry_passthrough_when_no_high_half() -> None:
    emit, carry = split_trailing_high_surrogate("plain text", "")
    assert emit == "plain text"
    assert carry == ""


# ---------------------------------------------------------------------------
# Unit: stripper
# ---------------------------------------------------------------------------


def _strip(chunks: list[str]) -> str:
    s = PseudoStreamStripper()
    out = [s.feed(c) for c in chunks]
    out.append(s.flush())
    return "".join(out)


def test_stripper_passes_plain_text() -> None:
    assert _strip(["hello ", "world"]) == "hello world"


def test_stripper_drops_fn_echo_block() -> None:
    text = 'before <function_results>{"fake": 1}</function_results> after'
    assert _strip([text]) == "before  after"


def test_stripper_drops_block_split_across_chunks() -> None:
    chunks = ["before <func", "tion_results>{'x':", " 1}</function_res", "ults> after"]
    assert _strip(chunks) == "before  after"


def test_stripper_drops_tool_call_tool_response_pair() -> None:
    text = (
        "ok <tool_call>{\"name\": \"fake\"}</tool_call>"
        "<tool_response>{\"fabricated\": true}</tool_response> done"
    )
    assert _strip([text]) == "ok  done"


def test_stripper_drops_markdown_pseudo_call() -> None:
    text = 'let me check\n[Tool call: get_weather]\n{"city": "Berlin"}\nDone!'
    assert _strip([text]) == "let me check\nDone!"


def test_stripper_holds_partial_opener_then_releases() -> None:
    # "[Tool" at a chunk end is held back; when the next chunk shows it was
    # NOT an opener, the held text is released.
    assert _strip(["see [Tool", "shed for details"]) == "see [Toolshed for details"


def test_stripper_drops_unclosed_block_at_flush() -> None:
    assert _strip(["before <function_results>{'never closed'"]) == "before "


def test_stripper_releases_when_markdown_block_never_closes() -> None:
    # No closing ``}`` ever arrives: past the hard cap the held buffer is
    # released (only the tail survives — the middle is accounted as
    # swallowed), and the filter returns to normal mode so later text flows.
    s = PseudoStreamStripper()
    out = [s.feed("[Tool call: x]\n" + "y" * 5000)]
    out.append(s.feed(" and then normal text"))
    out.append(s.flush())
    joined = "".join(out)
    assert "y" * 64 in joined  # held tail released after the cap
    assert "and then normal text" in joined


def test_strip_pseudo_fn_final_removes_echo_blocks() -> None:
    text = "a <tool_response>{'x': 1}</tool_response> b"
    assert strip_pseudo_fn_final(text) == "a  b"
    assert strip_pseudo_fn_final("") == ""


# ---------------------------------------------------------------------------
# Integration: loop display events are clean, recovery still works
# ---------------------------------------------------------------------------


def make_provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                # emit content in small pieces to exercise cross-chunk state
                for piece in entry.get("content_pieces", []):
                    yield LLMStreamChunk(content_delta=piece)
                content = entry.get("content", "")
                if content:
                    yield LLMStreamChunk(content_delta=content)
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop"
                )

            return _gen()

    return _FakeProvider()


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


@pytest.mark.asyncio
async def test_loop_display_stream_strips_pseudo_but_recovers_call() -> None:
    # A markdown pseudo call arrives split across chunks: the display stream
    # must not show it, yet the loop still recovers and executes the tool.
    pieces = [
        "Checking the weather.\n",
        "[Tool ",
        "call: get_weather]\n",
        '{"city": "Berlin',
        '"}',
    ]
    provider = make_provider(
        [{"content_pieces": pieces}, {"content": "It is sunny in Berlin."}]
    )
    router = ToolRouter()
    executed: list[str] = []

    async def get_weather(city: str) -> str:
        executed.append(city)
        return "sunny"

    router.register(get_weather)
    loop = CoreLoop(provider, RouterToolExecutor(router))
    events = await collect(loop.run([LLMMessage.text_of("user", "weather?")]))

    displayed = "".join(
        e.data["delta"] for e in events if e.kind == "content_delta"
    )
    assert "[Tool call:" not in displayed
    assert "get_weather" not in displayed.split("It is sunny")[0]
    assert "Checking the weather." in displayed
    assert "It is sunny in Berlin." in displayed
    assert executed == ["Berlin"]
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_loop_display_stream_strips_echo_blocks() -> None:
    pieces = ["answer: ", "<function_results>{'fake': 1}</function_results>", " 42"]
    provider = make_provider([{"content_pieces": pieces}])
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))
    events = await collect(loop.run([LLMMessage.text_of("user", "hi")]))

    displayed = "".join(e.data["delta"] for e in events if e.kind == "content_delta")
    assert displayed == "answer:  42"
    # and the transcript content is cleaned too (final strip)
    assert events[-1].data["textLength"] == len("answer:  42")


@pytest.mark.asyncio
async def test_loop_content_deltas_reassemble_emoji_split_across_chunks() -> None:
    pieces = ["look \ud83d", "\ude00 done"]  # 😀 split mid-pair
    provider = make_provider([{"content_pieces": pieces}])
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))
    events = await collect(loop.run([LLMMessage.text_of("user", "hi")]))

    displayed = "".join(e.data["delta"] for e in events if e.kind == "content_delta")
    assert displayed == "look \ud83d\ude00 done"
