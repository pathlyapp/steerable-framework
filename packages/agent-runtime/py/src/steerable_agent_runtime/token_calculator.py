"""Token calculator helper using tiktoken with fallback estimation."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def count_tokens(text: str, model_id: str = "gpt-4", *, raw: bool = False, multiplier: float = 1.0) -> int:
    """Calculate the token count of a given text using tiktoken, falling back to estimation.

    Args:
        text: The input text.
        model_id: The model identifier to pick the encoder.
        raw: If True, ignore custom multipliers.
        multiplier: A custom cost multiplier.
    """
    if not text:
        return 0

    token_multiplier = 1.0 if raw else multiplier

    try:
        import tiktoken

        # Try to resolve specific encoding or fallback to cl100k_base
        encoding = None
        supported_models = ["gpt-4", "gpt-3.5-turbo", "gpt-4-turbo", "text-embedding-ada-002", "gpt-4o"]
        
        # Normalize model name for tiktoken
        model_lower = model_id.lower()
        matched_model = None
        for sm in supported_models:
            if sm in model_lower:
                matched_model = sm
                break

        try:
            if matched_model:
                encoding = tiktoken.encoding_for_model(matched_model)
            else:
                encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            encoding = tiktoken.get_encoding("cl100k_base")

        tokens = encoding.encode(text)
        return int(len(tokens) * token_multiplier)

    except Exception as e:
        logger.debug("tiktoken_estimation_fallback error=%s", e)
        return _estimate_tokens(text, token_multiplier)


def _estimate_tokens(text: str, multiplier: float = 1.0) -> int:
    """Fallback token estimation when tiktoken is unavailable."""
    char_count = len(text)
    # Check for Chinese characters
    is_chinese = bool(re.search(r"[\u4e00-\u9fa5]", text))

    if is_chinese:
        base_tokens = int(char_count / 1.5)
    else:
        base_tokens = int(char_count / 4)

    return int(base_tokens * multiplier)


def count_messages_tokens(messages: list[dict[str, Any]] | list[any], model_id: str = "gpt-4", multiplier: float = 1.0) -> int:
    """Calculate total token count for a list of message objects."""
    total = 0
    for msg in messages:
        # Support dict, LLMMessage, or any object with content
        content = ""
        if isinstance(msg, dict):
            content = msg.get("content") or ""
        else:
            content = getattr(msg, "content", "") or ""
        total += count_tokens(content, model_id, multiplier=multiplier)
    return total
