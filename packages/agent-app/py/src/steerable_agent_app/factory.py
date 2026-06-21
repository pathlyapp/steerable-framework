from __future__ import annotations

from typing import Callable, Awaitable
from fastapi import FastAPI

from .router import create_chats_router, ChatStreamRequest
from steerable_agent_runtime import LoopConfig


def create_app(
    get_loop_config: Callable[[ChatStreamRequest], Awaitable[LoopConfig]],
    title: str = "Steerable Agent App",
    version: str = "0.2.0",
) -> FastAPI:
    """Assembles a standard Steerable Agent FastAPI application.

    Args:
        get_loop_config: A callable that accepts a ChatStreamRequest and returns
            a LoopConfig asynchronously.
        title: Title of the FastAPI application.
        version: Version of the FastAPI application.
    """
    app = FastAPI(title=title, version=version)
    
    # Include standard chats router
    chats_router = create_chats_router(get_loop_config)
    app.include_router(chats_router, prefix="/api/v2/chats", tags=["chats"])
    
    return app
