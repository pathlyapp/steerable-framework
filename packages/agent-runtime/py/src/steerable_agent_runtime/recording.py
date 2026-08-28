"""RecordingProvider — capture every outbound LLM request, plus the
prompt-invariant assertions built on the recording.

Wave 0 tripwire (docs/roadmap.md "Wave 0 — prerequisites"): Wave 1 rewrites
history into typed append-only envelopes, and without a recording of what the
model actually saw, a correct rewrite can silently regress. This module closes
that gap:

- ``RecordingProvider`` wraps any ``LLMProvider`` and snapshots every outbound
  request (messages + params) into a sink *before* delegating, so a failed
  request is recorded too. Two sinks ship here: ``InMemoryRequestSink`` for
  tests and ``JsonlRequestSink`` (one JSON object per line, the house record
  format) for the E2E harness and dogfooding.
- ``assert_stable_prefix`` is the executable form of "no history rewrite":
  request *n*'s messages must be a prefix of request *n+1*'s, except at
  declared compaction boundaries. It WILL fail on today's compaction/retry
  rewrite paths — that is the point. Declare the boundary, and Wave 1 flips
  the default to append-only.
- ``assert_bounded_items`` pins the "no unbounded injected items" rule: every
  message in every request must stay under a hard token cap (the same
  heuristic estimator the compaction layer uses).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from .llm import LLMMessage, LLMProvider, LLMStreamChunk, LLMUsage, TextPart
from .tokens import IMAGE_PART_TOKEN_ESTIMATE, estimate_text_tokens

#: Default per-item hard cap for ``assert_bounded_items`` — mirrors codex's
#: "no items larger than 10K tokens" context rule.
DEFAULT_MAX_ITEM_TOKENS = 10_000


@dataclass(slots=True)
class RecordedRequest:
    """One outbound provider request, snapshotted at send time.

    ``messages`` are plain dicts (serialized when recorded — the loop keeps
    mutating its transcript list afterwards, so a shallow reference would
    not be a snapshot). ``params`` carries the non-message arguments:
    ``tools``, ``temperature``, ``max_tokens``, and any provider-specific
    extras (e.g. ``tool_choice``).
    """

    seq: int
    kind: Literal["stream", "complete"]
    provider: str
    model: str
    messages: list[dict[str, Any]]
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "messages": self.messages,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecordedRequest:
        return cls(
            seq=int(data["seq"]),
            kind=data["kind"],
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            messages=list(data.get("messages") or []),
            params=dict(data.get("params") or {}),
        )


@runtime_checkable
class RequestSink(Protocol):
    """Destination for recorded requests."""

    def record(self, request: RecordedRequest) -> None: ...


class InMemoryRequestSink:
    """Test sink: recorded requests accumulate in ``.requests``."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []

    def record(self, request: RecordedRequest) -> None:
        self.requests.append(request)


class JsonlRequestSink:
    """Durable sink: append each request as one JSON line at ``path``.

    Flushed per record so an E2E harness can tail the file while the run is
    still in flight. The parent directory must exist — sink setup errors
    should surface at construction, not mid-turn.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        # Long-lived handle flushed per record so a harness can tail the file
        # mid-run; closed via close() when the run ends.
        self._fh = open(path, "a", encoding="utf-8")  # noqa: SIM115

    def record(self, request: RecordedRequest) -> None:
        self._fh.write(json.dumps(request.to_dict(), ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def load_recorded_requests(path: str) -> list[RecordedRequest]:
    """Read a JSONL recording back into ``RecordedRequest``s (E2E harness)."""

    out: list[RecordedRequest] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(RecordedRequest.from_dict(json.loads(line)))
    return out


def _content_for_record(message: LLMMessage) -> str | list[dict[str, Any]]:
    """Serialize content parts for the record.

    Text-only content records as a plain string so existing recordings and
    their consumers (E2E harness comparisons, ``assert_stable_prefix``) keep
    byte-identical output; non-text parts record as typed dicts.
    """
    if all(isinstance(part, TextPart) for part in message.content):
        return message.content_text
    return [asdict(part) for part in message.content]


def _message_to_dict(message: LLMMessage) -> dict[str, Any]:
    out: dict[str, Any] = {
        "role": message.role,
        "content": _content_for_record(message),
    }
    if message.name is not None:
        out["name"] = message.name
    if message.tool_call_id is not None:
        out["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        out["tool_calls"] = [
            call.model_dump() if hasattr(call, "model_dump") else dict(call)  # type: ignore[arg-type]
            for call in message.tool_calls
        ]
    return out


@dataclass
class RecordingProvider:
    """LLMProvider wrapper that records every outbound request to a sink.

    Purely additive like ``CalibratingProvider``: calls delegate to the inner
    provider and all chunks/returns pass through unchanged. The request is
    recorded *before* delegation so a provider error does not lose it.
    """

    inner: LLMProvider
    sink: RequestSink

    def __post_init__(self) -> None:
        self._seq = 0

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def model(self) -> str:
        return self.inner.model

    def __getattr__(self, attr: str) -> Any:
        # Transparent wrapper (same contract as CalibratingProvider): host
        # code reaching for inner attributes should not notice the wrap.
        return getattr(self.inner, attr)

    def _record(
        self,
        kind: Literal["stream", "complete"],
        messages: Sequence[LLMMessage],
        params: dict[str, Any],
    ) -> None:
        self._seq += 1
        self.sink.record(
            RecordedRequest(
                seq=self._seq,
                kind=kind,
                provider=self.inner.name,
                model=self.inner.model,
                messages=[_message_to_dict(m) for m in messages],
                params=params,
            )
        )

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[LLMMessage, LLMUsage]:
        params = _params_dict(tools, temperature, max_tokens, kwargs)
        self._record("complete", messages, params)
        return await self.inner.complete(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs
        )

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        params = _params_dict(tools, temperature, max_tokens, kwargs)
        self._record("stream", messages, params)
        async for chunk in self.inner.stream(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs
        ):
            yield chunk


def _params_dict(
    tools: Iterable[dict[str, Any]] | None,
    temperature: float | None,
    max_tokens: int | None,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if tools is not None:
        params["tools"] = list(tools)
    if temperature is not None:
        params["temperature"] = temperature
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    params.update(kwargs)
    return params


# ---------------------------------------------------------------------------
# Prompt-invariant assertions
# ---------------------------------------------------------------------------


def assert_stable_prefix(
    requests: Sequence[RecordedRequest],
    *,
    compaction_boundaries: Collection[int] = (),
) -> None:
    """Assert the recorded transcript only ever grew by appending.

    For every consecutive pair of requests, the earlier request's messages
    must be a prefix of the later one's — the executable form of "no history
    rewrite". A request whose index (0-based position in ``requests``) is in
    ``compaction_boundaries`` is exempt from the prefix check against its
    predecessor: that is where a declared rewrite (compaction, overflow
    recovery) happened. The exemption is per-boundary, so a rewrite anywhere
    else still fails.

    Raises ``AssertionError`` naming the request index, the first diverging
    message position, and both sides of the divergence.
    """

    boundaries = set(compaction_boundaries)
    for i in range(1, len(requests)):
        if i in boundaries:
            continue
        prev = requests[i - 1].messages
        cur = requests[i].messages
        if len(cur) < len(prev):
            raise AssertionError(
                f"history rewrite at request #{i} (seq {requests[i].seq}): "
                f"shrank from {len(prev)} to {len(cur)} messages; if this is a "
                f"declared compaction, add {i} to compaction_boundaries"
            )
        for pos, (before, after) in enumerate(zip(prev, cur)):
            if before != after:
                raise AssertionError(
                    f"history rewrite at request #{i} (seq {requests[i].seq}): "
                    f"message {pos} changed\n"
                    f"  was:  {_preview(before)}\n"
                    f"  now:  {_preview(after)}\n"
                    f"if this is a declared compaction, add {i} to "
                    f"compaction_boundaries"
                )


def assert_bounded_items(
    requests: Sequence[RecordedRequest],
    *,
    max_item_tokens: int = DEFAULT_MAX_ITEM_TOKENS,
) -> None:
    """Assert every message in every request stays under a hard size cap.

    "Item" = one message; its size is the heuristic token estimate of its
    semantic payload (content + serialized tool calls + name/tool_call_id),
    using the same estimator as the compaction layer. The cap is a tripwire
    for unbounded injection (skill catalogs, tool results, hook inserts) —
    not a budget.
    """

    for req in requests:
        for pos, message in enumerate(req.messages):
            size = _item_tokens(message)
            if size > max_item_tokens:
                raise AssertionError(
                    f"unbounded item at request #{req.seq - 1} (seq {req.seq}), "
                    f"message {pos} (role={message.get('role')}): "
                    f"~{size} tokens > cap {max_item_tokens}"
                )


def _item_tokens(message: dict[str, Any]) -> int:
    content = message.get("content")
    if isinstance(content, list):  # structured parts: text runs + per-image flat
        total = sum(
            estimate_text_tokens(str(part.get("text") or ""))
            if part.get("type") == "text"
            else IMAGE_PART_TOKEN_ESTIMATE
            for part in content
            if isinstance(part, dict)
        )
    else:
        total = estimate_text_tokens(str(content or ""))
    if message.get("tool_calls"):
        total += estimate_text_tokens(
            json.dumps(message["tool_calls"], ensure_ascii=False, default=str)
        )
    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if value:
            total += estimate_text_tokens(str(value))
    return total


def _preview(message: dict[str, Any], *, limit: int = 160) -> str:
    text = json.dumps(message, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"
