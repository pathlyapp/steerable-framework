"""RetryHooks: on_request_error wired to the harness backoff primitive."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_harness import RetryPolicy
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime import (
    CoreLoop,
    LoopEvent,
    RetryHooks,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk


def make_flaky_provider(*, fail_on: set[int] | None, script: list[dict[str, Any]]):
    """Provider whose stream raises on the given 1-based attempt numbers.

    ``fail_on=None`` means every attempt fails (persistent error).
    """

    class _FlakyProvider:
        name = "flaky"
        model = "flaky-model"

        def __init__(self) -> None:
            self.attempts = 0
            self.calls: list[list[LLMMessage]] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            self.attempts += 1
            attempt = self.attempts
            entry = script[min(self._idx, len(script) - 1)]

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if fail_on is None or attempt in fail_on:
                    raise RuntimeError("connection reset by peer")
                self._idx += 1
                content = entry.get("content", "")
                if content:
                    yield LLMStreamChunk(content_delta=content)
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop"
                )

            return _gen()

    return _FlakyProvider()


def tc(name: str, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments=args or {})


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


def _policy() -> RetryPolicy:
    # zero-ish delays keep the tests fast; jitter off for determinism
    return RetryPolicy(max_attempts=3, base_delay_ms=1, max_delay_ms=2, jitter=False)


@pytest.mark.asyncio
async def test_transient_error_is_retried_and_run_completes() -> None:
    provider = make_flaky_provider(fail_on={1, 2}, script=[{"content": "recovered"}])
    hooks = RetryHooks(_policy())
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()), hooks=hooks)

    events = await collect(loop.run([LLMMessage(role="user", content="hi")]))

    assert provider.attempts == 3  # 2 failures + 1 success
    assert hooks.retries == 2
    assert events[-1].data["status"] == "completed"
    assert not [e for e in events if e.kind == "error"]


@pytest.mark.asyncio
async def test_persistent_error_fails_after_max_attempts() -> None:
    provider = make_flaky_provider(fail_on=None, script=[{"content": "never"}])
    hooks = RetryHooks(_policy())
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()), hooks=hooks)

    events = await collect(loop.run([LLMMessage(role="user", content="hi")]))

    assert provider.attempts == 3  # max_attempts
    errors = [e for e in events if e.kind == "error"]
    assert len(errors) == 1
    assert events[-1].data["status"] == "failed"
    assert "exhausted" in events[-1].data["reason"]


@pytest.mark.asyncio
async def test_non_retryable_error_fails_immediately() -> None:
    provider = make_flaky_provider(fail_on=None, script=[{"content": "never"}])
    hooks = RetryHooks(_policy(), retryable=lambda exc: False)
    loop = CoreLoop(provider, RouterToolExecutor(ToolRouter()), hooks=hooks)

    events = await collect(loop.run([LLMMessage(role="user", content="hi")]))

    assert provider.attempts == 1  # no retry
    assert events[-1].data["status"] == "failed"
    assert "non-retryable" in events[-1].data["reason"]


@pytest.mark.asyncio
async def test_retry_budget_resets_each_round() -> None:
    # Round 0 burns 2 retries then succeeds with a tool call; round 1 must get
    # a fresh budget (another 2 retries) rather than inheriting round 0's.
    provider = make_flaky_provider(
        fail_on={1, 2, 4, 5},
        script=[{"content": "", "tool_calls": [tc("noop")]}, {"content": "done"}],
    )
    router = ToolRouter()

    async def noop() -> str:
        return "ok"

    router.register(noop)
    hooks = RetryHooks(_policy())
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)

    events = await collect(loop.run([LLMMessage(role="user", content="go")]))

    # attempts: round0 fails x2 + success, round1 fails x2 + success = 6
    assert provider.attempts == 6
    assert events[-1].data["status"] == "completed"
