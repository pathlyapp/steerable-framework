import pytest
from pathlib import Path
from steerable_agent_kit import (
    ToolSpec,
    ToolContext,
    SkillPack,
    SkillEngine,
    ContextProvider,
    ContextEngine,
    EntitlementDecision,
    EntitlementGate,
)
from steerable_agent_protocol import ToolCall, ToolResult, ChatMessage


def test_tool_spec():
    async def dummy_handler(ctx: ToolContext, call: ToolCall) -> ToolResult:
        return ToolResult(success=True, data={"hello": ctx.user_id})

    spec = ToolSpec(
        name="test_tool",
        description="A test tool",
        json_schema={"type": "object"},
        handler=dummy_handler,
        mode="read",
    )

    assert spec.name == "test_tool"
    assert spec.mode == "read"


@pytest.mark.asyncio
async def test_skill_engine(tmp_path):
    # Set up dummy skills directory
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    
    skill_a_dir = skills_dir / "skill-a"
    skill_a_dir.mkdir()
    
    skill_a_file = skill_a_dir / "SKILL.md"
    skill_a_file.write_text("""---
name: skill-a
description: Test Skill A
priority: 200
---
System instructions for Skill A.
""", encoding="utf-8")

    pack = SkillPack(name="test-pack", directory=skills_dir, priority=100)
    engine = SkillEngine()
    engine.register_pack(pack)

    assert len(engine.list_packs()) == 1
    assert engine.list_packs()[0].name == "test-pack"

    prompt = engine.build_system_skills()
    assert "Skill: skill-a" in prompt
    assert "System instructions for Skill A." in prompt


@pytest.mark.asyncio
async def test_context_engine():
    class TestProvider:
        name = "test_prov"
        async def provide(self, *, user_id, session_id, query, state):
            return "Some mock database context records."

    engine = ContextEngine()
    engine.register(TestProvider())

    assert len(engine.list_providers()) == 1

    ctx_str = await engine.build(user_id="user1", session_id="sess1", query="test", state={})
    assert "Test_prov Context" in ctx_str
    assert "Some mock database context records." in ctx_str


def test_models():
    from steerable_agent_kit.models import AgentSessionBase, ChatMessageBase, HarnessTraceBase, HarnessTraceEventBase

    session = AgentSessionBase(sessionId="sess1", userId="user1", chatId="chat1")
    assert session.sessionId == "sess1"
    assert session.id is not None

    msg = ChatMessageBase(chatId="chat1", content="hello", role="user")
    assert msg.content == "hello"
    assert msg.role == "user"

    trace = HarnessTraceBase(traceId="tr1")
    assert trace.traceId == "tr1"
    assert trace.status == "running"

    event = HarnessTraceEventBase(traceId="tr1", kind="span", name="test_span", sequence=1)
    assert event.name == "test_span"

