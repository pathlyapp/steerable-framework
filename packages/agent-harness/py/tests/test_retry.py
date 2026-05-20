from __future__ import annotations

import asyncio
import random

from steerable_agent_harness import retry
from steerable_agent_harness.retry import is_retryable_error


def test_default_policy_attempts() -> None:
    policy = retry.RetryPolicy()
    assert policy.max_attempts == 3
    assert policy.base_delay_ms == 200
    assert policy.max_delay_ms == 5000
    assert policy.jitter is True


def test_retry_delay_doubling_without_jitter() -> None:
    policy = retry.RetryPolicy(base_delay_ms=100, max_delay_ms=1_000_000, jitter=False)
    delays = [retry.next_retry_delay_ms(policy, attempt) for attempt in range(1, 6)]
    assert delays == [100, 200, 400, 800, 1600]


def test_retry_delay_caps_at_max() -> None:
    policy = retry.RetryPolicy(base_delay_ms=1000, max_delay_ms=2500, jitter=False)
    delays = [retry.next_retry_delay_ms(policy, attempt) for attempt in range(1, 6)]
    assert delays == [1000, 2000, 2500, 2500, 2500]


def test_retry_delay_jitter_within_bounds() -> None:
    policy = retry.RetryPolicy(base_delay_ms=200, max_delay_ms=5000, jitter=True)
    rng = random.Random(42)
    samples: list[int] = []
    state = random.getstate()
    try:
        random.seed(42)
        for attempt in range(1, 6):
            samples.append(retry.next_retry_delay_ms(policy, attempt))
    finally:
        random.setstate(state)
    for attempt, value in zip(range(1, 6), samples, strict=True):
        base = min(200 * (2 ** (attempt - 1)), 5000)
        assert int(base * 0.8) <= value <= int(base * 1.2) + 1
    assert rng  # silence unused warning if helper kept


def test_attempt_clamped_to_one() -> None:
    policy = retry.RetryPolicy(base_delay_ms=100, max_delay_ms=10_000, jitter=False)
    assert retry.next_retry_delay_ms(policy, 0) == 100
    assert retry.next_retry_delay_ms(policy, -1) == 100


def test_retry_golden_no_jitter(assert_golden) -> None:
    policy = retry.RetryPolicy(base_delay_ms=100, max_delay_ms=2_000, jitter=False)
    payload = {
        "policy": {
            "max_attempts": policy.max_attempts,
            "base_delay_ms": policy.base_delay_ms,
            "max_delay_ms": policy.max_delay_ms,
            "jitter": policy.jitter,
        },
        "delays": [retry.next_retry_delay_ms(policy, a) for a in range(1, 6)],
    }
    assert_golden("retry_no_jitter", payload)


# ---------------------------------------------------------------------------
# is_retryable_error
# ---------------------------------------------------------------------------


def test_is_retryable_default_network_types() -> None:
    assert is_retryable_error(asyncio.TimeoutError()) is True
    assert is_retryable_error(TimeoutError()) is True
    assert is_retryable_error(ConnectionError("reset")) is True
    assert is_retryable_error(ConnectionResetError("reset")) is True
    assert is_retryable_error(OSError("broken pipe")) is True


def test_is_retryable_value_error_not_retryable() -> None:
    assert is_retryable_error(ValueError("bad arg")) is False
    assert is_retryable_error(RuntimeError("logic")) is False
    assert is_retryable_error(KeyError("missing")) is False


def test_is_retryable_cancellation_never_retried() -> None:
    assert is_retryable_error(asyncio.CancelledError()) is False
    assert is_retryable_error(KeyboardInterrupt()) is False
    assert is_retryable_error(SystemExit(0)) is False


def test_is_retryable_should_retry_true_overrides_type() -> None:
    class CustomBusinessError(Exception):
        should_retry = True

    assert is_retryable_error(CustomBusinessError("hi")) is True


def test_is_retryable_should_retry_false_overrides_default_retryable() -> None:
    class FatalTimeout(asyncio.TimeoutError):
        should_retry = False

    assert is_retryable_error(FatalTimeout()) is False


def test_is_retryable_should_retry_none_falls_back_to_type_check() -> None:
    class WithExplicitNone(asyncio.TimeoutError):
        should_retry = None

    assert is_retryable_error(WithExplicitNone()) is True

    class PlainBusinessError(Exception):
        should_retry = None

    assert is_retryable_error(PlainBusinessError()) is False
