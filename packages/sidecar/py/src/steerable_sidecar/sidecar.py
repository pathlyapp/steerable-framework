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

from steerable_agent_protocol.generated import (
    AgentSession,
    SidecarHealth,
    ToolCall,
)
from steerable_agent_runtime import (
    BudgetExhaustedError,
    PolicyDeniedError,
    StorageError,
    ToolDispatchError,
    ToolRouter,
)
from steerable_agent_runtime.llm import LLMMessage, LLMProvider
from steerable_agent_runtime.storage import InMemoryStorage, StorageAdapter
from steerable_agent_runtime.transport.stdio_jsonrpc import (
    JsonRpcError,
    JsonRpcServer,
    StdioJsonRpcTransport,
    encode_frame,
)

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
    ) -> None:
        self.config = config or SidecarConfig()
        self.storage: StorageAdapter = storage or InMemoryStorage()
        self.tools: ToolRouter = tools or ToolRouter()
        self.server = JsonRpcServer()
        self._llm_provider_factory = llm_provider_factory or default_llm_provider_factory
        self._streams: dict[str, asyncio.Task[Any]] = {}
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
                response = await self.server.handle_frame(line.decode("utf-8"))
                if response is None:
                    continue
                writer.write(encode_frame(response))
                await self._maybe_drain(writer)
        finally:
            await transport.emit_notification(
                "lifecycle.shutdown",
                {"reason": "normal" if self._shutdown_requested.is_set() else "eof"},
            )
            await transport.aclose()
            close = getattr(writer, "close", None)
            if close is not None:
                close()
            self._serving = False

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
        # Schedule the actual stop so the response can be drained first.
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, lambda: self._shutdown_requested.set())
        return None

    async def _handle_shutdown_now(self, _params: dict[str, Any] | None) -> None:
        self._shutdown_requested.set()
        return None

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
        return None

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
        kwargs = _build_provider_kwargs(params)

        transport = self._transport
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
        return None

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

        return OpenAICompatProvider(
            base_url=base_url or "https://api.openai.com/v1",
            api_key=api_key,
            model=str(model),
        )
    if provider_kind in {"anthropic", "claude"}:
        from steerable_agent_runtime.llm import AnthropicProvider

        return AnthropicProvider(api_key=api_key, model=str(model))

    raise ValueError(f"unknown provider: {provider_kind!r}")
