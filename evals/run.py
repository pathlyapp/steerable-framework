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
    completed = subprocess.run(argv_harbor, cwd=REPO_ROOT)
    if completed.returncode != 0:
        print(f"harbor exited {completed.returncode}", file=sys.stderr)
        return EXIT_HARBOR
    _print_summary(jobs_dir)
    return EXIT_OK


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run pinned Terminal-Bench 2.1 tasks through Harbor. "
            f"Live agents: {', '.join(LIVE_AGENTS)}. DSH is skipped."
        )
    )
    parser.add_argument("--agent", required=True, help="oracle, claude-code, codex, or pi")
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
    parser.add_argument("--harbor", default="harbor", help="Harbor executable")
    return parser.parse_args(argv)


def _print_summary(jobs_dir: Path) -> None:
    results = sorted(jobs_dir.rglob("result.json"))
    if not results:
        print(f"harbor finished; no result.json under {jobs_dir}", file=sys.stderr)
        return
    latest = results[-1]
    try:
        payload = json.loads(latest.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read {latest}: {exc}", file=sys.stderr)
        return
    stats = payload.get("stats") if isinstance(payload, dict) else None
    print(f"harbor result: {latest}")
    if isinstance(stats, dict):
        print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
