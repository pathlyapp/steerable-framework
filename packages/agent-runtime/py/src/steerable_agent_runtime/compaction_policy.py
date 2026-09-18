"""Per-model compaction policy resolution (P3).

The single, explicit place compaction knobs are chosen. Before this module
the choice lived as an inline ``max_ctx >= 200_000`` if/else in sidecar
assembly — a hidden default that no test pinned and no model identity could
extend. ``resolve_compaction_policy`` makes it a named, testable step:
assembly declares the model and the window, the policy returns every knob.

Precedence: a model-family rule wins; otherwise the window size class
decides. Add a family by extending ``_FAMILY_POLICIES`` — never by
special-casing at the assembly site again.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Window at or above which the large-window policy applies. Desktop
#: deployments sit at 60k–131k; eval/eval-class models (GLM 1M) sit far
#: above it. Below the bound, small windows fold early and keep little —
#: every kept byte is cache prefix the next request re-pays for.
_LARGE_WINDOW_TOKENS = 200_000


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """The knob set ``CompactionHooks`` consumes. Resolved once at assembly;
    the hooks themselves stay policy-free."""

    keep_last_messages: int
    keep_last_tool_results: int
    fold_excerpt_chars: int
    keep_last_images: int
    image_offload: bool = True

    def as_params(self) -> dict[str, int | bool]:
        """The ``CompactionHooks`` kwargs for this policy."""
        return {
            "keep_last_messages": self.keep_last_messages,
            "keep_last_tool_results": self.keep_last_tool_results,
            "fold_excerpt_chars": self.fold_excerpt_chars,
            "keep_last_images": self.keep_last_images,
            "image_offload": self.image_offload,
        }


#: Small windows (desktop 60k–131k): keep the tail tight. Long excerpts and
#: many kept results would re-pay cache prefix on every request for context
#: the summarizer is about to shadow anyway.
_POLICY_DESKTOP = CompactionPolicy(
    keep_last_messages=6,
    keep_last_tool_results=2,
    fold_excerpt_chars=160,
    keep_last_images=1,
)

#: Large windows (GLM 1M eval traces): fold late and keep generous excerpts —
#: compile/train tails must survive folding with enough clue text to stay
#: actionable, and the window can afford the raw tail.
_POLICY_LARGE = CompactionPolicy(
    keep_last_messages=16,
    keep_last_tool_results=16,
    fold_excerpt_chars=4_000,
    keep_last_images=4,
)

#: Model-family overrides, keyed by lowercase name prefix (first match wins).
#: Empty today: the window classes cover every deployment we run. A family
#: entry belongs here when a model's behavior — not its window — demands
#: different knobs (e.g. a tokenizer whose estimates mislead the pressure
#: heuristic badly enough to need a lower threshold_ratio).
_FAMILY_POLICIES: tuple[tuple[str, CompactionPolicy], ...] = ()


def is_large_window(max_context_tokens: int) -> bool:
    """Whether the window falls in the large class. Shared by non-compaction
    knobs that branch on the same size class (e.g. spill inline budgets), so
    the threshold is defined exactly once."""
    return max_context_tokens >= _LARGE_WINDOW_TOKENS


def resolve_compaction_policy(
    *, model: str | None, max_context_tokens: int
) -> CompactionPolicy:
    """Resolve the compaction knob set for one deployment.

    ``model`` is the provider model id (family rules match on its lowercase
    prefix); ``max_context_tokens`` is the deployment's configured window.
    """
    if model:
        lowered = model.lower()
        for prefix, policy in _FAMILY_POLICIES:
            if lowered.startswith(prefix):
                return policy
    if is_large_window(max_context_tokens):
        return _POLICY_LARGE
    return _POLICY_DESKTOP
