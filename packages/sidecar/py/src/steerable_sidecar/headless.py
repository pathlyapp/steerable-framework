"""One-shot coding-agent entry for Harbor / Terminal-Bench.

Reads an instruction, runs CoreLoop with workspace bash/file tools, exits.
Does not start Electron. Provider config comes from the environment:

``STEERABLE_PROVIDER`` / ``STEERABLE_MODEL`` / ``STEERABLE_BASE_URL`` /
``STEERABLE_API_KEY`` (falling back to ``OPENAI_API_KEY`` /
``ANTHROPIC_API_KEY``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from steerable_agent_runtime import CoreLoop, LoopConfig, RouterToolExecutor
from steerable_agent_runtime.hooks import ChainHooks
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.storage import InMemoryStorage

from .acp_adapter import _env_provider_params
from .delivery import DeliveryHooks
from .sidecar import _default_loop_hooks, default_llm_provider_factory
from .workspace_tools import workspace_tools_for_cwd

__version__ = "0.2.5"

_SYSTEM = (
    "You are a coding agent in a Linux workspace. Complete the user's task "
    "by using bash, read_file, write_file, and edit_file. Inspect, then write "
    "the required output files; do not only explore or describe a plan. "
    "Prefer edit_file for in-place edits. Do not wait for confirmation. "
    "Hidden tests check exact files, formatting, and PATH: write every path "
    "the instruction names; do not pretty-print files compared byte-for-byte; "
    "print dates as YYYY-MM-DD when a check must show expiry; after "
    "apt-installing a binary, make `which <name>` work (symlink into /usr/bin "
    "if it landed in /usr/sbin). Do not wait with `while pgrep -f ...` — "
    "pgrep matches the wait loop; background the job and `wait $!`."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="steerable-sidecar-headless")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--instruction", help="Task instruction text")
    parser.add_argument("--instruction-file", type=Path)
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help="Round cap; overrides the harness spec's loop.max_rounds (default 80)",
    )
    parser.add_argument(
        "--harness",
        type=Path,
        help="Harness spec YAML (W1.2.2); omitted = the built-in default chain",
    )
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    instruction = _load_instruction(args.instruction, args.instruction_file)
    if not instruction:
        parser.error("pass --instruction or --instruction-file")
    try:
        asyncio.run(
            _run(
                instruction,
                cwd=args.cwd,
                max_rounds=args.max_rounds,
                harness_path=args.harness,
            )
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


def _load_instruction(text: str | None, path: Path | None) -> str:
    if text:
        return text
    if path is not None:
        return path.read_text(encoding="utf-8")
    return ""


def _assemble_harness(
    path: Path,
    params: dict[str, Any],
    *,
    provider: Any,
    executor: Any,
    tools: Any,
) -> tuple[Any, Any, Any, list[dict[str, Any]], Any]:
    """W1.2.2: assemble the declarative spec into the loop's seams.

    Returns (hooks, storage, executor, tool_descriptors, loop_limits). The
    spec's context/retry/validator/memory strategies REPLACE the built-in
    default chain — that is the factorial protocol's point: arms differ in
    exactly the dimensions the spec names. DeliveryHooks stays: delivery
    semantics are the transport's, not a harness dimension.
    """
    from steerable_agent_runtime.harness_spec import assemble_harness, load_harness_spec
    from steerable_agent_runtime.tokens import resolve_context_window

    spec = load_harness_spec(path)
    max_ctx = resolve_context_window(
        params.get("model"),
        explicit=int(params.get("maxContextTokens") or 0) or None,
        provider=params.get("provider"),
        base_url=params.get("baseUrl"),
    )
    assembled = assemble_harness(
        spec,
        provider=provider,
        runtime_params={
            "pressure_compaction": {"max_context_tokens": max_ctx},
            "informed_backtrack": {"max_context_tokens": max_ctx},
        },
    )
    descriptors = assembled.tool_selection.select(tools.describe_model())
    wrapped = assembled.orchestration.wrap(executor, provider=provider, tools=descriptors)
    if spec.orchestration.impl != "single":
        # The delegation family is the orchestration dimension's own surface:
        # advertised past tool selection (orthogonal dimension), mirroring the
        # sidecar chat path. Without this the subagent arm would run with no
        # agent_* tools visible — a sham comparison. (Caught by smoke test:
        # the model answered "no subagent tool exists".)
        from steerable_agent_runtime.orchestration import orchestration_tool_descriptors

        descriptors = [*descriptors, *orchestration_tool_descriptors()]
    return (
        ChainHooks(DeliveryHooks(), assembled.hooks),
        assembled.storage,
        wrapped,
        descriptors,
        spec.loop,
    )


async def _run(
    instruction: str,
    *,
    cwd: str,
    max_rounds: int | None = None,
    harness_path: Path | None = None,
) -> None:
    params = _env_provider_params()
    if not params.get("model"):
        raise ValueError("set STEERABLE_MODEL (or pass Harbor --model)")
    tools = workspace_tools_for_cwd(cwd, jailed=True)
    provider = default_llm_provider_factory(params)
    # consent_granted=True is deliberate and scoped to this entrypoint:
    # headless runs (Harbor evals, CI) are unattended — nobody answers an
    # approval prompt, and AutoApprover would auto-deny bash (mode
    # "other"), breaking every eval task. Safety here comes from the
    # layers that still apply: the workspace jail (tools scoped to cwd),
    # the router's critical-command blocklist, and the disposable eval
    # sandbox around the whole process. Interactive transports (ACP,
    # desktop sidecar) must NOT copy this — they wire ApprovalExecutor.
    executor: Any = RouterToolExecutor(tools, consent_granted=True)
    limits: Any = None
    if harness_path is None:
        hooks: Any = ChainHooks(DeliveryHooks(), _default_loop_hooks(params))
        storage: Any = InMemoryStorage()
        tool_descriptors = tools.describe_model()
    else:
        hooks, storage, executor, tool_descriptors, limits = _assemble_harness(
            harness_path, params, provider=provider, executor=executor, tools=tools
        )
    loop = CoreLoop(
        provider,
        executor,
        config=LoopConfig(
            max_rounds=max_rounds or (limits.max_rounds if limits else None) or 80,
            max_tool_errors=(limits.max_tool_errors if limits else None) or 16,
            tool_dedup=(
                limits.tool_dedup
                if limits is not None and limits.tool_dedup is not None
                else False
            ),
        ),
        hooks=hooks,
        history_store=storage,
        record_id="headless",
    )
    seed = [
        LLMMessage.text_of("system", _SYSTEM),
        LLMMessage.text_of("user", instruction),
    ]
    thinking = False
    # Run-summary telemetry (W1.4.3.3): the attribution report parses the
    # final STEERABLE_RUN_SUMMARY line. Everything is derived from the loop
    # event stream — never from provider internals — so the contract holds
    # for any harness spec. cost_usd stays absent: the catalog deliberately
    # carries no pricing fields, and a missing measurement is not zero.
    summary_rounds = 0
    summary_peak_context = 0
    summary_tool_errors = 0
    summary_pending_recovery = 0
    summary_tool_recoveries = 0
    summary_usage: dict[str, Any] = {}
    try:
        async for event in loop.run(seed, tools=tool_descriptors, chat_id="headless"):
            if event.kind == "llm_request":
                summary_rounds = max(summary_rounds, int(event.data.get("round", -1)) + 1)
            elif event.kind == "llm_response":
                summary_peak_context = max(
                    summary_peak_context, int(event.data.get("promptTokens") or 0)
                )
            elif event.kind == "tool_call_result":
                # Raised calls emit tool_error AND land here as success=False —
                # counting only this event avoids double-counting.
                if event.data.get("success"):
                    if summary_pending_recovery:
                        summary_pending_recovery -= 1
                        summary_tool_recoveries += 1
                else:
                    summary_tool_errors += 1
                    summary_pending_recovery += 1
            elif event.kind == "content_delta":
                thinking = False
                sys.stdout.write(str(event.data.get("delta", "")))
                sys.stdout.flush()
            elif event.kind == "reasoning_delta":
                if not thinking:
                    sys.stdout.write("[thinking]\n")
                    thinking = True
                # Log reasoning text too: a long generation must keep the log
                # growing so external stall watchdogs see liveness.
                sys.stdout.write(str(event.data.get("delta", "")))
                sys.stdout.flush()
            elif event.kind == "tool_call_start":
                thinking = False
                sys.stdout.write(
                    f"\n[tool {event.data.get('name')} {event.data.get('arguments')}]\n"
                )
                sys.stdout.flush()
            elif event.kind in ("tool_error", "error"):
                sys.stdout.write(f"\n[{event.kind} {event.data}]\n")
                sys.stdout.flush()
            elif event.kind == "completion":
                usage = event.data.get("usage")
                if isinstance(usage, dict):
                    summary_usage = usage
                sys.stdout.write("\n")
                sys.stdout.flush()
    finally:
        # Interactive sessions are real processes; a headless run must not
        # leak them past its own lifetime.
        sessions = getattr(tools, "shell_sessions", None)
        if sessions is not None:
            sessions.close_all()
        # Terminal line, after all model output: the parser keys on the
        # prefix and ignores everything earlier in the log.
        sys.stdout.write(
            "\nSTEERABLE_RUN_SUMMARY "
            + json.dumps(
                {
                    "rounds": summary_rounds or None,
                    "input_tokens": summary_usage.get("promptTokens"),
                    "output_tokens": summary_usage.get("completionTokens"),
                    "peak_context_tokens": summary_peak_context or None,
                    "tool_errors": summary_tool_errors,
                    "tool_recoveries": summary_tool_recoveries,
                }
            )
            + "\n"
        )
        sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
