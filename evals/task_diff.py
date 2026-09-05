"""Per-task set diff across two Harbor job directories.

``evals.attribution`` reports harness × model aggregates. It cannot answer
"which tasks did Claude Code pass that we failed". This module loads two job
dirs (via ``load_job``), normalises ``terminal-bench/foo`` vs ``foo``, and
prints the two exclusive pass sets.

CLI:
    python -m evals.task_diff \\
        --they evals/jobs/claude-code/2026-09-05__12-00-00 \\
        --we evals/jobs/steerable/2026-09-05__12-00-00
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from evals.attribution import load_job


def task_id(name: str) -> str:
    """Harbor ``task_name`` is ``terminal-bench/<id>``; some dumps store the id."""
    return name.rsplit("/", 1)[-1]


def _passed(scores: dict[str, float]) -> set[str]:
    return {task_id(name) for name, reward in scores.items() if reward > 0}


def _ids(scores: dict[str, float]) -> set[str]:
    return {task_id(name) for name in scores}


@dataclass(frozen=True, slots=True)
class TaskDiff:
    they_label: str
    we_label: str
    they_passed_we_failed: tuple[str, ...]
    we_passed_they_failed: tuple[str, ...]
    both_passed: tuple[str, ...]
    both_failed: tuple[str, ...]
    they_only: tuple[str, ...]
    we_only: tuple[str, ...]


def diff_jobs(they_path: Path, we_path: Path) -> TaskDiff:
    """Compare two Harbor job dirs. A task missing from one side is a failure there."""
    they = load_job(they_path, harness="they")
    we = load_job(we_path, harness="we")
    they_ids = _ids(they.scores)
    we_ids = _ids(we.scores)
    they_pass = _passed(they.scores)
    we_pass = _passed(we.scores)
    shared = they_ids & we_ids
    return TaskDiff(
        they_label=str(they_path),
        we_label=str(we_path),
        they_passed_we_failed=tuple(sorted(they_pass - we_pass)),
        we_passed_they_failed=tuple(sorted(we_pass - they_pass)),
        both_passed=tuple(sorted(they_pass & we_pass)),
        both_failed=tuple(sorted(shared - they_pass - we_pass)),
        they_only=tuple(sorted(they_ids - we_ids)),
        we_only=tuple(sorted(we_ids - they_ids)),
    )


def render_markdown(report: TaskDiff) -> str:
    def listing(title: str, ids: tuple[str, ...]) -> str:
        if not ids:
            return f"## {title}\n\n(none)\n"
        body = "\n".join(f"- `{task}`" for task in ids)
        return f"## {title}\n\n{body}\n"

    lines = [
        "# Task diff",
        "",
        f"- they: `{report.they_label}`",
        f"- we: `{report.we_label}`",
        f"- they passed, we failed: {len(report.they_passed_we_failed)}",
        f"- we passed, they failed: {len(report.we_passed_they_failed)}",
        f"- both passed: {len(report.both_passed)}",
        f"- both failed: {len(report.both_failed)}",
        "",
        listing("They passed, we failed", report.they_passed_we_failed),
        listing("We passed, they failed", report.we_passed_they_failed),
        listing("Both passed", report.both_passed),
        listing("Both failed (shared tasks)", report.both_failed),
        listing("Only in they", report.they_only),
        listing("Only in we", report.we_only),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--they", required=True, help="Harbor job dir (the other agent)")
    parser.add_argument("--we", required=True, help="Harbor job dir (steerable)")
    parser.add_argument("--out", help="write markdown here (default: stdout)")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the four exclusive sets as JSON instead of markdown",
    )
    args = parser.parse_args(argv)
    try:
        report = diff_jobs(Path(args.they), Path(args.we))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        text = json.dumps(
            {
                "they_passed_we_failed": list(report.they_passed_we_failed),
                "we_passed_they_failed": list(report.we_passed_they_failed),
                "both_passed": list(report.both_passed),
                "both_failed": list(report.both_failed),
                "they_only": list(report.they_only),
                "we_only": list(report.we_only),
            },
            indent=2,
        )
    else:
        text = render_markdown(report)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
