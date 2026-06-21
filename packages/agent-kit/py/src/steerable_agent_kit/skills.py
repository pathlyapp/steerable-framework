from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from steerable_agent_runtime.prompt.skill_loader import SkillModule, load_skills

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillPack:
    name: str                              # e.g. "deeppath-core"
    directory: Path                        # Directory containing subdirectories with SKILL.md files
    priority: int = 100                    # Assembly priority (higher priority packs are assembled first or last)


class SkillEngine:
    """Framework service to load, sort, and assemble SKILL.md files from various SkillPacks."""

    def __init__(self) -> None:
        self._packs: list[SkillPack] = []

    def register_pack(self, pack: SkillPack) -> None:
        """Register a SkillPack to be included in skill loading."""
        if any(p.name == pack.name for p in self._packs):
            logger.warning("SkillPack '%s' already registered. Overwriting.", pack.name)
            self._packs = [p for p in self._packs if p.name != pack.name]
        self._packs.append(pack)
        # Sort packs by priority (higher priority first)
        self._packs.sort(key=lambda p: p.priority, reverse=True)

    def list_packs(self) -> list[SkillPack]:
        """List all registered SkillPacks."""
        return list(self._packs)

    def build_system_skills(
        self,
        *,
        conditions: set[str] | None = None,
        enabled_skills: set[str] | None = None,
    ) -> str:
        """Load and assemble SKILL.md instructions across all registered packs.

        If `enabled_skills` is provided, only skills whose names are in that set will be included.
        """
        all_modules: list[SkillModule] = []

        # Load skills from each registered pack
        for pack in self._packs:
            if not pack.directory.is_dir():
                logger.warning("SkillPack '%s' directory does not exist: %s", pack.name, pack.directory)
                continue
            try:
                # Load skills in this pack's directory
                modules = load_skills(conditions=conditions or set(), skills_dir=pack.directory)
                all_modules.extend(modules)
            except Exception as e:
                logger.exception("Failed to load skills from SkillPack '%s'", pack.name)

        # Sort all loaded modules first by pack priority, then by skill-defined priority (if any)
        # Note: SkillModule contains a priority field (defaults to 500) from frontmatter.
        all_modules.sort(key=lambda m: m.priority)

        # Filter by enabled_skills if specified
        if enabled_skills is not None:
            all_modules = [m for m in all_modules if m.name in enabled_skills]

        # Assemble the markdown prompt
        sections: list[str] = []
        for mod in all_modules:
            # Format each skill nicely with markdown headers
            sections.append(f"# Skill: {mod.name}\n\n{mod.content}")

        return "\n\n".join(sections)
