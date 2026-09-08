"""Tests for the cross-run catalog stratifier.

The tiers it emits decide which tasks gate regression runs and which feed
A/B pairs, and the context classification steers roadmap investment — so
the cases below pin the tier boundaries, the reward-less trial exclusion,
and the pressure-threshold read of ``peak_context_tokens``.
"""

import json
from pathlib import Path

from evals.stratify_catalog import (
    collect,
    count_incomplete,
    main,
    report,
    tally,
)


def _trial(
    root: Path,
    run: str,
    task: str,
    tag: str,
    *,
    reward: float | None,
    peak: int | None = None,
) -> None:
    """Write one Harbor-shaped trial directory under a run's artifact."""
    trial = (
        root
        / run
        / "eval-steerable-0"
        / "evals"
        / "jobs"
        / "steerable"
        / "ts"
        / f"{task}__{tag}"
    )
    (trial / "agent").mkdir(parents=True, exist_ok=True)
    payload = {"verifier_result": {"rewards": {}}}
    if reward is not None:
        payload["verifier_result"]["rewards"]["reward"] = reward
    (trial / "result.json").write_text(json.dumps(payload))
    if peak is not None:
        (trial / "agent" / "headless.log").write_text(
            f'STEERABLE_RUN_SUMMARY {{"rounds": 3, "peak_context_tokens": {peak}}}\n'
        )


def test_tiers_split_on_full_pass_and_full_fail(tmp_path: Path) -> None:
    for run in ("r1", "r2", "r3"):
        _trial(tmp_path, run, "green", "h", reward=1.0)
        _trial(tmp_path, run, "red", "h", reward=0.0)
    _trial(tmp_path, "r1", "mixed", "h", reward=1.0)
    _trial(tmp_path, "r2", "mixed", "h", reward=0.0)
    _trial(tmp_path, "r3", "mixed", "h", reward=0.0)

    tallies = tally(collect(tmp_path), context_window=1000, pressure_frac=0.9)
    text = report(
        tallies, runs=3, context_window=1000, pressure_frac=0.9, incomplete=0
    )
    assert "stable-green (1)" in text
    assert "stable-red   (1)" in text
    assert "flaky        (1)" in text
    assert "1/3  mixed" in text


def test_a_trial_without_a_reward_is_not_a_failure(tmp_path: Path) -> None:
    """An errored trial is absent, not a zero — infrastructure failures
    must not push a task into the stable-red tier."""
    _trial(tmp_path, "r1", "task", "h", reward=1.0)
    _trial(tmp_path, "r2", "task", "h", reward=None)
    data = collect(tmp_path)
    assert [t.passed for t in data["task"]["r1"]] == [True]
    assert data["task"].get("r2") is None
    assert count_incomplete(tmp_path) == 1


def test_context_pressure_counts_only_failed_trials_over_threshold(
    tmp_path: Path,
) -> None:
    # Fails at 95% of the window: a context-length failure.
    _trial(tmp_path, "r1", "ctx", "h", reward=0.0, peak=950)
    # Fails at 50%: a capability failure.
    _trial(tmp_path, "r2", "ctx", "h", reward=0.0, peak=500)
    # Passes at 95%: pressure alone is not a failure.
    _trial(tmp_path, "r3", "ctx", "h", reward=1.0, peak=950)
    # Fails with no log: counted as other, never as context.
    _trial(tmp_path, "r4", "ctx", "h", reward=0.0)

    tallies = tally(collect(tmp_path), context_window=1000, pressure_frac=0.9)
    (row,) = [t for t in tallies if t.task == "ctx"]
    assert (row.passes, row.trials) == (1, 4)
    assert row.context_failures == 1
    assert row.other_failures == 2


def test_run_label_is_the_download_directory(tmp_path: Path) -> None:
    """Cross-run aggregation keys on the per-run download dir; a flat
    single-run root still collects under the ``.`` label."""
    _trial(tmp_path, "34031313764", "task", "h", reward=1.0)
    data = collect(tmp_path)
    assert list(data["task"]) == ["34031313764"]


def test_main_writes_json_sidecar(tmp_path: Path, capsys) -> None:
    _trial(tmp_path, "r1", "task", "h", reward=0.0, peak=950)
    out = tmp_path / "strata.json"
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "--context-window",
                "1000",
                "--json",
                str(out),
            ]
        )
        == 0
    )
    payload = json.loads(out.read_text())
    assert payload["runs"] == 1
    assert payload["tasks"][0]["task"] == "task"
    assert payload["tasks"][0]["context_failures"] == 1
    assert "stable-red   (1)" in capsys.readouterr().out


def test_main_rejects_a_missing_root(tmp_path: Path) -> None:
    assert main(["--root", str(tmp_path / "nope")]) == 1
