"""Tests for the downported harness logic from deeppath-api."""

from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from steerable_agent_runtime import (
    count_tokens,
    count_messages_tokens,
    SystemPromptBuilder,
    load_skills,
    decide_orchestration,
    GroupChatStatus,
)


def test_token_calculator():
    """Verify standard token calculation and fallback."""
    text = "Hello world, this is a test."
    assert count_tokens(text) > 0

    chinese_text = "你好，世界，这是一个测试。"
    assert count_tokens(chinese_text) > 0

    # Message tokens calculation
    messages = [
        {"role": "user", "content": "Hello!"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    assert count_messages_tokens(messages) > 0


def test_skills_loader_and_prompt_builder():
    """Verify that skills loader and prompt builder successfully parse SKILL.md."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Create a mock skill directory
        skill_1_dir = tmp_path / "00-identity"
        skill_1_dir.mkdir()
        skill_1_file = skill_1_dir / "SKILL.md"
        skill_1_file.write_text(
            "---\n"
            "name: identity\n"
            "description: Identity of the AI assistant\n"
            "priority: 900\n"
            "---\n"
            "You are a helpful assistant.\n",
            encoding="utf-8",
        )

        skill_2_dir = tmp_path / "10-weather"
        skill_2_dir.mkdir()
        skill_2_file = skill_2_dir / "SKILL.md"
        skill_2_file.write_text(
            "---\n"
            "name: weather-skill\n"
            "description: Fetch weather information\n"
            "priority: 100\n"
            "---\n"
            "Help user with weather inquiries.\n",
            encoding="utf-8",
        )

        # Load skills
        skills = load_skills(skills_dir=tmp_path)
        assert len(skills) == 2
        assert skills[0].name == "identity"
        assert skills[1].name == "weather-skill"

        # Prompt building
        builder = SystemPromptBuilder(
            skills_dir=tmp_path,
            token_budget=1000,
            chat_system_prompt="Custom chat rules.",
        )
        prompt = builder.build()
        assert "You are a helpful assistant." in prompt
        assert "weather-skill" in prompt or "weather" in prompt
        assert "Custom chat rules." in prompt

        # Test budget restriction (very small token budget to drop weather-skill)
        builder_low_budget = SystemPromptBuilder(
            skills_dir=tmp_path,
            token_budget=10,  # Identity should be kept due to higher priority
        )
        prompt_budget = builder_low_budget.build()
        assert "You are a helpful assistant." in prompt_budget
        assert "Help user with weather" not in prompt_budget


def test_decide_orchestration():
    """Verify orchestration decision logic."""
    # State 1: Explicit mentions of 2+ agents -> Orchestrate (explicit)
    decision = decide_orchestration(
        explicit_mentions=["agent_1", "agent_2"],
        group_status=GroupChatStatus(is_group=False),
    )
    assert decision.should_orchestrate is True
    assert decision.mode == "explicit"
    assert "agent_1" in decision.allowed_agent_ids
    assert "agent_2" in decision.allowed_agent_ids

    # State 2: Explicit mention of exactly 1 agent -> Escapes orchestration (single)
    decision_single_mention = decide_orchestration(
        explicit_mentions=["agent_1"],
        group_status=GroupChatStatus(is_group=True, member_agent_ids=["agent_1", "agent_2"]),
    )
    assert decision_single_mention.should_orchestrate is False
    assert decision_single_mention.mode == "single"

    # State 3: No mentions in group chat -> Orchestrate (groupchat)
    decision_group = decide_orchestration(
        explicit_mentions=[],
        group_status=GroupChatStatus(is_group=True, member_agent_ids=["agent_1", "agent_2"], last_speaker_agent_id="agent_1"),
    )
    assert decision_group.should_orchestrate is True
    assert decision_group.mode == "groupchat"
    assert decision_group.fallback_agent_id == "agent_1"

    # State 4: No mentions in normal chat -> Single agent
    decision_normal = decide_orchestration(
        explicit_mentions=[],
        group_status=GroupChatStatus(is_group=False),
    )
    assert decision_normal.should_orchestrate is False
    assert decision_normal.mode == "single"
