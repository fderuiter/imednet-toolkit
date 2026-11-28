"""Unit tests for json list paginator robustness."""

from unittest.mock import Mock

import pytest

from imednet.core.paginator import AsyncJsonListPaginator, JsonListPaginator


class MockClient:
    """Test suite for MockClient."""

    def __init__(self, response_data):
        """Initialize the test object."""
        self.response_data = response_data
        self.calls = []

    def get(self, path, params=None):
        """Helper function to get."""
        self.calls.append({"path": path, "params": params})
        response = Mock()
        response.json.return_value = self.response_data
        return response


class MockAsyncClient:
    """Test suite for MockAsyncClient."""

    def __init__(self, response_data):
        """Initialize the test object."""
        self.response_data = response_data
        self.calls = []

    async def get(self, path, params=None):
        """Helper function to get."""
        self.calls.append({"path": path, "params": params})
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
    paginator = AsyncJsonListPaginator(client, "/path")  # type: ignore

    with pytest.raises(TypeError, match="API response must be a list"):
        [item async for item in paginator]


@pytest.mark.asyncio
async def test_async_json_list_paginator_raises_on_none():
    """Test that AsyncJsonListPaginator raises TypeError when the API returns null."""
    client = MockAsyncClient(None)
    paginator = AsyncJsonListPaginator(client, "/path")  # type: ignore

    with pytest.raises(TypeError, match="API response must be a list"):
        [item async for item in paginator]


def test_json_list_paginator_limit_has_more_and_nested_iterators() -> None:
    """JsonListPaginator(limit=2) yields 2 items, tracks has_more, and isolates nested iterators."""
    client = MockClient([10, 20, 30, 40])
    paginator = JsonListPaginator(client, "/path", params={"filter": "active"}, limit=2)
    assert paginator.cursor is None
    assert paginator.has_more is True

    it = iter(paginator)
    assert it.has_more is True  # type: ignore[attr-defined]
    assert next(it) == 10
    assert it.has_more is True  # type: ignore[attr-defined]
    assert next(it) == 20
    assert it.has_more is False  # type: ignore[attr-defined]
    assert paginator.has_more is False
    with pytest.raises(StopIteration):
        next(it)
    assert paginator.cursor is None
    assert paginator.has_more is False
    assert client.calls == [{"path": "/path", "params": {"filter": "active"}}]

    # Nested iterators preserve all items without clobbering cursor/limit state
    pairs = [(a, b) for a in paginator for b in paginator]
    assert pairs == [(10, 10), (10, 20), (20, 10), (20, 20)]


@pytest.mark.asyncio
async def test_async_json_list_paginator_limit_has_more_and_nested_iterators() -> None:
    """AsyncJsonListPaginator(limit=2) yields 2 items, tracks has_more, and isolates nested iterators."""
    client = MockAsyncClient(["q1", "q2", "q3"])
    paginator = AsyncJsonListPaginator(client, "/queries", params={"studyKey": "S1"}, limit=2)  # type: ignore[arg-type]
    assert paginator.cursor is None
    assert paginator.has_more is True

    ait = paginator.__aiter__()
    assert ait.has_more is True  # type: ignore[attr-defined]
    assert await ait.__anext__() == "q1"
    assert ait.has_more is True  # type: ignore[attr-defined]
    assert await ait.__anext__() == "q2"
    assert ait.has_more is False  # type: ignore[attr-defined]
    assert paginator.has_more is False
    with pytest.raises(StopAsyncIteration):
        await ait.__anext__()
    assert paginator.cursor is None
    assert paginator.has_more is False
    assert client.calls == [{"path": "/queries", "params": {"studyKey": "S1"}}]

    nested_pairs: list[tuple[str, str]] = []
    async for outer in paginator:
        async for inner in paginator:
            nested_pairs.append((outer, inner))
    assert nested_pairs == [("q1", "q1"), ("q1", "q2"), ("q2", "q1"), ("q2", "q2")]
