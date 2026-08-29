"""Session branching: fork_record / branch_label / resolve_fork_seq / lineage."""

from __future__ import annotations

import pytest
from steerable_agent_runtime import (
    ContextManager,
    branch_label,
    entry_from_dict,
    entry_to_dict,
    fork_record,
    lineage,
    resolve_fork_seq,
)
from steerable_agent_runtime.branch import BranchPoint
from steerable_agent_runtime.history import HistorySeed
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.storage import InMemoryStorage


def _user(text: str) -> LLMMessage:
    return LLMMessage.text_of("user", text)


def _assistant(text: str) -> LLMMessage:
    return LLMMessage.text_of("assistant", text)


async def _append_turns(
    storage: InMemoryStorage, record_id: str, *texts: tuple[str, str]
) -> None:
    """Append (role, text) pairs as live items, continuing the record's seq."""
    existing = await storage.list_history(record_id)
    manager = ContextManager(
        [_user(t) if r == "user" else _assistant(t) for r, t in texts],
        first_seq=(int(existing[-1]["seq"]) + 1) if existing else 0,
    )
    await storage.append_history(
        record_id, [entry_to_dict(e) for e in manager.drain_pending()]
    )


async def _record_with_turns(storage: InMemoryStorage, record_id: str) -> None:
    """Three user/assistant turns as live items (seq 0..5)."""
    await _append_turns(
        storage,
        record_id,
        *[
            (r, f"{'question' if r == 'user' else 'answer'} {i}")
            for i in range(3)
            for r in ("user", "assistant")
        ],
    )


class TestBranchLabel:
    def test_last_user_message_wins(self) -> None:
        label = branch_label([_user("  hello   world "), _assistant("ok")])
        assert label == "hello world"

    def test_truncates_long_previews(self) -> None:
        label = branch_label([_user("x" * 200)], max_chars=60)
        assert len(label) == 60
        assert label.endswith("…")

    def test_falls_back_to_last_message_of_any_role(self) -> None:
        assert branch_label([_assistant("thinking out loud")]) == "[assistant] thinking out loud"

    def test_empty_prefix_has_fixed_marker(self) -> None:
        assert branch_label([]) == "(empty prefix)"


@pytest.mark.asyncio
class TestForkRecord:
    async def test_fork_seeds_fresh_record_with_provenance(self) -> None:
        storage = InMemoryStorage()
        await _record_with_turns(storage, "chat_1")

        point = (await fork_record(storage, "chat_1", until_seq=3)).point

        assert point.source_record_id == "chat_1"
        assert point.source_until_seq == 3
        assert point.record_id != "chat_1"
        assert point.label == "question 1"
        # The forked record is self-contained: one seed entry carrying the
        # prefix, provenance, and per-message kinds.
        entries = await storage.list_history(point.record_id)
        assert len(entries) == 1
        seed = entry_from_dict(entries[0])
        assert isinstance(seed, HistorySeed)
        assert seed.source_record_id == "chat_1"
        assert seed.source_until_seq == 3
        assert [m.content_text for m in seed.messages] == [
            "question 0",
            "answer 0",
            "question 1",
            "answer 1",
        ]
        assert seed.message_kinds == ("user", "assistant", "user", "assistant")

    async def test_fork_at_tip_when_no_until_seq(self) -> None:
        storage = InMemoryStorage()
        await _record_with_turns(storage, "chat_1")

        point = (await fork_record(storage, "chat_1")).point

        seed = entry_from_dict((await storage.list_history(point.record_id))[0])
        assert isinstance(seed, HistorySeed)
        assert len(seed.messages) == 6
        assert seed.source_until_seq is None

    async def test_explicit_record_id_and_label(self) -> None:
        storage = InMemoryStorage()
        await _record_with_turns(storage, "chat_1")

        point = (
            await fork_record(
                storage, "chat_1", until_seq=1, new_record_id="chat_2", label="my branch"
            )
        ).point

        assert point.record_id == "chat_2"
        assert point.label == "my branch"

    async def test_missing_source_record_raises(self) -> None:
        storage = InMemoryStorage()
        with pytest.raises(KeyError, match="record not found"):
            await fork_record(storage, "nope")

    async def test_source_record_is_never_mutated(self) -> None:
        storage = InMemoryStorage()
        await _record_with_turns(storage, "chat_1")
        before = await storage.list_history("chat_1")

        await fork_record(storage, "chat_1", until_seq=1)

        assert await storage.list_history("chat_1") == before


@pytest.mark.asyncio
class TestResolveForkSeq:
    async def test_tip_when_no_flags(self) -> None:
        storage = InMemoryStorage()
        await _record_with_turns(storage, "chat_1")
        assert await resolve_fork_seq(storage, "chat_1") == 5

    async def test_before_last_user_is_the_regen_address(self) -> None:
        """Regenerate forks at the last user item: the prompting turn stays
        in the prefix, the assistant reply after it is dropped."""
        storage = InMemoryStorage()
        await _record_with_turns(storage, "chat_1")
        assert await resolve_fork_seq(storage, "chat_1", before_last_user=True) == 4

    async def test_no_user_message_returns_none(self) -> None:
        storage = InMemoryStorage()
        await _append_turns(storage, "chat_1", ("assistant", "unsolicited"))
        assert await resolve_fork_seq(storage, "chat_1", before_last_user=True) is None

    async def test_missing_record_returns_none(self) -> None:
        storage = InMemoryStorage()
        assert await resolve_fork_seq(storage, "nope", before_last_user=True) is None

    async def test_user_index_addresses_the_kth_user_item(self) -> None:
        """The desktop's regen addressing: the prompting turn's ordinal."""
        storage = InMemoryStorage()
        await _record_with_turns(storage, "chat_1")
        assert await resolve_fork_seq(storage, "chat_1", user_index=0) == 0
        assert await resolve_fork_seq(storage, "chat_1", user_index=2) == 4
        assert await resolve_fork_seq(storage, "chat_1", user_index=3) is None
        assert await resolve_fork_seq(storage, "chat_1", user_index=-1) is None

    async def test_user_index_counts_seed_messages_but_cannot_address_inside(
        self,
    ) -> None:
        """A forked record's seed contributes its user messages to the
        ordinal; an index landing inside the (indivisible) seed is None."""
        storage = InMemoryStorage()
        await _record_with_turns(storage, "chat_1")
        await fork_record(storage, "chat_1", until_seq=3, new_record_id="a")
        await _append_turns(storage, "a", ("user", "variant question"))

        # Seed holds question 0/1 (indices 0-1); the live user item is 2.
        assert await resolve_fork_seq(storage, "a", user_index=2) == 1
        assert await resolve_fork_seq(storage, "a", user_index=1) is None
        assert await resolve_fork_seq(storage, "a", user_index=0) is None


@pytest.mark.asyncio
class TestLineage:
    async def test_single_root(self) -> None:
        storage = InMemoryStorage()
        await _record_with_turns(storage, "chat_1")

        chain = await lineage(storage, "chat_1")

        assert chain == [
            BranchPoint(
                record_id="chat_1",
                source_record_id=None,
                source_until_seq=None,
                label="root",
                depth=0,
            )
        ]

    async def test_fork_chain_is_root_first_with_depths(self) -> None:
        storage = InMemoryStorage()
        await _record_with_turns(storage, "chat_1")
        await fork_record(storage, "chat_1", until_seq=3, new_record_id="a")
        await _append_turns(storage, "a", ("user", "variant question"), ("assistant", "variant answer"))
        await fork_record(storage, "a", new_record_id="b")

        chain = await lineage(storage, "b")

        assert [p.record_id for p in chain] == ["chat_1", "a", "b"]
        assert [p.depth for p in chain] == [0, 1, 2]
        assert chain[1].label == "question 1"
        assert chain[2].label == "variant question"
        assert chain[2].source_record_id == "a"

    async def test_cycle_raises(self) -> None:
        storage = InMemoryStorage()
        # Hand-craft a provenance cycle: a's seed names b, b's seed names a.
        for record, source in (("a", "b"), ("b", "a")):
            seed = HistorySeed(
                seq=0,
                messages=(_user("x"),),
                token_estimate=1,
                source_record_id=source,
            )
            await storage.append_history(record, [entry_to_dict(seed)])

        with pytest.raises(ValueError, match="cycle"):
            await lineage(storage, "a")
