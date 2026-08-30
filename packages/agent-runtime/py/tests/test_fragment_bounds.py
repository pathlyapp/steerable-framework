"""Typed bounded context fragments (P2.2).

Every injected fragment is typed (``content_kind``) and bounded:
``append_fragment`` enforces the class's token cap with predictable
degradation, and the gate test keeps any cap above the no-review line tied
to an explicit in-code review note (the Codex P0 rule, as a CI gate).
"""

from __future__ import annotations

import pytest
import steerable_agent_runtime.loop
import steerable_agent_runtime.skills
import steerable_agent_runtime.world_state  # noqa: F401 — register world-state fragments
from steerable_agent_runtime import CoreLoop, RouterToolExecutor, ToolRouter
from steerable_agent_runtime.history import (
    DEFAULT_FRAGMENT_MAX_TOKENS,
    FRAGMENT_TOKEN_CEILING,
    ContextFragment,
    ContextManager,
)
from steerable_agent_runtime.hooks import NoopHooks, PreStepAction, TranscriptAppend
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.tokens import estimate_text_tokens
from test_loop import collect, final_completion, make_provider


class _Chatty(ContextFragment):
    content_kind = "test.chatty"
    max_tokens = 60

    def body(self) -> str:
        return "lorem ipsum dolor sit amet " * 200


class _LineFragment(ContextFragment):
    """Structured degradation: drop whole trailing lines, never cut mid-line."""

    content_kind = "test.lines"
    max_tokens = 40

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def body(self) -> str:
        return "\n".join(self._lines)

    def degrade(self, rendered: str, *, max_tokens: int) -> str:
        lines = rendered.splitlines()
        while lines and estimate_text_tokens("\n".join(lines)) > max_tokens:
            lines.pop()
        return "\n".join(lines) + "\n…[lines dropped: token cap]"


def test_fragment_within_cap_renders_byte_identical() -> None:
    class _Small(ContextFragment):
        content_kind = "test.small"

        def body(self) -> str:
            return "short notice"

    manager = ContextManager()
    item = manager.append_fragment(_Small())
    assert item.message.content_text == "short notice"


def test_fragment_over_cap_is_truncated_with_marker() -> None:
    manager = ContextManager()
    item = manager.append_fragment(_Chatty())
    text = item.message.content_text
    assert "truncated" in text
    # The cap is a real bound, not a suggestion: degraded text fits within
    # the cap plus the marker's own size.
    assert estimate_text_tokens(text) <= 60 + 20
    # And the truncation is lossy — the full body was much larger.
    assert estimate_text_tokens(_Chatty().body()) > 60


def test_structured_degrade_drops_whole_lines() -> None:
    fragment = _LineFragment([f"line {i}: " + "x" * 40 for i in range(50)])
    manager = ContextManager()
    item = manager.append_fragment(fragment)
    text = item.message.content_text
    assert "lines dropped" in text
    kept = [ln for ln in text.splitlines() if ln.startswith("line ")]
    assert kept, "no lines survived"
    # Every surviving line is complete (no mid-line cut).
    assert all(ln.endswith("x" * 40) for ln in kept)


def test_gate_all_fragments_bounded() -> None:
    """Every ContextFragment subclass is bounded; caps above the no-review
    line must carry an explicit review note in code."""
    seen: set[type] = set()
    stack = [ContextFragment]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub in seen:
                continue
            seen.add(sub)
            stack.append(sub)
    assert ContextFragment in {c for c in seen} or seen, "no fragments registered"
    for cls in seen:
        cap = cls.effective_max_tokens()
        assert cap <= FRAGMENT_TOKEN_CEILING, (
            f"{cls.__name__} declares max_tokens={cap} over the "
            f"{FRAGMENT_TOKEN_CEILING} ceiling"
        )
        if cap > DEFAULT_FRAGMENT_MAX_TOKENS:
            assert cls.review_note, (
                f"{cls.__name__} crosses the {DEFAULT_FRAGMENT_MAX_TOKENS}-token "
                "no-review line without a review note"
            )


def test_default_cap_is_the_no_review_line() -> None:
    class _Plain(ContextFragment):
        def body(self) -> str:
            return "x"

    assert _Plain.effective_max_tokens() == DEFAULT_FRAGMENT_MAX_TOKENS


def test_skill_catalog_fragment_is_bounded() -> None:
    """The catalog is the largest legitimate injection: it declares an
    explicit cap above the no-review line (with a review note) and degrades
    by dropping trailing skill lines."""
    from steerable_agent_runtime.skills import SkillCatalogFragment

    assert SkillCatalogFragment.effective_max_tokens() > DEFAULT_FRAGMENT_MAX_TOKENS
    assert SkillCatalogFragment.review_note
    lines = [f"- skill_{i}: " + "does a thing " * 6 for i in range(200)]
    fragment = SkillCatalogFragment("\n".join(lines))
    manager = ContextManager()
    item = manager.append_fragment(fragment)
    text = item.message.content_text
    assert estimate_text_tokens(text) <= SkillCatalogFragment.effective_max_tokens() + 20
    assert "dropped" in text


@pytest.mark.asyncio
async def test_loop_enforces_fragment_cap_for_hook_appends() -> None:
    """End to end: a hook's TranscriptAppend carrying a fragment goes through
    append_fragment, so an over-cap injection lands truncated in the record."""

    class _BigInjectHooks(NoopHooks):
        async def pre_step(self, transcript, ctx):
            if ctx.round_index != 0:
                return PreStepAction(kind="proceed")
            fragment = _Chatty()
            return PreStepAction(
                kind="proceed",
                appends=[
                    TranscriptAppend(
                        message=fragment.to_message(),
                        kind=fragment.content_kind,
                        fragment=fragment,
                    )
                ],
                reason="big injection",
            )

    provider = make_provider([{"content": "ok"}])
    loop = CoreLoop(
        provider,
        RouterToolExecutor(ToolRouter()),
        hooks=_BigInjectHooks(),
    )
    events = await collect(loop.run([LLMMessage.text_of("user", "hi")]))
    assert final_completion(events)["status"] == "completed"

    injected = [
        item
        for item in loop.history.record
        if getattr(item, "kind", None) == "test.chatty"
    ]
    assert injected, "injection missing from the record"
    text = injected[0].message.content_text
    assert "truncated" in text
    # The provider saw the degraded text, not the full body.
    assert estimate_text_tokens(provider.calls[0][-1].content_text) <= 60 + 20


def test_system_prompt_fragment_contract() -> None:
    """W2.8.2: the host's system prompt is a typed fragment — system-roled,
    unmarked (byte-stable for prefix caching), capped with a review note."""
    from steerable_agent_runtime import (
        SystemPromptFragment,
        render_fragment_capped,
    )

    fragment = SystemPromptFragment("你是助手。")
    assert fragment.role == "system"
    assert fragment.markers() == ("", "")
    assert fragment.render() == "你是助手。"  # bare body, no wrapper
    assert SystemPromptFragment.effective_max_tokens() == 4096
    assert SystemPromptFragment.review_note  # over the no-review line

    message = render_fragment_capped(fragment)
    assert message.role == "system"
    assert message.content_text == "你是助手。"

    # Over-cap: degraded with the visible marker, never appended whole.
    huge = SystemPromptFragment("规则。\n" * 10_000)
    capped = render_fragment_capped(huge)
    assert "fragment truncated" in capped.content_text
    assert estimate_text_tokens(capped.content_text) <= 4096 + 20
