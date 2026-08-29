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
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
)
from steerable_agent_runtime import (
    CoreLoop,
    LoopConfig,
    LoopEvent,
    RouterToolExecutor,
    ToolRouter,
)
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


def _env_provider_params() -> dict[str, Any]:
    """Provider config from the environment the editor spawned us with."""
    return {
        "provider": os.environ.get("STEERABLE_PROVIDER", "openai_compat"),
        "model": os.environ.get("STEERABLE_MODEL", ""),
        "baseUrl": (
            os.environ.get("STEERABLE_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("ANTHROPIC_BASE_URL")
        ),
        "apiKey": (
            os.environ.get("STEERABLE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or ""
        ),
    }


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
                load_session=False,
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
        self, cwd: str, **kwargs: Any
    ) -> NewSessionResponse:
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = _Session(cwd=cwd)
        return NewSessionResponse(session_id=session_id)

    async def close_session(self, session_id: str, **kwargs: Any) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None and session.task is not None:
            session.task.cancel()

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

        provider = self._provider_factory(self._provider_params)
        router = (
            self._tools
            if self._tools is not None
            else workspace_tools_for_cwd(session.cwd)
        )
        loop = CoreLoop(
            provider,
            RouterToolExecutor(router, consent_granted=True),
            config=LoopConfig(
                max_rounds=80,
                max_tool_errors=16,
                tool_dedup=False,
            ),
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
