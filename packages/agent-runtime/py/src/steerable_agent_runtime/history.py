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

import uuid
from dataclasses import dataclass
from typing import Iterable

from .llm import LLMMessage, LLMRole
from .tokens import estimate_tokens

#: Content-kind classification, stable ``<feature>.<name>`` strings (the
#: codex ``ContentItemKind`` convention). Base conversational kinds are the
#: bare roles; injected content uses feature-qualified kinds.
KIND_SYSTEM = "system"
KIND_USER = "user"
KIND_ASSISTANT = "assistant"
KIND_TOOL = "tool"
KIND_STEER = "steer.inject"
KIND_COMPACTION_BOUNDARY = "compaction.boundary"

_KIND_BY_ROLE: dict[str, str] = {
    "system": KIND_SYSTEM,
    "user": KIND_USER,
    "assistant": KIND_ASSISTANT,
    "tool": KIND_TOOL,
}


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
    """

    seq: int
    reason: str
    action: str = "compact"
    turn_id: str | None = None
    kind: str = KIND_COMPACTION_BOUNDARY


#: The record is a linear log of items and declared-rewrite markers.
RecordEntry = HistoryItem | CompactionBoundary


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
    """

    role: LLMRole = "user"
    content_kind: str = "generic"

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
        return LLMMessage(
            role=self.role,
            content=self.render(),
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
        if end and not trimmed.lower().endswith(end.lower()):
            return False
        return True


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
        """Render a fragment and append it under its ``content_kind``."""
        return self.append(
            fragment.to_message(name=name, tool_call_id=tool_call_id),
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
        boundary = CompactionBoundary(
            seq=self._next_seq,
            reason=reason,
            action=action,
            turn_id=turn_id or self.turn_id,
        )
        self._record.append(boundary)
        self._next_seq += 1
        self._boundary_index = len(self._record) - 1
        for message in messages:
            self.append(message, turn_id=turn_id)
        return boundary

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
        """Sum of per-item estimates for the visible span (no recompute)."""
        return sum(item.token_estimate for item in self.projection_items)
