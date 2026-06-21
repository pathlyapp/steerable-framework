from __future__ import annotations

import asyncio
from typing import Any, Callable, Awaitable, Optional
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from steerable_agent_protocol.generated import SSEEvent
from steerable_agent_runtime import (
    ChatLoop,
    LoopConfig,
    EmitCtx,
)
from steerable_agent_runtime.transport import (
    FastAPISseTransport,
    sse_response,
)


class ChatStreamRequest(BaseModel):
    """Standard generic chat stream request model."""
    sessionId: str = Field(..., description="Unique identifier for the agent session.")
    query: str = Field(..., description="The user query or message content.")
    model: Optional[str] = Field(None, description="Optional override for the model.")
    temperature: Optional[float] = Field(None, description="Optional override for the temperature.")
    maxTokens: Optional[int] = Field(None, description="Optional override for max tokens.")
    parameters: Optional[dict[str, Any]] = Field(None, description="Optional additional parameters.")


def create_chats_router(
    get_loop_config: Callable[[ChatStreamRequest], Awaitable[LoopConfig]]
) -> APIRouter:
    """Creates a standard chats router for FastAPI.

    Args:
        get_loop_config: A callable that accepts a ChatStreamRequest and returns
            a LoopConfig asynchronously.
    """
    router = APIRouter()

    @router.post("/stream")
    async def chat_stream(request: ChatStreamRequest) -> Any:
        """The standard framework-compatible SSE chat endpoint."""
        transport = FastAPISseTransport()

        async def _run_chat_loop():
            try:
                # 1. Resolve configuration using the caller-provided dependency
                config = await get_loop_config(request)
                loop = ChatLoop(config=config)

                # 2. Wire up ChatLoop's internal emit to transport's emit so events stream out in real time
                async def on_emit(ctx: EmitCtx) -> Any:
                    if ctx.event:
                        await transport.emit(ctx.event)

                loop.on("emit", on_emit)

                # 3. Execute the loop end-to-end and consume its events
                async for _ in loop.run():
                    pass

            except Exception as e:
                # Emit error event back to client
                try:
                    await transport.emit(
                        SSEEvent(
                            type="error",
                            event="error",
                            payload={"message": str(e)},
                        )
                    )
                except Exception:
                    pass
            finally:
                # Safely close the SSE connection
                await transport.aclose()

        # Spawn the async task to execute the ChatLoop and pipe outputs to SSE
        asyncio.create_task(_run_chat_loop())

        # Return Starlette StreamingResponse mapped to the transport
        return await sse_response(transport)

    return router
