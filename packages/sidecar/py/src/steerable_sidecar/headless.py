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
import os
import sys
from pathlib import Path

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
    parser.add_argument("--max-rounds", type=int, default=80)
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    instruction = _load_instruction(args.instruction, args.instruction_file)
    if not instruction:
        parser.error("pass --instruction or --instruction-file")
    try:
        asyncio.run(_run(instruction, cwd=args.cwd, max_rounds=args.max_rounds))
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


async def _run(instruction: str, *, cwd: str, max_rounds: int) -> None:
    params = _env_provider_params()
    if not params.get("model"):
        raise ValueError("set STEERABLE_MODEL (or pass Harbor --model)")
    tools = workspace_tools_for_cwd(cwd, jailed=True)
    loop = CoreLoop(
        default_llm_provider_factory(params),
        RouterToolExecutor(tools, consent_granted=True),
        config=LoopConfig(
            max_rounds=max_rounds,
            max_tool_errors=16,
            tool_dedup=False,
        ),
        hooks=ChainHooks(DeliveryHooks(), _default_loop_hooks(params)),
        history_store=InMemoryStorage(),
        record_id="headless",
    )
    seed = [
        LLMMessage.text_of("system", _SYSTEM),
        LLMMessage.text_of("user", instruction),
    ]
    thinking = False
    async for event in loop.run(seed, tools=tools.describe_model(), chat_id="headless"):
        if event.kind == "content_delta":
            thinking = False
            sys.stdout.write(str(event.data.get("delta", "")))
            sys.stdout.flush()
        elif event.kind == "reasoning_delta":
            if not thinking:
                sys.stdout.write("[thinking]\n")
                sys.stdout.flush()
                thinking = True
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
            sys.stdout.write("\n")
            sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
