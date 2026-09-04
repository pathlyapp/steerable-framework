"""Declarative harness assembly (W1.2): a harness is a declaration, not a
factory function.

A ``HarnessSpec`` names one or more implementations per strategy dimension
(``harness.STRATEGY_REGISTRY``); ``assemble_harness`` instantiates them into
the loop's existing seams — a ``ChainHooks`` over the hooks-producing
dimensions, one storage adapter, one tool selector, one executor wrapper.
Nothing here edits ``CoreLoop``.

Load-time discipline (W1.2.3): unknown dimensions, unknown implementation
names, and unknown parameter names all fail at load — a misspelled strategy
must never silently become a null one.

File format (YAML or JSON, by extension)::

    context:
      - impl: pressure_compaction
        params: {threshold_ratio: 0.8}
      - impl: spill
    retry: [informed_backtrack, simple]     # bare name = no params
    validator: self_critique
    tools: minimal
    memory: stateless
    orchestration: single

Hooks-producing dimensions (context/retry/validator) accept a list — the
current default composes e.g. overflow backtrack with backoff retry, and
ordering is significant (first retry decision wins). tools/memory/
orchestration are singular: one filter, one store, one wrapper.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .harness import STRATEGY_REGISTRY, RouterToolSelection
from .hooks import ChainHooks, LoopHooks
from .llm import LLMProvider
from .storage import StorageAdapter
from .tools import ToolRouter

#: Dimensions whose strategies contribute LoopHooks and therefore compose.
_HOOKS_DIMENSIONS = ("context", "retry", "validator")
#: Dimensions with a single choice.
_SINGULAR_DIMENSIONS = ("tools", "memory", "orchestration")

_DIMENSIONS = (*_HOOKS_DIMENSIONS, *_SINGULAR_DIMENSIONS)


class HarnessSpecError(ValueError):
    """A spec that names something unknown — raised at load, never mid-run."""


@dataclass(frozen=True, slots=True)
class ModuleChoice:
    """One named implementation plus its parameters."""

    impl: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoopLimits:
    """Optional ``loop:`` section — the LoopConfig knobs a harness pins.

    Absent fields mean "the entrypoint's default": the spec pins what the
    experiment varies, nothing more.
    """

    max_rounds: int | None = None
    max_tool_errors: int | None = None
    tool_dedup: bool | None = None


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    """The six-dimension harness declaration."""

    context: tuple[ModuleChoice, ...]
    retry: tuple[ModuleChoice, ...]
    validator: tuple[ModuleChoice, ...]
    tools: ModuleChoice
    memory: ModuleChoice
    orchestration: ModuleChoice
    loop: LoopLimits = LoopLimits()

    def describe(self) -> dict[str, Any]:
        """The ``harness.describe`` payload (W1.2.2): every dimension's
        chosen implementation(s) with their assumption contracts."""

        def _entry(choice: ModuleChoice, dimension: str) -> dict[str, Any]:
            cls = STRATEGY_REGISTRY[dimension][choice.impl]
            # slots=True dataclasses expose the field default via
            # __dataclass_fields__, not as a plain class attribute.
            default = getattr(cls, "__dataclass_fields__", {}).get("assumes")
            assumes = default.default if default is not None else ""
            return {
                "impl": choice.impl,
                "params": choice.params,
                "assumes": assumes if isinstance(assumes, str) else "",
            }

        return {
            dimension: [_entry(c, dimension) for c in getattr(self, dimension)]
            for dimension in _HOOKS_DIMENSIONS
        } | {
            dimension: _entry(getattr(self, dimension), dimension)
            for dimension in _SINGULAR_DIMENSIONS
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def describe_harness_registry() -> dict[str, Any]:
    """Every dimension's available implementations with their assumption
    contracts — the ``harness.describe`` vocabulary half (W1.2.2). Hosts
    render harness pickers from this payload; a new strategy needs no
    host-side constant to appear."""

    def _assumes(cls: type) -> str:
        default = getattr(cls, "__dataclass_fields__", {}).get("assumes")
        value = default.default if default is not None else ""
        return value if isinstance(value, str) else ""

    return {
        dimension: [
            {"impl": name, "assumes": _assumes(cls)} for name, cls in impls.items()
        ]
        for dimension, impls in STRATEGY_REGISTRY.items()
    }


def load_harness_spec(path: str | Path) -> HarnessSpec:
    """Load a spec from YAML or JSON (by extension; JSON is a YAML subset).

    YAML needs PyYAML, which is not a runtime dependency — a clear error
    beats a silent fallback when it is missing.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover — environment-dependent
            raise HarnessSpecError(
                f"{path}: YAML specs need PyYAML (not a runtime dependency); "
                "use JSON or install pyyaml"
            ) from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise HarnessSpecError(f"{path}: top level must be a mapping of dimensions")
    try:
        return harness_spec_from_dict(data)
    except HarnessSpecError as exc:
        raise HarnessSpecError(f"{path}: {exc}") from exc


def harness_spec_from_dict(data: dict[str, Any]) -> HarnessSpec:
    """Validate and normalize a raw mapping into a ``HarnessSpec``."""
    unknown = set(data) - set(_DIMENSIONS) - {"loop"}
    if unknown:
        raise HarnessSpecError(
            f"unknown dimensions: {sorted(unknown)}; expected subsets of {list(_DIMENSIONS)}"
        )
    missing = [d for d in _DIMENSIONS if d not in data]
    if missing:
        raise HarnessSpecError(
            f"missing dimensions: {missing} — a harness declares all six "
            "(see default.harness.yaml for the template)"
        )
    loop = _loop_limits(data.get("loop"))

    def _choices(dimension: str) -> tuple[ModuleChoice, ...]:
        raw = data[dimension]
        entries = raw if isinstance(raw, list) else [raw]
        choices = tuple(_choice(dimension, entry) for entry in entries)
        if dimension in _SINGULAR_DIMENSIONS and len(choices) != 1:
            raise HarnessSpecError(
                f"dimension {dimension!r} takes exactly one implementation, "
                f"got {[c.impl for c in choices]}"
            )
        if not choices:
            raise HarnessSpecError(f"dimension {dimension!r} has no implementation")
        return choices

    parsed = {dimension: _choices(dimension) for dimension in _DIMENSIONS}
    return HarnessSpec(
        context=parsed["context"],
        retry=parsed["retry"],
        validator=parsed["validator"],
        tools=parsed["tools"][0],
        memory=parsed["memory"][0],
        orchestration=parsed["orchestration"][0],
        loop=loop,
    )


def _loop_limits(raw: Any) -> LoopLimits:
    if raw is None:
        return LoopLimits()
    if not isinstance(raw, dict):
        raise HarnessSpecError(f"'loop' must be a mapping, got {raw!r}")
    known = {"max_rounds", "max_tool_errors", "tool_dedup"}
    unknown = set(raw) - known
    if unknown:
        raise HarnessSpecError(
            f"'loop' has unknown keys {sorted(unknown)} (expected subsets of {sorted(known)})"
        )
    return LoopLimits(
        max_rounds=raw.get("max_rounds"),
        max_tool_errors=raw.get("max_tool_errors"),
        tool_dedup=raw.get("tool_dedup"),
    )


def _choice(dimension: str, entry: Any) -> ModuleChoice:
    if isinstance(entry, str):
        entry = {"impl": entry}
    if not isinstance(entry, dict):
        raise HarnessSpecError(
            f"dimension {dimension!r}: entries must be names or mappings, got {entry!r}"
        )
    unknown = set(entry) - {"impl", "params"}
    if unknown:
        raise HarnessSpecError(
            f"dimension {dimension!r}: unknown entry keys {sorted(unknown)} "
            "(expected impl/params)"
        )
    impl = entry.get("impl")
    if not isinstance(impl, str) or not impl:
        raise HarnessSpecError(f"dimension {dimension!r}: entry missing 'impl' name")
    if impl not in STRATEGY_REGISTRY[dimension]:
        known = sorted(STRATEGY_REGISTRY[dimension])
        raise HarnessSpecError(
            f"dimension {dimension!r}: unknown implementation {impl!r}; known: {known}"
        )
    params = entry.get("params") or {}
    if not isinstance(params, dict):
        raise HarnessSpecError(f"dimension {dimension!r}: 'params' must be a mapping")
    return ModuleChoice(impl=impl, params=dict(params))


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AssembledHarness:
    """The instantiated harness: ready-to-wire loop seams plus the spec it
    came from (for attribution — a run record can name its harness)."""

    spec: HarnessSpec
    hooks: LoopHooks
    storage: StorageAdapter
    tool_selection: Any
    orchestration: Any

    def wrap_executor(
        self,
        executor: Any,
        *,
        provider: LLMProvider,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        return self.orchestration.wrap(executor, provider=provider, tools=tools)

    def wire_tools(self, router: ToolRouter) -> None:
        """Bind the run's ``ToolRouter`` to the tools dimension.

        Router-backed strategies (`harness.RouterToolSelection`) register
        discovery tools and read exposure tiers here; pure descriptor
        filters (full/minimal) have nothing to bind, so this is a no-op
        for them. Entrypoints with an in-process router call this once
        before `select_tools`; selecting a router-backed strategy without
        wiring raises rather than offering a tool that cannot dispatch.
        """
        if isinstance(self.tool_selection, RouterToolSelection):
            self.tool_selection.register(router)

    def select_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.tool_selection.select(tools)

    def describe(self) -> dict[str, Any]:
        return self.spec.describe()


def _instantiate(
    dimension: str, choice: ModuleChoice, runtime_params: dict[str, dict[str, Any]]
) -> Any:
    cls = STRATEGY_REGISTRY[dimension][choice.impl]
    # Runtime-resolved values (e.g. the model's context window) fill what the
    # spec leaves out; an explicit spec param always wins (explicit > implicit).
    params = {**runtime_params.get(choice.impl, {}), **choice.params}
    try:
        return cls(**params)
    except TypeError as exc:
        raise HarnessSpecError(
            f"dimension {dimension!r}, implementation {choice.impl!r}: {exc}"
        ) from exc


def assemble_harness(
    spec: HarnessSpec,
    *,
    provider: LLMProvider | None = None,
    runtime_params: dict[str, dict[str, Any]] | None = None,
) -> AssembledHarness:
    """Instantiate a spec into the loop's seams.

    ``provider`` is required by strategies that call the model themselves
    (self_critique's judges, summarizing compaction); they fail loud when it
    is absent. ``runtime_params`` maps implementation name → parameters the
    host resolves at run time (e.g. ``max_context_tokens`` from the model
    catalog); spec-literal params take precedence.
    """
    runtime = runtime_params or {}
    hook_parts: list[LoopHooks] = []
    for dimension in _HOOKS_DIMENSIONS:
        for choice in getattr(spec, dimension):
            strategy = _instantiate(dimension, choice, runtime)
            hook_parts.append(strategy.hooks(provider=provider))
    memory = _instantiate("memory", spec.memory, runtime)
    hook_parts.append(memory.hooks(provider=provider))
    return AssembledHarness(
        spec=spec,
        hooks=ChainHooks(*hook_parts),
        storage=memory.storage(),
        tool_selection=_instantiate("tools", spec.tools, runtime),
        orchestration=_instantiate("orchestration", spec.orchestration, runtime),
    )
