"""Discrimination self-checks for the compaction benchmark standard.

The standard is only worth running if it provably separates known-bad
strategies from the current implementation (standard.md §独立性条款 5).
These tests pin that ordering, plus simulator determinism, so a future
change that breaks the standard's discriminating power fails CI — not the
strategies.
"""

from __future__ import annotations

import asyncio

import pytest

from evals.compaction.simulator import (
    TASKS,
    ExtractiveSummarizer,
    expected_needles,
    run_arm,
    scan_needles,
)


def run(task: str, arm: str):
    return asyncio.run(run_arm(TASKS[task], arm))


def test_simulator_is_deterministic() -> None:
    a = run("light", "current").to_dict()
    b = run("light", "current").to_dict()
    assert a == b


def test_no_compaction_dies_on_overflow() -> None:
    report = run("light", "none")
    assert report.success is False
    assert report.overflows >= 1
    assert report.needle_recall == 0.0
    assert report.error is not None


def test_naive_truncate_loses_most_needles() -> None:
    report = run("light", "naive")
    assert report.compactions >= 1
    # Only the needles in the kept tail survive the drop.
    assert 0.0 < report.needle_recall < 0.5


def test_fold_only_survives_on_excerpts() -> None:
    report = run("light", "fold_only")
    assert report.compactions >= 1
    # Deterministic excerpt fallback: better than dropping everything,
    # worse than a real summary.
    assert 0.0 < report.needle_recall < 1.0


def test_current_implementation_full_recall() -> None:
    for task in ("light", "heavy"):
        report = run(task, "current")
        assert report.success, f"{task}: {report.error}"
        assert report.needle_recall == 1.0
        assert report.compactions >= 1
        # Overflow recoveries are a designed path (the heuristic can miss
        # the real window); what matters is they are recovered, not zero.


def test_standard_orders_the_arms() -> None:
    """The discrimination property: none < naive/fold_only < current."""
    none = run("light", "none")
    naive = run("light", "naive")
    fold_only = run("light", "fold_only")
    current = run("light", "current")
    assert none.needle_recall < naive.needle_recall
    assert naive.needle_recall < current.needle_recall
    assert fold_only.needle_recall < current.needle_recall


@pytest.mark.asyncio
async def test_summarizer_input_is_never_truncated() -> None:
    """Warm-prefix raw replay at the suite level: every summarizer call of
    a `current` run sees a gap-free prefix of the needle sequence — the
    accumulated summary plus the raw middle since the last compaction. A
    `[:2000]` truncation or a pre-folded replay would show up as a gap.
    """
    from evals.compaction.simulator import (
        CompactionHooks,
        CoreLoop,
        LLMMessage,
        PolicyProvider,
        RouterToolExecutor,
        ToolRouter,
        make_blob,
    )

    task = TASKS["heavy"]
    router = ToolRouter()

    async def emit(n: int) -> str:
        return make_blob(task, n - 1)

    router.register(emit)
    provider = PolicyProvider(task)
    summarizer = ExtractiveSummarizer()
    hooks = CompactionHooks(
        max_context_tokens=task.window_tokens, summarizer=summarizer
    )
    loop = CoreLoop(provider, RouterToolExecutor(router), hooks=hooks)
    async for _ in loop.run([LLMMessage.text_of("user", "go")]):
        pass

    assert summarizer.calls
    for call in summarizer.calls:
        seen = scan_needles("\n".join(m.content_text for m in call))
        assert sorted(seen) == list(range(max(seen) + 1)), (
            "gap in needle coverage — summarizer input was truncated or folded"
        )
