"""E2E: ``todo_write`` in a real sidecar process with a mock LLM.

``test_todo.py`` (agent-runtime) covers validation and store semantics in
process; what only this layer proves:

- the tool is on the default shipping path: a default headless run offers
  ``todo_write`` in the model's tools array with no env gate;
- a model-issued call crosses the process boundary, dispatches locally
  (the host does not know the tool), and its result lands in the next
  request's messages;
- a validation error comes back as the tool result so the model can retry
  within the same turn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from e2e_harness import (
    child_env,
    model_tool_names,
    run_headless,
    sse_text,
    sse_tool_call,
)


def _headless_env(tmp_path: Path, mock: Any) -> dict[str, str]:
    return child_env(
        tmp_path,
        {
            "STEERABLE_MODEL": "mock-e2e",
            "STEERABLE_BASE_URL": mock.base_url,
            "STEERABLE_API_KEY": "e2e-not-a-real-key",
        },
    )


def _tool_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in body.get("messages") or [] if m.get("role") == "tool"]


async def test_todo_write_is_offered_by_default_and_dispatches(
    e2e_gate: None, mock_openai: Any, tmp_path: Path
) -> None:
    todos = [
        {"id": "a", "content": "survey the repo", "status": "completed"},
        {"id": "b", "content": "write the patch", "status": "in_progress"},
        {"id": "c", "content": "run the tests", "status": "pending"},
    ]

    def responder(body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index == 0:
            return sse_tool_call("todo_write", {"todos": todos}, call_id="call_t1")
        return sse_text("E2E_TODO_OK")

    mock = mock_openai(responder)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    code, out, err = await run_headless(
        [
            "--cwd",
            str(workspace),
            "--instruction",
            "Track this three-step task, then confirm.",
            "--no-web-tools",
        ],
        _headless_env(tmp_path, mock),
    )
    assert code == 0, err[-2000:]
    assert "E2E_TODO_OK" in out

    # Default shipping path: no env gate, the descriptor is in the tools array.
    offered = model_tool_names(mock.requests[0].get("tools"))
    assert "todo_write" in offered

    # The tool result crossed back into the next request's messages with the
    # full list and the summary counts.
    tool_msgs = _tool_messages(mock.requests[1])
    assert len(tool_msgs) == 1
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["success"] is True
    value = payload["data"]["value"]
    assert [t["id"] for t in value["todos"]] == ["a", "b", "c"]
    assert value["summary"] == {
        "total": 3,
        "pending": 1,
        "inProgress": 1,
        "completed": 1,
    }


async def test_todo_write_validation_error_comes_back_as_tool_result(
    e2e_gate: None, mock_openai: Any, tmp_path: Path
) -> None:
    bad = [
        {"id": "a", "content": "one", "status": "in_progress"},
        {"id": "b", "content": "two", "status": "in_progress"},
    ]
    good = [{"id": "a", "content": "one", "status": "in_progress"}]

    def responder(body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index == 0:
            return sse_tool_call("todo_write", {"todos": bad}, call_id="call_bad")
        if index == 1:
            # The model sees the error and retries with a fixed list.
            tool_msgs = _tool_messages(body)
            assert tool_msgs, "expected the error tool result in the messages"
            return sse_tool_call("todo_write", {"todos": good}, call_id="call_ok")
        return sse_text("E2E_TODO_RETRY_OK")

    mock = mock_openai(responder)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    code, out, err = await run_headless(
        [
            "--cwd",
            str(workspace),
            "--instruction",
            "Track the task, fix the list if told, then confirm.",
            "--no-web-tools",
        ],
        _headless_env(tmp_path, mock),
    )
    assert code == 0, err[-2000:]
    assert "E2E_TODO_RETRY_OK" in out

    # Round 2's messages carry the validation error as the tool result.
    error_payload = json.loads(_tool_messages(mock.requests[1])[0]["content"])
    assert error_payload["success"] is False
    assert "at most one" in error_payload["error"]
    # Round 3's messages carry the accepted list (history accumulates, so
    # the latest tool message is the last one).
    ok_payload = json.loads(_tool_messages(mock.requests[2])[-1]["content"])
    assert ok_payload["success"] is True
    assert ok_payload["data"]["value"]["summary"]["inProgress"] == 1
