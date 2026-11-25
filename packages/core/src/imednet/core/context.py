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


def _safe_context_attr(obj: Any, attr: str) -> Any:
    """Read an attribute without invoking ``__getattr__`` on private names or spawning child mocks."""
    if obj is None:
        return None
    if isinstance(obj, NonCallableMock):
        try:
            obj_dict = object.__getattribute__(obj, "__dict__")
        except AttributeError:
            return None
        mock_children = obj_dict.get("_mock_children") or {}
        if attr not in obj_dict and attr not in mock_children:
            return None
        return getattr(obj, attr, None)
    if attr.startswith("_"):
        try:
            obj_dict = object.__getattribute__(obj, "__dict__")
        except AttributeError:
            obj_dict = None
        if isinstance(obj_dict, dict) and attr in obj_dict:
            return obj_dict[attr]
        for cls in type(obj).__mro__:
            if attr in cls.__dict__:
                return getattr(obj, attr, None)
        return None
    return getattr(obj, attr, None)


def extract_sdk_base_url(sdk: Any) -> str | None:
    """Extract a normalized string base URL from an SDK or client instance, if present."""
    if sdk is None:
        return None
    candidates = (
        _safe_context_attr(sdk, "_base_url"),
        _safe_context_attr(sdk, "base_url"),
        _safe_context_attr(_safe_context_attr(sdk, "config"), "base_url"),
        _safe_context_attr(_safe_context_attr(sdk, "_client"), "base_url"),
        _safe_context_attr(_safe_context_attr(sdk, "_async_client"), "base_url"),
        _safe_context_attr(_safe_context_attr(sdk, "client"), "base_url"),
        _safe_context_attr(_safe_context_attr(sdk, "async_client"), "base_url"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            try:
                return sanitize_base_url(candidate)
            except Exception:  # noqa: S112
                continue
    return None


def extract_sdk_security_key(sdk: Any) -> str | None:
    """Extract a non-empty governance secret from an SDK or client instance, if present.

    Precedence follows ADR-0005 when explicit encryption env vars are unset:
    explicit/configured ``security_key``, then OIDC bearer ``token``, then ``api_key``.
    """
    if sdk is None:
        return None
    cfg = _safe_context_attr(sdk, "config")
    sdk_auth = _safe_context_attr(sdk, "auth")
    sync_client_auth = _safe_context_attr(_safe_context_attr(sdk, "_client"), "auth")
    async_client_auth = _safe_context_attr(_safe_context_attr(sdk, "_async_client"), "auth")
    pub_client_auth = _safe_context_attr(_safe_context_attr(sdk, "client"), "auth")
    pub_async_auth = _safe_context_attr(_safe_context_attr(sdk, "async_client"), "auth")

    security_candidates = (
        _safe_context_attr(sdk, "_security_key"),
        _safe_context_attr(cfg, "security_key"),
        _safe_context_attr(sdk_auth, "security_key"),
        _safe_context_attr(sync_client_auth, "security_key"),
        _safe_context_attr(async_client_auth, "security_key"),
        _safe_context_attr(pub_client_auth, "security_key"),
        _safe_context_attr(pub_async_auth, "security_key"),
    )
    oidc_candidates = (
        _safe_context_attr(sdk_auth, "token"),
        _safe_context_attr(sync_client_auth, "token"),
        _safe_context_attr(async_client_auth, "token"),
        _safe_context_attr(pub_client_auth, "token"),
        _safe_context_attr(pub_async_auth, "token"),
        _safe_context_attr(cfg, "oidc_token"),
    )
    api_key_candidates = (
        _safe_context_attr(sdk, "_api_key"),
        _safe_context_attr(cfg, "api_key"),
        _safe_context_attr(sdk_auth, "api_key"),
        _safe_context_attr(sync_client_auth, "api_key"),
        _safe_context_attr(async_client_auth, "api_key"),
        _safe_context_attr(pub_client_auth, "api_key"),
        _safe_context_attr(pub_async_auth, "api_key"),
    )

    simulated_placeholders = {"SIMULATED_KEY", "simulated_security_key", "simulated_api_key"}
    simulated_fallback: str | None = None
    for candidate in (*security_candidates, *oidc_candidates, *api_key_candidates):
        if isinstance(candidate, str) and candidate.strip():
            if candidate in simulated_placeholders:
                if simulated_fallback is None:
                    simulated_fallback = candidate
                continue
            return candidate
    return simulated_fallback


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


_DEFAULT_ENVIRON = os.environ
_ENVIRON_DATA: dict[Any, Any] | None = getattr(_DEFAULT_ENVIRON, "_data", None)
_STRICT_ENV_KEY = "IMEDNET_STRICT_MODE"
_ENCODED_STRICT_KEY: Any = (
    _DEFAULT_ENVIRON.encodekey(_STRICT_ENV_KEY)
    if hasattr(_DEFAULT_ENVIRON, "encodekey")
    else _STRICT_ENV_KEY
)
_STRICT_ENV_CACHE: dict[Any, bool] = {}


def is_strict_mode() -> bool:
    """Return whether strict validation mode is enabled in the active context or environment."""
    active = _active_strict_mode.get()
    if active is not None:
        return active
    env = os.environ
    if env is _DEFAULT_ENVIRON and _ENVIRON_DATA is not None:
        raw = _ENVIRON_DATA.get(_ENCODED_STRICT_KEY)
        if raw is None:
            return False
        cached = _STRICT_ENV_CACHE.get(raw)
        if cached is not None:
            return cached
        decoded = _DEFAULT_ENVIRON.decodevalue(raw)
        val = parse_bool(decoded, strict=False)
        if len(_STRICT_ENV_CACHE) > 32:
            _STRICT_ENV_CACHE.clear()
        _STRICT_ENV_CACHE[raw] = val
        return val

    raw_str = env.get(_STRICT_ENV_KEY)
    if raw_str is None:
        return False
    cached = _STRICT_ENV_CACHE.get(raw_str)
    if cached is not None:
        return cached
    val = parse_bool(raw_str, strict=False)
    if len(_STRICT_ENV_CACHE) > 32:
        _STRICT_ENV_CACHE.clear()
    _STRICT_ENV_CACHE[raw_str] = val
    return val


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
