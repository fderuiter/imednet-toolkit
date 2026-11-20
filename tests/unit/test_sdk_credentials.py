"""Unit tests for sdk credentials."""

import pytest

from imednet.config import Config
from imednet.sdk import ImednetSDK


def test_missing_both_keys(monkeypatch) -> None:
    """Test that missing both keys."""
    monkeypatch.setattr(
        "imednet.sdk.load_config",
        lambda **_: Config(api_key="", security_key="", base_url=None),
    )
    with pytest.raises(ValueError, match="API key and security key are required"):
        ImednetSDK()


def test_missing_security_key(monkeypatch) -> None:
    """Test that missing security key."""
    monkeypatch.setattr(
        "imednet.sdk.load_config",
        lambda **_: Config(api_key="key", security_key="", base_url=None),
    )
    with pytest.raises(ValueError, match="Security key is required"):
        ImednetSDK()


def test_missing_api_key(monkeypatch) -> None:
    """Test that missing api key."""
    monkeypatch.setattr(
        "imednet.sdk.load_config",
        lambda **_: Config(api_key="", security_key="sec", base_url=None),
    )
    with pytest.raises(ValueError, match="API key is required"):
        ImednetSDK()


def test_initialization_succeeds(monkeypatch) -> None:
    """Test that initialization succeeds."""
    monkeypatch.setattr(
        "imednet.sdk.load_config",
        lambda **_: Config(api_key="key", security_key="sec", base_url=None),
    )
    sdk = ImednetSDK()
    assert isinstance(sdk, ImednetSDK)


@pytest.mark.asyncio
async def test_injected_transport_bypasses_credential_enforcement(monkeypatch) -> None:
    """Passing transport to ImednetSDK or AsyncImednetSDK bypasses credential checks."""
    import httpx

    from imednet.sdk import AsyncImednetSDK

    for env_var in ("IMEDNET_API_KEY", "IMEDNET_SECURITY_KEY", "IMEDNET_OIDC_TOKEN"):
        monkeypatch.delenv(env_var, raising=False)

    mock_transport = httpx.MockTransport(
        lambda _req: httpx.Response(200, json={"data": [], "pagination": {"totalPages": 0}})
    )
    with ImednetSDK(transport=mock_transport) as sdk:
        assert list(sdk.studies.list()) == []

    async with AsyncImednetSDK(transport=mock_transport) as async_sdk:
        assert [s async for s in async_sdk.studies.list()] == []

    with ImednetSDK(config=Config(), transport=mock_transport) as sdk_with_cfg:
        assert list(sdk_with_cfg.studies.list()) == []
