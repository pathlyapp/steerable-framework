"""Deterministic compaction-strategy simulator.

One task run = a scripted session against an algorithmic policy provider:

1. The user says "go". Round i: the policy issues tool call ``emit(n=i)``
   and the tool returns a large deterministic blob with planted *needles*
   (``NDL-<idx>:<secret>`` lines). The policy also emits a fixed-size
   narration so pressure comes from both tool results (foldable) and
   assistant content (not foldable).
2. After ``rounds`` tool results the policy writes a manifest of every
   needle it can still see in the request — the stand-in for a real model's
   post-compaction recall.
3. The simulated provider enforces the context window: a request whose
   estimate exceeds ``window_tokens`` raises ``context_overflow``.

The policy is strategy-blind: it only ever reads the content of the
incoming request. It never sees hook configuration, so every metric
difference between arms comes from the compaction strategy alone.

Metrics per arm (see standard.md): task success (exact recall), needle
recall, compactions, overflow errors, total prompt tokens, and the
byte-prefix cache-hit ratio between consecutive requests.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import Any

from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime import (
    CompactionHooks,
    CoreLoop,
    NoopHooks,
    RouterToolExecutor,
    ToolRouter,
    estimate_tokens,
)
from steerable_agent_runtime.hooks import PreStepAction, RewriteRequest
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk, LLMUsage
from steerable_agent_runtime.llm.errors import LLMError

#: Needle line format planted in blobs and expected in the manifest.
NEEDLE_RE = re.compile(r"NDL-(\d+):([0-9a-f]{8})")

_FILLER_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789 \n"


@dataclass(frozen=True)
class TaskConfig:
    """One scripted session shape. ``window_tokens`` is the simulated
    provider's hard context window."""

    name: str
    rounds: int
    blob_chars: int
    needles_per_blob: int
    narration_chars: int
    window_tokens: int
    seed: int


#: The suite. Narration dominates blobs so every compaction reaches the
#: summarize stage (fold alone cannot relieve the pressure) — the suite
#: measures summary fidelity, not just fold excerpts. (A blob-dominated
#: shape exercises the fold stage instead; that shape surfaces the known
#: fold-excerpt loss tracked for the P3 policy work, so it lives in the
#: report, not in the discrimination self-checks.)
TASKS: dict[str, TaskConfig] = {
    "light": TaskConfig(
        name="light",
        rounds=12,
        blob_chars=400,
        needles_per_blob=2,
        narration_chars=6_000,
        window_tokens=8_000,
        seed=1,
    ),
    "heavy": TaskConfig(
        name="heavy",
        rounds=30,
        blob_chars=400,
        needles_per_blob=3,
        narration_chars=6_000,
        window_tokens=16_000,
        seed=7,
    ),
}


def _needle_secret(seed: int, idx: int) -> str:
    return hashlib.sha1(f"{seed}:{idx}".encode()).hexdigest()[:8]


def needle_line(seed: int, idx: int) -> str:
    return f"NDL-{idx}:{_needle_secret(seed, idx)}"


def expected_needles(task: TaskConfig) -> dict[int, str]:
    """Every needle the task plants: ``{idx: secret}``."""
    return {
        idx: _needle_secret(task.seed, idx)
        for idx in range(task.rounds * task.needles_per_blob)
    }


def make_blob(task: TaskConfig, round_idx: int) -> str:
    """Deterministic filler with this round's needles on their own lines,
    evenly spaced. Same ``(seed, round_idx)`` always yields the same blob.
    """
    rng = random.Random(task.seed * 10_000 + round_idx)
    chars = [rng.choice(_FILLER_ALPHABET) for _ in range(task.blob_chars)]
    base = round_idx * task.needles_per_blob
    for k in range(task.needles_per_blob):
        line = needle_line(task.seed, base + k)
        pos = (k + 1) * task.blob_chars // (task.needles_per_blob + 1)
        chars[pos : pos + len(line)] = list(line)
    return "".join(chars)


def scan_needles(text: str) -> dict[int, str]:
    """All needles visible in ``text``: ``{idx: secret}``."""
    return {int(m.group(1)): m.group(2) for m in NEEDLE_RE.finditer(text)}


class PolicyProvider:
    """Algorithmic stand-in for an LLM, driven only by request content.

    - Issues ``emit(n)`` tool calls until the request shows round ``rounds``
      has been issued (max n over assistant tool calls — survives middle
      rewrites because the kept tail carries the latest rounds).
    - Then writes the manifest of every needle visible in the request.
    - Enforces the simulated window: over-estimate requests raise
      ``context_overflow`` exactly like a provider 400.
    """

    name = "sim"
    model = "sim-model"

    def __init__(self, task: TaskConfig) -> None:
        self._task = task
        self.requests: list[list[LLMMessage]] = []
        self.overflows = 0
        self.prompt_tokens_total = 0
        self.final_content = ""

    async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
        raise NotImplementedError

    def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
        request = list(messages)
        self.requests.append(request)
        est = estimate_tokens(request)
        task = self._task

        async def _gen() -> AsyncIterator[LLMStreamChunk]:
            if est > task.window_tokens:
                self.overflows += 1
                raise LLMError(
                    f"simulated context overflow: {est} > {task.window_tokens}",
                    kind="context_overflow",
                    status_code=400,
                )
            self.prompt_tokens_total += est
            issued = 0
            for m in request:
                for call in m.tool_calls or ():
                    if call.name == "emit":
                        issued = max(issued, int((call.arguments or {}).get("n", 0)))
            if issued < task.rounds:
                n = issued + 1
                narration = f"round {n} notes: " + ("r" * task.narration_chars)
                if narration:
                    yield LLMStreamChunk(content_delta=narration)
                yield LLMStreamChunk(
                    tool_call_delta=ToolCall(
                        id=f"call-{n}", name="emit", arguments={"n": n}
                    )
                )
                yield LLMStreamChunk(
                    finish_reason="tool_calls",
                    usage=LLMUsage(
                        prompt_tokens=est,
                        completion_tokens=est // 10,
                        total_tokens=est + est // 10,
                    ),
                )
            else:
                found = scan_needles(
                    "\n".join(m.content_text for m in request)
                )
                manifest = "\n".join(
                    f"NDL-{idx}:{found[idx]}" for idx in sorted(found)
                )
                self.final_content = f"MANIFEST\n{manifest}"
                yield LLMStreamChunk(content_delta=self.final_content)
                yield LLMStreamChunk(
                    finish_reason="stop",
                    usage=LLMUsage(
                        prompt_tokens=est,
                        completion_tokens=est // 10,
                        total_tokens=est + est // 10,
                    ),
                )

        return _gen()


class ExtractiveSummarizer:
    """Best-case stand-in for an LLM summary on the needle dimension:
    copies every needle line from the request into the summary verbatim.
    Records its requests for prefix-replay assertions.
    """

    name = "sim-summarizer"
    model = "sim-summarizer-model"

    def __init__(self) -> None:
        self.calls: list[list[LLMMessage]] = []

    async def complete(self, messages, *, cache_retention=None, **kw):
        self.calls.append(list(messages))
        found = scan_needles("\n".join(m.content_text for m in messages))
        lines = [f"NDL-{idx}:{found[idx]}" for idx in sorted(found)]
        text = "Summary of earlier work:\n" + ("\n".join(lines) or "(none)")
        return LLMMessage.text_of("assistant", text), None


class NaiveTruncateHooks(NoopHooks):
    """Strawman: over threshold, drop the middle span behind a marker —
    no summary, no fold excerpt, no orphan widening."""

    def __init__(self, *, max_context_tokens: int, threshold_ratio: float = 0.8,
                 keep_last: int = 6) -> None:
        self._max = max_context_tokens
        self._threshold = threshold_ratio
        self._keep_last = keep_last
        self.compactions = 0

    async def pre_step(self, transcript, ctx) -> PreStepAction:
        est = estimate_tokens(transcript)
        if est < self._threshold * self._max or len(transcript) <= self._keep_last + 1:
            return PreStepAction(kind="proceed")
        head = transcript[:1]
        tail = transcript[-self._keep_last :]
        dropped = len(transcript) - len(head) - len(tail)
        marker = LLMMessage.text_of("user", f"[naive: {dropped} messages dropped]")
        messages = [*head, marker, *tail]
        self.compactions += 1
        return PreStepAction(
            kind="proceed",
            rewrite=RewriteRequest(
                messages=messages,
                reason="naive truncate middle",
                action="compact",
                pre_tokens=est,
                post_tokens=estimate_tokens(messages),
            ),
        )


@dataclass
class ArmReport:
    """One arm's metrics on one task (see standard.md for definitions)."""

    task: str
    arm: str
    success: bool
    needle_recall: float
    needles_found: int
    needles_expected: int
    compactions: int
    overflows: int
    prompt_tokens_total: int
    cache_hit_ratio: float
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _serialize_request(request: list[LLMMessage]) -> str:
    return json.dumps(
        [
            [
                m.role,
                m.content_text,
                [[c.name, c.arguments] for c in m.tool_calls or []],
            ]
            for m in request
        ],
        ensure_ascii=False,
    )


def cache_hit_ratio(requests: list[list[LLMMessage]]) -> float:
    """Byte-prefix reuse between consecutive requests, weighted by request
    size — the measurable form of "the provider prompt cache keeps hitting".
    """
    if len(requests) < 2:
        return 1.0
    shared = 0
    total = 0
    prev = _serialize_request(requests[0])
    for req in requests[1:]:
        cur = _serialize_request(req)
        prefix = 0
        for a, b in zip(prev, cur):
            if a != b:
                break
            prefix += 1
        shared += prefix
        total += len(cur)
        prev = cur
    return shared / total if total else 1.0


def make_hooks(arm: str, task: TaskConfig, summarizer: ExtractiveSummarizer | None):
    if arm == "none":
        return NoopHooks()
    if arm == "naive":
        return NaiveTruncateHooks(max_context_tokens=task.window_tokens)
    if arm == "fold_only":
        return CompactionHooks(
            max_context_tokens=task.window_tokens,
            summarizer=None,
        )
    if arm == "current":
        assert summarizer is not None
        return CompactionHooks(
            max_context_tokens=task.window_tokens,
            summarizer=summarizer,
        )
    raise ValueError(f"unknown arm: {arm}")


ARMS = ("none", "naive", "fold_only", "current")


async def run_arm(task: TaskConfig, arm: str) -> ArmReport:
    """Run one arm on one task and score the run."""
    router = ToolRouter()

    async def emit(n: int) -> str:
        return make_blob(task, n - 1)

    router.register(emit)
    provider = PolicyProvider(task)
    summarizer = ExtractiveSummarizer() if arm == "current" else None
    hooks = make_hooks(arm, task, summarizer)
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)

    error: str | None = None
    final_status = "completed"
    try:
        async for event in loop.run([LLMMessage.text_of("user", "go")]):
            if event.data.get("status"):
                final_status = event.data["status"]
    except LLMError as exc:
        error = f"{exc.kind}: {exc}"
    if final_status != "completed" and error is None:
        error = f"turn ended with status {final_status!r}"

    expected = expected_needles(task)
    found = scan_needles(provider.final_content)
    correct = sum(1 for idx, secret in found.items() if expected.get(idx) == secret)
    recall = correct / len(expected) if expected else 1.0
    return ArmReport(
        task=task.name,
        arm=arm,
        success=error is None and recall == 1.0,
        needle_recall=round(recall, 4),
        needles_found=correct,
        needles_expected=len(expected),
        compactions=getattr(hooks, "compactions", 0),
        overflows=provider.overflows,
        prompt_tokens_total=provider.prompt_tokens_total,
        cache_hit_ratio=round(cache_hit_ratio(provider.requests), 4),
        error=error,
    )
