"""Per-task set diff across two Harbor job dirs."""

from __future__ import annotations

import json
from pathlib import Path

from evals.task_diff import diff_jobs, main, task_id


def _write_job(root: Path, name: str, scores: dict[str, float], agent: str = "a") -> Path:
    job = root / name
    job.mkdir(parents=True)
    (job / "config.json").write_text(
        json.dumps({"agents": [{"name": agent, "model_name": "m"}]}),
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


def test_task_id_strips_dataset_prefix() -> None:
    assert task_id("terminal-bench/fix-git") == "fix-git"
    assert task_id("fix-git") == "fix-git"


def test_they_passed_we_failed_normalises_prefixes(tmp_path: Path) -> None:
    they = _write_job(
        tmp_path,
        "claude",
        {"terminal-bench/fix-git": 1.0, "terminal-bench/regex-chess": 1.0, "gcode-to-text": 0.0},
        agent="claude-code",
    )
    we = _write_job(
        tmp_path,
        "steerable",
        {"fix-git": 1.0, "regex-chess": 0.0, "gcode-to-text": 1.0},
        agent="steerable",
    )
    report = diff_jobs(they, we)
    assert report.they_passed_we_failed == ("regex-chess",)
    assert report.we_passed_they_failed == ("gcode-to-text",)
    assert report.both_passed == ("fix-git",)
    assert report.both_failed == ()


def test_missing_on_one_side_counts_as_a_failure_there(tmp_path: Path) -> None:
    they = _write_job(tmp_path, "claude", {"a": 1.0, "b": 1.0})
    we = _write_job(tmp_path, "steerable", {"a": 0.0})
    report = diff_jobs(they, we)
    assert report.they_passed_we_failed == ("a", "b")
    assert report.they_only == ("b",)
    assert report.we_only == ()


def test_cli_json(tmp_path: Path, capsys) -> None:
    they = _write_job(tmp_path, "claude", {"x": 1.0})
    we = _write_job(tmp_path, "ours", {"x": 0.0})
    assert main(["--they", str(they), "--we", str(we), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["they_passed_we_failed"] == ["x"]
    assert payload["we_passed_they_failed"] == []
