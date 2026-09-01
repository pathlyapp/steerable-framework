"""W1.2.1/1.2.3: HarnessSpec loading, fail-loud validation, assembly."""

from __future__ import annotations

from pathlib import Path

import pytest

from steerable_agent_runtime.harness import (
    FullToolset,
    MinimalToolset,
    SingleAgent,
    Stateless,
    SubAgentDelegation,
)
from steerable_agent_runtime.harness_spec import (
    HarnessSpecError,
    assemble_harness,
    harness_spec_from_dict,
    load_harness_spec,
)
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.llm.errors import LLMError
from steerable_agent_runtime.storage import InMemoryStorage

_DEFAULT_SPEC = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "steerable_agent_runtime"
    / "default.harness.yaml"
)


class _Ctx:
    def __init__(self, round_index: int = 0) -> None:
        self.round_index = round_index
        self.chat_id = None
        self.last_prompt_tokens = None
        self.last_prompt_transcript_len = 0


# -- loading -----------------------------------------------------------------


def test_default_spec_loads_and_describes() -> None:
    spec = load_harness_spec(_DEFAULT_SPEC)
    assert [c.impl for c in spec.context] == ["pressure_compaction", "spill"]
    assert [c.impl for c in spec.retry] == ["informed_backtrack", "simple"]
    assert spec.tools.impl == "full"
    described = spec.describe()
    assert described["orchestration"]["impl"] == "single"
    assert described["orchestration"]["assumes"]


def test_default_spec_json_copy_in_sync() -> None:
    """The wheel ships the .json copy for PyYAML-free runtimes (Harbor trial
    containers); the .yaml stays the commented source. They must not drift."""
    import json as stdlib_json

    import yaml

    yaml_data = yaml.safe_load(_DEFAULT_SPEC.read_text(encoding="utf-8"))
    json_data = stdlib_json.loads(
        _DEFAULT_SPEC.with_suffix(".json").read_text(encoding="utf-8")
    )
    assert json_data == yaml_data, (
        "default.harness.json drifted from default.harness.yaml — regenerate: "
        "python -c \"import yaml, json, pathlib; p = pathlib.Path("
        "'packages/agent-runtime/py/src/steerable_agent_runtime/default.harness.yaml'"
        "); p.with_suffix('.json').write_text(json.dumps(yaml.safe_load("
        "p.read_text()), indent=2) + '\\n')\""
    )


def test_json_spec_loads(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    path.write_text(
        '{"context": "null", "retry": "none", "validator": "null",'
        ' "tools": "minimal", "memory": "stateless", "orchestration": "single"}',
        encoding="utf-8",
    )
    spec = load_harness_spec(path)
    assert spec.context[0].impl == "null"
    assert spec.tools.impl == "minimal"


def test_unknown_dimension_fails_loud() -> None:
    with pytest.raises(HarnessSpecError, match="unknown dimensions"):
        harness_spec_from_dict({"context": "null", "mystery": "x"})


def test_missing_dimension_fails_loud() -> None:
    with pytest.raises(HarnessSpecError, match="missing dimensions"):
        harness_spec_from_dict({"context": "null"})


def test_unknown_implementation_fails_loud() -> None:
    with pytest.raises(HarnessSpecError, match="unknown implementation 'magic'"):
        harness_spec_from_dict(_full(tools="magic"))


def test_unknown_entry_key_fails_loud() -> None:
    with pytest.raises(HarnessSpecError, match="unknown entry keys"):
        harness_spec_from_dict(_full(tools={"impl": "full", "prams": {}}))


def test_singular_dimension_rejects_a_list() -> None:
    with pytest.raises(HarnessSpecError, match="exactly one"):
        harness_spec_from_dict(_full(tools=["full", "minimal"]))


def test_bad_params_fail_at_assembly_with_context() -> None:
    spec = harness_spec_from_dict(
        _full(retry={"impl": "simple", "params": {"mx_attempts": 3}})
    )
    with pytest.raises(HarnessSpecError, match="'simple'"):
        assemble_harness(spec)


def _full(**overrides):
    data = {
        "context": "null",
        "retry": "none",
        "validator": "null",
        "tools": "full",
        "memory": "stateless",
        "orchestration": "single",
    }
    data.update(overrides)
    return data


# -- assembly ------------------------------------------------------------------


def test_assembled_null_harness_is_all_baselines() -> None:
    harness = assemble_harness(harness_spec_from_dict(_full()))
    assert isinstance(harness.tool_selection, FullToolset)
    assert isinstance(harness.orchestration, SingleAgent)
    assert isinstance(harness.storage, InMemoryStorage)


def test_assembled_tool_selection_applies() -> None:
    harness = assemble_harness(harness_spec_from_dict(_full(tools="minimal")))
    assert isinstance(harness.tool_selection, MinimalToolset)
    tools = [
        {"type": "function", "function": {"name": "bash"}},
        {"type": "function", "function": {"name": "grep"}},
    ]
    assert [t["function"]["name"] for t in harness.select_tools(tools)] == ["bash"]


@pytest.mark.asyncio
async def test_default_spec_behaves_like_the_hand_assembled_chain() -> None:
    """W1.2.4 seed: the declared default must compact on pressure, backtrack
    on overflow, and back off on transient errors — the three behaviors the
    hand-written ChainHooks(Compaction, Spill, Retry) provides."""
    spec = load_harness_spec(_DEFAULT_SPEC)
    harness = assemble_harness(
        spec,
        runtime_params={
            "pressure_compaction": {"max_context_tokens": 10},
            "informed_backtrack": {"max_context_tokens": 10},
        },
    )
    transcript = [LLMMessage.text_of("user", "word " * 50)]

    pre = await harness.hooks.pre_step(transcript, _Ctx())
    assert pre.rewrite is not None  # pressure compaction fired

    overflow = LLMError("overflow", kind="context_overflow", provider="test")
    action = await harness.hooks.on_request_error(overflow, transcript, _Ctx())
    assert action.kind == "retry" and action.rewrite is not None  # backtrack

    transient = LLMError("reset", kind="transport", provider="test")
    action = await harness.hooks.on_request_error(transient, [], _Ctx())
    assert action.kind == "retry" and action.delay_ms > 0  # backoff


def test_runtime_params_fill_but_spec_wins() -> None:
    spec = harness_spec_from_dict(
        _full(
            context={"impl": "pressure_compaction", "params": {"threshold_ratio": 0.5}},
        )
    )
    # Missing required max_context_tokens → loud error without runtime params.
    with pytest.raises(HarnessSpecError):
        assemble_harness(spec)
    harness = assemble_harness(
        spec,
        runtime_params={"pressure_compaction": {"max_context_tokens": 1000}},
    )
    assert harness is not None


def test_arm_c_subagent_spec_loads_and_differs_in_exactly_one_dimension() -> None:
    """W1.4.2: the arm C spec must assemble and differ from the default
    only in orchestration — the factorial protocol's one-dimension rule."""
    repo = Path(__file__).resolve().parents[4]
    arm_c = load_harness_spec(repo / "evals/harnesses/subagent.harness.yaml")
    default = load_harness_spec(
        repo / "packages/agent-runtime/py/src/steerable_agent_runtime/default.harness.yaml"
    )
    assert arm_c.orchestration.impl == "subagent"
    assert default.orchestration.impl == "single"
    # Every other dimension identical, entry by entry.
    assert arm_c.context == default.context
    assert arm_c.retry == default.retry
    assert arm_c.validator == default.validator
    assert arm_c.tools == default.tools
    assert arm_c.memory == default.memory

    harness = assemble_harness(
        arm_c,
        runtime_params={
            "pressure_compaction": {"max_context_tokens": 100_000},
            "informed_backtrack": {"max_context_tokens": 100_000},
        },
    )
    assert isinstance(harness.orchestration, SubAgentDelegation)


def test_arm_d_minimal_spec_loads_and_differs_in_exactly_one_dimension() -> None:
    """W1.5.3: the arm D spec must assemble and differ from the default
    only in the tools dimension — the interactive-session control arm."""
    repo = Path(__file__).resolve().parents[4]
    arm_d = load_harness_spec(repo / "evals/harnesses/minimal.harness.yaml")
    default = load_harness_spec(
        repo / "packages/agent-runtime/py/src/steerable_agent_runtime/default.harness.yaml"
    )
    assert arm_d.tools.impl == "minimal"
    assert default.tools.impl == "full"
    # Every other dimension identical, entry by entry, loop pins included.
    assert arm_d.context == default.context
    assert arm_d.retry == default.retry
    assert arm_d.validator == default.validator
    assert arm_d.memory == default.memory
    assert arm_d.orchestration == default.orchestration
    assert arm_d.loop == default.loop

    harness = assemble_harness(
        arm_d,
        runtime_params={
            "pressure_compaction": {"max_context_tokens": 100_000},
            "informed_backtrack": {"max_context_tokens": 100_000},
        },
    )
    selected = harness.tool_selection.select(
        [{"name": n} for n in ("bash", "read_file", "grep", "bash_session", "write_stdin")]
    )
    assert [t["name"] for t in selected] == ["bash", "read_file"]
