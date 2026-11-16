"""Protocols defining the interfaces for API endpoints and operations."""

from collections.abc import AsyncIterator, Iterator
from typing import (
    Any,
    Protocol,
    TypeVar,
)

from imednet.utils.typing import FilterValue, ItemId

T_co = TypeVar("T_co", covariant=True)


class SupportsGet(Protocol[T_co]):
    """Protocol for resources that support ``get`` operations."""

    def get(self, study_key: str | None, item_id: ItemId) -> T_co:
        """Get a single item synchronously."""
        ...

    async def async_get(self, study_key: str | None, item_id: ItemId) -> T_co:
        """Get a single item asynchronously."""
        ...


class SupportsList(Protocol[T_co]):
    """Protocol for resources that support ``list`` operations."""

    def list(self, study_key: str | None = None, **filters: FilterValue) -> Iterator[T_co]:
        """List items synchronously."""
        ...

    def async_list(
        self, study_key: str | None = None, **filters: FilterValue
    ) -> AsyncIterator[T_co]:
        """List items asynchronously."""
        ...


class SupportsCreate(Protocol[T_co]):
    """Protocol for resources that support ``create`` operations."""

    def create(self, *args: Any, **kwargs: Any) -> T_co:
        """Create an item synchronously."""
        ...

    async def async_create(self, *args: Any, **kwargs: Any) -> T_co:
        """Create an item asynchronously."""
        ...
