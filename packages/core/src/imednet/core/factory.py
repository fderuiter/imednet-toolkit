"""Factory for creating HTTP clients.

This module encapsulates the creation logic for `Client` and `AsyncClient`,
handling authentication, configuration, and retry policies.
"""

from __future__ import annotations

from typing import Any

from imednet.auth.api_key import ApiKeyAuth
from imednet.auth.oidc import OIDCAuth
from imednet.auth.strategy import AuthStrategy
from imednet.config import Config
from imednet.core.async_client import AsyncClient
from imednet.core.client import Client
from imednet.core.retry import RetryConfig


def _default_auth(config: Config) -> AuthStrategy:
    """Resolve the existing OIDC-before-key default policy once."""
    if config.oidc_token:
        return OIDCAuth(config.oidc_token)
    if not config.api_key and not config.security_key:
        raise ValueError("API key and security key are required")
    if not config.api_key:
        raise ValueError("API key is required")
    if not config.security_key:
        raise ValueError("Security key is required")
    return ApiKeyAuth(config.api_key, config.security_key)


class ClientFactory:
    """Factory for creating API clients."""

    @staticmethod
    def create_client(
        config: Config,
        timeout: float = 30.0,
        retry_config: RetryConfig | None = None,
        transport: Any = None,
        auth: AuthStrategy | None = None,
    ) -> Client:
        """Create a synchronous client."""
        if transport is None and config.simulation_mode:
            from imednet.simulation import create_simulation_transport

            transport = create_simulation_transport()
            auth = (
                auth
                if auth is not None
                else ApiKeyAuth(
                    config.api_key or "SIMULATED_KEY", config.security_key or "SIMULATED_KEY"
                )
            )
        if auth is None:
            auth = _default_auth(config)
        client = Client(
            api_key=config.api_key or "",
            security_key=config.security_key or "",
            base_url=config.base_url,
            timeout=timeout,
            retry_config=retry_config,
            auth=auth,
            transport=transport,
            simulation_mode=config.simulation_mode,
        )
        client.auth = auth
        return client

    @staticmethod
    def create_async_client(
        config: Config,
        timeout: float = 30.0,
        retry_config: RetryConfig | None = None,
        transport: Any = None,
        auth: AuthStrategy | None = None,
    ) -> AsyncClient:
        """Create an asynchronous client."""
        if transport is None and config.simulation_mode:
            from imednet.simulation import create_simulation_transport

            transport = create_simulation_transport()
            auth = (
                auth
                if auth is not None
                else ApiKeyAuth(
                    config.api_key or "SIMULATED_KEY", config.security_key or "SIMULATED_KEY"
                )
            )
        if auth is None:
            auth = _default_auth(config)
        async_client = AsyncClient(
            api_key=config.api_key or "",
            security_key=config.security_key or "",
            base_url=config.base_url,
            timeout=timeout,
            retry_config=retry_config,
            auth=auth,
            transport=transport,
            simulation_mode=config.simulation_mode,
        )
        async_client.auth = auth
        return async_client
