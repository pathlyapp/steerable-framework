"""Sidecar core: wires the runtime adapters into a JSON-RPC server.

Methods (see spec/sidecar/README.md for the full catalog):

  system.ping              -> SidecarHealth
  system.shutdown          -> null
  system.shutdown_now      -> null
  agent.session.create     -> AgentSession
  agent.session.resume     -> AgentSession
  agent.session.list       -> AgentSession[]
  agent.chat.stream        -> { streamId } (chunks pushed via `stream.chunk`,
                                            terminator via `stream.done`)
  agent.chat.cancel        -> null         (best-effort cancel of an in-flight stream)
  tool.list                -> ToolDescriptor[]
  tool.invoke              -> ToolResult
  trace.fetch              -> { trace, spans, events }
  config.get / config.set
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from steerable_agent_harness import BudgetLimit
from steerable_agent_protocol.generated import (
    AgentSession,
    SidecarHealth,
    ToolCall,
)
from steerable_agent_runtime import (
    AntiHallucinationConfig,
    AntiHallucinationHooks,
    BudgetExhaustedError,
    ChainHooks,
    CompactionHooks,
    CoreLoop,
    LoopConfig,
    LoopHooks,
    PolicyDeniedError,
    RetryHooks,
    RouterToolExecutor,
    StorageError,
    ToolDispatchError,
    ToolRouter,
    TraceRecorder,
)
from steerable_agent_runtime.llm import LLMMessage, LLMProvider
from steerable_agent_runtime.storage import InMemoryStorage, StorageAdapter
from steerable_agent_runtime.transport.stdio_jsonrpc import (
    JsonRpcError,
    JsonRpcServer,
    StdioJsonRpcTransport,
    encode_frame,
)

from .host_tools import HostToolExecutor

logger = logging.getLogger("steerable_sidecar")

PROTOCOL_VERSION = "0.1.0"
SIDECAR_VERSION = "0.1.0"
READY_PREFIX = "__SIDECAR_READY__:"


@dataclass
class SidecarConfig:
    """Sidecar runtime configuration."""

    log_level: str = "INFO"
    quiet_stderr: bool = False
    grace_period_seconds: float = 5.0
    install_signal_handlers: bool = True
    initial_tools: list[Any] = field(default_factory=list)


class Sidecar:
    """In-process sidecar harness.

    The main entrypoint composes a `JsonRpcServer`, a default `ToolRouter`, an
    `InMemoryStorage`, and a `StdioJsonRpcTransport`. Embedders can swap any of
    these by setting the corresponding attribute before calling ``serve()``.
    """

    def __init__(
        self,
        *,
        config: SidecarConfig | None = None,
        storage: StorageAdapter | None = None,
        tools: ToolRouter | None = None,
        llm_provider_factory: Any | None = None,
        loop_hooks_factory: Any | None = None,
    ) -> None:
        self.config = config or SidecarConfig()
        self.storage: StorageAdapter = storage or InMemoryStorage()
        self.tools: ToolRouter = tools or ToolRouter()
        self.server = JsonRpcServer()
        self._llm_provider_factory = llm_provider_factory or default_llm_provider_factory
        # Optional embedder hook for the CoreLoop chat path — receives the
        # request params, returns a LoopHooks (e.g. ChainHooks of retry +
        # compaction + spill). Defaults to RetryHooks alone.
        self._loop_hooks_factory = loop_hooks_factory
        self._streams: dict[str, asyncio.Task[Any]] = {}
        #: Active CoreLoop instances by stream id — the steer RPC targets
        #: these to inject user messages into a running turn.
        self._coreloops: dict[str, CoreLoop] = {}
        self._transport: StdioJsonRpcTransport | None = None
        self._started_ms = int(time.monotonic() * 1000)
        self._wall_started_ms = int(time.time() * 1000)
        self._shutdown_requested = asyncio.Event()
        self._serving = False

        self._register_default_methods()
        for tool in self.config.initial_tools:
            self.tools.register(tool)

    # ------------------------------------------------------------------
    # Method registration
    # ------------------------------------------------------------------

    def _register_default_methods(self) -> None:
        register = self.server.register
        register("system.ping", self._handle_ping)
        register("system.shutdown", self._handle_shutdown)
        register("system.shutdown_now", self._handle_shutdown_now)
        register("agent.session.create", self._handle_session_create)
        register("agent.session.resume", self._handle_session_resume)
        register("agent.session.list", self._handle_session_list)
        register("tool.list", self._handle_tool_list)
        register("tool.invoke", self._handle_tool_invoke)
        register("trace.fetch", self._handle_trace_fetch)
        register("config.get", self._handle_config_get)
        register("config.set", self._handle_config_set)
        register("agent.chat.stream", self._handle_chat_stream)
        register("agent.chat.cancel", self._handle_chat_cancel)
        register("agent.chat.steer", self._handle_chat_steer)

    # ------------------------------------------------------------------
    # Entrypoint
    # ------------------------------------------------------------------

    async def serve(self) -> None:
        """Run the sidecar until shutdown is requested."""

        self._configure_logging()
        if self.config.install_signal_handlers:
            self._install_signal_handlers()

        ready = await self.snapshot_health()
        self._emit_ready_marker(ready)

        reader, writer = await self._connect_stdio()
        transport = StdioJsonRpcTransport(writer)
        self._transport = transport
        self.server.attach_writer(writer)
        await transport.emit_notification(
            "lifecycle.ready",
            {
                "version": SIDECAR_VERSION,
                "protocolVersion": PROTOCOL_VERSION,
                "pid": os.getpid(),
                "listenInfo": {"transport": "stdio"},
            },
        )

        self._serving = True
        in_flight: set[asyncio.Task[None]] = set()
        try:
            while not self._shutdown_requested.is_set():
                line_task = asyncio.ensure_future(reader.readline())
                shutdown_task = asyncio.ensure_future(self._shutdown_requested.wait())
                done, pending = await asyncio.wait(
                    {line_task, shutdown_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if shutdown_task in done:
                    break
                line = line_task.result()
                if not line:
                    break
                # Dispatch each frame on its own task so the read loop keeps
                # serving while a handler awaits a reverse (sidecar -> host)
                # call — otherwise the two peers deadlock.
                task = asyncio.ensure_future(self._process_line(line, writer))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        finally:
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            await transport.emit_notification(
                "lifecycle.shutdown",
                {"reason": "normal" if self._shutdown_requested.is_set() else "eof"},
            )
            await transport.aclose()
            close = getattr(writer, "close", None)
            if close is not None:
                close()
            self._serving = False

    async def _process_line(self, line: bytes, writer: Any) -> None:
        response = await self.server.handle_frame(line.decode("utf-8"))
        if response is None:
            return
        writer.write(encode_frame(response))
        await self._maybe_drain(writer)

    async def request_shutdown(self) -> None:
        self._shutdown_requested.set()

    async def snapshot_health(self) -> SidecarHealth:
        uptime = int(time.monotonic() * 1000) - self._started_ms
        return SidecarHealth(
            status="ok" if self._serving or self._started_ms else "starting",
            version=SIDECAR_VERSION,
            protocolVersion=PROTOCOL_VERSION,
            uptimeMs=max(0, uptime),
            pid=os.getpid(),
            pythonVersion=platform.python_version(),
            platform=f"{sys.platform}-{platform.machine()}",
            loadedTools=len(self.tools.list_tools()),
            activeTraces=0,
        )

    # ------------------------------------------------------------------
    # Method handlers
    # ------------------------------------------------------------------

    async def _handle_ping(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        health = await self.snapshot_health()
        return health.model_dump(exclude_none=True)

    async def _handle_shutdown(self, _params: dict[str, Any] | None) -> None:
        _flush_shared_calibration()
        # Schedule the actual stop so the response can be drained first.
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, lambda: self._shutdown_requested.set())

    async def _handle_shutdown_now(self, _params: dict[str, Any] | None) -> None:
        _flush_shared_calibration()
        self._shutdown_requested.set()

    async def _handle_session_create(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = _require_params(params)
        session = AgentSession(
            sessionId=params.get("sessionId") or _new_session_id(),
            userId=params.get("userId") or "local",
            chatId=params["chatId"],
            currentStage=params.get("currentStage", "plan"),
            isActive=True,
            createdAt=_iso_now(),
            updatedAt=_iso_now(),
            scenario=params.get("scenario", "agent-entry"),
            stageData=params.get("stageData"),
            projectId=params.get("projectId"),
        )
        try:
            stored = await self.storage.upsert_session(session)
        except StorageError as exc:
            raise JsonRpcError(str(exc), code=-32011, kind="internal") from exc
        return stored.model_dump(exclude_none=True)

    async def _handle_session_resume(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = _require_params(params)
        session_id = params.get("sessionId")
        if not session_id:
            raise JsonRpcError("sessionId required", code=-32602, kind="invalid_params")
        session = await self.storage.get_session(session_id)
        if session is None:
            raise JsonRpcError(
                f"session not found: {session_id}", code=-32004, kind="invalid_request"
            )
        return session.model_dump(exclude_none=True)

    async def _handle_session_list(self, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        params = params or {}
        sessions = await self.storage.list_sessions(
            user_id=params.get("userId"),
            chat_id=params.get("chatId"),
            active_only=bool(params.get("activeOnly", False)),
        )
        return [s.model_dump(exclude_none=True) for s in sessions]

    async def _handle_tool_list(self, _params: dict[str, Any] | None) -> list[dict[str, Any]]:
        return self.tools.describe()

    async def _handle_tool_invoke(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = _require_params(params)
        try:
            call = ToolCall(
                id=params.get("id") or _new_call_id(),
                name=params["name"],
                arguments=params.get("arguments") or {},
            )
        except KeyError as exc:
            raise JsonRpcError(
                f"missing argument: {exc.args[0]}", code=-32602, kind="invalid_params"
            ) from exc
        try:
            result = await self.tools.dispatch(
                call,
                consent_granted=bool(params.get("consentGranted", False)),
                context=params.get("context"),
            )
        except PolicyDeniedError as exc:
            raise JsonRpcError(
                exc.message, code=-32020, kind="policy_denied", data=exc.data
            ) from exc
        except BudgetExhaustedError as exc:
            raise JsonRpcError(
                exc.message, code=-32021, kind="budget_exhausted", data=exc.data
            ) from exc
        except ToolDispatchError as exc:
            raise JsonRpcError(
                exc.message, code=-32030, kind="tool_failed", data=exc.data
            ) from exc
        return result.model_dump(exclude_none=True)

    async def _handle_trace_fetch(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = _require_params(params)
        trace_id = params.get("traceId")
        if not trace_id:
            raise JsonRpcError("traceId required", code=-32602, kind="invalid_params")
        trace = await self.storage.get_trace(trace_id)
        if trace is None:
            raise JsonRpcError(
                f"trace not found: {trace_id}", code=-32004, kind="invalid_request"
            )
        spans = await self.storage.list_spans(trace_id)
        events = await self.storage.list_events(trace_id)
        return {
            "trace": trace.model_dump(exclude_none=True),
            "spans": [s.model_dump(exclude_none=True) for s in spans],
            "events": [e.model_dump(exclude_none=True) for e in events],
        }

    async def _handle_config_get(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "logLevel": self.config.log_level,
            "gracePeriodSeconds": self.config.grace_period_seconds,
            "version": SIDECAR_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
        }

    async def _handle_config_set(self, params: dict[str, Any] | None) -> None:
        params = _require_params(params)
        log_level = params.get("logLevel")
        if log_level is not None:
            self.config.log_level = str(log_level)
            logging.getLogger().setLevel(self.config.log_level)

    async def _handle_chat_stream(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Start a streaming chat-completion run.

        Params shape (all optional unless noted)::

            {
              "provider": "openai_compat" | "anthropic" | <custom>,  # required
              "model": "gpt-4o-mini",                                # required
              "messages": [{"role": "...", "content": "..."}],       # required
              "baseUrl": "https://api.example.com/v1",
              "apiKey":  "sk-...",
              "temperature": 0.2,
              "maxTokens": 1024,
              "tools":   [...],         # OpenAI tool descriptors
              "streamId": "str_xyz",    # auto-generated if omitted
              "providerOptions": {...}, # passthrough
            }

        Returns ``{"streamId": "..."}`` immediately. Chunks arrive as
        ``stream.chunk`` notifications with ``{"streamId", "delta"}``;
        completion is signalled by ``stream.done`` with ``{"streamId",
        "finishReason", "usage"}``. Errors mid-stream are emitted as
        ``stream.error``.
        """

        params = _require_params(params)
        if self._transport is None:
            raise JsonRpcError(
                "transport not ready", code=-32099, kind="internal"
            )
        try:
            provider = self._llm_provider_factory(params)
        except Exception as exc:  # surface as RPC error before scheduling task
            raise JsonRpcError(
                f"failed to construct LLM provider: {exc}",
                code=-32602,
                kind="invalid_params",
            ) from exc

        stream_id = params.get("streamId") or _new_stream_id()
        messages = _coerce_messages(params.get("messages") or [])

        transport = self._transport
        if _use_coreloop(params):
            task = asyncio.create_task(
                self._run_chat_stream_coreloop(provider, messages, params, stream_id, transport)
            )
        else:
            kwargs = _build_provider_kwargs(params)
            task = asyncio.create_task(
                self._run_chat_stream(provider, messages, kwargs, stream_id, transport)
            )
        self._streams[stream_id] = task
        return {"streamId": stream_id}

    async def _handle_chat_cancel(self, params: dict[str, Any] | None) -> None:
        params = _require_params(params)
        stream_id = params.get("streamId")
        if not stream_id:
            raise JsonRpcError("streamId required", code=-32602, kind="invalid_params")
        task = self._streams.pop(stream_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _handle_chat_steer(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Inject a user message into a running CoreLoop turn.

        Soft-fail with ``{"ok": False}`` when the stream is unknown or not
        CoreLoop-backed — between the user hitting send and this RPC landing,
        the turn may legitimately have completed.
        """
        params = _require_params(params)
        stream_id = params.get("streamId")
        content = params.get("content")
        if not stream_id or not isinstance(content, str) or not content.strip():
            raise JsonRpcError(
                "streamId and non-empty content required",
                code=-32602,
                kind="invalid_params",
            )
        loop = self._coreloops.get(stream_id)
        if loop is None:
            return {"ok": False, "reason": "stream_not_active"}
        loop.steer(content)
        return {"ok": True}

    async def _run_chat_stream(
        self,
        provider: LLMProvider,
        messages: list[LLMMessage],
        kwargs: dict[str, Any],
        stream_id: str,
        transport: StdioJsonRpcTransport,
    ) -> None:
        try:
            iterator = provider.stream(messages, **kwargs)
            async for chunk in iterator:
                payload: dict[str, Any] = {"streamId": stream_id}
                if chunk.content_delta is not None:
                    payload["delta"] = chunk.content_delta
                if chunk.reasoning_delta is not None:
                    payload["reasoningDelta"] = chunk.reasoning_delta
                if chunk.tool_call_delta is not None:
                    payload["toolCall"] = chunk.tool_call_delta.model_dump(
                        exclude_none=True
                    )
                if chunk.finish_reason is not None:
                    payload["finishReason"] = chunk.finish_reason
                if chunk.usage is not None:
                    payload["usage"] = {
                        "promptTokens": chunk.usage.prompt_tokens,
                        "completionTokens": chunk.usage.completion_tokens,
                        "totalTokens": chunk.usage.total_tokens,
                    }
                await transport.emit_notification("stream.chunk", payload)
            await transport.emit_notification(
                "stream.done", {"streamId": stream_id, "ok": True}
            )
        except asyncio.CancelledError:
            await transport.emit_notification(
                "stream.done", {"streamId": stream_id, "ok": False, "cancelled": True}
            )
        except Exception as exc:
            logger.exception("chat stream %s failed", stream_id)
            await transport.emit_notification(
                "stream.error",
                {
                    "streamId": stream_id,
                    "kind": exc.__class__.__name__,
                    "message": str(exc),
                },
            )
        finally:
            self._streams.pop(stream_id, None)

    async def _run_chat_stream_coreloop(
        self,
        provider: LLMProvider,
        messages: list[LLMMessage],
        params: dict[str, Any],
        stream_id: str,
        transport: StdioJsonRpcTransport,
    ) -> None:
        """CoreLoop-backed chat stream (flag-gated — see ``_use_coreloop``).

        Maps LoopEvents onto the existing wire surface so hosts don't need a
        new protocol to opt in: content/reasoning deltas and tool progress
        arrive as ``stream.chunk``; the terminal completion arrives as
        ``stream.done`` with the loop's status/reason attached.
        """

        hooks: LoopHooks = (
            self._loop_hooks_factory(params)
            if self._loop_hooks_factory is not None
            else _default_loop_hooks(params)
        )
        # antiHallucination: sink the desktop loop's four guards (data-need
        # routing, deferred/claimed retry, grounding judge, narration) into
        # the CoreLoop via hooks. Off unless the host opts in.
        ah = params.get("antiHallucination")
        if ah:
            ah_opts = ah if isinstance(ah, dict) else {}
            tool_context = params.get("toolContext") or {}
            hooks = ChainHooks(
                AntiHallucinationHooks(
                    provider,
                    AntiHallucinationConfig(
                        mode=tool_context.get("mode"),
                        user_question=_last_message_content(messages, "user"),
                        last_assistant_tail=_last_message_content(
                            messages, "assistant", tail=400
                        ),
                        max_retries=int(ah_opts.get("maxRetries") or 2),
                        tools_available=bool(params.get("tools")),
                    ),
                ),
                hooks,
            )
        # toolsViaHost: every tool call is forwarded to the host over the
        # reverse channel (desktop deployment — tools live in Electron).
        # Otherwise the sidecar-local registry executes them.
        executor = (
            HostToolExecutor(
                self.server, tool_context=params.get("toolContext") or None
            )
            if params.get("toolsViaHost")
            else RouterToolExecutor(self.tools)
        )
        loop = CoreLoop(
            provider,
            executor,
            _build_loop_config(params),
            hooks=hooks,
        )
        # Persist the run as it streams so the host can inspect it afterwards
        # via trace.fetch (and so a future resume projection has the events).
        recorder = TraceRecorder(self.storage, chat_id=params.get("chatId"))
        self._coreloops[stream_id] = loop
        try:
            async for event in recorder.tee(
                loop.run(
                    messages,
                    tools=params.get("tools"),
                    chat_id=params.get("chatId"),
                )
            ):
                await self._emit_loop_event(transport, stream_id, event, recorder.trace_id)
        except asyncio.CancelledError:
            await transport.emit_notification(
                "stream.done",
                {
                    "streamId": stream_id,
                    "ok": False,
                    "cancelled": True,
                    "traceId": recorder.trace_id,
                },
            )
        except Exception as exc:
            logger.exception("coreloop chat stream %s failed", stream_id)
            await transport.emit_notification(
                "stream.error",
                {
                    "streamId": stream_id,
                    "kind": exc.__class__.__name__,
                    "message": str(exc),
                    # Failed turns are exactly the traces worth keeping — let
                    # the host persist them via trace.fetch.
                    "traceId": recorder.trace_id,
                },
            )
        finally:
            self._coreloops.pop(stream_id, None)
            await recorder.finalize()
            self._streams.pop(stream_id, None)

    @staticmethod
    async def _emit_loop_event(
        transport: StdioJsonRpcTransport,
        stream_id: str,
        event: Any,
        trace_id: str | None = None,
    ) -> None:
        kind = event.kind
        data = event.data
        if kind == "content_delta":
            await transport.emit_notification(
                "stream.chunk", {"streamId": stream_id, "delta": data["delta"]}
            )
        elif kind == "reasoning_delta":
            await transport.emit_notification(
                "stream.chunk", {"streamId": stream_id, "reasoningDelta": data["delta"]}
            )
        elif kind == "tool_call_start":
            await transport.emit_notification(
                "stream.chunk",
                {
                    "streamId": stream_id,
                    "toolCall": {
                        "id": data["id"],
                        "name": data["name"],
                        "arguments": data.get("arguments") or {},
                    },
                },
            )
        elif kind in ("tool_call_result", "tool_error"):
            payload: dict[str, Any] = {
                "id": data["id"],
                "name": data["name"],
                "success": data.get("success", False),
            }
            if "durationMs" in data:
                payload["durationMs"] = data["durationMs"]
            if "error" in data:
                payload["error"] = data["error"]
            if "resultPreview" in data:
                payload["resultPreview"] = data["resultPreview"]
            await transport.emit_notification(
                "stream.chunk", {"streamId": stream_id, "toolResult": payload}
            )
        elif kind in ("soft_timeout", "budget_exhausted"):
            await transport.emit_notification(
                "stream.chunk", {"streamId": stream_id, "notice": {"kind": kind, **data}}
            )
        elif kind == "hook_action":
            # Hook-driven control flow (compaction / retry / narration /
            # tool_choice). TraceRecorder already persists it; forward as a
            # notice so hosts can surface it live too.
            await transport.emit_notification(
                "stream.chunk",
                {"streamId": stream_id, "notice": {"kind": "hook_action", **data}},
            )
        elif kind == "steer":
            # The host already rendered the user's message; this confirms the
            # loop consumed it into the transcript (vs. still queued).
            await transport.emit_notification(
                "stream.chunk",
                {
                    "streamId": stream_id,
                    "notice": {"kind": "steer", "content": data.get("content", "")},
                },
            )
        elif kind == "error":
            await transport.emit_notification(
                "stream.error",
                {
                    "streamId": stream_id,
                    "kind": "LoopError",
                    "message": data["message"],
                    **({"traceId": trace_id} if trace_id else {}),
                },
            )
        elif kind == "completion" and data.get("status") != "executing":
            await transport.emit_notification(
                "stream.done",
                {
                    "streamId": stream_id,
                    "ok": data["status"] == "completed",
                    "status": data["status"],
                    "reason": data["reason"],
                    **({"traceId": trace_id} if trace_id else {}),
                },
            )

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _emit_ready_marker(self, health: SidecarHealth) -> None:
        if self.config.quiet_stderr:
            return
        payload = json.dumps(health.model_dump(exclude_none=True), separators=(",", ":"))
        sys.stderr.write(f"{READY_PREFIX}{payload}\n")
        sys.stderr.flush()

    def _configure_logging(self) -> None:
        logging.basicConfig(
            level=self.config.log_level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            stream=sys.stderr,
        )

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            import signal

            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda: self._shutdown_requested.set())
        except (NotImplementedError, RuntimeError):
            # Windows event-loop policies that lack add_signal_handler.
            pass

    @staticmethod
    async def _connect_stdio() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        transport, _ = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        return reader, writer

    @staticmethod
    async def _maybe_drain(writer: Any) -> None:
        drain = getattr(writer, "drain", None)
        if drain is None:
            return
        result = drain()
        if asyncio.iscoroutine(result):
            await result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_params(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise JsonRpcError("params must be an object", code=-32602, kind="invalid_params")
    return params


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex}"


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex}"


def _new_stream_id() -> str:
    return f"str_{uuid.uuid4().hex}"


def _coerce_messages(items: Any) -> list[LLMMessage]:
    if not isinstance(items, list):
        raise JsonRpcError("messages must be a list", code=-32602, kind="invalid_params")
    out: list[LLMMessage] = []
    for entry in items:
        if not isinstance(entry, dict):
            raise JsonRpcError(
                "each message must be an object", code=-32602, kind="invalid_params"
            )
        role = entry.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise JsonRpcError(
                f"invalid role: {role!r}", code=-32602, kind="invalid_params"
            )
        out.append(
            LLMMessage(
                role=role,  # type: ignore[arg-type]
                content=str(entry.get("content", "")),
                name=entry.get("name"),
                tool_call_id=entry.get("toolCallId"),
            )
        )
    return out


def _build_provider_kwargs(params: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if (tools := params.get("tools")) is not None:
        kwargs["tools"] = tools
    if (temp := params.get("temperature")) is not None:
        kwargs["temperature"] = float(temp)
    if (max_tokens := params.get("maxTokens")) is not None:
        kwargs["max_tokens"] = int(max_tokens)
    extra = params.get("providerOptions") or {}
    if isinstance(extra, dict):
        kwargs.update(extra)
    return kwargs


def _use_coreloop(params: dict[str, Any]) -> bool:
    """Flag resolution for the CoreLoop chat path: per-request
    ``useCoreLoop`` wins; otherwise the ``STEERABLE_SIDECAR_CORELOOP`` env
    var; default off (legacy direct-stream path)."""

    flag = params.get("useCoreLoop")
    if flag is not None:
        return bool(flag)
    return os.environ.get("STEERABLE_SIDECAR_CORELOOP", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _last_message_content(
    messages: list[LLMMessage], role: str, *, tail: int | None = None
) -> str:
    """Content of the last message with ``role`` (for anti-hallucination
    routing/judging context). ``tail`` truncates to the trailing N chars."""

    for message in reversed(messages):
        if message.role == role and message.content:
            content = message.content
            return content[-tail:] if tail else content
    return ""


def _default_loop_hooks(params: dict[str, Any]) -> LoopHooks:
    """Default hook chain for the CoreLoop chat path.

    CompactionHooks comes first: its ``on_request_error`` intercepts
    context-overflow failures and retries with a compacted transcript, and
    its ``pre_step`` keeps pressure under the threshold in the first place
    (parity with the desktop TS loop). Everything it declines falls through
    to RetryHooks' taxonomy-routed backoff.
    """
    from steerable_agent_runtime.tokens import resolve_context_window

    # Explicit maxContextTokens wins; otherwise the model's known context
    # window (a fixed 60k against a 131k model compacted far earlier than the
    # provider required — the dogfood 22-compacts/5-traces pathology).
    max_ctx = resolve_context_window(
        params.get("model"),
        explicit=int(params.get("maxContextTokens") or 0) or None,
    )
    return ChainHooks(
        CompactionHooks(max_context_tokens=max_ctx, model=params.get("model")),
        RetryHooks(),
    )


def _build_loop_config(params: dict[str, Any]) -> LoopConfig:
    budget = None
    if (budget_tokens := params.get("budgetTokens")) is not None:
        budget = BudgetLimit(
            max_tokens=int(budget_tokens),
            max_steps=int(params.get("maxRounds", 32)),
            max_tool_calls=int(params.get("budgetMaxToolCalls", 10_000)),
        )
    return LoopConfig(
        max_rounds=int(params.get("maxRounds", 32)),
        max_tool_errors=int(params.get("maxToolErrors", 3)),
        budget=budget,
        temperature=(
            float(params["temperature"]) if params.get("temperature") is not None else None
        ),
        max_tokens=int(params["maxTokens"]) if params.get("maxTokens") is not None else None,
        soft_timeout_ms=(
            int(params["softTimeoutMs"]) if params.get("softTimeoutMs") is not None else None
        ),
    )


_shared_calibration: Any | None = None
_shared_calibration_path: str | None = None


def _get_shared_calibration() -> Any:
    """Process-level UsageCalibration singleton.

    The provider factory runs per chat-stream request; a per-request
    calibration would accumulate samples only within one turn and rarely
    reach the persist threshold. A process-shared singleton accumulates
    across turns and is flushed periodically (persist_every) and on
    shutdown.
    """
    global _shared_calibration, _shared_calibration_path
    if _shared_calibration is None:
        from steerable_agent_runtime import UsageCalibration

        path = os.environ.get("STEERABLE_TOKEN_CALIBRATION_PATH") or os.path.join(
            os.path.expanduser("~"), ".steerable", "token-calibration.json"
        )
        _shared_calibration = UsageCalibration.load(path)
        _shared_calibration.register_factors()
        _shared_calibration_path = path
    return _shared_calibration


def _flush_shared_calibration() -> None:
    if _shared_calibration is not None and _shared_calibration_path is not None:
        try:
            _shared_calibration.save(_shared_calibration_path)
        except OSError:
            pass  # shutdown flush is best-effort; periodic flushes already ran


def _wrap_with_calibration(provider: LLMProvider) -> LLMProvider:
    """Wrap the provider so every request records estimated-vs-observed usage.

    Default-on: dogfooding should accumulate calibration samples with zero
    setup. Disable with ``STEERABLE_TOKEN_CALIBRATION=0``; override the
    aggregates file with ``STEERABLE_TOKEN_CALIBRATION_PATH`` (default
    ``~/.steerable/token-calibration.json``). Previously accumulated factors
    are registered into MODEL_TOKEN_FACTORS on load, so a restarted sidecar
    resumes with its measured corrections.
    """
    flag = os.environ.get("STEERABLE_TOKEN_CALIBRATION", "1").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return provider
    from steerable_agent_runtime import CalibratingProvider

    return CalibratingProvider(
        provider,
        _get_shared_calibration(),
        persist_path=_shared_calibration_path,
    )


def default_llm_provider_factory(params: dict[str, Any]) -> LLMProvider:
    """Construct an LLMProvider from a chat-stream request payload.

    Embedders can override this by passing ``llm_provider_factory=`` to
    ``Sidecar(...)`` — useful for tests or for sites that want to enforce a
    single configured provider.
    """

    provider_kind = (params.get("provider") or "").strip().lower()
    model = params.get("model")
    if not model:
        raise ValueError("model is required")
    base_url = params.get("baseUrl") or params.get("base_url")
    api_key = params.get("apiKey") or params.get("api_key") or ""

    if provider_kind in {"openai", "openai_compat", "openai-compatible", "ollama"}:
        from steerable_agent_runtime.llm import OpenAICompatProvider

        if provider_kind == "ollama":
            # Ollama's OpenAI-compatible API lives under /v1. Callers that
            # configure the native daemon root (e.g. the desktop app stores
            # http://127.0.0.1:11434 for its native /api/chat client) would
            # otherwise 404 on /chat/completions.
            base_url = (base_url or "http://127.0.0.1:11434").rstrip("/")
            if not base_url.endswith("/v1"):
                base_url = f"{base_url}/v1"

        return _wrap_with_calibration(
            OpenAICompatProvider(
                name=provider_kind or "openai_compat",
                base_url=base_url or "https://api.openai.com/v1",
                api_key=api_key,
                model=str(model),
            )
        )
    if provider_kind in {"anthropic", "claude"}:
        from steerable_agent_runtime.llm import AnthropicProvider

        return _wrap_with_calibration(
            AnthropicProvider(
                name=provider_kind or "anthropic", api_key=api_key, model=str(model)
            )
        )

    raise ValueError(f"unknown provider: {provider_kind!r}")
