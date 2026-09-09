"""Shared conversion from reference-machine expectations to CI time budgets.

Mirrors deepseek-harness `benchmarks/support/calibration.ts`: budgets are
reviewed source constants — environment variables must not override them.
The reference machine is an arm64 Mac (developer laptop); the CI runner is
a slower shared x64 box, so CI budgets scale the expectation up by a
measured ratio plus a variance headroom.
"""

#: Measured wall-time ratio between the x64 CI runner and the arm64
#: reference machine. First CI run (PR #46) measured medians at 0.38–0.81x
#: the reference budgets — GitHub's current runners are faster than the
#: reference Mac on these paths. We keep 1.0 rather than tightening to the
#: observed ratio: runner allocation varies, and the headroom below is what
#: absorbs a slow-runner day. Recalibrate from reported medians if the lane
#: goes flaky or drifts (see README).
CI_TIME_SCALE = 1.0

#: Allowed variance above the calibrated expectation. Wide enough to absorb
#: runner jitter, tight enough to catch a real regression.
PERFORMANCE_BUDGET_HEADROOM = 1.25


def ci_time_budget(expected_ms: float) -> float:
    """Convert a reference-machine duration into its CI wall-time budget.

    Returns milliseconds including machine scaling and variance headroom.
    """
    import math

    return math.ceil(expected_ms * CI_TIME_SCALE * PERFORMANCE_BUDGET_HEADROOM)
