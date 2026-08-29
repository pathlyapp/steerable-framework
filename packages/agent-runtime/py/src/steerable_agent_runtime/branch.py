"""Session branching: fork provenance, derived labels, lineage (Wave 5).

A branch is a NEW durable record seeded with a prefix of its source — the
codex/dsh fork shape, not pi's in-log entry tree. Steerable's record is
append-only linear, so branching happens at record granularity and the
source record is never mutated: a regenerate that used to truncate the
host's store and keep appending to the same record (leaving both tails
interleaved with no marker) now forks, and the old tail stays intact and
discoverable.

This module ties the existing primitives (``load_history_items`` +
``ContextManager.seed`` provenance) into one-call operations:

- ``fork_record`` — load the prefix, write a provenance-carrying
  ``HistorySeed`` into a fresh record, return the ``BranchPoint``.
- ``branch_label`` — a deterministic, LLM-free branch summary derived
  from the fork point (the last user message's preview).
- ``resolve_fork_seq`` — fork-point addressing for hosts that think in
  messages, not record seqs (regenerate = fork keeping the last user
  turn, dropping the assistant reply after it).
- ``lineage`` — walk the seed-provenance chain upwards, cycle-guarded.

Children discovery is deliberately host-side: ``StorageAdapter`` has no
record enumeration, so stores that can list records implement
``list_history_records(prefix)`` (optional, structural) and callers
combine it with the ``{source}:fork:{id}`` naming convention; everyone
else gets lineage-only views.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .history import (
    KIND_USER,
    HistorySeed,
    entry_from_dict,
    entry_to_dict,
)
from .resume import load_history_items
from .tokens import estimate_tokens

if TYPE_CHECKING:
    from .llm import LLMMessage
    from .storage import StorageAdapter

__all__ = [
    "BranchPoint",
    "ForkResult",
    "branch_label",
    "fork_record",
    "lineage",
    "resolve_fork_seq",
]

#: Bound on the provenance walk — a cycle or runaway chain is data
#: corruption, not a deep tree; fail loud past this depth.
_MAX_LINEAGE_DEPTH = 32

#: Reverse-scan page for ``resolve_fork_seq`` — the regen fork point is
#: almost always in the tail page.
_FORK_SCAN_PAGE = 128

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class BranchPoint:
    """One node in a branch family.

    ``source_record_id`` / ``source_until_seq`` are the fork provenance
    (None on a root). ``label`` is the derived branch summary — see
    ``branch_label``. ``depth`` is 0 on the root, +1 per fork hop (only
    populated by ``lineage``; ``fork_record`` leaves it 0 for the fresh
    branch it returns).
    """

    record_id: str
    source_record_id: str | None
    source_until_seq: int | None
    label: str
    depth: int = 0


@dataclass(frozen=True, slots=True)
class ForkResult:
    """What ``fork_record`` persisted: the branch node plus the seeded
    prefix messages (already loaded — callers running a turn on the fork
    shouldn't re-read the record to get them)."""

    point: BranchPoint
    messages: list["LLMMessage"]


def branch_label(messages: list["LLMMessage"], *, max_chars: int = 60) -> str:
    """Derive a branch summary from the forked prefix — deterministic, no
    LLM call: the last user message, whitespace-collapsed and truncated.
    Falls back to the last message of any role, then to a fixed marker
    for an empty prefix (forking an empty record is legal — the branch
    just shares nothing but provenance)."""

    def preview(text: str) -> str:
        collapsed = _WHITESPACE.sub(" ", text).strip()
        if len(collapsed) <= max_chars:
            return collapsed
        return collapsed[: max_chars - 1].rstrip() + "…"

    for message in reversed(messages):
        if message.role == "user" and message.content_text.strip():
            return preview(message.content_text)
    for message in reversed(messages):
        if message.content_text.strip():
            return preview(f"[{message.role}] {message.content_text}")
    return "(empty prefix)"


async def fork_record(
    storage: "StorageAdapter",
    source_record_id: str,
    *,
    until_seq: int | None = None,
    new_record_id: str | None = None,
    turn_id: str | None = None,
    label: str | None = None,
) -> ForkResult:
    """Fork ``source_record_id`` at ``until_seq`` into a fresh record.

    The seed is persisted up front with provenance and per-message kinds
    (the loop's host-view reconciliation needs them to keep forked records
    continuous); the forked record is self-contained — resume never
    dereferences the source. Raises ``KeyError`` when the source record
    has no entries.
    """

    items = await load_history_items(storage, source_record_id, until_seq=until_seq)
    if items is None:
        raise KeyError(f"record not found: {source_record_id}")
    messages = [item.message for item in items]
    record_id = new_record_id or f"{source_record_id}:fork:{uuid.uuid4().hex[:12]}"
    seed = HistorySeed(
        seq=0,
        messages=tuple(messages),
        token_estimate=estimate_tokens(messages),
        source_record_id=source_record_id,
        source_until_seq=until_seq,
        message_kinds=tuple(item.kind for item in items),
        turn_id=turn_id,
    )
    await storage.append_history(record_id, [entry_to_dict(seed)])
    return ForkResult(
        point=BranchPoint(
            record_id=record_id,
            source_record_id=source_record_id,
            source_until_seq=until_seq,
            label=label or branch_label(messages),
        ),
        messages=messages,
    )


async def resolve_fork_seq(
    storage: "StorageAdapter",
    record_id: str,
    *,
    before_last_user: bool = False,
    user_index: int | None = None,
) -> int | None:
    """Resolve a semantic fork address to a record seq.

    ``before_last_user=True`` is the regenerate address: the seq of the
    newest ``user`` item, so forking there keeps the prompting turn and
    drops the assistant reply being regenerated. ``user_index=K`` addresses
    the K-th (0-based) user message — hosts that think in messages (the
    desktop's regenerate) locate their truncation point as a user-message
    ordinal, and the record's user items align with the host's (steer
    injections are their own kind, not counted). Seed-contained user
    messages count toward the ordinal, but a K landing INSIDE a seed
    returns ``None`` — a seed is indivisible, so the host falls back
    (re-forking inside a forked prefix is the source record's business).
    ``None`` is also returned when the addressed item doesn't exist. With
    no flags, returns the newest seq (fork at tip).
    """

    if user_index is not None:
        if user_index < 0:
            return None
        seen = 0
        cursor: int | None = None
        while True:
            page = await storage.list_history(
                record_id, after_seq=cursor, limit=_FORK_SCAN_PAGE
            )
            if not page:
                return None
            for raw in page:
                if raw.get("entry") == "seed":
                    entry = entry_from_dict(raw)
                    assert isinstance(entry, HistorySeed)
                    kinds = entry.message_kinds or tuple(
                        m.role for m in entry.messages
                    )
                    count = sum(1 for k in kinds if k == KIND_USER)
                    if seen + count > user_index:
                        return None  # inside the seed: indivisible
                    seen += count
                    continue
                if raw.get("kind") == KIND_USER:
                    if seen == user_index:
                        return int(raw["seq"])
                    seen += 1
            if len(page) < _FORK_SCAN_PAGE:
                return None
            cursor = int(page[-1]["seq"])

    cursor = None
    newest_seq: int | None = None
    while True:
        page = await storage.list_history(
            record_id, until_seq=cursor, limit=_FORK_SCAN_PAGE, reverse=True
        )
        if not page:
            return None if newest_seq is None else newest_seq
        if newest_seq is None:
            newest_seq = int(page[0]["seq"])
            if not before_last_user:
                return newest_seq
        for raw in page:
            if raw.get("kind") == KIND_USER:
                return int(raw["seq"])
        if len(page) < _FORK_SCAN_PAGE:
            return None
        cursor = int(page[-1]["seq"]) - 1


async def lineage(
    storage: "StorageAdapter",
    record_id: str,
    *,
    max_depth: int = _MAX_LINEAGE_DEPTH,
) -> list[BranchPoint]:
    """Walk the fork-provenance chain upwards; returns root-first order
    including ``record_id`` itself as the last element.

    Each hop reads the record's first entry: a ``HistorySeed`` with
    ``source_record_id`` names the parent. A record with no seed (or an
    embedder-assembled seed without provenance) is a root. Cycles and
    over-deep chains raise ``ValueError`` — both are corruption, not data.
    """

    chain: list[BranchPoint] = []
    seen: set[str] = set()
    current: str | None = record_id
    while current is not None:
        if current in seen:
            raise ValueError(f"fork lineage cycle at record {current}")
        seen.add(current)
        if len(chain) >= max_depth:
            raise ValueError(
                f"fork lineage deeper than {max_depth} at record {current}"
            )
        first = await storage.list_history(current, limit=1)
        parent: str | None = None
        parent_seq: int | None = None
        label = "root"
        if first:
            entry = entry_from_dict(first[0])
            if isinstance(entry, HistorySeed):
                if entry.source_record_id is not None:
                    parent = entry.source_record_id
                    parent_seq = entry.source_until_seq
                label = branch_label(list(entry.messages))
        chain.append(
            BranchPoint(
                record_id=current,
                source_record_id=parent,
                source_until_seq=parent_seq,
                label=label,
            )
        )
        current = parent
    chain.reverse()
    return [
        BranchPoint(
            record_id=point.record_id,
            source_record_id=point.source_record_id,
            source_until_seq=point.source_until_seq,
            label=point.label,
            depth=index,
        )
        for index, point in enumerate(chain)
    ]
