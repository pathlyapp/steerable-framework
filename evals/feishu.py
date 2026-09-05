"""Post Harbor eval summaries to a Feishu custom bot.

Stdlib only so GitHub Actions gather jobs can run ``python3 -m evals.feishu``
without ``uv sync``. The webhook URL is read from the environment; never
commit it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_JOB_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}__")

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_POST = 2


def trial_task_id(trial_name: str) -> str:
    """Harbor trial folder ``fix-git__abc123`` → catalog id ``fix-git``."""
    return trial_name.rsplit("__", 1)[0]


def outcome_from_trial_dir(trial: Path) -> str | None:
    """``pass`` / ``fail`` / ``error`` from logs when job ``result.json`` is missing.

    GHA may kill Harbor at 360 min before the job-level ``result.json`` is
    written. Finished trials still have ``verifier/test-stdout.txt``.
    """
    stdout = trial / "verifier" / "test-stdout.txt"
    if stdout.is_file():
        try:
            text = stdout.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if re.search(r"\d+ failed", text) or re.search(r"(?m)^FAILED ", text):
            return "fail"
        if re.search(r"\d+ passed", text) or re.search(r"(?m)^PASSED ", text):
            return "pass"
    if (trial / "exception.txt").is_file():
        return "error"
    return None


def trial_log_summaries(root: Path) -> dict[str, dict[str, Any]]:
    """Per-agent pass/fail from trial dirs (timeout shards without job json)."""
    by_agent: dict[str, dict[str, str]] = {}
    if not root.is_dir():
        return {}
    for trial in root.rglob("*"):
        if not trial.is_dir() or "__" not in trial.name:
            continue
        if not _JOB_DIR.match(trial.parent.name):
            continue
        kind = outcome_from_trial_dir(trial)
        if kind is None:
            continue
        agent = agent_from_path(trial) or "steerable"
        by_agent.setdefault(agent, {})[trial_task_id(trial.name)] = kind
    out: dict[str, dict[str, Any]] = {}
    for agent, outcome in by_agent.items():
        passed = [task for task, kind in outcome.items() if kind == "pass"]
        failed = [task for task, kind in outcome.items() if kind == "fail"]
        errored = [task for task, kind in outcome.items() if kind == "error"]
        n_completed = len(outcome)
        out[agent] = {
            "mean": (len(passed) / n_completed) if n_completed else None,
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "n_errored": len(errored),
            "n_completed": n_completed,
        }
    return out


def summarize_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull Mean, pass/fail ids, and error count from a Harbor ``result.json``."""
    stats = payload.get("stats") if isinstance(payload, dict) else None
    if not isinstance(stats, dict):
        return {
            "mean": None,
            "passed": [],
            "failed": [],
            "errored": [],
            "n_errored": 0,
            "n_completed": 0,
        }
    n_errored = int(stats.get("n_errored_trials") or 0)
    n_completed = int(stats.get("n_completed_trials") or 0)
    mean: float | None = None
    passed: list[str] = []
    failed: list[str] = []
    errored: list[str] = []
    evals = stats.get("evals") or {}
    if isinstance(evals, dict):
        for body in evals.values():
            if not isinstance(body, dict):
                continue
            for metric in body.get("metrics") or []:
                if isinstance(metric, dict) and isinstance(metric.get("mean"), (int, float)):
                    mean = float(metric["mean"])
                    break
            rewards = ((body.get("reward_stats") or {}).get("reward")) or {}
            if isinstance(rewards, dict):
                for raw, names in rewards.items():
                    if not isinstance(names, list):
                        continue
                    ids = [trial_task_id(str(n)) for n in names]
                    try:
                        value = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if value >= 1.0:
                        passed.extend(ids)
                    else:
                        failed.extend(ids)
            exceptions = body.get("exception_stats") or {}
            if isinstance(exceptions, dict):
                for names in exceptions.values():
                    if not isinstance(names, list):
                        continue
                    errored.extend(trial_task_id(str(n)) for n in names)
            break
    return {
        "mean": mean,
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "n_errored": n_errored,
        "n_completed": n_completed,
    }


def agent_line(agent: str, status: str, summary: dict[str, Any] | None) -> str:
    if status == "skipped":
        return f"{agent}: 跳过"
    if status == "failed" and summary is None:
        return f"{agent}: 失败（无 result.json）"
    if summary is None:
        return f"{agent}: {status}"
    mean = summary["mean"]
    mean_s = f"{mean:.3f}" if isinstance(mean, float) else "n/a"
    n_pass = len(summary["passed"])
    n_fail = len(summary["failed"])
    extra = f" errored={summary['n_errored']}" if summary["n_errored"] else ""
    return f"{agent}: Mean {mean_s} 通过 {n_pass} / 失败 {n_fail}{extra}"


def overall_ok(rows: list[tuple[str, str, dict[str, Any] | None]]) -> bool:
    """成功 = 至少一个 agent 跑完且无 errored trials。"""
    any_ran = False
    for _agent, status, summary in rows:
        if status == "skipped":
            continue
        if status == "failed" and summary is None:
            return False
        if summary is None:
            return False
        any_ran = True
        if summary["n_errored"]:
            return False
    return any_ran


def card_payload(*, ok: bool, title: str, body: str) -> dict[str, Any]:
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "green" if ok else "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            ],
        },
    }


def post_feishu(webhook: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def agent_from_payload(payload: dict[str, Any]) -> str | None:
    """Harbor job ``stats.evals`` keys start with the agent name."""
    stats = payload.get("stats") if isinstance(payload, dict) else None
    if not isinstance(stats, dict):
        return None
    evals = stats.get("evals") or {}
    if not isinstance(evals, dict) or not evals:
        return None
    return str(next(iter(evals))).split("__", 1)[0] or None


def agent_from_path(result: Path) -> str | None:
    parts = result.parts
    if "jobs" in parts:
        idx = parts.index("jobs")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def iter_job_result_paths(root: Path) -> list[Path]:
    """Harbor job ``result.json`` only (parent is ``YYYY-MM-DD__HH-MM-SS``)."""
    return [
        path
        for path in sorted(root.rglob("result.json"))
        if _JOB_DIR.match(path.parent.name)
    ]


def load_job_result(path: Path) -> tuple[str | None, dict[str, Any] | None]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    agent = agent_from_payload(payload) or agent_from_path(path)
    return agent, payload


def payloads_by_agent(root: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in iter_job_result_paths(root):
        agent, payload = load_job_result(result)
        if agent and payload is not None:
            grouped.setdefault(agent, []).append(payload)
    return grouped


def merge_summaries(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Weighted Mean across shards; last Harbor job wins when a task id repeats.

    Env-start retries write a second ``result.json`` in the same jobs dir.
    """
    seen: set[str] = set()
    overlap = False
    for summary in parts:
        ids = set(summary.get("passed") or [])
        ids.update(summary.get("failed") or [])
        ids.update(summary.get("errored") or [])
        if seen & ids:
            overlap = True
        seen.update(ids)
    if overlap:
        outcome: dict[str, str] = {}
        for summary in parts:
            for task in summary.get("errored") or []:
                outcome[task] = "error"
            for task in summary.get("failed") or []:
                outcome[task] = "fail"
            for task in summary.get("passed") or []:
                outcome[task] = "pass"
        passed = [task for task, kind in outcome.items() if kind == "pass"]
        failed = [task for task, kind in outcome.items() if kind == "fail"]
        errored = [task for task, kind in outcome.items() if kind == "error"]
        n_completed = len(outcome)
        return {
            "mean": (len(passed) / n_completed) if n_completed else None,
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "n_errored": len(errored),
            "n_completed": n_completed,
        }
    passed: list[str] = []
    failed: list[str] = []
    errored: list[str] = []
    n_errored = 0
    n_completed = 0
    mean_acc = 0.0
    mean_weight = 0
    for summary in parts:
        passed.extend(summary.get("passed") or [])
        failed.extend(summary.get("failed") or [])
        errored.extend(summary.get("errored") or [])
        n_errored += int(summary.get("n_errored") or 0)
        n = int(summary.get("n_completed") or 0)
        n_completed += n
        mean = summary.get("mean")
        if isinstance(mean, float) and n:
            mean_acc += mean * n
            mean_weight += n
    return {
        "mean": (mean_acc / mean_weight) if mean_weight else None,
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "n_errored": n_errored,
        "n_completed": n_completed,
    }


def _status_by_agent(root: Path) -> dict[str, str]:
    by_agent: dict[str, list[str]] = {}
    for status_file in sorted(root.rglob("eval-status-*.txt")):
        agent = status_file.name.removeprefix("eval-status-").removesuffix(".txt")
        status = (
            status_file.read_text().strip().splitlines()[0]
            if status_file.stat().st_size
            else "unknown"
        )
        by_agent.setdefault(agent, []).append(status)
    out: dict[str, str] = {}
    for agent, statuses in by_agent.items():
        if "failed" in statuses:
            out[agent] = "failed"
        elif "ran" in statuses:
            out[agent] = "ran"
        else:
            out[agent] = statuses[0]
    return out


def collect_rows(root: Path) -> list[tuple[str, str, dict[str, Any] | None]]:
    grouped = payloads_by_agent(root)
    summaries = {
        agent: merge_summaries([summarize_result(payload) for payload in payloads])
        for agent, payloads in grouped.items()
    }
    # Job-level json wins; trial logs fill tasks Harbor did not record
    # (GHA 360-minute kill before the shard result.json is written).
    for agent, logs in trial_log_summaries(root).items():
        existing = summaries.get(agent)
        summaries[agent] = (
            merge_summaries([logs, existing]) if existing else logs
        )
    rows: list[tuple[str, str, dict[str, Any] | None]] = []
    for agent, status in sorted(_status_by_agent(root).items()):
        rows.append((agent, status, summaries.get(agent)))
    if rows:
        return rows
    # Oracle artifacts have no eval-status-*.txt. Skip trial result.json
    # (parent is ``fix-git__abc``, not a Harbor job stamp).
    out: list[tuple[str, str, dict[str, Any] | None]] = []
    for agent, summary in sorted(summaries.items()):
        status = (
            "ran"
            if summary["n_errored"] == 0 and summary["mean"] is not None
            else "failed"
        )
        out.append((agent, status, summary))
    return out


def build_message(
    rows: list[tuple[str, str, dict[str, Any] | None]],
    *,
    label: str,
    run_url: str,
) -> tuple[bool, str, str]:
    ok = overall_ok(rows)
    word = "成功" if ok else "失败"
    means = [
        f"{agent} {summary['mean']:.3f}"
        for agent, _status, summary in rows
        if summary and isinstance(summary.get("mean"), float)
    ]
    title = f"{word} · {label}"
    if means:
        title = f"{word} · {label} · " + " · ".join(means)
    lines = [agent_line(agent, status, summary) for agent, status, summary in rows]
    for agent, _status, summary in rows:
        if not summary:
            continue
        if summary["failed"]:
            lines.append(f"{agent} 未过: " + ", ".join(summary["failed"]))
        if summary["passed"]:
            lines.append(f"{agent} 已过: " + ", ".join(summary["passed"]))
    if run_url:
        lines.append(f"[GHA run]({run_url})")
    body = "\n".join(lines) if lines else "没有 Harbor result.json"
    return ok, title, body


def paired_ab_report(root: Path) -> str | None:
    """Paired sign-test report when both flaky arms are present.

    Harbor Mean on this split overwrites attempts; the catalog-go/no-go
    verdict is ``evals.flaky_score``.
    """
    from evals.flaky_score import collect, report

    data = collect(root)
    arms = {arm for byarm in data.values() for arm in byarm}
    if "a" not in arms or "b" not in arms:
        return None
    return report(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post Harbor eval results to Feishu")
    parser.add_argument("--root", type=Path, required=True, help="artifact / jobs directory")
    parser.add_argument("--label", default="Harbor eval")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--webhook-env", default="FEISHU_BOT_WEBHOOK")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"{args.root} is not a directory", file=sys.stderr)
        return EXIT_USAGE
    rows = collect_rows(args.root)
    ok, title, body = build_message(rows, label=args.label, run_url=args.run_url)
    extra = paired_ab_report(args.root)
    if extra:
        body = f"{body}\n\n{extra}"
    payload = card_payload(ok=ok, title=title, body=body)
    print(title)
    print(body)
    if args.dry_run:
        return EXIT_OK
    webhook = (os.environ.get(args.webhook_env) or "").strip()
    if not webhook:
        print(f"{args.webhook_env} unset; skip Feishu post", file=sys.stderr)
        return EXIT_OK
    try:
        post_feishu(webhook, payload)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"Feishu post failed: {exc}", file=sys.stderr)
        return EXIT_POST
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
