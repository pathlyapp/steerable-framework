"""Headless delivery discipline: stop inspect-only loops and force artifacts.

Overnight Terminal-Bench remainder failed several scored tasks with hidden
pytest `FileNotFoundError` on the named output (`eval.scm`, `program.py`,
`re.json`, …) after tens of bash/read_file calls and zero writes.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import socket
import stat
import subprocess
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
# ``cat doomgeneric_img.c`` after wrap-up named (same as read_file).
_BASH_VIEW_FILE = re.compile(
    r"^\s*(?:cat|head|tail|nl)(?:\s+-\S+)*\s+(\S+)\s*$"
)
_BASH_RUN_ENTRYPOINT = re.compile(r"\bnode\s+\S+")
# steal.py / pystan_analysis.py: the named script exists; running it
# writes a still-missing sibling (.npy, csv).
_BASH_RUN_PYTHON = re.compile(
    r"\b(?:python3?|pypy3?)\s+(['\"]?)([^'\"\s]+\.py)\1"
)
_BASH_MUTATE_FILE = re.compile(
    r"(?:>>|(?<![12])>)\s*(?:/|\./|[A-Za-z0-9._-]+/[A-Za-z0-9._/-]*|[A-Za-z0-9._-]+\.[A-Za-z0-9]+)"
    r"|\btee\b"
    r"|\b(?:cp|mv|touch)\s+\S+"
    r"|open\([^)]*['\"][wa]"
    r"|Path\([^)]*\)\.write"
    r"|\.write_text\("
)
# Command words that move bytes around without running the work: viewing,
# hashing, searching, and in-place text edits. ``_unverified_retry`` asks
# whether anything has run since the output last changed, and an unknown
# word counts as having run, so this list can only make that gate stand
# down, never make it fire on a trial that did execute something. That
# inversion is why a word list is usable here where a list of checks would
# not be: the set of ways to look at a file is closed, the set of ways to
# check one is not. ``sed`` belongs here for the same reason ``cat`` does —
# a blind in-place edit leaves the output no more verified than before.
_BASH_NO_RUN = frozenset(
    {
        "awk", "base64", "basename", "cat", "cd", "cksum", "cmp", "column",
        "cut", "diff", "dirname", "du", "echo", "egrep", "false", "fgrep",
        "file", "find", "grep", "head", "hexdump", "jq", "less", "ln", "ls",
        "md5sum", "more", "nl", "od", "printf", "pwd", "readlink", "realpath",
        "rev", "rg", "sed", "sha1sum", "sha256sum", "sort", "stat", "strings",
        "tail", "tr", "tree", "true", "uniq", "wc", "which", "xxd",
    }
)
_SHELL_SPLIT = re.compile(r"\|\||&&|[;|&\n]")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_COMPACT_MARKER = "[context compacted: earlier conversation summarized]"
_EXPLORE_NUDGE = (
    "You have inspected the workspace for many steps without creating the "
    "required output files. If you already know contents that satisfy every "
    "constraint the instruction states (path, length, format, metric), write "
    "those files now with write_file, edit_file, or bash "
    "(`cat > path <<'EOF'`). Do not paste the "
    "whole program only in reasoning or chat. Do not write placeholders, "
    "decoys, guesses, or a prose description of a rendering."
)
_EXPLORE_NUDGE_MISSING = _EXPLORE_NUDGE + " Still missing: {paths}."
_NO_ARTIFACT_RETRY = (
    "The turn is ending without a write to the named output files. Hidden "
    "tests look for those paths. If you already drafted the contents in "
    "this chat, write them now with bash `cat > path <<'EOF'` or "
    "write_file; do not only describe a plan, dump a placeholder, or "
    "truncate an existing file."
)
_MISSING_NAMED_RETRY = (
    "The turn is ending but these instruction-named output files still "
    "do not exist: {paths}. Hidden tests look for those paths. Emit a "
    "bash tool call now: `cat > {first} <<'EOF'` (or write_file). "
    "Pasting the program only in chat does not create the file."
)
_WRAP_UP_MARKER = "The time budget for this task is"
_LIVELOCK_WRITE_NOW = (
    "[system notice] Forced tool rounds produced no calls. Stop retrying "
    "empty reasoning: write the required files now, or state the blocker."
)
_WRAP_UP_NAMED = (
    "Time is almost up. These instruction-named output files still do "
    "not exist: {paths}. Emit a bash tool call now: "
    "`cat > {first} <<'EOF'` (or write_file). Do not keep exploring or "
    "only reason in chat."
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
    r"\bcalled\s+[`'\"]?([A-Za-z][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]*)[`'\"]?"
)
_FILE_CALLED = re.compile(
    r"\b(?:file|program|script|binary|elf|executable)(?:\s+\w+){0,2}\s+called\s+"
    r"[`'\"]?([A-Za-z][A-Za-z0-9._-]*)[`'\"]?",
    re.IGNORECASE,
)
_RUN_ENTRY = re.compile(
    r"`(?:node|python3?|pypy3?)\s+"
    r"([A-Za-z0-9./_-]+\.[A-Za-z][A-Za-z0-9]*)`"
)
_RUN_COMMAND = re.compile(
    r"`((?:node|python3?|pypy3?)\s+[A-Za-z0-9./_-]+\.[A-Za-z][A-Za-z0-9]*)`"
)
_TITLED_FILE = re.compile(
    r"\b(?:titled|named)\s+[`'\"]?([A-Za-z][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]*)[`'\"]?",
    re.IGNORECASE,
)
_WRITE_A_FILE = re.compile(
    r"\bwrite a (?:c |python |js |javascript |rust )?(?:file|program|script)\s+"
    r"[`'\"]?([A-Za-z][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]*)",
    re.IGNORECASE,
)
_WRITE_TO_FILE = re.compile(
    r"\bwrite (?:your )?(?:program|output|script|file|warrior)\s+to\s+"
    r"[`'\"]([A-Za-z][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]*)[`'\"]",
    re.IGNORECASE,
)
_NEW_FILE = re.compile(
    r"\ba new file\s+[`'\"]?([A-Za-z][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]*)",
    re.IGNORECASE,
)
# The patterns above each key on a verb phrasing ("write a file X", "named
# X"), so an instruction that states the requirement as a checklist item
# instead names no output at all. Four of the six stable reds whose hidden
# test reported the required file simply absent were missed that way:
# `image.c` must exist, `primers.fasta` exists and contains…, Confirms
# `my_warrior.red` was created, The output files (`ACCOUNTS.DAT`, …).
#
# Anchoring on the existence assertion rather than on the backticks is what
# keeps this selective. Treating every backticked filename as an output was
# measured over the 89 catalog instructions and adds 105 candidates across 49
# of the 58 passing tasks — including `test_outputs.py`, the hidden test
# itself, and `np.float64`, which is not a file. These three add 3, all of
# them plausible real outputs on tasks that already deliver them.
_ASSERTS_EXISTS = re.compile(
    r"`([A-Za-z][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]{0,7})`\s+"
    r"(?:must\s+|should\s+)?(?:exists?\b|be\s+created\b|was\s+created\b)",
    re.IGNORECASE,
)
_CONFIRMS_CREATED = re.compile(
    r"(?:confirms?|verif\w+|checks?)[^.\n]{0,30}"
    r"`([A-Za-z][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]{0,7})`"
    r"[^.\n]{0,25}(?:created|exists?)",
    re.IGNORECASE,
)
# Captures the run of text after "output file(s)" so every name in a list is
# read, not just the first: cobol-modernization names three .DAT files in one
# parenthesised list and the hidden test requires all three. The run cannot
# stop at a full stop, because the filenames themselves contain one; the
# length cap and the backtick requirement are what bound it instead.
_OUTPUT_FILE_LIST = re.compile(r"output\s+files?\b([^\n]{0,120})", re.IGNORECASE)
_BACKTICKED_FILE = re.compile(
    r"`([A-Za-z][A-Za-z0-9._-]*\.[A-Za-z][A-Za-z0-9]{0,7})`"
)
_COMPILE_AND_RUN = re.compile(
    r"\b((?:g?cc|clang)(?:\s+-\S+)*\s+-o\s+\S+\s+\S+\.c(?:\s+-lm)?"
    r"\s*&&\s*\./[A-Za-z0-9._-]+)"
)
_EMPTY_ROUND_RETRY = (
    "You produced no tool call and no final answer (reasoning only). "
    "Continue the task now with bash, read_file, write_file, or edit_file. "
    "Do not stop until the required output files exist."
)
_UNVERIFIED_RETRY = (
    "The turn is ending with nothing having run against the output since it "
    "was last written — viewing it does not count — so nothing would have "
    "caught it being wrong. Run one check now and fix what it reports: the "
    "program the instruction says hidden tests will execute, on the examples the "
    "instruction gives; the scoring CLI, eval command, or helper script it "
    "names; or a small check of the thresholds it states. Reading the file "
    "back is not a check. If the check passes, say so and stop."
)
# One retry, because the gain is in going from no check to a check, not in
# checking more. Measured over catalog-89 run 33369888461 by how many tool
# calls followed the last write, classified by ``_bash_writes`` — the same
# detector the write side of the gate uses, since most TB tasks deliver
# through bash rather than write_file: stopping on the write passes at
# 0.6875 (n=32), one or two calls after it at 0.7429 (n=35), three to five
# at 0.7273 (n=11), and eleven or more at 0.0000 (n=4). So one extra round
# is worth about five points and further rounds are not, but note that the
# tail carrying "further rounds are harmful" is seven trials, which sets the
# cap by the shape of the curve rather than by a well-measured cliff. Those
# counts predate the gate reading "ran" rather than "wrote last", which
# widens who it fires on without changing what one more round buys them.
_MAX_UNVERIFIED_RETRIES = 1
# Match CoreLoop ``_MAX_COMPLETION_REDOS`` (32). A lower cap accepted a
# text-only stop while primers.fasta / steal.py / out.txt were still
# missing, with wrap-up and idle-stream cuts still unused.
_MAX_MISSING_NAMED_RETRIES = 32
# Explore nudges stay bounded separately so inspect-gate (2 ignored
# nudges) is not delayed by the completion-redo budget.
_MAX_NAMED_EXPLORE_NUDGES = 16
# Z.AI coerces tool_choice=required to auto, so nudges are user text only.
# After this many ignored explore nudges, refuse inspect-only tools so
# Harbor's 180 min wait_for cannot be spent on bash/read_file while
# steal.py / primers.fasta / out.txt are still missing. Two ignored
# nudges is 8 inspect steps when named outputs are missing (gate 4);
# the default 8 would let extract-moves OCR every frame until Harbor
# 10800s.
_BLOCK_EXPLORE_AFTER_NUDGES = 2
# Default explore_before_nudge is 8. Named outputs still missing after 4
# inspects: nudge sooner so two ignored nudges gate ffmpeg/OCR/read loops
# before Harbor's 180 min wait_for. Tests that pass a smaller constructor
# value keep that value.
_EXPLORE_BEFORE_NUDGE_NAMED = 4
_INSPECT_BLOCKED = (
    "Stop inspecting. These instruction-named output files still do not "
    "exist: {paths}. Write them now with write_file, edit_file, or bash "
    "(cat/tee/python to that path). Further inspect-only bash is blocked "
    "until they exist. read_file of a file already on disk is still allowed."
)
# Instruction-named graphics source: RESX/RESY in a .c / header vs the
# named BMP/PNG on disk.
_MAX_SIZE_RETRIES = 4
_NAMED_C_FILE = re.compile(r"\b([A-Za-z][A-Za-z0-9._-]*\.c)\b")
_RESX = re.compile(r"RESX\s+(\d+)")
_RESY = re.compile(r"RESY\s+(\d+)")
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
# gpt2-codegolf: "Your c program must be <5000 bytes."
_BYTES_CAP = re.compile(r"<\s*(\d+)\s*bytes", re.IGNORECASE)
_SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".h", ".py", ".js", ".rs"})
_MAX_BYTES_RETRIES = 4
_BYTES_CAP_RETRY = (
    "{path} is {got} bytes; the instruction requires < {cap} bytes. "
    "Shrink it (wc -c) before stopping."
)
# train-fasttext: "less than 150MB"
_MB_CAP = re.compile(
    r"(?:less than|under|<)\s*(\d+)\s*(?:MB|megabytes)\b",
    re.IGNORECASE,
)
_MB_CAP_RETRY = (
    "{path} is {got} bytes; the instruction requires < {cap} MB. "
    "Shrink it before stopping."
)
# Instruction asks what a print shows — a huge raster dump is not that string.
_ASKS_SHOWN_TEXT = re.compile(
    r"what will the text show|what text a print",
    re.IGNORECASE,
)
_MAX_SHOWN_TEXT_BYTES = 4096
_SHOWN_TEXT_RETRY = (
    "{path} is {got} bytes; the instruction asks for the text a print "
    "shows, not a raster of the rendering. Write the short string."
)
_SHOWN_TEXT_PLACEHOLDER = re.compile(
    r"^(?:PROVISIONAL|PLACEHOLDER|TODO|TBD|FIXME|WIP|PENDING|STUB|"
    r"UNFINISHED|INCOMPLETE|TEMPORARY)\s*$",
    re.IGNORECASE,
)
_SHOWN_PLACEHOLDER_RETRY = (
    "{path} is a stub ({got!r}), not the text a print shows. Write the "
    "decoded print output; do not leave a status word."
)
# path-tracing-reverse: "<2k when compressed (`cat mystery.c | gzip | wc`)"
_GZIP_K_CAP = re.compile(r"<\s*(\d+)\s*k\b", re.IGNORECASE)
_GZIP_CAP_RETRY = (
    "{path} gzip-compresses to {got} bytes; the instruction requires "
    "< {cap} when compressed. Shrink it (gzip|wc) before stopping."
)
# regex-chess: all_legal_next_positions uses fen.split("\n"); a trailing
# newline in a JSON replacement became `Our move:  ` (empty illegal row).
_SPLIT_NEWLINES = re.compile(r"""\.split\(\s*['\"]\\n['\"]\s*\)""")
_MAX_JSON_RETRIES = 4
_JSON_BLANK_RETRY = (
    "{path} has an empty JSON string or a newline inside a string. A "
    "checker that splits on newlines treats that as an empty illegal row. "
    "Strip trailing newlines and empty replacements."
)
_CHECKER_NAME = re.compile(r"\b((?:check|eval)\.py)\b", re.IGNORECASE)
_CHECKER_FILENAMES = ("check.py", "eval.py")
_MAX_CHECKER_RUNS = 2
_CHECKER_TIMEOUT_SEC = 60
_CHECKER_FAIL = (
    "{name} exited {code}. Fix the named outputs until python3 {name} "
    "succeeds.\n{output}"
)
_CHECKER_TIMEOUT = (
    "{name} did not finish in {sec}s. Fix the hang or run a smaller "
    "local check before stopping."
)
_CHECKER_RETRY = (
    "A helper script ({name}) exists on disk. Run it now (python3 {name}) "
    "against the named outputs and fix failures before stopping."
)
# Mechanical checks on named artifacts. Not hidden-test answer matching.
_MAX_VALIDATE_RETRIES = 4
_SYNTAX_TIMEOUT_SEC = 20
_EMPTY_RETRY = (
    "{path} is empty. Write the real artifact at this path; an empty "
    "file will fail the tests that read it."
)
_UTF8_RETRY = (
    "{path} is not valid UTF-8. Rewrite it as UTF-8; compilers decode "
    "named sources as text."
)
_SYNTAX_RETRY = (
    "{path} does not parse:\n{output}"
)
# Instruction backtick: `node vm.js` / `python3 steal.py`. One shot when
# named outputs are still missing and the script is already on disk.
_MAX_ENTRY_RUNS = 1
_ENTRY_TIMEOUT_SEC = 180
_SIDE_EFFECT_SUFFIXES = frozenset(
    {".bmp", ".png", ".ppm", ".txt", ".npy", ".csv", ".json", ".fasta", ".fa"}
)
# Named sources are written by the agent, not produced by ``make``. Treating
# ``vm.js`` / ``ars.R`` as make targets burned the one make shot and skipped
# the backtick entrypoint that actually writes /tmp/frame.bmp.
_SOURCE_SUFFIXES = frozenset(
    {
        ".js",
        ".mjs",
        ".cjs",
        ".py",
        ".r",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".scm",
        ".lisp",
        ".red",
        ".rs",
        ".go",
        ".java",
        ".rb",
        ".sh",
        ".pl",
        ".ts",
    }
)
_ENTRY_FAIL = (
    "Ran `{cmd}` because named outputs were still missing. It exited "
    "{code}:\n{output}\nStill missing: {paths}."
)
_ENTRY_TIMEOUT = (
    "Ran `{cmd}` for {sec}s; named outputs still missing: {paths}. "
    "Drop short `timeout` wrappers so the process can finish."
)
_ENTRY_STILL_MISSING = (
    "Ran `{cmd}`; these named outputs are still missing: {paths}. "
    "The command must write them."
)
# Named ELF still missing and a Makefile is on disk (doomgeneric / MIPS).
_MAX_MAKE_RUNS = 1
_MAKE_TIMEOUT_SEC = 180
# Instruction-named TCP services (qemu ``telnet 127.0.0.1 6665``, nginx
# on port 80). Not a listen-port retry for tasks that name no ports.
_TELNET_ADDR = re.compile(
    r"\btelnet\s+(\d{1,3}(?:\.\d{1,3}){3}|localhost)\s+(\d{1,5})\b",
    re.IGNORECASE,
)
_NGINX_PORT = re.compile(
    r"\bnginx\b[^\n]{0,80}\bon port\s+(\d{1,5})\b",
    re.IGNORECASE,
)
_LISTENING_PORT = re.compile(
    r"\blistening on port\s+(\d{1,5})\b",
    re.IGNORECASE,
)
_MAX_LISTEN_RETRIES = 4
# QEMU ``-serial telnet:...,server,nowait`` is a single client. Connecting
# to peek ``login:`` drains boot output; the hidden test then reads empty.
_PROC_TCP = Path("/proc/net/tcp")
_PROC_TCP6 = Path("/proc/net/tcp6")
_LISTEN_ST = "0A"
_TELNET_RETRY = (
    "The instruction names `telnet {host} {port}`. Nothing is listening "
    "on that port. Keep the VM running in the background "
    "(-daemonize / nohup) until the port is open; do not stop on a "
    "helper that exits after qemu starts."
)
_TELNET_PROXY_RETRY = (
    "The instruction names `telnet {host} {port}`. A userspace process "
    "({comm}) is listening there instead of the VM serial. Bind the "
    "hypervisor serial to that port directly (`-serial telnet:…`); a "
    "replay proxy will not run a live login."
)
_SCRIPT_LISTEN_COMM = re.compile(
    r"^(python(\d+(\.\d+)*)?|node(js)?|ruby|perl|bash|sh|dash|zsh|pytest)$",
    re.IGNORECASE,
)
_LISTEN_RETRY = (
    "The instruction names a service on port {port}. Nothing accepted a "
    "TCP connection there. Start it in the background and leave it running."
)
# `/tmp/qemu-monitor.sock` is a UNIX socket, not a scored file. Treating it
# as named output made write_file create a regular file and blocked QEMU.
_SOCKET_SUFFIXES = frozenset({".sock", ".socket"})
_MAX_SOCKET_RETRIES = 4
_SOCKET_RETRY = (
    "The instruction names `{path}` as a monitor socket. That path is not "
    "a UNIX socket. Start the VM with `-monitor unix:{path},server,nowait` "
    "(or equivalent) and leave it running; do not write a regular file "
    "there."
)
# "build for only CPU execution": CMakeCache / Makefile.config must enable
# CPU_ONLY. Not a hidden-test dump — the instruction names the constraint.
_CPU_ONLY_INSTRUCTION = re.compile(
    r"\bonly CPU\b|\bCPU-only\b|\bCPU only\b|\bCPU_ONLY\b",
    re.IGNORECASE,
)
_MAX_CPU_ONLY_RETRIES = 4
_CPU_ONLY_RETRY = (
    "The instruction requires a CPU-only build. {path} does not enable "
    "CPU_ONLY (CMakeCache `CPU_ONLY:BOOL=ON`, or Makefile.config "
    "`CPU_ONLY := 1`). Reconfigure that build."
)


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _delivery_verify_enabled() -> bool:
    """Post-write verification gate (20a854d). ``STEERABLE_DELIVERY_VERIFY=0`` disables."""
    return _env_flag("STEERABLE_DELIVERY_VERIFY", default=True)


def _livelock_empty_streak() -> int:
    """Consecutive wrap-up ``tool_choice=required`` rounds with no tool call.

    Unset or ``0`` disables. A flaky arm sets ``STEERABLE_LIVELOCK_EMPTY_STREAK=3``.
    """
    raw = os.environ.get("STEERABLE_LIVELOCK_EMPTY_STREAK")
    if raw is None or not str(raw).strip():
        return 0
    return max(0, int(raw))


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
        self._named = tuple(raw)
        self._required = tuple(p for p in raw if not Path(p).exists())
        self._delivered = 0
        self.writes = 0
        self.consecutive_explore = 0
        self.ran_since_write = False
        self.nudges = 0
        self.completion_retries = 0
        self.empty_round_retries = 0
        self._size_retries = 0
        self._bytes_retries = 0
        self._json_retries = 0
        self._check_retries = 0
        self._validate_retries = 0
        self._entry_runs = 0
        self._make_runs = 0
        self._verify_retries = 0
        self._wrapping = False
        self._compact_nudges = 0
        self._wrap_up_named_nudges = 0
        self._force_tool = False
        self._forced_empty_streak = 0
        self._livelock_nudged = False
        self._listen_retries = 0
        self._socket_retries = 0
        self._sockets = named_socket_paths(instruction)
        self._cpu_only_retries = 0

    def inspect_block_result(self, call: ToolCall) -> ToolResult | None:
        """Refuse inspect-only tools after named-output nudges are ignored.

        Runs *before* the tool so a 3-hour OCR/ffmpeg/python-helper loop
        cannot eat the Harbor window. Also gates after the wrap-up named
        nudge so a render/OCR loop cannot continue after the time-budget
        notice when explore nudges were still 0. Bash that compiles (make/gcc), runs
        ``node …`` (side-effect frames), runs an on-disk named ``.py``,
        runs a helper ``.py`` whose source mutates a still-missing named
        path, or mutates a still-missing named path still runs.
        ``python3 gen.py`` that only inspects, and ``cat > explore.py``,
        do not. ``read_file`` of a path that already exists still runs,
        as does ``cat``/``head`` of that same on-disk file.
        Extensionless paths (sockets, qemu monitor) do not trigger the gate.
        """
        scored = self._scored_missing()
        gated = (
            self.nudges >= _BLOCK_EXPLORE_AFTER_NUDGES
            or self._wrap_up_named_nudges >= 1
        )
        if not scored or not gated:
            return None
        name = call.name
        if name not in _EXPLORE:
            return None
        if name == "read_file" and _read_file_on_disk(call):
            return None
        if name == "bash" and (
            _bash_delivers_required(call, scored, self._named)
            or _bash_reads_existing(_bash_command(call))
        ):
            return None
        listed = ", ".join(scored[:8])
        return ToolResult(
            success=False,
            error=_INSPECT_BLOCKED.format(paths=listed),
            needsFollowup=True,
        )

    def tool_made_progress(self, result: ToolResult, call: ToolCall) -> bool:
        """Only a successful write resets the loop's no-progress budget.

        Inspection is how a spiral looks from the outside: the failing
        catalog trials ran bash and read_file the whole way, so counting
        every successful call would reset the budget continuously and the
        guard would never fire. ``_BASH_WRITES`` already draws this line for
        the artifact retries — a compile, a `make`, or running a generator
        script delivers as much as a redirect.
        """
        if not result.success:
            return False
        if call.name in _MUTATING:
            return True
        return call.name == "bash" and _bash_writes(call)

    def wrap_up_may_drop_tools(self) -> bool:
        if self._delivery_missing():
            return False
        # qemu-startup / install-windows name no scored files. If wrap-up
        # withholds tools, before_completion (telnet/listen, monitor
        # socket, CPU_ONLY) never runs. Keep tools until those instruction
        # checks pass or retry out.
        if (
            self._listen_retries < _MAX_LISTEN_RETRIES
            and self._listen_unsatisfied()
        ):
            return False
        if (
            self._socket_retries < _MAX_SOCKET_RETRIES
            and self._socket_unsatisfied()
        ):
            return False
        if (
            self._cpu_only_retries < _MAX_CPU_ONLY_RETRIES
            and self._cpu_only_unsatisfied()
        ):
            return False
        return True

    def _delivery_missing(self) -> tuple[str, ...]:
        """Named paths still absent: created outputs must be non-empty;
        instruction-named files that existed at start must still exist."""
        required = set(self._required)
        missing: list[str] = []
        for path in self._named:
            if path in required:
                if not _file_ready(path):
                    missing.append(path)
            elif not Path(path).exists():
                missing.append(path)
        return tuple(missing)

    def _scored_missing(self) -> tuple[str, ...]:
        return tuple(
            p for p in self._delivery_missing() if "." in Path(p).name
        )

    def _output_files(self) -> tuple[str, ...]:
        """Named artifacts to validate: created this turn, else in-place edits."""
        created = tuple(p for p in self._required if Path(p).is_file())
        if created:
            return created
        return tuple(p for p in self._named if Path(p).is_file())

    def _veto_validate(self, message: str, reason: str) -> CompletionAction:
        self._validate_retries += 1
        self._force_tool = True
        return CompletionAction(kind="retry", message=message, reason=reason)

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
        missing = self._delivery_missing()
        named_missing = bool(missing)
        wrapping = any(
            _WRAP_UP_MARKER in (m.content_text or "") for m in transcript
        )
        # Remembered because ``before_completion`` is handed a draft, not the
        # transcript, and the verification gate there must stand down once
        # the budget is short: another round costs more than the check is
        # worth when the alternative is Harbor killing the trial.
        self._wrapping = self._wrapping or wrapping
        if wrapping and missing and self._wrap_up_named_nudges < 1:
            self._wrap_up_named_nudges += 1
            self._force_tool = True
            return PreStepAction(
                kind="proceed",
                appends=[
                    TranscriptAppend(
                        message=LLMMessage.text_of(
                            "user",
                            _WRAP_UP_NAMED.format(
                                paths=", ".join(missing[:8]),
                                first=missing[0],
                            ),
                        ),
                        kind="delivery.wrap_up_named",
                    )
                ],
                reason="wrap_up_named_output",
                tool_choice="required",
                append_action="delivery_nudge",
            )
        if wrapping and self._livelock_nudged:
            return PreStepAction(kind="proceed")
        nudge_limit = (
            _MAX_NAMED_EXPLORE_NUDGES if named_missing else self._max_nudges
        )
        explore_gate = self._explore_before_nudge
        if named_missing and explore_gate > _EXPLORE_BEFORE_NUDGE_NAMED:
            explore_gate = _EXPLORE_BEFORE_NUDGE_NAMED
        if (
            self.writes == 0
            and self.nudges < nudge_limit
            and (
                self.consecutive_explore >= explore_gate
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
        cap = _livelock_empty_streak()
        if wrapping and cap > 0 and tool_choice == "required":
            self._forced_empty_streak += 1
            if self._forced_empty_streak >= cap:
                self._force_tool = False
                self._livelock_nudged = True
                return PreStepAction(
                    kind="proceed",
                    appends=[
                        TranscriptAppend(
                            message=LLMMessage.text_of("user", _LIVELOCK_WRITE_NOW),
                            kind="delivery.forced_empty_livelock",
                        )
                    ],
                    reason="forced_empty_livelock",
                    append_action="delivery_nudge",
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
        self._forced_empty_streak = 0
        name = call.name
        # Ordered before the write branches so a call that both runs and
        # delivers — `python3 gen.py`, `make` — ends up unverified: what it
        # produced is exactly what nothing has been run against yet.
        if (
            name == "bash"
            and result.success
            and _bash_runs_something(_bash_command(call))
        ):
            self.ran_since_write = True
        if self._required:
            delivered = sum(1 for p in self._required if _file_ready(p))
            if delivered > self._delivered:
                self.writes += delivered - self._delivered
                self.consecutive_explore = 0
                self.ran_since_write = False
            elif name in _EXPLORE or name in _MUTATING:
                self.consecutive_explore += 1
            self._delivered = delivered
        elif result.success and (
            name in _MUTATING or (name == "bash" and _bash_writes(call))
        ):
            self.writes += 1
            self.consecutive_explore = 0
            self.ran_since_write = False
        elif name in _EXPLORE:
            self.consecutive_explore += 1
        return result

    def _unverified_retry(self) -> CompletionAction | None:
        """Refuse one completion where nothing ran against the output.

        ``ran_since_write`` is cleared by every landed write and set by every
        bash call that runs anything, so a false here means the delivery on
        disk has had nothing executed against it — whether the turn stopped
        on the write or spent its last calls looking at what it wrote.

        The looking case is the larger one. Over catalog-89 run 33369888461,
        56 trials ended on a non-writing bash: those whose last command ran a
        program passed at 0.727 against 0.533 for the 15 that only viewed the
        output, a gap four times the 0.688-to-0.743 one between stopping on
        the write and taking one more call.

        Classifying commands is what the earlier judgment avoided, on the
        grounds that a pattern would only recognise the checks it was written
        for. ``_BASH_NO_RUN`` escapes that by recognising looking instead of
        checking and treating every unknown word as a run, so the classifier
        errs into standing down rather than into demanding a check of a trial
        that already ran one.

        Reached only after the named-output and cap retries above have
        accepted, so the files exist and satisfy every constraint stated in
        the instruction that this module can evaluate on its own. Stands down
        once wrap-up has been announced, where an extra round risks the
        delivery it would be checking.
        """
        if not _delivery_verify_enabled():
            return None
        if self._wrapping or self._verify_retries >= _MAX_UNVERIFIED_RETRIES:
            return None
        if self.writes == 0 or self.ran_since_write:
            return None
        self._verify_retries += 1
        self._force_tool = True
        return CompletionAction(
            kind="retry",
            message=_UNVERIFIED_RETRY,
            reason="unverified_output",
        )

    def _named_image_size_retry(self) -> CompletionAction | None:
        """Veto a turn whose named BMP/PNG disagrees with source RESX/RESY."""
        if self._size_retries >= _MAX_SIZE_RETRIES:
            return None
        wants = source_resolutions(self._instruction, self._named)
        if not wants:
            return None
        for path in self._output_files():
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

    def _named_bytes_cap_retry(self) -> CompletionAction | None:
        """Veto a turn whose named source is over an instruction `<N bytes` cap."""
        if self._bytes_retries >= _MAX_BYTES_RETRIES:
            return None
        match = _BYTES_CAP.search(self._instruction or "")
        if not match:
            return None
        cap = int(match.group(1))
        if cap < 1:
            return None
        for path in self._output_files():
            suffix = Path(path).suffix.lower()
            if suffix not in _SOURCE_SUFFIXES or not Path(path).is_file():
                continue
            try:
                got = Path(path).stat().st_size
            except OSError:
                continue
            if got < cap:
                continue
            self._bytes_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_BYTES_CAP_RETRY.format(path=path, got=got, cap=cap),
                reason="named_bytes_cap",
            )
        return None

    def _named_mb_cap_retry(self) -> CompletionAction | None:
        """Veto a named file over an instruction `less than N MB` cap."""
        if self._bytes_retries >= _MAX_BYTES_RETRIES:
            return None
        match = _MB_CAP.search(self._instruction or "")
        if not match:
            return None
        cap_mb = int(match.group(1))
        cap = cap_mb * 1024 * 1024
        if cap < 1:
            return None
        for path in self._output_files():
            if not Path(path).is_file():
                continue
            try:
                got = Path(path).stat().st_size
            except OSError:
                continue
            if got < cap:
                continue
            self._bytes_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_MB_CAP_RETRY.format(path=path, got=got, cap=cap_mb),
                reason="named_mb_cap",
            )
        return None

    def _named_shown_text_retry(self) -> CompletionAction | None:
        """Veto a stub or huge named .txt when the instruction asks for print text."""
        if self._bytes_retries >= _MAX_BYTES_RETRIES:
            return None
        if not _ASKS_SHOWN_TEXT.search(self._instruction or ""):
            return None
        for path in self._output_files():
            if Path(path).suffix.lower() != ".txt" or not Path(path).is_file():
                continue
            try:
                body = Path(path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            got = len(body.encode("utf-8"))
            if _SHOWN_TEXT_PLACEHOLDER.match(body.strip()):
                self._bytes_retries += 1
                self._force_tool = True
                return CompletionAction(
                    kind="retry",
                    message=_SHOWN_PLACEHOLDER_RETRY.format(
                        path=path, got=body.strip()
                    ),
                    reason="named_shown_text",
                )
            if got <= _MAX_SHOWN_TEXT_BYTES:
                continue
            self._bytes_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_SHOWN_TEXT_RETRY.format(path=path, got=got),
                reason="named_shown_text",
            )
        return None

    def _named_gzip_cap_retry(self) -> CompletionAction | None:
        """Veto a named source over an instruction `<Nk gzip` compressed cap."""
        if self._bytes_retries >= _MAX_BYTES_RETRIES:
            return None
        text = self._instruction or ""
        if "gzip" not in text.lower():
            return None
        match = _GZIP_K_CAP.search(text)
        if not match:
            return None
        cap = int(match.group(1)) * 1000
        if cap < 1:
            return None
        for path in self._output_files():
            suffix = Path(path).suffix.lower()
            if suffix not in _SOURCE_SUFFIXES or not Path(path).is_file():
                continue
            try:
                raw = Path(path).read_bytes()
            except OSError:
                continue
            got = len(gzip.compress(raw))
            if got < cap:
                continue
            self._bytes_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_GZIP_CAP_RETRY.format(path=path, got=got, cap=cap),
                reason="named_gzip_cap",
            )
        return None

    def _named_json_blank_retry(self) -> CompletionAction | None:
        """Veto JSON whose strings would become empty rows under split('\\n')."""
        if self._json_retries >= _MAX_JSON_RETRIES:
            return None
        if not _SPLIT_NEWLINES.search(self._instruction or ""):
            return None
        for path in self._output_files():
            if Path(path).suffix.lower() != ".json" or not Path(path).is_file():
                continue
            try:
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not _json_has_blank_split_row(payload):
                continue
            self._json_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_JSON_BLANK_RETRY.format(path=path),
                reason="named_json_blank",
            )
        return None

    def _named_artifact_retry(self) -> CompletionAction | None:
        """Veto empty, non-UTF-8, or unparseable named artifacts."""
        if self._validate_retries >= _MAX_VALIDATE_RETRIES:
            return None
        required = set(self._required)
        for path in self._output_files():
            file = Path(path)
            try:
                size = file.stat().st_size
            except OSError:
                continue
            if size == 0:
                return self._veto_validate(
                    _EMPTY_RETRY.format(path=path), "named_empty"
                )
            suffix = file.suffix.lower()
            if suffix in _SOURCE_SUFFIXES:
                try:
                    file.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    return self._veto_validate(
                        _UTF8_RETRY.format(path=path), "named_utf8"
                    )
                except OSError:
                    continue
            if (
                suffix == ".py"
                and file.name.lower() not in _CHECKER_FILENAMES
            ):
                fail = _syntax_check_python(file)
                if fail:
                    return self._veto_validate(
                        _SYNTAX_RETRY.format(path=path, output=fail),
                        "named_syntax",
                    )
            if suffix == ".js" and path in required:
                fail = _syntax_check_js(file)
                if fail:
                    return self._veto_validate(
                        _SYNTAX_RETRY.format(path=path, output=fail),
                        "named_syntax",
                    )
        return None

    def _workspace_checkers(self) -> list[Path]:
        """Agent-visible check.py, plus instruction-named eval.py.

        Skip helpers the agent still has to write, and skip hidden ``/tests``.
        """
        names: list[str] = ["check.py"]
        seen_names = {"check.py"}
        for match in _CHECKER_NAME.finditer(self._instruction or ""):
            name = match.group(1)
            key = name.lower()
            if key not in seen_names:
                names.append(name)
                seen_names.add(key)
        required: set[Path] = set()
        for path in self._required:
            try:
                required.add(Path(path).resolve())
            except OSError:
                required.add(Path(path))
        roots = [Path("/app")]
        for path in (*self._named, *self._required):
            parent = Path(path).parent
            if parent not in roots:
                roots.append(parent)
        found: list[Path] = []
        seen: set[Path] = set()
        for name in names:
            for root in roots:
                candidate = root / name
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if resolved in seen or not candidate.is_file():
                    continue
                if resolved in required:
                    continue
                posix = resolved.as_posix()
                if posix == "/tests" or posix.startswith("/tests/"):
                    continue
                seen.add(resolved)
                found.append(candidate)
        return found

    def _named_checker_retry(self) -> CompletionAction | None:
        """Run workspace check.py / instruction-named eval.py; veto nonzero."""
        if self._check_retries >= _MAX_CHECKER_RUNS:
            return None
        if any(not Path(path).exists() for path in self._required):
            return None
        checkers = self._workspace_checkers()
        if not checkers:
            return None
        python = shutil.which("python3") or shutil.which("python")
        if python is None:
            self._check_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_CHECKER_RETRY.format(name=checkers[0].name),
                reason="named_checker",
            )
        for checker in checkers:
            code, output = _run_cmd(
                [python, str(checker)],
                cwd=str(checker.parent),
                timeout=_CHECKER_TIMEOUT_SEC,
            )
            if code == 0:
                continue
            self._check_retries += 1
            self._force_tool = True
            name = checker.name
            if code is None:
                message = _CHECKER_TIMEOUT.format(
                    name=name, sec=_CHECKER_TIMEOUT_SEC
                )
            else:
                message = _CHECKER_FAIL.format(
                    name=name, code=code, output=output
                )
            return CompletionAction(
                kind="retry",
                message=message,
                reason="named_checker",
            )
        return None

    def _run_named_make(self, missing: tuple[str, ...]) -> CompletionAction | None:
        """Run ``make`` once when a named ELF/binary is missing and a Makefile exists."""
        if self._make_runs >= _MAX_MAKE_RUNS or not missing:
            return None
        targets = _make_targets(missing)
        if not targets:
            return None
        makefile = _find_makefile((*self._named, *self._required))
        if makefile is None:
            return None
        binary = shutil.which("make")
        if binary is None:
            return None
        self._make_runs += 1
        code, output = _run_cmd(
            [binary, "-C", str(makefile.parent)],
            cwd=str(makefile.parent),
            timeout=_MAKE_TIMEOUT_SEC,
        )
        _promote_make_artifacts(makefile.parent, targets)
        still = tuple(p for p in self._required if not _file_ready(p))
        still_targets = _make_targets(still)
        if not still_targets:
            return None
        listed = ", ".join(still_targets[:8])
        cmd = f"make -C {makefile.parent}"
        if still_targets and code == 0:
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_ENTRY_STILL_MISSING.format(cmd=cmd, paths=listed),
                reason="named_make",
            )
        if still and code is None:
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_ENTRY_TIMEOUT.format(
                    cmd=cmd, sec=_MAKE_TIMEOUT_SEC, paths=listed
                ),
                reason="named_make",
            )
        if still:
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_ENTRY_FAIL.format(
                    cmd=cmd, code=code, output=output, paths=listed
                ),
                reason="named_make",
            )
        return None

    def _run_named_entrypoint(self, missing: tuple[str, ...]) -> CompletionAction | None:
        """Run a named node/python command once if it can write missing outputs.

        Prefers instruction backticks (``python3 helper.py``) and
        instruction ``gcc … file.c && ./binary`` compile-and-run lines. If
        those are absent, a named ``.py``/``.js`` that already exists is run
        when a named side-effect file (npy/txt/bmp/ppm/…) is still missing.
        """
        if self._entry_runs >= _MAX_ENTRY_RUNS or not missing:
            return None
        pairs: list[tuple[str, Path]] = []
        for command in (
            match.group(1)
            for pattern in (_RUN_COMMAND, _COMPILE_AND_RUN)
            for match in pattern.finditer(self._instruction or "")
        ):
            script = _resolve_run_script(command, (*self._named, *self._required))
            if script is None:
                continue
            if script.name.lower() in _CHECKER_FILENAMES:
                continue
            pairs.append((command, script))
        if not pairs:
            pairs.extend(
                _named_side_effect_scripts((*self._named, *self._required), missing)
            )
        for command, script in pairs:
            if _entrypoint_needs_missing_input(missing, script):
                continue
            argv = _run_command_argv(command, script)
            if argv is None:
                continue
            self._entry_runs += 1
            code, output = _run_cmd(
                argv,
                cwd=str(script.parent),
                timeout=_ENTRY_TIMEOUT_SEC,
            )
            still = tuple(p for p in self._required if not _file_ready(p))
            listed = ", ".join((still or missing)[:8])
            if still and code == 0:
                self._force_tool = True
                return CompletionAction(
                    kind="retry",
                    message=_ENTRY_STILL_MISSING.format(cmd=command, paths=listed),
                    reason="named_entrypoint",
                )
            if still and code is None:
                self._force_tool = True
                return CompletionAction(
                    kind="retry",
                    message=_ENTRY_TIMEOUT.format(
                        cmd=command, sec=_ENTRY_TIMEOUT_SEC, paths=listed
                    ),
                    reason="named_entrypoint",
                )
            if still:
                self._force_tool = True
                return CompletionAction(
                    kind="retry",
                    message=_ENTRY_FAIL.format(
                        cmd=command,
                        code=code,
                        output=output,
                        paths=listed,
                    ),
                    reason="named_entrypoint",
                )
            return None
        return None

    def _listen_unsatisfied(self) -> bool:
        for host, port, kind in _instruction_listen_targets(self._instruction):
            if kind == "telnet":
                if not _telnet_port_ready(host, port):
                    return True
            elif not _tcp_accepts(host, port):
                return True
        return False

    def _socket_unsatisfied(self) -> bool:
        return any(not _socket_ready(path) for path in self._sockets)

    def _instruction_socket_retry(self) -> CompletionAction | None:
        if self._socket_retries >= _MAX_SOCKET_RETRIES:
            return None
        for path in self._sockets:
            if _socket_ready(path):
                continue
            self._socket_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_SOCKET_RETRY.format(path=path),
                reason="instruction_socket",
            )
        return None

    def _cpu_only_failing_path(self) -> Path | None:
        if not _CPU_ONLY_INSTRUCTION.search(self._instruction or ""):
            return None
        makes, caches = _cpu_only_meta_files()
        if makes:
            for path in makes:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "CPU_ONLY:=1" not in re.sub(r"\s+", "", text):
                    return path
            return None
        for path in caches:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "CPU_ONLY:BOOL=ON" not in text:
                return path
        return None

    def _cpu_only_unsatisfied(self) -> bool:
        return self._cpu_only_failing_path() is not None

    def _instruction_listen_retry(self) -> CompletionAction | None:
        if self._listen_retries >= _MAX_LISTEN_RETRIES:
            return None
        for host, port, kind in _instruction_listen_targets(self._instruction):
            if kind == "telnet":
                status, comm = _telnet_status(host, port)
                if status == "ready":
                    continue
                self._listen_retries += 1
                self._force_tool = True
                if status == "proxy":
                    message = _TELNET_PROXY_RETRY.format(
                        host=host, port=port, comm=comm or "unknown"
                    )
                else:
                    message = _TELNET_RETRY.format(host=host, port=port)
                return CompletionAction(
                    kind="retry",
                    message=message,
                    reason="instruction_listen",
                )
            if _tcp_accepts(host, port):
                continue
            self._listen_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_LISTEN_RETRY.format(port=port),
                reason="instruction_listen",
            )
        return None

    def _instruction_cpu_only_retry(self) -> CompletionAction | None:
        if self._cpu_only_retries >= _MAX_CPU_ONLY_RETRIES:
            return None
        path = self._cpu_only_failing_path()
        if path is None:
            return None
        self._cpu_only_retries += 1
        self._force_tool = True
        return CompletionAction(
            kind="retry",
            message=_CPU_ONLY_RETRY.format(path=path),
            reason="instruction_cpu_only",
        )

    async def before_completion(
        self, draft: CompletionDraft, ctx: LoopContext
    ) -> CompletionAction:
        # Named paths beat empty-round: gcode-to-text wrap-up sent
        # reasoning-only completions while /app/out.txt was still missing,
        # and empty_round consumed the retries that should have named it.
        unready = tuple(p for p in self._required if not _file_ready(p))
        if unready:
            ran = self._run_named_make(unready)
            if ran is not None:
                return ran
            unready = tuple(p for p in self._required if not _file_ready(p))
        if unready:
            ran = self._run_named_entrypoint(unready)
            if ran is not None:
                return ran
        missing = tuple(p for p in self._named if not Path(p).exists())
        if missing and self.completion_retries < _MAX_MISSING_NAMED_RETRIES:
            self.completion_retries += 1
            self._force_tool = True
            listed = ", ".join(missing[:8])
            return CompletionAction(
                kind="retry",
                message=_MISSING_NAMED_RETRY.format(
                    paths=listed, first=missing[0]
                ),
                reason="missing_named_output",
            )
        size_retry = self._named_image_size_retry()
        if size_retry is not None:
            return size_retry
        bytes_retry = self._named_bytes_cap_retry()
        if bytes_retry is not None:
            return bytes_retry
        mb_retry = self._named_mb_cap_retry()
        if mb_retry is not None:
            return mb_retry
        gzip_retry = self._named_gzip_cap_retry()
        if gzip_retry is not None:
            return gzip_retry
        json_retry = self._named_json_blank_retry()
        if json_retry is not None:
            return json_retry
        shown_retry = self._named_shown_text_retry()
        if shown_retry is not None:
            return shown_retry
        artifact_retry = self._named_artifact_retry()
        if artifact_retry is not None:
            return artifact_retry
        check_retry = self._named_checker_retry()
        if check_retry is not None:
            return check_retry
        listen_retry = self._instruction_listen_retry()
        if listen_retry is not None:
            return listen_retry
        socket_retry = self._instruction_socket_retry()
        if socket_retry is not None:
            return socket_retry
        cpu_retry = self._instruction_cpu_only_retry()
        if cpu_retry is not None:
            return cpu_retry
        empty = not (draft.content or "").strip() and not draft.had_tool_calls
        if empty and self.empty_round_retries < self._max_empty_round_retries:
            self.empty_round_retries += 1
            self._force_tool = True
            return CompletionAction(
                kind="retry",
                message=_EMPTY_ROUND_RETRY,
                reason="empty_round",
            )
        unverified = self._unverified_retry()
        if unverified is not None:
            return unverified
        if (
            not self._named
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
        if Path(path).suffix.lower() in _SOCKET_SUFFIXES:
            continue
        if path not in seen:
            seen.append(path)
    names: list[str] = []
    for pattern in (
        _CALLED_WITH_EXT,
        _FILE_CALLED,
        _RUN_ENTRY,
        _TITLED_FILE,
        _WRITE_A_FILE,
        _WRITE_TO_FILE,
        _NEW_FILE,
        _ASSERTS_EXISTS,
        _CONFIRMS_CREATED,
    ):
        names.extend(match.group(1) for match in pattern.finditer(text))
    for match in _OUTPUT_FILE_LIST.finditer(text):
        names.extend(_BACKTICKED_FILE.findall(match.group(1)))
    for raw_name in names:
        name = raw_name.rstrip(".,;:)")
        path = name if name.startswith("/") else f"/app/{name.lstrip('./')}"
        if Path(path).suffix.lower() in _SOCKET_SUFFIXES:
            continue
        if path not in seen:
            seen.append(path)
    return tuple(seen)


def named_socket_paths(instruction: str) -> tuple[str, ...]:
    """Absolute UNIX-socket paths named in a TB instruction."""
    seen: list[str] = []
    for match in _NAMED_OUTPUT_PATH.finditer(instruction or ""):
        path = match.group(1).rstrip(".,;:)")
        if Path(path).suffix.lower() not in _SOCKET_SUFFIXES:
            continue
        if path not in seen:
            seen.append(path)
    return tuple(seen)


def _run_cmd(
    argv: list[str], *, cwd: str | None = None, timeout: int
) -> tuple[int | None, str]:
    """Return ``(exit_code, tail)``; ``exit_code is None`` on timeout."""
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, ""
    except OSError as exc:
        return 1, str(exc)
    blob = (completed.stderr or "") + (completed.stdout or "")
    tail = blob[-1500:] if blob else "(no output)"
    return completed.returncode, tail


def _syntax_check_python(path: Path) -> str:
    python = shutil.which("python3") or shutil.which("python")
    if python is None:
        return ""
    code, output = _run_cmd(
        [python, "-m", "py_compile", str(path)],
        cwd=str(path.parent),
        timeout=_SYNTAX_TIMEOUT_SEC,
    )
    if code == 0:
        return ""
    if code is None:
        return f"py_compile timed out after {_SYNTAX_TIMEOUT_SEC}s"
    return output or f"py_compile exited {code}"


def _syntax_check_js(path: Path) -> str:
    node = shutil.which("node")
    if node is None:
        return ""
    code, output = _run_cmd(
        [node, "--check", str(path)],
        cwd=str(path.parent),
        timeout=_SYNTAX_TIMEOUT_SEC,
    )
    if code == 0:
        return ""
    if code is None:
        return f"node --check timed out after {_SYNTAX_TIMEOUT_SEC}s"
    return output or f"node --check exited {code}"


def _resolve_run_script(
    command: str, named: tuple[str, ...] = ()
) -> Path | None:
    parts = command.replace("&&", " ").split()
    if len(parts) < 2:
        return None
    raw = parts[1]
    for tok in parts[1:]:
        if tok.startswith("-"):
            continue
        if "." in Path(tok).name:
            raw = tok
            break
    name = Path(raw).name
    candidates = [Path(raw)]
    if not raw.startswith("/"):
        candidates.append(Path("/app") / raw)
        candidates.append(Path(raw.lstrip("./")))
    for path in named:
        candidates.append(Path(path).parent / name)
    for path in candidates:
        if path.is_file():
            return path
    return None


def _run_command_argv(command: str, script: Path) -> list[str] | None:
    interpreter = command.split()[0]
    if interpreter in {"gcc", "cc", "clang"} or "&&" in command:
        bash = shutil.which("bash")
        if bash is None:
            return None
        return [bash, "-lc", command]
    if interpreter.startswith("python") or interpreter.startswith("pypy"):
        binary = shutil.which("python3") or shutil.which("python")
    else:
        binary = shutil.which(interpreter)
    if binary is None:
        return None
    return [binary, str(script)]


def _entrypoint_needs_missing_input(
    missing: tuple[str, ...], script: Path
) -> bool:
    """True when a named sibling the command needs (ELF, other source) is gone."""
    try:
        script_resolved = script.resolve()
    except OSError:
        script_resolved = script
    for path in missing:
        candidate = Path(path)
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved == script_resolved:
            continue
        if candidate.suffix.lower() in _SIDE_EFFECT_SUFFIXES:
            continue
        return True
    return False


def _named_side_effect_scripts(
    named: tuple[str, ...], missing: tuple[str, ...]
) -> list[tuple[str, Path]]:
    """Named ``.py``/``.js`` to run when a named side-effect file is still missing."""
    if not any(Path(path).suffix.lower() in _SIDE_EFFECT_SUFFIXES for path in missing):
        return []
    pairs: list[tuple[str, Path]] = []
    for raw in named:
        path = Path(raw)
        suffix = path.suffix.lower()
        if suffix not in {".py", ".js"}:
            continue
        if path.name.lower() in _CHECKER_FILENAMES:
            continue
        if not _file_ready(raw):
            continue
        interp = "python3" if suffix == ".py" else "node"
        pairs.append((f"{interp} {path.name}", path))
    return pairs


def _json_has_blank_split_row(value: object) -> bool:
    """True when a JSON string is empty or contains a newline."""
    if isinstance(value, str):
        return (not value) or ("\n" in value)
    if isinstance(value, list):
        return any(_json_has_blank_split_row(item) for item in value)
    if isinstance(value, dict):
        return any(_json_has_blank_split_row(item) for item in value.values())
    return False


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


_MAKEFILE_WALK_DEPTH = 2
_MAKEFILE_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__"})


def _child_dirs(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    try:
        children = list(path.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for child in children:
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith(".") or name in _MAKEFILE_SKIP_DIRS:
            continue
        out.append(child)
    return out


def _makefiles_at_depth(root: Path, depth: int) -> list[Path]:
    dirs = [root]
    for _ in range(depth):
        nxt: list[Path] = []
        for directory in dirs:
            nxt.extend(_child_dirs(directory))
        dirs = nxt
    found: list[Path] = []
    for directory in dirs:
        found.append(directory / "Makefile")
        found.append(directory / "makefile")
    return found


def _find_makefile(named: tuple[str, ...]) -> Path | None:
    """Makefile next to a named output, or two levels under that parent.

    make-doom / make-mips ship ``/app/doomgeneric/doomgeneric/Makefile``.
    A one-level ``/app/*/Makefile`` walk misses it. Closer Makefiles win.
    Do not walk ``/app`` unless a named path lives there.
    """
    candidates: list[Path] = []
    seen: set[Path] = set()
    roots: list[Path] = []
    seen_roots: set[Path] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(path)

    def add_root(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen_roots:
            return
        seen_roots.add(resolved)
        roots.append(path)

    for raw in named:
        parent = Path(raw).parent
        add_root(parent)
        posix = parent.as_posix()
        if posix == "/app" or posix.startswith("/app/"):
            add_root(Path("/app"))
    for depth in range(_MAKEFILE_WALK_DEPTH + 1):
        for root in roots:
            for makefile in _makefiles_at_depth(root, depth):
                add(makefile)
    for path in candidates:
        if path.is_file():
            return path
    return None


def _make_targets(missing: tuple[str, ...]) -> tuple[str, ...]:
    """Named ELFs/binaries ``make`` can produce; not sources or side-effect files."""
    return tuple(
        path
        for path in missing
        if Path(path).suffix.lower() not in _SIDE_EFFECT_SUFFIXES
        and Path(path).suffix.lower() not in _SOURCE_SUFFIXES
    )


def _promote_make_artifacts(make_dir: Path, missing: tuple[str, ...]) -> None:
    """Copy a named basename make wrote beside the Makefile to the named path.

    ``make -C doomgeneric/doomgeneric`` leaves ``doomgeneric_mips`` in that
    directory while the instruction names ``/app/doomgeneric_mips``.
    """
    for raw in missing:
        dest = Path(raw)
        name = dest.name
        if not name or dest.suffix.lower() in _SIDE_EFFECT_SUFFIXES:
            continue
        if _file_ready(raw):
            continue
        src = make_dir / name
        try:
            if not src.is_file():
                continue
            if src.resolve() == dest.resolve():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        except OSError:
            continue


def _file_ready(path: str) -> bool:
    """True when a named output exists and has at least one byte."""
    file = Path(path)
    try:
        return file.is_file() and file.stat().st_size > 0
    except OSError:
        return False


def _socket_ready(path: str) -> bool:
    try:
        mode = Path(path).lstat().st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode)


def _instruction_listen_targets(
    instruction: str,
) -> tuple[tuple[str, int, str], ...]:
    """Instruction-named TCP checks: ``(host, port, telnet|listen)``."""
    by_addr: dict[tuple[str, int], str] = {}
    for match in _TELNET_ADDR.finditer(instruction or ""):
        port = int(match.group(2))
        if 1 <= port <= 65535:
            by_addr[(match.group(1), port)] = "telnet"
    for match in _NGINX_PORT.finditer(instruction or ""):
        port = int(match.group(1))
        addr = ("127.0.0.1", port)
        if 1 <= port <= 65535 and addr not in by_addr:
            by_addr[addr] = "listen"
    for match in _LISTENING_PORT.finditer(instruction or ""):
        port = int(match.group(1))
        addr = ("127.0.0.1", port)
        if 1 <= port <= 65535 and addr not in by_addr:
            by_addr[addr] = "listen"
    return tuple((host, port, kind) for (host, port), kind in by_addr.items())


def _tcp_accepts(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _proc_tcp_listening(port: int) -> bool:
    return bool(_proc_tcp_listen_inodes(port))


def _proc_tcp_listen_inodes(port: int) -> tuple[str, ...]:
    needle = f"{port:04X}"
    inodes: list[str] = []
    for path in (_PROC_TCP, _PROC_TCP6):
        try:
            text = path.read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != _LISTEN_ST:
                continue
            local = parts[1]
            if local.rsplit(":", 1)[-1].upper() == needle:
                inodes.append(parts[9])
    return tuple(inodes)


def _is_script_listener(comm: str) -> bool:
    base = Path(comm).name
    if _SCRIPT_LISTEN_COMM.match(base):
        return True
    return base.lower().startswith("python")


def _tcp_listen_comm(port: int) -> str | None:
    inodes = _proc_tcp_listen_inodes(port)
    if not inodes:
        return None
    want = {f"socket:[{inode}]" for inode in inodes}
    try:
        # iterdir() is lazy: the OSError (containerized /proc denies pid 1)
        # raises on iteration, so materialize inside the try.
        pid_dirs = list(Path("/proc").iterdir())
    except OSError:
        return None
    for pid_dir in pid_dirs:
        if not pid_dir.name.isdigit():
            continue
        fd_dir = pid_dir / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target not in want:
                continue
            try:
                return (pid_dir / "comm").read_text(encoding="ascii").strip()
            except OSError:
                return None
    return None


def _telnet_status(host: str, port: int) -> tuple[str, str | None]:
    """``ready`` / ``closed`` / ``proxy``. Never connect on Linux."""
    if _PROC_TCP.is_file() or _PROC_TCP6.is_file():
        if not _proc_tcp_listening(port):
            return "closed", None
        comm = _tcp_listen_comm(port)
        if comm is not None and _is_script_listener(comm):
            return "proxy", comm
        return "ready", comm
    if _tcp_accepts(host, port):
        return "ready", None
    return "closed", None


def _telnet_port_ready(host: str, port: int) -> bool:
    """True if the VM serial (not a userspace proxy) is listening.

    Never connect on Linux: QEMU serial telnet is one client, and a probe
    steals boot output from the verifier.
    """
    status, _comm = _telnet_status(host, port)
    return status == "ready"


def _cpu_only_meta_files() -> tuple[list[Path], list[Path]]:
    """Makefile.config and CMakeCache.txt under cwd and /app, depth 2."""
    makes: list[Path] = []
    caches: list[Path] = []
    seen: set[Path] = set()
    roots: list[Path] = []
    cwd = Path.cwd()
    if cwd.is_dir():
        roots.append(cwd)
    app = Path("/app")
    try:
        if app.is_dir() and app.resolve() != cwd.resolve():
            roots.append(app)
    except OSError:
        if app.is_dir():
            roots.append(app)

    def rec(current: Path, depth: int) -> None:
        if depth > 2:
            return
        try:
            children = list(current.iterdir())
        except OSError:
            return
        for child in children:
            try:
                if child.is_symlink():
                    continue
                if child.is_file() and child.name in {
                    "Makefile.config",
                    "CMakeCache.txt",
                }:
                    resolved = child.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    if child.name == "Makefile.config":
                        makes.append(child)
                    else:
                        caches.append(child)
                elif child.is_dir() and child.name not in {".git", "node_modules"}:
                    rec(child, depth + 1)
            except OSError:
                continue

    for root in roots:
        rec(root, 0)
    return makes, caches


def _read_file_on_disk(call: ToolCall) -> bool:
    """True when read_file targets a path that already exists.

    Wrap-up named used to block every read_file, so make-mips could not
    open doomgeneric_img.c after the first idle cut.
    """
    args = call.arguments or {}
    raw = str(args.get("path") or args.get("file") or "")
    if not raw:
        return False
    try:
        return Path(raw).is_file()
    except OSError:
        return False


def _bash_reads_existing(command: str) -> bool:
    """True for ``cat``/``head`` of a single path that already exists."""
    match = _BASH_VIEW_FILE.match(command.strip())
    if not match:
        return False
    raw = match.group(1).strip("'\"")
    if not raw or raw.startswith("-"):
        return False
    try:
        return Path(raw).is_file()
    except OSError:
        return False


def _bash_command(call: ToolCall) -> str:
    args = call.arguments or {}
    return str(args.get("command") or args.get("cmd") or args.get("script") or "")


def _bash_writes(call: ToolCall) -> bool:
    return bool(_BASH_WRITES.search(_bash_command(call)))


def _bash_runs_something(command: str) -> bool:
    """True unless every segment of the command is drawn from ``_BASH_NO_RUN``.

    Segments are split on the shell's sequencing and pipe operators, leading
    environment assignments are dropped, and command substitution anywhere in
    a segment counts as running because the substituted command is the one
    that would execute. Splitting ignores quoting, so a quoted operator
    yields a fragment whose first word is unrecognised — running, which is
    the direction that silences the caller's gate rather than tripping it.
    """
    for segment in _SHELL_SPLIT.split(command):
        segment = segment.strip().lstrip("({")
        if not segment:
            continue
        if "$(" in segment or "`" in segment:
            return True
        words = segment.split()
        while words and _ENV_ASSIGN.match(words[0]):
            words.pop(0)
        if not words:
            continue
        if words[0].strip("'\"") not in _BASH_NO_RUN:
            return True
    return False


def _bash_delivers_required(
    call: ToolCall,
    missing: tuple[str, ...],
    named: tuple[str, ...] | None = None,
) -> bool:
    """True when bash compiles, runs node/named .py, or mutates a missing output."""
    command = _bash_command(call)
    if _BASH_BUILDS.search(command) or _BASH_RUN_ENTRYPOINT.search(command):
        return True
    if named and _bash_runs_existing_named_script(command, named):
        return True
    if missing and _bash_runs_helper_writing_missing(command, missing):
        return True
    if missing and any(path in command for path in missing):
        return bool(_BASH_MUTATE_FILE.search(command))
    return False


def _bash_runs_existing_named_script(command: str, named: tuple[str, ...]) -> bool:
    match = _BASH_RUN_PYTHON.search(command)
    if not match:
        return False
    raw = match.group(2)
    candidates = [raw]
    if not raw.startswith("/"):
        candidates.append(f"/app/{raw.lstrip('./')}")
    names = {Path(path).name: path for path in named}
    for cand in candidates:
        path = cand if cand in named else names.get(Path(cand).name)
        if path is not None and Path(path).is_file():
            return True
    return False


def _python_script_paths(command: str) -> list[str]:
    match = _BASH_RUN_PYTHON.search(command)
    if not match:
        return []
    raw = match.group(2)
    out = [raw]
    if not raw.startswith("/"):
        out.append(f"/app/{raw.lstrip('./')}")
    return out


def _bash_runs_helper_writing_missing(
    command: str, missing: tuple[str, ...]
) -> bool:
    """True when python3 foo.py's source writes a still-missing named output."""
    for cand in _python_script_paths(command):
        path = Path(cand)
        if not path.is_file():
            continue
        try:
            src = path.read_text(encoding="utf-8", errors="replace")[:64_000]
        except OSError:
            continue
        if any(item in src for item in missing) and _BASH_MUTATE_FILE.search(src):
            return True
    return False
