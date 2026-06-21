from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from steerable_agent_protocol import ChatMessage

logger = logging.getLogger(__name__)


@runtime_checkable
class ContextProvider(Protocol):
    """Protocol that any custom business context provider must implement."""
    name: str

    async def provide(
        self,
        *,
        user_id: str,
        session_id: str,
        query: str,
        state: dict[str, Any],
    ) -> list[ChatMessage] | str:
        """Fetch and return relevant context as a list of ChatMessages or a raw string."""
        ...


class ContextEngine:
    """Orchestrates and merges context from multiple ContextProviders."""

    def __init__(self) -> None:
        self._providers: dict[str, ContextProvider] = {}

    def register(self, provider: ContextProvider) -> None:
        """Register a ContextProvider."""
        if provider.name in self._providers:
            logger.warning("ContextProvider '%s' already registered. Overwriting.", provider.name)
        self._providers[provider.name] = provider

    def unregister(self, name: str) -> None:
        """Unregister a ContextProvider."""
        self._providers.pop(name, None)

    def list_providers(self) -> list[ContextProvider]:
        """List all registered ContextProviders."""
        return list(self._providers.values())

    async def build(
        self,
        *,
        user_id: str,
        session_id: str,
        query: str,
        state: dict[str, Any],
    ) -> str:
        """Query all registered context providers and assemble their outputs into a single string.

        This builds the context block to be injected into prompts.
        """
        blocks: list[str] = []

        for name, provider in self._providers.items():
            try:
                result = await provider.provide(
                    user_id=user_id,
                    session_id=session_id,
                    query=query,
                    state=state,
                )
                if isinstance(result, str):
                    if result.strip():
                        blocks.append(f"## {name.capitalize()} Context\n\n{result.strip()}")
                elif isinstance(result, list):
                    # Format ChatMessages or dicts into a readable string
                    formatted_messages: list[str] = []
                    for msg in result:
                        if isinstance(msg, ChatMessage):
                            role = msg.role
                            content = msg.content or ""
                        elif isinstance(msg, dict):
                            role = msg.get("role", "unknown")
                            content = msg.get("content") or ""
                        else:
                            continue
                        formatted_messages.append(f"[{role}]: {content}")
                    if formatted_messages:
                        blocks.append(f"## {name.capitalize()} Context\n\n" + "\n".join(formatted_messages))
            except Exception as e:
                logger.exception("Failed to build context from provider '%s'", name)

        return "\n\n".join(blocks)
