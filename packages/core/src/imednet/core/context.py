"""Thread- and task-local study, base URL, security key, and strict-mode context helpers."""

from __future__ import annotations

import contextvars
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import NonCallableMock

from imednet.errors.validation import ConfigurationError
from imednet.utils.url import sanitize_base_url
from imednet.utils.validators import parse_bool

_active_study: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_study", default=None
)
_active_base_url: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_base_url", default=None
)
_active_security_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_security_key", default=None
)
_active_strict_mode: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "active_strict_mode", default=None
)


def set_study_context(study_key: str) -> contextvars.Token[str | None]:
    """Set the active study key for the current thread/task context."""
    return _active_study.set(study_key)


def reset_study_context(token: contextvars.Token[str | None]) -> None:
    """Reset the active study key using a token from :func:`set_study_context`."""
    _active_study.reset(token)


def set_base_url_context(base_url: str) -> contextvars.Token[str | None]:
    """Set the active base URL for the current thread/task context."""
    return _active_base_url.set(base_url)


def reset_base_url_context(token: contextvars.Token[str | None]) -> None:
    """Reset the active base URL using a token from :func:`set_base_url_context`."""
    _active_base_url.reset(token)


def get_base_url_context() -> str | None:
    """Get the current base URL for the active thread/task context."""
    return _active_base_url.get()


def set_security_key_context(security_key: str | None) -> contextvars.Token[str | None]:
    """Set the active SDK security key for the current thread/task context."""
    return _active_security_key.set(security_key)


def reset_security_key_context(token: contextvars.Token[str | None]) -> None:
    """Reset the active SDK security key using a token from :func:`set_security_key_context`."""
    _active_security_key.reset(token)


def get_security_key_context() -> str | None:
    """Get the current SDK security key for the active thread/task context."""
    return _active_security_key.get()


@contextmanager
def security_key_context(security_key: str | None) -> Iterator[None]:
    """Temporarily set the active SDK security key for the current thread/task context."""
    token = set_security_key_context(security_key)
    try:
        yield
    finally:
        reset_security_key_context(token)


def extract_sdk_base_url(sdk: Any) -> str | None:
    """Extract a normalized string base URL from an SDK or client instance, if present."""
    if sdk is None or isinstance(sdk, NonCallableMock):
        return None
    candidates = (
        getattr(sdk, "_base_url", None),
        getattr(sdk, "base_url", None),
        getattr(getattr(sdk, "config", None), "base_url", None),
        getattr(getattr(sdk, "_client", None), "base_url", None),
        getattr(getattr(sdk, "_async_client", None), "base_url", None),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            try:
                return sanitize_base_url(candidate)
            except Exception:  # noqa: S112
                continue
    return None


def extract_sdk_security_key(sdk: Any) -> str | None:
    """Extract a non-empty string security_key from an SDK or client instance, if present."""
    if sdk is None:
        return None
    candidates = (
        getattr(sdk, "_security_key", None),
        getattr(getattr(sdk, "config", None), "security_key", None),
        getattr(getattr(sdk, "auth", None), "security_key", None),
        getattr(getattr(getattr(sdk, "_client", None), "auth", None), "security_key", None),
        getattr(getattr(getattr(sdk, "_async_client", None), "auth", None), "security_key", None),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


@contextmanager
def sdk_governance_context(sdk: Any) -> Iterator[None]:
    """Bind normalized ``base_url`` and ``security_key`` context from *sdk* for the block."""
    resolved_base = extract_sdk_base_url(sdk)
    resolved_sec = extract_sdk_security_key(sdk)
    token_base = set_base_url_context(resolved_base) if resolved_base is not None else None
    token_sec = set_security_key_context(resolved_sec) if resolved_sec is not None else None
    try:
        yield
    finally:
        if token_sec is not None:
            reset_security_key_context(token_sec)
        if token_base is not None:
            reset_base_url_context(token_base)


def set_strict_mode_context(strict_mode: bool) -> contextvars.Token[bool | None]:
    """Set the active strict validation mode for the current thread/task context."""
    return _active_strict_mode.set(strict_mode)


def reset_strict_mode_context(token: contextvars.Token[bool | None]) -> None:
    """Reset the active strict validation mode using a token from :func:`set_strict_mode_context`."""
    _active_strict_mode.reset(token)


def is_strict_mode() -> bool:
    """Return whether strict validation mode is enabled in the active context or environment."""
    active = _active_strict_mode.get()
    if active is not None:
        return active
    return parse_bool(os.getenv("IMEDNET_STRICT_MODE", "false"))


@contextmanager
def strict_mode_context(strict_mode: bool) -> Iterator[None]:
    """Temporarily set the active strict validation mode for the current thread/task context."""
    token = set_strict_mode_context(strict_mode)
    try:
        yield
    finally:
        reset_strict_mode_context(token)


def clear_study_context() -> None:
    """Clear the active study key for the current thread/task context."""
    _active_study.set(None)


def get_study_context() -> str | None:
    """Get the current study key for the active thread/task context."""
    return _active_study.get()


def get_current_study() -> str:
    """Get the current study key or raise when no study context is set."""
    study = _active_study.get()
    if not study:
        raise ConfigurationError(
            "No study key provided. You must either pass 'study_key' explicitly "
            "to the endpoint method or set it using ImednetSDK.study_context(...)."
        )
    return study


@contextmanager
def study_context(study_key: str) -> Iterator[None]:
    """Temporarily set the active study key for the current thread/task context."""
    token = set_study_context(study_key)
    try:
        yield
    finally:
        reset_study_context(token)
