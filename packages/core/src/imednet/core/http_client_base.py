"""Base class for HTTP clients with shared initialization and configuration."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar, cast

import httpx

from imednet.auth.strategy import AuthStrategy
from imednet.constants import (
    CONTENT_TYPE_JSON,
    HEADER_ACCEPT,
    HEADER_CONTENT_TYPE,
)
from imednet.core.http.executor import BaseRequestExecutor

from .base_client import BaseClient, Tracer  # type: ignore[attr-defined]
from .retry import RetryConfig, RetryPolicy

logger = logging.getLogger(__name__)

ClientT = TypeVar("ClientT", bound=httpx.Client | httpx.AsyncClient)
ExecutorT = TypeVar("ExecutorT", bound=BaseRequestExecutor)


class HTTPClientBase(BaseClient, ABC, Generic[ClientT, ExecutorT]):
    """Shared logic for synchronous and asynchronous HTTP clients."""

    _client: ClientT
    _executor: ExecutorT

    @abstractmethod
    def _get_client_class(self) -> type[ClientT]:
        """Return the underlying httpx client class."""

    @abstractmethod
    def _create_executor(
        self, client: ClientT, retry_config: RetryConfig | None = None
    ) -> ExecutorT:
        """Create the request executor."""

    def __init__(
        self,
        api_key: str | None = None,
        security_key: str | None = None,
        base_url: str | None = None,
        timeout: float | httpx.Timeout = 30.0,
        tracer: Tracer | None = None,
        retry_config: RetryConfig | None = None,
        auth: AuthStrategy | None = None,
    ) -> None:
        """Initialize the HTTP client.

        Args:
            api_key: iMednet API key.
            security_key: iMednet security key.
            base_url: Base URL for the iMednet API.
            timeout: Default request timeout in seconds or httpx.Timeout.
            tracer: Optional OpenTelemetry tracer instance.
            retry_config: Centralized configuration for retry behaviors.
            auth: Optional pre-configured AuthStrategy.
        """
        super().__init__(
            api_key=api_key,
            security_key=security_key,
            base_url=base_url,
            timeout=timeout,
            tracer=tracer,
            retry_config=retry_config,
            auth=auth,
        )

        self._executor = self._create_executor(self._client, self.retry_config)

    def _create_client(self, auth: AuthStrategy) -> ClientT:
        """Create the underlying httpx client with appropriate headers and configuration."""
        headers = {
            HEADER_ACCEPT: CONTENT_TYPE_JSON,
            HEADER_CONTENT_TYPE: CONTENT_TYPE_JSON,
        }
        headers.update(auth.get_headers())

        client_cls = self._get_client_class()
        client: Any = client_cls(
            base_url=self._base_url,
            headers=headers,
            timeout=self.timeout,
        )
        return cast(ClientT, client)

    @property
    def retry_policy(self) -> RetryPolicy:
        """Return the current retry policy of the executor."""
        return cast(RetryPolicy, self._executor.retry_config.retry_policy)

    @retry_policy.setter
    def retry_policy(self, policy: RetryPolicy) -> None:
        """Set a new retry policy for the executor."""
        self._executor.retry_config.retry_policy = policy

    @abstractmethod
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an HTTP request. Return type varies by async/sync client."""
