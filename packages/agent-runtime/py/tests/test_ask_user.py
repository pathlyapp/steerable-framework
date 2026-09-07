"""The ask_user tool: registration, blocking dispatch, answer injection."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from steerable_agent_protocol.generated import ToolCall

from steerable_agent_runtime import (
    ASK_USER_TOOL_NAME,
    CoreLoop,
    LLMMessage,
    RouterToolExecutor,
    ToolRouter,
    make_ask_user_tool,
)
from steerable_agent_runtime.llm import LLMStreamChunk, LLMUsage


def _msg(role: str, text: str) -> LLMMessage:
    return LLMMessage.text_of(role, text)  # type: ignore[arg-type]


def _provider(script: list[dict[str, Any]]):
    class _FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls: list[list[LLMMessage]] = []
            self._idx = 0

        async def complete(self, messages, *, tools=None, **kw):  # pragma: no cover
            raise NotImplementedError

        def stream(self, messages, *, tools=None, **kw):
            self.calls.append(list(messages))
            entry = script[min(self._idx, len(script) - 1)]
            self._idx += 1

            async def _gen():
                if entry.get("content"):
                    yield LLMStreamChunk(content_delta=entry["content"])
                for call in entry.get("tool_calls", []):
                    yield LLMStreamChunk(tool_call_delta=call)
                yield LLMStreamChunk(
                    finish_reason="tool_calls" if entry.get("tool_calls") else "stop",
                    usage=LLMUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
                )

            return _gen()

    return _FakeProvider()


def _register(router: ToolRouter, handler: Any) -> None:
    """Register the ask_user tool from the metadata the factory stamped on."""
    fn = make_ask_user_tool(handler)
    meta = fn.__steerable_tool_meta__
    router.register(
        fn,
        name=meta["name"],
        mode=meta["mode"],
        description=meta["description"],
        schema=meta["schema"],
        require_consent=meta["require_consent"],
        concurrency_safe=meta["concurrency_safe"],
        exposure=meta["exposure"],
    )


def test_tool_registers_with_expected_name_and_schema() -> None:
    router = ToolRouter()

    async def handler(intro: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
        return {}

    _register(router, handler)
    tool = router.get(ASK_USER_TOOL_NAME)
    assert tool is not None
    assert tool.concurrency_safe is False
    descriptors = router.describe_model()
    descriptor = next(d for d in descriptors if d["function"]["name"] == ASK_USER_TOOL_NAME)
    assert "questions" in descriptor["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_ask_user_blocks_and_injects_answers() -> None:
    """The loop pauses until the handler answers; the answers reach the
    model as the tool result."""
    router = ToolRouter()
    asked: list[dict[str, Any]] = []

    async def handler(intro: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
        asked.append({"intro": intro, "questions": questions})
        # Simulate the user taking a beat before answering — the loop must
        # still be waiting on dispatch, not racing ahead.
        await asyncio.sleep(0.01)
        return {"color": "blue"}

    _register(router, handler)

    provider = _provider(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="q1",
                        name=ASK_USER_TOOL_NAME,
                        arguments={
                            "intro": "Pick one",
                            "questions": [
                                {
                                    "id": "color",
                                    "text": "Which color?",
                                    "type": "select",
                                    "options": ["red", "blue"],
                                }
                            ],
                        },
                    )
                ]
            },
            {"content": "You chose blue."},
        ]
    )
    loop = CoreLoop(provider, RouterToolExecutor(router))
    events = [
        e
        async for e in loop.run(
            [_msg("user", "ask me my favorite color")],
            tools=router.describe_model(),
        )
    ]

    assert events[-1].kind == "completion"
    # The handler was invoked with the question set.
    assert asked[0]["intro"] == "Pick one"
    assert asked[0]["questions"][0]["id"] == "color"
    # The answers came back to the model as the tool result payload.
    tool_messages = [
        m
        for call in provider.calls
        for m in call
        if m.role == "tool" and m.name == ASK_USER_TOOL_NAME
    ]
    assert tool_messages, "ask_user result should reach the model"
    assert "blue" in tool_messages[0].content_text
