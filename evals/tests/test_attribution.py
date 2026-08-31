"""W1.3.3: attribution report — loading, effects, rank reversals, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.attribution import (
    JobResult,
    attribute,
    load_job,
    main,
    render_markdown,
)


def _write_job(
    root: Path,
    name: str,
    *,
    model: str,
    scores: dict[str, float],
    agent: str = "evals.harbor_steerable:SteerableHarborAgent",
) -> Path:
    job = root / name
    job.mkdir(parents=True)
    (job / "config.json").write_text(
        json.dumps({"agents": [{"name": agent, "model_name": model}]}),
        encoding="utf-8",
    )
    for i, (task, reward) in enumerate(scores.items()):
        trial = job / f"{task.split('/')[-1]}__t{i}"
        trial.mkdir()
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "task_name": task,
                    "verifier_result": {"rewards": {"reward": reward}},
                }
            ),
            encoding="utf-8",
        )
    return job


def test_load_job_projects_labels_and_scores(tmp_path: Path) -> None:
    job_dir = _write_job(
        tmp_path, "run-1", model="openai/m", scores={"terminal-bench/a": 1.0, "terminal-bench/b": 0.0}
    )
    job = load_job(job_dir, harness="default")
    assert job.agent.endswith("SteerableHarborAgent")
    assert job.model == "openai/m"
    assert job.harness == "default"
    assert job.scores == {"terminal-bench/a": 1.0, "terminal-bench/b": 0.0}


def test_load_job_fails_loud_on_missing_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no config.json"):
        load_job(tmp_path / "nope", harness="x")


def test_load_job_fails_loud_on_missing_model(tmp_path: Path) -> None:
    job = tmp_path / "run"
    job.mkdir()
    (job / "config.json").write_text(json.dumps({"agents": [{"name": "a"}]}))
    with pytest.raises(ValueError, match="model_name"):
        load_job(job, harness="x")


def test_load_job_skips_unscored_trials(tmp_path: Path) -> None:
    job_dir = _write_job(tmp_path, "run", model="m", scores={"terminal-bench/a": 1.0})
    crashed = job_dir / "crashed__t9"
    crashed.mkdir()
    (crashed / "result.json").write_text(
        json.dumps({"task_name": "terminal-bench/crashed", "exception_info": {"msg": "boom"}})
    )
    job = load_job(job_dir, harness="x")
    # A trial the verifier never scored is absent, not a zero — counting it
    # would conflate infra failure with task failure.
    assert job.scores == {"terminal-bench/a": 1.0}


def _job(harness: str, model: str, scores: dict[str, float]) -> JobResult:
    return JobResult(label=f"{harness}-{model}", agent="a", model=model, harness=harness, scores=scores)


def test_harness_main_effect_within_one_model() -> None:
    tasks = [f"t/{i}" for i in range(4)]
    jobs = [
        _job("default", "m", dict(zip(tasks, [1.0, 1.0, 0.0, 1.0]))),
        _job("minimal", "m", dict(zip(tasks, [0.0, 1.0, 0.0, 0.0]))),
    ]
    report = attribute(jobs)
    assert report.harness_means["default"] == pytest.approx(0.75)
    assert report.harness_means["minimal"] == pytest.approx(0.25)
    assert report.harness_effect == pytest.approx(0.5)
    assert report.model_effect is None  # one model: nothing to compare
    assert report.effect_ratio is None


def test_effect_ratio_and_rank_reversal() -> None:
    tasks = [f"t/{i}" for i in range(4)]
    jobs = [
        # harness default: model-a wins
        _job("default", "model-a", dict(zip(tasks, [1.0, 1.0, 1.0, 0.0]))),
        _job("default", "model-b", dict(zip(tasks, [0.0, 0.0, 1.0, 0.0]))),
        # harness minimal: model-b wins — a rank reversal
        _job("minimal", "model-a", dict(zip(tasks, [0.0, 1.0, 0.0, 0.0]))),
        _job("minimal", "model-b", dict(zip(tasks, [1.0, 1.0, 1.0, 1.0]))),
    ]
    report = attribute(jobs)
    # harness effect: within model-a |0.75−0.25|=0.5, within model-b
    # |0.25−1.0|=0.75 → mean 0.625
    assert report.harness_effect == pytest.approx(0.625)
    # model effect: within default |0.75−0.25|=0.5, within minimal
    # |0.25−1.0|=0.75 → mean 0.625
    assert report.model_effect == pytest.approx(0.625)
    assert report.effect_ratio == pytest.approx(1.0)
    assert report.rank_reversals == 1


def test_only_shared_tasks_enter_means() -> None:
    jobs = [
        _job("h1", "m", {"t/a": 1.0, "t/b": 1.0}),
        _job("h2", "m", {"t/a": 0.0}),  # t/b missing: still running or crashed
    ]
    report = attribute(jobs)
    assert report.n_tasks == 1
    assert report.harness_means == {"h1": 1.0, "h2": 0.0}


def test_disjoint_task_sets_fail_loud() -> None:
    jobs = [_job("h1", "m", {"t/a": 1.0}), _job("h2", "m", {"t/b": 0.0})]
    with pytest.raises(ValueError, match="share no scored tasks"):
        attribute(jobs)


def test_render_markdown_discloses_all_four_metrics() -> None:
    jobs = [
        _job("default", "m", {"t/a": 1.0}),
        _job("minimal", "m", {"t/a": 0.0}),
    ]
    text = render_markdown(attribute(jobs))
    assert "harness main effect" in text
    assert "model main effect" in text
    assert "effect ratio" in text
    assert "rank reversals" in text


def test_cli_end_to_end(tmp_path: Path, capsys) -> None:
    a = _write_job(tmp_path, "run-a", model="m", scores={"t/a": 1.0, "t/b": 1.0})
    b = _write_job(tmp_path, "run-b", model="m", scores={"t/a": 0.0, "t/b": 1.0})
    out = tmp_path / "report.md"
    rc = main(["--job", f"default={a}", "--job", f"minimal={b}", "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "harness `default` | 1.000" in text
    assert "harness `minimal` | 0.500" in text


def test_cli_rejects_malformed_job_spec(capsys) -> None:
    assert main(["--job", "no-equals-sign"]) == 1
