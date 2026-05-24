"""SystemPromptBuilder -- unified system prompt builder.

Loads skill modules from SKILL.md directories and assembles them into a single system prompt
with token budgeting and priority-based trimming.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, Sequence

from steerable_agent_runtime.prompt.skill_loader import SkillModule, load_skills
from steerable_agent_runtime.token_calculator import count_tokens

logger = logging.getLogger(__name__)

_FALLBACK_PROMPT = (
    "# 角色\n\n"
    "你是 AI 行动助手。"
    "通过工具直接操作并服务用户。用中文回复。"
)


class AgentProfileLike(Protocol):
    """Protocol for duck-typing agent profiles in the prompt builder."""

    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str | None: ...

    @property
    def role_prompt(self) -> str | None: ...

    @property
    def forbidden_prompt(self) -> str | None: ...

    @property
    def skill_ids(self) -> Sequence[str] | None: ...


class SystemPromptBuilder:
    """Build the unified system prompt from skill files."""

    def __init__(
        self,
        *,
        skills_dir: Path | str,
        conditions: set[str] | None = None,
        platform_hint: str = "",
        model_id: str = "gpt-4",
        agent_profile: AgentProfileLike | dict[str, Any] | None = None,
        chat_system_prompt: str | None = None,
        token_budget: int = 24000,
    ) -> None:
        """Initialize the prompt builder.

        Args:
            skills_dir: Path to the skills folder.
            conditions: Set of active runtime conditions (e.g., {"local_exec"}).
            platform_hint: Custom hint for platforms (e.g. system info / OS).
            model_id: Model ID used for token budgeting calculations.
            agent_profile: An object or dict describing custom agent settings.
            chat_system_prompt: Additional conversation-level settings.
            token_budget: Max token limit for loaded skills.
        """
        self._skills_dir = Path(skills_dir)
        self._conditions = conditions or set()
        self._platform_hint = platform_hint
        self._model_id = model_id
        self._agent_profile = agent_profile
        self._chat_system_prompt = (chat_system_prompt or "").strip() or None
        self._token_budget = token_budget

    def build(self) -> str:
        """Load skills and assemble the final system prompt."""
        modules = load_skills(conditions=self._conditions, skills_dir=self._skills_dir)

        # Filter modules if agent profile specifies custom skill_ids
        skill_ids = self._get_profile_attr("skill_ids")
        if skill_ids is not None:
            # Keep skill if its name matches or is in skill_ids (or starts with its numeric prefix)
            skill_ids_set = set(skill_ids)
            filtered_modules: list[SkillModule] = []
            for m in modules:
                # Matches either "00-identity" or "identity"
                if m.name in skill_ids_set or m.dir_name in skill_ids_set:
                    filtered_modules.append(m)
                else:
                    # Also match stripped names
                    stripped_dir = m.dir_name.split("-", 1)[-1] if "-" in m.dir_name else m.dir_name
                    if stripped_dir in skill_ids_set:
                        filtered_modules.append(m)
            modules = filtered_modules

        if not modules:
            logger.warning("prompt_builder_no_skills, using fallback")
            fallback = _FALLBACK_PROMPT
            agent_suffix = self._build_agent_suffix()
            if agent_suffix:
                return f"{fallback}\n\n{agent_suffix}"
            return fallback

        modules = self._apply_budget(modules)
        parts = self._assemble(modules)

        agent_suffix = self._build_agent_suffix()
        if agent_suffix:
            parts.append(agent_suffix)

        prompt = "\n\n".join(parts)
        logger.debug(
            "prompt_builder_assembled modules=%d chars=%d",
            len(parts), len(prompt)
        )
        return prompt

    def _get_profile_attr(self, name: str) -> Any:
        if self._agent_profile is None:
            return None
        if isinstance(self._agent_profile, dict):
            return self._agent_profile.get(name)
        return getattr(self._agent_profile, name, None)

    def _build_agent_suffix(self) -> str:
        """Assemble role + forbidden prompt + chat-level instructions."""
        parts: list[str] = []

        role_prompt = self._get_profile_attr("role_prompt")
        if role_prompt:
            parts.append(f"# 当前 Agent 设定\n\n{role_prompt.strip()}")

        forbidden_prompt = self._get_profile_attr("forbidden_prompt")
        if forbidden_prompt:
            parts.append(
                "# 禁止事项（优先级最高）\n\n"
                "以下内容严格禁止执行或响应，如遇到相关请求请直接拒绝并说明：\n\n"
                f"{forbidden_prompt.strip()}"
            )

        if self._chat_system_prompt:
            parts.append(
                f"# 本次对话补充设定\n\n{self._chat_system_prompt}"
            )

        return "\n\n".join(parts)

    def _count(self, text: str) -> int:
        """Count raw tokens for budget decisions."""
        return count_tokens(text, self._model_id, raw=True)

    def _apply_budget(self, modules: list[SkillModule]) -> list[SkillModule]:
        """Drop lowest-priority skills if total exceeds budget."""
        total = sum(self._count(m.content) for m in modules)

        if total <= self._token_budget:
            return modules

        by_priority_asc = sorted(modules, key=lambda m: m.priority)
        dropped: set[str] = set()
        excess = total - self._token_budget

        for m in by_priority_asc:
            if excess <= 0:
                break
            dropped.add(m.name)
            excess -= self._count(m.content)
            logger.info(
                "prompt_builder_budget_drop name=%s priority=%d tokens=%d",
                m.name, m.priority, self._count(m.content),
            )

        return [m for m in modules if m.name not in dropped]

    def _assemble(self, modules: list[SkillModule]) -> list[str]:
        """Build the ordered list of prompt sections."""
        parts: list[str] = []
        for module in modules:
            content = module.content
            if module.name == "local-exec" and self._platform_hint:
                content += f"\n{self._platform_hint}"
            parts.append(content)
        return parts
