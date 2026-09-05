"""E2E: ``run_code`` in a real sidecar process, under a real OS backend.

Layer choice: ``test_run_code.py`` proves the driver contract and the
one-round accounting, but it monkeypatches ``select_exec_backend`` to a
passthrough — so it never shows that the *shipped* confinement wrapping
can actually execute the driver. What only a real spawned process proves:

- registration is env-gated on the production path: a default headless run
  offers no ``run_code`` to the model, and ``STEERABLE_RUN_CODE=1`` adds it
  next to the native tools (never instead of them);
- the child really runs under the platform backend this host has
  (Seatbelt / bwrap / Landlock) — ``argv_for_exec`` produces an argv the OS
  accepts, and ``data._sandbox`` names the backend that confined it;
- nested ``tools.call`` frames cross back over stdio into the live
  executor: two workspace tools take effect (a file is written, then read
  back) inside **one** CoreLoop round, so the model is billed once;
- the driver's import refusal holds across the process boundary, reported
  as a failed outer ``ToolResult`` rather than a crashed child.

No network beyond the loopback mock, and the program only touches the
test's own workspace through the jailed workspace tools.
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


async def test_run_code_is_absent_by_default_and_offered_when_enabled(
    e2e_gate: None, mock_openai: Any, tmp_path: Path
) -> None:
    """The env switch is the registration surface: off by default, and when
    on it *adds* to the native tools instead of replacing them."""
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
    assert "run_code" not in default_tools
    assert {"bash", "read_file", "write_file"} <= default_tools

    await run(_headless_env(tmp_path, mock, STEERABLE_RUN_CODE="1"))
    enabled_tools = model_tool_names(mock.requests[1].get("tools"))
    assert "run_code" in enabled_tools
    # Native tools stay in the schema — this is the `both` shape, not
    # DSH's ptc preset that narrows the model's face to run_code alone.
    assert {"bash", "read_file", "write_file"} <= enabled_tools


async def test_program_chains_two_workspace_tools_in_one_round(
    e2e_gate: None, mock_openai: Any, tmp_path: Path
) -> None:
    """One run_code call writes a file and reads it back through the live
    executor, and costs the model exactly one extra round."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    program = (
        "tools.call('write_file', path='note.txt', content='from-run-code')\n"
        "echoed = tools.call('read_file', path='note.txt')\n"
        "return {'read_back': echoed}\n"
    )

    def responder(body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index == 0:
            return sse_tool_call(
                "run_code",
                {"code": program, "description": "write then read"},
                call_id="call_program",
            )
        return sse_text("E2E_RUN_CODE_OK")

    mock = mock_openai(responder)
    code, out, err = await run_headless(
        [
            "--cwd",
            str(workspace),
            "--instruction",
            "Write note.txt and read it back.",
            "--no-web-tools",
        ],
        _headless_env(tmp_path, mock, STEERABLE_RUN_CODE="1"),
    )
    assert code == 0, err[-2000:]
    assert "E2E_RUN_CODE_OK" in out

    # (a) One program, two nested tools, two model requests — the second
    # round is the model reading the result, not a second tool round.
    assert len(mock.requests) == 2

    # (b) The nested write really took effect on disk through the jailed
    # workspace tool, not inside the confined child's private view.
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "from-run-code"

    # (c) The model saw exactly one tool message, and it is the outer
    # program's result — nested results are not replayed into history.
    tool_messages = _tool_messages(mock.requests[1])
    assert [m.get("name") for m in tool_messages] == ["run_code"]
    payload = json.loads(tool_messages[0]["content"])
    assert payload["success"] is True
    data = payload["data"]
    assert [c["tool"] for c in data["calls"]] == ["write_file", "read_file"]
    assert "from-run-code" in json.dumps(data["value"], ensure_ascii=False)

    # (d) A real OS backend confined the child; "none" would mean the
    # confine-or-refuse rule silently degraded to an unsandboxed spawn.
    sandbox = data["_sandbox"]
    assert sandbox["backend"] in {"seatbelt", "bwrap", "landlock"}
    assert sandbox["enforcement"] != "none"


async def test_import_refusal_survives_the_process_boundary(
    e2e_gate: None, mock_openai: Any, tmp_path: Path
) -> None:
    """``import os`` fails the outer tool call with the driver's reason —
    the child does not crash into an opaque non-zero exit."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def responder(body: dict[str, Any], index: int) -> list[dict[str, Any]]:
        if index == 0:
            return sse_tool_call(
                "run_code",
                {"code": "import os\nreturn os.getcwd()", "description": "escape"},
                call_id="call_escape",
            )
        return sse_text("E2E_REFUSED_OK")

    mock = mock_openai(responder)
    code, out, err = await run_headless(
        [
            "--cwd",
            str(workspace),
            "--instruction",
            "Try to import os.",
            "--no-web-tools",
        ],
        _headless_env(tmp_path, mock, STEERABLE_RUN_CODE="1"),
    )
    assert code == 0, err[-2000:]
    assert "E2E_REFUSED_OK" in out

    tool_messages = _tool_messages(mock.requests[1])
    assert [m.get("name") for m in tool_messages] == ["run_code"]
    payload = json.loads(tool_messages[0]["content"])
    assert payload["success"] is False
    assert "os" in payload["error"]
    assert "not allowed" in payload["error"]
