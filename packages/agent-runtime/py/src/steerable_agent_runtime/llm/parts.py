"""Content parts — the structured form of ``LLMMessage.content``.

Wave 1 (docs/roadmap.md "Wave 1 — the foundation"): message content is a
list of typed parts instead of a bare string, unblocking multimodal input,
structured output, and (Wave 2) per-block cache annotations.

Text-only messages stay the ergonomic common case: ``LLMMessage.text_of``
builds them, ``LLMMessage.content_text`` projects them back to a plain
string, and both wire providers serialize text-only content in the legacy
string shorthand — wire bytes for existing text conversations are
unchanged. Non-text parts switch the serialization to the provider's block
array form.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True, slots=True)
class TextPart:
    """A run of plain text."""

    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ImagePart:
    """An image, carried by URL or base64 data.

    Construct via ``from_url`` / ``from_base64`` so the two source kinds
    stay self-documenting at the callsite. ``media_type`` is the MIME type
    (``image/png``, ``image/jpeg``, …); it is required for base64 data and
    advisory for URLs.
    """

    source: str
    is_url: bool
    media_type: str = "image/png"
    type: Literal["image"] = "image"

    @classmethod
    def from_url(cls, url: str, *, media_type: str = "image/png") -> ImagePart:
        return cls(source=url, is_url=True, media_type=media_type)

    @classmethod
    def from_base64(cls, data: str, *, media_type: str = "image/png") -> ImagePart:
        return cls(source=data, is_url=False, media_type=media_type)


#: The content union. New part kinds (audio, structured output, cache
#: annotations) join here; consumers switch on ``part.type``.
ContentPart = Union[TextPart, ImagePart]


def text_parts(text: str) -> list[ContentPart]:
    """Wrap plain text as a single-part content list (the common case)."""
    return [TextPart(text)]


def content_text(parts: list[ContentPart]) -> str:
    """Plain-text projection: concatenated text parts, images elided.

    Used by the token estimator, compaction, and display previews — anywhere
    a text-only view of the content is the right semantic.
    """
    return "".join(part.text for part in parts if isinstance(part, TextPart))
