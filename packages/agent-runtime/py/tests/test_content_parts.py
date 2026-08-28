"""Content parts (Wave 1): structured ``LLMMessage.content``.

Pins the text-only fast path (legacy string shorthand on the wire, so
existing conversations keep byte-identical bytes) and the multimodal block
form for both wire providers, plus the estimator / recording accounting.
"""

from __future__ import annotations

from steerable_agent_runtime import (
    ImagePart,
    InMemoryRequestSink,
    LLMMessage,
    LLMUsage,
    RecordingProvider,
    TextPart,
    assert_bounded_items,
    estimate_tokens,
    text_parts,
)
from steerable_agent_runtime.llm.anthropic_native import _split_system_and_messages
from steerable_agent_runtime.llm.openai_compat import _encode_message
from steerable_agent_runtime.tokens import (
    IMAGE_PART_TOKEN_ESTIMATE,
    estimate_text_tokens,
)


# ---------------------------------------------------------------------------
# Parts basics
# ---------------------------------------------------------------------------


def test_text_parts_round_trip() -> None:
    parts = text_parts("hello")
    assert parts == [TextPart("hello")]
    msg = LLMMessage(role="user", content=parts)
    assert msg.content_text == "hello"


def test_text_of_preserves_envelope_fields() -> None:
    msg = LLMMessage.text_of("tool", "result", name="search", tool_call_id="call_1")
    assert msg.role == "tool"
    assert msg.content_text == "result"
    assert msg.name == "search"
    assert msg.tool_call_id == "call_1"
    assert msg.tool_calls is None


def test_content_text_elides_images() -> None:
    msg = LLMMessage(
        role="user",
        content=[TextPart("look: "), ImagePart.from_url("https://x/y.png")],
    )
    assert msg.content_text == "look: "


# ---------------------------------------------------------------------------
# OpenAI-compatible wire shape
# ---------------------------------------------------------------------------


def test_openai_text_only_uses_string_shorthand() -> None:
    encoded = _encode_message(LLMMessage.text_of("user", "hi"))
    assert encoded == {"role": "user", "content": "hi"}


def test_openai_empty_content_stays_empty_string() -> None:
    # Legacy shape for tool-call-only assistant messages: content "".
    encoded = _encode_message(LLMMessage.text_of("assistant", ""))
    assert encoded["content"] == ""


def test_openai_image_url_switches_to_array_form() -> None:
    msg = LLMMessage(
        role="user",
        content=[TextPart("what is this?"), ImagePart.from_url("https://x/y.png")],
    )
    encoded = _encode_message(msg)
    assert encoded["content"] == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ]


def test_openai_image_base64_becomes_data_url() -> None:
    msg = LLMMessage(
        role="user",
        content=[ImagePart.from_base64("QUJD", media_type="image/jpeg")],
    )
    encoded = _encode_message(msg)
    assert encoded["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}
    ]


# ---------------------------------------------------------------------------
# Anthropic wire shape
# ---------------------------------------------------------------------------


def test_anthropic_text_only_stays_string() -> None:
    system, out = _split_system_and_messages(
        [LLMMessage.text_of("system", "rules"), LLMMessage.text_of("user", "hi")]
    )
    assert system == "rules"
    assert out == [{"role": "user", "content": "hi"}]


def test_anthropic_tool_result_uses_text_projection() -> None:
    _system, out = _split_system_and_messages(
        [LLMMessage.text_of("tool", "42", name="calc", tool_call_id="call_1")]
    )
    assert out == [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "42"}
            ],
        }
    ]


def test_anthropic_image_blocks() -> None:
    msg = LLMMessage(
        role="user",
        content=[
            TextPart("see"),
            ImagePart.from_url("https://x/y.png"),
            ImagePart.from_base64("QUJD", media_type="image/png"),
        ],
    )
    _system, out = _split_system_and_messages([msg])
    assert out == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "see"},
                {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "QUJD",
                    },
                },
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Estimator + recording accounting
# ---------------------------------------------------------------------------


def test_estimate_tokens_text_only_unchanged() -> None:
    msg = LLMMessage.text_of("user", "hello world")
    assert estimate_tokens([msg]) == estimate_tokens(
        [LLMMessage(role="user", content=text_parts("hello world"))]
    )


def test_estimate_tokens_image_adds_flat_estimate() -> None:
    text_only = estimate_tokens([LLMMessage.text_of("user", "hi")])
    with_image = estimate_tokens(
        [
            LLMMessage(
                role="user",
                content=[TextPart("hi"), ImagePart.from_url("https://x/y.png")],
            )
        ]
    )
    assert with_image - text_only == IMAGE_PART_TOKEN_ESTIMATE


def test_recording_text_only_content_stays_string() -> None:
    from steerable_agent_runtime.recording import _message_to_dict

    recorded = _message_to_dict(LLMMessage.text_of("user", "hello"))
    assert recorded["content"] == "hello"


def test_recording_multimodal_content_is_part_dicts() -> None:
    from steerable_agent_runtime.recording import _message_to_dict

    recorded = _message_to_dict(
        LLMMessage(
            role="user",
            content=[TextPart("hi"), ImagePart.from_url("https://x/y.png")],
        )
    )
    assert recorded["content"] == [
        {"text": "hi", "type": "text"},
        {
            "source": "https://x/y.png",
            "is_url": True,
            "media_type": "image/png",
            "type": "image",
        },
    ]


def test_bounded_items_counts_image_parts() -> None:
    from steerable_agent_runtime.recording import RecordedRequest

    req = RecordedRequest(
        seq=1,
        kind="stream",
        provider="fake",
        model="fake",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image", "source": "https://x/y.png"},
                ],
            }
        ],
    )
    # text ("hi" ~1 token) + one image (flat estimate) stays under the cap…
    assert_bounded_items([req], max_item_tokens=IMAGE_PART_TOKEN_ESTIMATE + 100)
    # …and blows past a cap below the image estimate.
    try:
        assert_bounded_items([req], max_item_tokens=100)
    except AssertionError as exc:
        assert "unbounded item" in str(exc)
    else:  # pragma: no cover - the assertion above must fire
        raise AssertionError("expected assert_bounded_items to reject the image")


def test_estimate_text_tokens_still_drives_text_only() -> None:
    msg = LLMMessage.text_of("user", "hello world")
    # message overhead (8) + text estimate; no image surcharge
    assert estimate_tokens([msg]) == 8 + estimate_text_tokens("hello world")
