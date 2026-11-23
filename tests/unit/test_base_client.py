"""Unit tests for base client."""

from unittest.mock import MagicMock

import httpx

from imednet.auth.strategy import AuthStrategy
from imednet.constants import HEADER_API_KEY, HEADER_SECURITY_KEY
from imednet.core import base_client
from imednet.core.base_client import BaseClient


class DummyClient(BaseClient):
    """Test suite for DummyClient."""

    def _create_client(self, auth: AuthStrategy) -> httpx.Client:
        """Helper function to  create client."""
        headers = auth.get_headers()
        return httpx.Client(headers=headers)


def test_initialization_from_env(monkeypatch) -> None:
    """Test that initialization from env."""
    monkeypatch.setenv("IMEDNET_API_KEY", "env_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "env_secret")
    monkeypatch.setenv("IMEDNET_BASE_URL", "https://env.example.com")

    tracer = MagicMock()
    monkeypatch.setattr(base_client, "trace", MagicMock(get_tracer=lambda _: tracer))

    client = DummyClient()

    assert client.base_url == "https://env.example.com"
    assert client._client.headers[HEADER_API_KEY] == "env_key"
    assert client._client.headers[HEADER_SECURITY_KEY] == "env_secret"
    assert client._tracer is tracer


def test_shared_tls_context_caching_env_overrides_and_transport_isolation(
    monkeypatch, tmp_path
) -> None:
    """Verify shared TLS contexts cache by env cert paths, skip injected transports, and preserve isolation."""
    import asyncio
    import ssl

    import certifi

    from imednet import AsyncImednetSDK, ImednetSDK
    from imednet.core import http_client_base
    from imednet.core.http_client_base import _OriginScopedAuth

    http_client_base._build_tls_context.cache_clear()

    ssl_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    orig_create_default_context = ssl.create_default_context

    def _spy_create_default_context(*args, **kwargs):
        ssl_calls.append((args, kwargs))
        return orig_create_default_context(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", _spy_create_default_context)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    # Injected custom and simulation transports skip TLS setup even when TLS cache is cold
    custom_sync_transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    custom_async_transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))

    sim_sdk = ImednetSDK(simulation_mode=True)
    inj_sdk = ImednetSDK(
        api_key="key",  # pragma: allowlist secret
        security_key="secret",  # pragma: allowlist secret
        base_url="https://a.example.test",
        transport=custom_sync_transport,
    )
    sim_sdk.close()
    inj_sdk.close()

    async def _check_async_injected() -> None:
        async with AsyncImednetSDK(simulation_mode=True):
            pass
        async with AsyncImednetSDK(
            api_key="key",  # pragma: allowlist secret
            security_key="secret",  # pragma: allowlist secret
            base_url="https://a.example.test",
            transport=custom_async_transport,
        ):
            pass

    asyncio.run(_check_async_injected())
    assert ssl_calls == []

    # Default environment creates one shared SSLContext and reuses it across sync and async SDKs
    sdk1 = ImednetSDK(
        api_key="key_a",  # pragma: allowlist secret
        security_key="secret_a",  # pragma: allowlist secret
        base_url="https://one.example.test",
    )
    sdk2 = ImednetSDK(
        api_key="key_b",  # pragma: allowlist secret
        security_key="secret_b",  # pragma: allowlist secret
        base_url="https://two.example.test",
    )
    try:
        assert len(ssl_calls) == 1
        ctx_default = sdk1._client._client._transport._pool._ssl_context
        assert sdk2._client._client._transport._pool._ssl_context is ctx_default

        # Per-instance httpx client, transport, CircuitBreaker, and _OriginScopedAuth remain isolated
        assert sdk1._client._client is not sdk2._client._client
        assert sdk1._client._client._transport is not sdk2._client._client._transport
        assert sdk1._client._circuit_breaker is not sdk2._client._circuit_breaker
        assert isinstance(sdk1._client._client.auth, _OriginScopedAuth)
        assert sdk1._client._client.auth is not sdk2._client._client.auth
    finally:
        sdk1.close()
        sdk2.close()

    async def _check_async_isolation() -> ssl.SSLContext:
        async with (
            AsyncImednetSDK(
                api_key="async_key_a",  # pragma: allowlist secret
                security_key="async_secret_a",  # pragma: allowlist secret
                base_url="https://a1.example.test",
            ) as asdk1,
            AsyncImednetSDK(
                api_key="async_key_b",  # pragma: allowlist secret
                security_key="async_secret_b",  # pragma: allowlist secret
                base_url="https://a2.example.test",
            ) as asdk2,
        ):
            assert asdk1._async_client._client is not asdk2._async_client._client
            assert asdk1._async_client._circuit_breaker is not asdk2._async_client._circuit_breaker
            assert isinstance(asdk1._async_client._client.auth, _OriginScopedAuth)
            assert asdk1._async_client._client.auth is not asdk2._async_client._client.auth
            ctx_a1 = asdk1._async_client._client._transport._pool._ssl_context
            ctx_a2 = asdk2._async_client._client._transport._pool._ssl_context
            assert ctx_a1 is ctx_a2
            return ctx_a1

    assert asyncio.run(_check_async_isolation()) is ctx_default
    assert len(ssl_calls) == 1

    # Changing SSL_CERT_FILE resolves a distinct matching SSLContext and caches it
    ca_copy = tmp_path / "custom_ca.pem"
    ca_copy.write_bytes(certifi.contents().encode("utf-8"))
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_copy))

    sdk_file_1 = ImednetSDK(
        api_key="key",  # pragma: allowlist secret
        security_key="secret",  # pragma: allowlist secret
        base_url="https://one.example.test",
    )
    sdk_file_2 = ImednetSDK(
        api_key="key",  # pragma: allowlist secret
        security_key="secret",  # pragma: allowlist secret
        base_url="https://one.example.test",
    )
    try:
        assert len(ssl_calls) == 2
        assert ssl_calls[-1][1] == {"cafile": str(ca_copy)}
        ctx_file = sdk_file_1._client._client._transport._pool._ssl_context
        assert ctx_file is not ctx_default
        assert sdk_file_2._client._client._transport._pool._ssl_context is ctx_file
    finally:
        sdk_file_1.close()
        sdk_file_2.close()

    # Changing SSL_CERT_DIR (with SSL_CERT_FILE unset and set) resolves matching SSLContexts
    certs_dir = tmp_path / "certs_dir"
    certs_dir.mkdir()
    monkeypatch.setenv("SSL_CERT_DIR", str(certs_dir))
    sdk_both = ImednetSDK(
        api_key="key",  # pragma: allowlist secret
        security_key="secret",  # pragma: allowlist secret
        base_url="https://one.example.test",
    )
    try:
        assert len(ssl_calls) == 3
        assert ssl_calls[-1][1] == {"cafile": str(ca_copy), "capath": str(certs_dir)}
        ctx_both = sdk_both._client._client._transport._pool._ssl_context
        assert ctx_both is not ctx_file
    finally:
        sdk_both.close()

    monkeypatch.delenv("SSL_CERT_FILE")
    sdk_dir_1 = ImednetSDK(
        api_key="key",  # pragma: allowlist secret
        security_key="secret",  # pragma: allowlist secret
        base_url="https://one.example.test",
    )
    sdk_dir_2 = ImednetSDK(
        api_key="key",  # pragma: allowlist secret
        security_key="secret",  # pragma: allowlist secret
        base_url="https://one.example.test",
    )
    try:
        assert len(ssl_calls) == 4
        assert ssl_calls[-1][1] == {"capath": str(certs_dir)}
        ctx_dir = sdk_dir_1._client._client._transport._pool._ssl_context
        assert ctx_dir is not ctx_default
        assert ctx_dir is not ctx_file
        assert sdk_dir_2._client._client._transport._pool._ssl_context is ctx_dir
    finally:
        sdk_dir_1.close()
        sdk_dir_2.close()
