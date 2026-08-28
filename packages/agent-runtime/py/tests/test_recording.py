"""RecordingProvider + prompt-invariant assertions (Wave 0 tripwires).

``assert_stable_prefix`` is the executable form of "no history rewrite": it
passes on a clean append-only sequence and fails the moment a request
mutates or drops earlier messages — unless that boundary was declared as a
compaction point.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    InMemoryRequestSink,
    JsonlRequestSink,
    RecordedRequest,
    RecordingProvider,
    RouterToolExecutor,
    ToolRouter,
    assert_bounded_items,
    assert_stable_prefix,
    load_recorded_requests,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk, LLMUsage, text_parts


class _FakeProvider:
    name = "fake"
    model = "fake-model"
    marker = "inner-attribute"

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = script
        self._idx = 0

    async def complete(self, messages, *, tools=None, **kw):
        return LLMMessage.text_of("assistant", "done"), LLMUsage(total_tokens=1)

    def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
        entry = self._script[min(self._idx, len(self._script) - 1)]
        self._idx += 1

        async def _gen() -> AsyncIterator[LLMStreamChunk]:
            content = entry.get("content", "")
            if content:
                yield LLMStreamChunk(content_delta=content)
            for call in entry.get("tool_calls", []):
                yield LLMStreamChunk(tool_call_delta=call)
            yield LLMStreamChunk(
                finish_reason="tool_calls" if entry.get("tool_calls") else "stop"
            )

        return _gen()


class _BoomProvider(_FakeProvider):
    def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
        async def _gen() -> AsyncIterator[LLMStreamChunk]:
            raise ConnectionError("provider exploded")
            yield  # pragma: no cover — makes this an async generator

        return _gen()


def _req(messages: list[dict[str, Any]], *, seq: int = 1) -> RecordedRequest:
    return RecordedRequest(
        seq=seq, kind="stream", provider="fake", model="fake-model", messages=messages
    )


# ---------------------------------------------------------------------------
# RecordingProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_records_stream_request_with_params() -> None:
    sink = InMemoryRequestSink()
    provider = RecordingProvider(_FakeProvider([{"content": "hi"}]), sink)
    tools = [{"type": "function", "function": {"name": "echo", "parameters": {}}}]

    chunks = [
        chunk
        async for chunk in provider.stream(
            [LLMMessage.text_of("user", "hello")],
            tools=tools,
            temperature=0.2,
            max_tokens=64,
            tool_choice="auto",
        )
    ]

    assert [c.content_delta for c in chunks if c.content_delta] == ["hi"]
    assert len(sink.requests) == 1
    req = sink.requests[0]
    assert req.seq == 1
    assert req.kind == "stream"
    assert req.provider == "fake"
    assert req.model == "fake-model"
    assert req.messages == [{"role": "user", "content": "hello"}]
    assert req.params["tools"] == tools
    assert req.params["temperature"] == 0.2
    assert req.params["max_tokens"] == 64
    assert req.params["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_recorded_messages_are_snapshots() -> None:
    sink = InMemoryRequestSink()
    provider = RecordingProvider(_FakeProvider([{"content": "hi"}]), sink)
    transcript = [LLMMessage.text_of("user", "one")]
    async for _ in provider.stream(transcript):
        pass
    # The loop appends to its transcript after the request; the record must
    # not observe later mutations.
    transcript.append(LLMMessage.text_of("assistant", "later"))
    transcript[0].content = text_parts("mutated")
    assert sink.requests[0].messages == [{"role": "user", "content": "one"}]


@pytest.mark.asyncio
async def test_records_complete_and_increments_seq() -> None:
    sink = InMemoryRequestSink()
    provider = RecordingProvider(_FakeProvider([]), sink)
    await provider.complete([LLMMessage.text_of("user", "a")])
    await provider.complete([LLMMessage.text_of("user", "b")])
    assert [r.seq for r in sink.requests] == [1, 2]
    assert all(r.kind == "complete" for r in sink.requests)


@pytest.mark.asyncio
async def test_records_request_even_when_provider_raises() -> None:
    sink = InMemoryRequestSink()
    provider = RecordingProvider(_BoomProvider([]), sink)
    with pytest.raises(ConnectionError):
        async for _ in provider.stream([LLMMessage.text_of("user", "go")]):
            pass
    assert len(sink.requests) == 1


def test_transparent_inner_attribute_access() -> None:
    provider = RecordingProvider(_FakeProvider([]), InMemoryRequestSink())
    assert provider.marker == "inner-attribute"
    assert provider.name == "fake"
    assert provider.model == "fake-model"


def test_jsonl_sink_round_trip(tmp_path) -> None:
    path = str(tmp_path / "requests.jsonl")
    sink = JsonlRequestSink(path)
    sink.record(_req([{"role": "user", "content": "一"}], seq=1))
    sink.record(
        _req(
            [
                {"role": "user", "content": "一"},
                {"role": "assistant", "content": "二"},
            ],
            seq=2,
        )
    )
    sink.close()

    loaded = load_recorded_requests(path)
    assert loaded == [
        _req([{"role": "user", "content": "一"}], seq=1),
        _req(
            [
                {"role": "user", "content": "一"},
                {"role": "assistant", "content": "二"},
            ],
            seq=2,
        ),
    ]


# ---------------------------------------------------------------------------
# assert_stable_prefix
# ---------------------------------------------------------------------------


def test_stable_prefix_passes_on_append_only_sequence() -> None:
    requests = [
        _req([{"role": "user", "content": "a"}], seq=1),
        _req(
            [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ],
            seq=2,
        ),
        _req(
            [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "tool", "content": "c", "tool_call_id": "call_1"},
            ],
            seq=3,
        ),
    ]
    assert_stable_prefix(requests)


def test_stable_prefix_passes_with_fewer_than_two_requests() -> None:
    assert_stable_prefix([])
    assert_stable_prefix([_req([{"role": "user", "content": "a"}])])


def test_stable_prefix_fails_on_rewrite() -> None:
    requests = [
        _req(
            [
                {"role": "system", "content": "long system prompt"},
                {"role": "user", "content": "a"},
            ],
            seq=1,
        ),
        _req(
            [
                {"role": "system", "content": "rewritten!"},
                {"role": "user", "content": "a"},
            ],
            seq=2,
        ),
    ]
    with pytest.raises(AssertionError, match="history rewrite at request #1"):
        assert_stable_prefix(requests)


def test_stable_prefix_fails_on_shrink() -> None:
    requests = [
        _req(
            [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ],
            seq=1,
        ),
        _req([{"role": "user", "content": "a"}], seq=2),
    ]
    with pytest.raises(AssertionError, match="shrank from 2 to 1"):
        assert_stable_prefix(requests)


def test_stable_prefix_respects_declared_compaction_boundaries() -> None:
    requests = [
        _req([{"role": "user", "content": "a"}], seq=1),
        # Compaction replaced the transcript wholesale before request #1.
        _req([{"role": "user", "content": "[compacted summary]"}], seq=2),
        # And it must keep growing append-only afterwards.
        _req(
            [
                {"role": "user", "content": "[compacted summary]"},
                {"role": "assistant", "content": "b"},
            ],
            seq=3,
        ),
    ]
    assert_stable_prefix(requests, compaction_boundaries={1})
    # Without the declaration the same sequence is a tripwire hit.
    with pytest.raises(AssertionError):
        assert_stable_prefix(requests)
    # A boundary declared elsewhere does not excuse this rewrite.
    with pytest.raises(AssertionError):
        assert_stable_prefix(requests, compaction_boundaries={2})


# ---------------------------------------------------------------------------
# assert_bounded_items
# ---------------------------------------------------------------------------


def test_bounded_items_passes_under_cap() -> None:
    requests = [_req([{"role": "user", "content": "a short message"}])]
    assert_bounded_items(requests)


def test_bounded_items_fails_over_cap() -> None:
    requests = [
        _req([{"role": "user", "content": "x" * 10_000}]),
    ]
    with pytest.raises(AssertionError, match="unbounded item"):
        assert_bounded_items(requests, max_item_tokens=100)


def test_bounded_items_counts_tool_call_payloads() -> None:
    big_args = {"payload": "y" * 10_000}
    requests = [
        _req(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "c1", "name": "echo", "arguments": big_args}],
                }
            ]
        )
    ]
    assert_bounded_items(requests, max_item_tokens=100_000)
    with pytest.raises(AssertionError, match="unbounded item"):
        assert_bounded_items(requests, max_item_tokens=100)


# ---------------------------------------------------------------------------
# Loop integration: a clean multi-round run is prefix-stable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_coreloop_run_satisfies_both_invariants() -> None:
    from steerable_agent_protocol.generated import ToolCall

    sink = InMemoryRequestSink()
    provider = RecordingProvider(
        _FakeProvider(
            [
                {
                    "content": "",
                    "tool_calls": [
                        ToolCall(id="call_1", name="echo", arguments={"text": "hi"})
                    ],
                },
                {"content": "final answer"},
            ]
        ),
        sink,
    )
    router = ToolRouter()

    async def echo(text: str = "") -> str:
        return text

    router.register(echo)
    loop = CoreLoop(provider, RouterToolExecutor(router))
    events = [event async for event in loop.run([LLMMessage.text_of("user", "go")])]

    assert events[-1].data["status"] == "completed"
    assert len(sink.requests) == 2
    assert_stable_prefix(sink.requests)
    assert_bounded_items(sink.requests)
    # Round 2's request carries the assistant turn and the tool result.
    roles = [m["role"] for m in sink.requests[1].messages]
    assert roles == ["user", "assistant", "tool"]
