from __future__ import annotations

from pathlib import Path

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage

from steerable_sidecar import headless as headless_mod
from steerable_sidecar.headless import (
    _load_instruction,
    _max_tokens,
    _run,
    _soft_timeout_ms,
    _temperature,
    main,
)


class _ScriptedProvider:
    name = "scripted"
    model = "scripted-model"

    def __init__(self, script, fail_on=None):
        self._script = script
        self._fail_on = fail_on or set()
        self._round = 0
        self.attempts = 0

    async def complete(self, *args, **kwargs):
        raise NotImplementedError

    def stream(self, messages, **kwargs):
        self.attempts += 1
        attempt = self.attempts
        chunks = self._script[min(self._round, len(self._script) - 1)]

        async def _gen():
            if attempt in self._fail_on:
                from steerable_agent_runtime.llm.errors import LLMError

                raise LLMError("upstream reset", kind="transport", provider=self.name)
                yield  # pragma: no cover — make this a generator
            self._round += 1
            for chunk in chunks:
                yield chunk

        return _gen()


def test_version(capsys) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == headless_mod.__version__


def test_system_prompt_names_edit_file_and_delivery() -> None:
    assert "edit_file" in headless_mod._SYSTEM
    assert "write_file" in headless_mod._SYSTEM
    assert "pgrep" in headless_mod._SYSTEM
    assert "YYYY-MM-DD" in headless_mod._SYSTEM
    assert "acceptance criteria" in headless_mod._SYSTEM
    assert "scoring CLI" in headless_mod._SYSTEM
    assert "wget" in headless_mod._SYSTEM
    assert "dd" in headless_mod._SYSTEM
    assert "carving" in headless_mod._SYSTEM
    assert "Hidden tests score files" in headless_mod._SYSTEM
    assert "wait for background jobs" in headless_mod._SYSTEM
    assert "truncated write_file" in headless_mod._SYSTEM
    assert "PIL" in headless_mod._SYSTEM
    assert "ASCII" in headless_mod._SYSTEM
    assert "drafted required file contents" in headless_mod._SYSTEM
    assert "timeout N" in headless_mod._SYSTEM
    assert "wget -c" in headless_mod._SYSTEM
    assert "sleep 290" in headless_mod._SYSTEM
    assert "BOS/EOS" in headless_mod._SYSTEM
    assert "paragraph describing" in headless_mod._SYSTEM
    assert "every solution" in headless_mod._SYSTEM
    assert "stdout phrase" in headless_mod._SYSTEM


def test_missing_instruction_errors() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_load_instruction_from_file(tmp_path: Path) -> None:
    path = tmp_path / "task.md"
    path.write_text("fix the repo", encoding="utf-8")
    assert _load_instruction(None, path) == "fix the repo"
    assert _load_instruction("inline", path) == "inline"
    assert _load_instruction(None, None) == ""


@pytest.mark.asyncio
async def test_run_streams_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    provider = _ScriptedProvider(
        [
            [
                LLMStreamChunk(content_delta="done"),
                LLMStreamChunk(
                    finish_reason="stop",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
            ]
        ]
    )
    monkeypatch.setattr(
        headless_mod, "_env_provider_params", lambda: {"model": "fake"}
    )
    monkeypatch.setattr(
        headless_mod, "default_llm_provider_factory", lambda _params: provider
    )
    await _run("hello", cwd=str(tmp_path), max_rounds=4)
    assert "done" in capsys.readouterr().out


def test_run_requires_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(headless_mod, "_env_provider_params", lambda: {"model": ""})
    assert main(["--instruction", "hi"]) == 2


def test_soft_timeout_ms_default_and_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEERABLE_SOFT_TIMEOUT_MS", raising=False)
    assert _soft_timeout_ms() == 9_000_000
    monkeypatch.setenv("STEERABLE_SOFT_TIMEOUT_MS", "0")
    assert _soft_timeout_ms() is None
    monkeypatch.setenv("STEERABLE_SOFT_TIMEOUT_MS", "60000")
    assert _soft_timeout_ms() == 60_000


def test_temperature_and_max_tokens_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEERABLE_TEMPERATURE", raising=False)
    monkeypatch.delenv("STEERABLE_MAX_TOKENS", raising=False)
    assert _temperature() is None
    assert _max_tokens() is None
    monkeypatch.setenv("STEERABLE_TEMPERATURE", "1.0")
    monkeypatch.setenv("STEERABLE_MAX_TOKENS", "65536")
    assert _temperature() == 1.0
    assert _max_tokens() == 65536
    monkeypatch.setenv("STEERABLE_MAX_TOKENS", "0")
    assert _max_tokens() is None


def test_headless_wrap_up_keeps_tools() -> None:
    import inspect

    src = inspect.getsource(headless_mod._run)
    assert "wrap_up_keeps_tools=True" in src
    assert "wrap_up_max_tool_rounds=12" in src
    assert "DeliveryHooks(instruction=instruction)" in src


@pytest.mark.asyncio
async def test_run_with_bash_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ScriptedProvider(
        [
            [
                LLMStreamChunk(
                    tool_call_delta=ToolCall(
                        id="c1", name="bash", arguments={"command": "echo hi"}
                    )
                ),
                LLMStreamChunk(
                    finish_reason="tool_calls",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
            ],
            [
                LLMStreamChunk(content_delta="ok"),
                LLMStreamChunk(
                    finish_reason="stop",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
            ],
        ]
    )
    monkeypatch.setattr(
        headless_mod, "_env_provider_params", lambda: {"model": "fake"}
    )
    monkeypatch.setattr(
        headless_mod, "default_llm_provider_factory", lambda _params: provider
    )
    await _run("run echo", cwd=str(tmp_path), max_rounds=8)


@pytest.mark.asyncio
async def test_run_retries_transient_stream_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    provider = _ScriptedProvider(
        [
            [
                LLMStreamChunk(content_delta="recovered"),
                LLMStreamChunk(
                    finish_reason="stop",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                ),
            ]
        ],
        fail_on={1},
    )
    monkeypatch.setattr(
        headless_mod, "_env_provider_params", lambda: {"model": "fake"}
    )
    monkeypatch.setattr(
        headless_mod, "default_llm_provider_factory", lambda _params: provider
    )
    await _run("hello", cwd=str(tmp_path), max_rounds=4)
    assert provider.attempts == 2
    assert "recovered" in capsys.readouterr().out
