"""ACP transport: serve the CoreLoop as an Agent Client Protocol agent.

ACP (JSON-RPC over stdio, editor↔agent) is precisely the sidecar's
transport and problem statement — this adapter is the peer that lets any
ACP client (Zed, JetBrains, …) drive a Steerable loop instead of the
bespoke 15-method surface. It implements the stable core of ``acp.Agent``:
``initialize`` / ``new_session`` / ``prompt`` / ``cancel`` /
``close_session``. Session loading, forking, and mode/config RPCs are
deliberately unimplemented (the SDK's default ``None`` answers advertise
that). Headless / Harbor evals use in-process ``bash`` / ``read_file`` /
``write_file`` scoped to the session cwd (see ``workspace_tools``). Editor
fs/terminal client bridges remain a follow-up for IDE embeddings.

Multi-turn is the loop's own record-aware seeding: each session is a
``chat_id`` whose durable record lives in the storage adapter, so a
``prompt`` carries only the new user message and the loop reconciles it
against the recorded history.

Event mapping (LoopEvent → ``session/update``):

- ``content_delta``   → ``AgentMessageChunk`` (text)
- ``reasoning_delta`` → ``AgentThoughtChunk`` (text)
- ``tool_call_start`` → ``ToolCallStart`` (title=name, raw_input=arguments)
- ``tool_call_result``/``tool_error`` → ``ToolCallProgress``
  (status completed/failed, raw_output=preview)
- ``completion``      → ends the prompt RPC: ``end_turn`` normally,
  ``cancelled`` after ``session/cancel``; a failed completion first
  surfaces its reason as a final agent message so the user sees it.
- everything else (stage/hook/steer/budget) is framework observability,
  not UI content — not forwarded.

Tool gating: the loop runs through ``ApprovalExecutor`` + ``AcpApprover``,
so every non-``read`` tool call asks the editor via
``session/request_permission`` (allow/always/reject/always-reject map onto
the framework's 8-variant algebra; ``read`` calls auto-approve). A
dismissed prompt or unreachable client fails closed as ``deny_once``.

Run over stdio with ``python -m steerable_sidecar.acp_adapter``. Provider
config comes from the environment (the editor spawns the agent):
``STEERABLE_PROVIDER`` / ``STEERABLE_MODEL`` / ``STEERABLE_BASE_URL`` /
``STEERABLE_API_KEY`` (falling back to ``OPENAI_API_KEY``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import acp
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
)
from steerable_agent_runtime import (
    ApprovalDecision,
    ApprovalExecutor,
    ApprovalRequest,
    CoreLoop,
    LoopConfig,
    LoopEvent,
    RouterToolExecutor,
    SessionApprovalCache,
    ToolExecutor,
    ToolRouter,
)
from steerable_agent_runtime.approval import ApprovalKind
from steerable_agent_runtime.llm import LLMMessage
from steerable_agent_runtime.storage import InMemoryStorage

from .workspace_tools import workspace_tools_for_cwd

logger = logging.getLogger(__name__)

__all__ = ["SteerableAcpAgent", "main"]


@dataclass(slots=True)
class _Session:
    cwd: str
    task: asyncio.Task[None] | None = None
    stop_reason: str = "end_turn"
    history: list[LLMMessage] = field(default_factory=list)
    approvals: SessionApprovalCache = field(default_factory=SessionApprovalCache)
    #: MCP servers the client asked to mount (W3.4.2.1); stdio-only today.
    mcp_servers: list[Any] = field(default_factory=list)
    #: Session mode (W3.4.2.3): "default" runs the full surface;
    #: "read-only" denies every non-read tool call before approval.
    mode: str = "default"
    #: Session-scoped provider overrides (W3.4.2.2): merged over the
    #: environment-derived params at prompt time.
    config_overrides: dict[str, Any] = field(default_factory=dict)


#: Session modes served over set_session_mode. "plan mode" is a framework
#: gap (no plan concept in the runtime) — what exists and is enforceable
#: today is the read-only gate, so that is what we advertise.
_SESSION_MODES: tuple[tuple[str, str], ...] = (
    ("default", "Full tool surface"),
    ("read-only", "Only read-mode tools execute; writes are denied"),
)

#: Session-scoped config keys accepted by set_config_option (W3.4.2.2).
_SESSION_CONFIG_KEYS = frozenset({"provider", "model", "baseUrl"})


class _ReadOnlyExecutor:
    """Session-mode gate (W3.4.2.3): deny every non-read tool call.

    Sits INSIDE the approval wrap — a read-only session denies outright
    instead of prompting for a write the mode forbids.
    """

    def __init__(self, inner: Any, router: ToolRouter) -> None:
        self._inner = inner
        self._modes = {t.name: t.mode for t in router.list_tools()}

    async def execute(self, call: ToolCall, ctx: Any) -> ToolResult:
        if self._modes.get(call.name) != "read":
            return ToolResult(
                success=False,
                error=(
                    f"session mode is read-only: {call.name} is a "
                    f"{self._modes.get(call.name, 'unknown')} tool"
                ),
                needsFollowup=True,
            )
        return await self._inner.execute(call, ctx)


#: ACP's four permission options mapped onto the framework's approval
#: algebra. ACP has no session-scope variants; an ``*_always`` verdict lands
#: in the session cache (no durable store on this path — the
#: ApprovalExecutor degrades it loudly, once per category).
_PERMISSION_OPTIONS: tuple[tuple[str, str, str, ApprovalKind], ...] = (
    ("allow-once", "Allow once", "allow_once", "allow_once"),
    ("allow-always", "Always allow", "allow_always", "allow_always"),
    ("reject-once", "Reject", "reject_once", "deny_once"),
    ("reject-always", "Always reject", "reject_always", "deny_always"),
)


class AcpApprover:
    """``Approver`` over the ACP client channel: the editor answers
    ``session/request_permission``.

    ``read``-mode calls auto-approve without prompting — editors do not gate
    reads, and prompting per read would drown the session. Everything else
    (writes, shell, destructive) asks. Fails closed: a dismissed prompt, an
    unknown option, or an unreachable client becomes ``deny_once`` — never a
    hang, never an auto-allow.
    """

    def __init__(self, conn: acp.Client, session_id: str) -> None:
        self._conn = conn
        self._session_id = session_id

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        if request.mode == "read":
            return ApprovalDecision("allow_once")
        try:
            response = await self._conn.request_permission(
                self._session_id,
                ToolCallUpdate(
                    tool_call_id=request.call_id or request.category,
                    title=request.tool_name,
                    raw_input=request.arguments,
                ),
                options=[
                    PermissionOption(option_id=option_id, name=name, kind=kind)
                    for option_id, name, kind, _ in _PERMISSION_OPTIONS
                ],
            )
        except Exception as exc:  # noqa: BLE001 — fail closed
            logger.warning("ACP permission request failed: %s", exc)
            return ApprovalDecision(
                "deny_once", f"ACP permission request failed: {exc}"
            )
        outcome = response.outcome
        if isinstance(outcome, AllowedOutcome):
            kinds = {oid: mapped for oid, _, _, mapped in _PERMISSION_OPTIONS}
            kind = kinds.get(outcome.option_id)
            if kind is not None:
                return ApprovalDecision(kind)
            logger.warning("client selected unknown option %r", outcome.option_id)
        return ApprovalDecision("deny_once", "permission request dismissed")


def _env_provider_params() -> dict[str, Any]:
    """Provider config from the environment the editor spawned us with.

    W5.3.1: when the generic keys are absent, the catalog's per-provider
    ``env`` names are honored (``provider=deepseek`` reads
    ``DEEPSEEK_API_KEY``) so a first-party deployment needs no shim vars.
    """
    provider = os.environ.get("STEERABLE_PROVIDER", "openai_compat")
    api_key = (
        os.environ.get("STEERABLE_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    if not api_key:
        from steerable_agent_runtime.model_resolve import provider_endpoint

        endpoint = provider_endpoint(provider)
        if endpoint:
            for var in endpoint.env_vars:
                api_key = os.environ.get(var)
                if api_key:
                    break
    return {
        "provider": provider,
        "model": os.environ.get("STEERABLE_MODEL", ""),
        "baseUrl": (
            os.environ.get("STEERABLE_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("ANTHROPIC_BASE_URL")
        ),
        "apiKey": api_key or "",
    }


def _default_loop_config() -> LoopConfig:
    """LoopConfig from the bundled default harness spec's ``loop:`` section.

    Falls back to the historical constants when the spec pins nothing —
    the spec declares what experiments vary; entrypoints keep owning the
    baseline.
    """
    from .sidecar import _DEFAULT_HARNESS_SPEC_PATH

    try:
        from steerable_agent_runtime.harness_spec import load_harness_spec

        limits = load_harness_spec(_DEFAULT_HARNESS_SPEC_PATH).loop
    except Exception:  # spec unreadable — baseline constants still apply
        limits = None
    return LoopConfig(
        max_rounds=(limits.max_rounds if limits else None) or 80,
        max_tool_errors=(limits.max_tool_errors if limits else None) or 16,
        tool_dedup=(
            limits.tool_dedup
            if limits is not None and limits.tool_dedup is not None
            else False
        ),
    )


class SteerableAcpAgent(acp.Agent):
    """``acp.Agent`` driving a CoreLoop per session."""

    def __init__(
        self,
        *,
        provider_params: dict[str, Any] | None = None,
        llm_provider_factory: Any | None = None,
        tools: ToolRouter | None = None,
        storage: InMemoryStorage | None = None,
    ) -> None:
        self._provider_params = provider_params or _env_provider_params()
        if llm_provider_factory is None:
            from .sidecar import default_llm_provider_factory

            llm_provider_factory = default_llm_provider_factory
        self._provider_factory = llm_provider_factory
        # None → per-session workspace tools (Harbor / headless). An explicit
        # empty ToolRouter is preserved for tests that inject their own set.
        self._tools = tools
        self._storage = storage or InMemoryStorage()
        self._conn: acp.Client | None = None
        self._sessions: dict[str, _Session] = {}

    # -- lifecycle ------------------------------------------------------

    def on_connect(self, conn: acp.Client) -> None:
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any | None = None,
        client_info: Any | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        from importlib.metadata import PackageNotFoundError, version

        try:
            pkg_version = version("steerable-sidecar")
        except PackageNotFoundError:
            pkg_version = "0.0.0"
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=False, audio=False, embedded_context=False
                ),
            ),
            agent_info=Implementation(
                name="steerable-sidecar",
                title="Steerable CoreLoop (ACP transport)",
                version=pkg_version,
            ),
        )

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> NewSessionResponse:
        from datetime import datetime, timezone

        from acp.schema import McpServerStdio
        from steerable_agent_protocol.generated import AgentSession

        session_id = uuid.uuid4().hex
        servers = list(mcp_servers or [])
        # Stdio-only today (W3.4.2.1): HTTP/SSE transports are an honest
        # gap — fail loud at session creation rather than silently dropping
        # the tools the client asked to mount.
        unsupported = [s for s in servers if not isinstance(s, McpServerStdio)]
        if unsupported:
            raise acp.RequestError(
                -32602,
                "only stdio MCP servers are supported; got "
                + ", ".join(type(s).__name__ for s in unsupported),
            )
        self._sessions[session_id] = _Session(cwd=cwd, mcp_servers=servers)
        # Persist a row so list_sessions (and a later load/resume after a
        # restart) can see this session before its first turn.
        now = datetime.now(timezone.utc).isoformat()
        await self._storage.upsert_session(
            AgentSession(
                sessionId=session_id,
                userId="acp",
                chatId=session_id,
                currentStage="plan",
                isActive=True,
                createdAt=now,
                updatedAt=now,
                scenario="acp",
                stageData={"cwd": cwd},
            )
        )
        from acp.schema import SessionMode, SessionModeState

        return NewSessionResponse(
            session_id=session_id,
            modes=SessionModeState(
                current_mode_id="default",
                available_modes=[
                    SessionMode(id=mode_id, name=name)
                    for mode_id, name in _SESSION_MODES
                ],
            ),
        )

    # -- session lifecycle (W3.4.1) --------------------------------------
    #
    # The adapter's session id IS the durable record id (loop.run uses it as
    # chat_id/record_id), so list/load/resume/fork are projections of the
    # storage protocol — no ACP-specific bookkeeping.

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> Any:
        from acp.schema import ListSessionsResponse, SessionInfo

        sessions = await self._storage.list_sessions()
        if cwd is not None:
            sessions = [s for s in sessions if self._session_cwd(s) == cwd]
        return ListSessionsResponse(
            sessions=[
                SessionInfo(
                    session_id=s.sessionId,
                    cwd=self._session_cwd(s) or "/",
                    title=s.scenario or None,
                    updated_at=self._session_updated_at(s),
                )
                for s in sessions
            ],
            next_cursor=None,
        )

    @staticmethod
    def _session_cwd(session: Any) -> str | None:
        stage_data = getattr(session, "stageData", None)
        if isinstance(stage_data, dict):
            cwd = stage_data.get("cwd")
            if isinstance(cwd, str):
                return cwd
        return None

    @staticmethod
    def _session_updated_at(session: Any) -> Any:
        updated = getattr(session, "updatedAt", None)
        if isinstance(updated, str):
            from datetime import datetime

            try:
                return datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    async def _hydrate_session(self, session_id: str, cwd: str) -> bool:
        """Register a live session whose host view comes from the record.

        The durable record (history entries), not chat messages, is what the
        loop writes — project it the same way ``agent.session.messages``
        does, then keep the conversational roles as the host view.
        """
        from steerable_agent_runtime.resume import load_history_items

        items = await load_history_items(self._storage, session_id)
        if items is None:
            return False
        history = [
            item.message
            for item in items
            if item.message.role in ("user", "assistant")
        ]
        self._sessions[session_id] = _Session(cwd=cwd, history=history)
        return True

    async def load_session(
        self, cwd: str, session_id: str, **kwargs: Any
    ) -> Any:
        from acp.schema import LoadSessionResponse

        if not await self._hydrate_session(session_id, cwd):
            raise acp.RequestError(-32602, f"unknown session: {session_id}")
        return LoadSessionResponse()

    async def resume_session(
        self, session_id: str, cwd: str, **kwargs: Any
    ) -> Any:
        from acp.schema import ResumeSessionResponse

        if not await self._hydrate_session(session_id, cwd):
            raise acp.RequestError(-32602, f"unknown session: {session_id}")
        return ResumeSessionResponse()

    async def fork_session(
        self, session_id: str, cwd: str, **kwargs: Any
    ) -> Any:
        """W3.4.1.2: branch-family fork on the standard protocol — the
        differentiator dsh's ACP explicitly does not have."""
        from acp.schema import ForkSessionResponse
        from steerable_agent_runtime.branch import fork_record

        try:
            fork = await fork_record(self._storage, session_id)
        except KeyError:
            raise acp.RequestError(-32602, f"unknown session: {session_id}") from None
        new_id = fork.point.record_id
        # fork.messages are LLMMessages already — the host view filters to
        # the conversational roles.
        history = [m for m in fork.messages if m.role in ("user", "assistant")]
        self._sessions[new_id] = _Session(cwd=cwd, history=history)
        from datetime import datetime, timezone

        from steerable_agent_protocol.generated import AgentSession

        now = datetime.now(timezone.utc).isoformat()
        await self._storage.upsert_session(
            AgentSession(
                sessionId=new_id,
                userId="acp",
                chatId=new_id,
                currentStage="plan",
                isActive=True,
                createdAt=now,
                updatedAt=now,
                scenario="acp",
                stageData={"cwd": cwd, "forkedFrom": session_id},
            )
        )
        return ForkSessionResponse(session_id=new_id)

    async def close_session(self, session_id: str, **kwargs: Any) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None and session.task is not None:
            session.task.cancel()

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> Any:
        from acp.schema import SetSessionModeResponse

        session = self._sessions.get(session_id)
        if session is None:
            raise acp.RequestError(-32602, f"unknown session: {session_id}")
        if mode_id not in {m for m, _ in _SESSION_MODES}:
            raise acp.RequestError(
                -32602,
                f"unknown mode {mode_id!r}; available: {[m for m, _ in _SESSION_MODES]}",
            )
        session.mode = mode_id
        return SetSessionModeResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: Any, **kwargs: Any
    ) -> Any:
        from acp.schema import SetSessionConfigOptionResponse

        session = self._sessions.get(session_id)
        if session is None:
            raise acp.RequestError(-32602, f"unknown session: {session_id}")
        if config_id not in _SESSION_CONFIG_KEYS:
            raise acp.RequestError(
                -32602,
                f"unknown config {config_id!r}; settable: {sorted(_SESSION_CONFIG_KEYS)}",
            )
        session.config_overrides[config_id] = value
        from acp.schema import SessionConfigSelect

        return SetSessionConfigOptionResponse(
            config_options=[
                SessionConfigSelect(
                    current_value=str(session.config_overrides.get(key, "")),
                    options=[],
                )
                for key in sorted(_SESSION_CONFIG_KEYS)
            ]
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        session = self._sessions.get(session_id)
        if session is not None and session.task is not None:
            session.stop_reason = "cancelled"
            session.task.cancel()

    # -- prompting ------------------------------------------------------

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> PromptResponse:
        session = self._sessions.get(session_id)
        if session is None:
            raise acp.RequestError(-32602, f"unknown session: {session_id}")
        if session.task is not None and not session.task.done():
            raise acp.RequestError(-32600, "session already has a prompt in flight")

        text = "".join(
            block.text for block in prompt if isinstance(block, TextContentBlock)
        )
        session.history.append(LLMMessage.text_of("user", text))
        session.stop_reason = "end_turn"

        # W3.4.2.2: session-scoped config overrides merge over the
        # environment-derived params (explicit session choice wins).
        provider = self._provider_factory(
            {**self._provider_params, **session.config_overrides}
        )
        router = (
            self._tools
            if self._tools is not None
            else workspace_tools_for_cwd(session.cwd)
        )
        mcp_clients = await self._mount_mcp(router, session)
        from .sidecar import _default_loop_hooks

        # The router's require_consent gate stays armed; with a client
        # attached, ApprovalExecutor routes every non-read call through
        # session/request_permission and bridges allow verdicts into the
        # router via ctx.consent_granted. Without a client there is nobody
        # to ask, so the armed gate alone fails closed on consented tools.
        executor: ToolExecutor = RouterToolExecutor(router)
        if session.mode == "read-only":
            executor = _ReadOnlyExecutor(executor, router)
        if self._conn is not None:
            executor = ApprovalExecutor(
                executor,
                AcpApprover(self._conn, session_id),
                session=session.approvals,
            )

        loop = CoreLoop(
            provider,
            executor,
            # W3.4.2.4: loop limits come from the bundled default harness
            # spec, not a hardcoded knob here — one declarative source.
            config=_default_loop_config(),
            hooks=_default_loop_hooks(self._provider_params),
            history_store=self._storage,
            record_id=session_id,
        )
        # The seed is the adapter-kept host view (user/assistant texts);
        # record-aware seeding reconciles it against the durable record so
        # the model sees the full history (tool rounds included) while the
        # record stays delta-only.
        events = loop.run(
            list(session.history),
            tools=router.describe_model(),
            chat_id=session_id,
        )
        assistant_text: list[str] = []
        session.task = asyncio.current_task()
        try:
            async for event in events:
                await self._forward(session_id, event)
                if event.kind == "content_delta":
                    assistant_text.append(str(event.data.get("delta", "")))
                if event.kind == "completion":
                    if event.data.get("status") == "failed":
                        await self._forward_text(
                            session_id,
                            f"\n\n[run failed: {event.data.get('reason', 'unknown')}]",
                        )
        except asyncio.CancelledError:
            session.stop_reason = "cancelled"
        finally:
            session.task = None
            # The router is per-prompt; its interactive shell sessions are
            # real processes that must not outlive the prompt that owns them.
            sessions = getattr(router, "shell_sessions", None)
            if sessions is not None:
                sessions.close_all()
            for client in mcp_clients:
                await client.aclose()
        text_out = "".join(assistant_text)
        if text_out:
            session.history.append(LLMMessage.text_of("assistant", text_out))
        return PromptResponse(stop_reason=session.stop_reason)

    async def _forward_text(self, session_id: str, text: str) -> None:
        if self._conn is None:
            return
        await self._conn.session_update(
            session_id,
            AgentMessageChunk(session_update="agent_message_chunk", content=TextContentBlock(type="text", text=text)),
        )

    async def _mount_mcp(self, router: ToolRouter, session: _Session) -> list[Any]:
        """W3.4.2.1: spawn the session's stdio MCP servers and register
        their catalogs on this prompt's router (qualified names)."""
        from steerable_agent_runtime.mcp import (
            McpStdioClient,
            mcp_invoker,
            register_mcp_catalog,
        )

        clients: list[Any] = []
        for server in session.mcp_servers:
            client = McpStdioClient(
                server.command,
                server.args,
                env={e.name: e.value for e in (server.env or [])},
            )
            try:
                await client.start()
                tools = await client.list_tools()
                register_mcp_catalog(
                    router,
                    server=server.name,
                    tools=tools,
                    invoker=mcp_invoker(client),
                )
            except Exception:
                await client.aclose()
                raise
            clients.append(client)
        return clients

    async def _forward(self, session_id: str, event: LoopEvent) -> None:
        conn = self._conn
        if conn is None:
            return
        kind = event.kind
        data = event.data
        if kind == "content_delta":
            await conn.session_update(
                session_id,
                AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=str(data.get("delta", ""))),
                ),
            )
        elif kind == "reasoning_delta":
            await conn.session_update(
                session_id,
                AgentThoughtChunk(
                    session_update="agent_thought_chunk",
                    content=TextContentBlock(type="text", text=str(data.get("delta", "")))
                ),
            )
        elif kind == "tool_call_start":
            await conn.session_update(
                session_id,
                ToolCallStart(
                    session_update="tool_call",
                    tool_call_id=str(data.get("id", "")),
                    title=str(data.get("name", "")),
                    raw_input=data.get("arguments") or {},
                ),
            )
        elif kind in ("tool_call_result", "tool_error"):
            await conn.session_update(
                session_id,
                ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=str(data.get("id", "")),
                    status="completed" if data.get("success") else "failed",
                    raw_output=str(
                        data.get("resultPreview") or data.get("error") or ""
                    ),
                ),
            )


def main() -> None:
    """Serve the ACP agent on stdio (how editors spawn agents)."""
    acp.run_agent(SteerableAcpAgent())


if __name__ == "__main__":  # pragma: no cover
    main()
