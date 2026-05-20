from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_ms: int = 200
    max_delay_ms: int = 5000
    jitter: bool = True


def next_retry_delay_ms(policy: RetryPolicy, attempt: int) -> int:
    if attempt < 1:
        attempt = 1
    delay = min(policy.base_delay_ms * (2 ** (attempt - 1)), policy.max_delay_ms)
    if policy.jitter:
        delay = int(delay * random.uniform(0.8, 1.2))
    return max(delay, 0)


_DEFAULT_RETRYABLE_TYPES: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    ConnectionError,
    TimeoutError,
    OSError,
)


def is_retryable_error(exc: BaseException) -> bool:
    """Default classifier for transient failures.

    Order of evaluation:

    1. ``CancelledError`` / ``KeyboardInterrupt`` / ``SystemExit`` are never
       retryable — they signal caller intent, not transient failure.
    2. An ``exc.should_retry`` attribute, if present, wins. ``True`` forces
       retry, ``False`` forces fail-fast. This is the recommended way for
       provider adapters to mark their own exception classes.
    3. Otherwise, retry if ``exc`` is an instance of one of the default
       transient I/O / network types (``asyncio.TimeoutError``,
       ``ConnectionError``, ``TimeoutError``, ``OSError``).

    Hook authors and callers that need different policy can replace the
    classifier by passing their own callable to whatever runner uses it;
    this function is only the default.
    """
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return False
    explicit = getattr(exc, "should_retry", None)
    if explicit is False:
        return False
    if explicit is True:
        return True
    return isinstance(exc, _DEFAULT_RETRYABLE_TYPES)
