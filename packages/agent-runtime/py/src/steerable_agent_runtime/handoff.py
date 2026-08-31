"""Structured handoff (W3.3): the exportable context-reset artifact.

``HistorySeed`` + ``CompactionBoundary`` already make the record
resettable; this module makes the reset *portable*. A ``HandoffBundle``
is the visible projection at export time, serialized as one versioned
JSON document with provenance — the file another process (or another
host, or a future session) rebuilds a session from.

The industry lesson encoded here: on very long tasks, summary-style
compaction is not enough — a full context reset must be possible. The
bundle is that reset point: teardown discards the working record, and
``seed_from_handoff`` rebuilds a fresh record whose projection is exactly
the bundle's contents. The source record is never mutated or read at
rebuild time — the bundle is self-contained.

Fail-closed like the durable record: a bundle written by a newer build
(newer ``HANDOFF_FORMAT_VERSION``) is refused whole, not partially read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .history import (
    ContextManager,
    HistorySeed,
    message_from_dict,
    message_to_dict,
)
from .llm import LLMMessage

#: Bundle format version. Bump only on structural change to the envelope.
HANDOFF_FORMAT_VERSION = 1


class HandoffFormatError(ValueError):
    """A handoff bundle this build cannot read (fail-closed)."""


@dataclass(frozen=True, slots=True)
class HandoffBundle:
    """The visible projection plus provenance, as one portable document."""

    messages: tuple[LLMMessage, ...]
    #: Per-message content kinds, parallel to ``messages``; empty when the
    #: visible span contains seed-expanded messages whose kinds are not all
    #: known (consumers fall back to role-derived kinds, same as HistorySeed).
    message_kinds: tuple[str, ...]
    token_estimate: int
    source_record_id: str | None
    source_until_seq: int | None
    exported_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": HANDOFF_FORMAT_VERSION,
            "messages": [message_to_dict(m) for m in self.messages],
            "message_kinds": list(self.message_kinds),
            "token_estimate": self.token_estimate,
            "source_record_id": self.source_record_id,
            "source_until_seq": self.source_until_seq,
            "exported_at": self.exported_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> HandoffBundle:
        version = data.get("v")
        if (
            not isinstance(version, int)
            or version < 1
            or version > HANDOFF_FORMAT_VERSION
        ):
            raise HandoffFormatError(
                f"unsupported handoff format version: {version!r} "
                f"(this build reads 1..{HANDOFF_FORMAT_VERSION})"
            )
        messages = tuple(message_from_dict(m) for m in data.get("messages") or ())
        kinds = tuple(str(k) for k in data.get("message_kinds") or ())
        if kinds and len(kinds) != len(messages):
            raise HandoffFormatError(
                f"message_kinds length {len(kinds)} != messages length {len(messages)}"
            )
        return HandoffBundle(
            messages=messages,
            message_kinds=kinds,
            token_estimate=int(data.get("token_estimate") or 0),
            source_record_id=data.get("source_record_id"),
            source_until_seq=data.get("source_until_seq"),
            exported_at=str(data.get("exported_at") or ""),
        )


def export_handoff(
    manager: ContextManager,
    *,
    source_record_id: str | None = None,
) -> HandoffBundle:
    """Snapshot the manager's visible projection as a portable bundle."""
    messages = tuple(manager.projection)
    items = manager.projection_items
    # Kinds are only fully known when the visible span is plain items (no
    # seed-expanded stretch); otherwise declare unknown.
    kinds = tuple(item.kind for item in items) if len(items) == len(messages) else ()
    return HandoffBundle(
        messages=messages,
        message_kinds=kinds,
        token_estimate=manager.projection_token_estimate,
        source_record_id=source_record_id,
        source_until_seq=manager.record[-1].seq if manager.record else None,
        exported_at=datetime.now(tz=timezone.utc).isoformat(),
    )


def seed_from_handoff(
    manager: ContextManager, bundle: HandoffBundle
) -> HistorySeed:
    """Rebuild a fresh record from a bundle: the full context reset.

    The bundle's messages land as ONE self-contained seed entry — the
    reset session's projection equals the bundle byte for byte, and resume
    never touches the source record.
    """
    return manager.seed(
        bundle.messages,
        source_record_id=bundle.source_record_id,
        source_until_seq=bundle.source_until_seq,
    )


def write_handoff(bundle: HandoffBundle, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_handoff(path: str | Path) -> HandoffBundle:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffFormatError(f"unreadable handoff at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HandoffFormatError(f"handoff at {path} is not an object")
    return HandoffBundle.from_dict(data)
