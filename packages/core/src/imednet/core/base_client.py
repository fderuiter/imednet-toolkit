# Base HTTP client for iMednet SDK

"""Common base client initialization for both synchronous and asynchronous SDKs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer
else:  # pragma: no cover - typing fallback for optional dependency
    Tracer = Any

import httpx

from imednet.auth.api_key import ApiKeyAuth
from imednet.auth.oidc import OIDCAuth
from imednet.auth.strategy import AuthStrategy
from imednet.config import load_config
from imednet.constants import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
)
from imednet.core.retry import RetryConfig
from imednet.utils import sanitize_base_url

trace: Any = None
try:
    from opentelemetry import trace as _trace

    trace = _trace
except Exception:  # pragma: no cover - optional dependency  # noqa: S110
    pass
logger = logging.getLogger(__name__)


class BaseClient:
    """Common initialization logic for HTTP clients."""

    def __init__(
        self,
        api_key: str | None = None,
        security_key: str | None = None,
        base_url: str | None = None,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        tracer: Tracer | None = None,
        retry_config: RetryConfig | None = None,
        auth: AuthStrategy | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        simulation_mode: bool | None = None,
    ) -> None:
        """Initialize common client settings.

        Args:
            api_key: iMednet API key.
            security_key: iMednet security key.
            base_url: Base URL for the iMednet API.
            timeout: Default request timeout in seconds or httpx.Timeout.
            tracer: Optional OpenTelemetry tracer instance.
            retry_config: Centralized configuration for retry behaviors.
            auth: Optional pre-configured AuthStrategy.
            transport: Optional custom httpx transport (e.g. for simulation).
            simulation_mode: Optional flag to enable offline simulation mode.
        """
        config = load_config(
            api_key=api_key,
            security_key=security_key,
            base_url=base_url,
            simulation_mode=simulation_mode,
            require_credentials=auth is None,
        )

        self.simulation_mode = config.simulation_mode
        self._transport = transport
        if self.simulation_mode and self._transport is None:
            from imednet.simulation import create_simulation_transport

            self._transport = create_simulation_transport()

        self.base_url = sanitize_base_url(config.base_url or DEFAULT_BASE_URL)
        self._base_url = httpx.URL(self.base_url)

        self.timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self.retry_config = retry_config or RetryConfig()

        if auth is not None:
            self.auth = auth
        elif self.simulation_mode:
            self.auth = ApiKeyAuth(
                config.api_key or "SIMULATED_KEY", config.security_key or "SIMULATED_KEY"
            )
        elif config.oidc_token:
            self.auth = OIDCAuth(config.oidc_token)
        else:
            self.auth = ApiKeyAuth(config.api_key or "", config.security_key or "")

        self._client = self._create_client(self.auth)
        self._tracer: Tracer | None = None

        if tracer is not None:
            self._tracer = tracer
        elif trace is not None:
            self._tracer = trace.get_tracer(__name__)
        else:
            self._tracer = None

    def _create_client(self, auth: AuthStrategy) -> Any:
        """Create the underlying HTTP client (to be implemented by subclasses)."""
        raise NotImplementedError
