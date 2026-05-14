"""Common runtime exceptions."""

from __future__ import annotations

from typing import Any


class RuntimeError(Exception):  # noqa: A001 - intentional override of builtin
    """Base class for all steerable-agent-runtime errors."""

    def __init__(self, message: str, *, data: Any | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data


class StorageError(RuntimeError):
    """Persistence layer failure."""


class ToolDispatchError(RuntimeError):
    """Tool router could not satisfy a ToolCall."""


class PolicyDeniedError(ToolDispatchError):
    """A tool call was denied by policy (e.g. destructive without consent)."""


class BudgetExhaustedError(RuntimeError):
    """The harness budget would be violated by the next operation."""


class TransportError(RuntimeError):
    """Wire-level failure (SSE close, JSON-RPC parse error, etc.)."""
