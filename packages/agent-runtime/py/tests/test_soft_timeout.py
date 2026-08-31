"""Soft timeout: wall-clock budget → graceful wrap-up instead of hard kill."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.hooks import CompletionAction, NoopHooks
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk
from steerable_agent_runtime.loop import _MAX_COMPLETION_REDOS


def make_provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self.tools_seen: list[Any] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            self.tools_seen.append(tools)
            entry = script[min(self._idx, len(script) - 1)]
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

    return _FakeProvider()


def tc(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments=args or {})


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


def _text_of(message: LLMMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(getattr(part, "text", "") for part in message.content or ())


@pytest.mark.asyncio
async def test_no_soft_timeout_runs_normally() -> None:
    provider = make_provider([{"content": "answer"}])
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()))
    events = await collect(loop.run([LLMMessage.text_of("user", "hi")]))
    assert not [e for e in events if e.kind == "soft_timeout"]
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_soft_timeout_triggers_wrap_up_round() -> None:
    # Round 0 runs a (slow) tool; by round 1 the deadline has passed, so the
    # loop must emit soft_timeout, withhold tool descriptors, and the model's
    # final text completes the turn.
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("slow")]},
            {"content": "partial answer"},
        ]
    )
    router = ToolRouter()

    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "slept"

    router.register(slow)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(soft_timeout_ms=10),  # 10ms — round 0's tool blows past it
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    soft = [e for e in events if e.kind == "soft_timeout"]
    assert len(soft) == 1
    assert soft[0].data["round"] == 1
    # wrap-up round saw no tool descriptors
    assert provider.tools_seen[1] is None
    # the wrap-up notice was appended to the transcript
    assert any(
        "time budget" in m.content_text for m in provider.calls[1]
    )
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_wrap_up_drops_tool_intent() -> None:
    # Even if the model insists on calling tools in the wrap-up round, the
    # loop must not execute them — it completes with whatever content exists.
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("noop")]},
            # wrap-up round: model ignores the notice and emits a tool call
            # plus some final text
            {"content": "fine, wrapping up", "tool_calls": [tc("noop")]},
        ]
    )
    router = ToolRouter()
    executed: list[str] = []

    async def noop() -> str:
        executed.append("noop")
        await asyncio.sleep(0.02)  # push elapsed past the 10ms deadline
        return "ok"

    router.register(noop)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(soft_timeout_ms=10),  # round 0 runs; deadline passes by round 1
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))

    # tool ran exactly once (round 0), not in the wrap-up round
    assert executed == ["noop"]
    assert events[-1].data["status"] == "completed"
    assert events[-1].data["textLength"] == len("fine, wrapping up")


@pytest.mark.asyncio
async def test_soft_timeout_never_interrupts_in_flight_tool() -> None:
    # Soft = checked only at round boundaries. A tool slower than the whole
    # budget still runs to completion.
    provider = make_provider(
        [{"content": "", "tool_calls": [tc("slow")]}, {"content": "done"}]
    )
    router = ToolRouter()
    finished: list[str] = []

    async def slow() -> str:
        await asyncio.sleep(0.05)
        finished.append("slow")
        return "ok"

    router.register(slow)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(soft_timeout_ms=1),
    )
    await collect(loop.run([LLMMessage.text_of("user", "go")]))
    assert finished == ["slow"]


@pytest.mark.asyncio
async def test_wrap_up_keeps_tools_executes_then_stops() -> None:
    """Harbor scores files; wrap-up must still let the model write them."""
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("slow")]},
            {"content": "", "tool_calls": [tc("write")]},
            {"content": "files are on disk"},
        ]
    )
    router = ToolRouter()
    executed: list[str] = []

    async def slow() -> str:
        await asyncio.sleep(0.05)
        executed.append("slow")
        return "slept"

    async def write() -> str:
        executed.append("write")
        return "wrote"

    router.register(slow)
    router.register(write)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(
            soft_timeout_ms=10,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=1,
        ),
    )
    schemas = [
        {"type": "function", "function": {"name": "slow", "parameters": {}}},
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=schemas)
    )

    assert executed == ["slow", "write"]
    assert [e for e in events if e.kind == "soft_timeout"]
    assert provider.tools_seen[1] == schemas
    assert any(
        "Write the required output files" in m.content_text
        and "drafted" in m.content_text
        for m in provider.calls[1]
    )
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_wrap_up_keeps_tools_caps_extra_act_rounds() -> None:
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("slow")]},
            {"content": "", "tool_calls": [tc("write")]},
            {"content": "", "tool_calls": [tc("write")]},
            {"content": "done"},
        ]
    )
    router = ToolRouter()
    executed: list[str] = []

    async def slow() -> str:
        await asyncio.sleep(0.05)
        executed.append("slow")
        return "slept"

    async def write() -> str:
        executed.append("write")
        return "wrote"

    router.register(slow)
    router.register(write)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(
            soft_timeout_ms=10,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=1,
        ),
    )
    schemas = [
        {"type": "function", "function": {"name": "slow", "parameters": {}}},
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    await collect(loop.run([LLMMessage.text_of("user", "go")], tools=schemas))
    # round 0: slow; wrap-up round 1: write; further writes dropped
    assert executed == ["slow", "write"]
    assert provider.tools_seen[2] is None


class _RetryTextOnce(NoopHooks):
    def __init__(self) -> None:
        self.retries = 0

    async def before_completion(self, draft, ctx):
        if self.retries == 0 and (draft.content or "").strip():
            self.retries += 1
            return CompletionAction(
                kind="retry",
                message="write the named output files now",
                reason="missing_named_output",
            )
        return CompletionAction(kind="accept")


@pytest.mark.asyncio
async def test_wrap_up_keeps_tools_retries_text_only_stop() -> None:
    """regex-chess: wrap-up summarized instead of writing /app/re.json."""
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("slow")]},
            {"content": "cannot write the file"},
            {"content": "", "tool_calls": [tc("write")]},
            {"content": "files are on disk"},
        ]
    )
    router = ToolRouter()
    executed: list[str] = []

    async def slow() -> str:
        await asyncio.sleep(0.05)
        executed.append("slow")
        return "slept"

    async def write() -> str:
        executed.append("write")
        return "wrote"

    router.register(slow)
    router.register(write)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(
            soft_timeout_ms=10,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=2,
        ),
        hooks=_RetryTextOnce(),
    )
    schemas = [
        {"type": "function", "function": {"name": "slow", "parameters": {}}},
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=schemas)
    )
    assert executed == ["slow", "write"]
    retries = [
        e
        for e in events
        if e.kind == "hook_action" and e.data.get("reason") == "missing_named_output"
    ]
    assert retries
    assert events[-1].data["status"] == "completed"


class _KeepToolsPastCap(NoopHooks):
    def wrap_up_may_drop_tools(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_wrap_up_keeps_tools_past_cap_when_hook_forbids_drop() -> None:
    """ars.R: wrap-up talked until the cap, then tools were withheld."""
    provider = make_provider(
        [
            {"content": "", "tool_calls": [tc("slow")]},
            {"content": "", "tool_calls": [tc("write")]},
            {"content": "", "tool_calls": [tc("write")]},
            {"content": "files are on disk"},
        ]
    )
    router = ToolRouter()
    executed: list[str] = []

    async def slow() -> str:
        await asyncio.sleep(0.05)
        executed.append("slow")
        return "slept"

    async def write() -> str:
        executed.append("write")
        return "wrote"

    router.register(slow)
    router.register(write)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(
            soft_timeout_ms=10,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=1,
            tool_dedup=False,
        ),
        hooks=_KeepToolsPastCap(),
    )
    schemas = [
        {"type": "function", "function": {"name": "slow", "parameters": {}}},
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    await collect(loop.run([LLMMessage.text_of("user", "go")], tools=schemas))
    assert executed == ["slow", "write", "write"]
    assert provider.tools_seen[2] == schemas


def test_max_completion_redos_covers_delivery_empty_rounds() -> None:
    # DeliveryHooks retries empty_round 6 times then missing_named_output;
    # idle-stream cuts also go through before_completion.
    assert _MAX_COMPLETION_REDOS >= 32


@pytest.mark.asyncio
async def test_soft_timeout_cuts_in_flight_reasoning_stream() -> None:
    """steal.py: a 166 min reasoning stream skipped wrap-up; Harbor killed."""

    class _SlowThinkThenWrite:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self.tools_seen: list[Any] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            self.tools_seen.append(tools)
            idx = self._idx
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if idx == 0:
                    yield LLMStreamChunk(reasoning_delta="draft steal.py here")
                    await asyncio.sleep(0.2)
                    yield LLMStreamChunk(content_delta="never reached")
                    return
                if tools is None:
                    yield LLMStreamChunk(content_delta="files are on disk")
                    yield LLMStreamChunk(finish_reason="stop")
                    return
                yield LLMStreamChunk(tool_call_delta=tc("write"))
                yield LLMStreamChunk(finish_reason="tool_calls")

            return _gen()

    router = ToolRouter()
    executed: list[str] = []

    async def write() -> str:
        executed.append("write")
        return "wrote"

    router.register(write)
    provider = _SlowThinkThenWrite()
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(
            soft_timeout_ms=10,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=2,
        ),
    )
    schemas = [
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=schemas)
    )
    assert [e for e in events if e.kind == "soft_timeout"]
    assert executed == ["write"]
    assert events[-1].data["status"] == "completed"
    assert any(
        "time budget" in m.content_text for m in provider.calls[1]
    )


@pytest.mark.asyncio
async def test_wrap_up_stream_timeout_stops_second_think() -> None:
    """After wrap-up, another long reasoning stream must not eat Harbor."""

    class _ThinkForeverOnWrap:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            idx = self._idx
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if idx == 0:
                    yield LLMStreamChunk(tool_call_delta=tc("slow"))
                    yield LLMStreamChunk(finish_reason="tool_calls")
                    return
                yield LLMStreamChunk(reasoning_delta="still planning")
                await asyncio.sleep(0.2)
                yield LLMStreamChunk(content_delta="never")

            return _gen()

    router = ToolRouter()

    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "slept"

    router.register(slow)
    loop = CoreLoop(
        _ThinkForeverOnWrap(),
        RouterToolExecutor(router),
        LoopConfig(
            soft_timeout_ms=10,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=2,
            wrap_up_tool_timeout_ms=20,
        ),
        hooks=_RetryTextOnce(),
    )
    schemas = [
        {"type": "function", "function": {"name": "slow", "parameters": {}}},
    ]
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=schemas)
    )
    assert [e for e in events if e.kind == "soft_timeout"]
    assert events[-1].data["status"] in {"completed", "failed", "budget_exhausted"}


class _RetryOnce(NoopHooks):
    def __init__(self) -> None:
        self.retries = 0

    async def before_completion(self, draft, ctx):
        if self.retries == 0:
            self.retries += 1
            return CompletionAction(
                kind="retry",
                message="write the named output files now",
                reason="missing_named_output",
            )
        return CompletionAction(kind="accept")


@pytest.mark.asyncio
async def test_idle_stream_cuts_dense_reasoning_without_wrap_up() -> None:
    """dna-assembly: hours of tokens, zero tools; do not wait for soft wrap-up."""

    class _DenseThinkThenWrite:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            idx = self._idx
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if idx == 0:
                    for i in range(8):
                        yield LLMStreamChunk(reasoning_delta=f"plan {i}")
                        await asyncio.sleep(0.02)
                    yield LLMStreamChunk(content_delta="never reached")
                    return
                yield LLMStreamChunk(tool_call_delta=tc("write"))
                yield LLMStreamChunk(finish_reason="tool_calls")

            return _gen()

    router = ToolRouter()
    executed: list[str] = []

    async def write() -> str:
        executed.append("write")
        return "wrote"

    router.register(write)
    loop = CoreLoop(
        _DenseThinkThenWrite(),
        RouterToolExecutor(router),
        LoopConfig(idle_stream_timeout_ms=50),
        hooks=_RetryOnce(),
    )
    schemas = [
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=schemas)
    )
    cuts = [
        e
        for e in events
        if e.kind == "hook_action" and e.data.get("action") == "idle_stream_cut"
    ]
    assert cuts
    assert not [e for e in events if e.kind == "soft_timeout"]
    deltas = [e.data.get("delta", "") for e in events if e.kind == "content_delta"]
    assert "never reached" not in "".join(deltas)
    assert executed == ["write"]


@pytest.mark.asyncio
async def test_second_idle_stream_cut_starts_wrap_up() -> None:
    """Z.AI ignores tool_choice=required; a second Hmm stream must wrap-up."""

    class _HmmTwiceThenWrite:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            idx = self._idx
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if idx < 2:
                    for i in range(8):
                        yield LLMStreamChunk(reasoning_delta=f"hmm {idx}-{i}")
                        await asyncio.sleep(0.02)
                    yield LLMStreamChunk(content_delta="never reached")
                    return
                yield LLMStreamChunk(tool_call_delta=tc("write"))
                yield LLMStreamChunk(finish_reason="tool_calls")

            return _gen()

    router = ToolRouter()
    executed: list[str] = []

    async def write() -> str:
        executed.append("write")
        return "wrote"

    router.register(write)
    loop = CoreLoop(
        _HmmTwiceThenWrite(),
        RouterToolExecutor(router),
        LoopConfig(
            idle_stream_timeout_ms=50,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=4,
        ),
        hooks=_RetryOnce(),
    )
    schemas = [
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=schemas)
    )
    cuts = [
        e
        for e in events
        if e.kind == "hook_action" and e.data.get("action") == "idle_stream_cut"
    ]
    assert len(cuts) == 2
    assert [e for e in events if e.kind == "soft_timeout"]
    deltas = [e.data.get("delta", "") for e in events if e.kind == "content_delta"]
    assert "never reached" not in "".join(deltas)
    assert executed == ["write"]


@pytest.mark.asyncio
async def test_idle_stream_keeps_cutting_during_wrap_up() -> None:
    """circuit-fibsqrt: the cut used to switch off once wrap-up began, so the
    spiral streamed on until Harbor killed the trial."""

    class _NeverStopsThinking:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.streams = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.streams += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                for i in range(8):
                    yield LLMStreamChunk(reasoning_delta=f"hmm {i}")
                    await asyncio.sleep(0.02)
                yield LLMStreamChunk(content_delta="never reached")

            return _gen()

    class _AlwaysRetry(NoopHooks):
        async def before_completion(self, draft, ctx):
            return CompletionAction(
                kind="retry",
                message="write the named output files now",
                reason="missing_named_output",
            )

    loop = CoreLoop(
        _NeverStopsThinking(),
        RouterToolExecutor(ToolRouter()),
        LoopConfig(
            idle_stream_timeout_ms=50,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=3,
        ),
        hooks=_AlwaysRetry(),
    )
    schemas = [
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=schemas)
    )
    cuts = [
        e
        for e in events
        if e.kind == "hook_action" and e.data.get("action") == "idle_stream_cut"
    ]
    assert len(cuts) > 2
    assert all(c.data["trigger"] == "active_ms" for c in cuts)
    assert [e for e in events if e.kind == "soft_timeout"]
    deltas = [e.data.get("delta", "") for e in events if e.kind == "content_delta"]
    assert "never reached" not in "".join(deltas)


@pytest.mark.asyncio
async def test_idle_stream_cuts_on_reasoning_volume() -> None:
    """A dense stream stays under the active-ms wall; volume must still cut."""

    class _DenseNoGaps:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            idx = self._idx
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if idx == 0:
                    for _ in range(20):
                        yield LLMStreamChunk(reasoning_delta="x" * 100)
                    yield LLMStreamChunk(content_delta="never reached")
                    return
                yield LLMStreamChunk(tool_call_delta=tc("write"))
                yield LLMStreamChunk(finish_reason="tool_calls")

            return _gen()

    router = ToolRouter()
    executed: list[str] = []

    async def write() -> str:
        executed.append("write")
        return "wrote"

    router.register(write)
    loop = CoreLoop(
        _DenseNoGaps(),
        RouterToolExecutor(router),
        LoopConfig(idle_stream_timeout_ms=600_000, idle_stream_max_chars=500),
        hooks=_RetryOnce(),
    )
    schemas = [
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=schemas)
    )
    cuts = [
        e
        for e in events
        if e.kind == "hook_action" and e.data.get("action") == "idle_stream_cut"
    ]
    assert len(cuts) == 1
    assert cuts[0].data["trigger"] == "chars"
    assert cuts[0].data["chars"] >= 500
    deltas = [e.data.get("delta", "") for e in events if e.kind == "content_delta"]
    assert "never reached" not in "".join(deltas)
    assert executed == ["write"]


@pytest.mark.asyncio
async def test_idle_stream_volume_ignores_tool_call_arguments() -> None:
    """A huge write_file argument is delivery, not a spiral: never cut it."""

    class _BigToolArgs:
        name = "fake"
        model = "fake-model"

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                yield LLMStreamChunk(reasoning_delta="short plan")
                yield LLMStreamChunk(tool_call_delta=tc("write"))
                for _ in range(20):
                    yield LLMStreamChunk(content_delta="y" * 100)
                yield LLMStreamChunk(finish_reason="tool_calls")

            return _gen()

    router = ToolRouter()
    executed: list[str] = []

    async def write() -> str:
        executed.append("write")
        return "wrote"

    router.register(write)
    loop = CoreLoop(
        _BigToolArgs(),
        RouterToolExecutor(router),
        LoopConfig(idle_stream_max_chars=500, max_rounds=2),
    )
    schemas = [
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=schemas)
    )
    assert not [
        e
        for e in events
        if e.kind == "hook_action" and e.data.get("action") == "idle_stream_cut"
    ]
    assert executed == ["write"]


@pytest.mark.asyncio
async def test_idle_cut_keeps_draft_and_drops_the_cut_reasoning() -> None:
    """openai_compat replays `reasoning` on every later request, so a kept cut
    trace re-primes the spiral the cut just stopped."""

    class _SpiralThenWrite:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.seen: list[list[LLMMessage]] = []

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            idx = len(self.seen)
            self.seen.append(list(messages))

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if idx < 2:
                    yield LLMStreamChunk(content_delta="partial draft")
                    for i in range(8):
                        yield LLMStreamChunk(reasoning_delta=f"hmm {idx}-{i}")
                        await asyncio.sleep(0.02)
                    return
                yield LLMStreamChunk(tool_call_delta=tc("write"))
                yield LLMStreamChunk(finish_reason="tool_calls")

            return _gen()

    router = ToolRouter()

    async def write() -> str:
        return "wrote"

    router.register(write)
    provider = _SpiralThenWrite()
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(
            idle_stream_timeout_ms=50,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=4,
        ),
        hooks=_RetryOnce(),
    )
    schemas = [
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    events = await collect(
        loop.run([LLMMessage.text_of("user", "go")], tools=schemas)
    )
    cuts = [
        e
        for e in events
        if e.kind == "hook_action" and e.data.get("action") == "idle_stream_cut"
    ]
    assert len(cuts) == 2
    drafts = [
        m
        for m in provider.seen[-1]
        if m.role == "assistant" and _text_of(m) == "partial draft"
    ]
    assert len(drafts) == 2
    assert all(m.reasoning is None for m in drafts)
    assert all(m.reasoning_details is None for m in drafts)


@pytest.mark.asyncio
async def test_idle_cut_appends_nothing_when_the_spiral_wrote_no_draft() -> None:
    """A reasoning-only cut has no draft, so appending would leave an empty
    assistant turn on the record once the trace is dropped."""

    class _PureSpiral:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.seen: list[list[LLMMessage]] = []

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            idx = len(self.seen)
            self.seen.append(list(messages))

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if idx < 2:
                    for i in range(8):
                        yield LLMStreamChunk(reasoning_delta=f"hmm {idx}-{i}")
                        await asyncio.sleep(0.02)
                    return
                yield LLMStreamChunk(tool_call_delta=tc("write"))
                yield LLMStreamChunk(finish_reason="tool_calls")

            return _gen()

    router = ToolRouter()

    async def write() -> str:
        return "wrote"

    router.register(write)
    provider = _PureSpiral()
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(
            idle_stream_timeout_ms=50,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=4,
        ),
        hooks=_RetryOnce(),
    )
    schemas = [
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    await collect(loop.run([LLMMessage.text_of("user", "go")], tools=schemas))
    replayed = [m for m in provider.seen[-1] if m.role == "assistant"]
    assert not [m for m in replayed if not _text_of(m) and not m.tool_calls]
    assert all(m.reasoning is None for m in replayed)


@pytest.mark.asyncio
async def test_idle_stream_ignores_long_sse_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """regex-chess: ~48 min with no SSE must not count as active reasoning."""
    import steerable_agent_runtime.loop as loop_mod

    monkeypatch.setattr(loop_mod, "_IDLE_REASONING_GAP_SEC", 0.03)

    class _GappyThink:
        name = "fake"
        model = "fake-model"

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                yield LLMStreamChunk(reasoning_delta="start")
                await asyncio.sleep(0.08)
                yield LLMStreamChunk(reasoning_delta="resume")
                yield LLMStreamChunk(content_delta="done")
                yield LLMStreamChunk(finish_reason="stop")

            return _gen()

    loop = CoreLoop(
        _GappyThink(),
        RouterToolExecutor(ToolRouter()),
        LoopConfig(idle_stream_timeout_ms=50),
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "go")]))
    cuts = [
        e
        for e in events
        if e.kind == "hook_action" and e.data.get("action") == "idle_stream_cut"
    ]
    assert not cuts
    assert events[-1].data["status"] == "completed"
