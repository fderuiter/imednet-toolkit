"""Global SDK configuration state.

This module provides the core configuration object for the iMednet SDK.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from imednet.auth.credentials import Credentials, CredentialSource
from imednet.utils.validators import parse_bool

__all__ = ["Config", "load_config"]


@dataclass(frozen=True)
class Config:
    """Configuration object for the SDK client.

    Attributes:
        api_key: The API key for authentication.
        security_key: The security key for authentication.
        base_url: The base URL for the iMednet API.
        oidc_token: Optional OIDC token for authentication.
        timeout: The default timeout for network requests.
        strict_mode: Toggle strict mode for data validation.
        simulation_mode: Toggle offline simulation mode.
    """

    api_key: str | None = None
    security_key: str | None = None
    base_url: str | None = None
    oidc_token: str | None = None
    timeout: float = 30.0
    strict_mode: bool = False
    simulation_mode: bool = False

    def __repr__(self) -> str:
        """Return a string representation of the configuration.

        Security-sensitive fields are masked.

        Returns:
            A string representation of the configuration.
        """
        return f"Config(api_key='********', security_key='********', oidc_token='********', base_url={self.base_url!r}, timeout={self.timeout!r}, strict_mode={self.strict_mode!r}, simulation_mode={self.simulation_mode!r})"


def _read_credential_source(credential_source: CredentialSource | None) -> Credentials:
    """Read and validate a snapshot from an optional CredentialSource."""
    credentials = Credentials()
    if credential_source is not None:
        try:
            snapshot = credential_source.get_credentials()
            if snapshot is not None:
                credentials = snapshot
        except Exception:
            raise ValueError("Credential source lookup failed") from None
        if not isinstance(credentials, Credentials):
            raise ValueError("Credential source returned an invalid snapshot")
    return credentials


def _finalize_credentials(
    api_key: str | None,
    security_key: str | None,
    base_url: str | None,
    oidc_token: str | None,
    *,
    simulation_mode: bool,
    require_credentials: bool,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Normalize string fields and validate authentication requirements."""
    api_key = (api_key or "").strip() or None
    security_key = (security_key or "").strip() or None
    base_url = base_url.strip() if base_url else None
    oidc_token = (oidc_token or "").strip() or None

    if simulation_mode:
        api_key = api_key or "simulated_api_key"
        security_key = security_key or "simulated_security_key"
    elif require_credentials and not oidc_token:
        if not api_key and not security_key:
            raise ValueError("Either OIDC token or both API key and security key are required")
        if not api_key:
            raise ValueError("API key is required when not using OIDC")
        if not security_key:
            raise ValueError("Security key is required when not using OIDC")
    return api_key, security_key, base_url, oidc_token


def merge_config_overrides(
    config: Config,
    *,
    api_key: str | None = None,
    security_key: str | None = None,
    base_url: str | None = None,
    oidc_token: str | None = None,
    timeout: float | None = None,
    strict_mode: bool | None = None,
    simulation_mode: bool | None = None,
    credential_source: CredentialSource | None = None,
    require_credentials: bool = True,
) -> Config:
    """Apply explicit overrides onto a pre-resolved Config without reading environment variables."""
    credentials = _read_credential_source(credential_source)
    resolved_api_key = (
        api_key
        if api_key is not None
        else (credentials.api_key if credentials.api_key is not None else config.api_key)
    )
    resolved_security_key = (
        security_key
        if security_key is not None
        else (
            credentials.security_key
            if credentials.security_key is not None
            else config.security_key
        )
    )
    resolved_oidc_token = (
        oidc_token
        if oidc_token is not None
        else (credentials.oidc_token if credentials.oidc_token is not None else config.oidc_token)
    )
    resolved_base_url = base_url if base_url is not None else config.base_url
    resolved_timeout = timeout if timeout is not None else config.timeout
    resolved_strict_mode = strict_mode if strict_mode is not None else config.strict_mode
    resolved_simulation_mode = (
        simulation_mode if simulation_mode is not None else config.simulation_mode
    )

    resolved_api_key, resolved_security_key, resolved_base_url, resolved_oidc_token = (
        _finalize_credentials(
            resolved_api_key,
            resolved_security_key,
            resolved_base_url,
            resolved_oidc_token,
            simulation_mode=resolved_simulation_mode,
            require_credentials=require_credentials,
        )
    )
    updated = replace(
        config,
        api_key=resolved_api_key,
        security_key=resolved_security_key,
        base_url=resolved_base_url,
        oidc_token=resolved_oidc_token,
        timeout=resolved_timeout,
        strict_mode=resolved_strict_mode,
        simulation_mode=resolved_simulation_mode,
    )
    return config if updated == config else updated


def load_config(
    api_key: str | None = None,
    security_key: str | None = None,
    base_url: str | None = None,
    oidc_token: str | None = None,
    timeout: float | None = None,
    strict_mode: bool | None = None,
    simulation_mode: bool | None = None,
    *,
    credential_source: CredentialSource | None = None,
    require_credentials: bool = True,
) -> Config:
    """Return configuration using arguments or environment variables.

    Args:
        api_key: The API key for authentication. Defaults to IMEDNET_API_KEY environment variable.
        security_key: The security key for authentication. Defaults to IMEDNET_SECURITY_KEY environment variable.
        base_url: The base URL for the iMednet API. Defaults to IMEDNET_BASE_URL environment variable.
        oidc_token: Optional OIDC token. Defaults to IMEDNET_OIDC_TOKEN environment variable.
        timeout: HTTP request timeout in seconds. Defaults to IMEDNET_TIMEOUT environment variable or 30.0.
        strict_mode: Toggle strict mode for data validation. Defaults to IMEDNET_STRICT_MODE environment variable or False.
        simulation_mode: Toggle offline simulation mode. Defaults to IMEDNET_SIMULATION_MODE environment variable or False.
        credential_source: Optional caller-owned source, consulted before the environment.
        require_credentials: Validate default credential requirements. Custom authentication
            or an injected client owns authentication when this is False.

    Returns:
        The loaded SDK configuration.

    Raises:
        ValueError: If required authentication parameters are missing.
    """
    credentials = _read_credential_source(credential_source)
    api_key = api_key if api_key is not None else credentials.api_key
    security_key = security_key if security_key is not None else credentials.security_key
    oidc_token = oidc_token if oidc_token is not None else credentials.oidc_token
    api_key = api_key if api_key is not None else os.getenv("IMEDNET_API_KEY")
    security_key = security_key if security_key is not None else os.getenv("IMEDNET_SECURITY_KEY")
    base_url = base_url if base_url is not None else os.getenv("IMEDNET_BASE_URL")
    oidc_token = oidc_token if oidc_token is not None else os.getenv("IMEDNET_OIDC_TOKEN")

    env_timeout_str = os.getenv("IMEDNET_TIMEOUT")
    if timeout is None and env_timeout_str is not None:
        try:
            timeout = float(env_timeout_str)
        except (ValueError, TypeError):
            timeout = 30.0
    if timeout is None:
        timeout = 30.0

    if strict_mode is None:
        env_strict_mode = os.getenv("IMEDNET_STRICT_MODE")
        if env_strict_mode is not None:  # noqa: SIM108
            strict_mode = parse_bool(env_strict_mode)
        else:
            strict_mode = False

    if simulation_mode is None:
        env_simulation_mode = os.getenv("IMEDNET_SIMULATION_MODE")
        if env_simulation_mode is not None:
            simulation_mode = parse_bool(env_simulation_mode)
        else:
            simulation_mode = False

    api_key, security_key, base_url, oidc_token = _finalize_credentials(
        api_key,
        security_key,
        base_url,
        oidc_token,
        simulation_mode=simulation_mode,
        require_credentials=require_credentials,
    )

    return Config(
        api_key=api_key,
        security_key=security_key,
        base_url=base_url,
        oidc_token=oidc_token,
        timeout=timeout,
        strict_mode=strict_mode,
        simulation_mode=simulation_mode,
    )
