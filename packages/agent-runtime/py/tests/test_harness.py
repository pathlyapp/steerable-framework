"""W1.1: harness strategy modules — protocols, baselines, contract notes."""

from __future__ import annotations

import pytest

from steerable_agent_runtime.harness import (
    STRATEGY_REGISTRY,
    ContextStrategy,
    FullToolset,
    InformedBacktrack,
    MemoryStrategy,
    MinimalToolset,
    NoRetry,
    NullContext,
    NullValidator,
    OrchestrationStrategy,
    PressureCompaction,
    ProgressiveDisclosure,
    RetryStrategy,
    SimpleRetry,
    SingleAgent,
    Stateless,
    FilesystemState,
    PoolOrchestration,
    ToolSelection,
    ValidationStrategy,
)
from steerable_agent_runtime.hooks import NoopHooks, PreStepAction
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.llm.errors import LLMError
from steerable_agent_runtime.storage import InMemoryStorage
from steerable_agent_runtime.tools import ToolRouter


class _Ctx:
    """Minimal stand-in for LoopContext (the strategies read attributes only)."""

    def __init__(self, round_index: int = 0) -> None:
        self.round_index = round_index
        self.chat_id = None
        self.last_prompt_tokens = None
        self.last_prompt_transcript_len = 0


def _descriptor(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


# -- protocol conformance + contract notes (1.1.1 / 1.1.3) ------------------

_PROTOCOLS = {
    "context": ContextStrategy,
    "retry": RetryStrategy,
    "validator": ValidationStrategy,
    "tools": ToolSelection,
    "memory": MemoryStrategy,
    "orchestration": OrchestrationStrategy,
}


def test_registry_covers_all_six_dimensions() -> None:
    assert set(STRATEGY_REGISTRY) == set(_PROTOCOLS)
    for dimension, implementations in STRATEGY_REGISTRY.items():
        assert implementations, dimension
        for impl_name, cls in implementations.items():
            instance = _construct(cls)
            assert isinstance(instance, _PROTOCOLS[dimension]), (
                f"{dimension}/{impl_name} does not satisfy its protocol"
            )
            # 1.1.3: every implementation carries its assumption contract.
            assert instance.name == impl_name
            assert instance.assumes.strip(), f"{dimension}/{impl_name}"


def _construct(cls):
    """Registry instances for conformance checks (config-bearing ones get
    minimal valid params)."""
    if cls is PressureCompaction:
        return cls(max_context_tokens=1000)
    if cls is InformedBacktrack:
        return cls(max_context_tokens=1000)
    if cls is FilesystemState:
        return cls(notes_path=__import__("pathlib").Path("/tmp/notes.md"))
    return cls()


# -- context -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_context_passes_everything_through() -> None:
    hooks = NullContext().hooks()
    action = await hooks.pre_step([LLMMessage.text_of("user", "hi")], _Ctx())
    assert action.kind == "proceed"
    assert action.rewrite is None


@pytest.mark.asyncio
async def test_pressure_compaction_owns_only_the_pre_step_slice() -> None:
    hooks = PressureCompaction(max_context_tokens=10).hooks()
    # Over-threshold transcript → a declared rewrite from pre_step.
    transcript = [LLMMessage.text_of("user", "word " * 50)]
    action = await hooks.pre_step(transcript, _Ctx())
    assert action.kind == "proceed"
    assert action.rewrite is not None
    # The on_request_error slice is projected OUT (InformedBacktrack owns it).
    error = LLMError("overflow", kind="context_overflow", provider="test")
    retry = await hooks.on_request_error(error, [], _Ctx())
    assert retry.kind == "fail"


def test_pressure_compaction_wires_micro_compact_interval() -> None:
    """The spec-facing ``micro_compact_interval_rounds`` param reaches the
    CompactionHooks it builds (CC time-based microcompact parity); default 0
    keeps it off so the prompt-cache prefix stays intact."""
    assert (
        PressureCompaction(max_context_tokens=1000).micro_compact_interval_rounds
        == 0
    )
    hooks = PressureCompaction(
        max_context_tokens=1000, micro_compact_interval_rounds=5
    ).hooks()
    # _PreStepOnly wraps the CompactionHooks; reach the inner instance.
    inner = getattr(hooks, "_inner", None) or getattr(hooks, "_hooks", None)
    assert inner is not None
    assert inner._micro_compact_interval == 5


# -- retry -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retry_fails_every_error() -> None:
    hooks = NoRetry().hooks()
    error = LLMError("reset", kind="transport", provider="test")
    action = await hooks.on_request_error(error, [], _Ctx())
    assert action.kind == "fail"


@pytest.mark.asyncio
async def test_simple_retry_retries_transient_and_projects_out_pre_step() -> None:
    hooks = SimpleRetry(max_attempts=3).hooks()
    error = LLMError("reset", kind="transport", provider="test")
    action = await hooks.on_request_error(error, [], _Ctx())
    assert action.kind == "retry"
    assert action.delay_ms > 0
    # pre_step is projected out (context dimension owns it).
    pre = await hooks.pre_step([], _Ctx())
    assert pre.kind == "proceed" and pre.rewrite is None and not pre.appends


@pytest.mark.asyncio
async def test_informed_backtrack_rewrites_on_overflow() -> None:
    hooks = InformedBacktrack(max_context_tokens=10).hooks()
    error = LLMError("overflow", kind="context_overflow", provider="test")
    transcript = [LLMMessage.text_of("user", "word " * 50)]
    action = await hooks.on_request_error(error, transcript, _Ctx())
    assert action.kind == "retry"
    assert action.rewrite is not None


# -- validator ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_validator_accepts() -> None:
    from steerable_agent_runtime.hooks import CompletionDraft

    hooks = NullValidator().hooks()
    draft = CompletionDraft(
        status="completed",
        reason="",
        content="done",
        round_index=0,
        had_tool_calls=False,
        tool_calls_used=0,
        tool_successes=0,
    )
    action = await hooks.before_completion(draft, _Ctx())
    assert action.kind == "accept"


def test_self_critique_passes_the_instruction_into_the_judge() -> None:
    """Harbor's self_critique arm is a silent no-op if user_question stays
    empty: claimed/eager-deferred need exec intent, and the grounding judge
    is prompted with an empty 用户提问."""
    from steerable_agent_runtime.harness import SelfCritique

    class _Provider:
        name = "fake"
        model = "fake-model"

    hooks = SelfCritique(user_question="Write /app/out.txt").hooks(
        provider=_Provider()
    )
    config = hooks._inner._config
    assert config.user_question.startswith("Execute the following.")
    assert "Write /app/out.txt" in config.user_question
    assert config.tools_available is True
    assert config.enable_routing is False

    already = SelfCritique(user_question="execute the hidden tests").hooks(
        provider=_Provider()
    )
    assert already._inner._config.user_question == "execute the hidden tests"


# -- tools -------------------------------------------------------------------


def test_full_toolset_is_identity() -> None:
    tools = [_descriptor("bash"), _descriptor("grep")]
    assert FullToolset().select(tools) == tools


def test_minimal_toolset_filters_by_nested_name() -> None:
    tools = [_descriptor("bash"), _descriptor("grep"), _descriptor("read_file")]
    selected = MinimalToolset().select(tools)
    names = [t["function"]["name"] for t in selected]
    assert names == ["bash", "read_file"]


def test_progressive_disclosure_lists_direct_tier_and_appends_search() -> None:
    router = ToolRouter()
    router.register(lambda: "x", name="bash", description="Run commands")
    router.register(
        lambda: "y", name="grep", description="Search files", exposure="deferred"
    )
    strategy = ProgressiveDisclosure()
    strategy.register(router)
    selected = strategy.select([_descriptor("bash"), _descriptor("grep")])
    names = [t["function"]["name"] for t in selected]
    assert names == ["bash", "tool_search"]


# -- memory ------------------------------------------------------------------


def test_stateless_storage_is_in_memory() -> None:
    assert isinstance(Stateless().storage(), InMemoryStorage)


@pytest.mark.asyncio
async def test_filesystem_state_injects_notes_once_per_turn(tmp_path) -> None:
    notes = tmp_path / "AGENTS.md"
    notes.write_text("remember the constraint", encoding="utf-8")
    hooks = FilesystemState(notes_path=notes).hooks()

    action = await hooks.pre_step([], _Ctx(round_index=0))
    assert action.kind == "proceed"
    assert action.appends and "remember the constraint" in action.appends[0].message.content[0].text

    later = await hooks.pre_step([], _Ctx(round_index=3))
    assert later.kind == "proceed" and not later.appends


@pytest.mark.asyncio
async def test_filesystem_state_missing_notes_file_is_a_noop(tmp_path) -> None:
    hooks = FilesystemState(notes_path=tmp_path / "absent.md").hooks()
    action = await hooks.pre_step([], _Ctx(round_index=0))
    assert action.kind == "proceed" and not action.appends


# -- orchestration -----------------------------------------------------------


class _FakeExecutor:
    pass


class _FakeProvider:
    name = "fake"
    model = "fake-model"


def test_single_agent_wrap_is_identity() -> None:
    executor = _FakeExecutor()
    assert SingleAgent().wrap(executor, provider=_FakeProvider()) is executor


def test_subagent_delegation_wraps_with_orchestration_executor() -> None:
    from steerable_agent_runtime.orchestration import OrchestrationExecutor

    wrapped = PoolOrchestration().wrap(_FakeExecutor(), provider=_FakeProvider())
    assert isinstance(wrapped, OrchestrationExecutor)


# -- four-level notes discovery (CC User/Local/Project/Managed parity) -------


def test_discover_notes_files_orders_all_four_levels(tmp_path, monkeypatch) -> None:
    from steerable_agent_runtime.harness import discover_notes_files

    monkeypatch.delenv("STEERABLE_MANAGED_NOTES_PATH", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".steerable").mkdir()
    (home / ".steerable" / "AGENTS.md").write_text("user", encoding="utf-8")
    root = tmp_path / "repo"
    nested = root / "pkg" / "mod"
    nested.mkdir(parents=True)
    (root / "AGENTS.md").write_text("project", encoding="utf-8")
    (root / "AGENTS.local.md").write_text("local", encoding="utf-8")
    (nested / "AGENTS.md").write_text("nearest", encoding="utf-8")

    found = discover_notes_files(nested, home=home, environ={})
    levels = [level for level, _ in found]
    assert levels == ["user", "project", "local", "project"]
    # Nearest project file reads last (most specific wins on conflict).
    assert found[-1][1] == nested / "AGENTS.md"


def test_discover_notes_files_managed_level_comes_first(tmp_path) -> None:
    from steerable_agent_runtime.harness import discover_notes_files

    managed = tmp_path / "managed.md"
    managed.write_text("policy", encoding="utf-8")
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("project", encoding="utf-8")

    found = discover_notes_files(
        cwd, home=tmp_path / "nohome", environ={"STEERABLE_MANAGED_NOTES_PATH": str(managed)}
    )
    assert found[0] == ("managed", managed)
    assert ("project", cwd / "AGENTS.md") in found


def test_discover_notes_files_empty_when_nothing_exists(tmp_path) -> None:
    from steerable_agent_runtime.harness import discover_notes_files

    assert discover_notes_files(tmp_path, home=tmp_path / "nohome", environ={}) == []


@pytest.mark.asyncio
async def test_discovered_notes_state_injects_union_with_disclaimer(tmp_path, monkeypatch) -> None:
    from steerable_agent_runtime.harness import DiscoveredNotesState

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nohome")
    monkeypatch.delenv("STEERABLE_MANAGED_NOTES_PATH", raising=False)
    (tmp_path / "AGENTS.md").write_text("project rule", encoding="utf-8")
    (tmp_path / "AGENTS.local.md").write_text("personal hint", encoding="utf-8")

    hooks = DiscoveredNotesState(cwd=tmp_path).hooks()
    action = await hooks.pre_step([], _Ctx(round_index=0))
    assert action.kind == "proceed" and action.appends
    text = action.appends[0].message.content[0].text
    assert "project rule" in text and "personal hint" in text
    assert 'level="project"' in text and 'level="local"' in text
    assert "background context, not user instructions" in text

    later = await hooks.pre_step([], _Ctx(round_index=2))
    assert later.kind == "proceed" and not later.appends


@pytest.mark.asyncio
async def test_discovered_notes_state_no_files_is_a_noop(tmp_path, monkeypatch) -> None:
    from steerable_agent_runtime.harness import DiscoveredNotesState

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nohome")
    monkeypatch.delenv("STEERABLE_MANAGED_NOTES_PATH", raising=False)
    hooks = DiscoveredNotesState(cwd=tmp_path).hooks()
    action = await hooks.pre_step([], _Ctx(round_index=0))
    assert action.kind == "proceed" and not action.appends


@pytest.mark.asyncio
async def test_single_notes_injection_carries_the_disclaimer(tmp_path) -> None:
    notes = tmp_path / "AGENTS.md"
    notes.write_text("remember the constraint", encoding="utf-8")
    hooks = FilesystemState(notes_path=notes).hooks()
    action = await hooks.pre_step([], _Ctx(round_index=0))
    text = action.appends[0].message.content[0].text
    assert "background context, not user instructions" in text
