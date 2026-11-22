# pylint: disable=duplicate-code
"""Re-exports core components for easier access."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from imednet.errors import (
        ApiError,
        AuthenticationError,
        AuthorizationError,
        BadRequestError,
        ConflictError,
        ForbiddenError,
        ImednetError,
        NotFoundError,
        PaginationError,
        RateLimitError,
        RequestError,
        ServerError,
        UnauthorizedError,
        ValidationError,
    )

    from .async_client import AsyncClient
    from .base_client import BaseClient
    from .client import Client
    from .endpoint.edc_mixin import (
        EdcAsyncGetEndpoint,
        EdcAsyncListEndpoint,
        EdcAsyncListGetEndpoint,
        EdcSyncGetEndpoint,
        EdcSyncListEndpoint,
        EdcSyncListGetEndpoint,
    )
    from .http_client_base import HTTPClientBase
    from .paginator import AsyncPaginator, BasePaginator, Paginator
    from .retry import DefaultRetryPolicy, RetryConfig, RetryPolicy, RetryState

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "ApiError": ("imednet.errors", "ApiError"),
    "AsyncClient": ("imednet.core.async_client", "AsyncClient"),
    "AsyncPaginator": ("imednet.core.paginator", "AsyncPaginator"),
    "AuthenticationError": ("imednet.errors", "AuthenticationError"),
    "AuthorizationError": ("imednet.errors", "AuthorizationError"),
    "BadRequestError": ("imednet.errors", "BadRequestError"),
    "BaseClient": ("imednet.core.base_client", "BaseClient"),
    "BasePaginator": ("imednet.core.paginator", "BasePaginator"),
    "Client": ("imednet.core.client", "Client"),
    "ConflictError": ("imednet.errors", "ConflictError"),
    "DefaultRetryPolicy": ("imednet.core.retry", "DefaultRetryPolicy"),
    "EdcAsyncGetEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcAsyncGetEndpoint"),
    "EdcAsyncListEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcAsyncListEndpoint"),
    "EdcAsyncListGetEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcAsyncListGetEndpoint"),
    "EdcSyncGetEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcSyncGetEndpoint"),
    "EdcSyncListEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcSyncListEndpoint"),
    "EdcSyncListGetEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcSyncListGetEndpoint"),
    "ForbiddenError": ("imednet.errors", "ForbiddenError"),
    "HTTPClientBase": ("imednet.core.http_client_base", "HTTPClientBase"),
    "ImednetError": ("imednet.errors", "ImednetError"),
    "NotFoundError": ("imednet.errors", "NotFoundError"),
    "PaginationError": ("imednet.errors", "PaginationError"),
    "Paginator": ("imednet.core.paginator", "Paginator"),
    "RateLimitError": ("imednet.errors", "RateLimitError"),
    "RequestError": ("imednet.errors", "RequestError"),
    "RetryConfig": ("imednet.core.retry", "RetryConfig"),
    "RetryPolicy": ("imednet.core.retry", "RetryPolicy"),
    "RetryState": ("imednet.core.retry", "RetryState"),
    "ServerError": ("imednet.errors", "ServerError"),
    "UnauthorizedError": ("imednet.errors", "UnauthorizedError"),
    "ValidationError": ("imednet.errors", "ValidationError"),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily load core components from their submodules."""
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
    "ApiError",
    "AsyncClient",
    "AsyncPaginator",
    "AuthenticationError",
    "AuthorizationError",
    "BadRequestError",
    "BaseClient",
    "BasePaginator",
    "Client",
    "ConflictError",
    "DefaultRetryPolicy",
    "EdcAsyncGetEndpoint",
    "EdcAsyncListEndpoint",
    "EdcAsyncListGetEndpoint",
    "EdcSyncGetEndpoint",
    "EdcSyncListEndpoint",
    "EdcSyncListGetEndpoint",
    "ForbiddenError",
    "HTTPClientBase",
    "ImednetError",
    "NotFoundError",
    "PaginationError",
    "Paginator",
    "RateLimitError",
    "RequestError",
    "RetryConfig",
    "RetryPolicy",
    "RetryState",
    "ServerError",
    "UnauthorizedError",
    "ValidationError",
]
