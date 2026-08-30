from __future__ import annotations

import json
from pathlib import Path

from evals.run import (
    EXIT_HARBOR,
    EXIT_OK,
    EXIT_SKIPPED,
    EXIT_USAGE,
    _harbor_child_env,
    _print_summary,
    harbor_progress_line,
    main,
)
from evals.suite import STEERABLE_IMPORT_PATH, load_suite, shard_tasks


def test_dry_run_oracle_prints_harbor_command(capsys) -> None:
    code = main(
        [
            "--agent",
            "oracle",
            "--split",
            "oracle-canary",
            "--dry-run",
            "--jobs-dir",
            "evals/jobs/oracle",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "--agent oracle" in captured.out
    assert "--include-task-name terminal-bench/fix-git" in captured.out
    assert "--model" not in captured.out


def test_dry_run_catalog_shard_selects_a_slice(capsys) -> None:
    code = main(
        [
            "--agent",
            "steerable",
            "--split",
            "catalog",
            "--shard",
            "0",
            "--shards",
            "8",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    includes = captured.out.count("--include-task-name")
    assert 10 <= includes <= 12
    suite = load_suite()
    expected = shard_tasks(
        suite.catalog, shard=0, shards=8, minutes=suite.catalog_minutes
    )
    for task in expected:
        assert f"--include-task-name terminal-bench/{task}" in captured.out


def test_dry_run_steerable_uses_import_path(capsys) -> None:
    code = main(
        [
            "--agent",
            "steerable",
            "--split",
            "oracle-canary",
            "--dry-run",
            "--environment-build-timeout-multiplier",
            "3",
            "--agent-timeout-multiplier",
            "3",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert STEERABLE_IMPORT_PATH in captured.out
    assert "--model openai/z-ai/glm-5.3-flash" in captured.out
    assert "--include-task-name terminal-bench/fix-git" in captured.out
    assert "--environment-build-timeout-multiplier 3" in captured.out
    assert "--agent-timeout-multiplier 3" in captured.out


def test_skip_missing_env_exits_3_for_pi(capsys, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = main(
        [
            "--agent",
            "pi",
            "--split",
            "oracle-canary",
            "--skip-missing-env",
        ]
    )
    captured = capsys.readouterr()
    assert code == EXIT_SKIPPED
    assert "ANTHROPIC_API_KEY" in captured.err


def test_dsh_is_usage_error(capsys) -> None:
    code = main(["--agent", "dsh", "--split", "cheap-12", "--dry-run"])
    captured = capsys.readouterr()
    assert code == EXIT_USAGE
    assert "Harbor" in captured.err


def test_print_summary_requires_mean_and_rejects_errors(
    tmp_path: Path, capsys
) -> None:
    job = tmp_path / "2026-08-29__12-00-00"
    job.mkdir()
    (job / "result.json").write_text(
        json.dumps(
            {
                "stats": {
                    "n_errored_trials": 0,
                    "evals": {
                        "oracle": {"metrics": [{"mean": 1.0}]},
                    },
                }
            }
        )
    )
    assert _print_summary(tmp_path, require_mean=1.0) == EXIT_OK

    (job / "result.json").write_text(
        json.dumps({"stats": {"n_errored_trials": 2, "evals": {}}})
    )
    assert _print_summary(tmp_path) == EXIT_HARBOR
    assert "errored" in capsys.readouterr().err

    (job / "result.json").write_text(json.dumps({"stats": {"n_errored_trials": 0}}))
    assert _print_summary(tmp_path, require_mean=1.0) == EXIT_HARBOR


def test_print_summary_missing_stats(tmp_path: Path) -> None:
    job = tmp_path / "empty"
    job.mkdir()
    (job / "result.json").write_text("{}")
    assert _print_summary(tmp_path) == EXIT_HARBOR


def test_print_summary_no_result(tmp_path: Path) -> None:
    assert _print_summary(tmp_path) == EXIT_HARBOR


def test_print_summary_appends_github_step_summary(
    tmp_path: Path, monkeypatch
) -> None:
    job = tmp_path / "2026-08-29__12-00-00"
    job.mkdir()
    (job / "result.json").write_text(
        json.dumps(
            {
                "stats": {
                    "n_errored_trials": 0,
                    "evals": {"steerable": {"metrics": [{"mean": 0.75}]}},
                }
            }
        )
    )
    summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert _print_summary(tmp_path) == EXIT_OK
    text = summary.read_text()
    assert "Mean: 0.750" in text
    assert "n_errored_trials: 0" in text


def test_harbor_child_env_moves_proxy_off_docker() -> None:
    out = _harbor_child_env(
        {"HTTP_PROXY": "http://127.0.0.1:7890", "OPENAI_API_KEY": "x"}
    )
    assert "HTTP_PROXY" not in out
    assert out["STEERABLE_HOST_PROXY"] == "http://127.0.0.1:7890"
    assert out["OPENAI_API_KEY"] == "x"


def test_harbor_progress_line_counts_finished_trials(tmp_path: Path) -> None:
    assert harbor_progress_line(tmp_path / "missing") == (
        "harbor progress: 0/0 trials done"
    )
    job = tmp_path / "2026-08-30__12-00-00"
    done = job / "fix-git__abc"
    running = job / "nginx-request-logging__def"
    done.mkdir(parents=True)
    running.mkdir()
    (done / "result.json").write_text("{}")
    (job / "result.json").write_text("{}")
    line = harbor_progress_line(tmp_path)
    assert line == "harbor progress: 1/2 trials done (fix-git)"

