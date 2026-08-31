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
    return JobResult(
        label=f"{harness}-{model}",
        agent="a",
        model=model,
        harness=harness,
        scores=scores,
        metrics={},
    )


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


def _write_log(trial: Path, lines: list[str]) -> None:
    agent = trial / "agent"
    agent.mkdir(exist_ok=True)
    (agent / "headless.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_rounds_counted_from_tool_markers(tmp_path: Path) -> None:
    job_dir = _write_job(tmp_path, "run", model="m", scores={"t/a": 1.0})
    trial = next(p for p in job_dir.iterdir() if p.is_dir())
    _write_log(trial, ["[thinking]", "[tool bash {'command': 'ls'}]", "done", "[tool read_file {}]"])
    job = load_job(job_dir, harness="default")
    assert job.metrics["t/a"].rounds == 2
    # No summary line → the cost axis is absent, not zero.
    assert job.metrics["t/a"].input_tokens is None
    assert job.metrics["t/a"].tool_errors is None


def test_run_summary_line_supplies_full_metrics(tmp_path: Path) -> None:
    job_dir = _write_job(tmp_path, "run", model="m", scores={"t/a": 1.0})
    trial = next(p for p in job_dir.iterdir() if p.is_dir())
    _write_log(
        trial,
        [
            "[tool bash {}]",
            'STEERABLE_RUN_SUMMARY {"rounds": 7, "input_tokens": 12000,'
            ' "output_tokens": 800, "cost_usd": 0.0042, "peak_context_tokens": 31000,'
            ' "tool_errors": 2, "tool_recoveries": 1}',
        ],
    )
    m = load_job(job_dir, harness="default").metrics["t/a"]
    assert m.rounds == 7  # summary overrides the marker count
    assert m.input_tokens == 12000
    assert m.output_tokens == 800
    assert m.cost_usd == pytest.approx(0.0042)
    assert m.peak_context_tokens == 31000
    assert m.tool_errors == 2
    assert m.tool_recoveries == 1


def test_truncated_summary_line_loses_telemetry_not_score(tmp_path: Path) -> None:
    job_dir = _write_job(tmp_path, "run", model="m", scores={"t/a": 1.0})
    trial = next(p for p in job_dir.iterdir() if p.is_dir())
    _write_log(trial, ["[tool bash {}]", 'STEERABLE_RUN_SUMMARY {"rounds":'])
    job = load_job(job_dir, harness="default")
    assert job.scores["t/a"] == 1.0
    assert job.metrics["t/a"].rounds == 1  # marker count survives


def test_efficiency_renders_na_when_metrics_absent(tmp_path: Path) -> None:
    job_dir = _write_job(tmp_path, "run", model="m", scores={"t/a": 1.0})
    trial = next(p for p in job_dir.iterdir() if p.is_dir())
    _write_log(trial, ["[tool bash {}]", "[tool bash {}]"])
    text = render_markdown(attribute([load_job(job_dir, harness="default")]))
    assert "| `default` | 2.0 | n/a | n/a | n/a | n/a | n/a |" in text


def test_efficiency_aggregates_full_data(tmp_path: Path) -> None:
    job_dir = _write_job(tmp_path, "run", model="m", scores={"t/a": 1.0, "t/b": 0.0})
    for i, trial in enumerate(sorted(p for p in job_dir.iterdir() if p.is_dir())):
        _write_log(
            trial,
            [
                f'STEERABLE_RUN_SUMMARY {{"rounds": {4 + i * 2}, "input_tokens": 10000,'
                f' "output_tokens": 1000, "tool_errors": 2, "tool_recoveries": 1}}'
            ],
        )
    report = attribute([load_job(job_dir, harness="default")])
    eff = report.efficiency["default"]
    assert eff.mean_rounds == pytest.approx(5.0)
    assert eff.mean_tokens == pytest.approx(11_000)
    assert eff.tool_error_rate == pytest.approx(4 / 10)
    assert eff.recovery_rate == pytest.approx(2 / 4)


def test_efficiency_refuses_partial_data(tmp_path: Path) -> None:
    # One trial with a summary, one without: the token mean would silently
    # lean on the reporting task, so the field must go n/a instead.
    # Rounds stay available — every trial carries the marker-count floor.
    job_dir = _write_job(tmp_path, "run", model="m", scores={"t/a": 1.0, "t/b": 1.0})
    trials = sorted(p for p in job_dir.iterdir() if p.is_dir())
    _write_log(trials[0], ['STEERABLE_RUN_SUMMARY {"rounds": 4, "input_tokens": 100, "output_tokens": 50}'])
    _write_log(trials[1], ["[tool bash {}]"])
    eff = attribute([load_job(job_dir, harness="default")]).efficiency["default"]
    assert eff.mean_tokens is None
    assert eff.mean_rounds == pytest.approx(2.5)
