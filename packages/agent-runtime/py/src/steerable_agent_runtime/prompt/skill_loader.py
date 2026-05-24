"""Skill loader -- discovers and parses skill directories.

Follows the Agent Skills standard (agentskills.io/specification) with custom extensions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_NAME_MAX_LEN = 64
_DESC_MAX_LEN = 1024


def _validate_name(name: str, source: str) -> bool:
    """Validate skill name per Agent Skills specification."""
    if not name or len(name) > _NAME_MAX_LEN:
        logger.warning("skill_name_invalid length=%d source=%s", len(name), source)
        return False
    if "--" in name:
        logger.warning("skill_name_consecutive_hyphens name=%s source=%s", name, source)
        return False
    if not _NAME_RE.match(name):
        logger.warning("skill_name_bad_format name=%s source=%s", name, source)
        return False
    return True


@dataclass
class SkillModule:
    """A single loaded skill with parsed metadata and content."""

    name: str
    description: str = ""
    compatibility: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    priority: int = 500
    tags: list[str] = field(default_factory=list)
    conditional: str | None = None
    content: str = ""
    dir_name: str = ""


def _parse_skill_dir(skill_dir: Path) -> SkillModule | None:
    """Parse a skill directory's SKILL.md into a SkillModule."""
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return None

    try:
        raw = skill_file.read_text(encoding="utf-8")
    except Exception:
        logger.warning("skill_loader_read_error path=%s", skill_file, exc_info=True)
        return None

    frontmatter: dict = {}
    body = raw

    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                logger.warning(
                    "skill_loader_yaml_error path=%s", skill_file, exc_info=True,
                )
            body = parts[2]

    content = body.strip()
    if not content:
        return None

    name = frontmatter.get("name", "")
    if not name:
        logger.warning("skill_missing_name dir=%s", skill_dir.name)
        return None

    if not _validate_name(name, skill_file.as_posix()):
        return None

    description = frontmatter.get("description", "")
    if not description:
        logger.warning("skill_missing_description name=%s", name)

    if len(description) > _DESC_MAX_LEN:
        logger.warning(
            "skill_description_too_long name=%s len=%d max=%d",
            name, len(description), _DESC_MAX_LEN,
        )

    return SkillModule(
        name=name,
        description=description,
        compatibility=frontmatter.get("compatibility", ""),
        metadata=frontmatter.get("metadata") or {},
        priority=int(frontmatter.get("priority", 500)),
        tags=frontmatter.get("tags") or [],
        conditional=frontmatter.get("conditional"),
        content=content,
        dir_name=skill_dir.name,
    )


def load_skills(
    *,
    conditions: set[str] | None = None,
    skills_dir: Path,
) -> list[SkillModule]:
    """Load all skill modules from disk.

    Each subdirectory of ``skills_dir`` that contains a ``SKILL.md``
    file is treated as a skill.
    """
    directory = Path(skills_dir)
    if not directory.is_dir():
        logger.warning("skill_loader_dir_missing dir=%s", directory)
        return []

    active_conditions = conditions or set()
    modules: list[SkillModule] = []

    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue

        module = _parse_skill_dir(child)
        if module is None:
            continue

        if module.conditional and module.conditional not in active_conditions:
            continue

        modules.append(module)

    modules.sort(key=lambda m: m.dir_name)

    logger.debug(
        "skill_loader_loaded count=%d names=%s",
        len(modules),
        [m.name for m in modules],
    )
    return modules
