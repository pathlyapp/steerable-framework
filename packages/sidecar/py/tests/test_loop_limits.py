"""One resolution rule for the limits `default.harness.yaml` pins.

The copies this replaced had drifted: headless answered `max_tool_errors` from
a literal 32 while the spec and ACP said 16, so a run never saw the declared
value. These tests fix the precedence and pin every entrypoint to the spec.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from steerable_sidecar.loop_limits import (
    BASELINE_MAX_ROUNDS,
    BASELINE_MAX_TOOL_ERRORS,
    resolve_loop_limits,
)


@dataclass(frozen=True)
class _Loop:
    """Stand-in for a spec's parsed ``loop:`` section."""

    max_rounds: int | None = None
    max_tool_errors: int | None = None
    tool_dedup: bool | None = None


def test_spec_values_win_over_the_baseline() -> None:
    resolved = resolve_loop_limits(_Loop(max_rounds=80, max_tool_errors=16, tool_dedup=False))
    assert (resolved.max_rounds, resolved.max_tool_errors, resolved.tool_dedup) == (80, 16, False)


def test_caller_override_wins_over_the_spec() -> None:
    resolved = resolve_loop_limits(
        _Loop(max_rounds=80, max_tool_errors=16),
        max_rounds=12,
        max_tool_errors=5,
    )
    assert (resolved.max_rounds, resolved.max_tool_errors) == (12, 5)


def test_absent_spec_field_falls_back_to_the_baseline() -> None:
    resolved = resolve_loop_limits(_Loop(max_rounds=7))
    assert resolved.max_rounds == 7
    assert resolved.max_tool_errors == BASELINE_MAX_TOOL_ERRORS
    assert resolved.tool_dedup is False


def test_missing_spec_answers_every_field_from_the_baseline() -> None:
    resolved = resolve_loop_limits(None)
    assert (resolved.max_rounds, resolved.max_tool_errors) == (
        BASELINE_MAX_ROUNDS,
        BASELINE_MAX_TOOL_ERRORS,
    )


def test_baseline_matches_the_bundled_spec() -> None:
    """A spec omission must not change behavior, so the two agree by value."""
    from steerable_agent_runtime.harness_spec import load_harness_spec
    from steerable_sidecar.sidecar import _DEFAULT_HARNESS_SPEC_PATH

    pinned = load_harness_spec(_DEFAULT_HARNESS_SPEC_PATH).loop
    assert pinned is not None
    assert (pinned.max_rounds, pinned.max_tool_errors) == (
        BASELINE_MAX_ROUNDS,
        BASELINE_MAX_TOOL_ERRORS,
    )


def test_tool_dedup_false_in_the_spec_is_not_read_as_unset() -> None:
    """`False` is a pinned value, not an absence — `or` would lose it."""
    assert resolve_loop_limits(_Loop(tool_dedup=False)).tool_dedup is False
    assert resolve_loop_limits(_Loop(tool_dedup=True)).tool_dedup is True


def test_zero_override_is_honored_not_treated_as_unset() -> None:
    """`max_rounds=0` from a caller means 0; `or` would silently give 80."""
    assert resolve_loop_limits(_Loop(max_rounds=80), max_rounds=0).max_rounds == 0


@pytest.mark.parametrize(
    "entrypoint",
    ["chat", "acp"],
)
def test_every_entrypoint_resolves_the_bundled_spec_identically(entrypoint: str) -> None:
    """The chat and ACP entrypoints must not disagree about the same spec."""
    from steerable_sidecar.acp_adapter import _default_loop_config
    from steerable_sidecar.sidecar import _build_loop_config

    config = _build_loop_config({}) if entrypoint == "chat" else _default_loop_config()
    assert (config.max_rounds, config.max_tool_errors, config.tool_dedup) == (80, 16, False)
