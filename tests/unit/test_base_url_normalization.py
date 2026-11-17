"""Unit tests for base url normalization."""

from unittest.mock import MagicMock

import httpx
import pytest

from imednet.core.async_client import AsyncClient
from imednet.core.client import Client
from imednet.core.endpoint.base import GenericEndpoint
from imednet.models.base import ImednetBaseModel


class MockModel(ImednetBaseModel):
    """Test suite for MockModel."""


class MockEndpoint(GenericEndpoint[MockModel]):
    """Test suite for MockEndpoint."""

    BASE_PATH = "/api/v1/edc/studies"
    MODEL = MockModel
    PATH = "/test"


def test_client_strips_api_suffix() -> None:
    """Test that client strips api suffix."""
    client = Client(api_key="k", security_key="s", base_url="https://x/api")
    assert client.base_url == "https://x"
    assert client._client.base_url == httpx.URL("https://x")


@pytest.mark.asyncio
async def test_async_client_strips_api_suffix() -> None:
    """Test that async client strips api suffix asynchronously."""
    async with AsyncClient(
        api_key="k",
        security_key="s",
        base_url="https://x/api",
    ) as client:
        assert client.base_url == "https://x"
        assert client._client.base_url == httpx.URL("https://x")


def test_build_safe_path_handles_special_characters() -> None:
    """Test that build safe path handles special characters."""
    client = MagicMock(spec=Client)
    endpoint = MockEndpoint(client)

    path = endpoint._build_path("user@domain.com", "user%40domain.com")

    assert path == "/api/v1/edc/studies/user@domain.com/user@domain.com"
    assert "%2540" not in path


def test_build_safe_path_prevents_double_slashes() -> None:
    """Test that build safe path prevents double slashes."""
    client = MagicMock(spec=Client)
    endpoint = MockEndpoint(client)

    path = endpoint._build_path("//study//", "/subjects/", "///record///")

    assert path == "/api/v1/edc/studies/study/subjects/record"
    assert "//" not in path
