"""Provider error taxonomy — structured classification for retry policy.

Codex grades failures with ``is_retryable``; dsh carries structured error
codes with a retryPolicy. The runtime's equivalent: every provider adapter
raises ``LLMError`` with a ``kind``, and ``RetryHooks`` routes on it instead
of treating every failure as "transient".

Kinds and their default retry routing:

- ``transport`` — connection reset / timeout / DNS. Retryable (backoff).
- ``rate_limit`` — HTTP 429. Retryable (backoff; honor ``Retry-After`` when
  the provider sends one).
- ``server`` — HTTP 5xx. Retryable (backoff).
- ``context_overflow`` — prompt exceeds the model's window. NOT retryable
  as-is: the same request must fail again. ``CompactionHooks`` intercepts
  this kind and retries with a compacted transcript.
- ``auth`` — HTTP 401/403. Not retryable; surfaces immediately.
- ``invalid_request`` — other 4xx. Not retryable.
- ``unknown`` — unclassified. Retryable, preserving the pre-taxonomy
  "retry everything" default for adapters that don't classify yet.
"""

from __future__ import annotations

from typing import Any, Literal

LLMErrorKind = Literal[
    "transport",
    "rate_limit",
    "context_overflow",
    "auth",
    "invalid_request",
    "server",
    "unknown",
]

#: Kinds RetryHooks will retry with backoff. context_overflow is deliberately
#: absent — retrying the identical over-long request is a guaranteed re-fail;
#: it needs a transcript rewrite (see CompactionHooks.on_request_error).
RETRYABLE_KINDS: frozenset[LLMErrorKind] = frozenset(
    {"transport", "rate_limit", "server", "unknown"}
)


class LLMError(Exception):
    """Provider failure carrying a classified ``kind``."""

    def __init__(
        self,
        message: str,
        *,
        kind: LLMErrorKind,
        status_code: int | None = None,
        provider: str | None = None,
        raw: Any | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.provider = provider
        self.raw = raw
        self.retry_after_ms = retry_after_ms

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS


#: Body substrings that mark a 400/413 as a context-window overflow rather
#: than a generic invalid request. Covers OpenAI, Ollama, vLLM, DeepSeek,
#: and Anthropic phrasings (matched case-insensitively).
_OVERFLOW_MARKERS = (
    "context length",
    "context window",
    "context_length",
    "context window",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
    "exceeds the context",
    "context size",
    "token limit",
)


def parse_retry_after_ms(value: object, *, cap_ms: int = 180_000) -> int | None:
    """Parse HTTP ``Retry-After`` delta-seconds into a bounded wait.

    HTTP-date values are ignored. Cap so a day-long header cannot stall wrap.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return min(int(seconds * 1000), cap_ms)


def classify_http_status(status_code: int, body: str = "") -> LLMErrorKind:
    """Map an HTTP failure status (+ optional response body) to a kind."""
    lowered = body.lower()
    looks_overflow = any(marker in lowered for marker in _OVERFLOW_MARKERS)
    if status_code in (400, 413) and looks_overflow:
        return "context_overflow"
    if status_code in (401, 403):
        return "auth"
    if status_code == 429:
        return "rate_limit"
    if status_code == 408:
        return "transport"
    if 500 <= status_code < 600:
        return "server"
    if 400 <= status_code < 500:
        return "invalid_request"
    return "unknown"


def classify_error(error: BaseException) -> LLMErrorKind:
    """Best-effort classification of an arbitrary provider exception.

    Adapters raise ``LLMError`` directly; this helper keeps hooks working
    when a raw httpx/asyncio exception slips through.
    """
    if isinstance(error, LLMError):
        return error.kind
    # httpx without importing it unconditionally (adapters import lazily).
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a runtime dep in practice
        httpx = None  # type: ignore[assignment]
    if httpx is not None:
        if isinstance(error, httpx.HTTPStatusError):
            body = ""
            try:
                body = error.response.text or ""
            except Exception:  # noqa: BLE001 - unreadable body
                body = ""
            return classify_http_status(error.response.status_code, body)
        if isinstance(error, httpx.TransportError):
            return "transport"
    # asyncio.TimeoutError / ConnectionError and friends are transport-level.
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "transport"
    return "unknown"


def is_retryable(error: BaseException) -> bool:
    """Default retry predicate: route on the classified kind."""
    return classify_error(error) in RETRYABLE_KINDS
