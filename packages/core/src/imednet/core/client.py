"""Core HTTP client for interacting with the iMednet REST API.

This module defines the `Client` class which handles:
- Authentication headers (API key and security key).
- Configuration of base URL, timeouts, and retry logic.
- Making HTTP GET and POST requests.
- Error mapping to custom errors.
- Context-manager support for automatic cleanup.
"""

from __future__ import annotations

import logging
import socket
from types import TracebackType
from typing import Any

import httpx

from imednet.core.context import reset_strict_mode_context, set_strict_mode_context
from imednet.core.http.executor import SyncRequestExecutor
from imednet.core.retry import RetryConfig

from .http_client_base import (
    _MAX_EXTERNAL_REDIRECTS,
    ExternalUrlError,
    HTTPClientBase,
    _parse_and_validate_external_url,
    _validate_addrinfo_results,
)

logger = logging.getLogger(__name__)


class Client(HTTPClientBase[httpx.Client, SyncRequestExecutor]):
    """Core HTTP client for the iMednet API.

    Attributes:
        base_url: Base URL for API requests.
        timeout: Default timeout for requests.
        retry_config: Centralized configuration for retry behaviors.
    """

    def _get_client_class(self) -> type[httpx.Client]:
        """Return the underlying synchronous HTTP client class."""
        return httpx.Client

    def _create_executor(
        self, client: httpx.Client, retry_config: RetryConfig | None = None
    ) -> SyncRequestExecutor:
        """Create a synchronous request executor for this client."""
        return SyncRequestExecutor(
            send=client.request,
            tracer=self._tracer,
            retry_config=retry_config,
            circuit_breaker=self._circuit_breaker,
        )

    def __enter__(self) -> Client:
        """Enter the synchronous context manager."""
        self._strict_mode_tokens.append(set_strict_mode_context(self.strict_mode))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit the synchronous context manager and close the client."""
        try:
            self.close()
        finally:
            if self._strict_mode_tokens:
                reset_strict_mode_context(self._strict_mode_tokens.pop())

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Internal method to dispatch a request via the executor."""
        return self._executor(method, path, **kwargs)

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make a GET request.

        Args:
            path: URL path or full URL.
            params: Query parameters.
            **kwargs: Additional arguments passed to the underlying executor.
        """
        return self._request("GET", path, params=params, **kwargs)

    def post(
        self,
        path: str,
        json: Any | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make a POST request.

        Args:
            path: URL path or full URL.
            json: JSON body for the request.
            **kwargs: Additional arguments passed to the underlying executor.
        """
        return self._request("POST", path, json=json, **kwargs)

    def fetch_external_url(self, url: str, **kwargs: Any) -> httpx.Response:
        """Fetch an external HTTPS URL with SSRF, origin-scoped auth, and redirect guards."""
        current_url = url
        for hop in range(_MAX_EXTERNAL_REDIRECTS + 1):
            validated_url, host_to_resolve, port = _parse_and_validate_external_url(current_url)
            if host_to_resolve is not None and self._should_resolve_external_dns():
                infos = socket.getaddrinfo(host_to_resolve, port, type=socket.SOCK_STREAM)
                _validate_addrinfo_results(infos, host_to_resolve)
            req_kwargs = self._prepare_external_kwargs(validated_url, kwargs)
            response = self._external_executor("GET", validated_url, **req_kwargs)
            next_url = self._resolve_external_redirect(validated_url, response, hop)
            if next_url is None:
                return response
            current_url = next_url
        raise ExternalUrlError("Maximum redirects exceeded for external URL")
