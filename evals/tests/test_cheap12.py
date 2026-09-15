"""cheap-12 is a 7/4/1 stratified draw, not a pinned easy subset."""

from __future__ import annotations

from evals.cheap12 import (
    FLAKY_BANDS,
    STABLE_RED,
    flaky_ids,
    select_cheap12,
)
from evals.suite import EXCLUSIVE_PACK_TASKS, load_suite


def test_flaky_bands_match_suite() -> None:
    suite = load_suite()
    assert flaky_ids() == set(suite.splits["flaky"])
    assert STABLE_RED <= set(suite.splits["loss-34"])


def test_flaky_bands_are_a_partition() -> None:
    seen: list[str] = []
    for band in FLAKY_BANDS.values():
        seen.extend(band)
    assert len(seen) == len(set(seen))


def test_cheap12_is_hamilton_7_4_1() -> None:
    suite = load_suite()
    drawn = select_cheap12(suite.catalog, suite.catalog_minutes)
    assert drawn == suite.splits["cheap-12"]
    flaky = set(suite.splits["flaky"])
    greens = [task for task in drawn if task not in flaky and task not in STABLE_RED]
    flakies = [task for task in drawn if task in flaky]
    reds = [task for task in drawn if task in STABLE_RED]
    assert len(greens) == 7
    assert len(flakies) == 4
    assert len(reds) == 1
    assert "fix-git" in greens
    assert "code-from-image" not in drawn
    assert "extract-moves-from-video" not in drawn
    assert not EXCLUSIVE_PACK_TASKS & set(drawn)
