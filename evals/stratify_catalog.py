"""Stratify catalog tasks by cross-run stability and classify failures.

One catalog run is one attempt per task — too coarse to tell a harness
regression from a coin flip (17 of 89 tasks flip between runs at the same
commit; see ``evals.flaky_score``). This module aggregates the same task
across N catalog runs of one commit and sorts it into three tiers:

- ``stable-green``: passed every run — the regression baseline.
- ``stable-red``: failed every run — candidate for the ``spiral-red`` /
  ``loss-24`` splits and for loss-taxonomy analysis.
- ``flaky``: mixed — the A/B pairing pool (``flaky`` split).

Failed trials are further classified by context pressure: the trial's
``peak_context_tokens`` (the ``STEERABLE_RUN_SUMMARY`` line in the trial's
``agent/headless.log``) against the model's context window. A failure at
≥ ``--pressure-frac`` of the window is a context-length failure; the rest
are capability failures. The split tells the roadmap whether to invest in
compaction or in tools.

Stdlib only, same as ``evals.feishu`` / ``evals.flaky_score``, so it runs
against downloaded artifacts without ``uv sync``:

    gh run download <run-id> -p 'eval-steerable-*' -D /tmp/tb-6run/<run-id>
    python -m evals.stratify_catalog --root /tmp/tb-6run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

EXIT_OK = 0
EXIT_USAGE = 1

#: The ``STEERABLE_RUN_SUMMARY`` terminal line in a trial's headless log.
_SUMMARY = re.compile(r"^STEERABLE_RUN_SUMMARY (\{.*\})\s*$", re.MULTILINE)

#: Default context window for the suite's default model
#: (``z-ai/glm-5.3-flash`` → 1_048_576 in the framework's model_info
#: prefix table). Override with ``--context-window`` for other models.
DEFAULT_CONTEXT_WINDOW = 1_048_576

#: Default pressure threshold: a failed trial whose peak prompt reached
#: ≥90% of the window counts as a context-length failure.
DEFAULT_PRESSURE_FRAC = 0.9


class Trial(NamedTuple):
    """One attempt of one task in one run."""

    passed: bool
    #: Peak prompt tokens the trial saw, or None when the log is absent.
    peak_context: int | None


def _passed(result: Path) -> bool | None:
    """``None`` when the trial produced no verifier reward at all."""
    try:
        payload = json.loads(result.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    rewards = ((payload.get("verifier_result") or {}).get("rewards")) or {}
    reward = rewards.get("reward")
    return None if reward is None else float(reward) > 0


def _peak_context(result: Path) -> int | None:
    """Peak prompt tokens from the trial's run summary, if logged."""
    log = result.parent / "agent" / "headless.log"
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return None
    matches = _SUMMARY.findall(text)
    if not matches:
        return None
    try:
        payload = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    peak = payload.get("peak_context_tokens")
    return peak if isinstance(peak, int) and peak > 0 else None


def _run_of(path: Path, root: Path) -> str:
    """Run label: the first path component under the download root.

    The download convention is one directory per GHA run id, so the label
    is the run id; a flat single-run root labels every trial ``.``.
    """
    rel = path.relative_to(root)
    return rel.parts[0] if len(rel.parts) > 1 else "."


def collect(root: Path) -> dict[str, dict[str, list[Trial]]]:
    """``task id → run → one entry per attempt`` (verdict-bearing only)."""
    out: dict[str, dict[str, list[Trial]]] = defaultdict(lambda: defaultdict(list))
    for result in sorted(root.rglob("jobs/steerable/*/*/result.json")):
        verdict = _passed(result)
        if verdict is None:
            continue
        task = result.parent.name.rsplit("__", 1)[0]
        out[task][_run_of(result, root)].append(
            Trial(verdict, _peak_context(result))
        )
    return out


def count_incomplete(root: Path) -> int:
    """Trials with no verifier reward (GHA-killed hangs, compose deaths)."""
    return sum(
        1
        for result in root.rglob("jobs/steerable/*/*/result.json")
        if _passed(result) is None
    )


class TaskTally(NamedTuple):
    task: str
    passes: int
    trials: int
    #: Failed trials at ≥ the pressure threshold of the context window.
    context_failures: int
    #: Failed trials under the threshold (or with no peak reading).
    other_failures: int


def tally(
    data: dict[str, dict[str, list[Trial]]],
    *,
    context_window: int,
    pressure_frac: float,
) -> list[TaskTally]:
    out: list[TaskTally] = []
    ceiling = context_window * pressure_frac
    for task, by_run in data.items():
        trials = [t for run in by_run.values() for t in run]
        passes = sum(t.passed for t in trials)
        context_failures = sum(
            1
            for t in trials
            if not t.passed
            and t.peak_context is not None
            and t.peak_context >= ceiling
        )
        other_failures = sum(1 for t in trials if not t.passed) - context_failures
        out.append(
            TaskTally(
                task=task,
                passes=passes,
                trials=len(trials),
                context_failures=context_failures,
                other_failures=other_failures,
            )
        )
    return sorted(out, key=lambda t: t.task)


def report(
    tallies: list[TaskTally],
    *,
    runs: int,
    context_window: int,
    pressure_frac: float,
    incomplete: int,
) -> str:
    green = [t for t in tallies if t.passes == t.trials]
    red = [t for t in tallies if t.passes == 0]
    flaky = [t for t in tallies if 0 < t.passes < t.trials]

    lines = [
        f"{runs} runs, {len(tallies)} tasks, "
        f"{incomplete} incomplete trials (no reward, excluded)",
        f"context window {context_window}, pressure threshold "
        f"{pressure_frac:.0%} (peak ≥ {int(context_window * pressure_frac)} tokens)",
        "",
        f"stable-green ({len(green)}): passed every run",
        f"stable-red   ({len(red)}): failed every run",
        f"flaky        ({len(flaky)}): mixed",
        "",
    ]

    def row(t: TaskTally) -> str:
        fail = ""
        if t.context_failures or t.other_failures:
            parts = []
            if t.context_failures:
                parts.append(f"{t.context_failures} context")
            if t.other_failures:
                parts.append(f"{t.other_failures} other")
            fail = f"  failures: {' + '.join(parts)}"
        return f"  {t.passes}/{t.trials}  {t.task}{fail}"

    if flaky:
        lines.append("== flaky ==")
        lines.extend(row(t) for t in flaky)
        lines.append("")
    if red:
        lines.append("== stable-red ==")
        lines.extend(row(t) for t in red)
        lines.append("")
    context_total = sum(t.context_failures for t in tallies)
    other_total = sum(t.other_failures for t in tallies)
    lines.append(
        f"failed trials: {context_total + other_total} "
        f"({context_total} context-pressure, {other_total} other)"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--context-window", type=int, default=DEFAULT_CONTEXT_WINDOW)
    parser.add_argument("--pressure-frac", type=float, default=DEFAULT_PRESSURE_FRAC)
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="also write the tallies as JSON for docs/suite.yaml reuse",
    )
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"not a directory: {args.root}", file=sys.stderr)
        return EXIT_USAGE
    if not 0 < args.pressure_frac <= 1:
        print("--pressure-frac must be in (0, 1]", file=sys.stderr)
        return EXIT_USAGE

    data = collect(args.root)
    if not data:
        print(f"no trial results under {args.root}", file=sys.stderr)
        return EXIT_USAGE
    tallies = tally(
        data,
        context_window=args.context_window,
        pressure_frac=args.pressure_frac,
    )
    runs = len({run for by_run in data.values() for run in by_run})
    incomplete = count_incomplete(args.root)
    print(
        report(
            tallies,
            runs=runs,
            context_window=args.context_window,
            pressure_frac=args.pressure_frac,
            incomplete=incomplete,
        )
    )
    if args.json:
        payload = {
            "runs": runs,
            "context_window": args.context_window,
            "pressure_frac": args.pressure_frac,
            "incomplete_trials": incomplete,
            "tasks": [t._asdict() for t in tallies],
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
