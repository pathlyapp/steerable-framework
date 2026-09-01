from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from steerable_agent_protocol.generated import ToolCall
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage

from steerable_sidecar import headless as headless_mod
from steerable_sidecar.headless import (
    _hard_run_timeout_sec,
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
    assert "named thresholds" in headless_mod._SYSTEM
    assert "scoring CLI" in headless_mod._SYSTEM
    assert "quantize" in headless_mod._SYSTEM
    assert "rewritten sidecar" in headless_mod._SYSTEM
    assert "exact output that must" in headless_mod._SYSTEM
    assert "source-size cap" in headless_mod._SYSTEM
    assert "wget" in headless_mod._SYSTEM
    assert "dd" in headless_mod._SYSTEM
    assert "carving" in headless_mod._SYSTEM
    assert "Hidden tests score files" in headless_mod._SYSTEM
    assert "wait for background jobs" in headless_mod._SYSTEM
    assert "stop reasoning" in headless_mod._SYSTEM
    assert "truncated write_file" in headless_mod._SYSTEM
    assert "PIL" in headless_mod._SYSTEM
    assert "ASCII" in headless_mod._SYSTEM
    assert "8x8" in headless_mod._SYSTEM
    assert "occupancy" in headless_mod._SYSTEM
    assert "uncompressed BMP" in headless_mod._SYSTEM
    assert "drafted required file contents" in headless_mod._SYSTEM
    assert "or in reasoning" in headless_mod._SYSTEM
    assert "cat > path <<'EOF'" in headless_mod._SYSTEM
    assert "timeout N" in headless_mod._SYSTEM
    assert "login prompt" in headless_mod._SYSTEM
    assert "replay proxy" in headless_mod._SYSTEM
    assert "framebuffer" in headless_mod._SYSTEM
    assert "UNIX socket" in headless_mod._SYSTEM
    assert "daemonize" in headless_mod._SYSTEM
    assert "wget -c" in headless_mod._SYSTEM
    assert "sleep 290" in headless_mod._SYSTEM
    assert "BOS/EOS" in headless_mod._SYSTEM
    assert "add_special_tokens=False" in headless_mod._SYSTEM
    assert "paragraph describing" in headless_mod._SYSTEM
    assert "every solution" in headless_mod._SYSTEM
    assert "stdout phrase" in headless_mod._SYSTEM
    assert "concatenated dump" in headless_mod._SYSTEM
    assert "default query prompts" in headless_mod._SYSTEM
    assert "OCR" in headless_mod._SYSTEM
    assert "1 fps" in headless_mod._SYSTEM
    assert "BPE garbage" in headless_mod._SYSTEM
    assert "player moves" in headless_mod._SYSTEM
    assert "help iterations" in headless_mod._SYSTEM
    assert "policy enum" in headless_mod._SYSTEM
    assert "position embeddings" in headless_mod._SYSTEM
    assert "held-out inputs" in headless_mod._SYSTEM
    assert "pre-shrink score" in headless_mod._SYSTEM
    assert "valid UTF-8" in headless_mod._SYSTEM
    assert "relative filename" in headless_mod._SYSTEM
    assert "system interpreter" in headless_mod._SYSTEM
    assert "stock video backend" in headless_mod._SYSTEM
    assert "DOOMGENERIC_RESX" in headless_mod._SYSTEM
    assert "width×height" in headless_mod._SYSTEM
    assert "gzip|wc" in headless_mod._SYSTEM
    assert "/usr/local/bin/python" in headless_mod._SYSTEM
    assert "adapt-control" in headless_mod._SYSTEM
    assert "iteration counts" in headless_mod._SYSTEM
    assert "NameError" in headless_mod._SYSTEM
    assert "baseline JPEG" in headless_mod._SYSTEM
    assert "runtime or speedup" in headless_mod._SYSTEM
    assert "meet the threshold" in headless_mod._SYSTEM
    assert "sequence verbatim" in headless_mod._SYSTEM
    assert "expression tags" in headless_mod._SYSTEM
    assert "fusion or subprotein order" in headless_mod._SYSTEM
    assert "ASCII raster" in headless_mod._SYSTEM
    assert "annealing arm" in headless_mod._SYSTEM
    assert "javascript: URLs" in headless_mod._SYSTEM
    assert "unclosed tags" in headless_mod._SYSTEM
    assert "byte-identical" in headless_mod._SYSTEM
    assert "<input/>" in headless_mod._SYSTEM
    assert "Connection refused" in headless_mod._SYSTEM
    assert "x-axis units" in headless_mod._SYSTEM
    assert "handwritten accuracy" in headless_mod._SYSTEM
    assert "below the named" in headless_mod._SYSTEM
    assert "hidden test set" in headless_mod._SYSTEM
    assert "within 0.02" in headless_mod._SYSTEM
    assert "thousandths under" in headless_mod._SYSTEM
    assert "empty illegal row" in headless_mod._SYSTEM
    assert "Our move:" in headless_mod._SYSTEM
    assert "0.994 is not > 0.995" in headless_mod._SYSTEM
    assert "Nth highest cosine" in headless_mod._SYSTEM


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
    assert "wrap_up_max_tool_rounds=16" in src
    assert "wrap_up_tool_timeout_ms=120_000" in src
    assert "wrap_up_hard_cap_ms=10_500_000" in src
    assert '"STEERABLE_IDLE_STREAM_TIMEOUT_MS", 600_000' in src
    assert "_hard_run_timeout_sec" in src
    assert "wait_for" in src
    assert "DeliveryGatedExecutor" in src
    hooks_src = src[src.index("ChainHooks") :]
    assert hooks_src.index("_default_loop_hooks") < hooks_src.index("delivery")


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


@pytest.mark.asyncio
async def test_run_emits_run_summary_terminal_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """W1.4.3.3 emission side: the final stdout line is a parseable
    STEERABLE_RUN_SUMMARY carrying rounds/usage/peak-context/error metrics
    derived from the loop event stream (attribution.py's contract)."""
    import json

    provider = _ScriptedProvider(
        [
            [
                LLMStreamChunk(
                    tool_call_delta=ToolCall(
                        id="c1", name="bash", arguments={"command": "exit 3"}
                    )
                ),
                LLMStreamChunk(
                    finish_reason="tool_calls",
                    usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                ),
            ],
            [
                # A write, not another probe: DeliveryHooks vetoes completion
                # for turns that never mutate files, and the veto's re-stream
                # would (correctly) add a fourth request to the billable totals.
                LLMStreamChunk(
                    tool_call_delta=ToolCall(
                        id="c2",
                        name="write_file",
                        arguments={"path": "out.txt", "content": "x"},
                    )
                ),
                LLMStreamChunk(
                    finish_reason="tool_calls",
                    usage=LLMUsage(prompt_tokens=20, completion_tokens=5, total_tokens=25),
                ),
            ],
            [
                LLMStreamChunk(content_delta="done"),
                LLMStreamChunk(
                    finish_reason="stop",
                    usage=LLMUsage(prompt_tokens=30, completion_tokens=5, total_tokens=35),
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
    await _run("fail then recover", cwd=str(tmp_path), max_rounds=8)

    lines = capsys.readouterr().out.splitlines()
    summary_lines = [ln for ln in lines if ln.startswith("STEERABLE_RUN_SUMMARY ")]
    assert len(summary_lines) == 1
    assert lines[-1] == summary_lines[0]  # terminal line, never mid-stream
    summary = json.loads(summary_lines[0][len("STEERABLE_RUN_SUMMARY "):])
    assert summary["rounds"] == 3
    # Completion usage is the run's accumulated totals.
    assert summary["input_tokens"] == 60
    assert summary["output_tokens"] == 15
    # Peak context is the largest single-request prompt, not the sum.
    assert summary["peak_context_tokens"] == 30
    assert summary["tool_errors"] == 1
    assert summary["tool_recoveries"] == 1
    assert "cost_usd" not in summary  # absent, never zero-filled


def test_hard_run_timeout_defaults_under_harbor_wrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STEERABLE_HARD_TIMEOUT_SEC", raising=False)
    assert _hard_run_timeout_sec() == 10_200.0
    monkeypatch.setenv("STEERABLE_HARD_TIMEOUT_SEC", "0")
    assert _hard_run_timeout_sec() is None


@pytest.mark.asyncio
async def test_run_exits_on_hard_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    class _Hang:
        name = "hang"
        model = "hang-model"

        async def complete(self, *args, **kwargs):
            raise NotImplementedError

        def stream(self, messages, **kwargs):
            async def _gen():
                await asyncio.sleep(30)
                yield LLMStreamChunk(content_delta="never")

            return _gen()

    monkeypatch.setattr(headless_mod, "_hard_run_timeout_sec", lambda: 0.05)
    monkeypatch.setattr(
        headless_mod, "_env_provider_params", lambda: {"model": "fake"}
    )
    monkeypatch.setattr(
        headless_mod, "default_llm_provider_factory", lambda _params: _Hang()
    )
    await _run("hang", cwd=str(tmp_path), max_rounds=4)
    assert "[hard_timeout]" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_swallows_loop_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    class _BoomLoop:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, *args, **kwargs):
            raise ConnectionError("incomplete chunked read")
            yield  # pragma: no cover — keep this an async generator

    monkeypatch.setattr(headless_mod, "_hard_run_timeout_sec", lambda: None)
    monkeypatch.setattr(
        headless_mod, "_env_provider_params", lambda: {"model": "fake"}
    )
    monkeypatch.setattr(
        headless_mod,
        "default_llm_provider_factory",
        lambda _params: _ScriptedProvider([[]]),
    )
    monkeypatch.setattr(headless_mod, "CoreLoop", _BoomLoop)
    await _run("crash", cwd=str(tmp_path), max_rounds=2)
    assert "[loop_error ConnectionError:" in capsys.readouterr().out
