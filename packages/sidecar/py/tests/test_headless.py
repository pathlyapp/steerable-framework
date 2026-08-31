from __future__ import annotations

from pathlib import Path

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage

from steerable_sidecar import headless as headless_mod
from steerable_sidecar.headless import _load_instruction, _run, main


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


@pytest.mark.asyncio
async def test_run_with_harness_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """W1.2.2: --harness assembles the spec's strategies instead of the
    built-in default chain."""
    from steerable_agent_runtime.harness_spec import load_harness_spec

    spec_path = (
        Path(headless_mod.__file__).parents[4]
        / "agent-runtime/py/src/steerable_agent_runtime/default.harness.yaml"
    )
    spec = load_harness_spec(spec_path)  # fails loud if the bundled spec breaks
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
    await _run("hello", cwd=str(tmp_path), max_rounds=4, harness_path=spec_path)
    assert "done" in capsys.readouterr().out


def test_assemble_harness_replaces_default_chain(tmp_path: Path) -> None:
    """The spec's strategies, not the built-in chain, drive the loop."""
    from steerable_agent_runtime.compaction import CompactionHooks
    from steerable_agent_runtime.hooks import ChainHooks
    from steerable_agent_runtime.observation_aging import ObservationAgingHooks

    spec_path = tmp_path / "aging.harness.yaml"
    spec_path.write_text(
        "context:\n"
        "  - impl: observation_aging\n"
        "retry:\n"
        "  - impl: simple\n"
        'validator: "null"\n'
        "tools: full\n"
        "memory: stateless\n"
        "orchestration: single\n",
        encoding="utf-8",
    )
    hooks, storage, executor, descriptors, limits = headless_mod._assemble_harness(
        spec_path,
        {"model": "fake"},
        provider=None,
        executor=object(),
        tools=_FakeTools(),
    )

    def _flatten(h: object) -> list[object]:
        if isinstance(h, ChainHooks):
            return [x for sub in h._hooks for x in _flatten(sub)]
        return [h]

    kinds = [type(h).__name__ for h in _flatten(hooks)]
    assert "ObservationAgingHooks" in kinds
    assert "CompactionHooks" not in kinds  # spec did not ask for it
    assert "DeliveryHooks" in kinds  # transport semantics always stay
    assert limits.max_rounds is None  # no loop section → entrypoint default


def test_assemble_harness_loop_limits(tmp_path: Path) -> None:
    """W3.4.2.4: the spec's loop section reaches LoopConfig."""
    spec_path = tmp_path / "limits.harness.yaml"
    spec_path.write_text(
        "context:\n  - impl: observation_aging\n"
        "retry:\n  - impl: simple\n"
        'validator: "null"\n'
        "tools: full\nmemory: stateless\norchestration: single\n"
        "loop:\n  max_rounds: 7\n  tool_dedup: true\n",
        encoding="utf-8",
    )
    *_, limits = headless_mod._assemble_harness(
        spec_path, {"model": "fake"}, provider=None, executor=object(), tools=_FakeTools()
    )
    assert limits.max_rounds == 7
    assert limits.tool_dedup is True
    assert limits.max_tool_errors is None


def test_assemble_harness_subagent_advertises_delegation_tools(tmp_path: Path) -> None:
    """W1.4.2.1 fix: the subagent arm must advertise the agent_* family —
    a live smoke test caught the model answering "no subagent tool exists"
    when only the executor was wrapped but no descriptor was added."""
    spec_path = tmp_path / "sub.harness.yaml"
    spec_path.write_text(
        "context:\n  - impl: observation_aging\n"
        "retry:\n  - impl: simple\n"
        'validator: "null"\n'
        "tools: full\nmemory: stateless\norchestration: subagent\n",
        encoding="utf-8",
    )
    *_, descriptors, _limits = headless_mod._assemble_harness(
        spec_path, {"model": "fake"}, provider=None, executor=object(), tools=_FakeTools()
    )
    names = {d.get("function", {}).get("name") or d.get("name") for d in descriptors}
    assert {"agent_spawn", "agent_send", "agent_wait", "agent_close"} <= names


def test_assemble_harness_single_adds_no_delegation_tools(tmp_path: Path) -> None:
    """The default arm's surface must stay exactly the workspace set —
    the factorial protocol forbids smuggling orchestration tools into a
    single-agent arm."""
    spec_path = tmp_path / "single.harness.yaml"
    spec_path.write_text(
        "context:\n  - impl: observation_aging\n"
        "retry:\n  - impl: simple\n"
        'validator: "null"\n'
        "tools: full\nmemory: stateless\norchestration: single\n",
        encoding="utf-8",
    )
    *_, descriptors, _limits = headless_mod._assemble_harness(
        spec_path, {"model": "fake"}, provider=None, executor=object(), tools=_FakeTools()
    )
    names = {d.get("function", {}).get("name") or d.get("name") for d in descriptors}
    assert not {"agent_spawn", "agent_send", "agent_wait", "agent_close"} & names


class _FakeTools:
    def describe_model(self) -> list[dict]:
        return [{"name": "bash"}]


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
