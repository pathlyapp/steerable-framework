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
    main,
)
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


def test_dry_run_steerable_with_harness_passes_spec_kwarg(capsys) -> None:
    code = main(
        [
            "--agent",
            "steerable",
            "--split",
            "oracle-canary",
            "--harness",
            "default",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "--agent-kwarg harness=" in captured.out
    assert "default.harness.yaml" in captured.out
    # The run unit is agent × harness: jobs land in their own directory.
    assert "steerable-default" in captured.out


def test_harness_rejected_for_baseline_agents(capsys) -> None:
    code = main(
        ["--agent", "codex", "--split", "oracle-canary", "--harness", "default", "--dry-run"]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "does not accept a harness dimension" in captured.err


def test_unknown_harness_fails_loud(capsys) -> None:
    code = main(
        ["--agent", "steerable", "--split", "oracle-canary", "--harness", "nope", "--dry-run"]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "unknown harness" in captured.err


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


def test_harbor_child_env_moves_proxy_off_docker() -> None:
    out = _harbor_child_env(
        {"HTTP_PROXY": "http://127.0.0.1:7890", "OPENAI_API_KEY": "x"}
    )
    assert "HTTP_PROXY" not in out
    assert out["STEERABLE_HOST_PROXY"] == "http://127.0.0.1:7890"
    assert out["OPENAI_API_KEY"] == "x"

