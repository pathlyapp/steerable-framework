"""Run the pinned Terminal-Bench suite through Harbor."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from evals.suite import (
    LIVE_AGENTS,
    SuiteError,
    agent_ready,
    harbor_argv,
    load_suite,
    missing_env,
    resolve_tasks,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_HARBOR = 2
EXIT_SKIPPED = 3

REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        suite = load_suite()
        spec = suite.agents.get(args.agent)
        if spec is None:
            known = ", ".join(sorted(suite.agents))
            raise SuiteError(f"unknown agent {args.agent!r}; expected one of {known}")
        if spec.skipped:
            raise SuiteError(spec.reason or f"agent {args.agent!r} is skipped")
        tasks = resolve_tasks(suite, args.split, args.tasks)
        jobs_dir = (
            Path(args.jobs_dir)
            if args.jobs_dir
            else REPO_ROOT / suite.jobs_dir / args.agent
        )
        argv_harbor = harbor_argv(
            suite,
            agent=args.agent,
            tasks=tasks,
            jobs_dir=jobs_dir,
            model=args.model,
            n_concurrent=args.n_concurrent,
            n_attempts=args.n_attempts,
            agent_setup_timeout_multiplier=args.agent_setup_timeout_multiplier,
            environment_build_timeout_multiplier=args.environment_build_timeout_multiplier,
            agent_timeout_multiplier=args.agent_timeout_multiplier,
            harbor_bin=args.harbor,
        )
    except SuiteError as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE

    if args.dry_run:
        print(shlex.join(argv_harbor))
        return EXIT_OK

    if not agent_ready(spec, os.environ):
        needed = " or ".join(missing_env(spec, os.environ))
        message = f"agent {args.agent!r} skipped: set {needed}"
        print(message, file=sys.stderr)
        return EXIT_SKIPPED if args.skip_missing_env else EXIT_USAGE

    print(shlex.join(argv_harbor))
    if shutil.which(args.harbor) is None:
        print(
            f"{args.harbor!r} not found on PATH. Install with: uv tool install harbor",
            file=sys.stderr,
        )
        return EXIT_USAGE

    jobs_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), *(p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p)]
    )
    completed = subprocess.run(
        argv_harbor, cwd=REPO_ROOT, env=_harbor_child_env(env)
    )
    if completed.returncode != 0:
        print(f"harbor exited {completed.returncode}", file=sys.stderr)
        return EXIT_HARBOR
    return _print_summary(jobs_dir, require_mean=args.require_mean)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run pinned Terminal-Bench 2.1 tasks through Harbor. "
            f"Live agents: {', '.join(LIVE_AGENTS)}. DSH is skipped."
        )
    )
    parser.add_argument(
        "--agent",
        required=True,
        help="oracle, steerable, claude-code, codex, or pi",
    )
    parser.add_argument("--split", default="cheap-12", help="split name from suite.yaml")
    parser.add_argument(
        "--tasks",
        nargs="+",
        help="override split with catalog task ids",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the Harbor command and exit")
    parser.add_argument(
        "--skip-missing-env",
        action="store_true",
        help="exit 3 when the agent is live but its API key is unset",
    )
    parser.add_argument("--jobs-dir", help="Harbor --jobs-dir (default evals/jobs/<agent>)")
    parser.add_argument("--model", help="override suite.yaml model (provider/model)")
    parser.add_argument("--n-concurrent", type=int)
    parser.add_argument("--n-attempts", type=int)
    parser.add_argument(
        "--agent-setup-timeout-multiplier",
        type=float,
        help="Harbor --agent-setup-timeout-multiplier",
    )
    parser.add_argument(
        "--environment-build-timeout-multiplier",
        type=float,
        help="Harbor --environment-build-timeout-multiplier",
    )
    parser.add_argument(
        "--agent-timeout-multiplier",
        type=float,
        help="Harbor --agent-timeout-multiplier (agent.run wall clock)",
    )
    parser.add_argument(
        "--require-mean",
        type=float,
        help="fail if the latest job Mean is below this value (oracle canary)",
    )
    parser.add_argument("--harbor", default="harbor", help="Harbor executable")
    return parser.parse_args(argv)


_DOCKER_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _harbor_child_env(env: dict[str, str]) -> dict[str, str]:
    """Keep Clash off Docker Hub pulls; trial containers still get STEERABLE_HOST_PROXY."""
    out = dict(env)
    host_proxy = (
        out.get("STEERABLE_HOST_PROXY")
        or out.get("HTTPS_PROXY")
        or out.get("HTTP_PROXY")
        or out.get("https_proxy")
        or out.get("http_proxy")
    )
    if host_proxy:
        out["STEERABLE_HOST_PROXY"] = host_proxy
    for key in _DOCKER_PROXY_KEYS:
        out.pop(key, None)
    return out


def _print_summary(jobs_dir: Path, *, require_mean: float | None = None) -> int:
    results = sorted(jobs_dir.glob("*/result.json"))
    if not results:
        print(f"harbor finished; no job result.json under {jobs_dir}", file=sys.stderr)
        return EXIT_HARBOR
    latest = results[-1]
    try:
        payload = json.loads(latest.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read {latest}: {exc}", file=sys.stderr)
        return EXIT_HARBOR
    stats = payload.get("stats") if isinstance(payload, dict) else None
    print(f"harbor result: {latest}")
    if not isinstance(stats, dict):
        print(f"harbor result missing stats: {latest}", file=sys.stderr)
        return EXIT_HARBOR
    print(json.dumps(stats, indent=2, sort_keys=True))
    errored = stats.get("n_errored_trials") or 0
    if errored:
        print(f"harbor reported {errored} errored trial(s)", file=sys.stderr)
        return EXIT_HARBOR
    mean = _job_mean(stats)
    if require_mean is not None:
        if mean is None or mean + 1e-9 < require_mean:
            print(
                f"harbor mean {mean!r} below required {require_mean}",
                file=sys.stderr,
            )
            return EXIT_HARBOR
    return EXIT_OK


def _job_mean(stats: dict) -> float | None:
    evals = stats.get("evals") or {}
    if not isinstance(evals, dict):
        return None
    means: list[float] = []
    for body in evals.values():
        if not isinstance(body, dict):
            continue
        for metric in body.get("metrics") or []:
            if isinstance(metric, dict) and isinstance(metric.get("mean"), (int, float)):
                means.append(float(metric["mean"]))
    return means[0] if means else None


if __name__ == "__main__":
    sys.exit(main())
