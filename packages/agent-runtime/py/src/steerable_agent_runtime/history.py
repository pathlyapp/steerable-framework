"""Typed append-only history — the model-visible record and its projection.

Wave 1 foundation (docs/roadmap.md "Wave 1 — the foundation"): the
model-visible transcript becomes a *projection* of a typed, append-only
record instead of a mutable ``list[LLMMessage]`` that hooks rewrite in
place.

- ``HistoryItem`` is the envelope: monotonic ``seq``, ``turn_id``, a stable
  ``<feature>.<name>`` content ``kind``, the payload ``LLMMessage``, and a
  ``token_estimate`` computed once at append time with the same estimator
  the compaction layer uses, so bounding and pressure checks never
  recompute.
- ``ContextManager`` owns the record. ``append()`` grows it;
  ``replace_all()`` is the ONLY rewrite path and is itself append-only: it
  records a ``CompactionBoundary`` declaring all prior items superseded,
  then appends the replacement items. ``projection`` renders the
  model-visible transcript — every item after the newest boundary.
- ``ContextFragment`` is the codex ``ContextualUserFragment`` counterpart
  (``codex-rs/context-fragments/src/fragment.rs``): injected content
  carries stable markers so it can recognise its own rendering in retained
  history — resume, compaction, and tests asserting what the model saw.

The record is the single source of truth for what the model saw; the
display event stream stays a separate, lossy view. Durable persistence (a
dedicated StorageAdapter channel) and O(tail) resume land on top of this
module in later Wave 1 steps.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from steerable_agent_protocol.generated import ToolCall

from .llm import LLMMessage, LLMRole
from .llm.parts import ImagePart, TextPart
from .tokens import estimate_text_tokens, estimate_tokens

logger = logging.getLogger(__name__)

#: Content-kind classification, stable ``<feature>.<name>`` strings (the
#: codex ``ContentItemKind`` convention). Base conversational kinds are the
#: bare roles; injected content uses feature-qualified kinds.
KIND_SYSTEM = "system"
KIND_USER = "user"
KIND_ASSISTANT = "assistant"
KIND_TOOL = "tool"
KIND_STEER = "steer.inject"
KIND_COMPACTION_BOUNDARY = "compaction.boundary"
KIND_HISTORY_SEED = "history.seed"

_KIND_BY_ROLE: dict[str, str] = {
    "system": KIND_SYSTEM,
    "user": KIND_USER,
    "assistant": KIND_ASSISTANT,
    "tool": KIND_TOOL,
}


def kind_for_role(role: LLMRole) -> str:
    """The role-derived content kind for unclassified messages."""
    return _KIND_BY_ROLE.get(role, role)


@dataclass(frozen=True, slots=True)
class HistoryItem:
    """One appended envelope in the record. Frozen: items never mutate."""

    seq: int
    kind: str
    message: LLMMessage
    token_estimate: int
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompactionBoundary:
    """Declared-rewrite marker appended by ``ContextManager.replace_all``.

    Everything before this entry in the record is superseded: it stays in
    the record (append-only, auditable) but is invisible to the projection.
    ``action`` is the ``hook_action`` label of the rewriter (``"compact"``,
    ``"overflow_recovery"``, …) so traces and assertions can attribute the
    boundary without guessing.

    ``replacement_count`` is the number of messages the rewrite appended
    right after this boundary. It lets the host-seed reconciliation see
    through the loop's OWN compactions (``compact`` / ``overflow_recovery``):
    the rewrite re-states content the host already had, so the host's next
    raw seed still matches the pre-compaction conversation and the loop
    keeps its compaction instead of discarding it as a spurious
    ``host_revision`` (W6-10). ``None`` for records written before this
    field existed — those fall back to treating the boundary as opaque.
    """

    seq: int
    reason: str
    action: str = "compact"
    turn_id: str | None = None
    kind: str = KIND_COMPACTION_BOUNDARY
    replacement_count: int | None = None


@dataclass(frozen=True, slots=True)
class HistorySeed:
    """An inline-seeded prefix — the fork/regenerate primitive.

    Carries a projected prefix copied from a source record (or assembled by
    the embedder) as ONE entry, with provenance. Projection expands it
    inline: a forked record reads as ``[seed, live items…]`` and never
    dereferences the source record — the seed is self-contained.

    ``source_record_id`` / ``source_until_seq`` name where the prefix came
    from (None for an embedder-assembled seed); they are audit metadata,
    never read at projection time.

    ``message_kinds`` carries the per-message content kinds of the seeded
    prefix (parallel to ``messages``) when the source is known — the
    host-view reconciliation in the loop needs them to tell loop-owned
    injections (``world_state.*``, ``skills.catalog``, tool rounds) from
    host-echoed messages. Empty means unknown; consumers fall back to
    role-derived kinds.
    """

    seq: int
    messages: tuple[LLMMessage, ...]
    token_estimate: int
    source_record_id: str | None = None
    source_until_seq: int | None = None
    turn_id: str | None = None
    kind: str = KIND_HISTORY_SEED
    message_kinds: tuple[str, ...] = ()


#: The record is a linear log of items, declared-rewrite markers, and seeds.
RecordEntry = HistoryItem | CompactionBoundary | HistorySeed

#: Durable record format version. v1 is the pre-versioning shape (no ``v``
#: key); writers always stamp ``v: 2``. Bump only on a structural change to
#: the envelope shapes below — never for additive content kinds.
RECORD_FORMAT_VERSION = 2


class RecordFormatError(ValueError):
    """A durable record entry this build cannot read (fail-closed).

    Raised by ``entry_from_dict`` for a missing/unsupported ``v`` or an
    unknown envelope discriminant. Deliberately a ``ValueError`` subclass so
    existing ``except ValueError`` callers keep working; the class name is
    the machine-greppable signal that the record — not the input — is at
    fault. There is no skip-and-continue read path: a record this build
    cannot fully read is refused whole rather than silently truncated
    (dsh's required-on-read stance; the ``ignorable`` field it deleted is
    not adopted).
    """


#: Default per-fragment token cap — the no-review line. Fragments stay
#: under it silently; crossing it requires an explicit ``review_note`` on
#: the class (the Codex "P0 items crossing 1k tokens" rule, gated in tests).
DEFAULT_FRAGMENT_MAX_TOKENS = 1024

#: Absolute per-fragment ceiling. No injected item may exceed this,
#: reviewed or not (the Codex "no items larger than 10K tokens" rule).
FRAGMENT_TOKEN_CEILING = 10_000

#: Appended by the default degradation when a fragment exceeds its cap.
_TRUNCATION_MARKER = "\n…[fragment truncated: exceeded its token cap]"


class ContextFragment:
    """Injected content with a stable, self-recognisable rendering.

    ``markers()`` returns the ``(start, end)`` pair that ``render()`` wraps
    around ``body()``. Three modes:

    - both empty: unmarked fragment — renders as the bare body and never
      self-matches (for content that must stay byte-identical to what a
      user would have typed);
    - start only: prefix notice — ``matches_text`` recognises retained text
      starting with the marker (the house style for ``[system notice] …``
      injections);
    - both: wrapped block — recognised when the retained text starts and
      ends with the markers (codex's ``matches_marked_text`` semantics).

    Subclasses with constant markers should also override ``type_markers``
    so ``matches_text`` works without an instance.

    Every fragment is bounded: ``append_fragment`` enforces the class's
    token cap via ``degrade`` — an over-cap injection degrades predictably
    instead of inflating the transcript and leaning on compaction later.
    """

    role: LLMRole = "user"
    content_kind: str = "generic"
    #: Hard token cap for the rendered text. ``None`` selects
    #: ``DEFAULT_FRAGMENT_MAX_TOKENS``. Values above the default require
    #: ``review_note``; values above ``FRAGMENT_TOKEN_CEILING`` are rejected
    #: by the gate test.
    max_tokens: int | None = None
    #: Explicit-review record required when ``max_tokens`` crosses the
    #: no-review line: why this fragment legitimately needs the larger cap.
    review_note: str | None = None

    @classmethod
    def effective_max_tokens(cls) -> int:
        return cls.max_tokens if cls.max_tokens is not None else DEFAULT_FRAGMENT_MAX_TOKENS

    def degrade(self, rendered: str, *, max_tokens: int) -> str:
        """Reduce an over-cap rendering to fit ``max_tokens``.

        The default truncates with a visible marker; fragments with line or
        section structure override this to drop whole units instead of
        cutting mid-line.
        """
        marker = _TRUNCATION_MARKER
        budget = max_tokens - estimate_text_tokens(marker)
        if budget <= 0:
            return marker.strip()
        text = rendered
        # Proportional first cut, then refine against the estimator — the
        # estimator is monotonic in length, so this converges in a few steps.
        estimate = estimate_text_tokens(text)
        if estimate > budget:
            text = text[: max(0, int(len(text) * (budget / estimate) * 0.95))]
        while text and estimate_text_tokens(text) > budget:
            text = text[: max(0, int(len(text) * 0.9))]
        return text + marker

    def markers(self) -> tuple[str, str]:
        return ("", "")

    def body(self) -> str:
        raise NotImplementedError

    def render(self) -> str:
        start, end = self.markers()
        body = self.body()
        if not start and not end:
            return body
        return f"{start}{body}{end}"

    def to_message(
        self, *, name: str | None = None, tool_call_id: str | None = None
    ) -> LLMMessage:
        return LLMMessage.text_of(
            self.role,
            self.render(),
            name=name,
            tool_call_id=tool_call_id,
        )

    @classmethod
    def type_markers(cls) -> tuple[str, str]:
        return ("", "")

    @classmethod
    def matches_text(cls, text: str) -> bool:
        start, end = cls.type_markers()
        if not start and not end:
            return False
        trimmed = text.strip()
        if start and not trimmed.lower().startswith(start.lower()):
            return False
        return not (end and not trimmed.lower().endswith(end.lower()))


class ContextManager:
    """Owns the append-only record; projects the model-visible transcript.

    The loop seeds it with the incoming messages and funnels every
    transcript mutation through it. ``projection`` is the only read the
    provider ever sees; ``record`` keeps the full history including
    superseded spans for audit, resume, and the prompt-invariant
    assertions.
    """

    def __init__(
        self,
        messages: Iterable[LLMMessage] = (),
        *,
        turn_id: str | None = None,
        token_model: str | None = None,
        first_seq: int = 0,
    ) -> None:
        self.turn_id = turn_id or uuid.uuid4().hex
        #: Model name for calibrated per-item token estimates (tokens.py).
        self._token_model = token_model
        self._record: list[RecordEntry] = []
        self._next_seq = first_seq
        self._boundary_index = -1
        #: Entries appended since the last ``drain_pending`` — the loop
        #: flushes them to the durable channel at request/turn boundaries.
        self._pending: list[RecordEntry] = []
        for message in messages:
            self.append(message)

    def _estimate(self, message: LLMMessage) -> int:
        return estimate_tokens([message], model=self._token_model)

    def append(
        self,
        message: LLMMessage,
        *,
        kind: str | None = None,
        turn_id: str | None = None,
    ) -> HistoryItem:
        """Append one message to the record. The only growth path."""
        item = HistoryItem(
            seq=self._next_seq,
            kind=kind or _KIND_BY_ROLE.get(message.role, message.role),
            message=message,
            token_estimate=self._estimate(message),
            turn_id=turn_id or self.turn_id,
        )
        self._record.append(item)
        self._pending.append(item)
        self._next_seq += 1
        return item

    def append_fragment(
        self,
        fragment: ContextFragment,
        *,
        name: str | None = None,
        tool_call_id: str | None = None,
        turn_id: str | None = None,
    ) -> HistoryItem:
        """Render a fragment and append it under its ``content_kind``.

        Enforces the fragment's token cap: an over-cap rendering is degraded
        (``fragment.degrade``) before it lands, so the record — and therefore
        the provider request — never carries an unbounded injection.
        """
        rendered = fragment.render()
        cap = fragment.effective_max_tokens()
        if estimate_text_tokens(rendered) > cap:
            degraded = fragment.degrade(rendered, max_tokens=cap)
            logger.warning(
                "fragment %s exceeded its %d-token cap; degraded before append",
                fragment.content_kind,
                cap,
            )
            message = LLMMessage.text_of(
                fragment.role, degraded, name=name, tool_call_id=tool_call_id
            )
        else:
            message = fragment.to_message(name=name, tool_call_id=tool_call_id)
        return self.append(
            message,
            kind=fragment.content_kind,
            turn_id=turn_id,
        )

    def replace_all(
        self,
        messages: Iterable[LLMMessage],
        *,
        reason: str,
        action: str = "compact",
        turn_id: str | None = None,
    ) -> CompactionBoundary:
        """The single declared rewrite path — itself append-only.

        Appends a ``CompactionBoundary`` superseding every prior entry,
        then appends the replacement messages as new items. The projection
        changes; the record only grows.
        """
        replacements = list(messages)
        boundary = CompactionBoundary(
            seq=self._next_seq,
            reason=reason,
            action=action,
            turn_id=turn_id or self.turn_id,
            replacement_count=len(replacements),
        )
        self._record.append(boundary)
        self._pending.append(boundary)
        self._next_seq += 1
        self._boundary_index = len(self._record) - 1
        for message in replacements:
            self.append(message, turn_id=turn_id)
        return boundary

    def seed(
        self,
        messages: Iterable[LLMMessage],
        *,
        source_record_id: str | None = None,
        source_until_seq: int | None = None,
        turn_id: str | None = None,
    ) -> HistorySeed:
        """Inline-seed a projected prefix as ONE entry (fork/regenerate).

        The seed is visible to the projection (it lands after the newest
        boundary — on a fresh record, at the head) and self-contained:
        resume of the forked record never touches the source.
        """
        seeded = tuple(messages)
        entry = HistorySeed(
            seq=self._next_seq,
            messages=seeded,
            token_estimate=estimate_tokens(list(seeded), model=self._token_model),
            source_record_id=source_record_id,
            source_until_seq=source_until_seq,
            turn_id=turn_id or self.turn_id,
        )
        self._record.append(entry)
        self._pending.append(entry)
        self._next_seq += 1
        return entry

    def drain_pending(self) -> list[RecordEntry]:
        """Return and clear the entries appended since the last drain.

        The loop drains at request/turn boundaries and persists the batch to
        the durable channel; the manager itself stays sync and storage-free.
        """
        pending = self._pending
        self._pending = []
        return pending

    def mark_persisted_prefix(self, count: int) -> None:
        """Drop the first ``count`` pending entries — they are already durable.

        The continuous per-chat log (decision ② of the W1 design): a new
        turn's seed messages are the previous turn's projection plus the new
        user message; the loop verifies the prefix against the durable
        record and marks it here, so only genuinely new entries flush.
        """
        if count < 0 or count > len(self._pending):
            raise ValueError(
                f"persisted prefix {count} outside pending span {len(self._pending)}"
            )
        self._pending = self._pending[count:]

    @property
    def record(self) -> list[RecordEntry]:
        """The full append-only log, including superseded spans."""
        return list(self._record)

    @property
    def projection(self) -> list[LLMMessage]:
        """The model-visible transcript: every item after the newest boundary."""
        out: list[LLMMessage] = []
        for entry in self._record[self._boundary_index + 1 :]:
            if isinstance(entry, HistoryItem):
                out.append(entry.message)
            elif isinstance(entry, HistorySeed):
                out.extend(entry.messages)
        return out

    @property
    def projection_items(self) -> list[HistoryItem]:
        """The visible items (envelopes, not bare messages)."""
        return [
            entry
            for entry in self._record[self._boundary_index + 1 :]
            if isinstance(entry, HistoryItem)
        ]

    @property
    def latest_boundary(self) -> CompactionBoundary | None:
        if self._boundary_index < 0:
            return None
        entry = self._record[self._boundary_index]
        return entry if isinstance(entry, CompactionBoundary) else None

    @property
    def projection_token_estimate(self) -> int:
        """Sum of per-entry estimates for the visible span (no recompute)."""
        return sum(
            entry.token_estimate
            for entry in self._record[self._boundary_index + 1 :]
            if isinstance(entry, (HistoryItem, HistorySeed))
        )


# ---------------------------------------------------------------------------
# Durable serialization — the record channel's JSON shape
# ---------------------------------------------------------------------------
#
# Entries persist as plain JSON dicts (the SQLAlchemy reference store keeps
# them in a JSON column). The shape is runtime-fidelity — full content
# parts, tool calls, and estimates — NOT the lossy display/record shape of
# recording.py. ``entry`` is the envelope discriminant; ``kind`` stays the
# content classification.


def message_to_dict(message: LLMMessage) -> dict[str, Any]:
    """Full-fidelity message serialization for the durable record."""
    content: list[dict[str, Any]] = []
    for part in message.content:
        if isinstance(part, TextPart):
            content.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            content.append(
                {
                    "type": "image",
                    "source": part.source,
                    "is_url": part.is_url,
                    "media_type": part.media_type,
                }
            )
    out: dict[str, Any] = {"role": message.role, "content": content}
    if message.name is not None:
        out["name"] = message.name
    if message.tool_call_id is not None:
        out["tool_call_id"] = message.tool_call_id
    if message.tool_calls is not None:
        out["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments or {}}
            for call in message.tool_calls
        ]
    if message.reasoning:
        out["reasoning"] = message.reasoning
    if message.reasoning_details:
        out["reasoning_details"] = message.reasoning_details
    return out


def message_from_dict(data: dict[str, Any]) -> LLMMessage:
    """Inverse of ``message_to_dict``."""
    content = []
    for part in data.get("content") or []:
        ptype = part.get("type")
        if ptype == "text":
            content.append(TextPart(text=str(part.get("text") or "")))
        elif ptype == "image":
            content.append(
                ImagePart(
                    source=str(part.get("source") or ""),
                    is_url=bool(part.get("is_url")),
                    media_type=str(part.get("media_type") or "image/png"),
                )
            )
    tool_calls = data.get("tool_calls")
    return LLMMessage(
        role=data["role"],
        content=content,
        name=data.get("name"),
        tool_call_id=data.get("tool_call_id"),
        tool_calls=(
            [
                ToolCall(
                    id=str(call.get("id") or ""),
                    name=str(call.get("name") or ""),
                    arguments=call.get("arguments") or {},
                )
                for call in tool_calls
            ]
            if tool_calls is not None
            else None
        ),
        reasoning=data.get("reasoning"),
        reasoning_details=data.get("reasoning_details"),
    )


def entry_to_dict(entry: RecordEntry) -> dict[str, Any]:
    """Serialize one record entry for ``StorageAdapter.append_history``."""
    if isinstance(entry, HistoryItem):
        return {
            "entry": "item",
            "v": RECORD_FORMAT_VERSION,
            "seq": entry.seq,
            "kind": entry.kind,
            "turn_id": entry.turn_id,
            "token_estimate": entry.token_estimate,
            "message": message_to_dict(entry.message),
        }
    if isinstance(entry, CompactionBoundary):
        out: dict[str, Any] = {
            "entry": "boundary",
            "v": RECORD_FORMAT_VERSION,
            "seq": entry.seq,
            "kind": entry.kind,
            "turn_id": entry.turn_id,
            "reason": entry.reason,
            "action": entry.action,
        }
        # Additive + optional: omitted when None so older readers (which
        # ignore unknown keys) and older records (which lack it) both work.
        if entry.replacement_count is not None:
            out["replacement_count"] = entry.replacement_count
        return out
    if isinstance(entry, HistorySeed):
        return {
            "entry": "seed",
            "v": RECORD_FORMAT_VERSION,
            "seq": entry.seq,
            "kind": entry.kind,
            "turn_id": entry.turn_id,
            "token_estimate": entry.token_estimate,
            "source_record_id": entry.source_record_id,
            "source_until_seq": entry.source_until_seq,
            "messages": [message_to_dict(m) for m in entry.messages],
            "message_kinds": list(entry.message_kinds),
        }
    raise TypeError(f"unknown record entry type: {type(entry).__name__}")


def entry_from_dict(data: dict[str, Any]) -> RecordEntry:
    """Inverse of ``entry_to_dict``; fail-closed on unreadable shapes.

    Version gate first: a missing ``v`` is the pre-versioning v1 shape
    (accepted); a ``v`` newer than this build's ``RECORD_FORMAT_VERSION``
    means the record was written by a newer build — refuse it whole rather
    than guess (the desktop-downgrade case). An unknown ``entry``
    discriminant is refused the same way. Both raise ``RecordFormatError``.
    """
    version = data.get("v", 1)
    if not isinstance(version, int) or version < 1 or version > RECORD_FORMAT_VERSION:
        raise RecordFormatError(
            f"unsupported record format version: {version!r} "
            f"(this build reads v1..v{RECORD_FORMAT_VERSION})"
        )
    envelope = data.get("entry")
    if envelope == "item":
        return HistoryItem(
            seq=int(data["seq"]),
            kind=str(data["kind"]),
            message=message_from_dict(data["message"]),
            token_estimate=int(data.get("token_estimate") or 0),
            turn_id=data.get("turn_id"),
        )
    if envelope == "boundary":
        replacement_count = data.get("replacement_count")
        return CompactionBoundary(
            seq=int(data["seq"]),
            reason=str(data.get("reason") or ""),
            action=str(data.get("action") or "compact"),
            turn_id=data.get("turn_id"),
            replacement_count=(
                int(replacement_count) if replacement_count is not None else None
            ),
        )
    if envelope == "seed":
        return HistorySeed(
            seq=int(data["seq"]),
            messages=tuple(message_from_dict(m) for m in data.get("messages") or []),
            token_estimate=int(data.get("token_estimate") or 0),
            source_record_id=data.get("source_record_id"),
            source_until_seq=data.get("source_until_seq"),
            turn_id=data.get("turn_id"),
            message_kinds=tuple(str(k) for k in data.get("message_kinds") or ()),
        )
    raise RecordFormatError(f"unknown record entry envelope: {envelope!r}")


@runtime_checkable
class HistoryStore(Protocol):
    """The durable record channel the loop depends on.

    Structurally satisfied by ``StorageAdapter``; kept narrow so a product
    can persist the record without adopting the full storage interface.
    Entries are the JSON dicts from ``entry_to_dict``.
    """

    async def append_history(
        self, record_id: str, entries: Iterable[dict[str, Any]]
    ) -> None: ...

    async def list_history(
        self,
        record_id: str,
        *,
        after_seq: int | None = None,
        until_seq: int | None = None,
        limit: int | None = None,
        reverse: bool = False,
    ) -> list[dict[str, Any]]: ...
