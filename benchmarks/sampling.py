"""Sampling helpers for the benchmark lane.

A benchmark measures a real user path N times and asserts the MEDIAN stays
under the calibrated budget — a single slow sample (runner jitter, GC) must
not fail the gate, a sustained regression must. On failure the full sample
distribution is printed so the verdict is explainable from the log alone.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass

#: Samples per benchmark. 5 keeps the lane fast while giving the median
#: enough mass to shrug off a single jitter spike.
DEFAULT_SAMPLES = 5


@dataclass(frozen=True)
class SampleReport:
    """Distribution of one benchmark's samples, in milliseconds."""

    samples: tuple[float, ...]

    @property
    def median(self) -> float:
        return statistics.median(self.samples)

    @property
    def minimum(self) -> float:
        return min(self.samples)

    @property
    def maximum(self) -> float:
        return max(self.samples)

    def render(self) -> str:
        points = ", ".join(f"{s:.1f}" for s in self.samples)
        return (
            f"samples=[{points}] min={self.minimum:.1f} "
            f"median={self.median:.1f} max={self.maximum:.1f} (ms)"
        )


def measure(fn: Callable[[], None], *, samples: int = DEFAULT_SAMPLES) -> SampleReport:
    """Run ``fn`` ``samples`` times, wall-clock each, return the distribution."""
    timings: list[float] = []
    for _ in range(samples):
        start = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - start) * 1000)
    return SampleReport(tuple(timings))


def assert_within_budget(report: SampleReport, budget_ms: float, label: str) -> None:
    """Assert the median is within budget; on failure print the distribution."""
    assert report.median <= budget_ms, (
        f"{label}: median {report.median:.1f}ms exceeds budget {budget_ms:.0f}ms — "
        f"{report.render()}. If this is a real regression, the budget is a "
        f"reviewed constant in the benchmark file; if it is runner jitter, "
        f"recalibrate CI_TIME_SCALE in benchmarks/calibration.py from this "
        f"run's medians."
    )
