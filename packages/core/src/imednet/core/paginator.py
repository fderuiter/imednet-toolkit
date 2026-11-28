"""Pagination helpers for iterating through API responses."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from imednet.constants import LARGE_PAGE_SIZE
from imednet.core.protocols import AsyncRequesterProtocol, RequesterProtocol
from imednet.errors.client import PaginationError

MAX_PAGE_SIZE = LARGE_PAGE_SIZE

ClientT = TypeVar("ClientT", RequesterProtocol, AsyncRequesterProtocol)


def _parse_int_param(name: str, value: Any) -> int:
    """Parse an integer pagination parameter, rejecting booleans, floats, and non-integers."""
    if isinstance(value, (bool, float)):
        raise ValueError(f"Pagination '{name}' must be an integer, got {type(value).__name__}.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Pagination '{name}' must be an integer, got {value!r}.") from exc


@dataclass
class PaginatorCursorState:
    """Isolated cursor and remaining-item state owned by a single pagination iterator."""

    cursor: int | None = None
    remaining_in_page: int = 0
    remaining_limit: int | None = None
    started: bool = False

    @property
    def has_more(self) -> bool:
        """Return True before iteration starts or while buffered/subsequent items remain."""
        if not self.started:
            return True
        return self.remaining_in_page > 0 or self.cursor is not None

    def mark_exhausted(self) -> None:
        """Mark this iterator state as started with no remaining items or pages."""
        self.started = True
        self.cursor = None
        self.remaining_in_page = 0


class _PaginatorIterator(Iterator[Any]):
    """Synchronous per-iterator wrapper owning an isolated ``PaginatorCursorState``."""

    def __init__(
        self,
        paginator: BasePaginator[Any],
        state: PaginatorCursorState,
        factory: Callable[[PaginatorCursorState], Iterator[Any]],
    ) -> None:
        self._paginator = paginator
        self.state = state
        self._factory = factory
        self._gen: Iterator[Any] | None = None

    @property
    def cursor(self) -> int | None:
        """Return this iterator's next page cursor, or ``None`` before start and after exhaustion."""
        return self.state.cursor

    @property
    def has_more(self) -> bool:
        """Return True if this iterator has not started or has remaining items."""
        return self.state.has_more

    def __iter__(self) -> _PaginatorIterator:
        return self

    def __next__(self) -> Any:
        self._paginator._state = self.state
        if self._gen is None:
            self._gen = self._factory(self.state)
        try:
            return next(self._gen)
        except StopIteration:
            self.state.mark_exhausted()
            raise

    def close(self) -> None:
        """Close the underlying generator and mark this iterator state exhausted."""
        if self._gen is not None and hasattr(self._gen, "close"):
            self._gen.close()
        self.state.mark_exhausted()


class _AsyncPaginatorIterator(AsyncIterator[Any]):
    """Asynchronous per-iterator wrapper owning an isolated ``PaginatorCursorState``."""

    def __init__(
        self,
        paginator: BasePaginator[Any],
        state: PaginatorCursorState,
        factory: Callable[[PaginatorCursorState], AsyncIterator[Any]],
    ) -> None:
        self._paginator = paginator
        self.state = state
        self._factory = factory
        self._agen: AsyncIterator[Any] | None = None

    @property
    def cursor(self) -> int | None:
        """Return this iterator's next page cursor, or ``None`` before start and after exhaustion."""
        return self.state.cursor

    @property
    def has_more(self) -> bool:
        """Return True if this iterator has not started or has remaining items."""
        return self.state.has_more

    def __aiter__(self) -> _AsyncPaginatorIterator:
        return self

    async def __anext__(self) -> Any:
        self._paginator._state = self.state
        if self._agen is None:
            self._agen = self._factory(self.state)
        try:
            return await self._agen.__anext__()
        except StopAsyncIteration:
            self.state.mark_exhausted()
            raise

    async def aclose(self) -> None:
        """Close the underlying async generator and mark this iterator state exhausted."""
        if self._agen is not None and hasattr(self._agen, "aclose"):
            await self._agen.aclose()
        self.state.mark_exhausted()


class BasePaginator(Generic[ClientT]):
    """Shared paginator implementation."""

    #: Active number of items requested per page.
    page_size: int

    def __init__(
        self,
        client: ClientT,
        path: str,
        params: dict[str, Any] | None = None,
        page_size: int = 100,
        page_param: str = "page",
        size_param: str = "size",
        data_key: str = "data",
        metadata_key: str = "metadata",
    ) -> None:
        """Initialize the paginator.

        Args:
            client: Requester instance (sync or async).
            path: API path for the request.
            params: Query parameters.
            page_size: Number of items per page (1 to 500).
            page_param: Query parameter name for the page index.
            size_param: Query parameter name for the page size.
            data_key: Key in the response JSON containing the items list.
            metadata_key: Key in the response JSON containing pagination metadata.
        """
        self.client: ClientT = client
        self.path = path
        self.params = params.copy() if params else {}

        self.page_param = page_param
        self.size_param = size_param
        self.data_key = data_key
        self.metadata_key = metadata_key

        explicit_page = self.params.pop(self.page_param, None)
        if explicit_page is not None:
            parsed_page = _parse_int_param(self.page_param, explicit_page)
            if parsed_page < 0:
                raise ValueError(f"Pagination '{self.page_param}' must be >= 0, got {parsed_page}.")
            self._explicit_page: int | None = parsed_page
        else:
            self._explicit_page = None

        explicit_size = self.params.pop(self.size_param, None)
        explicit_limit = self.params.pop("limit", None)
        if explicit_limit is not None:
            parsed_limit = _parse_int_param("limit", explicit_limit)
            if parsed_limit < 1:
                raise ValueError(f"Pagination 'limit' must be >= 1, got {parsed_limit}.")
            self._explicit_limit: int | None = parsed_limit
        else:
            self._explicit_limit = None

        if explicit_size is not None:
            raw_size: Any = explicit_size
        elif self._explicit_limit is not None:
            raw_size = min(self._explicit_limit, MAX_PAGE_SIZE)
        else:
            raw_size = page_size
        parsed_size = _parse_int_param(self.size_param, raw_size)
        if parsed_size < 1 or parsed_size > MAX_PAGE_SIZE:
            raise ValueError(
                f"Pagination '{self.size_param}' must be between 1 and {MAX_PAGE_SIZE}, got {parsed_size}."
            )
        self.page_size = parsed_size
        self._single_page_slice: int | None = (
            parsed_size if self._explicit_page is not None and explicit_size is not None else None
        )
        self._state = self._new_cursor_state()

    def _new_cursor_state(self) -> PaginatorCursorState:
        """Create a fresh per-iterator cursor state initialized from configured bounds."""
        return PaginatorCursorState(remaining_limit=self._explicit_limit)

    def _start_page_cursor(self, state: PaginatorCursorState) -> None:
        """Activate a cursor state at the initial page before the first page fetch."""
        state.started = True
        state.cursor = self._explicit_page if self._explicit_page is not None else 0
        state.remaining_in_page = 0
        state.remaining_limit = self._explicit_limit

    @property
    def cursor(self) -> int | None:
        """The next page cursor (0-based page index), or ``None`` before start and when exhausted."""
        return self._state.cursor

    @property
    def has_more(self) -> bool:
        """Return True before iteration starts or while unconsumed items/pages remain."""
        return self._state.has_more

    def _build_params(self, page: int) -> dict[str, Any]:
        """Build the query parameters for a specific page."""
        query = dict(self.params)
        query[self.page_param] = page
        query[self.size_param] = self.page_size
        return query

    def _extract_items(self, payload: dict[str, Any]) -> list[Any]:
        """Extract item list from the API response payload."""
        if not isinstance(payload, dict):
            raise TypeError(f"API response must be a dictionary, got {type(payload).__name__}")

        if "recordData" in payload:
            items = payload.get("recordData", []) or []
        else:
            items = payload.get(self.data_key, []) or []

        if not isinstance(items, list):
            raise TypeError(
                f"Expected a list of items under key '{self.data_key}', got {type(items).__name__}"
            )
        return items

    def _next_page(self, payload: dict[str, Any], page: int, items_count: int) -> int | None:
        """Determine the next page index based on the response payload and metadata."""
        if self._explicit_page is not None:
            return None

        pagination = payload.get("pagination")
        if pagination is not None and not isinstance(pagination, dict):
            raise TypeError(
                f"Response field 'pagination' must be a dictionary, got {type(pagination).__name__}"
            )
        pagination = pagination or {}
        total_pages = pagination.get("totalPages")
        if total_pages is None:
            if items_count >= self.page_size:
                raise PaginationError(
                    "Response pagination metadata is missing required 'totalPages' cursor."
                )
            return None
        if isinstance(total_pages, bool) or not isinstance(total_pages, int):
            raise PaginationError(
                "Response pagination cursor 'totalPages' must be an integer, "
                f"got {type(total_pages).__name__}."
            )
        if total_pages < 0:
            raise PaginationError("Response pagination cursor 'totalPages' cannot be negative.")
        if total_pages == 0:
            if items_count > 0:
                raise PaginationError(
                    "Response pagination cursor 'totalPages' cannot be 0 when items are present."
                )
            return None
        if page >= total_pages:
            raise PaginationError(
                "Response pagination cursor 'totalPages' is inconsistent with the current page."
            )
        if page >= total_pages - 1:
            return None
        return page + 1

    def _get_page_params(self, state: PaginatorCursorState | None = None) -> dict[str, Any] | None:
        """Return parameters for the active iterator's current page or None if exhausted."""
        active = state if state is not None else self._state
        if active.cursor is None:
            return None
        return self._build_params(active.cursor)

    def _process_page_response(
        self,
        payload: dict[str, Any],
        state: PaginatorCursorState | None = None,
    ) -> list[Any]:
        """Process payload, update iterator cursor state, and return bounded page items."""
        active = state if state is not None else self._state
        if active.cursor is None:
            return []
        raw_items = self._extract_items(payload)
        next_cursor = self._next_page(payload, active.cursor, len(raw_items))
        items = raw_items
        if self._single_page_slice is not None:
            items = items[: self._single_page_slice]
        if active.remaining_limit is not None:
            if len(items) > active.remaining_limit:
                items = items[: active.remaining_limit]
            active.remaining_limit -= len(items)
            if active.remaining_limit <= 0:
                next_cursor = None
        active.cursor = next_cursor
        return items

    def _process_json_list_response(
        self,
        payload: Any,
        state: PaginatorCursorState | None = None,
    ) -> list[Any]:
        """Validate and slice a raw JSON list response according to iterator limit state."""
        if not isinstance(payload, list):
            raise TypeError(f"API response must be a list, got {type(payload).__name__}")
        active = state if state is not None else self._state
        active.started = True
        active.cursor = None
        limit = active.remaining_limit if state is not None else self._explicit_limit
        if limit is not None:
            items = payload[:limit]
            active.remaining_limit = max(0, limit - len(items))
            return items
        return payload


def _merge_limit_param(params: dict[str, Any] | None, limit: int | None) -> dict[str, Any] | None:
    """Merge an explicit keyword ``limit`` into ``params`` prior to ``BasePaginator`` parsing."""
    if limit is None:
        return params
    merged = dict(params) if params else {}
    merged["limit"] = limit
    return merged


class Paginator(BasePaginator[RequesterProtocol]):
    """Iterate synchronously over paginated API results.

    The requester owns retries, circuit breaking and HTTP telemetry.
    """

    def _iterate_pages(self, state: PaginatorCursorState) -> Iterator[Any]:
        self._start_page_cursor(state)
        while state.cursor is not None:
            response = self.client.get(self.path, params=self._get_page_params(state))
            items = self._process_page_response(response.json(), state)
            total_in_page = len(items)
            for idx, item in enumerate(items):
                state.remaining_in_page = total_in_page - idx - 1
                yield item
            state.remaining_in_page = 0
        state.mark_exhausted()

    def __iter__(self) -> Iterator[Any]:
        """Return an isolated synchronous iterator over all items across pages."""
        state = self._new_cursor_state()
        self._state = state
        return _PaginatorIterator(self, state, self._iterate_pages)


class AsyncPaginator(BasePaginator[AsyncRequesterProtocol]):
    """Asynchronous variant of :class:`Paginator`."""

    async def _iterate_pages_async(self, state: PaginatorCursorState) -> AsyncIterator[Any]:
        self._start_page_cursor(state)
        while state.cursor is not None:
            response = await self.client.get(self.path, params=self._get_page_params(state))
            items = self._process_page_response(response.json(), state)
            total_in_page = len(items)
            for idx, item in enumerate(items):
                state.remaining_in_page = total_in_page - idx - 1
                yield item
            state.remaining_in_page = 0
        state.mark_exhausted()

    def __aiter__(self) -> AsyncIterator[Any]:
        """Return an isolated asynchronous iterator over all items across pages."""
        state = self._new_cursor_state()
        self._state = state
        return _AsyncPaginatorIterator(self, state, self._iterate_pages_async)


class JsonListPaginator(Paginator):
    """Paginator for endpoints returning a raw list."""

    def __init__(
        self,
        client: RequesterProtocol,
        path: str,
        params: dict[str, Any] | None = None,
        page_size: int = 100,
        page_param: str = "page",
        size_param: str = "size",
        data_key: str = "data",
        metadata_key: str = "metadata",
        *,
        limit: int | None = None,
    ) -> None:
        """Initialize a raw JSON list paginator with optional client-side ``limit``."""
        super().__init__(
            client,
            path,
            params=_merge_limit_param(params, limit),
            page_size=page_size,
            page_param=page_param,
            size_param=size_param,
            data_key=data_key,
            metadata_key=metadata_key,
        )

    def _iterate_list(self, state: PaginatorCursorState) -> Iterator[Any]:
        state.started = True
        state.cursor = None
        response = self.client.get(self.path, params=self.params)
        items = self._process_json_list_response(response.json(), state)
        total_in_page = len(items)
        for idx, item in enumerate(items):
            state.remaining_in_page = total_in_page - idx - 1
            yield item
        state.mark_exhausted()

    def __iter__(self) -> Iterator[Any]:
        """Iterate over a single response returning a raw JSON list."""
        state = self._new_cursor_state()
        self._state = state
        return _PaginatorIterator(self, state, self._iterate_list)


class AsyncJsonListPaginator(AsyncPaginator):
    """Asynchronous variant of :class:`JsonListPaginator`."""

    def __init__(
        self,
        client: AsyncRequesterProtocol,
        path: str,
        params: dict[str, Any] | None = None,
        page_size: int = 100,
        page_param: str = "page",
        size_param: str = "size",
        data_key: str = "data",
        metadata_key: str = "metadata",
        *,
        limit: int | None = None,
    ) -> None:
        """Initialize an async raw JSON list paginator with optional client-side ``limit``."""
        super().__init__(
            client,
            path,
            params=_merge_limit_param(params, limit),
            page_size=page_size,
            page_param=page_param,
            size_param=size_param,
            data_key=data_key,
            metadata_key=metadata_key,
        )

    async def _iterate_list_async(self, state: PaginatorCursorState) -> AsyncIterator[Any]:
        state.started = True
        state.cursor = None
        response = await self.client.get(self.path, params=self.params)
        items = self._process_json_list_response(response.json(), state)
        total_in_page = len(items)
        for idx, item in enumerate(items):
            state.remaining_in_page = total_in_page - idx - 1
            yield item
        state.mark_exhausted()

    def __aiter__(self) -> AsyncIterator[Any]:
        """Iterate asynchronously over a single response returning a raw JSON list."""
        state = self._new_cursor_state()
        self._state = state
        return _AsyncPaginatorIterator(self, state, self._iterate_list_async)
