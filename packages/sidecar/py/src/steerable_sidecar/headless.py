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
from .delivery import DeliveryGatedExecutor, DeliveryHooks
from .sidecar import (
    _default_loop_hooks,
    _summarizer_for,
    default_llm_provider_factory,
)
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
    "pgrep matches the wait loop; background the job and `wait $!`. Do not "
    "poll with a short `sleep` loop (`sleep 290; cat log`); bash already "
    "waits up to one hour. "
    "Downloads and compiles can take many minutes: do not treat a slow "
    "wget/gcc as a deadlock. Resume incomplete downloads (`wget -c`); do "
    "not extract an unfinished tarball. Do not wrap the main scoring, "
    "compile, or VM command in a short `timeout N` (N under a few minutes); "
    "bash already caps at one hour. "
    "If you start a VM that later telnet/ssh or a screenshot must reach, "
    "start it with -daemonize or nohup/setsid so it survives this bash "
    "tool returning; do not `wait $!` on the VM process. Poll until the "
    "login prompt or desktop is actually there. If the instruction names "
    "a monitor socket for keyboard, send a key and confirm the framebuffer "
    "changes. "
    "Disk-image and deleted-file work may use dd, debugfs, strings, and "
    "carving. If the instruction states a length, size, format, accuracy, or named "
    "path, do not write a candidate that violates it (quantize or shrink an "
    "oversized model). A local numeric check "
    "must use the same metric and split the hidden tests will use. "
    "When fitting peaks or converting measurements, use the x-axis units "
    "already in the data file unless the instruction specifies a conversion. "
    "If hidden tests compare runtime or speedup against a golden or "
    "baseline, time both on the same input and keep optimizing until you "
    "meet the threshold before stopping. "
    "When the instruction names a PDB, fpbase, or other sequence API, query "
    "that API and paste the returned sequence verbatim, including expression "
    "tags; do not substitute a protein recalled from memory that matches a "
    "spectrum. "
    "If the instruction names a scoring CLI, run that CLI for the local "
    "check, not a rewritten metric. For a trained model, use that library's "
    "own test/eval command (the printed P@1 or equivalent), not a handwritten "
    "accuracy loop. If it names a program hidden tests will "
    "execute, run that program on the provided examples, not a rewritten "
    "sidecar. Run it the way the instruction writes it; if it opens a "
    "relative filename, copy that file into /app and into the current "
    "directory so a later `node /app/vm.js` still finds it. Confirm "
    "side-effect files (for example /tmp/frame.bmp) actually appear. "
    "If it names a source file for graphics or I/O, compile that file in, "
    "not a stock video backend. If a produced image width×height does not "
    "match DOOMGENERIC_RESX/RESY (or another named size) in that source, "
    "keep compiling it — do not screenshot a 1024×768 display. "
    "If it shows an example input and the exact output that must "
    "be produced, run that example and keep fixing until the output matches. "
    "If it states a source-size cap, check that pipeline (raw wc -c, or "
    "gzip|wc when the instruction says gzip) and shrink until it fits. "
    "Token counts must use that tokenizer's default "
    "special-token and concatenation settings (do not strip BOS/EOS or "
    "pass add_special_tokens=False unless the instruction says to). "
    "If the scored file is an image, ASCII grid, or other exact rendering, "
    "write those characters — not a paragraph describing them. If the "
    "instruction asks what text a print, image, or recording shows, write "
    "that text string, not a pixel or ASCII raster of the rendering. If the "
    "instruction asks for every solution that meets a criterion, write all "
    "of them. If it names a required stdout phrase, print that exact phrase. "
    "If you design mutagenic primers, reconstruct the product the way a "
    "checker will (reverse-complement of the reverse primer concatenated "
    "with the forward primer) and keep each annealing arm within the stated "
    "length bounds. "
    "Hidden tests score files on disk, not this chat: if a time-budget "
    "notice appears, stop reasoning, wait for background jobs (`wait`), "
    "then write or verify the required files. Do not overwrite an existing "
    "complete output with a truncated write_file. "
    "PNG/JPEG/BMP files are pixels, not UTF-8: read_file returns an ASCII "
    "preview for 8-bit PNG, baseline JPEG, and uncompressed BMP (square "
    "images also get a rank/file 8x8 brightness and occupancy grid); decode exact pixels "
    "with Python (PIL/numpy) or ffmpeg. "
    "If a long video is the input, extract a sparse sample (scene-change or "
    "1 fps), not every frame; OCR that sample and write the scored file "
    "before wrap-up. If the instruction asks for player moves or typed "
    "commands from a video, write those command lines, not a dump of "
    "on-screen narration. If a compiled model continuation is repetitive "
    "BPE garbage, the checkpoint layout is wrong — keep fixing until a "
    "real English prompt continues as English. If that continuation is not "
    "valid UTF-8, the packing is still wrong. If the program "
    "hidden tests will execute raises NameError, fix that file and rerun it. "
    "If you drafted required file contents in this chat or in reasoning, "
    "write_file them to the named path before more inspect steps. "
    "When stripping XSS or other active content from HTML, remove scripts "
    "and javascript: URLs but keep the document well-formed so a headless "
    "browser can still open it. Leave files that contain no active content "
    "byte-identical, including whitespace and whether empty tags are "
    "`<input>` or `<input/>`; do not pretty-print or re-serialize them. "
    "Do not leave leftover tokens such as `);` "
    "or unclosed tags. A Connection refused from that browser "
    "means the sanitizer crashed the session — keep a complete html/head/body "
    "with matching tags. "
    "When extracting one scored string from a concatenated dump, match the "
    "benchmark or dataset the instruction names — not an adjacent title. "
    "If you rank dump lines with a named embedding package, keep that "
    "package's default query prompts; stripping them often writes a "
    "query-word hit instead of the library's own title. "
    "If the instruction says a script is provided to help iterations, run "
    "that script and fix failures before stopping. If it names a config "
    "under /etc, edit that file so the named service settings take effect. "
    "If it names a policy enum or exact setting, apply that value, not a "
    "nearby library default. When wrapping a model for pipeline or tensor "
    "parallel, keep the original forward's extra tensors (position "
    "embeddings, attention mask); do not drop them. "
    "If you recover a matrix from queries, check that it reconstructs "
    "the queried function on held-out inputs before stopping. "
    "When porting a sampler, pass the source warmup, thinning, "
    "adapt-control, seed, chains, and iteration counts into the new "
    "client, not library defaults. "
    "Before finishing, write a small local check for the instruction's "
    "named thresholds (cosine, KL, Levenshtein, runtime, P@1), run it, "
    "and fix failures. If a local check prints a number below the named "
    "bar, that is a failure — keep iterating; do not mark it passed. "
    "After shrinking or quantizing a scored model, re-run that eval CLI; "
    "the pre-shrink score does not count. Your local split is not the "
    "hidden test set: if the named bar is a lower bound, beat it by a "
    "clear margin on your split before shrinking; after quantize, if you "
    "are within 0.02 of the bar, shrink less or keep the unquantized model. "
    "If a posterior mean sits just outside a named interval, increase "
    "warmup and sampling draws and rerun with the same seed; do not stop "
    "a few thousandths under the bound. "
    "Do not replace or uninstall the system interpreter or pytest the "
    "hidden tests will use; do not retarget /usr/local/bin/python or "
    "python3 to a different binary. Install compiled extensions into "
    "the workspace. "
    "Hidden tests still run after you stop."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="steerable-sidecar-headless")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--instruction", help="Task instruction text")
    parser.add_argument("--instruction-file", type=Path)
    parser.add_argument("--max-rounds", type=int, default=250)
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


def _soft_timeout_ms() -> int | None:
    """Wrap up before Harbor's agent-timeout kill.

    Long TB tasks set ``[agent] timeout_sec = 3600``; with Harbor ×3 that is
    180 minutes. Unset defaults to 150 minutes so keep-tools wrap-up has
    ~30 minutes before Harbor ×12 kills a 900s task (180 min). cheap-12
    stays at ×3 (45 min).
    ``STEERABLE_SOFT_TIMEOUT_MS=0`` disables.
    """
    raw = os.environ.get("STEERABLE_SOFT_TIMEOUT_MS")
    if raw is None or not str(raw).strip():
        return 9_000_000
    value = int(raw)
    return None if value <= 0 else value


def _temperature() -> float | None:
    raw = os.environ.get("STEERABLE_TEMPERATURE")
    if raw is None or not str(raw).strip():
        return None
    return float(raw)


def _max_tokens() -> int | None:
    raw = os.environ.get("STEERABLE_MAX_TOKENS")
    if raw is None or not str(raw).strip():
        return None
    value = int(raw)
    return None if value <= 0 else value


async def _run(instruction: str, *, cwd: str, max_rounds: int) -> None:
    params = _env_provider_params()
    if not params.get("model"):
        raise ValueError("set STEERABLE_MODEL (or pass Harbor --model)")
    tools = workspace_tools_for_cwd(cwd, jailed=True)
    provider = default_llm_provider_factory(params)
    delivery = DeliveryHooks(instruction=instruction)
    loop = CoreLoop(
        provider,
        DeliveryGatedExecutor(
            RouterToolExecutor(tools, consent_granted=True),
            delivery,
        ),
        config=LoopConfig(
            max_rounds=max_rounds,
            max_tool_errors=32,
            tool_dedup=False,
            temperature=_temperature(),
            max_tokens=_max_tokens(),
            soft_timeout_ms=_soft_timeout_ms(),
            tool_timeout_ms=3_600_000,
            wrap_up_keeps_tools=True,
            wrap_up_max_tool_rounds=16,
            # steal.py / gcode: a 1-hour bash or another reasoning stream
            # after the 150 min soft timeout ate Harbor's remaining 30 min.
            wrap_up_tool_timeout_ms=120_000,
            wrap_up_hard_cap_ms=10_500_000,
        ),
        hooks=ChainHooks(
            # Compact first so a same-round write nudge is folded onto the
            # rewritten tail instead of sitting in the summarized middle.
            _default_loop_hooks(params, summarizer=_summarizer_for(provider)),
            delivery,
        ),
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
        elif event.kind in ("tool_error", "error", "hook_action", "soft_timeout"):
            sys.stdout.write(f"\n[{event.kind} {event.data}]\n")
            sys.stdout.flush()
        elif event.kind == "completion":
            sys.stdout.write("\n")
            sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
