from __future__ import annotations

from collections.abc import AsyncIterator, Sequence, Iterable
import pytest
from fastapi.testclient import TestClient
from steerable_agent_runtime import LoopConfig, LLMMessage, LLMProvider, LLMStreamChunk, LLMUsage, ToolRouter
from steerable_agent_app import create_app, ChatStreamRequest


class DummyLLMProvider(LLMProvider):
    """A minimal mock provider for testing chat flow."""
    def __init__(self, name: str = "dummy", model: str = "dummy-model") -> None:
        self.name = name
        self.model = model

    async def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMStreamChunk]:
        # Yield one text chunk and done
        yield LLMStreamChunk(content_delta="Hello from dummy model!")
        yield LLMStreamChunk(finish_reason="stop", usage=LLMUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10))

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Iterable[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[LLMMessage, LLMUsage]:
        raise NotImplementedError()


@pytest.mark.asyncio
async def test_steerable_agent_app_stream():
    # 1. Define a dummy get_loop_config resolver
    async def get_loop_config(request: ChatStreamRequest) -> LoopConfig:
        provider = DummyLLMProvider()
        return LoopConfig(
            provider=provider,
            provider_kind="openai_compat",
            initial_messages=[LLMMessage(role="user", content=request.query)],
            session_id=request.sessionId,
            tool_router=ToolRouter(),
        )

    # 2. Assemble app using factory
    app = create_app(get_loop_config=get_loop_config)
    client = TestClient(app)

    # 3. Request stream endpoint
    payload = {
        "sessionId": "test_session_123",
        "query": "Hi, tell me a joke!"
    }
    response = client.post("/api/v2/chats/stream", json=payload)
    
    # 4. Verify SSE stream output
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    
    # Read response text to verify the events are present
    content = response.text
    assert "Hello from dummy model!" in content
    assert "done" in content
