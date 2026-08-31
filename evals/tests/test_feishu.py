from __future__ import annotations

import json
from pathlib import Path

from evals.feishu import (
    agent_line,
    build_message,
    card_payload,
    collect_rows,
    merge_summaries,
    outcome_from_trial_dir,
    overall_ok,
    summarize_result,
    trial_task_id,
)


def test_trial_task_id_strips_harbor_suffix() -> None:
    assert trial_task_id("fix-git__jsWvBUi") == "fix-git"


def test_summarize_result_reads_mean_and_reward_buckets() -> None:
    payload = {
        "stats": {
            "n_completed_trials": 12,
            "n_errored_trials": 0,
            "evals": {
                "steerable": {
                    "metrics": [{"mean": 0.75}],
                    "reward_stats": {
                        "reward": {
                            "1.0": ["fix-git__a", "polyglot-c-py__b"],
                            "0.0": ["nginx-request-logging__c"],
                        }
                    },
                }
            },
        }
    }
    summary = summarize_result(payload)
    assert summary["mean"] == 0.75
    assert summary["passed"] == ["fix-git", "polyglot-c-py"]
    assert summary["failed"] == ["nginx-request-logging"]
    assert summary["n_errored"] == 0


def test_overall_ok_requires_a_clean_run() -> None:
    ran = ("steerable", "ran", {"mean": 0.75, "passed": ["a"], "failed": ["b"], "n_errored": 0, "n_completed": 12})
    skipped = ("pi", "skipped", None)
    errored = ("steerable", "ran", {"mean": None, "passed": [], "failed": [], "n_errored": 2, "n_completed": 2})
    assert overall_ok([ran, skipped]) is True
    assert overall_ok([skipped]) is False
    assert overall_ok([errored]) is False


def test_title_starts_with_success_or_failure() -> None:
    rows = [
        (
            "steerable",
            "ran",
            {
                "mean": 0.75,
                "passed": ["fix-git"],
                "failed": ["nginx-request-logging"],
                "n_errored": 0,
                "n_completed": 12,
            },
        )
    ]
    ok, title, body = build_message(rows, label="cheap-12", run_url="https://example.test/run")
    assert ok is True
    assert title.startswith("成功 · ")
    assert "0.750" in title
    assert "steerable 未过: nginx-request-logging" in body
    assert "https://example.test/run" in body
    card = card_payload(ok=ok, title=title, body=body)
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "green"


def test_collect_rows_from_weekly_artifact_layout(tmp_path: Path) -> None:
    (tmp_path / "eval-status-steerable.txt").write_text("ran\n")
    job = tmp_path / "evals" / "jobs" / "steerable" / "2026-08-30__00-00-00"
    job.mkdir(parents=True)
    (job / "result.json").write_text(
        json.dumps(
            {
                "stats": {
                    "n_completed_trials": 1,
                    "n_errored_trials": 0,
                    "evals": {"steerable": {"metrics": [{"mean": 1.0}], "reward_stats": {"reward": {"1.0": ["fix-git__x"]}}}},
                }
            }
        )
    )
    (tmp_path / "eval-status-pi.txt").write_text("skipped\n")
    rows = collect_rows(tmp_path)
    assert [r[0] for r in rows] == ["pi", "steerable"]
    assert rows[0][1] == "skipped"
    assert rows[1][2] is not None
    assert rows[1][2]["mean"] == 1.0
    assert agent_line("pi", "skipped", None) == "pi: 跳过"


def test_collect_rows_skips_trial_json_and_names_agent_from_evals_key(tmp_path: Path) -> None:
    job = tmp_path / "2026-08-30__10-52-39"
    job.mkdir()
    (job / "result.json").write_text(
        json.dumps(
            {
                "stats": {
                    "n_completed_trials": 1,
                    "n_errored_trials": 0,
                    "evals": {
                        "oracle__terminal-bench/terminal-bench-2-1": {
                            "metrics": [{"mean": 1.0}],
                            "reward_stats": {"reward": {"1.0": ["fix-git__abc"]}},
                        }
                    },
                }
            }
        )
    )
    trial = job / "fix-git__abc"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps({"stats": {"n_completed_trials": 1, "evals": {}}})
    )
    rows = collect_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0][0] == "oracle"
    assert rows[0][2] is not None
    assert rows[0][2]["mean"] == 1.0
    assert rows[0][2]["passed"] == ["fix-git"]


def test_collect_rows_keeps_both_agents_when_job_stamps_collide(tmp_path: Path) -> None:
    """GHA download-artifact without merge-multiple: artifact name is the prefix."""
    stamp = "2026-08-30__11-00-06"
    for agent, mean in (("oracle", 1.0), ("steerable", 1.0)):
        job = tmp_path / f"{agent}-fix-git" / stamp
        job.mkdir(parents=True)
        (job / "result.json").write_text(
            json.dumps(
                {
                    "stats": {
                        "n_completed_trials": 1,
                        "n_errored_trials": 0,
                        "evals": {
                            f"{agent}__terminal-bench/terminal-bench-2-1": {
                                "metrics": [{"mean": mean}],
                                "reward_stats": {"reward": {"1.0": ["fix-git__x"]}},
                            }
                        },
                    }
                }
            )
        )
    rows = collect_rows(tmp_path)
    assert [r[0] for r in rows] == ["oracle", "steerable"]
    assert all(r[2] is not None and r[2]["mean"] == 1.0 for r in rows)
    _ok, title, body = build_message(rows, label="GHA oracle canary", run_url="")
    assert "oracle 1.000" in title
    assert "steerable 1.000" in title
    assert "oracle 已过: fix-git" in body
    assert "steerable 已过: fix-git" in body


def test_collect_rows_reads_nested_weekly_status_files(tmp_path: Path) -> None:
    steerable = tmp_path / "eval-steerable"
    job = steerable / "2026-08-30__12-00-00"
    job.mkdir(parents=True)
    (steerable / "eval-status-steerable.txt").write_text("ran\n")
    (job / "result.json").write_text(
        json.dumps(
            {
                "stats": {
                    "n_completed_trials": 1,
                    "n_errored_trials": 0,
                    "evals": {
                        "steerable__z-ai/glm-5.3-flash__terminal-bench/terminal-bench-2-1": {
                            "metrics": [{"mean": 0.75}],
                            "reward_stats": {"reward": {"1.0": ["fix-git__a"], "0.0": ["nginx-request-logging__b"]}},
                        }
                    },
                }
            }
        )
    )
    pi = tmp_path / "eval-pi"
    pi.mkdir()
    (pi / "eval-status-pi.txt").write_text("skipped\n")
    rows = {name: (status, summary) for name, status, summary in collect_rows(tmp_path)}
    assert rows["pi"][0] == "skipped"
    assert rows["steerable"][0] == "ran"
    assert rows["steerable"][1] is not None
    assert rows["steerable"][1]["mean"] == 0.75


def test_collect_rows_merges_catalog_shards(tmp_path: Path) -> None:
    for i, (mean, passed, failed, n) in enumerate(
        (
            (1.0, ["fix-git__a"], [], 1),
            (0.0, [], ["qemu-alpine-ssh__b"], 1),
        )
    ):
        shard = tmp_path / f"eval-steerable-{i}"
        job = shard / f"2026-08-30__12-0{i}-00"
        job.mkdir(parents=True)
        (shard / "eval-status-steerable.txt").write_text("ran\n")
        reward: dict[str, list[str]] = {}
        if passed:
            reward["1.0"] = passed
        if failed:
            reward["0.0"] = failed
        (job / "result.json").write_text(
            json.dumps(
                {
                    "stats": {
                        "n_completed_trials": n,
                        "n_errored_trials": 0,
                        "evals": {
                            "steerable__z-ai/glm-5.3-flash__terminal-bench/terminal-bench-2-1": {
                                "metrics": [{"mean": mean}],
                                "reward_stats": {"reward": reward},
                            }
                        },
                    }
                }
            )
        )
    rows = collect_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0][0] == "steerable"
    assert rows[0][1] == "ran"
    assert rows[0][2] is not None
    assert rows[0][2]["mean"] == 0.5
    assert rows[0][2]["n_completed"] == 2
    assert rows[0][2]["passed"] == ["fix-git"]
    assert rows[0][2]["failed"] == ["qemu-alpine-ssh"]


def test_merge_summaries_retry_overwrites_env_start_error() -> None:
    first = {
        "mean": 0.5,
        "passed": ["largest-eigenval"],
        "failed": ["video-processing"],
        "errored": ["protein-assembly"],
        "n_errored": 1,
        "n_completed": 4,
    }
    retry = {
        "mean": 1.0,
        "passed": ["protein-assembly"],
        "failed": [],
        "errored": [],
        "n_errored": 0,
        "n_completed": 1,
    }
    merged = merge_summaries([first, retry])
    assert set(merged["passed"]) == {"largest-eigenval", "protein-assembly"}
    assert merged["failed"] == ["video-processing"]
    assert merged["n_errored"] == 0
    assert merged["n_completed"] == 3
    assert merged["mean"] == 2 / 3


def test_outcome_from_trial_dir_reads_pytest_summary(tmp_path: Path) -> None:
    trial = tmp_path / "2026-08-31__01-44-00" / "build-cython-ext__abc"
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / "test-stdout.txt").write_text(
        "PASSED ../tests/test_outputs.py::test_ok\n"
        "========================= 1 passed in 0.1s =========================\n"
    )
    assert outcome_from_trial_dir(trial) == "pass"
    (trial / "verifier" / "test-stdout.txt").write_text(
        "FAILED ../tests/test_outputs.py::test_ok\n"
        "========================= 1 failed in 0.1s =========================\n"
    )
    assert outcome_from_trial_dir(trial) == "fail"
    empty = tmp_path / "2026-08-31__01-44-00" / "protein-assembly__xyz"
    empty.mkdir(parents=True)
    (empty / "exception.txt").write_text("TLS handshake timeout\n")
    assert outcome_from_trial_dir(empty) == "error"


def test_collect_rows_fills_timeout_trials_from_logs(tmp_path: Path) -> None:
    """GHA kill before job result.json: still score finished trial pytest logs."""
    shard = tmp_path / "eval-steerable-5"
    job = shard / "2026-08-31__01-44-01"
    trial = job / "build-cython-ext__abc"
    (trial / "verifier").mkdir(parents=True)
    (shard / "eval-status-steerable.txt").write_text("failed\n")
    (trial / "verifier" / "test-stdout.txt").write_text(
        "PASSED ../tests/test_outputs.py::test_ok\n"
        "========================= 1 passed in 0.1s =========================\n"
    )
    rows = collect_rows(tmp_path)
    assert rows[0][0] == "steerable"
    assert rows[0][2] is not None
    assert rows[0][2]["passed"] == ["build-cython-ext"]
    assert rows[0][2]["n_completed"] == 1
    assert rows[0][2]["mean"] == 1.0


def test_collect_rows_job_json_wins_over_trial_logs(tmp_path: Path) -> None:
    shard = tmp_path / "eval-steerable-0"
    job = shard / "2026-08-31__01-44-00"
    trial = job / "fix-git__x"
    (trial / "verifier").mkdir(parents=True)
    (shard / "eval-status-steerable.txt").write_text("ran\n")
    (trial / "verifier" / "test-stdout.txt").write_text(
        "FAILED ../tests/test_outputs.py::test_ok\n"
        "========================= 1 failed in 0.1s =========================\n"
    )
    (job / "result.json").write_text(
        json.dumps(
            {
                "stats": {
                    "n_completed_trials": 1,
                    "n_errored_trials": 0,
                    "evals": {
                        "steerable": {
                            "metrics": [{"mean": 1.0}],
                            "reward_stats": {"reward": {"1.0": ["fix-git__x"]}},
                        }
                    },
                }
            }
        )
    )
    rows = collect_rows(tmp_path)
    assert rows[0][2] is not None
    assert rows[0][2]["passed"] == ["fix-git"]
    assert rows[0][2]["failed"] == []
