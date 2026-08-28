"""Provider error taxonomy + taxonomy-routed retry + overflow recovery."""

from __future__ import annotations

import pytest
from steerable_agent_runtime import (
    ChainHooks,
    CompactionHooks,
    CoreLoop,
    RetryHooks,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.llm.errors import (
    LLMError,
    classify_error,
    classify_http_status,
    is_retryable,
)

# ---------------------------------------------------------------------------
# classify_http_status
# ---------------------------------------------------------------------------


def test_status_mapping() -> None:
    assert classify_http_status(429) == "rate_limit"
    assert classify_http_status(401) == "auth"
    assert classify_http_status(403) == "auth"
    assert classify_http_status(500) == "server"
    assert classify_http_status(503) == "server"
    assert classify_http_status(408) == "transport"
    assert classify_http_status(400, "bad request") == "invalid_request"
    assert classify_http_status(404) == "invalid_request"


def test_context_overflow_markers() -> None:
    assert (
        classify_http_status(400, "This model's maximum context length is 8192")
        == "context_overflow"
    )
    assert (
        classify_http_status(400, "prompt is too long: 200000 tokens")
        == "context_overflow"
    )
    assert classify_http_status(413, "Payload Too Large: too many tokens") == (
        "context_overflow"
    )
    # 413 without overflow phrasing stays a plain client error
    assert classify_http_status(413, "file upload too large") == "invalid_request"


def test_classify_error_passthrough_and_fallbacks() -> None:
    err = LLMError("x", kind="rate_limit")
    assert classify_error(err) == "rate_limit"
    assert classify_error(TimeoutError("t")) == "transport"
    assert classify_error(ConnectionError("c")) == "transport"
    assert classify_error(ValueError("?")) == "unknown"


def test_is_retryable_routing() -> None:
    assert is_retryable(LLMError("x", kind="transport"))
    assert is_retryable(LLMError("x", kind="rate_limit"))
    assert is_retryable(LLMError("x", kind="server"))
    assert is_retryable(LLMError("x", kind="unknown"))
    assert not is_retryable(LLMError("x", kind="auth"))
    assert not is_retryable(LLMError("x", kind="invalid_request"))
    assert not is_retryable(LLMError("x", kind="context_overflow"))


# ---------------------------------------------------------------------------
# RetryHooks routing
# ---------------------------------------------------------------------------


class _FailNTimes:
    """Provider that raises the given error N times, then streams text."""

    def __init__(self, error: Exception, n: int):
        self._error = error
        self._left = n
        self.attempts = 0
        self.model = "fake"
        self.seen: list[list[LLMMessage]] = []

    async def stream(self, messages, **_kw):
        self.attempts += 1
        self.seen.append(list(messages))
        if self._left > 0:
            self._left -= 1
            raise self._error
        from steerable_agent_runtime.llm import LLMStreamChunk

        yield LLMStreamChunk(content_delta="ok", finish_reason="stop")


@pytest.mark.asyncio
async def test_auth_error_fails_fast_without_retry() -> None:
    provider = _FailNTimes(LLMError("bad key", kind="auth"), n=5)
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()), hooks=RetryHooks())
    events = [e async for e in loop.run([LLMMessage.text_of("user", "hi")])]
    assert provider.attempts == 1  # no retries on auth
    assert events[-1].data["status"] == "failed"
    assert "non-retryable" in events[-1].data["reason"]


@pytest.mark.asyncio
async def test_transport_error_retries_then_recovers() -> None:
    provider = _FailNTimes(LLMError("reset", kind="transport"), n=2)
    hooks = RetryHooks()
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()), hooks=hooks)
    events = [e async for e in loop.run([LLMMessage.text_of("user", "hi")])]
    assert provider.attempts == 3
    assert hooks.retries == 2
    assert events[-1].data["status"] == "completed"


@pytest.mark.asyncio
async def test_retry_hooks_alone_do_not_retry_overflow() -> None:
    """Without CompactionHooks in the chain an overflow fails immediately —
    retrying the identical over-long request is a guaranteed re-fail."""
    provider = _FailNTimes(LLMError("too long", kind="context_overflow"), n=5)
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()), hooks=RetryHooks())
    events = [e async for e in loop.run([LLMMessage.text_of("user", "hi")])]
    assert provider.attempts == 1
    assert events[-1].data["status"] == "failed"


# ---------------------------------------------------------------------------
# Overflow recovery via CompactionHooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overflow_triggers_compaction_retry() -> None:
    """First request overflows; the chain compacts and retries; the retried
    request carries the folded-tool marker and the turn completes."""
    provider = _FailNTimes(LLMError("ctx", kind="context_overflow"), n=1)
    hooks = ChainHooks(
        CompactionHooks(max_context_tokens=100_000),  # high: pre_step stays idle
        RetryHooks(),
    )
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()), hooks=hooks)

    # A transcript with several tool messages so folding has something to fold.
    messages = [
        LLMMessage.text_of("user", "goal"),
        LLMMessage.text_of("assistant", "calling tools"),
        *[
            LLMMessage.text_of("tool", "x" * 500, name=f"t{i}", tool_call_id=f"c{i}")
            for i in range(5)
        ],
        LLMMessage.text_of("user", "continue"),
    ]
    events = [e async for e in loop.run(messages)]

    assert provider.attempts == 2
    assert events[-1].data["status"] == "completed"
    # The retried request saw a compacted transcript (old tool output folded).
    retried = provider.seen[1]
    folded = [m for m in retried if m.role == "tool" and "folded" in m.content_text]
    assert folded, "expected folded tool markers in the retried transcript"


@pytest.mark.asyncio
async def test_overflow_recovery_is_bounded_per_round() -> None:
    provider = _FailNTimes(LLMError("ctx", kind="context_overflow"), n=10)
    compaction = CompactionHooks(max_context_tokens=100_000)
    hooks = ChainHooks(compaction, RetryHooks())
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()), hooks=hooks)
    events = [e async for e in loop.run([LLMMessage.text_of("user", "hi")])]

    # initial + max_overflow_retries(2) recovery attempts, then fail
    assert provider.attempts == 3
    assert compaction.overflow_recoveries == 2
    assert events[-1].data["status"] == "failed"
    assert "context overflow persists" in events[-1].data["reason"]
