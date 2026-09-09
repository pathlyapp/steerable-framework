"""Wire-format tests for the Google Gemini native provider."""

from __future__ import annotations

import pytest

from steerable_agent_runtime import LLMMessage, LLMUsage
from steerable_agent_runtime.llm.google_genai import (
    GoogleGenAIProvider,
    _decode_response,
    _encode_contents,
    _parse_stream_chunk,
)


def _provider(**kwargs) -> GoogleGenAIProvider:
    return GoogleGenAIProvider(
        name="google", model="gemini-3-pro", api_key="gk-test", **kwargs
    )


class TestBuildBody:
    def test_system_becomes_system_instruction(self) -> None:
        body = _provider()._build_body(
            messages=[
                LLMMessage.text_of("system", "be terse"),
                LLMMessage.text_of("user", "hi"),
            ],
            tools=None,
            temperature=None,
            max_tokens=None,
            extra={},
        )
        assert body["systemInstruction"] == {"parts": [{"text": "be terse"}]}
        assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]

    def test_assistant_maps_to_model_role(self) -> None:
        body = _provider()._build_body(
            messages=[LLMMessage.text_of("assistant", "sure")],
            tools=None,
            temperature=None,
            max_tokens=None,
            extra={},
        )
        assert body["contents"] == [{"role": "model", "parts": [{"text": "sure"}]}]

    def test_tool_round_trip_uses_function_parts(self) -> None:
        from steerable_agent_protocol.generated import ToolCall

        body = _provider()._build_body(
            messages=[
                LLMMessage.text_of(
                    "assistant",
                    "checking",
                    tool_calls=[ToolCall(id="exec", name="exec", arguments={"cmd": "ls"})],
                ),
                LLMMessage.text_of("tool", "file.txt", name="exec", tool_call_id="exec"),
            ],
            tools=None,
            temperature=None,
            max_tokens=None,
            extra={},
        )
        assert body["contents"] == [
            {
                "role": "model",
                "parts": [
                    {"text": "checking"},
                    {"functionCall": {"name": "exec", "args": {"cmd": "ls"}}},
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "exec",
                            "response": {"result": "file.txt"},
                        }
                    }
                ],
            },
        ]

    def test_tools_become_function_declarations(self) -> None:
        body = _provider()._build_body(
            messages=[LLMMessage.text_of("user", "hi")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "description": "run",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            temperature=None,
            max_tokens=None,
            extra={},
        )
        assert body["tools"] == [
            {
                "functionDeclarations": [
                    {
                        "name": "exec",
                        "description": "run",
                        "parameters": {"type": "object"},
                    }
                ]
            }
        ]

    def test_generation_config_carries_sampling(self) -> None:
        body = _provider()._build_body(
            messages=[LLMMessage.text_of("user", "hi")],
            tools=None,
            temperature=0.4,
            max_tokens=512,
            extra={},
        )
        assert body["generationConfig"]["temperature"] == 0.4
        assert body["generationConfig"]["maxOutputTokens"] == 512

    def test_extra_generation_config_passthrough(self) -> None:
        body = _provider()._build_body(
            messages=[LLMMessage.text_of("user", "hi")],
            tools=None,
            temperature=None,
            max_tokens=None,
            extra={"generationConfig": {"thinkingConfig": {"thinkingBudget": 1024}}},
        )
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 1024}


class TestParseStreamChunk:
    def test_text_delta(self) -> None:
        chunk = _parse_stream_chunk(
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "hel"}]}}]}
        )
        assert chunk is not None
        assert chunk.content_delta == "hel"

    def test_thought_part_maps_to_reasoning(self) -> None:
        chunk = _parse_stream_chunk(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "hmm", "thought": True}],
                        }
                    }
                ]
            }
        )
        assert chunk is not None
        assert chunk.reasoning_delta == "hmm"
        assert chunk.content_delta is None

    def test_function_call_arrives_whole(self) -> None:
        chunk = _parse_stream_chunk(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"functionCall": {"name": "exec", "args": {"cmd": "ls"}}}
                            ],
                        }
                    }
                ]
            }
        )
        assert chunk is not None
        assert chunk.tool_call_delta is not None
        assert chunk.tool_call_delta.name == "exec"
        assert chunk.tool_call_delta.arguments == {"cmd": "ls"}

    def test_finish_reason_mapping(self) -> None:
        chunk = _parse_stream_chunk(
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "x"}]},
                        "finishReason": "MAX_TOKENS",
                    }
                ]
            }
        )
        assert chunk is not None
        assert chunk.finish_reason == "length"

    def test_usage_metadata(self) -> None:
        chunk = _parse_stream_chunk(
            {
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 15,
                    "cachedContentTokenCount": 4,
                }
            }
        )
        assert chunk is not None
        assert chunk.usage == LLMUsage(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cached_prompt_tokens=4,
        )

    def test_empty_chunk_is_skipped(self) -> None:
        assert _parse_stream_chunk({}) is None


class TestDecodeResponse:
    def test_full_payload(self) -> None:
        message, usage = _decode_response(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "hello"},
                                {"functionCall": {"name": "exec", "args": {}}},
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 3,
                    "candidatesTokenCount": 7,
                    "totalTokenCount": 10,
                },
            }
        )
        assert message.content_text == "hello"
        assert (message.tool_calls or [])[0].name == "exec"
        assert usage.total_tokens == 10


class TestEncodeContents:
    def test_empty_transcript(self) -> None:
        system, contents = _encode_contents([])
        assert system == ""
        assert contents == []


@pytest.mark.asyncio
async def test_provider_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        GoogleGenAIProvider(name="x", model="m", base_url="")
