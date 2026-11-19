"""Pagination helpers for iterating through API responses."""

from collections.abc import AsyncIterator, Iterator
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


class BasePaginator(Generic[ClientT]):
    """Shared paginator implementation."""

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
        if (
            explicit_size is not None
            and explicit_limit is not None
            and _parse_int_param(self.size_param, explicit_size)
            != _parse_int_param("limit", explicit_limit)
        ):
            raise ValueError(
                f"Conflicting pagination bounds: '{self.size_param}'={explicit_size!r} "
                f"and 'limit'={explicit_limit!r}."
            )
        self._explicit_limit: int | None = (
            _parse_int_param("limit", explicit_limit) if explicit_limit is not None else None
        )
        raw_size = (
            explicit_size
            if explicit_size is not None
            else (explicit_limit if explicit_limit is not None else page_size)
        )
        parsed_size = _parse_int_param(self.size_param, raw_size)
        if parsed_size < 1 or parsed_size > MAX_PAGE_SIZE:
            raise ValueError(
                f"Pagination '{self.size_param}' must be between 1 and {MAX_PAGE_SIZE}, got {parsed_size}."
            )
        self.page_size = parsed_size
        self._single_page_slice: int | None = (
            self._explicit_limit
            if self._explicit_limit is not None
            else (
                parsed_size
                if self._explicit_page is not None and explicit_size is not None
                else None
            )
        )
        self._cursor: int | None = None
        self._remaining_in_page: int = 0
        self._exhausted = False

    @property
    def cursor(self) -> int | None:
        """The next page cursor (0-based page index), or ``None`` when exhausted."""
        return self._cursor

    @property
    def has_more(self) -> bool:
        """Return True if unconsumed items remain in the current page or subsequent pages."""
        return self._remaining_in_page > 0 or self._cursor is not None

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
        if self._explicit_page is not None or self._explicit_limit is not None:
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

    def _get_page_params(self) -> dict[str, Any] | None:
        """Return parameters for the current page or None if exhausted."""
        if self._cursor is None:
            return None
        return self._build_params(self._cursor)

    def _process_page_response(self, payload: dict[str, Any]) -> list[Any]:
        """Process payload, update cursor, and return items."""
        if self._cursor is None:
            return []
        items = self._extract_items(payload)
        self._cursor = self._next_page(payload, self._cursor, len(items))
        if self._single_page_slice is not None:
            return items[: self._single_page_slice]
        return items

    def _process_json_list_response(self, payload: Any) -> list[Any]:
        """Process raw list response."""
        if not isinstance(payload, list):
            raise TypeError(f"API response must be a list, got {type(payload).__name__}")
        return payload


class Paginator(BasePaginator[RequesterProtocol]):
    """Iterate synchronously over paginated API results.

    The requester owns retries, circuit breaking and HTTP telemetry.
    """

    def __iter__(self) -> Iterator[Any]:
        """Iterate over all items across all pages."""
        self._cursor = self._explicit_page if self._explicit_page is not None else 0
        self._remaining_in_page = 0
        while self._cursor is not None:
            response = self.client.get(self.path, params=self._get_page_params())
            items = self._process_page_response(response.json())
            total_in_page = len(items)
            for idx, item in enumerate(items):
                self._remaining_in_page = total_in_page - idx - 1
                yield item
            self._remaining_in_page = 0


class AsyncPaginator(BasePaginator[AsyncRequesterProtocol]):
    """Asynchronous variant of :class:`Paginator`."""

    async def __aiter__(self) -> AsyncIterator[Any]:
        """Iterate asynchronously over all items across all pages."""
        self._cursor = self._explicit_page if self._explicit_page is not None else 0
        self._remaining_in_page = 0
        while self._cursor is not None:
            response = await self.client.get(self.path, params=self._get_page_params())
            items = self._process_page_response(response.json())
            total_in_page = len(items)
            for idx, item in enumerate(items):
                self._remaining_in_page = total_in_page - idx - 1
                yield item
            self._remaining_in_page = 0


class JsonListPaginator(Paginator):
    """Paginator for endpoints returning a raw list."""

    def __iter__(self) -> Iterator[Any]:
        """Iterate over a single response that returns a list directly."""
        response = self.client.get(self.path, params=self.params)
        yield from self._process_json_list_response(response.json())


class AsyncJsonListPaginator(AsyncPaginator):
    """Asynchronous variant of :class:`JsonListPaginator`."""

    async def __aiter__(self) -> AsyncIterator[Any]:
        """Iterate asynchronously over a single response that returns a list directly."""
        response = await self.client.get(self.path, params=self.params)
        for item in self._process_json_list_response(response.json()):
            yield item
