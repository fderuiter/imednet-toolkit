"""Explicit, in-memory credential capabilities; no persistent storage or renewal."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, repr=False)
class Credentials:
    """A credential snapshot whose representation never includes secret values."""

    api_key: str | None = field(default=None, repr=False)
    security_key: str | None = field(default=None, repr=False)
    oidc_token: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        """Return only a redacted description."""
        return "Credentials(<redacted>)"


class CredentialSource(Protocol):
    """Read a caller-owned credential snapshot; None permits environment fallback."""

    def get_credentials(self) -> Credentials | None:
        """Return current credentials without logging or persisting their contents."""
        ...


class MemoryCredentialStore:
    """Caller-owned ephemeral store, with explicit overwrite and deletion.

    Reads return immutable snapshots. Clearing drops this store's reference;
    Python does not guarantee zeroization of strings or revoke existing clients.
    """

    def __init__(self, credentials: Credentials | None = None) -> None:
        """Initialize a store without reading the environment or touching disk."""
        self._credentials = credentials

    def get_credentials(self) -> Credentials | None:
        """Return the current immutable snapshot."""
        return self._credentials

    def set_credentials(self, credentials: Credentials) -> None:
        """Replace the snapshot for future client construction."""
        self._credentials = credentials

    def clear(self) -> None:
        """Delete this store's snapshot without changing existing clients."""
        self._credentials = None

    def __repr__(self) -> str:
        """Return a description without exposing the snapshot."""
        return "MemoryCredentialStore(<redacted>)"
