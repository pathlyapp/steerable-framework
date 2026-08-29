from __future__ import annotations

import json
from pathlib import Path

from evals.run import EXIT_HARBOR, EXIT_OK, EXIT_SKIPPED, EXIT_USAGE, _print_summary, main
from evals.suite import STEERABLE_IMPORT_PATH


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


def test_dry_run_steerable_uses_import_path(capsys) -> None:
    code = main(
        [
            "--agent",
            "steerable",
            "--split",
            "oracle-canary",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert STEERABLE_IMPORT_PATH in captured.out
    assert "--include-task-name terminal-bench/fix-git" in captured.out


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

