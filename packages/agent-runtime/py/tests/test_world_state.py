"""Wave 2 world-state sections: RFC 7386 merge-patch diffing.

The full state is injected once as a ``<world-state>`` fragment; later
turns inject nothing when unchanged and a small ``<world-state-patch>``
tail fragment when changed — the cached prefix stays byte-stable. The
snapshot embedded in each fragment is the only channel: resume, fork, and
compaction all diff against what the model has actually seen.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from steerable_agent_runtime import (
    CoreLoop,
    LLMMessage,
    LLMStreamChunk,
    LoopContext,
    LoopEvent,
    RouterToolExecutor,
    StaticWorldStateSection,
    ToolRouter,
    WorldStateFragment,
    WorldStateHooks,
    WorldStatePatchFragment,
    apply_merge_patch,
    last_world_state_snapshot,
    merge_patch,
)


def _msg(role: str, content: str) -> LLMMessage:
    return LLMMessage.text_of(role, content)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RFC 7386 — the spec's own application vectors
# ---------------------------------------------------------------------------


RFC7386_VECTORS = [
    # (target, patch, expected)
    ({"a": "b"}, {"a": "c"}, {"a": "c"}),
    ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
    ({"a": "b"}, {"a": None}, {}),
    ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
    ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
    ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
    ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
    ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
    (["a", "b"], ["c", "d"], ["c", "d"]),
    ({"a": "b"}, ["c"], ["c"]),
    ({"a": "foo"}, None, None),
    ({"a": "foo"}, "bar", "bar"),
    ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
    ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
    ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
]


@pytest.mark.parametrize("target,patch,expected", RFC7386_VECTORS)
def test_apply_merge_patch_rfc_vectors(target: Any, patch: Any, expected: Any) -> None:
    assert apply_merge_patch(target, patch) == expected


def test_merge_patch_returns_none_when_unchanged() -> None:
    assert merge_patch({"a": {"b": 1}, "c": [1, 2]}, {"c": [1, 2], "a": {"b": 1}}) is None


def test_merge_patch_diffs_key_wise_and_marks_removals_null() -> None:
    patch = merge_patch(
        {"time": {"iso": "t1"}, "workspace": {"cwd": "/a"}, "mood": "x"},
        {"time": {"iso": "t2"}, "workspace": {"cwd": "/a"}},
    )
    assert patch == {"time": {"iso": "t2"}, "mood": None}


def test_merge_patch_replaces_arrays_wholesale() -> None:
    assert merge_patch({"a": [1, 2]}, {"a": [1, 2, 3]}) == {"a": [1, 2, 3]}


@pytest.mark.parametrize(
    "previous,current",
    [
        ({"a": 1}, {"a": 2, "b": {"c": [1]}},),
        ({"a": {"b": {"c": 1}}, "d": [1, 2]}, {"a": {}}),
        ([1], {"a": 1}),
        ({"a": 1}, [1]),
    ],
)
def test_merge_patch_round_trip(previous: Any, current: Any) -> None:
    patch = merge_patch(previous, current)
    if patch is None:
        assert previous == current
    else:
        assert apply_merge_patch(previous, patch) == current


# ---------------------------------------------------------------------------
# Fragments: render / self-match / embedded snapshot
# ---------------------------------------------------------------------------


def _fragment_json(text: str, end_marker: str) -> Any:
    """The JSON body line of a rendered fragment (after the snapshot
    comment, before the end marker)."""
    body = text.split("-->", 1)[1]
    return json.loads(body.strip().removesuffix(end_marker).strip().splitlines()[-1])


def test_full_fragment_round_trips_its_snapshot() -> None:
    snapshot = {"time": {"iso": "2026-08-29T06:00:00+08:00"}, "ws": {"cwd": "/app"}}
    text = WorldStateFragment(snapshot).render()
    assert text.startswith("<world-state>")
    assert text.strip().endswith("</world-state>")
    assert WorldStateFragment.matches_text(text)
    assert _fragment_json(text, "</world-state>") == snapshot
    assert last_world_state_snapshot([_msg("user", text)]) == snapshot


def test_patch_fragment_round_trips_patch_and_new_snapshot() -> None:
    previous = {"time": {"iso": "t1"}, "ws": {"cwd": "/a"}}
    current = {"time": {"iso": "t2"}, "ws": {"cwd": "/a"}}
    patch = merge_patch(previous, current)
    assert patch is not None
    text = WorldStatePatchFragment(patch, current).render()
    assert WorldStatePatchFragment.matches_text(text)
    # The visible delta is the patch; the embedded snapshot is the NEW full
    # state so the next turn diffs against it.
    assert _fragment_json(text, "</world-state-patch>") == patch
    assert last_world_state_snapshot([_msg("user", text)]) == current


def test_last_snapshot_prefers_the_most_recent_fragment() -> None:
    first = WorldStateFragment({"time": "t1"}).render()
    second = WorldStatePatchFragment({"time": "t2"}, {"time": "t2"}).render()
    transcript = [_msg("user", first), _msg("assistant", "ok"), _msg("user", second)]
    assert last_world_state_snapshot(transcript) == {"time": "t2"}


def test_last_snapshot_none_without_fragments() -> None:
    assert last_world_state_snapshot([_msg("user", "hello")]) is None


def test_full_and_patch_markers_do_not_cross_match() -> None:
    assert not WorldStateFragment.matches_text(
        WorldStatePatchFragment({"a": 1}, {"a": 1}).render()
    )
    assert not WorldStatePatchFragment.matches_text(
        WorldStateFragment({"a": 1}).render()
    )


# ---------------------------------------------------------------------------
# Hooks: inject once, then diff
# ---------------------------------------------------------------------------


def _hooks(*sections: tuple[str, Any]) -> WorldStateHooks:
    return WorldStateHooks([StaticWorldStateSection(k, v) for k, v in sections])


@pytest.mark.asyncio
async def test_first_turn_injects_full_state() -> None:
    hooks = _hooks(("time", {"iso": "t1"}), ("ws", {"cwd": "/a"}))
    action = await hooks.pre_step([_msg("user", "hi")], LoopContext(round_index=0))
    assert action.kind == "proceed"
    assert action.appends is not None and len(action.appends) == 1
    append = action.appends[0]
    assert append.kind == "world_state.snapshot"
    assert action.append_action == "world_state"
    text = append.message.content_text
    assert WorldStateFragment.matches_text(text)
    assert _fragment_json(text, "</world-state>") == {
        "time": {"iso": "t1"},
        "ws": {"cwd": "/a"},
    }


@pytest.mark.asyncio
async def test_unchanged_state_injects_nothing() -> None:
    hooks = _hooks(("time", {"iso": "t1"}))
    prior = _msg("user", WorldStateFragment({"time": {"iso": "t1"}}).render())
    action = await hooks.pre_step(
        [prior, _msg("user", "again")], LoopContext(round_index=0)
    )
    assert action.kind == "proceed"
    assert action.appends is None


@pytest.mark.asyncio
async def test_changed_section_costs_one_tail_patch() -> None:
    hooks = _hooks(("time", {"iso": "t2"}), ("ws", {"cwd": "/a"}))
    prior = _msg(
        "user",
        WorldStateFragment({"time": {"iso": "t1"}, "ws": {"cwd": "/a"}}).render(),
    )
    action = await hooks.pre_step([prior, _msg("user", "again")], LoopContext(round_index=0))
    assert action.appends is not None and len(action.appends) == 1
    append = action.appends[0]
    assert append.kind == "world_state.patch"
    text = append.message.content_text
    assert WorldStatePatchFragment.matches_text(text)
    # Only the changed section renders; the embedded snapshot is the new
    # full state.
    assert '"time"' in text and '"ws"' not in text.split("-->", 1)[1]
    assert last_world_state_snapshot([append.message]) == {
        "time": {"iso": "t2"},
        "ws": {"cwd": "/a"},
    }


@pytest.mark.asyncio
async def test_removed_section_patches_null() -> None:
    hooks = _hooks(("time", {"iso": "t2"}))
    prior = _msg(
        "user",
        WorldStateFragment({"time": {"iso": "t1"}, "mood": "x"}).render(),
    )
    action = await hooks.pre_step([prior], LoopContext(round_index=0))
    assert action.appends is not None
    text = action.appends[0].message.content_text
    assert _fragment_json(text, "</world-state-patch>") == {
        "time": {"iso": "t2"},
        "mood": None,
    }


@pytest.mark.asyncio
async def test_null_fields_are_stripped_before_diffing() -> None:
    """Merge-patch nulls mean deletion, so a None value is unrepresentable
    — absent carries the same meaning and keeps the diff coherent."""
    hooks = _hooks(("time", {"iso": "t1", "note": None}))
    action = await hooks.pre_step([_msg("user", "hi")], LoopContext(round_index=0))
    assert action.appends is not None
    text = action.appends[0].message.content_text
    assert _fragment_json(text, "</world-state>") == {"time": {"iso": "t1"}}
    assert last_world_state_snapshot([_msg("user", text)]) == {"time": {"iso": "t1"}}


@pytest.mark.asyncio
async def test_lost_snapshot_reinjects_full_state() -> None:
    """Compaction folded the last world-state fragment → Absent → full."""
    hooks = _hooks(("time", {"iso": "t2"}))
    action = await hooks.pre_step(
        [_msg("user", "compacted summary")], LoopContext(round_index=0)
    )
    assert action.appends is not None
    assert action.appends[0].kind == "world_state.snapshot"


@pytest.mark.asyncio
async def test_no_injection_beyond_round_zero() -> None:
    hooks = _hooks(("time", {"iso": "t1"}))
    action = await hooks.pre_step([_msg("user", "hi")], LoopContext(round_index=3))
    assert action.kind == "proceed"
    assert action.appends is None


# ---------------------------------------------------------------------------
# Loop integration over a continuous per-chat record
# ---------------------------------------------------------------------------


def _provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                yield LLMStreamChunk(finish_reason="stop")

            return _gen()

    return _FakeProvider()


async def _collect(events) -> list[LoopEvent]:
    return [event async for event in events]


@pytest.mark.asyncio
async def test_second_turn_diffs_against_what_the_model_saw() -> None:
    from steerable_agent_runtime.storage import InMemoryStorage

    storage = InMemoryStorage()
    hooks1 = _hooks(("time", {"iso": "t1"}))
    provider1 = _provider([{"content": "answer one"}])
    loop1 = CoreLoop(
        provider1,
        RouterToolExecutor(ToolRouter()),
        hooks=hooks1,
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop1.run([_msg("user", "question one")], chat_id="chat_1"))
    # Turn 1: full state injected right after the user message.
    assert WorldStateFragment.matches_text(provider1.calls[0][1].content_text)

    # Turn 2, unchanged state: zero tokens — no new world-state message.
    hooks2 = _hooks(("time", {"iso": "t1"}))
    provider2 = _provider([{"content": "answer two"}])
    loop2 = CoreLoop(
        provider2,
        RouterToolExecutor(ToolRouter()),
        hooks=hooks2,
        history_store=storage,
        record_id="chat_1",
    )
    seed2 = [*loop1.history.projection, _msg("user", "question two")]
    await _collect(loop2.run(seed2, chat_id="chat_1"))
    request2 = provider2.calls[0]
    assert sum(
        WorldStateFragment.matches_text(m.content_text)
        or WorldStatePatchFragment.matches_text(m.content_text)
        for m in request2
    ) == 1  # still only turn 1's fragment

    # Turn 3, changed state: one small tail patch; the prefix is untouched.
    hooks3 = _hooks(("time", {"iso": "t2"}))
    provider3 = _provider([{"content": "answer three"}])
    loop3 = CoreLoop(
        provider3,
        RouterToolExecutor(ToolRouter()),
        hooks=hooks3,
        history_store=storage,
        record_id="chat_1",
    )
    seed3 = [*loop2.history.projection, _msg("user", "question three")]
    await _collect(loop3.run(seed3, chat_id="chat_1"))
    request3 = provider3.calls[0]
    # Prefix stability: request 3 begins with request 2's exact messages
    # plus turn 2's terminal answer and the new user message.
    assert [m.content_text for m in request3[: len(request2)]] == [
        m.content_text for m in request2
    ]
    tail = request3[-1]
    assert WorldStatePatchFragment.matches_text(tail.content_text)
    assert '"t2"' in tail.content_text


@pytest.mark.asyncio
async def test_production_shaped_turns_diff_world_state() -> None:
    """The payoff under the real host contract: the host rebuilds its lossy
    view each turn (no fragments in the seed), yet record-aware seeding
    keeps turn 1's fragment in the projection — so a changed section diffs
    (one tail patch) instead of re-injecting in full."""
    from steerable_agent_runtime.storage import InMemoryStorage

    storage = InMemoryStorage()
    provider1 = _provider([{"content": "answer one"}])
    loop1 = CoreLoop(
        provider1,
        RouterToolExecutor(ToolRouter()),
        hooks=_hooks(("time", {"iso": "t1"})),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(loop1.run([_msg("user", "question one")], chat_id="chat_1"))
    assert WorldStateFragment.matches_text(provider1.calls[0][-1].content_text)

    # Turn 2's seed is the host-DB view — no world-state fragment in it.
    provider2 = _provider([{"content": "answer two"}])
    loop2 = CoreLoop(
        provider2,
        RouterToolExecutor(ToolRouter()),
        hooks=_hooks(("time", {"iso": "t2"})),
        history_store=storage,
        record_id="chat_1",
    )
    await _collect(
        loop2.run(
            [
                _msg("user", "question one"),
                _msg("assistant", "answer one"),
                _msg("user", "question two"),
            ],
            chat_id="chat_1",
        )
    )

    request2 = provider2.calls[0]
    # Turn 1's full fragment is still in context (record-aware seeding)…
    assert any(WorldStateFragment.matches_text(m.content_text) for m in request2)
    # …and the new injection is the small patch, not a full re-injection.
    patches = [
        m for m in request2 if WorldStatePatchFragment.matches_text(m.content_text)
    ]
    assert len(patches) == 1
    assert '"t2"' in patches[0].content_text
    assert patches[0] is request2[-1]
