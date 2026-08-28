"""Typed append-only history (Wave 1 step 1): ContextManager, fragments.

The manager is the loop's only transcript write path: appends grow the
record, ``replace_all`` is the sole declared rewrite and is itself
append-only (a boundary marker plus replacement items). The projection —
what the provider sees — is every item after the newest boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_runtime import (
    CompactionBoundary,
    ContextFragment,
    ContextManager,
    CoreLoop,
    HistoryItem,
    InMemoryRequestSink,
    LoopConfig,
    NoopHooks,
    PreStepAction,
    RecordingProvider,
    RouterToolExecutor,
    ToolRouter,
    assert_stable_prefix,
    tool,
)
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk, LLMUsage
from steerable_agent_runtime.loop import SoftTimeoutNotice


class _FakeProvider:
    name = "fake"
    model = "fake-model"

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


def _msg(role: str, content: str) -> LLMMessage:
    return LLMMessage.text_of(role, content)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------


def test_seed_and_append_assign_monotonic_envelopes() -> None:
    manager = ContextManager(
        [_msg("system", "sys"), _msg("user", "goal")], token_model="fake-model"
    )

    item = manager.append(_msg("assistant", "hi"))

    assert [i.seq for i in manager.record] == [0, 1, 2]
    assert item.kind == "assistant"
    assert item.turn_id == manager.turn_id
    assert item.token_estimate > 0
    assert [type(e) for e in manager.record] == [HistoryItem] * 3
    assert [m.content_text for m in manager.projection] == ["sys", "goal", "hi"]


def test_projection_is_a_throwaway_copy() -> None:
    manager = ContextManager([_msg("user", "one")])
    projection = manager.projection
    projection.append(_msg("user", "sneaky"))
    assert [m.content_text for m in manager.projection] == ["one"]


def test_replace_all_records_boundary_and_supersedes() -> None:
    manager = ContextManager([_msg("system", "sys"), _msg("user", "goal")])
    manager.append(_msg("assistant", "draft"))
    manager.append(_msg("tool", "payload"))

    boundary = manager.replace_all(
        [_msg("system", "sys"), _msg("user", "goal"), _msg("user", "summary")],
        reason="context pressure",
        action="compact",
    )

    # Projection is exactly the replacement…
    assert [m.content_text for m in manager.projection] == ["sys", "goal", "summary"]
    # …but the record kept the superseded span plus the boundary marker.
    kinds = [
        e.kind if isinstance(e, HistoryItem) else e.kind for e in manager.record
    ]
    assert kinds == [
        "system",
        "user",
        "assistant",
        "tool",
        "compaction.boundary",
        "system",
        "user",
        "user",
    ]
    assert isinstance(boundary, CompactionBoundary)
    assert boundary.reason == "context pressure"
    assert boundary.action == "compact"
    assert manager.latest_boundary == boundary


def test_second_replace_all_narrows_projection_to_newest_span() -> None:
    manager = ContextManager([_msg("user", "v0")])
    manager.replace_all([_msg("user", "v1")], reason="first")
    manager.replace_all([_msg("user", "v2")], reason="second")

    assert [m.content_text for m in manager.projection] == ["v2"]
    assert len(manager.record) == 1 + 2 + 2  # item + (boundary+item) x2
    assert manager.latest_boundary is not None
    assert manager.latest_boundary.reason == "second"


def test_projection_token_estimate_counts_visible_span_only() -> None:
    manager = ContextManager([_msg("user", "x" * 400)])
    full = manager.projection_token_estimate
    assert full > 0
    manager.replace_all([_msg("user", "tiny")], reason="shrink")
    shrunk = manager.projection_token_estimate
    assert 0 < shrunk < full


def test_append_fragment_renders_under_its_content_kind() -> None:
    manager = ContextManager()
    item = manager.append_fragment(SoftTimeoutNotice())
    assert item.kind == "loop.soft_timeout_notice"
    assert item.message.role == "user"
    assert SoftTimeoutNotice.matches_text(item.message.content_text)
    assert manager.projection[-1].content_text == item.message.content_text


# ---------------------------------------------------------------------------
# ContextFragment
# ---------------------------------------------------------------------------


class _UnmarkedFragment(ContextFragment):
    content_kind = "test.unmarked"

    def body(self) -> str:
        return "bare body"


class _WrappedFragment(ContextFragment):
    content_kind = "test.wrapped"

    def markers(self) -> tuple[str, str]:
        return ("<wrap>", "</wrap>")

    def body(self) -> str:
        return "inside"

    @classmethod
    def type_markers(cls) -> tuple[str, str]:
        return ("<wrap>", "</wrap>")


def test_unmarked_fragment_renders_bare_and_never_matches() -> None:
    fragment = _UnmarkedFragment()
    assert fragment.render() == "bare body"
    assert _UnmarkedFragment.matches_text("bare body") is False


def test_wrapped_fragment_round_trips() -> None:
    fragment = _WrappedFragment()
    assert fragment.render() == "<wrap>inside</wrap>"
    assert _WrappedFragment.matches_text("<wrap>inside</wrap>")
    assert _WrappedFragment.matches_text("  <WRAP>inside</WRAP>  ")
    assert not _WrappedFragment.matches_text("<wrap>unclosed")
    assert not _WrappedFragment.matches_text("unrelated")


def test_prefix_notice_matches_by_prefix() -> None:
    assert SoftTimeoutNotice.matches_text(
        "[system notice] The time budget for this task is exhausted. …"
    )
    assert not SoftTimeoutNotice.matches_text("[system notice] The task ended …")
    assert not SoftTimeoutNotice.matches_text("hello")


def test_fragment_to_message_carries_tool_fields() -> None:
    message = _UnmarkedFragment().to_message(name="t", tool_call_id="c1")
    assert message.role == "user"
    assert message.name == "t"
    assert message.tool_call_id == "c1"


# ---------------------------------------------------------------------------
# CoreLoop integration (behaviour-identical adoption)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_run_record_matches_what_the_model_saw() -> None:
    sink = InMemoryRequestSink()
    provider = RecordingProvider(_FakeProvider([{"content": "final answer"}]), sink)
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))

    events = [
        event
        async for event in loop.run([_msg("user", "hello")])
    ]
    assert any(e.kind == "completion" for e in events)

    # One request; the record replays exactly what was sent.
    assert len(sink.requests) == 1
    sent = sink.requests[0].messages
    assert [m.content_text for m in loop.history.projection] == [
        "hello",
        "final answer",
    ]
    assert sent == [{"role": "user", "content": "hello"}]
    assert_stable_prefix(sink.requests)  # no declared boundaries needed
    assert [item.kind for item in loop.history.projection_items] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_tool_round_appends_in_order() -> None:
    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    sink = InMemoryRequestSink()
    provider = RecordingProvider(
        _FakeProvider(
            [
                {"tool_calls": [ToolCall(id="c1", name="echo", arguments={"text": "hi"})]},
                {"content": "done"},
            ]
        ),
        sink,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router))

    async for _ in loop.run([_msg("user", "echo hi")]):
        pass

    assert [item.kind for item in loop.history.projection_items] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    # Two requests, the second strictly extends the first.
    assert_stable_prefix(sink.requests)
    second = sink.requests[1].messages
    assert [m["role"] for m in second] == ["user", "assistant", "tool"]


class _RewriteOnceHooks(NoopHooks):
    """Compaction stand-in: rewrite the transcript on the second round."""

    def __init__(self) -> None:
        self.rewrites = 0

    async def pre_step(self, transcript, ctx):
        if ctx.round_index == 1 and self.rewrites == 0:
            self.rewrites += 1
            return PreStepAction(
                kind="proceed",
                transcript=[_msg("user", "compacted goal")],
                reason="test compaction",
            )
        return PreStepAction(kind="proceed", transcript=transcript)


@pytest.mark.asyncio
async def test_hook_rewrite_goes_through_a_recorded_boundary() -> None:
    router = ToolRouter()

    @tool(router=router, description="Echo text")
    async def echo(text: str) -> dict[str, str]:
        return {"echo": text}

    sink = InMemoryRequestSink()
    provider = RecordingProvider(
        _FakeProvider(
            [
                {"tool_calls": [ToolCall(id="c1", name="echo", arguments={"text": "hi"})]},
                {"content": "done"},
            ]
        ),
        sink,
    )
    hooks = _RewriteOnceHooks()
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)

    async for _ in loop.run([_msg("user", "echo hi")]):
        pass

    # The rewrite is a declared boundary in the record, and the projection
    # is exactly the rewritten transcript.
    boundary = loop.history.latest_boundary
    assert boundary is not None
    assert boundary.reason == "test compaction"
    assert [m.content_text for m in loop.history.projection] == ["compacted goal", "done"]

    # The recording shows the rewrite: the prefix assertion must fail
    # without the declared boundary and pass with it.
    with pytest.raises(AssertionError, match="history rewrite"):
        assert_stable_prefix(sink.requests)
    assert_stable_prefix(sink.requests, compaction_boundaries={1})


@pytest.mark.asyncio
async def test_soft_timeout_notice_lands_as_marked_fragment() -> None:
    sink = InMemoryRequestSink()
    provider = RecordingProvider(
        _FakeProvider([{"content": "wrap-up"}]), sink
    )
    loop = CoreLoop(
        provider,
        RouterToolExecutor(ToolRouter()),
        LoopConfig(soft_timeout_ms=0),
    )

    async for _ in loop.run([_msg("user", "hello")]):
        pass

    kinds = [item.kind for item in loop.history.projection_items]
    assert "loop.soft_timeout_notice" in kinds
    notice = loop.history.projection_items[kinds.index("loop.soft_timeout_notice")]
    assert SoftTimeoutNotice.matches_text(notice.message.content_text)


@pytest.mark.asyncio
async def test_steer_lands_as_typed_append() -> None:
    provider = _FakeProvider([{"content": "first"}, {"content": "second"}])
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))

    # Steer before the run: the inbox drains at the first round boundary.
    loop.steer("mid-turn note")
    async for _ in loop.run([_msg("user", "hello")]):
        pass

    kinds = [item.kind for item in loop.history.projection_items]
    assert "steer.inject" in kinds
