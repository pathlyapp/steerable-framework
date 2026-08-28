"""Wire ``parts`` coercion (Wave 1): ChatMessage.parts → LLMMessage content.

The wire schema keeps ``content: string`` as the text projection and adds an
optional additive ``parts`` array; the sidecar prefers ``parts`` when present
so multimodal messages reach the runtime losslessly while text-only clients
are untouched.
"""

from __future__ import annotations

import pytest

from steerable_agent_runtime.llm import ImagePart, TextPart
from steerable_agent_runtime.transport.stdio_jsonrpc import JsonRpcError
from steerable_sidecar.sidecar import _coerce_messages


def test_text_only_message_uses_content_string() -> None:
    [msg] = _coerce_messages([{"role": "user", "content": "hello"}])
    assert msg.content == [TextPart("hello")]
    assert msg.content_text == "hello"


def test_parts_are_authoritative_when_present() -> None:
    [msg] = _coerce_messages(
        [
            {
                "role": "user",
                "content": "look at this",  # projection — ignored
                "parts": [
                    {"type": "text", "text": "look at this"},
                    {"type": "image", "url": "https://x/y.png"},
                ],
            }
        ]
    )
    assert msg.content == [
        TextPart("look at this"),
        ImagePart.from_url("https://x/y.png"),
    ]


def test_image_part_with_inline_data() -> None:
    [msg] = _coerce_messages(
        [
            {
                "role": "user",
                "content": "",
                "parts": [
                    {"type": "image", "data": "QUJD", "mediaType": "image/jpeg"}
                ],
            }
        ]
    )
    assert msg.content == [ImagePart.from_base64("QUJD", media_type="image/jpeg")]


def test_parts_preserve_envelope_fields() -> None:
    [msg] = _coerce_messages(
        [
            {
                "role": "tool",
                "content": "42",
                "name": "calc",
                "toolCallId": "call_1",
                "parts": [{"type": "text", "text": "42"}],
            }
        ]
    )
    assert msg.name == "calc"
    assert msg.tool_call_id == "call_1"
    assert msg.content == [TextPart("42")]


def test_invalid_part_type_rejected() -> None:
    with pytest.raises(JsonRpcError):
        _coerce_messages(
            [{"role": "user", "content": "", "parts": [{"type": "audio"}]}]
        )


def test_image_part_without_source_rejected() -> None:
    with pytest.raises(JsonRpcError):
        _coerce_messages(
            [{"role": "user", "content": "", "parts": [{"type": "image"}]}]
        )


def test_non_list_parts_rejected() -> None:
    with pytest.raises(JsonRpcError):
        _coerce_messages([{"role": "user", "content": "", "parts": "nope"}])
