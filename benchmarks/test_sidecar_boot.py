"""Benchmark: sidecar cold boot to ready.

User path: the desktop's first-message latency is dominated by this —
spawn the Python subprocess, import the world, and reach the point where
the JSON-RPC transport can serve. The timing endpoint is the
``__SIDECAR_READY__`` stderr marker the sidecar emits once health is
snapshotted, before the stdio loop starts.

Every sample is a fresh child process with a private mkdtemp root; the
child is always bounded, awaited, and reaped (terminate → wait → kill
backstop).
"""

from __future__ import annotations

import subprocess
import sys

from benchmarks.calibration import ci_time_budget
from benchmarks.sampling import assert_within_budget, measure

#: Reference-machine expectation (median of 5, arm64 Mac): measured 261ms,
#: budgeted at 400ms — subprocess spawn jitters more than in-process work.
#: Reviewed constant — recalibrate only with a recorded measurement.
REFERENCE_MS = 400.0

_READY_MARKER = "__SIDECAR_READY__"


def _cold_boot() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "steerable_sidecar", "--log-level", "ERROR"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            if _READY_MARKER in line:
                break
        else:
            raise RuntimeError(
                "sidecar exited without emitting the ready marker: "
                f"returncode={proc.poll()}"
            )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_sidecar_cold_boot_within_budget() -> None:
    report = measure(_cold_boot)
    assert_within_budget(report, ci_time_budget(REFERENCE_MS), "sidecar cold boot")
