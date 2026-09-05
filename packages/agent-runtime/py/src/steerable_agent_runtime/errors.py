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


class StoreAlreadyOwnedError(StorageError):
    """Another process already holds the write lease for this database."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"store already owned: {path} "
            "(another process has this sqlite database open for write)"
        )
        self.path = path


class ToolDispatchError(RuntimeError):
    """Tool router could not satisfy a ToolCall."""


class PolicyDeniedError(ToolDispatchError):
    """A tool call was denied by policy (e.g. destructive without consent)."""


class ApprovalAborted(PolicyDeniedError):
    """The approval decision for a call was ``abort``.

    Distinct from denial: denial becomes a tool result the model can react to
    while the run continues; abort ends the turn. The loop records the current
    batch (every tool_call keeps a response) and finishes the run as failed.
    """


class BudgetExhaustedError(RuntimeError):
    """The harness budget would be violated by the next operation."""


class TransportError(RuntimeError):
    """Wire-level failure (SSE close, JSON-RPC parse error, etc.)."""
