"""Global SDK configuration state.

This module provides the core configuration object for the iMednet SDK.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

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
    """

    api_key: str | None = None
    security_key: str | None = None
    base_url: str | None = None
    oidc_token: str | None = None
    timeout: float = 30.0
    strict_mode: bool = False

    def __repr__(self) -> str:
        """Return a string representation of the configuration.

        Security-sensitive fields are masked.

        Returns:
            A string representation of the configuration.
        """
        return f"Config(api_key='********', security_key='********', oidc_token='********', base_url={self.base_url!r}, timeout={self.timeout!r}, strict_mode={self.strict_mode!r})"


def load_config(
    api_key: str | None = None,
    security_key: str | None = None,
    base_url: str | None = None,
    oidc_token: str | None = None,
    timeout: float | None = None,
    strict_mode: bool | None = None,
) -> Config:
    """Return configuration using arguments or environment variables.

    Args:
        api_key: The API key for authentication. Defaults to IMEDNET_API_KEY environment variable.
        security_key: The security key for authentication. Defaults to IMEDNET_SECURITY_KEY environment variable.
        base_url: The base URL for the iMednet API. Defaults to IMEDNET_BASE_URL environment variable.
        oidc_token: Optional OIDC token. Defaults to IMEDNET_OIDC_TOKEN environment variable.
        timeout: HTTP request timeout in seconds. Defaults to IMEDNET_TIMEOUT environment variable or 30.0.
        strict_mode: Toggle strict mode for data validation. Defaults to IMEDNET_STRICT_MODE environment variable or False.

    Returns:
        The loaded SDK configuration.

    Raises:
        ValueError: If required authentication parameters are missing.
    """
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

    # Set the environment variable so models can read the resolved value during validation
    os.environ["IMEDNET_STRICT_MODE"] = str(strict_mode).lower()

    api_key = (api_key or "").strip() or None
    security_key = (security_key or "").strip() or None
    base_url = base_url.strip() if base_url else None
    oidc_token = (oidc_token or "").strip() or None

    if not oidc_token:
        if not api_key and not security_key:
            raise ValueError("Either OIDC token or both API key and security key are required")
        if not api_key:
            raise ValueError("API key is required when not using OIDC")
        if not security_key:
            raise ValueError("Security key is required when not using OIDC")

    return Config(
        api_key=api_key,
        security_key=security_key,
        base_url=base_url,
        oidc_token=oidc_token,
        timeout=timeout,
        strict_mode=strict_mode,
    )
