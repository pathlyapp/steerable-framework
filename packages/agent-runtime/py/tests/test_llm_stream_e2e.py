"""Full-stream tests for the Responses and Gemini providers over mock HTTP.

The wire helpers are covered by pure-function tests; these exercise the SSE
line loop itself (``data:`` framing, event dispatch, tool-call assembly,
terminal usage) through httpx.MockTransport — no network, real client code.
"""

from __future__ import annotations

import json

import httpx
import pytest

from steerable_agent_runtime import LLMMessage
from steerable_agent_runtime.llm.google_genai import GoogleGenAIProvider
from steerable_agent_runtime.llm.openai_responses import OpenAIResponsesProvider


def _sse(lines: list[dict]) -> bytes:
    return "".join(f"data: {json.dumps(line)}\n\n" for line in lines).encode()


@pytest.mark.asyncio
async def test_responses_stream_full_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.output_text.delta", "delta": "let me "},
        {"type": "response.output_text.delta", "delta": "check"},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "exec",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "delta": '{"cmd":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "delta": ' "ls"}',
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "total_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 3},
                },
            },
        },
    ]
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(200, content=_sse(events))

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw)
    )

    provider = OpenAIResponsesProvider(
        name="openai",
        model="gpt-5",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
    )
    chunks = [
        chunk
        async for chunk in provider.stream([LLMMessage.text_of("user", "hi")])
    ]

    text = "".join(c.content_delta or "" for c in chunks)
    assert text == "let me check"
    calls = [c.tool_call_delta for c in chunks if c.tool_call_delta is not None]
    assert len(calls) == 1
    assert calls[0].name == "exec"
    assert calls[0].arguments == {"cmd": "ls"}
    usages = [c.usage for c in chunks if c.usage is not None]
    assert usages[-1].total_tokens == 20
    assert usages[-1].cached_prompt_tokens == 3
    # The request went to the Responses endpoint with the item-based body.
    assert seen_bodies[0]["model"] == "gpt-5"
    assert seen_bodies[0]["store"] is False
    assert seen_bodies[0]["input"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_responses_stream_failure_event_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steerable_agent_runtime.llm.errors import LLMError

    events = [
        {
            "type": "response.failed",
            "response": {"error": {"message": "model overloaded"}},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(events))

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw)
    )

    provider = OpenAIResponsesProvider(
        name="openai", model="gpt-5", base_url="http://x/v1"
    )
    with pytest.raises(LLMError, match="model overloaded"):
        async for _ in provider.stream([LLMMessage.text_of("user", "hi")]):
            pass


@pytest.mark.asyncio
async def test_gemini_stream_full_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "thinking", "thought": True}],
                    }
                }
            ]
        },
        {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "answer"}]}}
            ]
        },
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "exec", "args": {"cmd": "ls"}}}
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 6,
                "totalTokenCount": 16,
                "cachedContentTokenCount": 2,
            },
        },
    ]
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=_sse(events))

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw)
    )

    provider = GoogleGenAIProvider(
        name="google", model="gemini-3-pro", api_key="gk"
    )
    chunks = [
        chunk
        async for chunk in provider.stream([LLMMessage.text_of("user", "hi")])
    ]

    reasoning = "".join(c.reasoning_delta or "" for c in chunks)
    text = "".join(c.content_delta or "" for c in chunks)
    assert reasoning == "thinking"
    assert text == "answer"
    calls = [c.tool_call_delta for c in chunks if c.tool_call_delta is not None]
    assert len(calls) == 1
    assert calls[0].name == "exec"
    finishes = [c.finish_reason for c in chunks if c.finish_reason]
    assert finishes == ["stop"]
    usages = [c.usage for c in chunks if c.usage is not None]
    assert usages[-1].cached_prompt_tokens == 2
    assert ":streamGenerateContent?alt=sse" in seen_urls[0]
    assert "gemini-3-pro" in seen_urls[0]


@pytest.mark.asyncio
async def test_gemini_complete_non_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "done"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 4,
            "candidatesTokenCount": 2,
            "totalTokenCount": 6,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert ":generateContent" in str(request.url)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw)
    )

    provider = GoogleGenAIProvider(name="google", model="gemini-3-pro")
    message, usage = await provider.complete([LLMMessage.text_of("user", "hi")])
    assert message.content_text == "done"
    assert usage.total_tokens == 6
