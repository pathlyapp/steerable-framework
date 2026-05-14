from __future__ import annotations

import random

from steerable_agent_harness import retry


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
