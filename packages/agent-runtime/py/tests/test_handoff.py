"""W3.3: structured handoff — export, version gate, full context reset."""

from __future__ import annotations

import json

import pytest

from steerable_agent_runtime.handoff import (
    HANDOFF_FORMAT_VERSION,
    HandoffBundle,
    HandoffFormatError,
    export_handoff,
    read_handoff,
    seed_from_handoff,
    write_handoff,
)
from steerable_agent_runtime.history import ContextManager
from steerable_agent_runtime.llm import LLMMessage


def _manager_with_history() -> ContextManager:
    manager = ContextManager(turn_id="turn-1")
    manager.append(LLMMessage.text_of("system", "sys"))
    manager.append(LLMMessage.text_of("user", "fix the bug"))
    manager.append(LLMMessage.text_of("assistant", "reading"))
    manager.append(
        LLMMessage.text_of("tool", '{"success": true}', name="read_file", tool_call_id="c1")
    )
    manager.append(LLMMessage.text_of("assistant", "done"))
    return manager


def test_export_captures_projection_with_kinds_and_provenance() -> None:
    manager = _manager_with_history()
    bundle = export_handoff(manager, source_record_id="rec-1")
    assert [m.role for m in bundle.messages] == [
        "system", "user", "assistant", "tool", "assistant",
    ]
    assert len(bundle.message_kinds) == len(bundle.messages)
    assert bundle.token_estimate > 0
    assert bundle.source_record_id == "rec-1"
    assert bundle.source_until_seq is not None
    assert bundle.exported_at


def test_full_context_reset_round_trip() -> None:
    """3.3.2: tear down the session, rebuild from the handoff file — the
    rebuilt projection equals the original byte for byte."""
    original = _manager_with_history()
    bundle = export_handoff(original, source_record_id="rec-1")

    # Teardown: the original manager is discarded; only the bundle survives.
    rebuilt = ContextManager(turn_id="turn-2")
    seed = seed_from_handoff(rebuilt, bundle)

    assert rebuilt.projection == original.projection
    assert seed.source_record_id == "rec-1"
    # The reset record is self-contained: one seed entry, then live items.
    assert len(rebuilt.record) == 1


def test_reset_session_continues_appending() -> None:
    bundle = export_handoff(_manager_with_history())
    rebuilt = ContextManager()
    seed_from_handoff(rebuilt, bundle)
    rebuilt.append(LLMMessage.text_of("user", "one more thing"))
    assert rebuilt.projection[-1].content_text == "one more thing"
    assert len(rebuilt.projection) == len(bundle.messages) + 1


def test_file_round_trip(tmp_path) -> None:
    bundle = export_handoff(_manager_with_history(), source_record_id="rec-9")
    path = tmp_path / "handoff.json"
    write_handoff(bundle, path)
    loaded = read_handoff(path)
    assert loaded == bundle


def test_newer_version_refused_whole(tmp_path) -> None:
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"v": HANDOFF_FORMAT_VERSION + 1, "messages": []}))
    with pytest.raises(HandoffFormatError, match="unsupported handoff format"):
        read_handoff(path)


def test_kinds_length_mismatch_refused() -> None:
    bundle = export_handoff(_manager_with_history())
    data = bundle.to_dict()
    data["message_kinds"] = ["only.one"]
    with pytest.raises(HandoffFormatError, match="message_kinds length"):
        HandoffBundle.from_dict(data)


def test_unreadable_file_fails_loud(tmp_path) -> None:
    path = tmp_path / "junk.json"
    path.write_text("not json{")
    with pytest.raises(HandoffFormatError, match="unreadable handoff"):
        read_handoff(path)


def test_export_after_compaction_uses_visible_span() -> None:
    """The bundle is the *projection*, not the record: superseded spans
    (pre-compaction messages) must not leak into the handoff."""
    manager = _manager_with_history()
    manager.replace_all(
        [LLMMessage.text_of("system", "sys"), LLMMessage.text_of("user", "summary")],
        reason="test compaction",
    )
    bundle = export_handoff(manager)
    assert [m.content_text for m in bundle.messages] == ["sys", "summary"]
    # And the record still holds the superseded span (audit intact).
    assert len(manager.record) > 2
