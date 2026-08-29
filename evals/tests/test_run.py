from __future__ import annotations

from evals.run import EXIT_SKIPPED, EXIT_USAGE, main


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
    assert "--include-task-name fix-git" in captured.out
    assert "--model" not in captured.out


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
