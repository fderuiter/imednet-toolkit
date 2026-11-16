"""Thread- and task-local study context helpers."""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

from imednet.errors.validation import ConfigurationError

_active_study: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_study", default=None
)
_active_base_url: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_base_url", default=None
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
