"""Request retry — the ``on_request_error`` hook consumer.

`steerable_agent_harness.next_retry_delay_ms` has existed since the harness
was extracted but nothing called it; this hook wires it into the CoreLoop.
Local models (Ollama) flake far more often than cloud endpoints, so the
desktop path gets real robustness from this.

Semantics: each round gets its own retry budget (the counter resets when the
round advances — a request that failed twice in round 3 doesn't eat round
7's budget). Retryable classification routes on the provider error taxonomy
(``llm.errors``): transport / rate_limit / server / unknown retry with
backoff; auth / invalid_request fail fast; context_overflow fails here too —
it is ``CompactionHooks``' job to retry those with a rewritten transcript.
Pass ``retryable=`` to customize.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from steerable_agent_harness import RetryPolicy, next_retry_delay_ms

from .hooks import NoopHooks, RetryAction
from .llm.errors import classify_error

if TYPE_CHECKING:
    from .llm import LLMMessage


def _default_retryable(error: Exception) -> bool:
    kind = classify_error(error)
    # context_overflow is excluded: retrying the identical over-long request
    # is a guaranteed re-fail. CompactionHooks intercepts that kind upstream.
    return kind in ("transport", "rate_limit", "server", "unknown")


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

    async def on_request_error(
        self, error: Exception, transcript: list[LLMMessage], ctx: Any
    ) -> RetryAction:
        round_index = getattr(ctx, "round_index", 0)
        if round_index != self._round:
            self._round = round_index
            self._attempt = 0
        self._attempt += 1

        if not self._retryable(error):
            return RetryAction(
                kind="fail",
                reason=f"non-retryable error ({classify_error(error)}): {error}",
            )
        if self._attempt >= self._policy.max_attempts:
            return RetryAction(
                kind="fail",
                reason=f"exhausted {self._policy.max_attempts} attempts: {error}",
            )

        self.retries += 1
        delay_ms = next_retry_delay_ms(self._policy, self._attempt)
        retry_after = getattr(error, "retry_after_ms", None)
        if isinstance(retry_after, int) and retry_after > delay_ms:
            delay_ms = retry_after
        return RetryAction(
            kind="retry",
            delay_ms=delay_ms,
        )
