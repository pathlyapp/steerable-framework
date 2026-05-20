"""A1.5b tests — ``RetryPolicy`` on the LLM stream.

Contract:

* Retry coverage is **stream creation + first chunk only**. Once any chunk
  has been yielded downstream, exceptions propagate verbatim.
* Default classifier (``is_retryable_error``) considers asyncio/network/IO
  errors retryable, plus anything tagging itself with ``should_retry=True``.
* ``CancelledError`` and ``HookError`` always bypass retry.
* Retries are silent w.r.t. the ``error`` hook — only the *final* failure
  (retries exhausted, non-retryable, or mid-stream) reaches the hook.
* No ``retry_policy`` (or ``max_attempts <= 1``) is observably equivalent to
  the A1.4 behaviour (direct ``provider.stream`` call, no extra awaits).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

import pytest

from steerable_agent_protocol.generated import SSEEvent
from steerable_agent_runtime import (
    ChatLoop,
    ErrorCtx,
    LLMMessage,
    LLMStreamChunk,
    LLMUsage,
    LoopConfig,
    LoopEndCtx,
    RetryPolicy,
    ToolRouter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _RetryableProvider:
    """Raises ``exc`` for the first ``fail_count`` ``stream`` calls, then
    streams ``chunks`` and stops. Used to exercise retry-success paths."""

    name = "retryable"
    model = "retryable-model"

    def __init__(
        self,
        *,
        exc: Exception,
        fail_count: int,
        chunks: list[LLMStreamChunk],
    ) -> None:
        self._exc = exc
        self._fail_count = fail_count
        self._chunks = chunks
        self.calls = 0

    async def complete(self, *a: Any, **kw: Any) -> tuple[LLMMessage, Any]:
        raise NotImplementedError

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        self.calls += 1
        if self.calls <= self._fail_count:
            raise self._exc
        for chunk in self._chunks:
            yield chunk


class _MidStreamRaisingProvider:
    """Yields ``yield_chunks`` then raises ``exc``. Used to verify mid-stream
    exceptions are NOT retried."""

    name = "mid-raising"
    model = "mid-raising-model"

    def __init__(
        self,
        *,
        yield_chunks: list[LLMStreamChunk],
        exc: Exception,
    ) -> None:
        self._yield_chunks = yield_chunks
        self._exc = exc
        self.calls = 0

    async def complete(self, *a: Any, **kw: Any) -> tuple[LLMMessage, Any]:
        raise NotImplementedError

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        self.calls += 1
        for chunk in self._yield_chunks:
            yield chunk
        raise self._exc


def _make_router() -> ToolRouter:
    return ToolRouter()


def _finish(reason: str = "stop", usage: LLMUsage | None = None) -> LLMStreamChunk:
    return LLMStreamChunk(finish_reason=reason, usage=usage)


def _text(text: str) -> LLMStreamChunk:
    return LLMStreamChunk(content_delta=text)


def _capture_to(buf: list[Any]):
    """Return an async hook callback that appends every received ctx into
    ``buf``. Hook callbacks must be coroutine functions; a plain ``lambda``
    that returns ``None`` cannot be awaited by the registry."""

    async def _cb(ctx: Any) -> None:
        buf.append(ctx)

    return _cb


# A tiny / zero-delay retry policy so the suite stays fast and deterministic.
_FAST_RETRY = RetryPolicy(
    max_attempts=3, base_delay_ms=0, max_delay_ms=0, jitter=False
)


# ---------------------------------------------------------------------------
# Path 1: retry_policy=None → no retry, single attempt, error hook fires once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retry_policy_keeps_a14_behaviour() -> None:
    provider = _RetryableProvider(
        exc=ConnectionError("boom"),
        fail_count=99,
        chunks=[_finish()],
    )
    errors: list[ErrorCtx] = []
    loop = ChatLoop(
        LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=_make_router(),
            initial_messages=[LLMMessage(role="user", content="hi")],
            retry_policy=None,
        )
    )
    loop.on("error", _capture_to(errors))

    async for _ in loop.run():
        pass

    assert provider.calls == 1, "no retry policy → exactly one provider.stream() call"
    assert len(errors) == 1
    assert errors[0].phase == "llm_stream"
    assert isinstance(errors[0].exception, ConnectionError)


@pytest.mark.asyncio
async def test_max_attempts_one_acts_like_no_retry() -> None:
    provider = _RetryableProvider(
        exc=ConnectionError("boom"),
        fail_count=99,
        chunks=[_finish()],
    )
    errors: list[ErrorCtx] = []
    loop = ChatLoop(
        LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=_make_router(),
            initial_messages=[LLMMessage(role="user", content="hi")],
            retry_policy=RetryPolicy(
                max_attempts=1, base_delay_ms=0, max_delay_ms=0, jitter=False
            ),
        )
    )
    loop.on("error", _capture_to(errors))

    async for _ in loop.run():
        pass

    assert provider.calls == 1
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# Path 2: transient failure → retry → success, NO error hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failure_then_success_no_error_hook() -> None:
    provider = _RetryableProvider(
        exc=ConnectionError("flaky"),
        fail_count=2,
        chunks=[_text("hello"), _finish()],
    )
    errors: list[ErrorCtx] = []
    end_ctx: list[LoopEndCtx] = []
    loop = ChatLoop(
        LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=_make_router(),
            initial_messages=[LLMMessage(role="user", content="ping")],
            retry_policy=_FAST_RETRY,
        )
    )
    loop.on("error", _capture_to(errors))
    loop.on("loop_end", _capture_to(end_ctx))

    async for _ in loop.run():
        pass

    assert provider.calls == 3, "first 2 attempts fail, 3rd succeeds"
    assert errors == [], "retries are silent w.r.t. the error hook"
    assert end_ctx and end_ctx[0].final_status == "completed"


# ---------------------------------------------------------------------------
# Path 3: retries exhausted → error hook fires once with final exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_exhausted_fires_error_hook_once() -> None:
    provider = _RetryableProvider(
        exc=ConnectionError("always"),
        fail_count=99,
        chunks=[_finish()],
    )
    errors: list[ErrorCtx] = []
    error_sse: list[SSEEvent] = []
    loop = ChatLoop(
        LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=_make_router(),
            initial_messages=[LLMMessage(role="user", content="ping")],
            retry_policy=_FAST_RETRY,
        )
    )
    loop.on("error", _capture_to(errors))

    async for sse in loop.run():
        if sse.type == "agent" and sse.event == "error":
            error_sse.append(sse)

    assert provider.calls == _FAST_RETRY.max_attempts, "all attempts consumed"
    assert len(errors) == 1, "error hook fires once, after retries are exhausted"
    assert isinstance(errors[0].exception, ConnectionError)
    assert len(error_sse) == 1
    assert error_sse[0].payload["errorType"] == "ConnectionError"


# ---------------------------------------------------------------------------
# Path 4: non-retryable exception → no retry, error hook fires immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_retryable_exception_skips_retry() -> None:
    provider = _RetryableProvider(
        exc=ValueError("bad arg"),
        fail_count=99,
        chunks=[_finish()],
    )
    errors: list[ErrorCtx] = []
    loop = ChatLoop(
        LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=_make_router(),
            initial_messages=[LLMMessage(role="user", content="hi")],
            retry_policy=_FAST_RETRY,
        )
    )
    loop.on("error", _capture_to(errors))

    async for _ in loop.run():
        pass

    assert provider.calls == 1, "non-retryable → exactly one attempt"
    assert len(errors) == 1
    assert isinstance(errors[0].exception, ValueError)


# ---------------------------------------------------------------------------
# Path 5: mid-stream failure → NOT retried (would lose state)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_stream_failure_is_not_retried() -> None:
    provider = _MidStreamRaisingProvider(
        yield_chunks=[_text("partial-")],
        exc=ConnectionError("dropped"),
    )
    errors: list[ErrorCtx] = []
    end_ctx: list[LoopEndCtx] = []
    loop = ChatLoop(
        LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=_make_router(),
            initial_messages=[LLMMessage(role="user", content="hi")],
            retry_policy=_FAST_RETRY,
        )
    )
    loop.on("error", _capture_to(errors))
    loop.on("loop_end", _capture_to(end_ctx))

    async for _ in loop.run():
        pass

    assert provider.calls == 1, "first chunk already yielded → no retry"
    assert len(errors) == 1
    assert isinstance(errors[0].exception, ConnectionError)
    assert end_ctx and end_ctx[0].final_status == "failed"


# ---------------------------------------------------------------------------
# Path 6: per-call `should_retry` attribute opt-in
# ---------------------------------------------------------------------------


class _CustomTransientError(Exception):
    should_retry = True


@pytest.mark.asyncio
async def test_should_retry_true_attribute_enables_retry_for_custom_type() -> None:
    provider = _RetryableProvider(
        exc=_CustomTransientError("blip"),
        fail_count=1,
        chunks=[_text("ok"), _finish()],
    )
    errors: list[ErrorCtx] = []
    loop = ChatLoop(
        LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=_make_router(),
            initial_messages=[LLMMessage(role="user", content="hi")],
            retry_policy=_FAST_RETRY,
        )
    )
    loop.on("error", _capture_to(errors))

    async for _ in loop.run():
        pass

    assert provider.calls == 2
    assert errors == []


# ---------------------------------------------------------------------------
# Path 7: cancellation during retry sleep is not swallowed
# ---------------------------------------------------------------------------


class _SlowFailingProvider:
    """Always raises retryable error so the loop hits ``asyncio.sleep``
    between attempts. Used to make sure cancellation is honoured during
    backoff sleep."""

    name = "slow-failing"
    model = "slow-failing-model"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *a: Any, **kw: Any) -> tuple[LLMMessage, Any]:
        raise NotImplementedError

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        self.calls += 1
        raise ConnectionError("nope")
        # pragma: no cover — unreachable, kept for typing
        yield LLMStreamChunk()  # type: ignore[unreachable]


@pytest.mark.asyncio
async def test_cancellation_during_retry_propagates() -> None:
    provider = _SlowFailingProvider()
    policy = RetryPolicy(
        # Big delay so cancellation hits us during asyncio.sleep
        max_attempts=5,
        base_delay_ms=1_000,
        max_delay_ms=10_000,
        jitter=False,
    )
    loop = ChatLoop(
        LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=_make_router(),
            initial_messages=[LLMMessage(role="user", content="hi")],
            retry_policy=policy,
        )
    )
    end_ctx: list[LoopEndCtx] = []
    loop.on("loop_end", _capture_to(end_ctx))

    async def consume() -> None:
        async for _ in loop.run():
            pass

    task = asyncio.create_task(consume())
    # Yield once so the loop reaches the retry sleep, then cancel.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert end_ctx and end_ctx[0].final_status == "cancelled"


# ---------------------------------------------------------------------------
# Path 8: HookError during stream startup is NOT retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_send_messages_hook_error_is_not_retried() -> None:
    """A hook that raises during ``before_send_messages`` should surface as
    ``HookError`` immediately — not get wrapped in retry logic."""

    provider = _RetryableProvider(
        exc=ConnectionError("never reached"),
        fail_count=0,
        chunks=[_finish()],
    )
    loop = ChatLoop(
        LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            tool_router=_make_router(),
            initial_messages=[LLMMessage(role="user", content="hi")],
            retry_policy=_FAST_RETRY,
        )
    )

    async def bad_hook(ctx: Any) -> None:
        raise RuntimeError("nope")

    loop.on("before_send_messages", bad_hook)

    from steerable_agent_runtime import HookError  # local import keeps test isolated

    with pytest.raises(HookError):
        async for _ in loop.run():
            pass

    assert provider.calls == 0, "hook raised before stream was even attempted"
