"""Sidecar core: wires the runtime adapters into a JSON-RPC server.

Methods (see spec/sidecar/README.md for the full catalog):

  system.ping              -> SidecarHealth
  system.shutdown          -> null
  system.shutdown_now      -> null
  agent.session.create     -> AgentSession
  agent.session.resume     -> AgentSession
  agent.session.list       -> AgentSession[]
  agent.session.fork       -> BranchPoint  (fork a record, no turn run)
  agent.session.branches   -> { lineage, children } (branch-family view)
  agent.chat.stream        -> { streamId } (chunks pushed via `stream.chunk`,
                                            terminator via `stream.done`)
  agent.chat.cancel        -> null         (best-effort cancel of an in-flight stream)
  tool.list                -> ToolDescriptor[]
  tool.invoke              -> ToolResult
  workspace.apply_edits    -> { content, diff, applied, matches }  (pure edit
                                algorithm on supplied content; the host owns
                                all file I/O — W6-1 single source of truth)
  skills.list              -> { skills }  (parse + select SKILL.md from host
                                roots; single parse source so the desktop no
                                longer re-parses — eager/catalog both returned)
  trace.fetch              -> { trace, spans, events }
  trace.export             -> { status, traceId, privacyMode }  (OTLP/HTTP push, W6-6)
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
from collections import OrderedDict
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
    ApprovalExecutor,
    AutoApprover,
    BudgetExhaustedError,
    ChainHooks,
    CompactionHooks,
    CoreLoop,
    DEFAULT_SHELL_TOOLS,
    FilesystemSkillProvider,
    HistorySeed,
    JsonApprovalStore,
    LoopConfig,
    LoopHooks,
    PolicyDeniedError,
    RetryHooks,
    RouterToolExecutor,
    SandboxedToolExecutor,
    SessionApprovalCache,
    SkillDefinition,
    SkillExecutor,
    SkillHooks,
    StaticWorldStateSection,
    StorageError,
    SubagentConfig,
    SubagentExecutor,
    ToolDispatchError,
    ToolRouter,
    TraceRecorder,
    WorldStateHooks,
    estimate_cost_usd,
    export_trace,
    branch_label,
    entry_from_dict,
    fork_record,
    lineage,
    resolve_fork_seq,
    select_catalog,
    select_skills,
    skill_to_dict,
    skill_tool_descriptor,
    subagent_tool_descriptor,
)
from steerable_agent_runtime.llm import (
    ContentPart,
    ImagePart,
    LLMMessage,
    LLMProvider,
    TextPart,
)
from steerable_agent_runtime.resume import project_transcript
from steerable_agent_runtime.storage import InMemoryStorage, StorageAdapter
from steerable_agent_runtime.transport.stdio_jsonrpc import (
    JsonRpcError,
    JsonRpcServer,
    StdioJsonRpcTransport,
    encode_frame,
)

from .file_edit import EditError, EditOp, apply_edits
from .host_tools import HostApprover, HostToolExecutor
from .sandbox import select_exec_backend

logger = logging.getLogger("steerable_sidecar")

PROTOCOL_VERSION = "0.1.0"
SIDECAR_VERSION = "0.1.0"

# asyncio's default 64 KiB StreamReader limit kills the read loop with
# LimitOverrunError the moment a single JSON-RPC frame exceeds it — a large
# reverse-channel tool result (e.g. a file read) or a long conversation's
# chat.stream request crosses it easily, and the turn then dies silently
# (host sees a hang, no trace). 16 MiB stays bounded while covering
# realistic frames.
STDIO_STREAM_LIMIT = 16 * 1024 * 1024
READY_PREFIX = "__SIDECAR_READY__:"

#: Bound on per-chat session approval caches held by one sidecar. Eviction
#: only re-asks a cached *_for_session decision once; decisions are cheap.
_APPROVAL_SESSION_CACHE_CAP = 64


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
        #: Session-scope approval caches per chat (Wave 3 approval algebra).
        #: LRU-bounded so a long-lived sidecar hosting many chats doesn't
        #: grow without limit; eviction only means a cached *_for_session
        #: decision is re-asked once.
        self._approval_sessions: OrderedDict[str, SessionApprovalCache] = OrderedDict()
        self._transport: StdioJsonRpcTransport | None = None
        self._started_ms = int(time.monotonic() * 1000)
        self._wall_started_ms = int(time.time() * 1000)
        self._shutdown_requested = asyncio.Event()
        self._serving = False

        self._register_default_methods()
        for tool in self.config.initial_tools:
            self.tools.register(tool)

    def _approval_session(self, chat_id: Any) -> SessionApprovalCache:
        """Session-scope approval cache for one chat (LRU-bounded)."""
        key = str(chat_id) if chat_id else ""
        cache = self._approval_sessions.get(key)
        if cache is None:
            cache = SessionApprovalCache()
            self._approval_sessions[key] = cache
            while len(self._approval_sessions) > _APPROVAL_SESSION_CACHE_CAP:
                self._approval_sessions.popitem(last=False)
        else:
            self._approval_sessions.move_to_end(key)
        return cache

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
        register("workspace.apply_edits", self._handle_workspace_apply_edits)
        register("skills.list", self._handle_skills_list)
        register("trace.fetch", self._handle_trace_fetch)
        register("trace.export", self._handle_trace_export)
        register("config.get", self._handle_config_get)
        register("config.set", self._handle_config_set)
        register("agent.chat.stream", self._handle_chat_stream)
        register("agent.chat.cancel", self._handle_chat_cancel)
        register("agent.chat.steer", self._handle_chat_steer)
        register("agent.chat.fork", self._handle_chat_fork)
        register("agent.session.fork", self._handle_session_fork)
        register("agent.session.branches", self._handle_session_branches)

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

    async def _handle_workspace_apply_edits(
        self, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Pure structured-edit algorithm on caller-supplied content.

        The host (desktop) owns file read / version check / atomic write; this
        is only the locate-and-replace surgery so the algorithm has a single
        Python source of truth shared with the headless / ACP workspace tools.
        """
        params = _require_params(params)
        content = params.get("content")
        if not isinstance(content, str):
            raise JsonRpcError(
                "workspace.apply_edits: `content` (string) is required",
                code=-32602,
                kind="invalid_params",
            )
        raw_edits = params.get("edits")
        if not isinstance(raw_edits, list):
            raise JsonRpcError(
                "workspace.apply_edits: `edits` (array) is required",
                code=-32602,
                kind="invalid_params",
            )
        ops = [
            EditOp(old_text=str(e.get("oldText", "")), new_text=str(e.get("newText", "")))
            for e in raw_edits
            if isinstance(e, dict)
        ]
        file_path = str(params.get("filePath") or "file")
        try:
            result = apply_edits(content, ops, file_path=file_path)
        except EditError as exc:
            raise JsonRpcError(
                str(exc), code=-32030, kind="edit_failed", data={"code": exc.code}
            ) from exc
        return {
            "content": result.content,
            "diff": result.diff,
            "applied": len(result.matches),
            "matches": [
                {
                    "level": m.level,
                    "startLine": m.start_line,
                    "oldLineCount": m.old_line_count,
                }
                for m in result.matches
            ],
        }

    async def _handle_skills_list(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Parse + select skills from host-supplied roots (single source of
        truth for SKILL.md parsing, so the desktop no longer re-parses).

        Roots are host-local paths — the sidecar shares the filesystem with
        the desktop host. Returns both layers (eager + catalog) with bodies;
        the host applies its own layer filter / budget / name lookup."""
        params = _require_params(params)
        roots_raw = params.get("roots")
        if not isinstance(roots_raw, list):
            raise JsonRpcError(
                "skills.list: `roots` (array of paths) is required",
                code=-32602,
                kind="invalid_params",
            )
        roots = [str(r) for r in roots_raw]
        conditions = set(params.get("conditions") or [])
        exclude = list(params.get("exclude") or [])
        ignore_conditions = bool(params.get("ignoreConditions"))
        provider = FilesystemSkillProvider(roots)
        definitions = [d for d in provider.list() if isinstance(d, SkillDefinition)]
        selected = select_skills(definitions, conditions, exclude, ignore_conditions)
        return {"skills": [skill_to_dict(d) for d in selected]}

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

    async def _handle_trace_export(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Export a stored trace to an OTLP/HTTP collector (W6-6).

        Params: ``traceId`` (required), ``endpoint`` (required, the collector's
        ``/v1/traces`` URL), ``privacyMode`` (``"full"`` | ``"metadata"``,
        default ``"metadata"``), ``serviceName`` (optional). The payload is
        secret-redacted regardless of mode; ``metadata`` additionally strips
        event payload bodies and free-form span attributes.
        """
        params = _require_params(params)
        trace_id = params.get("traceId")
        endpoint = params.get("endpoint")
        if not trace_id:
            raise JsonRpcError("traceId required", code=-32602, kind="invalid_params")
        if not endpoint:
            raise JsonRpcError("endpoint required", code=-32602, kind="invalid_params")
        privacy_mode = params.get("privacyMode", "metadata")
        if privacy_mode not in ("full", "metadata"):
            raise JsonRpcError(
                f"invalid privacyMode: {privacy_mode}", code=-32602, kind="invalid_params"
            )
        trace = await self.storage.get_trace(trace_id)
        if trace is None:
            raise JsonRpcError(
                f"trace not found: {trace_id}", code=-32004, kind="invalid_request"
            )
        spans = await self.storage.list_spans(trace_id)
        events = await self.storage.list_events(trace_id)
        try:
            status = export_trace(
                trace,
                spans,
                events,
                str(endpoint),
                privacy_mode=privacy_mode,
                service_name=str(params.get("serviceName") or "steerable-agent"),
            )
        except Exception as exc:  # collector unreachable / non-2xx
            raise JsonRpcError(
                f"trace export failed: {exc}", code=-32603, kind="internal"
            ) from exc
        return {"status": status, "traceId": trace_id, "privacyMode": privacy_mode}

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

    async def _handle_chat_fork(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Start a new CoreLoop stream seeded from a recorded trace — the
        variant/regenerate primitive for trace-sourced sessions.

        Params: everything ``agent.chat.stream`` takes (provider/model are
        required), plus ONE fork source::

            {
              "recordId": "chat_...",       # Wave 1: fork the durable record
              "untilSeq": 41,               # optional inclusive record-seq bound
                                            # (see resume.load_history_transcript)
              # — or, legacy trace source —
              "traceId": "tr_...",          # the trace to fork from
              "untilSequence": 41,          # optional inclusive event-sequence
                                            # bound (see resume.project_transcript)
              "messages": [...],            # optional messages appended after
                                            # the projected seed (e.g. the
                                            # re-asked user turn)
            }

        A record fork runs under a fresh ``<recordId>:fork:<streamId>``
        record seeded by one provenance-carrying ``history.seed`` entry, so
        the variant never pollutes the source chat's log.

        Returns ``{"streamId", "seedMessages"}``. The fork always runs the
        CoreLoop path (projection is a CoreLoop concept) and records its own
        trace, so each variant is independently auditable. Hosts that keep
        their own message store (the desktop) don't need this RPC — they
        truncate their store and call ``agent.chat.stream`` with the rebuilt
        history.
        """
        params = _require_params(params)
        if self._transport is None:
            raise JsonRpcError(
                "transport not ready", code=-32099, kind="internal"
            )
        stream_id = params.get("streamId") or _new_stream_id()
        source_record = params.get("recordId")
        if source_record is not None:
            # Wave 1 record fork (Wave 5: via branch.fork_record): seed from
            # the durable record (optionally truncated at a record seq) into
            # a FRESH record id, so the variant never pollutes the source
            # chat's log. The seed entry is persisted up front with
            # provenance and per-message kinds (the loop's host-view
            # reconciliation needs them to keep forked records continuous);
            # the run's own continuous-log seeding then recognises the
            # prefix and only appends genuinely new items.
            until_seq = params.get("untilSeq")
            try:
                fork = await fork_record(
                    self.storage,
                    str(source_record),
                    until_seq=int(until_seq) if until_seq is not None else None,
                    new_record_id=f"{source_record}:fork:{stream_id}",
                )
            except KeyError:
                raise JsonRpcError(
                    f"record not found: {source_record}",
                    code=-32004,
                    kind="invalid_request",
                ) from None
            seed = list(fork.messages)
            params = {**params, "recordId": fork.point.record_id}
        else:
            trace_id = params.get("traceId")
            if not trace_id:
                raise JsonRpcError(
                    "traceId or recordId required", code=-32602, kind="invalid_params"
                )
            events = await self.storage.list_events(str(trace_id))
            if not events:
                raise JsonRpcError(
                    f"trace not found: {trace_id}", code=-32004, kind="invalid_request"
                )
            events.sort(key=lambda e: getattr(e, "sequence", 0))
            until = params.get("untilSequence")
            seed = project_transcript(
                events, until_sequence=int(until) if until is not None else None
            )
        seed.extend(_coerce_messages(params.get("messages") or []))

        try:
            provider = self._llm_provider_factory(params)
        except Exception as exc:
            raise JsonRpcError(
                f"failed to construct LLM provider: {exc}",
                code=-32602,
                kind="invalid_params",
            ) from exc

        transport = self._transport
        task = asyncio.create_task(
            self._run_chat_stream_coreloop(provider, seed, params, stream_id, transport)
        )
        self._streams[stream_id] = task
        return {"streamId": stream_id, "seedMessages": len(seed)}

    async def _handle_session_fork(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Fork a durable record WITHOUT running a turn (Wave 5).

        Params::

            {
              "recordId": "chat_...",        # the source record (required)
              "untilSeq": 41,                # exact inclusive fork point, or
              "beforeLastUser": true,        # regen addressing: fork at the
                                             # newest user item (the prompting
                                             # turn stays, the reply is dropped)
              "beforeUserIndex": 2,          # regen addressing by user-message
                                             # ordinal (mid-history regen)
              "newRecordId": "chat_...:r2",  # optional explicit branch id
              "label": "...",                # optional host-supplied summary
            }

        Returns the BranchPoint (``recordId``, ``sourceRecordId``,
        ``sourceUntilSeq``, ``label``) plus ``seedMessages`` (count). The
        host then runs turns on the branch by passing the returned
        ``recordId`` to ``agent.chat.stream``. The source record is never
        mutated — this is the non-destructive regenerate primitive: the old
        tail stays intact and discoverable via ``agent.session.branches``.
        """
        params = _require_params(params)
        source_record = params.get("recordId")
        if not source_record:
            raise JsonRpcError("recordId required", code=-32602, kind="invalid_params")
        until_seq = params.get("untilSeq")
        user_index = params.get("beforeUserIndex")
        if until_seq is None and user_index is not None:
            until_seq = await resolve_fork_seq(
                self.storage, str(source_record), user_index=int(user_index)
            )
            if until_seq is None:
                raise JsonRpcError(
                    f"user message index {user_index} not addressable in "
                    f"record: {source_record}",
                    code=-32004,
                    kind="invalid_request",
                )
        if until_seq is None and params.get("beforeLastUser"):
            until_seq = await resolve_fork_seq(
                self.storage, str(source_record), before_last_user=True
            )
            if until_seq is None:
                raise JsonRpcError(
                    f"no user message to fork before: {source_record}",
                    code=-32004,
                    kind="invalid_request",
                )
        try:
            fork = await fork_record(
                self.storage,
                str(source_record),
                until_seq=int(until_seq) if until_seq is not None else None,
                new_record_id=(
                    str(params["newRecordId"]) if params.get("newRecordId") else None
                ),
                label=str(params["label"]) if params.get("label") else None,
            )
        except KeyError:
            raise JsonRpcError(
                f"record not found: {source_record}",
                code=-32004,
                kind="invalid_request",
            ) from None
        point = fork.point
        return {
            "recordId": point.record_id,
            "sourceRecordId": point.source_record_id,
            "sourceUntilSeq": point.source_until_seq,
            "label": point.label,
            "seedMessages": len(fork.messages),
        }

    async def _handle_session_branches(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """Branch-family view of a record (Wave 5).

        Returns ``{"lineage": [...root-first BranchPoints...], "children":
        [...]}``. Lineage walks seed provenance upwards and always works;
        children discovery needs record enumeration — stores implementing
        the optional ``list_history_records`` extension get direct children
        (records whose seed names this one as source), others report an
        empty list (documented degradation, not an error).
        """
        params = _require_params(params)
        record_id = params.get("recordId")
        if not record_id:
            raise JsonRpcError("recordId required", code=-32602, kind="invalid_params")
        chain = await lineage(self.storage, str(record_id))
        children: list[dict[str, Any]] = []
        list_records = getattr(self.storage, "list_history_records", None)
        if callable(list_records):
            for candidate in await list_records():
                if candidate == record_id:
                    continue
                first = await self.storage.list_history(candidate, limit=1)
                if not first:
                    continue
                entry = entry_from_dict(first[0])
                if isinstance(entry, HistorySeed) and entry.source_record_id == record_id:
                    children.append(
                        {
                            "recordId": candidate,
                            "sourceRecordId": record_id,
                            "sourceUntilSeq": entry.source_until_seq,
                            "label": branch_label(list(entry.messages)),
                        }
                    )
        return {
            "lineage": [
                {
                    "recordId": point.record_id,
                    "sourceRecordId": point.source_record_id,
                    "sourceUntilSeq": point.source_until_seq,
                    "label": point.label,
                    "depth": point.depth,
                }
                for point in chain
            ],
            "children": children,
        }

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
            else _default_loop_hooks(params, summarizer=_summarizer_for(provider))
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
        # execSandbox: opt-in per-exec confinement for shell/subprocess
        # calls (Wave 3). The command is rewritten to run under the
        # platform's backend (Seatbelt on macOS, bwrap on Linux) and then
        # delegated — over toolsViaHost the confined command is spawned by
        # the host's shell, giving the desktop per-exec confinement without
        # the host learning sandbox mechanics. Wrapped inside the approval
        # layer so the approver reviews the ORIGINAL command, not the
        # sandboxed invocation. Absent → commands run unconfined (legacy
        # behavior); enabled with no available backend → enforcement "none"
        # (requireFull refuses instead).
        exec_sandbox = params.get("execSandbox")
        if isinstance(exec_sandbox, dict) and exec_sandbox.get("enabled"):
            backend = select_exec_backend(
                writable_roots=[
                    str(r) for r in exec_sandbox.get("writableRoots") or []
                ],
                network=bool(exec_sandbox.get("network")),
                allowed_hosts=exec_sandbox.get("allowedHosts") or None,
                shell=str(exec_sandbox.get("shell") or "/bin/sh"),
            )
            executor = SandboxedToolExecutor(
                executor,
                backend,
                shell_tools=(
                    exec_sandbox["tools"]
                    if exec_sandbox.get("tools") is not None
                    else DEFAULT_SHELL_TOOLS
                ),
                command_arg=str(exec_sandbox.get("commandArg") or "command"),
                require_full=bool(exec_sandbox.get("requireFull")),
            )
        # approval: opt-in approval algebra (Wave 3). ``{"mode": "auto"}`` is
        # the headless policy (safe modes auto-approve, the rest auto-deny —
        # a run never hangs on a prompt nobody answers); ``{"mode": "host"}``
        # asks the host UI over the reverse channel. Absent → no approval
        # layer, the router's require_consent gate alone (legacy behavior).
        # Wrapped innermost so a subagent's child-loop calls are gated too.
        approval = params.get("approval")
        if isinstance(approval, dict) and approval.get("mode") in ("auto", "host"):
            timeout_ms = approval.get("timeoutMs")
            store_path = approval.get("storePath")
            executor = ApprovalExecutor(
                executor,
                (
                    AutoApprover()
                    if approval["mode"] == "auto"
                    else HostApprover(self.server)
                ),
                session=self._approval_session(params.get("chatId")),
                store=JsonApprovalStore(store_path) if store_path else None,
                timeout_s=(float(timeout_ms) / 1000.0) if timeout_ms else None,
            )
        # subagent: opt-in delegation seam — advertise the tool and answer it
        # with a bounded child CoreLoop (depth-1 by construction). Products
        # that don't want delegation (the desktop today) simply don't pass it.
        # ``{"toolFilter": ["read_a", "read_b"]}`` narrows the child's tool
        # domain (W4-5): filtered-out calls fail closed with
        # tool_not_delegated, so a read-only research sub-agent cannot reach
        # the parent's write/shell tools by construction.
        tools = params.get("tools")
        if params.get("subagent"):
            subagent_opts = params.get("subagent")
            tool_filter = (
                frozenset(str(t) for t in subagent_opts.get("toolFilter"))
                if isinstance(subagent_opts, dict)
                and isinstance(subagent_opts.get("toolFilter"), list)
                else None
            )
            executor = SubagentExecutor(
                executor, provider, SubagentConfig(tool_filter=tool_filter)
            )
            tools = [*(tools or []), subagent_tool_descriptor()]
        # worldState: slow-changing host context (time, workspace, git
        # branch, …) as plain per-section data. The loop injects it once as
        # a <world-state> fragment; later turns diff against the snapshot
        # embedded in the last fragment — unchanged state costs zero tokens,
        # a change costs one small RFC 7386 tail patch. Hosts adopting this
        # stop rebuilding the system prompt per turn, keeping the cached
        # prefix byte-stable.
        world_state = params.get("worldState")
        if isinstance(world_state, dict) and world_state:
            hooks = ChainHooks(
                WorldStateHooks(
                    [
                        StaticWorldStateSection(str(key), value)
                        for key, value in world_state.items()
                    ]
                ),
                hooks,
            )
        # skills: layered disclosure. The host injects the eager layer into
        # the system prompt itself; the sidecar lists the catalog layer
        # (first-round pre_step injection, recorded as a hook_action event)
        # and answers `skill` tool calls with the full body. ``mode:
        # "eager"`` keeps everything host-side (compat for hosts that have
        # not adopted layered disclosure). Roots are host-local paths — the
        # sidecar shares the filesystem with the desktop host.
        skills_param = params.get("skills")
        if isinstance(skills_param, dict) and skills_param.get("mode", "layered") != "eager":
            roots = [str(r) for r in skills_param.get("roots") or []]
            conditions = set(skills_param.get("conditions") or [])
            exclude = list(skills_param.get("exclude") or [])
            ignore_conditions = bool(skills_param.get("ignoreConditions"))
            if roots:
                skill_provider = FilesystemSkillProvider(roots)
                if select_catalog(
                    skill_provider.list(), conditions, exclude, ignore_conditions
                ):
                    hooks = ChainHooks(
                        SkillHooks(
                            skill_provider,
                            conditions=conditions,
                            exclude=exclude,
                            ignore_conditions=ignore_conditions,
                        ),
                        hooks,
                    )
                    executor = SkillExecutor(
                        executor,
                        skill_provider,
                        conditions=conditions,
                        exclude=exclude,
                        ignore_conditions=ignore_conditions,
                    )
                    tools = [*(tools or []), skill_tool_descriptor()]
        loop = CoreLoop(
            provider,
            executor,
            _build_loop_config(params),
            hooks=hooks,
            # Wave 1 durable record: the continuous per-chat log. An
            # explicit recordId (the fork path's fresh log) wins over the
            # default chat_id-derived one.
            history_store=self.storage,
            record_id=params.get("recordId") or params.get("chatId"),
        )
        # Persist the run as it streams so the host can inspect it afterwards
        # via trace.fetch (and so a future resume projection has the events).
        recorder = TraceRecorder(self.storage, chat_id=params.get("chatId"))
        self._coreloops[stream_id] = loop
        try:
            async for event in recorder.tee(
                loop.run(
                    messages,
                    tools=tools,
                    chat_id=params.get("chatId"),
                )
            ):
                await self._emit_loop_event(
                    transport,
                    stream_id,
                    event,
                    recorder.trace_id,
                    model=params.get("model"),
                )
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
        model: str | None = None,
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
            if "sandbox" in data:
                # W4-2: per-exec sandbox marker for the host's tool card.
                payload["sandbox"] = data["sandbox"]
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
            # W6-9: forward the run's accumulated billable usage, plus a cost
            # estimate when the model is priced (None → key omitted, never a
            # fabricated $0.00 for unpriced/local models).
            done: dict[str, Any] = {
                "streamId": stream_id,
                "ok": data["status"] == "completed",
                "status": data["status"],
                "reason": data["reason"],
                **({"traceId": trace_id} if trace_id else {}),
            }
            usage = data.get("usage")
            if isinstance(usage, dict):
                done["usage"] = usage
                cost = estimate_cost_usd(
                    model,
                    int(usage.get("promptTokens") or 0),
                    int(usage.get("completionTokens") or 0),
                )
                if cost is not None:
                    done["usage"] = {**usage, "costUsd": cost}
            await transport.emit_notification("stream.done", done)

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
        reader = asyncio.StreamReader(limit=STDIO_STREAM_LIMIT)
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


def _coerce_part(item: Any) -> ContentPart:
    """Coerce one wire ``ContentPart`` (spec/chat/ContentPart.schema.json).

    The wire part is authoritative when ``parts`` is present on a
    ChatMessage; ``content`` is then just its text projection.
    """
    if not isinstance(item, dict):
        raise JsonRpcError("each part must be an object", code=-32602, kind="invalid_params")
    kind = item.get("type")
    if kind == "text":
        return TextPart(str(item.get("text") or ""))
    if kind == "image":
        url = item.get("url")
        if url:
            return ImagePart.from_url(
                str(url), media_type=str(item.get("mediaType") or "image/png")
            )
        data = item.get("data")
        if data:
            return ImagePart.from_base64(
                str(data), media_type=str(item.get("mediaType") or "image/png")
            )
        raise JsonRpcError(
            "image part needs url or data", code=-32602, kind="invalid_params"
        )
    raise JsonRpcError(f"invalid part type: {kind!r}", code=-32602, kind="invalid_params")


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
        wire_parts = entry.get("parts")
        if wire_parts is not None:
            if not isinstance(wire_parts, list):
                raise JsonRpcError(
                    "parts must be a list", code=-32602, kind="invalid_params"
                )
            out.append(
                LLMMessage(
                    role=role,  # type: ignore[arg-type]
                    content=[_coerce_part(p) for p in wire_parts],
                    name=entry.get("name"),
                    tool_call_id=entry.get("toolCallId"),
                )
            )
            continue
        out.append(
            LLMMessage.text_of(
                role,  # type: ignore[arg-type]
                str(entry.get("content", "")),
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
        if message.role == role and message.content_text:
            content = message.content_text
            return content[-tail:] if tail else content
    return ""


def _summarizer_for(provider: Any) -> Any | None:
    """Wire the turn's provider as the compaction summarizer.

    The desktop rolling summary made a genuine model call; the deterministic
    excerpt fallback would be a quality regression now that the framework is
    the sole owner of cross-turn compaction. Reusing the turn provider for the
    one-off ``complete`` mirrors how AntiHallucinationHooks already reuses it
    for the grounding judge. Opt out with ``STEERABLE_SIDECAR_SUMMARIZER=0``
    (cost-sensitive deployments keep the deterministic excerpts).
    """
    flag = os.environ.get("STEERABLE_SIDECAR_SUMMARIZER", "1").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return None
    return provider


def _default_loop_hooks(params: dict[str, Any], summarizer: Any | None = None) -> LoopHooks:
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
        CompactionHooks(
            max_context_tokens=max_ctx,
            model=params.get("model"),
            summarizer=summarizer,
        ),
        # Spill oversized tool results to disk instead of inlining them into
        # the transcript (W4-7: this hook existed since Wave 0 but was never
        # on the default chain — a single megabyte-sized shell output could
        # blow the context in one round). Opt out with
        # STEERABLE_SIDECAR_SPILL=0; override the spill directory with
        # STEERABLE_SPILL_DIR (default: a per-process temp dir).
        *_spill_hooks(),
        RetryHooks(),
    )


def _spill_hooks() -> list[LoopHooks]:
    flag = os.environ.get("STEERABLE_SIDECAR_SPILL", "1").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return []
    import tempfile

    from steerable_agent_runtime import FilesystemSpillStore, SpillHooks

    directory = os.environ.get("STEERABLE_SPILL_DIR") or os.path.join(
        tempfile.gettempdir(), "steerable-spill"
    )
    return [SpillHooks(FilesystemSpillStore(directory))]


def _build_loop_config(params: dict[str, Any]) -> LoopConfig:
    max_rounds = int(params.get("maxRounds", 32))
    budget = None
    if (budget_tokens := params.get("budgetTokens")) is not None:
        budget = BudgetLimit(
            max_tokens=int(budget_tokens),
            max_steps=max_rounds,
            max_tool_calls=int(params.get("budgetMaxToolCalls", 10_000)),
        )
    else:
        # Default cost guard (2026-08-26, production-calibrated). The desktop
        # TS loop had a fixed 60k token budget; the CoreLoop path launched
        # with none — a default-on regression. Production distribution over
        # 31k api traces: mean 145k total tokens/trace ≈ 1.1× deepseek's
        # 131k window, and the api's fixed 120k cap cut 6% of real tasks
        # (budget_exhausted terminal). Default to 2× the model's context
        # window: real tasks fit, runaway cost stays bounded. maxRounds is
        # the primary runaway guard; only the token axis of BudgetLimit is
        # consumed by the loop today (steps/tool_calls stay inert).
        from steerable_agent_runtime.tokens import resolve_context_window

        window = resolve_context_window(
            params.get("model"),
            explicit=int(params.get("maxContextTokens") or 0) or None,
        )
        budget = BudgetLimit(
            max_tokens=2 * window,
            max_steps=max_rounds,
            max_tool_calls=10_000,
        )
    return LoopConfig(
        max_rounds=max_rounds,
        max_tool_errors=int(params.get("maxToolErrors", 3)),
        budget=budget,
        temperature=(
            float(params["temperature"]) if params.get("temperature") is not None else None
        ),
        max_tokens=int(params["maxTokens"]) if params.get("maxTokens") is not None else None,
        soft_timeout_ms=(
            int(params["softTimeoutMs"]) if params.get("softTimeoutMs") is not None else None
        ),
        # Per-tool backstop against hung executors (in-process or remote).
        # LoopConfig carries the default; the param only overrides.
        **(
            {"tool_timeout_ms": int(params["toolTimeoutMs"])}
            if params.get("toolTimeoutMs") is not None
            else {}
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


def _wrap_with_recording(provider: LLMProvider) -> LLMProvider:
    """Wrap the provider so every outbound request lands in a JSONL record.

    Opt-in via ``STEERABLE_REQUEST_RECORD_PATH=<file.jsonl>`` — the E2E
    harness and dogfood runs set it to assert the prompt invariants
    (``assert_stable_prefix`` / ``assert_bounded_items``) on real traffic.
    Off by default: the record carries full prompt contents.
    """

    path = os.environ.get("STEERABLE_REQUEST_RECORD_PATH", "").strip()
    if not path:
        return provider
    from steerable_agent_runtime import JsonlRequestSink, RecordingProvider

    return RecordingProvider(provider, JsonlRequestSink(path))


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

        return _wrap_with_recording(
            _wrap_with_calibration(
                OpenAICompatProvider(
                    name=provider_kind or "openai_compat",
                    base_url=base_url or "https://api.openai.com/v1",
                    api_key=api_key,
                    model=str(model),
                )
            )
        )
    if provider_kind in {"anthropic", "claude"}:
        from steerable_agent_runtime.llm import AnthropicProvider

        return _wrap_with_recording(
            _wrap_with_calibration(
                _wrap_with_cache_control(
                    AnthropicProvider(
                        name=provider_kind or "anthropic", api_key=api_key, model=str(model)
                    )
                )
            )
        )

    raise ValueError(f"unknown provider: {provider_kind!r}")


def _wrap_with_cache_control(provider: LLMProvider) -> LLMProvider:
    """Emit prompt-cache breakpoints (Wave 4, W4-4) — default-on.

    Anthropic is the only provider with an explicit breakpoint API; for the
    implicit prefix caches (OpenAI-compatible, Ollama) the wrapper is a
    pass-through. ``STEERABLE_CACHE_CONTROL=0`` disables it (a debugging
    escape hatch, e.g. diffing wire bytes against a recorded fixture).
    """

    flag = os.environ.get("STEERABLE_CACHE_CONTROL", "1").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return provider
    from steerable_agent_runtime import CacheControlProvider

    return CacheControlProvider(provider)
