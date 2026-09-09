"""Benchmark: a pressure compaction firing mid-run.

User path: a long session crosses the token threshold and the loop rewrites
the transcript — fold old tool results, call the summarizer, swap the
context — while the user waits for the next token. Providers answer
instantly here, so the measured wall-clock is the framework's compaction
machinery itself: pressure estimate, transcript rewrite, summary
integration.
"""

from __future__ import annotations

import asyncio

from steerable_agent_runtime import (
    CompactionHooks,
    CoreLoop,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk

from benchmarks.calibration import ci_time_budget
from benchmarks.sampling import assert_within_budget, measure

#: Reference-machine expectation (median of 5, arm64 Mac). Reviewed
#: constant — recalibrate only with a recorded measurement.
REFERENCE_MS = 60.0

#: Tool output size per round; 12 rounds x 3KB crosses the tiny test budget.
_BLOB = "y" * 3_000
_ROUNDS = 12


class _ScriptedProvider:
    """Plays a fixed script of rounds, instantly."""

    name = "bench"
    model = "bench-model"

    def __init__(self, script: list[dict]):
        self._script = script
        self._round = 0

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        entry = self._script[min(self._round, len(self._script) - 1)]
        self._round += 1

        async def _gen():
            if entry.get("content"):
                yield LLMStreamChunk(content_delta=entry["content"])
            for call in entry.get("tool_calls", []):
                yield LLMStreamChunk(tool_call_delta=call)
            yield LLMStreamChunk(
                finish_reason="tool_calls" if entry.get("tool_calls") else "stop"
            )

        return _gen()


def _tool_call(n: int) -> ToolCall:
    return ToolCall(id=f"call-{n}", name="emit", arguments={"n": n})


def _compaction_run() -> None:
    script = [
        {"content": "", "tool_calls": [_tool_call(i)]} for i in range(_ROUNDS)
    ] + [{"content": "final"}]
    provider = _ScriptedProvider(script)
    summarizer = _ScriptedProvider([{"content": "summary"}])
    router = ToolRouter()

    async def emit(n: int) -> str:
        return _BLOB

    router.register(emit)
    hooks = CompactionHooks(
        max_context_tokens=8_000,  # tiny budget to force pressure
        threshold_ratio=0.7,
        keep_last_messages=4,
        keep_last_tool_results=1,
        summarizer=summarizer,
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)

    async def run() -> None:
        async for _ in loop.run([LLMMessage.text_of("user", "go")]):
            pass

    asyncio.run(run())
    # The benchmark only measures anything if compaction actually fired.
    assert hooks.compactions >= 1, "compaction did not fire — fix the fixture"


def test_compaction_within_budget() -> None:
    report = measure(_compaction_run)
    assert_within_budget(report, ci_time_budget(REFERENCE_MS), "compaction run")
