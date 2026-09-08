"""E2E: ``run_js``/``wait_js`` in a real sidecar process with a real Node worker.

``test_ptc_js.py`` covers the worker protocol behind a passthrough backend;
what only this layer proves:

- registration is env-gated on the production path: a default headless run
  offers no ``run_js``/``wait_js``, and ``STEERABLE_PTC_JS=1`` adds both next
  to the native tools;
- the worker really runs under the platform's layer-2 backend (Seatbelt /
  bwrap / Landlock) — ``data._sandbox`` names it;
- a cell's nested ``tools.<name>(...)`` Promise crosses stdio into the live
  executor and resolves with the tool's data, inside **one** CoreLoop round;
- ``store``/``load`` and a yielded cell survive across model rounds of the
  same chat (the conversational shape run_code does not have).

Skipped when no Node runtime is on PATH (the worker cannot start).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from e2e_harness import (
    child_env,
    model_tool_names,
    run_headless,
    sse_text,
    sse_tool_call,
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="run_js e2e needs a Node.js runtime"
)


def _tool_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in body.get("messages") or [] if m.get("role") == "tool"]


def _headless_env(tmp_path: Path, mock: Any, **overrides: str) -> dict[str, str]:
    return child_env(
        tmp_path,
        {
            "STEERABLE_MODEL": "mock-e2e",
            "STEERABLE_BASE_URL": mock.base_url,
            "STEERABLE_API_KEY": "e2e-not-a-real-key",
            **overrides,
        },
    )


async def test_ptc_js_is_absent_by_default_and_offered_when_enabled(
    e2e_gate: None, mock_openai: Any, tmp_path: Path
) -> None:
    mock = mock_openai(lambda _body, _index: sse_text("OK"))
    workspace = tmp_path / "ws"
    workspace.mkdir()

    async def run(env: dict[str, str]) -> None:
        code, out, err = await run_headless(
            [
                "--cwd",
                str(workspace),
                "--instruction",
                "Reply with exactly: OK",
                "--no-web-tools",
            ],
            env,
        )
        assert code == 0, err[-2000:]
        assert "OK" in out

    await run(_headless_env(tmp_path, mock))
    default_tools = model_tool_names(mock.requests[0].get("tools"))
    assert "run_js" not in default_tools
    assert "wait_js" not in default_tools

    await run(_headless_env(tmp_path, mock, STEERABLE_PTC_JS="1"))
    enabled_tools = model_tool_names(mock.requests[1].get("tools"))
    assert "run_js" in enabled_tools
    assert "wait_js" in enabled_tools
    # Native tools stay in the schema — the JS PTC adds to the model's face.
    assert {"bash", "read_file", "write_file"} <= enabled_tools


async def test_cell_chains_a_workspace_tool_and_remembers_across_rounds(
    e2e_gate: None, mock_openai: Any, tmp_path: Path
) -> None:
    """One run_js cell writes a file through the jailed workspace tool and
    stores a value; a second cell (next model round) reads it back via
    ``load`` — the session KV crosses cells and rounds of one chat."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    first_cell = (
        "const w = await tools.write_file({path: 'note.txt', content: 'from-js'});"
        " store('seen', w.bytes);"
        " return w.bytes;"
    )
    second_cell = "return { remembered: load('seen') };"

    def responder(body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index == 0:
            return sse_tool_call(
                "run_js",
                {"code": first_cell, "description": "write + store"},
                call_id="call_js1",
            )
        if index == 1:
            return sse_tool_call(
                "run_js",
                {"code": second_cell, "description": "load"},
                call_id="call_js2",
            )
        return sse_text("E2E_PTC_JS_OK")

    mock = mock_openai(responder)
    code, out, err = await run_headless(
        [
            "--cwd",
            str(workspace),
            "--instruction",
            "Write note.txt, then remember its size.",
            "--no-web-tools",
        ],
        _headless_env(tmp_path, mock, STEERABLE_PTC_JS="1"),
    )
    assert code == 0, err[-2000:]
    assert "E2E_PTC_JS_OK" in out

    # Three model requests: two tool rounds + the final answer. The headless
    # delivery layer may add one wrap-up round (nested writes inside a cell
    # do not count as artifact writes), so assert the floor, not the exact
    # count — request indexes 1 and 2 are the two tool rounds either way.
    assert len(mock.requests) >= 3
    # The nested write took effect on disk through the jailed workspace tool.
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "from-js"

    first = json.loads(_tool_messages(mock.requests[1])[0]["content"])
    assert first["success"] is True
    first_data = first["data"]
    assert first_data["status"] == "completed"
    assert [c["tool"] for c in first_data["calls"]] == ["write_file"]
    # A real OS backend confined the worker ("none" would be a silent
    # confine-or-refuse degradation).
    sandbox = first_data["_sandbox"]
    assert sandbox["backend"] in {"seatbelt", "bwrap", "landlock"}
    assert sandbox["enforcement"] != "none"

    # requests[2] carries both rounds' run_js results; the last one is the
    # second cell's.
    second = json.loads(_tool_messages(mock.requests[2])[-1]["content"])
    assert second["success"] is True
    # The first cell's store() is visible to the second cell: the KV is
    # chat-scoped, not cell-scoped.
    assert second["data"]["value"] == {"remembered": len("from-js")}


async def test_yielded_cell_is_resumed_by_wait_js(
    e2e_gate: None, mock_openai: Any, tmp_path: Path
) -> None:
    """exec → ``status: running`` + cellId → wait_js → final result: the
    codex CodeModeHost round-trip across two model rounds."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    program = (
        "text('half'); "
        "await new Promise(r => setTimeout(r, 400)); "
        "return 'cell-finished';"
    )

    def responder(body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index == 0:
            return sse_tool_call(
                "run_js",
                {"code": program, "description": "slow cell", "yieldTimeMs": 50},
                call_id="call_js_exec",
            )
        if index == 1:
            # The model reads the cellId out of the first result and waits.
            tool_messages = _tool_messages(body)
            payload = json.loads(tool_messages[0]["content"])
            cell_id = payload["data"]["cellId"]
            assert payload["data"]["status"] == "running"
            return sse_tool_call(
                "wait_js",
                {"cellId": cell_id, "yieldTimeMs": 5000},
                call_id="call_js_wait",
            )
        return sse_text("E2E_PTC_JS_WAIT_OK")

    mock = mock_openai(responder)
    code, out, err = await run_headless(
        [
            "--cwd",
            str(workspace),
            "--instruction",
            "Run the slow cell and wait for it.",
            "--no-web-tools",
        ],
        _headless_env(tmp_path, mock, STEERABLE_PTC_JS="1"),
    )
    assert code == 0, err[-2000:]
    assert "E2E_PTC_JS_WAIT_OK" in out

    # requests[2] carries both rounds' tool messages; the wait_js result is
    # the one named wait_js (index 0 is the first round's run_js result).
    wait_messages = [
        m for m in _tool_messages(mock.requests[2]) if m.get("name") == "wait_js"
    ]
    assert len(wait_messages) == 1
    waited = json.loads(wait_messages[0]["content"])
    assert waited["success"] is True
    assert waited["data"]["status"] == "completed"
    assert waited["data"]["value"] == "cell-finished"
