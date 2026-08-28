"""LoopHooks extension points + single-write-path trajectory tests.

Verifies the three hook points are actually called by CoreLoop, that the
default NoopHooks preserve existing behavior, and that the trajectory is
derivable from the completion event stream alone (no separate record channel).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult

from steerable_agent_runtime import (
    ChainHooks,
    CoreLoop,
    LoopEvent,
    NoopHooks,
    PreStepAction,
    RetryAction,
    RewriteRequest,
    RouterToolExecutor,
    ToolRouter,
    TranscriptAppend,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk


def make_provider(script: list[dict[str, Any]]):
    """Fake LLMProvider playing back a scripted sequence of turns."""

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
                if entry.get("error") is not None:
                    raise entry["error"]
                content = entry.get("content", "")
                if content:
                    yield LLMStreamChunk(content_delta=content)
                for tc in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=tc)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                    usage=entry.get("usage"),
                )

            return _gen()

    return _FakeProvider()


def tc(name: str, args: dict[str, Any] | None = None, call_id: str | None = None) -> ToolCall:
    return ToolCall(id=call_id or f"call_{name}", name=name, arguments=args or {})


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


def final_completion(events: list[LoopEvent]) -> dict[str, Any]:
    completions = [e for e in events if e.kind == "completion"]
    assert completions, "loop never emitted a completion event"
    return completions[-1].data


# ---------------------------------------------------------------------------
# pre_step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_step_reject_ends_turn_without_calling_model() -> None:
    provider = make_provider([{"content": "should not be produced"}])
    router = ToolRouter()

    class _Reject(NoopHooks):
        async def pre_step(self, transcript, ctx):
            return PreStepAction(kind="reject", reason="context over pressure")

    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=_Reject())
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    decision = final_completion(events)
    assert decision["status"] == "failed"
    assert "context over pressure" in decision["reason"]
    # model was never called
    assert provider.calls == []


@pytest.mark.asyncio
async def test_pre_step_rewrite_transcript_is_seen_by_model() -> None:
    provider = make_provider([{"content": "ok"}])
    router = ToolRouter()

    class _Compact(NoopHooks):
        async def pre_step(self, transcript, ctx):
            # simulate compaction: collapse to a single summary message
            return PreStepAction(
                kind="proceed",
                rewrite=RewriteRequest(
                    messages=[LLMMessage.text_of("user", "[compacted summary]")],
                    reason="test compaction",
                ),
            )

    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=_Compact())
    await collect(loop.run([LLMMessage.text_of("user", "original long history")]))

    # the model saw the rewritten (compacted) transcript, not the original
    assert provider.calls[0][0].content_text == "[compacted summary]"


# ---------------------------------------------------------------------------
# Wave 1 declarations: appends + RewriteRequest composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_step_appends_land_on_record_with_kind() -> None:
    provider = make_provider([{"content": "ok"}])

    class _Inject(NoopHooks):
        async def pre_step(self, transcript, ctx):
            return PreStepAction(
                kind="proceed",
                appends=[
                    TranscriptAppend(
                        LLMMessage.text_of("system", "catalog here"),
                        kind="skills.catalog",
                    )
                ],
                reason="catalog injected",
                append_action="skill_catalog",
            )

    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()), hooks=_Inject())
    events = await collect(loop.run([LLMMessage.text_of("user", "hi")]))

    # The appended message reaches the model after the seeded user message…
    assert [m.content_text for m in provider.calls[0]] == ["hi", "catalog here"]
    # …carries its record kind…
    kinds = [item.kind for item in loop.history.projection_items]
    assert kinds == ["user", "skills.catalog", "assistant"]
    # …and is observable as a labelled hook_action.
    assert any(
        e.kind == "hook_action"
        and e.data.get("action") == "skill_catalog"
        and e.data.get("reason") == "catalog injected"
        for e in events
    )


@pytest.mark.asyncio
async def test_chain_hooks_rewrite_then_append_is_one_boundary() -> None:
    """hook1 rewrites, hook2 appends ≡ replace_all then append (one boundary)."""

    class _Rewrite(NoopHooks):
        async def pre_step(self, transcript, ctx):
            return PreStepAction(
                kind="proceed",
                rewrite=RewriteRequest(
                    messages=[LLMMessage.text_of("user", "compacted")],
                    reason="compact",
                ),
            )

    class _Append(NoopHooks):
        async def pre_step(self, transcript, ctx):
            # sees the post-rewrite projection
            assert [m.content_text for m in transcript] == ["compacted"]
            return PreStepAction(
                kind="proceed",
                appends=[TranscriptAppend(LLMMessage.text_of("system", "extra"))],
            )

    hooks = ChainHooks(_Rewrite(), _Append())
    action = await hooks.pre_step([LLMMessage.text_of("user", "original")], ctx=None)  # type: ignore[arg-type]

    # Folded into a single rewrite; no separate appends remain.
    assert action.appends is None
    assert action.rewrite is not None
    assert [m.content_text for m in action.rewrite.messages] == ["compacted", "extra"]


@pytest.mark.asyncio
async def test_chain_hooks_append_then_rewrite_folds_earlier_appends() -> None:
    """A later rewrite already computed against earlier appends — drop them."""

    class _Append(NoopHooks):
        async def pre_step(self, transcript, ctx):
            return PreStepAction(
                kind="proceed",
                appends=[TranscriptAppend(LLMMessage.text_of("system", "catalog"))],
            )

    class _Rewrite(NoopHooks):
        async def pre_step(self, transcript, ctx):
            # The rewriter saw the appended catalog and kept it in its output.
            return PreStepAction(
                kind="proceed",
                rewrite=RewriteRequest(
                    messages=list(transcript), reason="compact"
                ),
            )

    hooks = ChainHooks(_Append(), _Rewrite())
    action = await hooks.pre_step([LLMMessage.text_of("user", "original")], ctx=None)  # type: ignore[arg-type]

    assert action.appends is None
    assert action.rewrite is not None
    assert [m.content_text for m in action.rewrite.messages] == [
        "original",
        "catalog",
    ]


@pytest.mark.asyncio
async def test_chain_hooks_appends_only_compose_in_order() -> None:
    class _AppendA(NoopHooks):
        async def pre_step(self, transcript, ctx):
            return PreStepAction(
                kind="proceed",
                appends=[TranscriptAppend(LLMMessage.text_of("user", "A"))],
            )

    class _AppendB(NoopHooks):
        async def pre_step(self, transcript, ctx):
            return PreStepAction(
                kind="proceed",
                appends=[TranscriptAppend(LLMMessage.text_of("user", "B"))],
            )

    hooks = ChainHooks(_AppendA(), _AppendB())
    action = await hooks.pre_step([LLMMessage.text_of("user", "seed")], ctx=None)  # type: ignore[arg-type]

    assert action.rewrite is None
    assert action.appends is not None
    assert [a.message.content_text for a in action.appends] == ["A", "B"]


# ---------------------------------------------------------------------------
# post_tool_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_tool_result_rewrites_result_before_transcript() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("add", {"a": 1, "b": 2})]},
            {"content": "done"},
        ]
    )
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)

    class _Spill(NoopHooks):
        def __init__(self) -> None:
            self.seen: list[ToolResult] = []

        async def post_tool_result(self, result, call, ctx):
            self.seen.append(result)
            # simulate spill: replace data with a preview + locator
            return ToolResult(success=result.success, data={"preview": "...", "locator": "/tmp/x"})

    hooks = _Spill()
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    await collect(loop.run([LLMMessage.text_of("user", "add")]))

    # hook saw the original result
    assert len(hooks.seen) == 1 and hooks.seen[0].success is True
    # the transcript got the rewritten (preview) result, not the raw one
    second_call_messages = provider.calls[1]
    tool_msgs = [m for m in second_call_messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "preview" in tool_msgs[0].content_text


# ---------------------------------------------------------------------------
# on_request_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_request_error_retry_recovers() -> None:
    provider = make_provider(
        [
            {"error": ConnectionError("stream dropped")},
            {"content": "recovered after retry"},
        ]
    )
    router = ToolRouter()

    class _Retry(NoopHooks):
        def __init__(self) -> None:
            self.calls = 0

        async def on_request_error(self, error, transcript, ctx):
            self.calls += 1
            return RetryAction(kind="retry", delay_ms=0)

    hooks = _Retry()
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    assert hooks.calls == 1
    assert final_completion(events)["status"] == "completed"
    deltas = [e.data["delta"] for e in events if e.kind == "content_delta"]
    assert "".join(deltas) == "recovered after retry"


@pytest.mark.asyncio
async def test_on_request_error_fail_emits_error_event_and_ends() -> None:
    provider = make_provider([{"error": ConnectionError("stream dropped")}])
    router = ToolRouter()

    class _Fail(NoopHooks):
        async def on_request_error(self, error, transcript, ctx):
            return RetryAction(kind="fail", reason=f"giving up: {error}")

    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=_Fail())
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    # the previously-never-emitted "error" kind now fires
    assert any(e.kind == "error" for e in events)
    decision = final_completion(events)
    assert decision["status"] == "failed"
    assert "giving up" in decision["reason"]


# ---------------------------------------------------------------------------
# NoopHooks preserve existing behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_hooks_preserve_default_behavior() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("add", {"a": 1, "b": 2})]},
            {"content": "Sum is 3."},
        ]
    )
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=NoopHooks())

    events = await collect(loop.run([LLMMessage.text_of("user", "add")]))
    assert final_completion(events)["status"] == "completed"
    starts = [e for e in events if e.kind == "tool_call_start"]
    assert len(starts) == 1 and starts[0].data["name"] == "add"


# ---------------------------------------------------------------------------
# Single write path: trajectory derivable from the completion event stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trajectory_matches_completion_event_stream() -> None:
    # The trajectory must be reconstructable from the completion events alone —
    # there is no separate record channel that could drift.
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("add", {"a": 1, "b": 2})]},
            {"content": "Sum is 3."},
        ]
    )
    router = ToolRouter()

    async def add(a: int, b: int) -> int:
        return a + b

    router.register(add)
    loop = CoreLoop(provider, RouterToolExecutor(router))

    events = await collect(loop.run([LLMMessage.text_of("user", "add")]))

    completions = [e.data for e in events if e.kind == "completion"]
    # completion events now carry the full step summary
    assert all("round" in c and "finishReason" in c and "status" in c for c in completions)
    # one trajectory entry per completion event, same order, same status
    assert len(loop.trajectory) == len(completions)
    for traj, comp in zip(loop.trajectory, completions):
        assert traj.decision["status"] == comp["status"]
        assert traj.step["round"] == comp["round"]
        assert traj.step["finishReason"] == comp["finishReason"]
