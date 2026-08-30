from __future__ import annotations

import json
from pathlib import Path

from evals.feishu import (
    agent_line,
    build_message,
    card_payload,
    collect_rows,
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
    assert "nginx-request-logging" in body
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
