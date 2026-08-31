"""Variance attribution across harness and model factors (W1.3.3).

Reads Harbor job directories and reports the disclosure metrics the
harness-as-independent-variable literature requires (arXiv 2605.23950):
the harness main effect, the model main effect, their ratio, and the
number of model-rank reversals across harnesses. Without these numbers a
claim like "our harness scores higher" is unfalsifiable — the same model
under two harnesses is a different measured system.

A job dir is tagged (agent, model, harness): agent/model come from the
job's ``config.json``; the harness label comes from the CLI (until
W1.3.1 lands the harness dimension in suite.yaml and it can be read from
the job config). Scores come from each trial's ``result.json``
(``verifier_result.rewards.reward``).

CLI:
    python -m evals.attribution \
        --job default=evals/jobs/steerable-arm-a/2026-08-31__11-14-23 \
        --job minimal=evals/jobs/steerable-arm-b/...
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class JobResult:
    """One job dir projected to its factor labels and per-task scores."""

    label: str
    agent: str
    model: str
    harness: str
    scores: dict[str, float]


@dataclass(frozen=True, slots=True)
class AttributionReport:
    harness_means: dict[str, float]
    model_means: dict[str, float]
    #: Range (max − min) of per-harness mean scores within each model,
    #: averaged over models that ran ≥2 harnesses.
    harness_effect: float | None
    #: Symmetric range across models within each harness.
    model_effect: float | None
    #: harness_effect / model_effect; None when either side is undefined
    #: or the model effect is zero.
    effect_ratio: float | None
    #: Model pairs whose score order flips between any two harnesses.
    rank_reversals: int
    n_tasks: int
    n_jobs: int


def load_job(path: Path, *, harness: str) -> JobResult:
    """Project a Harbor job dir; missing pieces fail loud — a silent
    default would fabricate a factor level that was never run."""
    config_path = path / "config.json"
    if not config_path.exists():
        raise ValueError(f"{path}: no config.json — not a Harbor job dir")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    agents = config.get("agents") or []
    if len(agents) != 1:
        raise ValueError(f"{path}: expected exactly one agent, got {len(agents)}")
    agent = agents[0].get("name") or ""
    model = agents[0].get("model_name") or ""
    if not agent or not model:
        raise ValueError(f"{path}: config.json missing agent name/model_name")

    scores: dict[str, float] = {}
    for trial_dir in sorted(p for p in path.iterdir() if p.is_dir()):
        result_path = trial_dir / "result.json"
        if not result_path.exists():
            continue  # still running or died before writing a result
        result = json.loads(result_path.read_text(encoding="utf-8"))
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        reward = rewards.get("reward")
        if reward is None:
            continue  # verifier never scored (agent error) — not a zero
        scores[result["task_name"]] = float(reward)
    if not scores:
        raise ValueError(f"{path}: no scored trials")
    return JobResult(
        label=path.name, agent=agent, model=model, harness=harness, scores=scores
    )


def attribute(jobs: list[JobResult]) -> AttributionReport:
    """Descriptive two-factor decomposition over the shared task set.

    Only tasks scored in *every* job enter the means — pairwise-complete
    data would let a harness dodge its worst tasks.
    """
    if not jobs:
        raise ValueError("no jobs to attribute")
    shared = set(jobs[0].scores)
    for job in jobs[1:]:
        shared &= set(job.scores)
    if not shared:
        raise ValueError("jobs share no scored tasks")

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    harness_means = {
        harness: mean(
            [mean([job.scores[t] for t in shared]) for job in jobs if job.harness == harness]
        )
        for harness in dict.fromkeys(job.harness for job in jobs)
    }
    model_means = {
        model: mean(
            [mean([job.scores[t] for t in shared]) for job in jobs if job.model == model]
        )
        for model in dict.fromkeys(job.model for job in jobs)
    }

    harness_effect = _factor_effect(jobs, key=lambda j: j.harness, fixed=lambda j: j.model, shared=shared)
    model_effect = _factor_effect(jobs, key=lambda j: j.model, fixed=lambda j: j.harness, shared=shared)
    ratio = (
        harness_effect / model_effect
        if harness_effect is not None and model_effect
        else None
    )
    reversals = _rank_reversals(jobs, shared)
    return AttributionReport(
        harness_means=harness_means,
        model_means=model_means,
        harness_effect=harness_effect,
        model_effect=model_effect,
        effect_ratio=ratio,
        rank_reversals=reversals,
        n_tasks=len(shared),
        n_jobs=len(jobs),
    )


def _factor_effect(
    jobs: list[JobResult],
    *,
    key,
    fixed,
    shared: set[str],
) -> float | None:
    """Mean over fixed-level groups of the key-level mean range. None when
    no fixed level has ≥2 key levels to compare."""
    ranges: list[float] = []
    fixed_levels = dict.fromkeys(fixed(j) for j in jobs)
    for level in fixed_levels:
        group = [j for j in jobs if fixed(j) == level]
        means = {}
        for job in group:
            means.setdefault(key(job), []).extend(job.scores[t] for t in shared)
        if len(means) < 2:
            continue
        per_key = [sum(v) / len(v) for v in means.values()]
        ranges.append(max(per_key) - min(per_key))
    if not ranges:
        return None
    return sum(ranges) / len(ranges)


def _rank_reversals(jobs: list[JobResult], shared: set[str]) -> int:
    """Count model pairs whose mean-score order differs between harnesses."""
    harnesses = dict.fromkeys(j.harness for j in jobs)
    models = dict.fromkeys(j.model for j in jobs)
    reversals = 0
    for i, left in enumerate(models):
        for right in list(models)[i + 1 :]:
            orders: list[int] = []
            for harness in harnesses:
                pair = [j for j in jobs if j.harness == harness and j.model in (left, right)]
                by_model = {
                    m: [j.scores[t] for j in pair if j.model == m for t in shared]
                    for m in (left, right)
                }
                if not all(by_model.values()):
                    continue
                means = {m: sum(v) / len(v) for m, v in by_model.items()}
                orders.append((means[left] > means[right]) - (means[left] < means[right]))
            if len(orders) >= 2 and any(o != orders[0] for o in orders[1:]):
                reversals += 1
    return reversals


def render_markdown(report: AttributionReport) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    lines = [
        "# Harness × Model Attribution",
        "",
        f"Jobs: {report.n_jobs} · shared scored tasks: {report.n_tasks}",
        "",
        "## Means",
        "",
        "| factor level | mean reward |",
        "| --- | --- |",
    ]
    for harness, value in report.harness_means.items():
        lines.append(f"| harness `{harness}` | {value:.3f} |")
    for model, value in report.model_means.items():
        lines.append(f"| model `{model}` | {value:.3f} |")
    lines += [
        "",
        "## Main effects",
        "",
        f"- harness main effect (mean range within model): {pct(report.harness_effect)}",
        f"- model main effect (mean range within harness): {pct(report.model_effect)}",
        f"- harness/model effect ratio: {report.effect_ratio:.2f}" if report.effect_ratio is not None else "- harness/model effect ratio: n/a",
        f"- model rank reversals across harnesses: {report.rank_reversals}",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--job",
        action="append",
        required=True,
        metavar="HARNESS=PATH",
        help="a job dir tagged with its harness label (repeatable)",
    )
    parser.add_argument("--out", help="write the markdown report here (default: stdout)")
    args = parser.parse_args(argv)

    jobs: list[JobResult] = []
    for spec in args.job:
        harness, sep, raw_path = spec.partition("=")
        if not sep or not harness or not raw_path:
            print(f"--job must be HARNESS=PATH, got {spec!r}", file=sys.stderr)
            return 1
        try:
            jobs.append(load_job(Path(raw_path), harness=harness))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    try:
        report = attribute(jobs)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    text = render_markdown(report)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
