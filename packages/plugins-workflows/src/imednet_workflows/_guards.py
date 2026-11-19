"""Client-mode validation guards for synchronous and asynchronous workflows."""

from __future__ import annotations

from typing import Any

from imednet.sdk import AsyncImednetSDK, ImednetSDK

_SYNC_FACADE_ATTRS = ("create_record", "get_studies", "get_records", "get_forms")
_ASYNC_FACADE_ATTRS = (
    "async_create_record",
    "async_get_studies",
    "async_get_records",
    "async_get_forms",
)


def _is_mock_object(sdk: Any) -> bool:
    """Return True if ``sdk`` is an instance from ``unittest.mock``."""
    return type(sdk).__module__.startswith("unittest.mock")


def is_async_only_sdk(sdk: Any) -> bool:
    """Return True when ``sdk`` is exclusively configured for asynchronous calls."""
    if isinstance(sdk, AsyncImednetSDK):
        return True
    if isinstance(sdk, ImednetSDK):
        return False
    has_sync = any(hasattr(sdk, attr) for attr in _SYNC_FACADE_ATTRS)
    has_async = any(hasattr(sdk, attr) for attr in _ASYNC_FACADE_ATTRS)
    if _is_mock_object(sdk):
        return has_async and not has_sync
    return has_async and not has_sync


def is_sync_only_sdk(sdk: Any) -> bool:
    """Return True when ``sdk`` is exclusively configured for synchronous calls."""
    if isinstance(sdk, ImednetSDK):
        return True
    if isinstance(sdk, AsyncImednetSDK):
        return False
    has_sync = any(hasattr(sdk, attr) for attr in _SYNC_FACADE_ATTRS)
    has_async = any(hasattr(sdk, attr) for attr in _ASYNC_FACADE_ATTRS)
    if _is_mock_object(sdk):
        return has_sync and not has_async
    return has_sync and not has_async


def is_async_sdk(sdk: Any) -> bool:
    """Return True when ``sdk`` should use asynchronous validation helpers."""
    if isinstance(sdk, AsyncImednetSDK):
        return True
    if isinstance(sdk, ImednetSDK):
        return False
    return hasattr(sdk, "async_create_record")


def ensure_sync_sdk(sdk: Any, owner: str, sync_method: str, async_method: str) -> None:
    """Raise ``TypeError`` if a synchronous workflow method is called with an async SDK.

    Args:
        sdk: The SDK or facade instance bound to the workflow.
        owner: Class or module name owning the method.
        sync_method: Synchronous method name invoked by the caller.
        async_method: Recommended ``async_*`` counterpart method name.

    Raises:
        TypeError: If ``sdk`` is an asynchronous SDK client.
    """
    if is_async_only_sdk(sdk):
        raise TypeError(
            f"{owner}.{sync_method}() cannot be used with an asynchronous SDK client "
            f"({type(sdk).__name__}); await {owner}.{async_method}() instead."
        )


def ensure_async_sdk(sdk: Any, owner: str, async_method: str, sync_method: str) -> None:
    """Raise ``TypeError`` if an asynchronous workflow method is called with a sync SDK.

    Args:
        sdk: The SDK or facade instance bound to the workflow.
        owner: Class or module name owning the method.
        async_method: Asynchronous method name invoked by the caller.
        sync_method: Recommended synchronous counterpart method name.

    Raises:
        TypeError: If ``sdk`` is a synchronous SDK client.
    """
    if is_sync_only_sdk(sdk):
        raise TypeError(
            f"{owner}.{async_method}() cannot be used with a synchronous SDK client "
            f"({type(sdk).__name__}); call {owner}.{sync_method}() instead."
        )
