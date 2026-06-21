from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AuthPrincipal:
    """Represents an authenticated user or agent principal."""
    user_id: str
    email: str | None = None
    is_admin: bool = False


@runtime_checkable
class AuthBackend(Protocol):
    """Protocol that any custom authentication backend must implement."""

    async def authenticate(self, token: str) -> AuthPrincipal | None:
        """Authenticate a token and return the AuthPrincipal or None if invalid."""
        ...

    async def issue_token(self, user_id: str) -> str:
        """Issue an authentication token for a given user ID."""
        ...
