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
class TrialMetrics:
    """Efficiency metrics for one trial (W1.4.3.3's cost-normalized axis).

    Every field is optional: arms A–C predate the run-summary line, so
    only ``rounds`` (counted from the headless log's ``[tool`` markers)
    is available for them. Runs whose headless emits a
    ``STEERABLE_RUN_SUMMARY {json}`` final line carry the full set.
    Missing data renders as n/a — never zero-filled, because an absent
    measurement is not a measurement of zero.
    """

    rounds: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    peak_context_tokens: int | None = None
    tool_errors: int | None = None
    tool_recoveries: int | None = None


@dataclass(frozen=True, slots=True)
class JobResult:
    """One job dir projected to its factor labels and per-task scores."""

    label: str
    agent: str
    model: str
    harness: str
    scores: dict[str, float]
    metrics: dict[str, TrialMetrics]


@dataclass(frozen=True, slots=True)
class HarnessEfficiency:
    """Per-harness cost-axis aggregates (W1.4.3.3). Each field is None
    when no trial in the harness carried that measurement."""

    mean_rounds: float | None
    mean_tokens: float | None
    mean_cost_usd: float | None
    mean_peak_context: float | None
    tool_error_rate: float | None
    recovery_rate: float | None


@dataclass(frozen=True, slots=True)
class AttributionReport:
    harness_means: dict[str, float]
    model_means: dict[str, float]
    efficiency: dict[str, HarnessEfficiency]
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
    metrics: dict[str, TrialMetrics] = {}
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
        metrics[result["task_name"]] = _trial_metrics(trial_dir)
    if not scores:
        raise ValueError(f"{path}: no scored trials")
    return JobResult(
        label=path.name,
        agent=agent,
        model=model,
        harness=harness,
        scores=scores,
        metrics=metrics,
    )


#: Final-line contract between headless.py and this report. headless prints
#: ``STEERABLE_RUN_SUMMARY {json}`` at run end; anything earlier in the log
#: is model output and must not be parsed as telemetry.
RUN_SUMMARY_PREFIX = "STEERABLE_RUN_SUMMARY "


def _trial_metrics(trial_dir: Path) -> TrialMetrics:
    """Extract efficiency metrics from a trial's agent log.

    Rounds are approximated by counting ``[tool`` markers in headless.log
    (one per tool call; the loop makes at most one round's worth of calls
    per step). The summary line, when present, overrides with exact values.
    """
    log_path = trial_dir / "agent" / "headless.log"
    if not log_path.exists():
        return TrialMetrics()
    rounds = 0
    summary: dict[str, object] | None = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("[tool "):
            rounds += 1
        elif line.startswith(RUN_SUMMARY_PREFIX):
            try:
                summary = json.loads(line[len(RUN_SUMMARY_PREFIX):])
            except json.JSONDecodeError:
                continue  # a truncated final line loses telemetry, not the score
    if summary is None:
        return TrialMetrics(rounds=rounds or None)

    def integer(key: str) -> int | None:
        value = summary.get(key)
        return value if isinstance(value, int) else None

    cost = summary.get("cost_usd")
    return TrialMetrics(
        rounds=integer("rounds") or (rounds or None),
        input_tokens=integer("input_tokens"),
        output_tokens=integer("output_tokens"),
        cost_usd=cost if isinstance(cost, (int, float)) else None,
        peak_context_tokens=integer("peak_context_tokens"),
        tool_errors=integer("tool_errors"),
        tool_recoveries=integer("tool_recoveries"),
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
    efficiency = {
        harness: _efficiency([j for j in jobs if j.harness == harness], shared)
        for harness in dict.fromkeys(j.harness for j in jobs)
    }
    return AttributionReport(
        harness_means=harness_means,
        model_means=model_means,
        efficiency=efficiency,
        harness_effect=harness_effect,
        model_effect=model_effect,
        effect_ratio=ratio,
        rank_reversals=reversals,
        n_tasks=len(shared),
        n_jobs=len(jobs),
    )


def _efficiency(jobs: list[JobResult], shared: set[str]) -> HarnessEfficiency:
    """Aggregate the cost axis over one harness's jobs on the shared tasks."""
    trials = [j.metrics[t] for j in jobs for t in shared if t in j.metrics]

    def mean_of(field: str) -> float | None:
        values = [getattr(m, field) for m in trials]
        present = [v for v in values if v is not None]
        if not present or len(present) < len(values):
            # Partial data would silently bias the mean toward tasks that
            # happened to report — report nothing instead.
            return None
        return sum(present) / len(present)

    mean_rounds = mean_of("rounds")
    input_tokens = [m.input_tokens for m in trials]
    output_tokens = [m.output_tokens for m in trials]
    mean_tokens = (
        sum(i + o for i, o in zip(input_tokens, output_tokens)) / len(trials)
        if trials and all(v is not None for v in (*input_tokens, *output_tokens))
        else None
    )
    errors = [m.tool_errors for m in trials]
    recoveries = [m.tool_recoveries for m in trials]
    rounds = [m.rounds for m in trials]
    tool_error_rate = (
        sum(errors) / sum(rounds)  # type: ignore[operator]
        if trials and all(v is not None for v in (*errors, *rounds)) and sum(rounds)  # type: ignore[arg-type]
        else None
    )
    recovery_rate = (
        sum(recoveries) / sum(errors)  # type: ignore[operator]
        if trials and all(v is not None for v in (*recoveries, *errors)) and sum(errors)  # type: ignore[arg-type]
        else None
    )
    return HarnessEfficiency(
        mean_rounds=mean_rounds,
        mean_tokens=mean_tokens,
        mean_cost_usd=mean_of("cost_usd"),
        mean_peak_context=mean_of("peak_context_tokens"),
        tool_error_rate=tool_error_rate,
        recovery_rate=recovery_rate,
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
        "## Efficiency (cost-normalized axis, W1.4.3.3)",
        "",
        "| harness | mean rounds | mean tokens | mean cost | peak ctx | tool err | recovery |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    def num(value: float | None, digits: int = 1) -> str:
        return "n/a" if value is None else f"{value:.{digits}f}"

    for harness, eff in report.efficiency.items():
        lines.append(
            f"| `{harness}` | {num(eff.mean_rounds)} | {num(eff.mean_tokens, 0)} "
            f"| {('n/a' if eff.mean_cost_usd is None else f'${eff.mean_cost_usd:.4f}')} "
            f"| {num(eff.mean_peak_context, 0)} | {pct(eff.tool_error_rate)} "
            f"| {pct(eff.recovery_rate)} |"
        )
    lines.append("")
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
