"""Request retry — the ``on_request_error`` hook consumer.

`steerable_agent_harness.next_retry_delay_ms` has existed since the harness
was extracted but nothing called it; this hook wires it into the CoreLoop.
Local models (Ollama) flake far more often than cloud endpoints, so the
desktop path gets real robustness from this.

Semantics: each round gets its own retry budget (the counter resets when the
round advances — a request that failed twice in round 3 doesn't eat round
7's budget). Retryable classification defaults to "everything except obvious
auth/permission failures"; pass ``retryable=`` to customize.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from steerable_agent_harness import RetryPolicy, next_retry_delay_ms

from .hooks import NoopHooks, RetryAction


def _default_retryable(_error: Exception) -> bool:
    return True


class RetryHooks(NoopHooks):
    """``on_request_error`` hook: bounded exponential backoff per round."""

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        *,
        retryable: Callable[[Exception], bool] = _default_retryable,
    ) -> None:
        self._policy = policy or RetryPolicy()
        self._retryable = retryable
        self._round = -1
        self._attempt = 0
        # Observability for callers/tests.
        self.retries = 0

    async def on_request_error(self, error: Exception, ctx: Any) -> RetryAction:
        round_index = getattr(ctx, "round_index", 0)
        if round_index != self._round:
            self._round = round_index
            self._attempt = 0
        self._attempt += 1

        if not self._retryable(error):
            return RetryAction(kind="fail", reason=f"non-retryable error: {error}")
        if self._attempt >= self._policy.max_attempts:
            return RetryAction(
                kind="fail",
                reason=f"exhausted {self._policy.max_attempts} attempts: {error}",
            )

        self.retries += 1
        return RetryAction(
            kind="retry",
            delay_ms=next_retry_delay_ms(self._policy, self._attempt),
        )
