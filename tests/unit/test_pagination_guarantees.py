"""Unit tests for pagination guarantees."""

import itertools

import httpx
import pytest
import respx

from imednet.core.paginator import Paginator
from imednet.errors import PaginationError


@respx.mock
def test_empty_result_set_stops_immediately() -> None:
    """Test that empty result set stops immediately."""
    respx.get("https://api.test/items").mock(
        return_value=httpx.Response(200, json={"data": [], "pagination": {"totalPages": 0}})
    )
    with httpx.Client(base_url="https://api.test") as client:
        paginator = Paginator(client, "/items")
        assert list(paginator) == []
        assert paginator.cursor is None


@respx.mock
def test_single_page_iteration() -> None:
    """Test that single page iteration."""
    respx.get("https://api.test/items").mock(
        return_value=httpx.Response(200, json={"data": [1, 2], "pagination": {"totalPages": 1}})
    )
    with httpx.Client(base_url="https://api.test") as client:
        paginator = Paginator(client, "/items", page_size=50)
        assert paginator.page_size == 50
        assert paginator.cursor is None
        assert list(paginator) == [1, 2]
        assert paginator.cursor is None


@respx.mock
def test_last_page_partial_results() -> None:
    """Test that last page partial results."""
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        """Helper function to responder."""
        page = int(request.url.params["page"])
        calls.append(page)
        if page == 0:
            return httpx.Response(200, json={"data": [1, 2], "pagination": {"totalPages": 2}})
        return httpx.Response(200, json={"data": [3], "pagination": {"totalPages": 2}})

    respx.get("https://api.test/items").mock(side_effect=responder)
    with httpx.Client(base_url="https://api.test") as client:
        paginator = Paginator(client, "/items", page_size=2)
        assert list(paginator) == [1, 2, 3]
        assert calls == [0, 1]


@respx.mock
def test_missing_cursor_raises_typed_error() -> None:
    """Test that missing cursor raises typed error."""
    respx.get("https://api.test/items").mock(
        return_value=httpx.Response(200, json={"data": [1, 2], "pagination": {}})
    )
    with httpx.Client(base_url="https://api.test") as client:
        paginator = Paginator(client, "/items", page_size=2)
        with pytest.raises(PaginationError, match="missing required 'totalPages'"):
            list(paginator)


@respx.mock
def test_malformed_cursor_raises_typed_error() -> None:
    """Test that malformed cursor raises typed error."""
    respx.get("https://api.test/items").mock(
        return_value=httpx.Response(
            200, json={"data": [1], "pagination": {"totalPages": "not-an-integer"}}
        )
    )
    with httpx.Client(base_url="https://api.test") as client:
        paginator = Paginator(client, "/items")
        with pytest.raises(PaginationError, match="must be an integer"):
            list(paginator)


@respx.mock
def test_large_result_set_iteration_is_lazy_and_bounded() -> None:
    """Test that large result set iteration is lazy and bounded."""
    calls: list[int] = []
    total_pages = 100_000

    def responder(request: httpx.Request) -> httpx.Response:
        """Helper function to responder."""
        page = int(request.url.params["page"])
        calls.append(page)
        return httpx.Response(
            200,
            json={"data": [page], "pagination": {"totalPages": total_pages}},
        )

    respx.get("https://api.test/items").mock(side_effect=responder)
    with httpx.Client(base_url="https://api.test") as client:
        paginator = Paginator(client, "/items", page_size=1)
        first_three = list(itertools.islice(paginator, 3))
        assert first_three == [0, 1, 2]
        assert calls == [0, 1, 2]


@respx.mock
def test_iteration_can_be_interrupted_and_resumed() -> None:
    """Test that iteration can be interrupted and resumed."""
    calls: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        """Helper function to responder."""
        page = int(request.url.params["page"])
        calls.append(page)
        if page == 0:
            return httpx.Response(200, json={"data": [1, 2], "pagination": {"totalPages": 2}})
        return httpx.Response(200, json={"data": [3], "pagination": {"totalPages": 2}})

    respx.get("https://api.test/items").mock(side_effect=responder)
    with httpx.Client(base_url="https://api.test") as client:
        paginator = Paginator(client, "/items", page_size=2)
        iterator = iter(paginator)
        assert next(iterator) == 1
        assert list(iterator) == [2, 3]
        assert calls == [0, 1]


@respx.mock
def test_paginator_next_page_advances_cursor_only_after_page_succeeds() -> None:
    """Paginator.next_page preserves _cursor across HTTP errors and advances only on success."""
    from imednet.core.paginator import JsonListPaginator

    attempts: list[int] = []
    failed_pages: set[int] = set()

    def responder(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        attempts.append(page)
        if page == 1 and 1 not in failed_pages:
            failed_pages.add(1)
            return httpx.Response(429, json={"error": "rate limited"})
        if page == 2 and 2 not in failed_pages:
            failed_pages.add(2)
            return httpx.Response(503, json={"error": "unavailable"})
        data = [page * 10 + 1, page * 10 + 2]
        resp = httpx.Response(200, json={"data": data, "pagination": {"totalPages": 3}})
        return resp

    def checking_get(client: httpx.Client, path: str, params: dict[str, int] | None = None):
        resp = client.get(path, params=params)
        resp.raise_for_status()
        return resp

    class _RaisingClient:
        def __init__(self, inner: httpx.Client) -> None:
            self._inner = inner

        def get(self, path: str, params: dict[str, int] | None = None, **kwargs: object):
            del kwargs
            return checking_get(self._inner, path, params=params)

    respx.get("https://api.test/paged").mock(side_effect=responder)
    list_calls = 0

    def list_responder(_request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        list_calls += 1
        if list_calls == 1:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json=[{"id": 1}, {"id": 2}])

    respx.get("https://api.test/raw-list").mock(side_effect=list_responder)

    with httpx.Client(base_url="https://api.test") as raw_client:
        client = _RaisingClient(raw_client)
        paginator = Paginator(client, "/paged", page_size=2)  # type: ignore[arg-type]
        assert paginator.cursor is None
        assert paginator.has_more is False

        assert paginator.next_page() == [1, 2]
        assert paginator.cursor == 1
        assert paginator.has_more is True

        with pytest.raises(httpx.HTTPStatusError):
            paginator.next_page()
        assert paginator.cursor == 1
        assert paginator.has_more is True

        assert paginator.next_page() == [11, 12]
        assert paginator.cursor == 2
        assert paginator.has_more is True

        with pytest.raises(httpx.HTTPStatusError):
            paginator.next_page()
        assert paginator.cursor == 2
        assert paginator.has_more is True

        assert paginator.next_page() == [21, 22]
        assert paginator.cursor is None
        assert paginator.has_more is False
        assert paginator.next_page() == []
        assert attempts == [0, 1, 1, 2, 2]

        json_list_pag = JsonListPaginator(client, "/raw-list")  # type: ignore[arg-type]
        with pytest.raises(httpx.HTTPStatusError):
            json_list_pag.next_page()
        assert json_list_pag.cursor == 0
        assert json_list_pag.has_more is True
        assert json_list_pag.next_page() == [{"id": 1}, {"id": 2}]
        assert json_list_pag.cursor is None
        assert json_list_pag.has_more is False


@respx.mock
def test_paginated_iterator_buffers_partial_page_and_recovers_after_error() -> None:
    """_PaginatedIterator preserves unconsumed partial-page items and retries failed pages."""
    from imednet.core.endpoint.operations.list import ListOperation

    calls: list[int] = []
    failed_once = False

    class _RaisingClient:
        def __init__(self, inner: httpx.Client) -> None:
            self._inner = inner

        def get(self, path: str, params: dict[str, int] | None = None, **kwargs: object):
            del kwargs
            resp = self._inner.get(path, params=params)
            resp.raise_for_status()
            return resp

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal failed_once
        page = int(request.url.params["page"])
        calls.append(page)
        if page == 1 and not failed_once:
            failed_once = True
            return httpx.Response(429, json={"error": "slow down"})
        items = [{"v": page * 4 + i} for i in range(4)]
        return httpx.Response(200, json={"data": items, "pagination": {"totalPages": 2}})

    respx.get("https://api.test/buf").mock(side_effect=responder)
    with httpx.Client(base_url="https://api.test") as raw_client:
        op = ListOperation("/buf", params={}, page_size=4, parse_func=lambda d: d["v"])
        it = op.execute_sync(_RaisingClient(raw_client), Paginator)  # type: ignore[arg-type]
        head = list(itertools.islice(it, 2))
        assert head == [0, 1]
        assert it.has_more is True  # type: ignore[attr-defined]
        assert calls == [0]

        # First next_page drains buffered remainder [2, 3] from page 0 without HTTP call
        buffered_rest = it.next_page()  # type: ignore[attr-defined]
        assert buffered_rest == [2, 3]
        assert calls == [0]
        assert it.has_more is True  # type: ignore[attr-defined]

        # Next step attempts page 1 -> raises 429, preserving cursor at page 1
        with pytest.raises(httpx.HTTPStatusError):
            it.next_page()  # type: ignore[attr-defined]
        assert it.has_more is True  # type: ignore[attr-defined]

        # Retry succeeds and marks iterator exhausted
        assert it.next_page() == [4, 5, 6, 7]  # type: ignore[attr-defined]
        assert it.has_more is False  # type: ignore[attr-defined]
        assert calls == [0, 1, 1]
