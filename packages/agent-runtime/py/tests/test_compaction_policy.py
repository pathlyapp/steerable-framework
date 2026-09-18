"""P3: per-model compaction policy resolution.

The knob set is chosen in ONE place (``resolve_compaction_policy``), never
by an inline if/else at an assembly site. These tests pin the resolution
contract: window classes, family-rule precedence, and that the resolved
params map exactly onto ``CompactionHooks`` kwargs.
"""

from __future__ import annotations

from steerable_agent_runtime import (
    CompactionHooks,
    is_large_window,
    resolve_compaction_policy,
)
from steerable_agent_runtime.compaction_policy import (
    _LARGE_WINDOW_TOKENS,
    CompactionPolicy,
)


def test_desktop_window_gets_the_tight_policy() -> None:
    policy = resolve_compaction_policy(model="deepseek-v4", max_context_tokens=60_000)
    assert policy == CompactionPolicy(
        keep_last_messages=6,
        keep_last_tool_results=2,
        fold_excerpt_chars=160,
        keep_last_images=1,
        image_offload=True,
    )


def test_large_window_gets_the_generous_policy() -> None:
    policy = resolve_compaction_policy(
        model="z-ai/glm-5.3", max_context_tokens=1_048_576
    )
    assert policy.keep_last_tool_results == 16
    assert policy.keep_last_messages == 16
    assert policy.fold_excerpt_chars == 4_000


def test_window_boundary_is_exclusive_below() -> None:
    assert (
        resolve_compaction_policy(
            model=None, max_context_tokens=_LARGE_WINDOW_TOKENS - 1
        ).fold_excerpt_chars
        == 160
    )
    assert (
        resolve_compaction_policy(
            model=None, max_context_tokens=_LARGE_WINDOW_TOKENS
        ).fold_excerpt_chars
        == 4_000
    )


def test_model_family_rule_wins_over_window_class() -> None:
    # No family rules ship today; pin the precedence with a synthetic entry.
    from steerable_agent_runtime import compaction_policy as cp

    family = CompactionPolicy(
        keep_last_messages=1,
        keep_last_tool_results=1,
        fold_excerpt_chars=1,
        keep_last_images=0,
    )
    cp._FAMILY_POLICIES = (*cp._FAMILY_POLICIES, ("testmodel", family))
    try:
        assert (
            resolve_compaction_policy(
                model="testmodel-x", max_context_tokens=1_048_576
            )
            is family
        )
        # Prefix matching is case-insensitive and prefix-anchored.
        assert (
            resolve_compaction_policy(
                model="TestModel-x", max_context_tokens=1_048_576
            )
            is family
        )
        assert (
            resolve_compaction_policy(
                model="other-testmodel", max_context_tokens=1_048_576
            )
            is not family
        )
    finally:
        cp._FAMILY_POLICIES = cp._FAMILY_POLICIES[:-1]


def test_is_large_window_matches_the_policy_class_boundary() -> None:
    """Spill budgets and the policy class share one threshold — the assembly
    sites must never re-derive it."""
    assert not is_large_window(_LARGE_WINDOW_TOKENS - 1)
    assert is_large_window(_LARGE_WINDOW_TOKENS)


def test_policy_params_construct_compaction_hooks() -> None:
    """The resolved params are exactly the hook's knob surface — a policy
    field that drifts from the constructor fails here, not in production."""
    policy = resolve_compaction_policy(model=None, max_context_tokens=131_072)
    hooks = CompactionHooks(max_context_tokens=131_072, **policy.as_params())
    assert hooks._keep_last == policy.keep_last_messages
    assert hooks._keep_last_tools == policy.keep_last_tool_results
    assert hooks._fold_excerpt_chars == policy.fold_excerpt_chars
    assert hooks._keep_last_images == policy.keep_last_images
    assert hooks._image_offload == policy.image_offload


def test_policy_params_construct_pressure_compaction() -> None:
    """The sidecar assembly spreads ``as_params()`` into the
    ``pressure_compaction`` dimension — a policy field that
    ``PressureCompaction`` does not forward fails here, not at runtime."""
    from steerable_agent_runtime.harness import PressureCompaction

    policy = resolve_compaction_policy(model=None, max_context_tokens=131_072)
    dimension = PressureCompaction(max_context_tokens=131_072, **policy.as_params())
    hooks = dimension.hooks(provider=None)
    assert isinstance(hooks._inner, CompactionHooks)
    assert hooks._inner._keep_last_images == policy.keep_last_images
    assert hooks._inner._image_offload == policy.image_offload
