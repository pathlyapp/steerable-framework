"""Drive the Rust CoreLoop from Python via `steerable_agent_runtime_native`.

Python keeps the provider, executor, and hooks (async, GIL-friendly). Rust
owns think → act → observe. Enabled when ``STEERABLE_RUST_CORELOOP=1``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from steerable_agent_protocol.generated import ToolCall, ToolResult
from steerable_agent_runtime.hooks import CompletionAction, CompletionDraft
from steerable_agent_runtime.history import ContextManager
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.llm.errors import LLMError
from steerable_agent_runtime.llm.parts import ImagePart, TextPart
from steerable_agent_runtime.loop import CoreLoop, LoopContext, LoopEvent, _apply_rewrite
from steerable_agent_runtime.pseudo import (
    PseudoStreamStripper,
    extract_inline_tool_calls,
    split_trailing_high_surrogate,
)
from steerable_agent_runtime.replay import build_step_decision_event

logger = logging.getLogger(__name__)


def native_available() -> bool:
    try:
        import steerable_agent_runtime_native  # noqa: F401
    except ImportError:
        return False
    return True


def _config_json(loop: CoreLoop) -> str:
    config = loop._config
    payload: dict[str, Any] = {
        "max_rounds": config.max_rounds,
        "max_tool_errors": config.max_tool_errors,
        "persist_tool_results": config.persist_tool_results,
        "parallel_tools": config.parallel_tools,
        "tool_dedup": config.tool_dedup,
        "tool_timeout_ms": config.tool_timeout_ms,
        "soft_timeout_ms": config.soft_timeout_ms,
        "wrap_up_keeps_tools": config.wrap_up_keeps_tools,
        "wrap_up_max_tool_rounds": config.wrap_up_max_tool_rounds,
        "wrap_up_tool_timeout_ms": config.wrap_up_tool_timeout_ms,
        "wrap_up_hard_cap_ms": config.wrap_up_hard_cap_ms,
        "steer_mode": config.steer_mode,
    }
    if config.budget is not None:
        payload["budget"] = {
            "max_tokens": config.budget.max_tokens,
            "max_steps": config.budget.max_steps,
            "max_tool_calls": config.budget.max_tool_calls,
            "cached_token_weight": config.budget.cached_token_weight,
        }
    return json.dumps(payload)


def _msg_to_json(message: LLMMessage) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for part in message.content:
        if isinstance(part, ImagePart):
            content.append(
                {
                    "type": "image",
                    "source": part.source,
                    "media_type": part.media_type,
                }
            )
        else:
            content.append({"type": "text", "text": getattr(part, "text", "")})
    tool_calls = []
    for call in message.tool_calls or []:
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        tool_calls.append({"id": call.id, "name": call.name, "arguments": arguments})
    return {
        "role": message.role,
        "content": content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": tool_calls,
        "reasoning": message.reasoning,
        "reasoning_details": message.reasoning_details,
    }


def _msg_from_json(raw: dict[str, Any]) -> LLMMessage:
    parts: list[Any] = []
    for part in raw.get("content") or []:
        if part.get("type") == "image":
            source = part.get("source") or ""
            media = part.get("media_type") or "image/png"
            if source.startswith(("http://", "https://", "data:")):
                parts.append(ImagePart.from_url(source, media_type=media))
            else:
                parts.append(ImagePart.from_base64(source, media_type=media))
        else:
            parts.append(TextPart(part.get("text") or ""))
    tool_calls = [
        ToolCall(
            id=call.get("id") or "",
            name=call["name"],
            arguments=call.get("arguments") or {},
        )
        for call in raw.get("tool_calls") or []
    ]
    return LLMMessage(
        role=raw["role"],
        content=parts,
        name=raw.get("name"),
        tool_call_id=raw.get("tool_call_id"),
        tool_calls=tool_calls or None,
        reasoning=raw.get("reasoning"),
        reasoning_details=raw.get("reasoning_details"),
    )


def _chunk_to_json(chunk: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content_delta": chunk.content_delta,
        "reasoning_delta": chunk.reasoning_delta,
        "reasoning_details": chunk.reasoning_details,
        "finish_reason": chunk.finish_reason,
    }
    if chunk.tool_call_delta is not None:
        call = chunk.tool_call_delta
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        payload["tool_call_delta"] = {
            "id": call.id,
            "name": call.name,
            "arguments": arguments,
        }
    if chunk.usage is not None:
        usage = chunk.usage
        payload["usage"] = {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cached_prompt_tokens": usage.cached_prompt_tokens,
            "cache_creation_tokens": usage.cache_creation_tokens,
        }
    return payload


def _result_to_json(result: ToolResult) -> dict[str, Any]:
    return result.model_dump()


def _result_from_json(raw: dict[str, Any]) -> ToolResult:
    return ToolResult.model_validate(raw)


async def _collect_stream(
    loop: CoreLoop,
    messages_json: str,
    tools: list[dict[str, Any]] | None,
    cancel_flag: threading.Event,
    run_started: float,
    surrogate_mode: list[bool],
) -> str:
    request = json.loads(messages_json)
    if isinstance(request, dict):
        message_items = request.get("messages", [])
        tool_choice = request.get("tool_choice")
        tools_enabled = bool(request.get("tools_enabled"))
    else:
        message_items = request
        tool_choice = None
        tools_enabled = True
    messages = [_msg_from_json(item) for item in message_items]
    wrap_up_request = any(
        message.role == "user"
        and message.content_text.startswith("[system notice] The time budget")
        for message in messages
    )
    stream = loop._provider.stream(
        messages,
        tools=tools if tools_enabled else None,
        **({"tool_choice": tool_choice} if tool_choice else {}),
    )
    chunks: list[dict[str, Any]] = []
    stream_started = time.monotonic()
    last_chunk_at = stream_started
    active_ms = 0
    stream_chars = 0
    tool_seen = False
    raw_content: list[str] = []
    stripper = PseudoStreamStripper()
    content_carry = ""
    cut_finish_reason: str | None = None
    import steerable_agent_runtime.loop as loop_module

    try:
        while True:
            soft_timeout_ms = loop._config.soft_timeout_ms
            wait_seconds = None
            if soft_timeout_ms is not None and not wrap_up_request:
                wait_seconds = max(
                    soft_timeout_ms / 1000 - (time.monotonic() - run_started),
                    0.000_001,
                )
            try:
                if wait_seconds is None:
                    chunk = await anext(stream)
                else:
                    chunk = await asyncio.wait_for(anext(stream), wait_seconds)
            except StopAsyncIteration:
                break
            except TimeoutError:
                cut_finish_reason = "__soft_timeout_cut__"
                break
            now = time.monotonic()
            gap = now - last_chunk_at
            last_chunk_at = now
            if gap <= loop_module._IDLE_REASONING_GAP_SEC:
                active_ms += int(gap * 1000)
            payload = _chunk_to_json(chunk)
            if chunk.content_delta:
                raw_content.append(chunk.content_delta)
                emit_text, content_carry = split_trailing_high_surrogate(
                    chunk.content_delta, content_carry
                )
                if content_carry or any(
                    0xD800 <= ord(char) <= 0xDFFF for char in chunk.content_delta
                ):
                    surrogate_mode[0] = True
                payload["content_delta"] = stripper.feed(emit_text) or None
            chunks.append(payload)
            if chunk.tool_call_delta is not None:
                tool_seen = True
                loop._run_context.reasoning_since_progress = 0
            if not tool_seen:
                delta_chars = len(chunk.reasoning_delta or "") + len(
                    chunk.content_delta or ""
                )
                stream_chars += delta_chars
                loop._run_context.reasoning_since_progress += len(
                    chunk.reasoning_delta or ""
                )
                trigger = None
                idle_ms = loop._config.idle_stream_timeout_ms
                max_chars = loop._config.idle_stream_max_chars
                stale_cap = loop._config.reasoning_without_progress_chars
                if idle_ms is not None and active_ms >= idle_ms:
                    trigger = "active_ms"
                elif max_chars is not None and stream_chars >= max_chars:
                    trigger = "chars"
                elif (
                    stale_cap is not None
                    and loop._run_context.reasoning_since_progress >= stale_cap
                ):
                    trigger = "no_progress"
                if trigger is not None:
                    stale_chars = loop._run_context.reasoning_since_progress
                    cut_finish_reason = (
                        f"__idle_stream_cut__:{trigger}:{stream_chars}:"
                        f"{stale_chars}"
                    )
                    if trigger == "no_progress":
                        loop._run_context.reasoning_since_progress = 0
                    break
            if cancel_flag.is_set() or loop._cancel_event.is_set():
                break
    except Exception as exc:  # noqa: BLE001 — Rust applies on_request_error
        return json.dumps(
            {
                "ok": False,
                "error": str(exc),
                "kind": getattr(exc, "kind", "unknown"),
                "status_code": getattr(exc, "status_code", None),
                "provider": getattr(exc, "provider", None),
                "retry_after_ms": getattr(exc, "retry_after_ms", None),
            }
        )
    display_tail = stripper.feed(content_carry) + stripper.flush()
    if display_tail:
        chunks.append({"content_delta": display_tail})
    if cut_finish_reason is not None:
        chunks.append({"finish_reason": cut_finish_reason})
    elif not tool_seen:
        recovered, _ = extract_inline_tool_calls("".join(raw_content))
        for index, call in enumerate(recovered):
            chunks.append(
                {
                    "tool_call_delta": {
                        "id": f"recovered_{index}",
                        "name": call["name"],
                        "arguments": call.get("arguments") or {},
                    }
                }
            )
    return json.dumps(chunks)


async def _exec_tool(loop: CoreLoop, call_json: str) -> str:
    from steerable_agent_runtime.approval import ApprovalAborted

    raw = json.loads(call_json)
    call = ToolCall(
        id=raw.get("id") or "",
        name=raw["name"],
        arguments=raw.get("arguments") or {},
    )
    try:
        result = await loop._execute_tool(call, loop._run_context)
    except ApprovalAborted as exc:
        return json.dumps({"ok": False, "error": f"__approval_abort__:{exc}"})
    except Exception as exc:  # noqa: BLE001 — matches CoreLoop tool_error
        return json.dumps({"ok": False, "error": str(exc)})
    made_progress = getattr(loop._hooks, "tool_made_progress", None)
    if callable(made_progress) and made_progress(result, call):
        loop._run_context.reasoning_since_progress = 0
    return json.dumps({"ok": True, "result": _result_to_json(result)})


async def _before_completion(loop: CoreLoop, draft_json: str) -> str:
    raw = json.loads(draft_json)
    draft = CompletionDraft(
        status=raw.get("status") or "",
        reason=raw.get("reason") or "",
        content=raw.get("content") or "",
        round_index=int(raw.get("round_index") or 0),
        had_tool_calls=bool(raw.get("had_tool_calls")),
        tool_calls_used=int(raw.get("tool_calls_used") or 0),
        tool_successes=int(raw.get("tool_successes") or 0),
    )
    action: CompletionAction = await loop._hooks.before_completion(draft, loop._run_context)
    return json.dumps(
        {"kind": action.kind, "message": action.message, "reason": action.reason}
    )


async def _post_tool_result(loop: CoreLoop, payload_json: str) -> str:
    raw = json.loads(payload_json)
    result = _result_from_json(raw["result"])
    call_raw = raw["call"]
    call = ToolCall(
        id=call_raw.get("id") or "",
        name=call_raw["name"],
        arguments=call_raw.get("arguments") or {},
    )
    updated = await loop._hooks.post_tool_result(result, call, loop._run_context)
    made_progress = getattr(loop._hooks, "tool_made_progress", None)
    if callable(made_progress) and made_progress(updated, call):
        loop._run_context.reasoning_since_progress = 0
    return json.dumps(_result_to_json(updated))


async def _pre_step(
    loop: CoreLoop, manager: ContextManager, transcript_json: str
) -> str:
    messages = [_msg_from_json(item) for item in json.loads(transcript_json)]
    action = await loop._hooks.pre_step(messages, loop._run_context)
    rewrite = action.rewrite
    if rewrite is not None:
        _apply_rewrite(manager, rewrite)
    applied_appends: list[LLMMessage] = []
    for item in action.appends or ():
        if item.fragment is not None:
            entry = manager.append_fragment(item.fragment)
        else:
            entry = manager.append(item.message, kind=item.kind)
        applied_appends.append(entry.message)
    if action.kind != "reject" and loop._compact_event.is_set():
        loop._compact_event.clear()
        compact_now = getattr(loop._hooks, "compact_now", None)
        if callable(compact_now):
            manual = await compact_now(manager.projection, loop._run_context)
            if manual.rewrite is not None:
                rewrite = manual.rewrite
                _apply_rewrite(manager, rewrite)
    payload: dict[str, Any] = {
        "kind": action.kind,
        "reason": rewrite.reason if rewrite is not None else action.reason,
        "action": (
            rewrite.action
            if rewrite is not None
            else action.append_action or "append"
        ),
        "tool_choice": action.tool_choice,
        "pre_tokens": (
            rewrite.pre_tokens if rewrite is not None else None
        ),
        "post_tokens": (
            rewrite.post_tokens if rewrite is not None else None
        ),
    }
    if applied_appends:
        payload["appends"] = [_msg_to_json(message) for message in applied_appends]
    if rewrite is not None:
        payload["rewrite"] = [_msg_to_json(message) for message in manager.projection]
    return json.dumps(payload)


async def _on_request_error(
    loop: CoreLoop, manager: ContextManager, payload_json: str
) -> str:
    raw = json.loads(payload_json)
    error_raw = raw["error"]
    error = LLMError(
        error_raw.get("message") or "provider request failed",
        kind=error_raw.get("kind") or "unknown",
        status_code=error_raw.get("status_code"),
        provider=error_raw.get("provider"),
        retry_after_ms=error_raw.get("retry_after_ms"),
    )
    transcript = [_msg_from_json(item) for item in raw.get("transcript", [])]
    loop._run_context.round_index = int(raw.get("round_index") or 0)
    action = await loop._hooks.on_request_error(error, transcript, loop._run_context)
    if action.rewrite is not None:
        _apply_rewrite(manager, action.rewrite)
    payload: dict[str, Any] = {
        "kind": action.kind,
        "delay_ms": action.delay_ms,
        "reason": action.reason,
    }
    if action.rewrite is not None:
        payload["rewrite"] = [
            _msg_to_json(message) for message in action.rewrite.messages
        ]
    return json.dumps(payload)


def _native_history_kind(message: LLMMessage) -> str | None:
    if (
        message.role == "user"
        and message.content_text.startswith("[system notice] The time budget")
    ):
        return "loop.soft_timeout_notice"
    if (
        message.role == "tool"
        and "not executed" in message.content_text
        and "approval abort" in message.content_text
    ):
        return "loop.abort_skip"
    if (
        message.role == "tool"
        and "not executed" in message.content_text
        and "turn was cancelled" in message.content_text
    ):
        return "loop.cancel_skip"
    if (
        message.role == "tool"
        and "not executed" in message.content_text
        and "mid-turn message" in message.content_text
    ):
        return "loop.steer_skip"
    return None


async def _sync_history(
    loop: CoreLoop,
    manager: ContextManager,
    history_json: str,
    chat_id: str | None,
) -> str:
    incoming = [_msg_from_json(item) for item in json.loads(history_json)]
    current = manager.projection
    if incoming[: len(current)] == current:
        for message in incoming[len(current) :]:
            manager.append(message, kind=_native_history_kind(message))
    elif incoming != current:
        manager.replace_all(
            incoming,
            reason="native CoreLoop history rewrite",
            action="native_sync",
        )
    await loop._flush_history(manager, chat_id)
    return json.dumps({"ok": True})


def _await_on_loop(coro: Any, aio_loop: asyncio.AbstractEventLoop) -> str:
    """Schedule ``coro`` on the running asyncio loop; block without holding the GIL."""
    box: queue.Queue[str] = queue.Queue()

    def start() -> None:
        task = aio_loop.create_task(coro)

        def done(completed: asyncio.Task[str]) -> None:
            try:
                box.put(completed.result())
            except Exception as exc:  # noqa: BLE001 — surface to Rust
                logger.exception("Rust CoreLoop Python callback failed")
                box.put(json.dumps({"ok": False, "error": str(exc)}))

        task.add_done_callback(done)

    aio_loop.call_soon_threadsafe(start)
    return box.get()


async def run_native(
    loop: CoreLoop,
    messages: Sequence[LLMMessage],
    *,
    tools: list[dict[str, Any]] | None = None,
    chat_id: str | None = None,
) -> AsyncIterator[LoopEvent]:
    """Yield LoopEvents from the Rust CoreLoop, calling back into Python I/O."""
    import steerable_agent_runtime_native as native

    aio_loop = asyncio.get_running_loop()
    out: asyncio.Queue[tuple[LoopEvent, threading.Event] | None] = asyncio.Queue()
    loop._run_context = LoopContext(chat_id=chat_id)
    record_id = loop._record_id or chat_id
    seed = list(messages)
    first_seq = 0
    persisted_prefix = 0
    pre_boundary: dict[str, Any] | None = None
    if loop._history_store is not None and record_id is not None:
        seed, first_seq, persisted_prefix, pre_boundary = (
            await loop._plan_record_seeding(record_id, list(messages))
        )
    if (
        pre_boundary is not None
        and loop._history_store is not None
        and record_id is not None
    ):
        await loop._history_store.append_history(record_id, [pre_boundary])
    manager = ContextManager(
        seed,
        token_model=getattr(loop._provider, "model", None),
        first_seq=first_seq,
    )
    if persisted_prefix:
        manager.mark_persisted_prefix(persisted_prefix)
    loop.history = manager
    worker_error: list[BaseException] = []
    worker_result: list[dict[str, Any]] = []
    cancel_flag = threading.Event()
    steer_q: queue.Queue[str] = queue.Queue()
    active_tool_tasks: set[asyncio.Task[str]] = set()
    tool_cancel_reason = ["cancelled"]
    run_started = time.monotonic()
    surrogate_mode = [False]
    logger.info("CoreLoop engine=rust chat_id=%s", chat_id)

    while True:
        try:
            steer_q.put_nowait(loop._inbox.get_nowait())
        except asyncio.QueueEmpty:
            break
    if loop._cancel_event.is_set():
        cancel_flag.set()

    orig_cancel = loop.cancel
    orig_steer = loop.steer

    def cancel_active_tools(reason: str) -> None:
        tool_cancel_reason[0] = reason

        def cancel_now() -> None:
            for task in tuple(active_tool_tasks):
                task.cancel()

        aio_loop.call_soon_threadsafe(cancel_now)

    def cancel() -> None:
        cancel_flag.set()
        cancel_active_tools("cancelled")
        orig_cancel()

    def steer(content: str) -> None:
        orig_steer(content)
        while True:
            try:
                steer_q.put_nowait(loop._inbox.get_nowait())
            except asyncio.QueueEmpty:
                break
        if content and loop._config.steer_mode == "interrupt":
            cancel_active_tools("interrupted by steer")

    loop.cancel = cancel  # type: ignore[method-assign]
    loop.steer = steer  # type: ignore[method-assign]

    def emit(kind: str, data_json: str) -> None:
        data = json.loads(data_json)
        if kind == "content_delta" and surrogate_mode[0]:
            delta = data.get("delta")
            if isinstance(delta, str):
                expanded: list[str] = []
                for char in delta:
                    codepoint = ord(char)
                    if codepoint > 0xFFFF:
                        codepoint -= 0x10000
                        expanded.extend(
                            (
                                chr(0xD800 + (codepoint >> 10)),
                                chr(0xDC00 + (codepoint & 0x3FF)),
                            )
                        )
                    else:
                        expanded.append(char)
                data["delta"] = "".join(expanded)
        event = LoopEvent(kind, data)  # type: ignore[arg-type]
        if kind == "completion":
            step_keys = (
                "round",
                "traceStepId",
                "finishReason",
                "textLength",
                "toolCalls",
                "toolCallCount",
                "toolErrorCount",
            )
            step = {key: data[key] for key in step_keys if key in data}
            decision = {
                key: data[key]
                for key in ("status", "reason", "confidence")
                if key in data
            }
            loop.trajectory.append(build_step_decision_event(step, decision))
        consumed = threading.Event()
        aio_loop.call_soon_threadsafe(out.put_nowait, (event, consumed))
        consumed.wait()

    def stream_llm(messages_json: str) -> str:
        return _await_on_loop(
            _collect_stream(
                loop,
                messages_json,
                tools,
                cancel_flag,
                run_started,
                surrogate_mode,
            ),
            aio_loop,
        )

    def exec_tool(call_json: str) -> str:
        box: queue.Queue[str] = queue.Queue()

        def start() -> None:
            task = aio_loop.create_task(_exec_tool(loop, call_json))
            active_tool_tasks.add(task)

            def done(completed: asyncio.Task[str]) -> None:
                active_tool_tasks.discard(completed)
                try:
                    box.put(completed.result())
                except asyncio.CancelledError:
                    box.put(
                        json.dumps(
                            {"ok": False, "error": tool_cancel_reason[0]}
                        )
                    )
                except BaseException as exc:  # noqa: BLE001
                    box.put(json.dumps({"ok": False, "error": str(exc)}))

            task.add_done_callback(done)

        aio_loop.call_soon_threadsafe(start)
        return box.get()

    def tool_concurrency_safe(call_json: str) -> bool:
        raw = json.loads(call_json)
        call = ToolCall(
            id=raw.get("id") or "",
            name=raw["name"],
            arguments=raw.get("arguments") or {},
        )
        check = getattr(loop._executor, "concurrency_safe", None)
        return bool(check is not None and check(call))

    def tool_dedup_exempt(call_json: str) -> bool:
        raw = json.loads(call_json)
        call = ToolCall(
            id=raw.get("id") or "",
            name=raw["name"],
            arguments=raw.get("arguments") or {},
        )
        check = getattr(loop._executor, "dedup_exempt", None)
        return bool(check is not None and check(call))

    def sync_history(history_json: str) -> str:
        return _await_on_loop(
            _sync_history(loop, manager, history_json, chat_id),
            aio_loop,
        )

    def sync_context(context_json: str) -> str:
        raw = json.loads(context_json)
        ctx = loop._run_context
        prior_tool_successes = ctx.tool_successes
        ctx.round_index = int(raw.get("round_index") or 0)
        ctx.tool_calls_used = int(raw.get("tool_calls_used") or 0)
        ctx.tool_successes = int(raw.get("tool_successes") or 0)
        if ctx.tool_successes > prior_tool_successes:
            ctx.reasoning_since_progress = 0
        ctx.consecutive_tool_errors = int(raw.get("consecutive_tool_errors") or 0)
        ctx.last_prompt_transcript_len = int(
            raw.get("last_prompt_transcript_len") or 0
        )
        ctx.last_prompt_tokens = (
            int(raw.get("last_prompt_tokens") or 0)
            if ctx.last_prompt_transcript_len
            else None
        )
        ctx.last_cached_prompt_tokens = int(
            raw.get("last_cached_prompt_tokens") or 0
        )
        ctx.last_cache_creation_tokens = int(
            raw.get("last_cache_creation_tokens") or 0
        )
        ctx.accumulated_prompt_tokens = int(
            raw.get("accumulated_prompt_tokens") or 0
        )
        ctx.accumulated_completion_tokens = int(
            raw.get("accumulated_completion_tokens") or 0
        )
        return json.dumps({"ok": True})

    def before_completion(draft_json: str) -> str:
        return _await_on_loop(_before_completion(loop, draft_json), aio_loop)

    def post_tool_result(payload_json: str) -> str:
        return _await_on_loop(_post_tool_result(loop, payload_json), aio_loop)

    def pre_step(transcript_json: str) -> str:
        return _await_on_loop(_pre_step(loop, manager, transcript_json), aio_loop)

    def on_request_error(payload_json: str) -> str:
        return _await_on_loop(
            _on_request_error(loop, manager, payload_json), aio_loop
        )

    def wrap_up_may_drop_tools() -> bool:
        callback = getattr(loop._hooks, "wrap_up_may_drop_tools", None)
        return bool(callback is None or callback())

    def poll_control(payload_json: str) -> str:
        drain = True
        if payload_json:
            try:
                drain = bool(json.loads(payload_json).get("drain", True))
            except json.JSONDecodeError:
                drain = True
        interrupt = loop._config.steer_mode == "interrupt" and not steer_q.empty()
        steers: list[str] = []
        if drain:
            while True:
                try:
                    content = steer_q.get_nowait()
                    steers.append(content)
                    manager.append(
                        LLMMessage.text_of("user", content),
                        kind="steer.inject",
                    )
                except queue.Empty:
                    break
        return json.dumps(
            {
                "cancel": cancel_flag.is_set(),
                "interrupt": interrupt,
                "steers": steers,
            }
        )

    def worker() -> None:
        try:
            usage_json = native.run_turn(
                _config_json(loop),
                json.dumps([_msg_to_json(message) for message in seed]),
                json.dumps(tools or []),
                getattr(loop._provider, "name", "unknown"),
                getattr(loop._provider, "model", "unknown"),
                emit,
                stream_llm,
                exec_tool,
                tool_concurrency_safe,
                tool_dedup_exempt,
                sync_history,
                sync_context,
                before_completion,
                post_tool_result,
                poll_control,
                pre_step,
                on_request_error,
                wrap_up_may_drop_tools,
            )
            raw = json.loads(usage_json or "{}")
            worker_result.append(raw)
            ctx = loop._run_context
            if ctx is not None:
                ctx.accumulated_prompt_tokens = int(raw.get("prompt_tokens") or 0)
                ctx.accumulated_completion_tokens = int(raw.get("completion_tokens") or 0)
                ctx.last_cached_prompt_tokens = int(raw.get("cached_prompt_tokens") or 0)
                ctx.last_cache_creation_tokens = int(raw.get("cache_creation_tokens") or 0)
        except BaseException as exc:  # noqa: BLE001 — deliver to async consumer
            worker_error.append(exc)
            logger.exception("Rust CoreLoop worker failed")
        finally:
            aio_loop.call_soon_threadsafe(out.put_nowait, None)

    threading.Thread(target=worker, daemon=True, name="steerable-rust-coreloop").start()
    while True:
        item = await out.get()
        if item is None:
            break
        event, consumed = item
        try:
            yield event
        finally:
            consumed.set()
    if worker_error:
        raise worker_error[0]
    raw_history = worker_result[0].get("history", []) if worker_result else []
    if raw_history:
        await _sync_history(loop, manager, json.dumps(raw_history), chat_id)
    loop.cancel = orig_cancel  # type: ignore[method-assign]
    loop.steer = orig_steer  # type: ignore[method-assign]
