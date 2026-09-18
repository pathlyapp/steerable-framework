"""Run the compaction simulator suite and print a markdown report.

Usage:
    python -m evals.compaction.run_sim                     # all arms x all tasks
    python -m evals.compaction.run_sim --arms none,current --tasks light
    python -m evals.compaction.run_sim --json /tmp/report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from evals.compaction.simulator import ARMS, TASKS, ArmReport, run_arm


def render_table(reports: list[ArmReport]) -> str:
    header = (
        "| task | arm | success | recall | compactions | overflows | "
        "prompt tokens | cache hit |\n"
        "|---|---|---|---|---|---|---|---|"
    )
    rows = [
        f"| {r.task} | {r.arm} | {'✅' if r.success else '❌'} | "
        f"{r.needle_recall:.2f} ({r.needles_found}/{r.needles_expected}) | "
        f"{r.compactions} | {r.overflows} | {r.prompt_tokens_total} | "
        f"{r.cache_hit_ratio:.3f} |"
        for r in reports
    ]
    return "\n".join([header, *rows])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--tasks", default=",".join(TASKS))
    parser.add_argument("--json", default=None, help="also write the report here")
    args = parser.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    task_names = [t.strip() for t in args.tasks.split(",") if t.strip()]
    reports: list[ArmReport] = []
    for task_name in task_names:
        for arm in arms:
            reports.append(asyncio.run(run_arm(TASKS[task_name], arm)))

    print(render_table(reports))
    if args.json:
        Path(args.json).write_text(
            json.dumps([r.to_dict() for r in reports], indent=2) + "\n"
        )


if __name__ == "__main__":
    main()
