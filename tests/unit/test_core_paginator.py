"""Unit tests for core paginator."""

from typing import Any

import pytest

from imednet.core.paginator import (
    AsyncJsonListPaginator,
    AsyncPaginator,
    JsonListPaginator,
    Paginator,
)


class DummyClient:
    """Test suite for DummyClient."""

    def __init__(self, responses: list[dict[str, Any]]):
        """Initialize the test object."""
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(self, path: str, params: dict[str, Any] | None = None):
        """Helper function to get."""
        self.calls.append({"path": path, "params": params})
        data = self.responses.pop(0)
        return type("Resp", (), {"json": lambda self: data})()


class AsyncDummyClient:
    """Test suite for AsyncDummyClient."""

    def __init__(self, responses: list[dict[str, Any]]):
        """Initialize the test object."""
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None):
        """Helper function to get."""
        self.calls.append({"path": path, "params": params})
        data = self.responses.pop(0)
        return type("Resp", (), {"json": lambda self: data})()


def test_single_page_iteration() -> None:
    """Test that single page iteration."""
    client = DummyClient([{"data": [1, 2]}])
    paginator = Paginator(client, "/p")
    assert list(paginator) == [1, 2]
    assert client.calls[0]["params"]["page"] == 0


def test_multiple_page_iteration() -> None:
    """Test that multiple page iteration."""
    client = DummyClient(
        [
            {"data": [1], "pagination": {"totalPages": 2}},
            {"data": [2], "pagination": {"totalPages": 2}},
        ]
    )
    paginator = Paginator(client, "/p", params={"a": 1}, page_size=10)
    items = list(paginator)
    assert items == [1, 2]
    assert client.calls[0]["params"] == {"a": 1, "page": 0, "size": 10}
    assert client.calls[1]["params"] == {"a": 1, "page": 1, "size": 10}


def test_empty_result() -> None:
    """Test that an empty data list returns an empty iterator."""
    client = DummyClient([{"data": []}])
    paginator = Paginator(client, "/p")
    assert list(paginator) == []


def test_custom_keys() -> None:
    """Test that custom data keys work."""
    client = DummyClient([{"custom_data": [1, 2]}])
    paginator = Paginator(client, "/p", data_key="custom_data")
    assert list(paginator) == [1, 2]


@pytest.mark.asyncio
async def test_async_paginator() -> None:
    """Test AsyncPaginator iteration."""
    client = AsyncDummyClient(
        [
            {"data": [1], "pagination": {"totalPages": 2}},
            {"data": [2], "pagination": {"totalPages": 2}},
        ]
    )
    paginator = AsyncPaginator(client, "/p", params={"a": 1}, page_size=10)  # type: ignore
    items = [item async for item in paginator]
    assert items == [1, 2]
    assert client.calls[0]["params"] == {"a": 1, "page": 0, "size": 10}
    assert client.calls[1]["params"] == {"a": 1, "page": 1, "size": 10}


@pytest.mark.asyncio
async def test_async_paginator_empty() -> None:
    """Test AsyncPaginator with empty results."""
    client = AsyncDummyClient([{"data": []}])
    paginator = AsyncPaginator(client, "/p")  # type: ignore
    items = [item async for item in paginator]
    assert items == []


def test_pagination_key_is_null() -> None:
    """Test handling of responses where 'pagination' is explicitly null."""
    client = DummyClient([{"data": [1, 2], "pagination": None}])
    paginator = Paginator(client, "/p")
    assert list(paginator) == [1, 2]


def test_invalid_payload_type_raises_error() -> None:
    """Test TypeError is raised when the API response payload is not a dictionary."""
    client = DummyClient([["this is a list, not a dict"]])  # type: ignore
    paginator = Paginator(client, "/p")
    with pytest.raises(TypeError, match="API response must be a dictionary"):
        list(paginator)


def test_invalid_data_type_raises_error() -> None:
    """Test TypeError is raised when the 'data' field is not a list."""
    client = DummyClient([{"data": "not a list"}])
    paginator = Paginator(client, "/p")
    with pytest.raises(TypeError, match="Expected a list of items under key 'data'"):
        list(paginator)


def test_invalid_pagination_type_raises_error() -> None:
    """Test TypeError is raised when the 'pagination' field is not a dictionary."""
    client = DummyClient([{"data": [1, 2], "pagination": "not a dict"}])
    paginator = Paginator(client, "/p")
    with pytest.raises(TypeError, match="Response field 'pagination' must be a dictionary"):
        list(paginator)


def test_json_list_paginator_invalid_payload_type_raises_error() -> None:
    """Test TypeError is raised in JsonListPaginator when the response is not a list."""
    client = DummyClient([{"not a": "list"}])
    paginator = JsonListPaginator(client, "/p")
    with pytest.raises(TypeError, match="API response must be a list"):
        # explicitly consume the generator to trigger the exception
        list(paginator)


@pytest.mark.asyncio
async def test_async_json_list_paginator_invalid_payload_type_raises_error() -> None:
    """Test TypeError is raised in AsyncJsonListPaginator when the response is not a list."""
    client = AsyncDummyClient([{"not a": "list"}])
    paginator = AsyncJsonListPaginator(client, "/p")  # type: ignore
    with pytest.raises(TypeError, match="API response must be a list"):
        # explicitly consume the async generator to trigger the exception
        [item async for item in paginator]


def test_json_list_paginator_valid_list() -> None:
    """Test JsonListPaginator yields items correctly from a list."""
    client = DummyClient([[1, 2, 3]])
    paginator = JsonListPaginator(client, "/p")
    assert list(paginator) == [1, 2, 3]


@pytest.mark.asyncio
async def test_async_json_list_paginator_valid_list() -> None:
    """Test AsyncJsonListPaginator yields items correctly from a list."""
    client = AsyncDummyClient([[1, 2, 3]])
    paginator = AsyncJsonListPaginator(client, "/p")  # type: ignore
    items = [item async for item in paginator]
    assert items == [1, 2, 3]


@pytest.mark.parametrize(
    ("params", "page_size"),
    [
        ({"page": 0, "size": 1}, 100),
        ({"page": 5, "size": 500}, 100),
        ({}, 1),
        ({}, 500),
    ],
)
def test_paginator_accepts_valid_bounds(params: dict[str, Any], page_size: int) -> None:
    """Test Paginator and AsyncPaginator accept page >= 0 and 1 <= size <= 500."""
    client = DummyClient([{"data": [1], "pagination": {"totalPages": 6}}])
    paginator = Paginator(client, "/p", params=params, page_size=page_size)
    if "page" in params:
        assert list(paginator) == [1]
    else:
        single_client = DummyClient([{"data": [1], "pagination": {"totalPages": 1}}])
        single_paginator = Paginator(single_client, "/p", params=params, page_size=page_size)
        assert list(single_paginator) == [1]


@pytest.mark.parametrize(
    ("params", "page_size", "match"),
    [
        ({"page": -1}, 100, "page.*>= 0"),
        ({"size": 0}, 100, "size.*between 1 and 500"),
        ({"size": -10}, 100, "size.*between 1 and 500"),
        ({"size": 501}, 100, "size.*between 1 and 500"),
        ({}, 0, "size.*between 1 and 500"),
        ({}, 501, "size.*between 1 and 500"),
        ({"page": True}, 100, "must be an integer"),
        ({"size": False}, 100, "must be an integer"),
        ({"page": "abc"}, 100, "must be an integer"),
    ],
)
def test_paginator_rejects_invalid_bounds(
    params: dict[str, Any], page_size: int, match: str
) -> None:
    """Test Paginator and AsyncPaginator reject out-of-range page and size values."""
    client = DummyClient([{"data": [1]}])
    with pytest.raises(ValueError, match=match):
        Paginator(client, "/p", params=params, page_size=page_size)

    async_client = AsyncDummyClient([{"data": [1]}])
    with pytest.raises(ValueError, match=match):
        AsyncPaginator(async_client, "/p", params=params, page_size=page_size)  # type: ignore


@pytest.mark.parametrize(
    ("params", "expected_size"),
    [
        ({"page": 0, "size": 2}, 2),
        ({"limit": 2}, 2),
        ({"size": 2, "limit": 2}, 2),
    ],
)
def test_paginator_explicit_page_or_limit_bounds_to_single_page(
    params: dict[str, Any], expected_size: int
) -> None:
    """Test explicit page or limit in params restricts iteration to one page without leaking limit."""
    sync_client = DummyClient(
        [
            {"data": [1, 2, 3], "pagination": {"totalPages": 4}},
            {"data": [4, 5], "pagination": {"totalPages": 4}},
        ]
    )
    paginator = Paginator(sync_client, "/p", params=params, page_size=100)
    assert paginator.page_size == expected_size
    assert list(paginator) == [1, 2]
    assert len(sync_client.calls) == 1
    assert sync_client.calls[0]["params"] == {"page": 0, "size": expected_size}


@pytest.mark.asyncio
async def test_async_paginator_explicit_limit_bounds_to_single_page() -> None:
    """Test AsyncPaginator restricts iteration to one page when limit is explicitly provided."""
    async_client = AsyncDummyClient(
        [
            {"data": [10, 20, 30], "pagination": {"totalPages": 3}},
            {"data": [40], "pagination": {"totalPages": 3}},
        ]
    )
    paginator = AsyncPaginator(async_client, "/p", params={"limit": 2}, page_size=100)  # type: ignore[arg-type]
    assert paginator.page_size == 2
    assert [item async for item in paginator] == [10, 20]
    assert len(async_client.calls) == 1
    assert async_client.calls[0]["params"] == {"page": 0, "size": 2}


def test_paginator_rejects_conflicting_size_and_limit() -> None:
    """Test Paginator rejects conflicting explicit size and limit values."""
    client = DummyClient([{"data": [1]}])
    with pytest.raises(ValueError, match="Conflicting pagination bounds"):
        Paginator(client, "/p", params={"size": 10, "limit": 25})
