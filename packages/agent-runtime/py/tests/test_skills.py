"""Skill seam tests: frontmatter compat, catalog selection, tool roundtrip,
first-round catalog injection.

Frontmatter fixtures mirror the shapes in deeppath-agent's built-in skills
(`src/local-backend/skills/*/SKILL.md`) — CJK descriptions, inline condition
arrays, `match`, quoted strings — so the Python parser stays field-compatible
with the TS loader it replaces on the CoreLoop path.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime import (
    CoreLoop,
    FilesystemSkillProvider,
    LoopConfig,
    LoopEvent,
    RouterToolExecutor,
    SkillExecutor,
    SkillHooks,
    ToolRouter,
    render_skill_catalog,
    select_catalog,
    skill_tool_descriptor,
    tool,
)
from steerable_agent_runtime.llm import LLMMessage, LLMStreamChunk, LLMUsage
from steerable_agent_runtime.skills import matches_conditions


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def write_skill(root: Path, dir_name: str, text: str) -> Path:
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    write_skill(
        root,
        "00-identity",
        """---
name: identity
description: Defines the assistant core role. Always loaded as the foundational skill.
priority: 1000
---

# 角色
你是测试助手。
""",
    )
    write_skill(
        root,
        "81-anti-deferred",
        """---
name: anti-deferred-execution
description: 零容忍规则——光说不做即违规。
priority: 880
conditions: [has-tools]
---

# 反拖延
""",
    )
    write_skill(
        root,
        "85-local-exec",
        """---
name: local-exec
description: Local shell / filesystem control via local_exec_shell.
priority: 700
conditions: [tool:local_exec_shell, tool:local_read_file]
match: any
---

# 本地执行
运行 `{scripts}/helper.py` 完成辅助任务。
""",
    )
    write_skill(
        root,
        "90-cflog",
        """---
name: cflog
displayName: 智能测井处理链
description: "CIFLog workflow guidance — connection, card scanning, replay."
priority: 600
conditions: [tool:cflog_list_cards]
match: all
---

# CIFLog
正文内容。
""",
    )
    return root


# ---------------------------------------------------------------------------
# Scripted provider (same pattern as test_antihallucination)
# ---------------------------------------------------------------------------


def make_provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self.stream_kwargs: list[dict[str, Any]] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw) -> AsyncIterator[LLMStreamChunk]:
            self.calls.append(list(messages))
            self.stream_kwargs.append(dict(kw))
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen() -> AsyncIterator[LLMStreamChunk]:
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                for tc in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=tc)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                    usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )

            return _gen()

    return _FakeProvider()


def tc(name: str, args: dict[str, Any] | None = None, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args or {})


async def collect(loop_run: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [e async for e in loop_run]


# ---------------------------------------------------------------------------
# Frontmatter parsing / provider behavior
# ---------------------------------------------------------------------------


def test_frontmatter_field_compat(skills_root: Path) -> None:
    provider = FilesystemSkillProvider([skills_root])
    by_name = {s.name: s for s in provider.list()}
    assert set(by_name) == {"identity", "anti-deferred-execution", "local-exec", "cflog"}

    cflog = by_name["cflog"]
    assert cflog.display_name == "智能测井处理链"
    assert cflog.description == "CIFLog workflow guidance — connection, card scanning, replay."
    assert cflog.priority == 600
    assert cflog.conditions == ("tool:cflog_list_cards",)
    assert cflog.match == "all"
    assert cflog.dir_name == "90-cflog"

    anti = by_name["anti-deferred-execution"]
    assert anti.description == "零容忍规则——光说不做即违规。"
    assert anti.conditions == ("has-tools",)
    assert anti.match == "any"  # default when conditions are present


def test_layer_criterion(skills_root: Path) -> None:
    provider = FilesystemSkillProvider([skills_root])
    by_name = {s.name: s for s in provider.list()}
    # priority >= 850 defaults to eager; below defaults to catalog.
    assert by_name["identity"].layer == "eager"
    assert by_name["anti-deferred-execution"].layer == "eager"
    assert by_name["local-exec"].layer == "catalog"
    assert by_name["cflog"].layer == "catalog"


def test_layer_explicit_overrides_priority(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "forced-eager",
        "---\nname: low-prio-eager\npriority: 100\nlayer: eager\n---\n\nbody\n",
    )
    write_skill(
        root,
        "forced-catalog",
        "---\nname: high-prio-catalog\npriority: 999\nlayer: catalog\n---\n\nbody\n",
    )
    provider = FilesystemSkillProvider([root])
    by_name = {s.name: s for s in provider.list()}
    assert by_name["low-prio-eager"].layer == "eager"
    assert by_name["high-prio-catalog"].layer == "catalog"


def test_disable_model_invocation(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "manual-only",
        "---\nname: manual-only\ndescription: user trigger only\n"
        "disable-model-invocation: true\n---\n\nbody\n",
    )
    provider = FilesystemSkillProvider([root])
    skill = provider.get("manual-only")
    assert skill is not None
    assert skill.model_invocable is False
    # Hidden from the catalog even though it is catalog-layer.
    assert select_catalog(provider.list()) == []


def test_name_validation_and_missing_name(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    write_skill(root, "bad-upper", "---\nname: BadName\n---\n\nbody\n")
    write_skill(root, "bad-hyphens", "---\nname: bad--name\n---\n\nbody\n")
    write_skill(root, "no-name", "---\ndescription: no name field\n---\n\nbody\n")
    write_skill(root, "no-frontmatter", "just a body, no frontmatter at all\n")
    write_skill(root, "good", "---\nname: good-name\n---\n\nbody\n")
    provider = FilesystemSkillProvider([root])
    assert [s.name for s in provider.list()] == ["good-name"]


def test_user_root_overrides_builtin(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    write_skill(builtin, "10-foo", "---\nname: foo\ndescription: builtin\n---\n\nbuiltin body\n")
    write_skill(user, "99-foo", "---\nname: foo\ndescription: user override\n---\n\nuser body\n")
    provider = FilesystemSkillProvider([builtin, user])
    skills = provider.list()
    assert len(skills) == 1
    assert skills[0].description == "user override"
    skill = provider.get("foo")
    assert skill is not None
    assert skill.content == "user body"


def test_get_resolves_aliases(skills_root: Path) -> None:
    provider = FilesystemSkillProvider([skills_root])
    assert provider.get("cflog") is not None
    assert provider.get("CFLOG") is not None  # case-insensitive
    assert provider.get("90-cflog") is not None  # dir name
    assert provider.get("智能测井处理链") is not None  # display name
    assert provider.get("nonexistent") is None


def test_scripts_placeholder_resolved(skills_root: Path) -> None:
    provider = FilesystemSkillProvider([skills_root])
    skill = provider.get("local-exec")
    assert skill is not None
    assert "{scripts}" not in skill.content
    assert str(skills_root / "85-local-exec" / "scripts") in skill.content


def _summary(**overrides: Any):
    from steerable_agent_runtime import SkillSummary

    base: dict[str, Any] = {
        "name": "x",
        "description": "",
        "display_name": "",
        "priority": 500,
        "layer": "catalog",
        "model_invocable": True,
        "conditions": (),
        "match": "any",
        "dir_name": "x",
    }
    base.update(overrides)
    return SkillSummary(**base)


def test_conditions_matching() -> None:
    assert matches_conditions(_summary(conditions=()), set())
    assert matches_conditions(_summary(conditions=("a",)), {"a"})
    assert not matches_conditions(_summary(conditions=("a",)), {"b"})
    assert matches_conditions(_summary(conditions=("a", "b"), match="all"), {"a", "b"})
    assert not matches_conditions(_summary(conditions=("a", "b"), match="all"), {"a"})


# ---------------------------------------------------------------------------
# Catalog selection + rendering
# ---------------------------------------------------------------------------


def test_select_catalog_filters(skills_root: Path) -> None:
    provider = FilesystemSkillProvider([skills_root])
    # No conditions active: only unconditional catalog skills qualify — none
    # here (local-exec and cflog both carry conditions).
    assert select_catalog(provider.list()) == []
    # cflog tool active: cflog (match=all, single condition) listed.
    catalog = select_catalog(provider.list(), {"tool:cflog_list_cards"})
    assert [s.name for s in catalog] == ["cflog"]
    # local-exec via any-of conditions; eager skills never listed.
    catalog = select_catalog(provider.list(), {"tool:local_read_file", "has-tools"})
    assert [s.name for s in catalog] == ["local-exec"]
    # Exclusion drops by name / dir / displayName alike.
    assert select_catalog(provider.list(), {"tool:cflog_list_cards"}, exclude=["CFLOG"]) == []
    assert select_catalog(provider.list(), {"tool:cflog_list_cards"}, exclude=["90-cflog"]) == []


def test_render_skill_catalog(skills_root: Path) -> None:
    provider = FilesystemSkillProvider([skills_root])
    catalog = select_catalog(provider.list(), {"tool:cflog_list_cards", "tool:local_read_file"})
    text = render_skill_catalog(catalog)
    assert "# Available skills" in text
    assert "`skill`" in text
    assert "- local-exec: Local shell / filesystem control" in text
    assert "- cflog(智能测井处理链): CIFLog workflow guidance" in text
    # Eager-layer skills are not part of the catalog.
    assert "identity" not in text


def test_skill_tool_descriptor_shape() -> None:
    descriptor = skill_tool_descriptor()
    assert descriptor["type"] == "function"
    assert descriptor["function"]["name"] == "skill"
    assert descriptor["function"]["parameters"]["required"] == ["name"]


# ---------------------------------------------------------------------------
# SkillHooks: first-round catalog injection
# ---------------------------------------------------------------------------


async def test_catalog_injected_first_round_only(skills_root: Path) -> None:
    provider = make_provider(
        [
            {"tool_calls": [tc("get_data")]},
            {"content": "done"},
        ]
    )
    router = ToolRouter()

    @tool(name="get_data", description="fetch")
    async def get_data() -> str:
        return "data"

    router.register(get_data)
    loop = CoreLoop(
        provider,
        RouterToolExecutor(router),
        LoopConfig(),
        hooks=SkillHooks(
            FilesystemSkillProvider([skills_root]),
            conditions={"tool:local_read_file"},
        ),
    )
    events = await collect(
        loop.run(
            [
                LLMMessage.text_of("system", "BASE PROMPT"),
                LLMMessage.text_of("user", "hi"),
            ],
            tools=[{"type": "function", "function": {"name": "get_data"}}],
        )
    )

    # Wave 1: the catalog is an appended standalone system message (hooks
    # never rewrite in place) — the base prompt stays byte-identical and the
    # catalog lands right after the first user message.
    first_request = provider.calls[0]
    assert first_request[0].role == "system"
    assert first_request[0].content_text == "BASE PROMPT"
    catalog_msgs = [
        m
        for m in first_request
        if m.role == "system" and "# Available skills" in m.content_text
    ]
    assert len(catalog_msgs) == 1
    assert "- local-exec:" in catalog_msgs[0].content_text
    # Injection is a declared append → hook_action with the skill label.
    actions = [e for e in events if e.kind == "hook_action"]
    assert any(
        e.data.get("action") == "skill_catalog" and e.data.get("round") == 0
        for e in actions
    ), [e.data for e in actions]
    # Second round: not re-appended (single catalog copy, identical prompt).
    second_catalog = [
        m
        for m in provider.calls[1]
        if m.role == "system" and "# Available skills" in m.content_text
    ]
    assert len(second_catalog) == 1
    assert second_catalog[0].content_text == catalog_msgs[0].content_text


async def test_catalog_injected_without_system_message(skills_root: Path) -> None:
    provider = make_provider([{"content": "done"}])
    loop = CoreLoop(
        provider,
        RouterToolExecutor(ToolRouter()),
        LoopConfig(),
        hooks=SkillHooks(
            FilesystemSkillProvider([skills_root]),
            conditions={"tool:local_read_file"},
        ),
    )
    await collect(loop.run([LLMMessage.text_of("user", "hi")]))
    # No base system message: the catalog is appended after the user message.
    first_request = provider.calls[0]
    assert first_request[0].role == "user"
    catalog = first_request[1]
    assert catalog.role == "system"
    assert "# Available skills" in catalog.content_text


async def test_empty_catalog_leaves_transcript_untouched(skills_root: Path) -> None:
    provider = make_provider([{"content": "done"}])
    loop = CoreLoop(
        provider,
        RouterToolExecutor(ToolRouter()),
        LoopConfig(),
        # No conditions active → no catalog skills → no rewrite, no event.
        hooks=SkillHooks(FilesystemSkillProvider([skills_root])),
    )
    events = await collect(
        loop.run(
            [
                LLMMessage.text_of("system", "BASE PROMPT"),
                LLMMessage.text_of("user", "hi"),
            ]
        )
    )
    assert provider.calls[0][0].content_text == "BASE PROMPT"
    assert not [
        e for e in events if e.kind == "hook_action" and e.data.get("action") == "skill_catalog"
    ]


# ---------------------------------------------------------------------------
# SkillExecutor: tool roundtrip
# ---------------------------------------------------------------------------


async def test_skill_tool_roundtrip(skills_root: Path) -> None:
    provider = make_provider(
        [
            {"tool_calls": [tc("skill", {"name": "cflog"})]},
            {"content": "按技能执行完毕"},
        ]
    )
    executor = SkillExecutor(
        RouterToolExecutor(ToolRouter()),
        FilesystemSkillProvider([skills_root]),
        conditions={"tool:cflog_list_cards"},
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = await collect(
        loop.run(
            [LLMMessage.text_of("user", "用 cflog 技能处理")],
            tools=[skill_tool_descriptor()],
        )
    )

    results = [e for e in events if e.kind == "tool_call_result"]
    assert len(results) == 1
    assert results[0].data["success"] is True
    # The body enters the transcript as the tool message (model-visible ⟺
    # logged); tool messages serialize the ToolResult payload as JSON.
    tool_messages = [m for m in provider.calls[1] if m.role == "tool"]
    assert len(tool_messages) == 1
    payload = json.loads(tool_messages[0].content_text)
    assert payload["data"] == {"skill": "cflog"}
    assert payload["message"].startswith('<skill_content name="cflog">\n')
    assert "# CIFLog" in payload["message"]
    assert payload["message"].endswith("</skill_content>")
    done = [e for e in events if e.kind == "completion" and e.data.get("status") == "completed"]
    assert len(done) == 1


async def test_skill_tool_unknown_name_lists_available(skills_root: Path) -> None:
    provider = make_provider(
        [
            {"tool_calls": [tc("skill", {"name": "nosuch"})]},
            {"content": "ok"},
        ]
    )
    executor = SkillExecutor(
        RouterToolExecutor(ToolRouter()),
        FilesystemSkillProvider([skills_root]),
        conditions={"tool:cflog_list_cards"},
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = await collect(
        loop.run([LLMMessage.text_of("user", "hi")], tools=[skill_tool_descriptor()])
    )
    results = [e for e in events if e.kind == "tool_call_result"]
    assert results[0].data["success"] is False
    tool_messages = [m for m in provider.calls[1] if m.role == "tool"]
    assert "Unknown skill: nosuch" in tool_messages[0].content_text
    assert "cflog" in tool_messages[0].content_text  # available list guides the retry


async def test_skill_tool_rejects_non_model_invocable(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    write_skill(
        root,
        "manual",
        "---\nname: manual\ndescription: d\ndisable-model-invocation: true\n---\n\nbody\n",
    )
    provider = make_provider(
        [{"tool_calls": [tc("skill", {"name": "manual"})]}, {"content": "ok"}]
    )
    executor = SkillExecutor(RouterToolExecutor(ToolRouter()), FilesystemSkillProvider([root]))
    loop = CoreLoop(provider, executor, LoopConfig())
    events = await collect(
        loop.run([LLMMessage.text_of("user", "hi")], tools=[skill_tool_descriptor()])
    )
    results = [e for e in events if e.kind == "tool_call_result"]
    assert results[0].data["success"] is False
    tool_messages = [m for m in provider.calls[1] if m.role == "tool"]
    assert "user-invocable only" in tool_messages[0].content_text


async def test_skill_tool_rejects_excluded(skills_root: Path) -> None:
    provider = make_provider(
        [{"tool_calls": [tc("skill", {"name": "cflog"})]}, {"content": "ok"}]
    )
    executor = SkillExecutor(
        RouterToolExecutor(ToolRouter()),
        FilesystemSkillProvider([skills_root]),
        conditions={"tool:cflog_list_cards"},
        exclude=["cflog"],
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = await collect(
        loop.run([LLMMessage.text_of("user", "hi")], tools=[skill_tool_descriptor()])
    )
    results = [e for e in events if e.kind == "tool_call_result"]
    assert results[0].data["success"] is False
    tool_messages = [m for m in provider.calls[1] if m.role == "tool"]
    assert "not available in this mode" in tool_messages[0].content_text


async def test_skill_executor_passes_through_other_tools(skills_root: Path) -> None:
    provider = make_provider(
        [{"tool_calls": [tc("get_data")]}, {"content": "done"}]
    )
    router = ToolRouter()

    @tool(name="get_data", description="fetch")
    async def get_data() -> str:
        return "real-data"

    router.register(get_data)
    executor = SkillExecutor(
        RouterToolExecutor(router), FilesystemSkillProvider([skills_root])
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = await collect(
        loop.run(
            [LLMMessage.text_of("user", "hi")],
            tools=[{"type": "function", "function": {"name": "get_data"}}],
        )
    )
    results = [e for e in events if e.kind == "tool_call_result"]
    assert results[0].data["success"] is True
    tool_messages = [m for m in provider.calls[1] if m.role == "tool"]
    assert "real-data" in tool_messages[0].content_text


async def test_skill_tool_missing_name_argument(skills_root: Path) -> None:
    provider = make_provider(
        [{"tool_calls": [tc("skill", {})]}, {"content": "ok"}]
    )
    executor = SkillExecutor(
        RouterToolExecutor(ToolRouter()), FilesystemSkillProvider([skills_root])
    )
    loop = CoreLoop(provider, executor, LoopConfig())
    events = await collect(
        loop.run([LLMMessage.text_of("user", "hi")], tools=[skill_tool_descriptor()])
    )
    results = [e for e in events if e.kind == "tool_call_result"]
    assert results[0].data["success"] is False
    tool_messages = [m for m in provider.calls[1] if m.role == "tool"]
    assert "missing required argument" in tool_messages[0].content_text
