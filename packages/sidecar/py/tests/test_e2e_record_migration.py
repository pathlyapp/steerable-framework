"""E2E: durable record v1→v2 migration through a real sidecar process.

Layer choice: the migration runs inside the sidecar's storage read path
(``resume.load_history_items`` → ``history.entry_from_dict`` →
``upgrade_entry_dict``), so the honest test boots a real
``python -m steerable_sidecar --storage-path <db>`` against a sqlite
record written in the v1 shape (no ``v`` key — what a pre-versioning build
persisted) and observes the read/upgrade/resume behavior over RPC. The v1
bytes are written with the production ``SqliteStorage.append_history``
channel, not a private fixture format.

What is proven through the real process:

- a v1 record loads and projects correctly (``agent.session.messages``);
- the upgraded record drives a real resumed turn: the mock LLM receives the
  v1 transcript verbatim and the reply lands in the record;
- the on-disk v1 entries are never rewritten (append-only: the record
  legitimately mixes v1 and v2 rows; only new appends carry ``v: 2``);
- a record written by a newer build (``v`` ahead of this build) is refused
  whole with an error naming the remedy and the record id, and the record
  is left byte-identical.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from steerable_agent_runtime.storage import SqliteStorage

from e2e_harness import SidecarRPCError, sse_text


def _v1_item(seq: int, role: str, text: str) -> dict[str, Any]:
    """The pre-versioning (v1) on-disk shape: no ``v`` key."""
    return {
        "entry": "item",
        "seq": seq,
        "kind": role,
        "turn_id": "turn-v1",
        "token_estimate": max(1, len(text) // 4),
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


async def _write_record(db_path: Path, record_id: str, entries: list[dict[str, Any]]) -> None:
    store = SqliteStorage(str(db_path))
    try:
        await store.append_history(record_id, entries)
    finally:
        store.close()


def _raw_rows(db_path: Path, record_id: str) -> list[tuple[int, dict[str, Any]]]:
    db = sqlite3.connect(str(db_path))
    try:
        rows = db.execute(
            "SELECT seq, data FROM history WHERE record_id = ? ORDER BY seq",
            (record_id,),
        ).fetchall()
    finally:
        db.close()
    return [(int(seq), json.loads(data)) for seq, data in rows]


def _sidecar_argv(db_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "steerable_sidecar",
        "--log-level",
        "ERROR",
        "--storage-path",
        str(db_path),
    ]


async def test_v1_record_loads_and_resumes_through_real_sidecar(
    e2e_gate: None, sidecar_factory: Any, mock_openai: Any, tmp_path: Path
) -> None:
    db_path = tmp_path / "records.db"
    record_id = "chat_v1"
    await _write_record(
        db_path,
        record_id,
        [
            _v1_item(0, "user", "question 1"),
            _v1_item(1, "assistant", "answer 1"),
            _v1_item(2, "user", "question 2"),
        ],
    )

    mock = mock_openai(lambda _body, _index: sse_text("answer 2"))
    client = await sidecar_factory(_sidecar_argv(db_path))

    # The v1 record loads and projects: the host-facing transcript read.
    projected = await client.request("agent.session.messages", {"recordId": record_id})
    assert projected["messages"] == [
        {"seq": 0, "role": "user", "content": "question 1"},
        {"seq": 1, "role": "assistant", "content": "answer 1"},
        {"seq": 2, "role": "user", "content": "question 2"},
    ]

    # The upgraded record drives a real resumed turn: the model receives the
    # v1 transcript verbatim and the loop completes.
    result = await client.request(
        "agent.chat.stream",
        {
            "provider": "openai_compat",
            "model": "mock-e2e",
            "baseUrl": mock.base_url,
            "apiKey": "e2e-not-a-real-key",
            "messages": [],
            "resume": True,
            "recordId": record_id,
            "useCoreLoop": True,
        },
    )
    stream_id = result["streamId"]
    done = await client.wait_for_notification(
        "stream.done",
        predicate=lambda p: p.get("streamId") == stream_id,
        timeout=60.0,
    )
    assert done["status"] == "completed"
    assert len(mock.requests) == 1
    assert mock.requests[0]["messages"] == [
        {"role": "user", "content": "question 1"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "question 2"},
    ]

    # Append-only across versions: the v1 rows keep their original bytes
    # (no ``v`` key); only the resumed turn's appends are stamped v2.
    await client.aclose()
    rows = _raw_rows(db_path, record_id)
    assert [seq for seq, _ in rows][:3] == [0, 1, 2]
    assert all("v" not in data for seq, data in rows if seq <= 2)
    new_rows = [data for seq, data in rows if seq >= 3]
    assert new_rows, "the resumed turn must append to the record"
    assert all(data.get("v") == 2 for data in new_rows)
    assert any(
        data.get("entry") == "item"
        and data.get("message", {}).get("role") == "assistant"
        and any(
            part.get("text") == "answer 2"
            for part in data["message"].get("content", [])
        )
        for data in new_rows
    )


async def test_future_record_version_is_refused_with_remedy(
    e2e_gate: None, sidecar_factory: Any, tmp_path: Path
) -> None:
    db_path = tmp_path / "records.db"
    record_id = "chat_from_the_future"
    await _write_record(
        db_path,
        record_id,
        [{**_v1_item(0, "user", "hello from a newer build"), "v": 99}],
    )
    before = _raw_rows(db_path, record_id)

    client = await sidecar_factory(_sidecar_argv(db_path))

    # The projection read refuses the whole record, naming the remedy…
    with pytest.raises(SidecarRPCError) as excinfo:
        await client.request("agent.session.messages", {"recordId": record_id})
    message = excinfo.value.error["message"]
    assert "unsupported record format version" in message
    assert "upgrade the app" in message
    assert record_id in message

    # …and so does the resume path (no silent truncation onto a live turn).
    with pytest.raises(SidecarRPCError) as excinfo2:
        await client.request(
            "agent.chat.stream",
            {
                "provider": "openai_compat",
                "model": "mock-e2e",
                "baseUrl": "http://127.0.0.1:9/v1",
                "apiKey": "e2e-not-a-real-key",
                "messages": [],
                "resume": True,
                "recordId": record_id,
                "useCoreLoop": True,
            },
        )
    assert "unsupported record format version" in excinfo2.value.error["message"]

    # The refusal left the record byte-identical.
    await client.aclose()
    assert _raw_rows(db_path, record_id) == before
