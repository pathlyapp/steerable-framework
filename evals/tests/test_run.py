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
    _retry_agent_timeout,
    env_start_error_tasks,
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
        suite.catalog,
        shard=0,
        shards=8,
        minutes=suite.catalog_minutes,
        pack_floor=suite.pack_floor_minutes,
    )
    for task in expected:
        assert f"--include-task-name terminal-bench/{task}" in captured.out


def test_dry_run_failed_prev_shard_selects_a_slice(capsys) -> None:
    code = main(
        [
            "--agent",
            "steerable",
            "--split",
            "failed-prev",
            "--shard",
            "0",
            "--shards",
            "4",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    suite = load_suite()
    expected = shard_tasks(
        suite.splits["failed-prev"],
        shard=0,
        shards=4,
        minutes=suite.catalog_minutes,
        pack_floor=suite.pack_floor_minutes,
    )
    assert expected
    includes = captured.out.count("--include-task-name")
    assert includes == len(expected)
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


def test_env_start_error_tasks_detects_docker_tls(tmp_path: Path) -> None:
    trial = tmp_path / "2026-08-31__01-44-17" / "protein-assembly__7EyVCbZ"
    trial.mkdir(parents=True)
    (trial / "exception.txt").write_text(
        "RuntimeError: Docker compose command failed for environment "
        'protein-assembly. net/http: TLS handshake timeout\n'
    )
    other = tmp_path / "2026-08-31__01-44-17" / "video-processing__abc"
    other.mkdir()
    (other / "exception.txt").write_text("AssertionError: landing frame\n")
    assert env_start_error_tasks(tmp_path) == ("protein-assembly",)


def test_retry_agent_timeout_fits_remaining_gha_wall() -> None:
    assert _retry_agent_timeout(None) is None
    assert _retry_agent_timeout(3) == 3
    assert _retry_agent_timeout(6) == 6
    assert _retry_agent_timeout(12) == 6


def test_print_summary_retry_job_clears_env_start_error(
    tmp_path: Path, capsys
) -> None:
    first = tmp_path / "2026-08-31__01-44-17"
    first.mkdir()
    (first / "result.json").write_text(
        json.dumps(
            {
                "stats": {
                    "n_completed_trials": 2,
                    "n_errored_trials": 1,
                    "evals": {
                        "steerable": {
                            "metrics": [{"mean": 0.5}],
                            "reward_stats": {"reward": {"1.0": ["largest-eigenval__a"]}},
                            "exception_stats": {
                                "RuntimeError": ["protein-assembly__b"]
                            },
                        }
                    },
                }
            }
        )
    )
    (first / "protein-assembly__b").mkdir()
    (first / "protein-assembly__b" / "exception.txt").write_text(
        "TLS handshake timeout\n"
    )
    retry = tmp_path / "2026-08-31__04-20-00"
    retry.mkdir()
    (retry / "result.json").write_text(
        json.dumps(
            {
                "stats": {
                    "n_completed_trials": 1,
                    "n_errored_trials": 0,
                    "evals": {
                        "steerable": {
                            "metrics": [{"mean": 1.0}],
                            "reward_stats": {
                                "reward": {"1.0": ["protein-assembly__c"]}
                            },
                        }
                    },
                }
            }
        )
    )
    assert _print_summary(tmp_path) == EXIT_OK
    assert "errored=0" in capsys.readouterr().out

