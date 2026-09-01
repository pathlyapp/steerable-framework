"""Score a flaky A/B dispatch by per-task pass rate and decide if an arm won.

``evals.feishu`` reports the Harbor mean of a single attempt per task, which
is the right number for a score of record and the wrong one for judging a
harness change: 17 of the 89 catalog tasks flip between runs at the same
commit, so a one-attempt mean carries a ±0.19 band and a change worth +0.05
is invisible inside it. This module keeps every attempt instead of letting the
last one overwrite the rest, and reports the paired comparison the ``flaky``
split was built to support.

Stdlib only, same as ``evals.feishu``, so it runs against downloaded
artifacts without ``uv sync``.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

#: ``eval-steerable-flaky-a-7`` → arm ``a``. Artifacts from the catalog,
#: spiral-red, and failed-prev jobs carry no arm, and score as the single
#: arm ``-``.
_ARM = re.compile(r"^eval-steerable-flaky-([ab])-\d+$")
#: Tool-call lines in ``headless.log``, counted as trajectory length.
_TOOL = re.compile(r"\[tool \w+ ")

EXIT_OK = 0
EXIT_USAGE = 1


class Trial(NamedTuple):
    """One attempt: whether it scored, and how many tool calls it took.

    Trajectory length is carried alongside the verdict because pass rate at
    three attempts is a coarse readout — it moves only when an outcome flips
    — while the spread of tool calls within a task responds to a change in
    sampling without waiting for a flip. When the two disagree, spread is
    the earlier signal and the verdict is the one that counts.
    """

    passed: bool
    calls: int | None


def _arm_of(path: Path, root: Path) -> str:
    """Arm label for the artifact directory containing ``path``."""
    for part in path.relative_to(root).parts:
        found = _ARM.match(part)
        if found:
            return found.group(1)
    return "-"


def _passed(result: Path) -> bool | None:
    """``None`` when the trial produced no verifier reward at all."""
    try:
        payload = json.loads(result.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    rewards = ((payload.get("verifier_result") or {}).get("rewards")) or {}
    reward = rewards.get("reward")
    return None if reward is None else float(reward) > 0


def _calls(result: Path) -> int | None:
    """Tool calls in the trial's agent log, or ``None`` when it is absent."""
    log = result.parent / "agent" / "headless.log"
    try:
        return len(_TOOL.findall(log.read_text(errors="replace")))
    except OSError:
        return None


def collect(root: Path) -> dict[str, dict[str, list[Trial]]]:
    """``task id → arm → one entry per attempt``.

    Keyed by trial rather than task so three attempts of one task stay three
    data points; Harbor names them ``task__hash`` with a fresh hash each time.
    """
    out: dict[str, dict[str, list[Trial]]] = defaultdict(lambda: defaultdict(list))
    for result in sorted(root.rglob("jobs/steerable/*/*/result.json")):
        verdict = _passed(result)
        if verdict is None:
            continue
        task = result.parent.name.rsplit("__", 1)[0]
        out[task][_arm_of(result, root)].append(Trial(verdict, _calls(result)))
    return out


def spread(trials: list[Trial]) -> float | None:
    """Standard deviation of trajectory length across attempts of one task.

    Needs two attempts with logs to mean anything. Reported per arm and
    averaged over tasks: if a change narrows how differently the same task
    gets attempted, this falls even while pass rates sit still, and if the
    provider ignored the knob it stays put.
    """
    lengths = [t.calls for t in trials if t.calls is not None]
    if len(lengths) < 2:
        return None
    return statistics.stdev(lengths)


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact binomial p for ``wins`` of ``wins + losses`` under p=0.5.

    The comparison is paired by task, so only tasks where the arms disagree
    carry information; ties tell us nothing about direction. That is a sign
    test, and at these counts the exact sum beats any normal approximation.
    """
    n = wins + losses
    if n == 0:
        return 1.0
    extreme = min(wins, losses)
    tail = sum(math.comb(n, k) for k in range(extreme + 1)) / 2**n
    return min(1.0, 2 * tail)


def bootstrap_delta(
    deltas: list[float], *, rounds: int = 20_000, seed: int = 0
) -> tuple[float, float, float]:
    """Mean per-task pass-rate change and its 95% percentile interval.

    The sign test only counts directions, so it calls a change of +0.5 on
    three tasks inconclusive. Resampling tasks keeps the magnitudes and pairs
    each task with itself, which is where the shared task set pays off. An
    interval that excludes zero is the signal to spend a catalog run.
    """
    if not deltas:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(
        sum(rng.choice(deltas) for _ in range(n)) / n for _ in range(rounds)
    )
    return (
        sum(deltas) / n,
        means[int(rounds * 0.025)],
        means[int(rounds * 0.975)],
    )


def report(data: dict[str, dict[str, list[Trial]]]) -> str:
    """Per-task pass rates, arm means, trajectory spread, and the verdict."""
    arms = sorted({arm for byarm in data.values() for arm in byarm})
    lines = [f"{'task':<38}" + "".join(f"{'arm ' + a:>10}" for a in arms)]
    for task in sorted(data):
        row = f"{task:<38}"
        for arm in arms:
            trials = data[task].get(arm) or []
            passes = sum(t.passed for t in trials)
            row += f"{'-' if not trials else f'{passes}/{len(trials)}':>10}"
        lines.append(row)
    lines.append("")
    for arm in arms:
        trials = [t for byarm in data.values() for t in byarm.get(arm, [])]
        passes = sum(t.passed for t in trials)
        rate = passes / len(trials) if trials else 0.0
        spreads = [
            s
            for byarm in data.values()
            if (s := spread(byarm.get(arm) or [])) is not None
        ]
        tail = (
            f"  trajectory spread {statistics.mean(spreads):.1f} tool calls "
            f"over {len(spreads)} tasks"
            if spreads
            else ""
        )
        lines.append(
            f"arm {arm}: {passes}/{len(trials)} attempts passed  "
            f"pass rate {rate:.4f}{tail}"
        )
    if len(arms) == 2:
        a, b = arms
        wins = losses = ties = 0
        deltas: list[float] = []
        for byarm in data.values():
            ta, tb = byarm.get(a) or [], byarm.get(b) or []
            if not ta or not tb:
                continue
            ra = sum(t.passed for t in ta) / len(ta)
            rb = sum(t.passed for t in tb) / len(tb)
            deltas.append(rb - ra)
            if rb > ra:
                wins += 1
            elif ra > rb:
                losses += 1
            else:
                ties += 1
        p = sign_test(wins, losses)
        mean, lo, hi = bootstrap_delta(deltas)
        lines.append("")
        lines.append(
            f"paired on {len(deltas)} tasks: arm {b} better on {wins}, arm "
            f"{a} better on {losses}, tied on {ties}"
        )
        lines.append(f"sign test p = {p:.4f}")
        lines.append(
            f"mean per-task change {mean:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]"
        )
        # Either test alone misreads one shape of result: the sign test calls
        # a large change on few tasks inconclusive, and the interval can
        # exclude zero on the strength of a single task. Requiring both keeps
        # a catalog run from being spent on noise.
        agree = p < 0.05 and (lo > 0) == (hi > 0)
        verdict = (
            f"arm {b} wins" if agree and wins > losses
            else f"arm {a} wins" if agree
            else "no separation at this sample size"
        )
        lines.append(f"verdict: {verdict}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"not a directory: {args.root}", file=sys.stderr)
        return EXIT_USAGE
    data = collect(args.root)
    if not data:
        print(f"no trial results under {args.root}", file=sys.stderr)
        return EXIT_USAGE
    print(report(data))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
