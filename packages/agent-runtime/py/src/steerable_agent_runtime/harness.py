"""Harness strategy modules (W1.1) — named, replaceable policy units.

HarnessLab's six-module division, adapted onto the loop's existing seams:
the loop's extension point stays ``LoopHooks`` / ``ToolExecutor`` /
``StorageAdapter``; these protocols name the *strategy dimension* each
choice occupies, so a harness can be declared as six named choices and
ablated one dimension at a time (W1.2 assembles them declaratively).

Every implementation carries an ``assumes`` contract: what it takes for
granted about the model, the task, or the environment. Attribution reports
quote these when interpreting deltas — a dimension that shows no effect
under a wrong assumption says nothing about the dimension.

Naming: the plan's dimension names (``ContextManager``, ``RetryPolicy``)
collide with existing runtime types, so protocols take the ``*Strategy``
suffix; registry keys keep the plan's dimension names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .compaction import CompactionHooks
from .history import ContextFragment
from .hooks import LoopHooks, NoopHooks
from .llm import LLMProvider
from .retry import RetryHooks
from .storage import InMemoryStorage, StorageAdapter
from .tool_search import DEFAULT_MAX_RESULTS
from .tools import ToolRouter

# ---------------------------------------------------------------------------
# Protocols — one per harness dimension
# ---------------------------------------------------------------------------


@runtime_checkable
class ContextStrategy(Protocol):
    """Dimension ``context``: how the loop manages context pressure."""

    name: str
    assumes: str

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks: ...


@runtime_checkable
class RetryStrategy(Protocol):
    """Dimension ``retry``: what happens when an LLM request fails."""

    name: str
    assumes: str

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks: ...


@runtime_checkable
class ValidationStrategy(Protocol):
    """Dimension ``validator``: how completions are checked before emit."""

    name: str
    assumes: str

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks: ...


@runtime_checkable
class ToolSelection(Protocol):
    """Dimension ``tools``: which of the registered tools the model sees."""

    name: str
    assumes: str

    def select(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]: ...


@runtime_checkable
class RouterToolSelection(Protocol):
    """Optional ``tools``-dimension seam: selection that needs the run's
    ``ToolRouter`` — to register discovery tools and to read exposure
    tiers.

    ``AssembledHarness.wire_tools`` calls ``register`` before ``select``
    runs. A strategy implementing this protocol must treat unwired
    ``select`` as an error: the model is never offered a tool that cannot
    dispatch.
    """

    def register(self, router: ToolRouter) -> None: ...


@runtime_checkable
class MemoryStrategy(Protocol):
    """Dimension ``memory``: the run's record store plus any persistent
    notes the strategy injects (``hooks``; ``NoopHooks`` when stateless)."""

    name: str
    assumes: str

    def storage(self) -> StorageAdapter: ...

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks: ...


@runtime_checkable
class OrchestrationStrategy(Protocol):
    """Dimension ``orchestration``: whether the loop can delegate to child
    loops, expressed as a ``ToolExecutor`` wrapper."""

    name: str
    assumes: str

    def wrap(
        self,
        executor: Any,
        *,
        provider: LLMProvider,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Hook projections — adapt a multi-slice LoopHooks to the one slice a
# dimension owns, so assembly never double-applies a cross-cutting impl.
# ---------------------------------------------------------------------------


class _PreStepOnly(NoopHooks):
    def __init__(self, inner: LoopHooks) -> None:
        self._inner = inner

    async def pre_step(self, transcript: Any, ctx: Any) -> Any:
        return await self._inner.pre_step(transcript, ctx)


class _OnRequestErrorOnly(NoopHooks):
    def __init__(self, inner: LoopHooks) -> None:
        self._inner = inner

    async def on_request_error(self, error: Exception, transcript: Any, ctx: Any) -> Any:
        return await self._inner.on_request_error(error, transcript, ctx)


class _BeforeCompletionOnly(NoopHooks):
    def __init__(self, inner: LoopHooks) -> None:
        self._inner = inner

    async def before_completion(self, draft: Any, ctx: Any) -> Any:
        return await self._inner.before_completion(draft, ctx)


class _PostToolResultOnly(NoopHooks):
    def __init__(self, inner: LoopHooks) -> None:
        self._inner = inner

    async def post_tool_result(self, result: Any, call: Any, ctx: Any) -> Any:
        return await self._inner.post_tool_result(result, call, ctx)


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NullContext:
    """Baseline: no context management at all."""

    name: str = "null"
    assumes: str = (
        "the trajectory fits the model window unaided; overflow fails the "
        "turn instead of being recovered"
    )

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks:
        return NoopHooks()


@dataclass(frozen=True, slots=True)
class PressureCompaction:
    """The existing pressure/hysteresis compaction, owning the pre_step slice."""

    max_context_tokens: int
    threshold_ratio: float = 0.8
    keep_last_messages: int = 6
    keep_last_tool_results: int = 2
    fold_excerpt_chars: int | None = None
    model: str | None = None
    name: str = "pressure_compaction"
    assumes: str = (
        "long trajectories exceed the window; older detail is expendable "
        "before recent state, and a summarizer (or folding) preserves enough "
        "for the task to continue"
    )

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks:
        extra = (
            {"fold_excerpt_chars": self.fold_excerpt_chars}
            if self.fold_excerpt_chars is not None
            else {}
        )
        return _PreStepOnly(
            CompactionHooks(
                max_context_tokens=self.max_context_tokens,
                threshold_ratio=self.threshold_ratio,
                keep_last_messages=self.keep_last_messages,
                keep_last_tool_results=self.keep_last_tool_results,
                summarizer=provider,
                model=self.model,
                **extra,
            )
        )


@dataclass(frozen=True, slots=True)
class ObservationAging:
    """Age-graded degradation of tool results (W2.1), owning pre_step and
    post_tool_result slices of one hooks object."""

    fresh_rounds: int = 3
    keep_tokens: int = 200
    fold_after_rounds: int = 8
    compress_tokens: int = 1000
    name: str = "observation_aging"
    assumes: str = (
        "stale observations lose value faster than they lose size; the "
        "durable record keeps originals so folding only shrinks the "
        "projection, never the record"
    )

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks:
        from .observation_aging import AgingRules, ObservationAgingHooks

        return ObservationAgingHooks(
            AgingRules(
                fresh_rounds=self.fresh_rounds,
                keep_tokens=self.keep_tokens,
                fold_after_rounds=self.fold_after_rounds,
                compress_tokens=self.compress_tokens,
            )
        )


@dataclass(frozen=True, slots=True)
class SpillExternalization:
    """The existing large-result spill (SpillHooks), owning the
    post_tool_result slice — a context-pressure mechanism: oversized payloads
    leave the transcript as a preview + locator."""

    directory: str | None = None
    max_inline_bytes: int = 16_000
    preview_bytes: int = 2_000
    name: str = "spill"
    assumes: str = (
        "oversized tool results are re-readable from disk later; a preview "
        "plus locator loses nothing the model needed inline"
    )

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks:
        import tempfile

        from .spill import FilesystemSpillStore, SpillHooks

        store = FilesystemSpillStore(self.directory or tempfile.mkdtemp(prefix="steerable-spill-"))
        return _PostToolResultOnly(
            SpillHooks(
                store,
                max_inline_bytes=self.max_inline_bytes,
                preview_bytes=self.preview_bytes,
            )
        )


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NoRetry:
    """Baseline: every request error is terminal."""

    name: str = "none"
    assumes: str = (
        "provider errors are terminal; retries would mask the measurement "
        "(or are unwanted in audited runs)"
    )

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks:
        return NoopHooks()


@dataclass(frozen=True, slots=True)
class SimpleRetry:
    """The existing bounded exponential backoff (RetryHooks)."""

    max_attempts: int = 4
    base_delay_ms: int = 200
    max_delay_ms: int = 5000
    name: str = "simple"
    assumes: str = (
        "failures are dominated by transient transport/rate-limit/server "
        "errors; context overflow is NOT retried here — that belongs to "
        "informed_backtrack"
    )

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks:
        from steerable_agent_harness import RetryPolicy

        return _OnRequestErrorOnly(
            RetryHooks(
                RetryPolicy(
                    max_attempts=self.max_attempts,
                    base_delay_ms=self.base_delay_ms,
                    max_delay_ms=self.max_delay_ms,
                )
            )
        )


@dataclass(frozen=True, slots=True)
class InformedBacktrack:
    """Overflow recovery: compact the transcript, then retry the request.

    Owns the ``on_request_error`` slice of ``CompactionHooks``; pairs with
    ``PressureCompaction`` in the default harness (two projections over the
    same class — independent instances, so ablating one never disturbs the
    other).
    """

    max_context_tokens: int
    model: str | None = None
    name: str = "informed_backtrack"
    assumes: str = (
        "context-overflow errors are recoverable by rewriting the transcript "
        "shorter; a bounded number of times per round"
    )

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks:
        return _OnRequestErrorOnly(
            CompactionHooks(
                max_context_tokens=self.max_context_tokens,
                summarizer=provider,
                model=self.model,
            )
        )


# ---------------------------------------------------------------------------
# validator
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NullValidator:
    """Baseline: completions emit as drafted."""

    name: str = "null"
    assumes: str = (
        "the model's self-report is truthful; used to measure the "
        "validator's marginal contribution"
    )

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks:
        return NoopHooks()


@dataclass(frozen=True, slots=True)
class SelfCritique:
    """The existing discipline-retry + grounding stack (AntiHallucinationHooks)."""

    name: str = "self_critique"
    assumes: str = (
        "the model sometimes claims executions it did not perform; a "
        "discipline retry or grounding judge catches a useful fraction"
    )

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks:
        if provider is None:
            raise ValueError("SelfCritique requires the turn's LLM provider")
        from .antihallucination import AntiHallucinationHooks

        return _BeforeCompletionOnly(AntiHallucinationHooks(provider))


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def _tool_name(descriptor: dict[str, Any]) -> str | None:
    """Name from an OpenAI-nested (``function.name``) or flat descriptor."""
    function = descriptor.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    name = descriptor.get("name")
    return name if isinstance(name, str) else None


@dataclass(frozen=True, slots=True)
class FullToolset:
    """Baseline: the model sees every registered tool."""

    name: str = "full"
    assumes: str = (
        "the model handles the full tool surface; selection noise is zero "
        "and no capability is hidden"
    )

    def select(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return list(tools)


@dataclass(frozen=True, slots=True)
class MinimalToolset:
    """Allowlist filter — the eval baseline's four workspace tools."""

    allowed: frozenset[str] = frozenset({"bash", "read_file", "write_file", "edit_file"})
    name: str = "minimal"
    assumes: str = (
        "four generic tools suffice to express every task action; used to "
        "measure the tool surface's marginal contribution"
    )

    def select(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [t for t in tools if _tool_name(t) in self.allowed]


@dataclass(frozen=True, slots=True)
class ProgressiveDisclosure:
    """The direct tier plus the tool-search descriptor; the model discovers
    the deferred tier on demand.

    The registry's exposure tiers are the listing policy: ``select`` keeps
    descriptors the bound router marks ``direct`` (names the router does
    not know pass through — tiers govern only the router's own
    registrations), drops any stale search descriptor, and appends one
    canonical ``tool_search`` descriptor. The strategy therefore needs the
    run's router: the entrypoint calls ``AssembledHarness.wire_tools``
    before selection, which registers the discovery tool — dispatchable by
    the time its descriptor is offered — and binds the tier source.
    ``select`` before ``register`` raises rather than offer a dead tool.
    """

    max_results: int = DEFAULT_MAX_RESULTS
    name: str = "progressive"
    assumes: str = (
        "the model can express its need as a search query, and discovery "
        "latency costs less than the tokens a full tool listing would burn"
    )
    # Bound by register(), never by construction: assemble_harness builds
    # strategies from spec params alone, so the router arrives via the
    # entrypoint-driven wiring step like every other runtime dependency.
    _router: ToolRouter | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # slots=True never materializes an init=False field's default (the
        # slot descriptor shadows it), so the unwired sentinel is set here.
        object.__setattr__(self, "_router", None)

    def register(self, router: ToolRouter) -> None:
        from .tool_search import register_tool_search

        register_tool_search(router, max_results=self.max_results)
        object.__setattr__(self, "_router", router)

    def select(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from .tool_search import TOOL_SEARCH_NAME, tool_search_descriptor

        if self._router is None:
            raise ValueError(
                "tools: progressive requires the run's ToolRouter — the "
                "entrypoint must call AssembledHarness.wire_tools(router) "
                "before select_tools. Paths whose tools arrive over the "
                "wire without an in-process router (the sidecar host-tools "
                "chat path) cannot serve it; use tools: full or minimal."
            )
        unlisted = {
            registered.name
            for registered in self._router.list_tools()
            if registered.exposure != "direct"
        }
        kept = [
            descriptor
            for descriptor in tools
            if (name := _tool_name(descriptor)) not in unlisted
            and name != TOOL_SEARCH_NAME
        ]
        return [*kept, tool_search_descriptor(max_results=self.max_results)]


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stateless:
    """Baseline: an in-memory record per run, nothing persists."""

    name: str = "stateless"
    assumes: str = (
        "nothing is worth keeping across runs; the record serves this run's "
        "replay only"
    )

    def storage(self) -> StorageAdapter:
        return InMemoryStorage()

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks:
        return NoopHooks()


@dataclass(frozen=True, slots=True)
class FilesystemState:
    """AGENTS.md-style persistent notes: the notes file is injected at turn
    start; the model maintains it with the ordinary file tools."""

    notes_path: Path | str
    max_chars: int = 8_000
    name: str = "filesystem"
    assumes: str = (
        "tasks benefit from cross-session working notes, and the model can "
        "be trusted to maintain its own notes file with file tools"
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "notes_path", Path(self.notes_path))

    def storage(self) -> StorageAdapter:
        return InMemoryStorage()

    def hooks(self, *, provider: LLMProvider | None = None) -> LoopHooks:
        return _NotesInjection(self.notes_path, self.max_chars)


class AgentNotesFragment(ContextFragment):
    """The AGENTS.md-style notes file, injected once at turn start.

    Typed so the injection passes through the fragment token gate like
    every other context surface (hooks.py: raw messages append unbounded).
    """

    content_kind = "memory.notes"
    max_tokens = 2_000
    review_note = (
        "cross-session working notes are the memory payload itself, not a "
        "notice: an 8 KB notes file needs ~2k tokens, and degrading below "
        "that would silently drop the oldest remembered facts"
    )

    def __init__(self, notes_path: Path, notes: str) -> None:
        self._path = notes_path
        self._notes = notes

    def body(self) -> str:
        return f'<agent-notes path="{self._path}">\n{self._notes}\n</agent-notes>'

    @classmethod
    def type_markers(cls) -> tuple[str, str]:
        return ('<agent-notes path="', "</agent-notes>")


class _NotesInjection(NoopHooks):
    """pre_step slice: append the notes file once per turn (round 0)."""

    def __init__(self, notes_path: Path, max_chars: int) -> None:
        self._path = notes_path
        self._max_chars = max_chars

    async def pre_step(self, transcript: Any, ctx: Any) -> Any:
        from .hooks import PreStepAction, TranscriptAppend

        if getattr(ctx, "round_index", 0) != 0:
            return PreStepAction(kind="proceed")
        try:
            notes = self._path.read_text(encoding="utf-8")
        except OSError:
            return PreStepAction(kind="proceed")
        if not notes.strip():
            return PreStepAction(kind="proceed")
        fragment = AgentNotesFragment(self._path, notes[: self._max_chars])
        return PreStepAction(
            kind="proceed",
            appends=[
                TranscriptAppend(
                    message=fragment.to_message(),
                    kind=fragment.content_kind,
                    fragment=fragment,
                )
            ],
            append_action="memory_notes",
        )


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SingleAgent:
    """Baseline: no delegation; the executor passes through unchanged."""

    name: str = "single"
    assumes: str = (
        "one agent suffices; used to measure orchestration's marginal "
        "contribution"
    )

    def wrap(
        self,
        executor: Any,
        *,
        provider: LLMProvider,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        return executor


@dataclass(frozen=True, slots=True)
class SubAgentDelegation:
    """The existing AgentPool six-tool delegation (OrchestrationExecutor)."""

    name: str = "subagent"
    assumes: str = (
        "exploration subtasks burn context that isolation can keep out of "
        "the parent; children return conclusions, not transcripts"
    )

    def wrap(
        self,
        executor: Any,
        *,
        provider: LLMProvider,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        from .orchestration import OrchestrationExecutor

        return OrchestrationExecutor(executor, provider, tools=tools)


# ---------------------------------------------------------------------------
# Registry — the W1.2 loader's lookup table. Unknown names fail loud there.
# ---------------------------------------------------------------------------

#: dimension name -> implementation name -> class. Keys are the plan's
#: dimension names; implementations are classes (the spec instantiates them
#: with its parameters).
STRATEGY_REGISTRY: dict[str, dict[str, type]] = {
    "context": {
        "null": NullContext,
        "pressure_compaction": PressureCompaction,
        "observation_aging": ObservationAging,
        "spill": SpillExternalization,
    },
    "retry": {
        "none": NoRetry,
        "simple": SimpleRetry,
        "informed_backtrack": InformedBacktrack,
    },
    "validator": {"null": NullValidator, "self_critique": SelfCritique},
    "tools": {
        "full": FullToolset,
        "minimal": MinimalToolset,
        "progressive": ProgressiveDisclosure,
    },
    "memory": {"stateless": Stateless, "filesystem": FilesystemState},
    "orchestration": {"single": SingleAgent, "subagent": SubAgentDelegation},
}
