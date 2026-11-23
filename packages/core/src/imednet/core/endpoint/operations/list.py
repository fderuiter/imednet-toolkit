"""Operation for executing list requests.

This module encapsulates the logic for fetching and parsing a list of resources
from the API, handling pagination seamlessly.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, Generic, TypeVar

from imednet.core.paginator import AsyncPaginator, Paginator
from imednet.core.protocols import AsyncRequesterProtocol, RequesterProtocol

T = TypeVar("T")


class _PaginatedIterator(Iterator[T]):
    """Synchronous iterator over parsed items with buffered single-page stepping."""

    def __init__(self, paginator: Any, parse_func: Callable[[Any], T]) -> None:
        self._paginator = paginator
        self._parse_func = parse_func
        self._buffer: deque[T] = deque()
        self._started = False
        self._fallback_iter: Iterator[Any] | None = None
        self._fallback_exhausted = False

    def _supports_step_page(self) -> bool:
        """Return True if the underlying paginator exposes synchronous page stepping."""
        return callable(getattr(self._paginator, "next_page", None)) and isinstance(
            getattr(self._paginator, "has_more", None), bool
        )

    @property
    def has_more(self) -> bool:
        """Return True if buffered items or subsequent server pages remain."""
        if self._buffer:
            return True
        if self._fallback_iter is not None:
            return not self._fallback_exhausted and bool(
                getattr(self._paginator, "has_more", False)
            )
        return bool(getattr(self._paginator, "has_more", False))

    def next_page(self) -> list[T]:
        """Return buffered partial-page items first, or fetch and parse the next page."""
        if self._buffer:
            buffered = list(self._buffer)
            self._buffer.clear()
            return buffered

        if self._supports_step_page():
            step_fn = self._paginator.next_page
            self._started = True
            raw_items = step_fn()
            if not raw_items:
                return []
            return [self._parse_func(item) for item in raw_items]

        if self._fallback_exhausted:
            return []
        if self._fallback_iter is None:
            self._fallback_iter = iter(self._paginator)
        batch_size = max(1, int(getattr(self._paginator, "page_size", 100)))
        parsed_batch: list[T] = []
        for _ in range(batch_size):
            try:
                raw_item = next(self._fallback_iter)
            except StopIteration:
                self._fallback_exhausted = True
                break
            parsed_batch.append(self._parse_func(raw_item))
        return parsed_batch

    def __iter__(self) -> _PaginatedIterator[T]:
        return self

    def __next__(self) -> T:
        if not self._supports_step_page():
            if self._fallback_iter is None:
                self._fallback_iter = iter(self._paginator)
            try:
                return self._parse_func(next(self._fallback_iter))
            except StopIteration:
                self._fallback_exhausted = True
                raise

        step_fn = self._paginator.next_page
        sentinel = object()
        while not self._buffer:
            if self._started and not bool(getattr(self._paginator, "has_more", False)):
                raise StopIteration
            was_started = self._started
            prev_cursor = getattr(self._paginator, "cursor", sentinel)
            self._started = True
            raw_items = step_fn()
            if not raw_items:
                curr_cursor = getattr(self._paginator, "cursor", sentinel)
                if not bool(getattr(self._paginator, "has_more", False)) or (
                    was_started and curr_cursor == prev_cursor
                ):
                    raise StopIteration
                continue
            self._buffer.extend(self._parse_func(item) for item in raw_items)
        return self._buffer.popleft()


class _AsyncPaginatedIterator(AsyncIterator[T]):
    """Asynchronous iterator over parsed items exposing pagination truncation state."""

    def __init__(self, paginator: Any, parse_func: Callable[[Any], T]) -> None:
        self._paginator = paginator
        self._parse_func = parse_func
        self._iterator: AsyncIterator[Any] | None = None

    @property
    def has_more(self) -> bool:
        """Return True if more items remain in the underlying async paginator."""
        return bool(getattr(self._paginator, "has_more", False))

    def __aiter__(self) -> _AsyncPaginatedIterator[T]:
        return self

    async def __anext__(self) -> T:
        if self._iterator is None:
            self._iterator = self._paginator.__aiter__()
        item = await self._iterator.__anext__()
        return self._parse_func(item)


class ListOperation(Generic[T]):
    """Operation for executing list requests.

    Encapsulates the logic for setting up a paginator, iterating through pages,
    and parsing the results.
    """

    def __init__(
        self,
        path: str,
        params: dict[str, Any],
        page_size: int,
        parse_func: Callable[[Any], T],
    ) -> None:
        """Initialize the list operation.

        Args:
            path: The API endpoint path.
            params: Query parameters for the request.
            page_size: The number of items per page.
            parse_func: A function to parse a raw JSON item into the model T.
        """
        self.path = path
        self.params = params
        self.page_size = page_size
        self.parse_func = parse_func

    def _process_item(self, item: Any) -> T:
        """Process a single raw item from the paginator."""
        return self.parse_func(item)

    def execute_sync(
        self,
        client: RequesterProtocol,
        paginator_cls: type[Paginator],
    ) -> Iterator[T]:
        """Execute synchronous list request.

        Args:
            client: The synchronous HTTP client.
            paginator_cls: The paginator class to use.

        Returns:
            An iterator of parsed items.
        """
        paginator = paginator_cls(
            client,
            self.path,
            params=self.params,
            page_size=self.page_size,
        )
        return _PaginatedIterator(paginator, self._process_item)

    def execute_async(
        self,
        client: AsyncRequesterProtocol,
        paginator_cls: type[AsyncPaginator],
    ) -> AsyncIterator[T]:
        """Execute asynchronous list request.

        Args:
            client: The asynchronous HTTP client.
            paginator_cls: The async paginator class to use.

        Returns:
            An async iterator of parsed items.
        """
        paginator = paginator_cls(
            client,
            self.path,
            params=self.params,
            page_size=self.page_size,
        )
        return _AsyncPaginatedIterator(paginator, self._process_item)
