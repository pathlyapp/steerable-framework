"""E2E: the workspace tool set in a real sidecar process with a mock LLM.

``test_workspace_tools.py`` covers the handlers in process; what only this
layer proves: every tool the model is offered on the default path actually
crosses the process boundary, executes against the real (jailed) filesystem,
and its result lands in the next request's messages. One scripted task flow
exercises the whole set the way a model would use it: write → read → edit →
patch → glob → grep → bash → interactive bash_session/write_stdin.
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

EXPECTED_TOOLS = {
    "write_file",
    "read_file",
    "edit_file",
    "apply_patch",
    "glob",
    "grep",
    "bash",
    "bash_session",
    "write_stdin",
}


def _tool_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in body.get("messages") or [] if m.get("role") == "tool"]


def _last_tool_payload(body: dict[str, Any]) -> dict[str, Any]:
    msgs = _tool_messages(body)
    assert msgs, "expected at least one tool message in the request"
    return json.loads(msgs[-1]["content"])


async def test_workspace_tools_end_to_end_task_flow(
    e2e_gate: None, mock_openai: Any, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def responder(body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index == 0:
            # Kick the flow off by creating the file. (Must be an explicit
            # branch: the fallthrough is the final text, or a late request
            # would re-issue this write and revert the file.)
            return sse_tool_call(
                "write_file",
                {"path": "hello.py", "content": "print('v1')\n"},
                call_id="c_write",
            )
        # Every round after the first: the previous tool result must have
        # crossed back successfully before the script issues the next call.
        if index == 1:
            payload = _last_tool_payload(body)
            assert payload["success"] is True, payload
            return sse_tool_call(
                "read_file", {"path": "hello.py"}, call_id="c_read"
            )
        if index == 2:
            payload = _last_tool_payload(body)
            assert payload["success"] is True, payload
            assert "print('v1')" in json.dumps(payload)
            return sse_tool_call(
                "edit_file",
                {
                    "path": "hello.py",
                    "edits": [{"oldText": "v1", "newText": "v2"}],
                },
                call_id="c_edit",
            )
        if index == 3:
            payload = _last_tool_payload(body)
            assert payload["success"] is True, payload
            return sse_tool_call(
                "apply_patch",
                {
                    "patches": [
                        {
                            "path": "hello.py",
                            "edits": [
                                {"oldText": "print('v2')", "newText": "print('v3')"}
                            ],
                        }
                    ]
                },
                call_id="c_patch",
            )
        if index == 4:
            payload = _last_tool_payload(body)
            assert payload["success"] is True, payload
            return sse_tool_call("glob", {"pattern": "*.py"}, call_id="c_glob")
        if index == 5:
            payload = _last_tool_payload(body)
            assert payload["success"] is True, payload
            assert "hello.py" in json.dumps(payload)
            return sse_tool_call("grep", {"query": "v3"}, call_id="c_grep")
        if index == 6:
            payload = _last_tool_payload(body)
            assert payload["success"] is True, payload
            assert "hello.py" in json.dumps(payload)
            return sse_tool_call(
                "bash", {"command": "python3 hello.py"}, call_id="c_bash"
            )
        if index == 7:
            payload = _last_tool_payload(body)
            assert payload["success"] is True, payload
            assert "v3" in json.dumps(payload)
            return sse_tool_call(
                "bash_session", {"command": "cat"}, call_id="c_sess"
            )
        if index == 8:
            payload = _last_tool_payload(body)
            assert payload["success"] is True, payload
            session_id = payload["data"]["sessionId"]
            assert session_id
            return sse_tool_call(
                "write_stdin",
                {"sessionId": session_id, "chars": "ping\n", "yieldMs": 3000},
                call_id="c_stdin",
            )
        if index == 9:
            # The write above drained the shell banner; the echo of "ping"
            # arrives on a poll (write_stdin returns new output only).
            payload = _last_tool_payload(body)
            assert payload["success"] is True, payload
            session_id = payload["data"]["sessionId"]
            return sse_tool_call(
                "write_stdin",
                {"sessionId": session_id, "chars": "", "yieldMs": 3000},
                call_id="c_poll",
            )
        if index == 10:
            payload = _last_tool_payload(body)
            assert payload["success"] is True, payload
            assert "ping" in payload["data"]["output"]
            session_id = payload["data"]["sessionId"]
            return sse_tool_call(
                "write_stdin",
                {"sessionId": session_id, "close": True},
                call_id="c_close",
            )
        # index >= 11 (and any late delivery-check round): close out with text.
        if index >= 11:
            if _tool_messages(body):
                payload = _last_tool_payload(body)
                assert payload["success"] is True, payload
            return sse_text("E2E_WORKSPACE_OK")
        return sse_text("E2E_WORKSPACE_OK")

    mock = mock_openai(responder)
    code, out, err = await run_headless(
        [
            "--cwd",
            str(workspace),
            "--instruction",
            "Create hello.py, evolve it to v3, verify it, then confirm.",
            "--no-web-tools",
        ],
        child_env(
            tmp_path,
            {
                "STEERABLE_MODEL": "mock-e2e",
                "STEERABLE_BASE_URL": mock.base_url,
                "STEERABLE_API_KEY": "e2e-not-a-real-key",
            },
        ),
    )
    assert code == 0, err[-2000:]
    assert "E2E_WORKSPACE_OK" in out

    # The whole set was offered on the default path (no env gates).
    offered = model_tool_names(mock.requests[0].get("tools"))
    assert EXPECTED_TOOLS <= offered

    # The real filesystem reflects the scripted edits: v1 → v2 → v3.
    assert (workspace / "hello.py").read_text() == "print('v3')\n"
