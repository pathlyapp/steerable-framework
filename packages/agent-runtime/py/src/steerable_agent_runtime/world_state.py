"""World-state sections with RFC 7386 merge-patch diffing.

World state is the slow-changing context a host wants the model to know —
current time, workspace root, git branch, feature flags. The naive host
rebuilds the system prompt with fresh values every turn, which busts the
prompt cache at the very prefix. This module keeps the prefix byte-stable:
the full state is injected once as a ``<world-state>`` fragment; later
turns inject nothing when nothing changed, and a small tail
``<world-state-patch>`` fragment — an RFC 7386 JSON merge patch — when
something did. An unchanged section costs zero tokens; a changed one costs
a small tail patch.

Each fragment embeds the full snapshot (base64url JSON comment) so the
next turn — or a resumed/forked session — diffs against what the model
has actually seen, with no side channel. If compaction folds the last
world-state fragment, the next turn re-injects the full state: correct by
construction.

Sections are pure data: ``id`` (stable — it is persisted in the record)
plus ``snapshot()`` (JSON-able comparison data, kept small). Rendering is
uniform JSON so the framework owns the format end to end.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .history import ContextFragment
from .hooks import NoopHooks, PreStepAction, TranscriptAppend
from .llm import LLMMessage
from .tokens import estimate_text_tokens

# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


@runtime_checkable
class WorldStateSection(Protocol):
    """A typed slice of model-visible world state.

    ``id`` is stable and persisted in the record. ``snapshot()`` returns
    JSON-able comparison data — only what decides what the model must be
    told next, and small: it rides inside every injected fragment. None
    object fields are stripped before diffing: merge-patch nulls mean
    deletion, so a null value is unrepresentable — absent carries the same
    meaning for world state.
    """

    @property
    def id(self) -> str: ...

    def snapshot(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class StaticWorldStateSection:
    """A section over a plain data value — the shape hosts pass over the
    wire (``worldState`` param) when the value is computed host-side."""

    section_id: str
    value: Any

    @property
    def id(self) -> str:
        return self.section_id

    def snapshot(self) -> Any:
        return self.value


# ---------------------------------------------------------------------------
# RFC 7386 merge patch
# ---------------------------------------------------------------------------


def apply_merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 application: a non-object patch replaces the target
    wholesale; an object patch merges key-wise, null values delete."""
    if not isinstance(patch, dict):
        return patch
    base = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = apply_merge_patch(base.get(key), value)
    return base


def _strip_nulls(value: Any) -> Any:
    """Drop None object fields recursively (merge-patch nulls mean
    deletion). List elements are kept — arrays replace wholesale, so a
    null inside a list is representable."""
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_nulls(v) for v in value]
    return value


def merge_patch(previous: Any, current: Any) -> Any | None:
    """The RFC 7386 patch advancing ``previous`` to ``current``.

    Returns None when the two are equal (no patch needed). Objects diff
    key-wise (keys only in ``previous`` map to null); any other unequal
    pair replaces wholesale — arrays are replaced, never merged.
    """
    if previous == current:
        return None
    if isinstance(previous, dict) and isinstance(current, dict):
        patch: dict[str, Any] = {}
        for key in previous:
            if key not in current:
                patch[key] = None
        for key, value in current.items():
            if key not in previous:
                patch[key] = value
            else:
                sub = merge_patch(previous[key], value)
                if sub is not None:
                    patch[key] = sub
        return patch
    return current


# ---------------------------------------------------------------------------
# Fragments
# ---------------------------------------------------------------------------

#: HTML-comment tag carrying the full snapshot inside every fragment.
_SNAPSHOT_COMMENT = "world-state-snapshot:"

#: Aggregate bound on one injected world-state fragment (Wave 4, W4-7).
#: Sections are meant to be small comparison data; a host that stuffs a
#: whole file listing into one would otherwise inject an unbounded blob
#: (codex's hook-output hard-cap counterpart for this surface). An
#: oversized section is replaced by a marker object — the injection stays
#: bounded and the omission is visible to the model and the trace.
DEFAULT_MAX_SECTION_BYTES = 8_192

_PATCH_PREAMBLE = (
    "RFC 7386 JSON merge patch against the world state: null deletes a "
    "section, objects merge recursively, any other value replaces."
)


def _json(value: Any) -> str:
    """Deterministic JSON — byte-stable rendering for identical states."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _encode_snapshot(snapshot: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(_json(dict(snapshot)).encode("utf-8")).decode("ascii")


def _decode_snapshot(text: str) -> dict[str, Any] | None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("<!--") and _SNAPSHOT_COMMENT in line:
            payload = line.split(_SNAPSHOT_COMMENT, 1)[1].split("-->", 1)[0].strip()
            try:
                data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
            except (ValueError, binascii.Error):
                return None
            return data if isinstance(data, dict) else None
    return None


class WorldStateFragment(ContextFragment):
    """Full world-state injection — first turn, or after the record lost
    the previous snapshot (e.g. compaction folded it)."""

    content_kind = "world_state.snapshot"
    max_tokens = 4096
    review_note = (
        "Aggregate cap over the per-section 8 KiB byte caps; backstops a "
        "host registering many sections. Reviewed 2026-08-30 (P2.2)."
    )

    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        self._snapshot = dict(snapshot)

    @classmethod
    def type_markers(cls) -> tuple[str, str]:
        return ("<world-state>", "</world-state>")

    def markers(self) -> tuple[str, str]:
        return self.type_markers()

    def body(self) -> str:
        return (
            f"\n<!-- {_SNAPSHOT_COMMENT}{_encode_snapshot(self._snapshot)} -->\n"
            f"{_json(self._snapshot)}\n"
        )

    def degrade(self, rendered: str, *, max_tokens: int) -> str:
        # Drop trailing whole sections and re-render, keeping the result
        # decodable (closing marker intact, snapshot comment consistent).
        # A single section is byte-capped well under this cap, so the loop
        # converges before empty in any realistic configuration.
        kept = dict(self._snapshot)
        text = rendered
        while kept and estimate_text_tokens(text) > max_tokens:
            kept.pop(next(reversed(kept)))
            text = WorldStateFragment(kept).render()
        return text


class WorldStatePatchFragment(ContextFragment):
    """Tail patch injection — only the sections that changed since the
    last injection, as an RFC 7386 merge patch. Embeds the new full
    snapshot so the next turn diffs against it."""

    content_kind = "world_state.patch"
    max_tokens = 4096
    review_note = (
        "Aggregate cap over the per-section 8 KiB byte caps; backstops a "
        "host registering many sections. Reviewed 2026-08-30 (P2.2)."
    )

    def __init__(self, patch: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
        self._patch = dict(patch)
        self._snapshot = dict(snapshot)

    @classmethod
    def type_markers(cls) -> tuple[str, str]:
        return ("<world-state-patch>", "</world-state-patch>")

    def markers(self) -> tuple[str, str]:
        return self.type_markers()

    def body(self) -> str:
        return (
            f"\n<!-- {_SNAPSHOT_COMMENT}{_encode_snapshot(self._snapshot)} -->\n"
            f"{_PATCH_PREAMBLE}\n"
            f"{_json(self._patch)}\n"
        )

    def degrade(self, rendered: str, *, max_tokens: int) -> str:
        # Drop trailing patch entries while keeping the embedded full
        # snapshot intact — the model sees a partial patch, and the next
        # turn still diffs against the complete snapshot. If the snapshot
        # comment alone exceeds the cap, fall back to plain truncation
        # (closing marker lost → next turn re-injects the full state).
        kept = dict(self._patch)
        while kept:
            text = WorldStatePatchFragment(kept, self._snapshot).render()
            if estimate_text_tokens(text) <= max_tokens:
                return text
            kept.pop(next(reversed(kept)))
        text = WorldStatePatchFragment({}, self._snapshot).render()
        if estimate_text_tokens(text) <= max_tokens:
            return text
        return super().degrade(rendered, max_tokens=max_tokens)


def last_world_state_snapshot(
    transcript: Sequence[LLMMessage],
) -> dict[str, Any] | None:
    """The full snapshot embedded in the most recent world-state fragment
    still visible in the transcript — None when there is none (first turn,
    or compaction folded it)."""
    for message in reversed(transcript):
        text = message.content_text
        if WorldStatePatchFragment.matches_text(text) or WorldStateFragment.matches_text(
            text
        ):
            return _decode_snapshot(text)
    return None


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


class WorldStateHooks(NoopHooks):
    """Injects world state on the first round of each run.

    Round 0 with no visible prior snapshot appends the full state; with
    one, appends only the RFC 7386 delta — and nothing at all when the
    state is unchanged, so a steady-state turn adds zero tokens and the
    cached prefix stays byte-stable. Stateless across runs: everything is
    derived from the transcript, so resume and fork diff correctly.

    Each section's serialized snapshot is bounded at ``max_section_bytes``
    (W4-7): an oversized section is replaced by a marker object so the
    injection stays bounded and the omission is visible.
    """

    def __init__(
        self,
        sections: Sequence[WorldStateSection],
        *,
        max_section_bytes: int = DEFAULT_MAX_SECTION_BYTES,
    ) -> None:
        self._sections = list(sections)
        self._max_section_bytes = max_section_bytes

    def _bounded_snapshot(self, section: WorldStateSection) -> Any:
        value = _strip_nulls(section.snapshot())
        size = len(_json(value).encode("utf-8"))
        if size <= self._max_section_bytes:
            return value
        return {
            "_omitted": True,
            "reason": (
                f"section '{section.id}' snapshot is {size} bytes, over the "
                f"{self._max_section_bytes}-byte cap; not injected"
            ),
        }

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: Any
    ) -> PreStepAction:
        if ctx.round_index != 0 or not self._sections:
            return PreStepAction(kind="proceed")
        current = {
            section.id: self._bounded_snapshot(section) for section in self._sections
        }
        previous = last_world_state_snapshot(transcript)
        if previous is None:
            fragment: ContextFragment = WorldStateFragment(current)
        else:
            patch = merge_patch(previous, current)
            if patch is None:
                return PreStepAction(kind="proceed")
            fragment = WorldStatePatchFragment(patch, current)
        return PreStepAction(
            kind="proceed",
            appends=[
                TranscriptAppend(
                    message=fragment.to_message(),
                    kind=fragment.content_kind,
                    fragment=fragment,
                )
            ],
            append_action="world_state",
        )
