"""Headless delivery discipline: stop inspect-only loops and force artifacts.

Overnight Terminal-Bench remainder failed several scored tasks with hidden
pytest `FileNotFoundError` on the named output (`eval.scm`, `program.py`,
`re.json`, …) after tens of bash/read_file calls and zero writes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime.hooks import (
    CompletionAction,
    CompletionDraft,
    NoopHooks,
    PreStepAction,
    TranscriptAppend,
)
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.loop import LoopContext
from steerable_sidecar.png_ascii import raster_header_size

_MUTATING = frozenset({"write_file", "edit_file"})
_EXPLORE = frozenset({"bash", "read_file"})
# TB agents usually create scored files with bash (`python … open(…,'w')`,
# `cat > /app/out`). Counting only write_file caused a false no_artifact
# retry after the file already existed (count-dataset-tokens). When the
# instruction names output paths, delivery is those paths existing — not
# write_file of a helper like gen.py or `python3 gen.py`.
_BASH_WRITES = re.compile(
    r"(?:>>|(?<![12])>)\s*(?:/|\./|[A-Za-z0-9._-]+/[A-Za-z0-9._/-]*|[A-Za-z0-9._-]+\.[A-Za-z0-9]+)"
    r"|\btee\b"
    r"|\b(?:cp|mv|touch)\s+\S+"
    r"|open\([^)]*['\"][wa]"
    r"|Path\([^)]*\)\.write"
    r"|\.write_text\("
    r"|\b(?:python3?|pypy3?)\s+\S+\.py\b"
    r"|\b(?:g?cc|g\+\+|clang\+\+|rustc)\s+[^\n]*\s-o\s"
    r"|\b(?:make|cmake|qemu-img)\b"
)
_BASH_BUILDS = re.compile(
    r"\b(?:g?cc|g\+\+|clang\+\+|rustc)\s+[^\n]*\s-o\s"
    r"|\b(?:make|cmake|qemu-img)\b"
)
_BASH_MUTATE_FILE = re.compile(
    r"(?:>>|(?<![12])>)\s*(?:/|\./|[A-Za-z0-9._-]+/[A-Za-z0-9._/-]*|[A-Za-z0-9._-]+\.[A-Za-z0-9]+)"
    r"|\btee\b"
    r"|\b(?:cp|mv|touch)\s+\S+"
    r"|open\([^)]*['\"][wa]"
    r"|Path\([^)]*\)\.write"
    r"|\.write_text\("
)

_COMPACT_MARKER = "[context compacted: earlier conversation summarized]"
_EXPLORE_NUDGE = (
    "You have inspected the workspace for many steps without creating the "
    "required output files. If you already know contents that satisfy every "
    "constraint the instruction states (path, length, format, metric), write "
    "those files now with write_file, edit_file, or bash. Do not paste the "
    "whole program only in reasoning or chat. Do not write placeholders, "
    "decoys, guesses, or a prose description of a rendering."
)
_EXPLORE_NUDGE_MISSING = _EXPLORE_NUDGE + " Still missing: {paths}."
_NO_ARTIFACT_RETRY = (
    "The turn is ending without a write to the named output files. Hidden "
    "tests look for those paths. If you already drafted the contents in "
    "this chat, write them now with write_file, edit_file, or bash; do not "
    "only describe a plan, dump a placeholder, or truncate an existing file."
)
_MISSING_NAMED_RETRY = (
    "The turn is ending but these instruction-named output files still "
    "do not exist: {paths}. Hidden tests look for those paths. Write them "
    "now with write_file, edit_file, or bash — helper scripts alone are "
    "not enough."
)
# Absolute paths TB instructions name as outputs (`/app/re.json`,
# `/tmp/frame.bmp`, `/app/polyglot/cmain`). Existing paths at start are
# inputs. Extensionless names must be nested so `/app/caffe` is not an
# output just because the instruction mentions the clone dir.
_NAMED_OUTPUT_PATH = re.compile(
    r"(?:^|[\s`'\"(\[])"
    r"((?:/app|/tmp|/workspace|/home/agent)"
    r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)"
)
# TB often says "called vm.js" / "run `node vm.js`" without `/app/`.
_CALLED_WITH_EXT = re.compile(
    r"\bcalled\s+([A-Za-z][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]*)\b"
)
_FILE_CALLED = re.compile(
    r"\b(?:file|program|script|binary|elf)\s+called\s+"
    r"([A-Za-z][A-Za-z0-9._-]*)\b",
    re.IGNORECASE,
)
_RUN_ENTRY = re.compile(
    r"`(?:node|python3?|pypy3?)\s+"
    r"([A-Za-z0-9./_-]+\.[A-Za-z][A-Za-z0-9]*)`"
)
_TITLED_FILE = re.compile(
    r"\btitled\s+([A-Za-z][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]*)\b"
)
_EMPTY_ROUND_RETRY = (
    "You produced no tool call and no final answer (reasoning only). "
    "Continue the task now with bash, read_file, write_file, or edit_file. "
    "Do not stop until the required output files exist."
)
# Match CoreLoop ``_MAX_COMPLETION_REDOS`` (16). Eight retries still let
# dna-assembly / steal.py / regex-chess stop after a text-only summary.
_MAX_MISSING_NAMED_RETRIES = 16
# Z.AI coerces tool_choice=required to auto, so nudges are user text only.
# After this many ignored explore nudges, refuse inspect-only tools so
# Harbor's 180 min wait_for cannot be spent on bash/read_file while
# steal.py / primers.fasta / out.txt are still missing.
_BLOCK_EXPLORE_AFTER_NUDGES = 4
_INSPECT_BLOCKED = (
    "Stop inspecting. These instruction-named output files still do not "
    "exist: {paths}. Write them now with write_file, edit_file, or bash "
    "(cat/tee/python to that path). Further read_file or inspect-only bash "
    "is blocked until they exist."
)
# make-doom-for-mips wrote /tmp/frame.bmp at 1024×768 (stock display /
# screenshot) while doomgeneric.h names 640×400. Instruction text does
# not mention those numbers; they live in the named graphics source.
_MAX_SIZE_RETRIES = 4
_NAMED_C_FILE = re.compile(r"\b([A-Za-z][A-Za-z0-9._-]*\.c)\b")
_RESX = re.compile(r"DOOMGENERIC_RESX\s+(\d+)")
_RESY = re.compile(r"DOOMGENERIC_RESY\s+(\d+)")
_IMAGE_SUFFIXES = frozenset({".bmp", ".png"})
_SIZE_MISMATCH_RETRY = (
    "{path} is {got}; the instruction-named graphics source defines "
    "{want}. Compile that source into the binary that writes this file; "
    "do not screenshot a different display size."
)
_UNREADABLE_IMAGE_RETRY = (
    "{path} exists but is not a readable BMP/PNG header. Rewrite it from "
    "the instruction-named graphics source."
)


class DeliveryHooks(NoopHooks):
    """Nudge, then veto completion, when a coding turn never mutates files."""

    def __init__(
        self,
        *,
        explore_before_nudge: int = 8,
        max_nudges: int = 3,
        min_tools_for_completion_retry: int = 2,
        max_empty_round_retries: int = 6,
        instruction: str = "",
        named_outputs: Iterable[str] | None = None,
    ) -> None:
        self._explore_before_nudge = explore_before_nudge
        self._max_nudges = max_nudges
        self._min_tools_for_completion_retry = min_tools_for_completion_retry
        self._max_empty_round_retries = max_empty_round_retries
        raw = (
            tuple(named_outputs)
            if named_outputs is not None
            else named_output_paths(instruction)
        )
        self._instruction = instruction
        self._required = tuple(p for p in raw if not Path(p).exists())
        self._delivered = 0
        self.writes = 0
        self.consecutive_explore = 0
        self.nudges = 0
        self.completion_retries = 0
        self.empty_round_retries = 0
        self._size_retries = 0
        self._compact_nudges = 0
        self._force_tool = False

    def inspect_block_result(self, call: ToolCall) -> ToolResult | None:
        """Refuse inspect-only tools after named-output nudges are ignored.

        Runs *before* the tool so a 3-hour OCR/ffmpeg/python-helper loop
        cannot eat the Harbor window. Bash that compiles (make/gcc) or
        mutates a still-missing named path still runs. ``python3 gen.py``
        and ``cat > explore.py`` do not.
        Extensionless paths (sockets, qemu monitor) do not trigger the gate.
        """
        scored = self._scored_missing()
        if not scored or self.nudges < _BLOCK_EXPLORE_AFTER_NUDGES:
            return None
        name = call.name
        if name not in _EXPLORE:
            return None
        if name == "bash" and _bash_delivers_required(call, scored):
            return None
        listed = ", ".join(scored[:8])
        return ToolResult(
            success=False,
            error=_INSPECT_BLOCKED.format(paths=listed),
            needsFollowup=True,
        )

    def _scored_missing(self) -> tuple[str, ...]:
        return tuple(
            p
            for p in self._required
            if "." in Path(p).name and not Path(p).exists()
        )

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: LoopContext
    ) -> PreStepAction:
        appends = None
        append_action = None
        reason = None
        n_compacts = sum(
            1 for m in transcript if _COMPACT_MARKER in (m.content_text or "")
        )
        new_compact = n_compacts > self._compact_nudges
        missing = tuple(p for p in self._required if not Path(p).exists())
        named_missing = bool(missing)
        # dna-assembly planned primers.fasta for the whole soft timeout after
        # three nudges; keep asking while named outputs are still absent.
        nudge_limit = (
            _MAX_MISSING_NAMED_RETRIES if named_missing else self._max_nudges
        )
        if (
            self.writes == 0
            and self.nudges < nudge_limit
            and (
                self.consecutive_explore >= self._explore_before_nudge
                or new_compact
            )
        ):
            self.nudges += 1
            self.consecutive_explore = 0
            if new_compact:
                self._compact_nudges = n_compacts
            text = (
                _EXPLORE_NUDGE_MISSING.format(paths=", ".join(missing[:8]))
                if missing
                else _EXPLORE_NUDGE
            )
            appends = [
                TranscriptAppend(
                    message=LLMMessage.text_of("user", text),
                    kind="delivery.explore_nudge",
                )
            ]
            reason = "explore_without_write"
            append_action = "delivery_nudge"
        tool_choice = (
            "required"
            if self._force_tool or self.writes == 0
            else None
        )
        if tool_choice and reason is None:
            reason = (
                "empty_round_force_tool"
                if self._force_tool
                else "no_write_force_tool"
            )
        if appends or tool_choice:
            return PreStepAction(
                kind="proceed",
                appends=appends,
                reason=reason,
                tool_choice=tool_choice,
                append_action=append_action,
            )
        return PreStepAction(kind="proceed")

    async def post_tool_result(
        self, result: ToolResult, call: ToolCall, ctx: LoopContext
    ) -> ToolResult:
        self._force_tool = False
        name = call.name
        if self._required:
            delivered = sum(1 for p in self._required if Path(p).exists())
            if delivered > self._delivered:
                self.writes += delivered - self._delivered
                self.consecutive_explore = 0
            elif name in _EXPLORE or name in _MUTATING:
                self.consecutive_explore += 1
            self._delivered = delivered
        elif result.success and (
            name in _MUTATING or (name == "bash" and _bash_writes(call))
        ):
            self.writes += 1
            self.consecutive_explore = 0
        elif name in _EXPLORE:
            self.consecutive_explore += 1
        return result

    def _named_image_size_retry(self) -> CompletionAction | None:
        """Veto a turn whose named BMP/PNG disagrees with source RESX/RESY."""
        if self._size_retries >= _MAX_SIZE_RETRIES:
            return None
        wants = source_resolutions(self._instruction, self._required)
        if not wants:
            return None
        for path in self._required:
            suffix = Path(path).suffix.lower()
            if suffix not in _IMAGE_SUFFIXES or not Path(path).is_file():
                continue
            try:
                raw = Path(path).read_bytes()[:64]
            except OSError:
                continue
            got = raster_header_size(raw)
            if got is None:
                self._size_retries += 1
                self._force_tool = True
                return CompletionAction(
                    kind="retry",
                    message=_UNREADABLE_IMAGE_RETRY.format(path=path),
                    reason="named_image_unreadable",
                )
            if got in wants:
                continue
            want_text = " or ".join(f"{w}x{h}" for w, h in sorted(wants))
            self._size_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_SIZE_MISMATCH_RETRY.format(
                    path=path, got=f"{got[0]}x{got[1]}", want=want_text
                ),
                reason="named_image_size",
            )
        return None

    async def before_completion(
        self, draft: CompletionDraft, ctx: LoopContext
    ) -> CompletionAction:
        # Named paths beat empty-round: gcode-to-text wrap-up sent
        # reasoning-only completions while /app/out.txt was still missing,
        # and empty_round consumed the retries that should have named it.
        missing = tuple(p for p in self._required if not Path(p).exists())
        if missing and self.completion_retries < _MAX_MISSING_NAMED_RETRIES:
            self.completion_retries += 1
            self._force_tool = True
            listed = ", ".join(missing[:8])
            return CompletionAction(
                kind="retry",
                message=_MISSING_NAMED_RETRY.format(paths=listed),
                reason="missing_named_output",
            )
        size_retry = self._named_image_size_retry()
        if size_retry is not None:
            return size_retry
        empty = not (draft.content or "").strip() and not draft.had_tool_calls
        if empty and self.empty_round_retries < self._max_empty_round_retries:
            self.empty_round_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_EMPTY_ROUND_RETRY,
                reason="empty_round",
            )
        if (
            not self._required
            and self.writes == 0
            and draft.tool_calls_used >= self._min_tools_for_completion_retry
            and self.completion_retries < 1
        ):
            self.completion_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_NO_ARTIFACT_RETRY,
                reason="no_artifact",
            )
        return CompletionAction(kind="accept")


def source_resolutions(
    instruction: str, named_paths: Iterable[str]
) -> frozenset[tuple[int, int]]:
    """RESX×RESY pairs from instruction-named ``.c`` / sibling headers."""
    names = tuple(dict.fromkeys(_NAMED_C_FILE.findall(instruction or "")))
    if not names:
        return frozenset()
    images = [Path(p) for p in named_paths]
    found: set[tuple[int, int]] = set()
    for name in names:
        located = _locate_named_file(name, images)
        if located is None:
            continue
        for text in _source_cluster_texts(located, images):
            found.update(_res_pairs(text))
    return frozenset(found)


def _res_pairs(text: str) -> set[tuple[int, int]]:
    xs = [int(match.group(1)) for match in _RESX.finditer(text)]
    ys = [int(match.group(1)) for match in _RESY.finditer(text)]
    return set(zip(xs, ys))


def _locate_named_file(name: str, images: Sequence[Path]) -> Path | None:
    candidates: list[Path] = []
    for image in images:
        candidates.append(image.parent / name)
    app = Path("/app")
    if app.is_dir():
        candidates.append(app / name)
    for path in candidates:
        if path.is_file():
            return path
    for image in images:
        parent = image.parent
        try:
            children = list(parent.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                hit = child / name
                if hit.is_file():
                    return hit
    if app.is_dir():
        for hit in app.rglob(name):
            if hit.is_file():
                return hit
    return None


def _source_cluster_texts(
    source: Path, images: Sequence[Path]
) -> list[str]:
    folders = [source.parent, source.parent.parent]
    for image in images:
        folders.append(image.parent)
    paths = [source]
    seen = {source}
    for folder in folders:
        if folder == folder.parent:
            continue
        header = folder / "doomgeneric.h"
        if header.is_file() and header not in seen:
            paths.append(header)
            seen.add(header)
    texts: list[str] = []
    for path in paths:
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace")[:64_000])
        except OSError:
            continue
    return texts


def named_output_paths(instruction: str) -> tuple[str, ...]:
    """Absolute output paths named in a TB instruction (not `/usr` inputs)."""
    seen: list[str] = []
    text = instruction or ""
    for match in _NAMED_OUTPUT_PATH.finditer(text):
        path = match.group(1).rstrip(".,;:)")
        name = path.rsplit("/", 1)[-1]
        if "." not in name and path.count("/") < 3:
            continue
        if path not in seen:
            seen.append(path)
    for pattern in (_CALLED_WITH_EXT, _FILE_CALLED, _RUN_ENTRY, _TITLED_FILE):
        for match in pattern.finditer(text):
            name = match.group(1).rstrip(".,;:)")
            if name.startswith("/"):
                path = name
            else:
                path = f"/app/{name.lstrip('./')}"
            if path not in seen:
                seen.append(path)
    return tuple(seen)


class DeliveryGatedExecutor:
    """Run inspect-only tools only while named outputs can still wait."""

    def __init__(self, inner: object, hooks: DeliveryHooks) -> None:
        self._inner = inner
        self._hooks = hooks

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        blocked = self._hooks.inspect_block_result(call)
        if blocked is not None:
            return blocked
        return await self._inner.execute(call, ctx)  # type: ignore[union-attr]

    def concurrency_safe(self, call: ToolCall) -> bool:
        check = getattr(self._inner, "concurrency_safe", None)
        return bool(check(call)) if check is not None else False


def _bash_command(call: ToolCall) -> str:
    args = call.arguments or {}
    return str(args.get("command") or args.get("cmd") or args.get("script") or "")


def _bash_writes(call: ToolCall) -> bool:
    return bool(_BASH_WRITES.search(_bash_command(call)))


def _bash_delivers_required(call: ToolCall, required: tuple[str, ...]) -> bool:
    """True when bash compiles or mutates a still-missing named output."""
    command = _bash_command(call)
    if _BASH_BUILDS.search(command):
        return True
    if required and any(path in command for path in required):
        return bool(_BASH_MUTATE_FILE.search(command))
    return False
