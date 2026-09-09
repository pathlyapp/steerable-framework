"""Benchmark: one CoreLoop round against an instant provider.

User path: every turn of every chat pays this — history projection, hook
dispatch, event emission, tool routing — before the model's first token
arrives. The provider answers instantly, so the measured wall-clock is pure
framework overhead per round (plus one event-loop build per sample, which
mirrors how hosts drive the loop).

Budget: REFERENCE_MS is the reviewed expectation on the arm64 reference
machine; the CI budget derives from it via benchmarks/calibration.py.
"""

from __future__ import annotations

import asyncio

from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    RouterToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk

from benchmarks.calibration import ci_time_budget
from benchmarks.sampling import assert_within_budget, measure

#: Reference-machine expectation (median of 5, arm64 Mac): measured 0.7ms,
#: budgeted at 2ms to leave headroom for legitimate growth. Reviewed
#: constant — recalibrate only with a recorded measurement.
REFERENCE_MS = 2.0


class _InstantProvider:
    """Answers every round immediately with a fixed text token."""

    name = "bench"
    model = "bench-model"

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        async def _gen():
            yield LLMStreamChunk(content_delta="ok")
            yield LLMStreamChunk(finish_reason="stop")

        return _gen()


def _one_round() -> None:
    # Seed a 50-turn transcript (100 messages) so the measurement covers the
    # history-projection work a long session pays every round — a bare
    # one-message seed would measure almost nothing.
    seed = [LLMMessage.text_of("user", "hi")]
    for i in range(50):
        seed.append(LLMMessage.text_of("user", f"question {i}"))
        seed.append(LLMMessage.text_of("assistant", f"answer {i}"))
    loop = CoreLoop(
        _InstantProvider(),
        RouterToolExecutor(ToolRouter()),
        LoopConfig(),
    )

    async def run() -> None:
        async for _ in loop.run(seed):
            pass

    asyncio.run(run())


def test_coreloop_round_within_budget() -> None:
    report = measure(_one_round)
    assert_within_budget(report, ci_time_budget(REFERENCE_MS), "coreloop round")
