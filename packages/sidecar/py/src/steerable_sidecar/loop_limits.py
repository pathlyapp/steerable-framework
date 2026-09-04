"""Loop-limit resolution shared by every sidecar entrypoint.

`default.harness.yaml`'s `loop:` section is the single declarative source for
the limits the harness pins (W3.4.2.4). Three entrypoints consume it — the
desktop chat path (`agent.chat.stream`), headless, and ACP — and each has its
own override channel: a request param, a CLI flag, or nothing.

One rule, one implementation. A per-entrypoint copy is how a spec value stops
reaching a run: headless once answered `max_tool_errors` from a literal 32
while the spec and the other entrypoints said 16, and nothing failed.

Precedence, highest first: the caller's explicit override, the spec's `loop:`
field, then the baseline below. The baseline answers only a field a custom
spec omits — the bundled default pins all three.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Entrypoint baseline for a field the spec leaves unset. Equal by value to
#: the bundled `default.harness.yaml`, so an omission cannot quietly change
#: behavior (`test_baseline_matches_the_bundled_spec` pins the equality).
BASELINE_MAX_ROUNDS = 80
BASELINE_MAX_TOOL_ERRORS = 16
BASELINE_TOOL_DEDUP = False


@dataclass(frozen=True, slots=True)
class ResolvedLoopLimits:
    """The three loop limits a `LoopConfig` needs, fully resolved."""

    max_rounds: int
    max_tool_errors: int
    tool_dedup: bool


def resolve_loop_limits(
    limits: Any | None,
    *,
    max_rounds: int | None = None,
    max_tool_errors: int | None = None,
) -> ResolvedLoopLimits:
    """Resolve loop limits from a spec's `loop:` section plus overrides.

    @param limits: the spec's parsed `loop:` section, or None when the spec is
        absent or unreadable — the baseline then answers every field.
    @param max_rounds: caller override (request param / CLI flag), or None.
    @param max_tool_errors: caller override, or None.
    @returns The resolved limits, baseline-filled and never None.
    """

    def first_set(*candidates: Any, baseline: Any) -> Any:
        """The first candidate that is not None. `0` and `False` are values a
        caller or spec deliberately set, so identity with None is the test."""
        for candidate in candidates:
            if candidate is not None:
                return candidate
        return baseline

    def pinned(field: str) -> Any | None:
        return getattr(limits, field, None) if limits is not None else None

    return ResolvedLoopLimits(
        max_rounds=int(
            first_set(max_rounds, pinned("max_rounds"), baseline=BASELINE_MAX_ROUNDS)
        ),
        max_tool_errors=int(
            first_set(
                max_tool_errors,
                pinned("max_tool_errors"),
                baseline=BASELINE_MAX_TOOL_ERRORS,
            )
        ),
        tool_dedup=bool(first_set(pinned("tool_dedup"), baseline=BASELINE_TOOL_DEDUP)),
    )
