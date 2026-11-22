# pylint: disable=duplicate-code
"""Authentication helpers."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .api_key import ApiKeyAuth
    from .credentials import Credentials, CredentialSource, MemoryCredentialStore
    from .oidc import OIDCAuth
    from .strategy import AuthStrategy

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "ApiKeyAuth": ("imednet.auth.api_key", "ApiKeyAuth"),
    "AuthStrategy": ("imednet.auth.strategy", "AuthStrategy"),
    "CredentialSource": ("imednet.auth.credentials", "CredentialSource"),
    "Credentials": ("imednet.auth.credentials", "Credentials"),
    "MemoryCredentialStore": ("imednet.auth.credentials", "MemoryCredentialStore"),
    "OIDCAuth": ("imednet.auth.oidc", "OIDCAuth"),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily resolve authentication classes on demand."""
        try:
            module_path, obj_name = _LAZY_ATTRS[name]
        except KeyError:
            raise AttributeError(name) from None
        mod = import_module(module_path)  # nosem
        obj = getattr(mod, obj_name)
        globals()[name] = obj
        return obj


def __dir__() -> list[str]:
    """Return module attributes including lazy exports."""
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "ApiKeyAuth",
    "AuthStrategy",
    "CredentialSource",
    "Credentials",
    "MemoryCredentialStore",
    "OIDCAuth",
]
