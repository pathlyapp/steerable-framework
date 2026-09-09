"""Shared conversion from reference-machine expectations to CI time budgets.

Mirrors deepseek-harness `benchmarks/support/calibration.ts`: budgets are
reviewed source constants — environment variables must not override them.
The reference machine is an arm64 Mac (developer laptop); the CI runner is
a slower shared x64 box, so CI budgets scale the expectation up by a
measured ratio plus a variance headroom.
"""

#: Measured wall-time ratio between the x64 CI runner and the arm64
#: reference machine. Initial value follows dsh's; recalibrate from the
#: first CI runs' reported medians (see README).
CI_TIME_SCALE = 2.0

#: Allowed variance above the calibrated expectation. Wide enough to absorb
#: runner jitter, tight enough to catch a real regression.
PERFORMANCE_BUDGET_HEADROOM = 1.25


def ci_time_budget(expected_ms: float) -> float:
    """Convert a reference-machine duration into its CI wall-time budget.

    Returns milliseconds including machine scaling and variance headroom.
    """
    import math

    return math.ceil(expected_ms * CI_TIME_SCALE * PERFORMANCE_BUDGET_HEADROOM)
