"""Unit tests for json list paginator robustness."""

from unittest.mock import Mock

import pytest

from imednet.core.paginator import AsyncJsonListPaginator, JsonListPaginator


class MockClient:
    """Test suite for MockClient."""

    def __init__(self, response_data):
        """Initialize the test object."""
        self.response_data = response_data

    def get(self, path, params=None):
        """Helper function to get."""
        response = Mock()
        response.json.return_value = self.response_data
        return response


class MockAsyncClient:
    """Test suite for MockAsyncClient."""

    def __init__(self, response_data):
        """Initialize the test object."""
        self.response_data = response_data

    async def get(self, path, params=None):
        """Helper function to get."""
        response = Mock()
        response.json.return_value = self.response_data
        return response


def test_json_list_paginator_raises_on_dict():
    """Test that JsonListPaginator raises TypeError when the API returns a dictionary.

    instead of the expected list.
    """
    # Simulate an error response or unexpected object structure
    client = MockClient({"error": "Something went wrong", "details": "Unexpected format"})
    paginator = JsonListPaginator(client, "/path")

    # Currently this fails (returns empty list), we want it to raise TypeError
    with pytest.raises(TypeError, match="API response must be a list"):
        list(paginator)


def test_json_list_paginator_raises_on_none():
    """Test that JsonListPaginator raises TypeError when the API returns null."""
    client = MockClient(None)
    paginator = JsonListPaginator(client, "/path")

    with pytest.raises(TypeError, match="API response must be a list"):
        list(paginator)


@pytest.mark.asyncio
async def test_async_json_list_paginator_raises_on_dict():
    """Test that AsyncJsonListPaginator raises TypeError when the API returns a dictionary.

    instead of the expected list.
    """
    client = MockAsyncClient({"error": "Async error"})
    paginator = AsyncJsonListPaginator(client, "/path")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="API response must be a list"):
        [item async for item in paginator]


@pytest.mark.asyncio
async def test_async_json_list_paginator_raises_on_none():
    """Test that AsyncJsonListPaginator raises TypeError when the API returns null."""
    client = MockAsyncClient(None)
    paginator = AsyncJsonListPaginator(client, "/path")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="API response must be a list"):
        [item async for item in paginator]
