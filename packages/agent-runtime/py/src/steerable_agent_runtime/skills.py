"""Skill seam — layered disclosure of SKILL.md prompt modules.

A *skill* is a directory containing a ``SKILL.md``: YAML-ish frontmatter
(name, description, priority, conditions, …) plus a markdown body of
instructions. Products inject foundational skills into the system prompt
directly; everything else is disclosed progressively — the model sees a
one-line-per-skill catalog and calls the ``skill`` tool to load a body on
demand. This is the pattern codex / Claude Code / Cursor converge on, and
it is what keeps a large skill library from blowing the prompt budget.

Three roles (capability seam):

- **Definition** — ``SkillSummary`` / ``SkillDefinition`` and the
  ``SkillProvider`` protocol.
- **Provider** — ``FilesystemSkillProvider`` reads ``<root>/<dir>/SKILL.md``.
  Frontmatter parsing is field-compatible with deeppath-agent's
  ``skill-loader.ts`` (same minimal YAML subset, same name validation,
  same user-root-overrides-builtin semantics) and adds two fields:
  ``layer: eager|catalog`` (default derived from priority) and the
  ecosystem's ``disable-model-invocation: true`` (Claude/codex) mapped to
  ``model_invocable=False``.
- **Consumer** — ``skill_tool_descriptor()`` (OpenAI schema to append to
  the tools list), ``SkillExecutor`` (``ToolExecutor`` decorator answering
  ``skill`` calls with the body wrapped in ``<skill_content>`` markers),
  and ``SkillHooks`` (``pre_step`` injects the catalog into the system
  message on the first round — a transcript rewrite, so the injection is
  recorded as a ``hook_action`` event: model-visible ⟺ logged).

Opt-in: hosts that don't want skills pass no provider and nothing changes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence, runtime_checkable

from steerable_agent_protocol.generated import ToolCall, ToolResult

from .hooks import NoopHooks, PreStepAction
from .llm import LLMMessage
from .loop import LoopContext, ToolExecutor

logger = logging.getLogger(__name__)

#: Priority at or above which a skill defaults to the eager layer (always
#: injected into the system prompt). Chosen so deeppath-agent's existing
#: built-ins keep their current behavior without frontmatter changes:
#: identity 1000 / plan-mode 950 / tool-usage 900 / anti-deferred 880 /
#: data-grounding 875 stay eager; proactive-coding 705 / local-exec 700 /
#: cflog 600 and user imports (default 500) become catalog.
EAGER_PRIORITY_THRESHOLD = 850

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_NAME_MAX_LEN = 64
_DESC_MAX_LEN = 1024

Layer = Literal["eager", "catalog"]


# ---------------------------------------------------------------------------
# Definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """Everything about a skill except its body (cheap to list)."""

    name: str
    description: str
    display_name: str
    priority: int
    layer: Layer
    #: False = user-invocable only (``/name``); hidden from the catalog and
    #: rejected by ``SkillExecutor`` (ecosystem ``disable-model-invocation``).
    model_invocable: bool
    conditions: tuple[str, ...]
    match: Literal["any", "all"]
    dir_name: str


@dataclass(frozen=True, slots=True)
class SkillDefinition(SkillSummary):
    """A skill with its body; ``content`` has ``{scripts}`` resolved."""

    content: str
    root: str


@runtime_checkable
class SkillProvider(Protocol):
    """Source of skills. ``list`` is the catalog view; ``get`` loads a body.

    ``get`` accepts the skill's ``name``, directory name, or display name
    (case-insensitive) — the same aliases the desktop's ``/`` trigger and
    forced-skill path accept, so a name the model copies out of the catalog
    always resolves.
    """

    def list(self) -> Sequence[SkillSummary]: ...

    def get(self, name: str) -> SkillDefinition | None: ...


def matches_conditions(skill: SkillSummary, active: set[str]) -> bool:
    """Condition gate, ported from the TS loader: no conditions = always;
    ``match: all`` requires every condition, otherwise any one suffices."""
    if not skill.conditions:
        return True
    if skill.match == "all":
        return all(c in active for c in skill.conditions)
    return any(c in active for c in skill.conditions)


def select_catalog(
    skills: Sequence[SkillSummary],
    conditions: set[str] | frozenset[str] = frozenset(),
    exclude: Sequence[str] = (),
    ignore_conditions: bool = False,
) -> list[SkillSummary]:
    """The model-visible catalog: catalog-layer, condition-matching,
    model-invocable skills, minus the host's exclusions (e.g. plan mode
    drops execution-oriented skills). ``ignore_conditions`` lists every
    catalog skill regardless of conditions (the desktop's all-round
    assistant persona opts into everything)."""
    active = set(conditions)
    excluded = {e.lower().strip() for e in exclude}

    def is_excluded(s: SkillSummary) -> bool:
        return (
            s.name.lower() in excluded
            or s.dir_name.lower() in excluded
            or (s.display_name != "" and s.display_name.lower() in excluded)
        )

    return [
        s
        for s in skills
        if s.layer == "catalog"
        and s.model_invocable
        and not is_excluded(s)
        and (ignore_conditions or matches_conditions(s, active))
    ]


def render_skill_catalog(
    skills: Sequence[SkillSummary], *, tool_name: str = "skill"
) -> str:
    """System-prompt section listing the catalog layer, one line per skill."""
    lines = [
        "# Available skills (load on demand)",
        "",
        "The skills below may be relevant to this turn; their full instructions "
        f"are not loaded yet. When the task matches one, call the `{tool_name}` "
        "tool with its `name` to load the instructions, then follow them. If "
        "none match, ignore this list — do not guess unlisted skill names.",
        "",
    ]
    for s in skills:
        label = s.name if not s.display_name else f"{s.name}({s.display_name})"
        lines.append(f"- {label}: {s.description}" if s.description else f"- {label}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Provider: filesystem
# ---------------------------------------------------------------------------


class FilesystemSkillProvider:
    """Reads ``<root>/<dir>/SKILL.md`` from each root, in order.

    Later roots override earlier ones on the same (case-insensitive) name —
    pass the built-in root first and the user root second to mirror the
    desktop's user-overrides-builtin semantics. Discovery happens once at
    construction; build a fresh provider per turn (skills can be imported
    mid-session).
    """

    def __init__(self, roots: Sequence[str | Path]) -> None:
        merged: dict[str, SkillDefinition] = {}
        for root in roots:
            for definition in _load_root(Path(root)):
                merged[definition.name.lower()] = definition
        self._skills = sorted(merged.values(), key=lambda d: d.dir_name)

    def list(self) -> Sequence[SkillSummary]:
        return self._skills

    def get(self, name: str) -> SkillDefinition | None:
        key = name.lower().strip()
        for skill in self._skills:
            if (
                skill.name.lower() == key
                or skill.dir_name.lower() == key
                or (skill.display_name != "" and skill.display_name.lower() == key)
            ):
                return skill
        return None


def _load_root(root: Path) -> list[SkillDefinition]:
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        logger.warning("skill root missing: %s", root)
        return []
    out: list[SkillDefinition] = []
    for skill_dir in entries:
        definition = _parse_skill_dir(skill_dir)
        if definition is not None:
            out.append(definition)
    return out


def _parse_skill_dir(skill_dir: Path) -> SkillDefinition | None:
    skill_file = skill_dir / "SKILL.md"
    try:
        if not skill_file.is_file():
            return None
        raw = skill_file.read_text(encoding="utf-8")
    except OSError:
        return None

    fm_raw = ""
    body = raw
    if raw.startswith("---"):
        rest = raw[3:]
        end = rest.find("\n---")
        if end != -1:
            fm_raw = rest[:end]
            body = re.sub(r"^\r?\n", "", rest[end + 4 :])

    content = body.strip()
    if not content:
        return None

    fm = _parse_frontmatter(fm_raw) if fm_raw.strip() else {}

    name = fm.get("name") if isinstance(fm.get("name"), str) else ""
    if not name:
        logger.warning("skill missing name: dir=%s", skill_dir.name)
        return None
    if not _valid_name(name, str(skill_file)):
        return None

    description = fm.get("description") if isinstance(fm.get("description"), str) else ""
    if len(description) > _DESC_MAX_LEN:
        logger.warning(
            "skill description too long: name=%s len=%d max=%d",
            name,
            len(description),
            _DESC_MAX_LEN,
        )

    priority_raw = fm.get("priority")
    priority = int(priority_raw) if isinstance(priority_raw, (int, float)) else 500

    conditions_raw = fm.get("conditions")
    conditions = tuple(conditions_raw) if isinstance(conditions_raw, list) else ()
    match: Literal["any", "all"] = (
        "all" if str(fm.get("match") or "").lower() == "all" else "any"
    )

    layer_raw = str(fm.get("layer") or "").lower()
    layer: Layer
    if layer_raw in ("eager", "catalog"):
        layer = layer_raw  # type: ignore[assignment]
    else:
        layer = "eager" if priority >= EAGER_PRIORITY_THRESHOLD else "catalog"

    # Ecosystem compat: Claude Code / codex mark user-only skills with
    # `disable-model-invocation: true`; they stay `/`-triggerable but leave
    # the catalog and get rejected by the skill tool.
    model_invocable = fm.get("disable-model-invocation") is not True

    # Same `{scripts}` resolution as the TS prompt assembly: point at the
    # skill's own scripts/ directory with uniform separators.
    scripts_path = str(skill_dir / "scripts")
    content = re.sub(r"\{scripts\}[/\\]", scripts_path + "/", content)
    content = content.replace("{scripts}", scripts_path)

    return SkillDefinition(
        name=name,
        description=description,
        display_name=(fm.get("displayName") or "").strip()
        if isinstance(fm.get("displayName"), str)
        else "",
        priority=priority,
        layer=layer,
        model_invocable=model_invocable,
        conditions=conditions,
        match=match,
        content=content,
        dir_name=skill_dir.name,
        root=str(skill_dir.parent),
    )


def _valid_name(name: str, source: str) -> bool:
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


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    """Minimal YAML-subset frontmatter parser — a 1:1 port of the TS loader's
    (flat keys only: scalars, quoted strings, inline ``[a, b]`` arrays,
    booleans, numbers; comments and blank lines skipped)."""
    out: dict[str, Any] = {}
    for raw_line in raw.split("\n"):
        trimmed = raw_line.rstrip("\r").strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        colon = trimmed.find(":")
        if colon == -1:
            continue
        key = trimmed[:colon].strip()
        value = trimmed[colon + 1 :].strip()
        if not value:
            continue
        out[key] = _parse_value(value)
    return out


def _parse_value(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [s for s in (_unquote(p.strip()) for p in inner.split(",")) if s]
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return _unquote(value)
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"-?\d+(\.\d+)?", value):
        return float(value) if "." in value else int(value)
    return value


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


# ---------------------------------------------------------------------------
# Consumer: tool descriptor + executor decorator + catalog hook
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillConfig:
    """Tunables for the skill tool exposed by ``SkillExecutor``."""

    tool_name: str = "skill"
    description: str = (
        "Load a skill's full instructions by name. Call this when the task "
        "matches a skill from the available-skills list in the system prompt, "
        "then follow the loaded instructions."
    )


def skill_tool_descriptor(config: SkillConfig | None = None) -> dict[str, Any]:
    """OpenAI tool schema to append to the loop's tools list."""
    config = config or SkillConfig()
    return {
        "type": "function",
        "function": {
            "name": config.tool_name,
            "description": config.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Skill name exactly as listed in the available-skills "
                            "catalog."
                        ),
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    }


class SkillExecutor:
    """ToolExecutor decorator: ``config.tool_name`` calls load a skill body.

    The body is returned wrapped in ``<skill_content name="...">`` markers
    (dsh's convention) so both the model and trace readers can tell loaded
    instructions apart from tool data.
    """

    def __init__(
        self,
        inner: ToolExecutor,
        provider: SkillProvider,
        config: SkillConfig | None = None,
        *,
        conditions: set[str] | frozenset[str] = frozenset(),
        exclude: Sequence[str] = (),
        ignore_conditions: bool = False,
    ) -> None:
        self._inner = inner
        self._provider = provider
        self._config = config or SkillConfig()
        self._conditions = conditions
        self._exclude = tuple(exclude)
        self._ignore_conditions = ignore_conditions

    async def execute(self, call: ToolCall, ctx: LoopContext) -> ToolResult:
        if call.name != self._config.tool_name:
            return await self._inner.execute(call, ctx)
        name = str((call.arguments or {}).get("name") or "").strip()
        if not name:
            return ToolResult(
                success=False,
                error="missing required argument: name",
                needsFollowup=True,
            )
        skill = self._provider.get(name)
        if skill is None:
            available = [
                s.name
                for s in select_catalog(
                    self._provider.list(),
                    self._conditions,
                    self._exclude,
                    self._ignore_conditions,
                )
            ]
            error = f"Unknown skill: {name}"
            if available:
                error += f". Available skills: {', '.join(available)}"
            return ToolResult(
                success=False,
                error=error,
                needsFollowup=True,
                data={"unknownSkill": name},
            )
        excluded = {e.lower().strip() for e in self._exclude}
        if (
            skill.name.lower() in excluded
            or skill.dir_name.lower() in excluded
            or (skill.display_name != "" and skill.display_name.lower() in excluded)
        ):
            return ToolResult(
                success=False,
                error=f"Skill '{skill.name}' is not available in this mode.",
                needsFollowup=True,
                data={"skill": skill.name, "excluded": True},
            )
        if not skill.model_invocable:
            return ToolResult(
                success=False,
                error=(
                    f"Skill '{skill.name}' is user-invocable only "
                    "(disable-model-invocation); do not load it yourself."
                ),
                needsFollowup=True,
                data={"skill": skill.name, "modelInvocable": False},
            )
        return ToolResult(
            success=True,
            message=(
                f'<skill_content name="{skill.name}">\n{skill.content}\n</skill_content>'
            ),
            data={"skill": skill.name},
        )

    def concurrency_safe(self, call: ToolCall) -> bool:
        # Loading a skill is a pure read — safe to batch with siblings;
        # other calls defer to the inner executor's own judgement.
        if call.name == self._config.tool_name:
            return True
        inner_safe = getattr(self._inner, "concurrency_safe", None)
        return bool(inner_safe and inner_safe(call))


class SkillHooks(NoopHooks):
    """LoopHooks: inject the skill catalog into the system message, once.

    The injection is a first-round ``pre_step`` transcript rewrite, so the
    loop records it as a ``hook_action`` event (``action: skill_catalog``)
    and the catalog is reconstructable from the trace. Compaction preserves
    system messages, so the catalog survives later rewrites.
    """

    def __init__(
        self,
        provider: SkillProvider,
        *,
        conditions: set[str] | frozenset[str] = frozenset(),
        exclude: Sequence[str] = (),
        config: SkillConfig | None = None,
        ignore_conditions: bool = False,
    ) -> None:
        self._provider = provider
        self._conditions = conditions
        self._exclude = tuple(exclude)
        self._config = config or SkillConfig()
        self._ignore_conditions = ignore_conditions
        self._injected = False

    async def pre_step(
        self, transcript: list[LLMMessage], ctx: LoopContext
    ) -> PreStepAction:
        if self._injected:
            return PreStepAction(kind="proceed")
        self._injected = True
        catalog = select_catalog(
            self._provider.list(), self._conditions, self._exclude, self._ignore_conditions
        )
        if not catalog:
            return PreStepAction(kind="proceed")
        section = render_skill_catalog(catalog, tool_name=self._config.tool_name)
        rewritten = list(transcript)
        if rewritten and rewritten[0].role == "system":
            first = rewritten[0]
            rewritten[0] = LLMMessage(
                role="system",
                content=f"{first.content}\n\n{section}",
                name=first.name,
            )
        else:
            rewritten.insert(0, LLMMessage(role="system", content=section))
        return PreStepAction(
            kind="proceed",
            transcript=rewritten,
            reason=f"skill catalog injected ({len(catalog)} skills)",
            rewrite_action="skill_catalog",
        )
