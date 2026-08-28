"""Usage calibration — per-request estimated-vs-observed token recording.

The production aggregate regression (see CORELOOP_TODO P0) can only resolve
one global factor per model family: day-level cjk/other character sums are
collinear (r=0.88), so per-char coefficients are not identifiable from
aggregates. Per-request pairs are. This module closes that loop:

- ``CalibratingProvider`` wraps any ``LLMProvider``: it estimates each
  outgoing request with the *base* heuristic (no model factor — measuring
  against the already-corrected estimate would make the ratio converge to
  1.0 regardless of the true error), observes the usage the provider
  reports, and accumulates rolling per-model sums.
- ``UsageCalibration`` keeps those sums, derives ``observed / estimated``
  factors once enough samples exist, and (optionally) registers them into
  ``MODEL_TOKEN_FACTORS`` so compaction thresholds track the real
  tokenizer. Exact model names win over family prefixes via longest-prefix
  matching, so a measured ``qwen3-32b`` factor overrides the production
  ``deepseek`` family default without touching it.
- Aggregates persist to a small JSON file so a host process (the sidecar)
  accumulates samples across restarts.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .llm import LLMMessage, LLMProvider, LLMStreamChunk, LLMUsage
from .tokens import estimate_text_tokens, estimate_tokens, register_model_factor

#: Requests needed before a derived factor is trusted and auto-registered.
DEFAULT_MIN_SAMPLES = 20

#: Per-request observed/estimated ratios outside this band are provider or
#: accounting glitches (usage missing on one side, cached-token accounting
#: surprises); clip rather than let one request poison the rolling sums.
_RATIO_CLIP_LO = 0.1
_RATIO_CLIP_HI = 10.0


@dataclass(slots=True)
class ModelCalibration:
    """Rolling sums for one model. Factor = obs_prompt / est_prompt (ratio
    of sums — robust to many small requests, and completion-side sums are
    kept for future output-token budgeting)."""

    requests: int = 0
    est_prompt: float = 0.0
    obs_prompt: float = 0.0
    est_completion: float = 0.0
    obs_completion: float = 0.0

    @property
    def prompt_factor(self) -> float | None:
        if self.est_prompt <= 0:
            return None
        return self.obs_prompt / self.est_prompt


class UsageCalibration:
    """Per-model rolling aggregates with optional auto-registration."""

    def __init__(self, *, min_samples: int = DEFAULT_MIN_SAMPLES, auto_register: bool = True):
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        self.min_samples = min_samples
        self.auto_register = auto_register
        self.models: dict[str, ModelCalibration] = {}

    def record(
        self,
        model: str,
        *,
        est_prompt: int,
        obs_prompt: int,
        est_completion: int = 0,
        obs_completion: int = 0,
    ) -> None:
        """Fold one request's estimated/observed pair into the rolling sums.

        Requests where the provider reported no usage (``obs_prompt == 0``)
        or the estimate degenerated are skipped — they carry no signal.
        """
        if not model or est_prompt <= 0 or obs_prompt <= 0:
            return
        ratio = obs_prompt / est_prompt
        if not (_RATIO_CLIP_LO <= ratio <= _RATIO_CLIP_HI):
            return
        entry = self.models.setdefault(model, ModelCalibration())
        entry.requests += 1
        entry.est_prompt += est_prompt
        entry.obs_prompt += obs_prompt
        entry.est_completion += est_completion
        entry.obs_completion += obs_completion
        if self.auto_register and entry.requests >= self.min_samples:
            factor = entry.prompt_factor
            if factor is not None:
                register_model_factor(model, factor)

    def factor(self, model: str) -> float | None:
        """Derived prompt-side factor, or None when samples are insufficient."""
        entry = self.models.get(model)
        if entry is None or entry.requests < self.min_samples:
            return None
        return entry.prompt_factor

    def register_factors(self) -> dict[str, float]:
        """Register every sufficiently-sampled model into MODEL_TOKEN_FACTORS.

        Returns the {model: factor} pairs that were registered. Call after
        ``load()`` so a restarted host resumes with its measured factors.
        """
        registered: dict[str, float] = {}
        for model in self.models:
            factor = self.factor(model)
            if factor is not None:
                register_model_factor(model, factor)
                registered[model] = factor
        return registered

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "models": {name: asdict(entry) for name, entry in self.models.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], **kwargs: Any) -> UsageCalibration:
        cal = cls(**kwargs)
        for name, raw in (data.get("models") or {}).items():
            cal.models[name] = ModelCalibration(
                requests=int(raw.get("requests", 0)),
                est_prompt=float(raw.get("est_prompt", 0.0)),
                obs_prompt=float(raw.get("obs_prompt", 0.0)),
                est_completion=float(raw.get("est_completion", 0.0)),
                obs_completion=float(raw.get("obs_completion", 0.0)),
            )
        return cal

    def save(self, path: str) -> None:
        """Atomic write: tmp file + rename so a crash mid-write can't corrupt."""
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: str, **kwargs: Any) -> UsageCalibration:
        """Load aggregates from ``path``; missing/corrupt file → empty."""
        try:
            with open(path, encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh), **kwargs)
        except (OSError, ValueError):
            return cls(**kwargs)


@dataclass
class CalibratingProvider:
    """LLMProvider wrapper that records estimated-vs-observed usage.

    Purely additive: every call delegates to the inner provider and all
    chunks/returns pass through unchanged. ``persist_every`` controls how
    often (in recorded requests) aggregates are flushed to
    ``persist_path``; 0 disables periodic flushing (call ``flush()``).
    """

    inner: LLMProvider
    calibration: UsageCalibration
    persist_path: str | None = None
    persist_every: int = 10

    def __post_init__(self) -> None:
        self._since_flush = 0

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model(self) -> str:
        return self.inner.model

    def __getattr__(self, attr: str) -> Any:
        # Transparent wrapper: host code (and tests) reaching for inner
        # provider attributes like ``base_url`` should not notice the wrap.
        # Only called when normal lookup fails, so no recursion on ``inner``.
        return getattr(self.inner, attr)

    def _recorded(self, est_prompt: int, usage: LLMUsage, completion_text: str) -> None:
        self.calibration.record(
            self.inner.model,
            est_prompt=est_prompt,
            obs_prompt=usage.prompt_tokens,
            est_completion=estimate_text_tokens(completion_text),
            obs_completion=usage.completion_tokens,
        )
        self._since_flush += 1
        if self.persist_path and self.persist_every > 0 and self._since_flush >= self.persist_every:
            self.flush()

    def flush(self) -> None:
        if self.persist_path:
            self.calibration.save(self.persist_path)
        self._since_flush = 0

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[LLMMessage, LLMUsage]:
        est_prompt = estimate_tokens(messages)
        message, usage = await self.inner.complete(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs
        )
        self._recorded(est_prompt, usage, message.content_text)
        return message, usage

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        est_prompt = estimate_tokens(messages)
        usage: LLMUsage | None = None
        completion_parts: list[str] = []
        async for chunk in self.inner.stream(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs
        ):
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.content_delta:
                completion_parts.append(chunk.content_delta)
            if chunk.reasoning_delta:
                # Reasoning tokens are billed as completion; include them.
                completion_parts.append(chunk.reasoning_delta)
            yield chunk
        if usage is not None:
            self._recorded(est_prompt, usage, "".join(completion_parts))
