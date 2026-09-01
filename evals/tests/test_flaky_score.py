"""Tests for the scorer that decides whether a change earns a catalog run.

A wrong verdict here costs either a six-hour run spent on noise or a real
improvement discarded, so the cases below cover the two ways this module has
to be right: every attempt has to survive collection, and a verdict has to
need both the sign test and the interval.
"""

import json
from pathlib import Path

import pytest

from evals.flaky_score import (
    Trial,
    _arm_of,
    bootstrap_delta,
    collect,
    report,
    sign_test,
    spread,
)


def _trial(root: Path, arm: str, shard: int, task: str, tag: str, *, reward: float,
           calls: int = 0) -> None:
    """Write one Harbor-shaped trial directory under an arm artifact."""
    artifact = f"eval-steerable-flaky-{arm}-{shard}" if arm in "ab" else "eval-catalog-0"
    trial = root / artifact / "evals" / "jobs" / "steerable" / "run" / f"{task}__{tag}"
    (trial / "agent").mkdir(parents=True, exist_ok=True)
    (trial / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": reward}}})
    )
    (trial / "agent" / "headless.log").write_text(
        "".join(f"[tool bash {{'command': 'echo {i}'}}]\n" for i in range(calls))
    )


def test_arm_comes_from_the_artifact_directory(tmp_path: Path) -> None:
    flaky = tmp_path / "eval-steerable-flaky-b-12" / "x" / "result.json"
    flaky.parent.mkdir(parents=True)
    assert _arm_of(flaky, tmp_path) == "b"
    plain = tmp_path / "eval-catalog-3" / "x" / "result.json"
    plain.parent.mkdir(parents=True)
    assert _arm_of(plain, tmp_path) == "-"


def test_every_attempt_survives_collection(tmp_path: Path) -> None:
    """Three attempts of one task must stay three data points.

    Harbor gives each attempt its own ``task__hash`` directory, so keying by
    task rather than by trial is what would silently reduce n_attempts=3 back
    to the single-attempt measurement this module exists to replace.
    """
    for i, reward in enumerate((1.0, 0.0, 1.0)):
        _trial(tmp_path, "a", 0, "some-task", f"h{i}", reward=reward, calls=5 + i)
    data = collect(tmp_path)
    assert [t.passed for t in data["some-task"]["a"]] == [True, False, True]
    assert [t.calls for t in data["some-task"]["a"]] == [5, 6, 7]


def test_a_trial_without_a_reward_is_not_a_failure(tmp_path: Path) -> None:
    """An errored trial is absent, not a zero: counting it as a loss would
    let infrastructure failures decide the verdict."""
    trial = tmp_path / "eval-steerable-flaky-a-0" / "evals" / "jobs" / "steerable"
    trial = trial / "run" / "broken__h0"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"verifier_result": {}}))
    assert collect(tmp_path) == {}


def test_sign_test_matches_the_exact_binomial() -> None:
    assert sign_test(0, 0) == 1.0
    assert sign_test(5, 0) == 2 * (1 / 32)
    assert sign_test(3, 3) == 1.0


def test_spread_needs_two_logged_attempts() -> None:
    assert spread([]) is None
    assert spread([Trial(True, 10)]) is None
    assert spread([Trial(True, 10), Trial(False, None)]) is None
    assert spread([Trial(True, 10), Trial(True, 20)]) == pytest.approx(7.07, abs=0.01)


def test_bootstrap_interval_brackets_a_clear_shift() -> None:
    mean, lo, hi = bootstrap_delta([0.5] * 8)
    assert mean == 0.5
    assert lo == hi == 0.5
    mean, lo, hi = bootstrap_delta([0.0, 0.0, 0.0, 0.0])
    assert (mean, lo, hi) == (0.0, 0.0, 0.0)


def test_verdict_needs_both_the_sign_test_and_the_interval(tmp_path: Path) -> None:
    """Six tasks that all move the same way should read as a win.

    Either statistic alone misreads one shape of result, so the verdict is
    gated on both agreeing; this is the case where they do.
    """
    for i in range(6):
        for tag in range(3):
            _trial(tmp_path, "a", i, f"task-{i}", f"h{tag}", reward=0.0, calls=4)
            _trial(tmp_path, "b", i, f"task-{i}", f"h{tag}", reward=1.0, calls=4)
    text = report(collect(tmp_path))
    assert "arm b wins" in text
    assert "mean per-task change +1.0000" in text


def test_one_lopsided_task_does_not_carry_a_verdict(tmp_path: Path) -> None:
    """A single task moving cannot be enough, however far it moves."""
    _trial(tmp_path, "a", 0, "mover", "h0", reward=0.0, calls=4)
    _trial(tmp_path, "b", 0, "mover", "h0", reward=1.0, calls=4)
    for i in range(1, 5):
        _trial(tmp_path, "a", i, f"tied-{i}", "h0", reward=1.0, calls=4)
        _trial(tmp_path, "b", i, f"tied-{i}", "h0", reward=1.0, calls=4)
    assert "no separation at this sample size" in report(collect(tmp_path))


def test_spread_is_reported_per_arm(tmp_path: Path) -> None:
    """The readout that answers whether a sampling knob had any effect."""
    for tag, calls in enumerate((10, 10, 10)):
        _trial(tmp_path, "a", 0, "task", f"h{tag}", reward=1.0, calls=calls)
    for tag, calls in enumerate((2, 20, 40)):
        _trial(tmp_path, "b", 0, "task", f"h{tag}", reward=1.0, calls=calls)
    text = report(collect(tmp_path))
    assert "arm a: 3/3 attempts passed  pass rate 1.0000  trajectory spread 0.0" in text
    assert "arm b: 3/3 attempts passed  pass rate 1.0000  trajectory spread 19.0" in text
