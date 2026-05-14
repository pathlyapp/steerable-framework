from __future__ import annotations

from dataclasses import dataclass
import random


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
