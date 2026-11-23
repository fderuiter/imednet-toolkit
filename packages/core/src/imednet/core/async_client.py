"""Asynchronous HTTP client for the iMednet API."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Sequence
from typing import Any

import httpx

from imednet.core.context import reset_strict_mode_context, set_strict_mode_context
from imednet.core.http.executor import AsyncRequestExecutor
from imednet.core.retry import RetryConfig

from .http_client_base import (
    _MAX_EXTERNAL_REDIRECTS,
    _ORIGINAL_GETADDRINFO,
    HTTPClientBase,
    _parse_and_validate_external_url,
    _validate_addrinfo_results,
)

logger = logging.getLogger(__name__)


class AsyncClient(HTTPClientBase[httpx.AsyncClient, AsyncRequestExecutor]):
    """Asynchronous variant of :class:`~imednet.core.client.Client`."""

    def _get_client_class(self) -> type[httpx.AsyncClient]:
        """Return the underlying asynchronous HTTP client class."""
        return httpx.AsyncClient

    def _create_executor(
        self, client: httpx.AsyncClient, retry_config: RetryConfig | None = None
    ) -> AsyncRequestExecutor:
        """Create an asynchronous request executor for this client."""
        return AsyncRequestExecutor(
            send=client.request,
            tracer=self._tracer,
            retry_config=retry_config,
            circuit_breaker=self._circuit_breaker,
        )

    async def __aenter__(self) -> AsyncClient:
        """Enter the asynchronous context manager."""
        self._strict_mode_tokens.append(set_strict_mode_context(self.strict_mode))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        """Exit the asynchronous context manager and close the client."""
        try:
            await self.aclose()
        finally:
            if self._strict_mode_tokens:
                reset_strict_mode_context(self._strict_mode_tokens.pop())

    async def aclose(self) -> None:
        """Close the underlying async HTTP client."""
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Internal method to dispatch an asynchronous request via the executor."""
        return await self._executor(method, path, **kwargs)

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an asynchronous GET request.

        Args:
            path: URL path or full URL.
            params: Query parameters.
            **kwargs: Additional arguments passed to the underlying executor.
        """
        return await self._request("GET", path, params=params, **kwargs)

    async def post(
        self,
        path: str,
        json: Any | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an asynchronous POST request.

        Args:
            path: URL path or full URL.
            json: JSON body for the request.
            **kwargs: Additional arguments passed to the underlying executor.
        """
        return await self._request("POST", path, json=json, **kwargs)

    async def fetch_external_url(self, url: str, **kwargs: Any) -> httpx.Response:
        """Asynchronously fetch an external HTTPS URL with SSRF and redirect protections."""
        current_url = url
        for hop in range(_MAX_EXTERNAL_REDIRECTS + 1):
            validated_url, host_to_resolve, port = _parse_and_validate_external_url(current_url)
            if host_to_resolve is not None and self._should_resolve_external_dns():
                infos: Sequence[Any]
                if socket.getaddrinfo is not _ORIGINAL_GETADDRINFO:
                    infos = socket.getaddrinfo(host_to_resolve, port, type=socket.SOCK_STREAM)
                else:
                    loop = asyncio.get_running_loop()
                    infos = await loop.getaddrinfo(host_to_resolve, port, type=socket.SOCK_STREAM)
                _validate_addrinfo_results(infos, host_to_resolve)
            req_kwargs = self._prepare_external_kwargs(validated_url, kwargs)
            response = await self._external_executor("GET", validated_url, **req_kwargs)
            next_url = self._resolve_same_origin_redirect(validated_url, response, hop)
            if next_url is None:
                return response
            current_url = next_url
        raise ValueError("Maximum redirects exceeded for external URL")
