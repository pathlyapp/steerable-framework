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
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.storage import InMemoryStorage

from .acp_adapter import _env_provider_params
from .sidecar import default_llm_provider_factory
from .workspace_tools import workspace_tools_for_cwd

__version__ = "0.2.5"

_SYSTEM = (
    "You are a coding agent in a Linux workspace. Solve the task using the "
    "bash, read_file, and write_file tools. Paths are relative to the current "
    "workspace. Do not wait for confirmation."
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
    tools = workspace_tools_for_cwd(cwd)
    loop = CoreLoop(
        default_llm_provider_factory(params),
        RouterToolExecutor(tools, consent_granted=True),
        config=LoopConfig(max_rounds=max_rounds),
        history_store=InMemoryStorage(),
        record_id="headless",
    )
    seed = [
        LLMMessage.text_of("system", _SYSTEM),
        LLMMessage.text_of("user", instruction),
    ]
    async for event in loop.run(seed, tools=tools.describe_model(), chat_id="headless"):
        if event.kind == "content_delta":
            sys.stdout.write(str(event.data.get("delta", "")))
            sys.stdout.flush()
        elif event.kind == "completion":
            sys.stdout.write("\n")
            sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
