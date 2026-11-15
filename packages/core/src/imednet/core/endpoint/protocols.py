"""Protocols defining the interfaces for API endpoints and operations."""

from collections.abc import AsyncIterator, Iterator
from typing import (
    Any,
    Protocol,
    TypeVar,
    runtime_checkable,
)

from imednet.core.paginator import AsyncPaginator, Paginator
from imednet.core.protocols import AsyncRequesterProtocol, RequesterProtocol
from imednet.models.base import ImednetBaseModel
from imednet.utils.typing import FilterValue, ItemId

T = TypeVar("T", bound=ImednetBaseModel)
T_co = TypeVar("T_co", covariant=True)


@runtime_checkable
class EndpointProtocol(Protocol):
    """Protocol defining the interface for endpoint classes."""

    PATH: str
    MODEL: type[ImednetBaseModel]
    _id_param: str
    requires_study_key: bool
    PAGE_SIZE: int

    def _auto_filter(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Apply automatic filters (e.g., default study key)."""
        ...

    def _build_path(self, *segments: Any) -> str:
        """Build the API path."""
        ...

    def _validate_study_key(self, study_key: str | None) -> None:
        """Validate that a study key is provided if required."""
        ...

    def _get_endpoint_path(self, study_key: str | None, *extra_segments: Any) -> str:
        """Build the API path with optional study key and extra segments."""
        ...

    def _raise_not_found(self, study_key: str | None, item_id: ItemId | None = None) -> None:
        """Raise a standardized NotFoundError."""
        ...


@runtime_checkable
class ListEndpointProtocol(Protocol[T_co]):
    """Protocol defining the interface for listing endpoint classes."""

    def _list_sync(
        self,
        client: RequesterProtocol,
        paginator_cls: type[Paginator],
        *,
        study_key: str | None = None,
        extra_params: dict[str, Any] | None = None,
        **filters: Any,
    ) -> Iterator[T]:
        """List items synchronously."""
        ...

    def _list_async(
        self,
        client: AsyncRequesterProtocol,
        paginator_cls: type[AsyncPaginator],
        *,
        study_key: str | None = None,
        extra_params: dict[str, Any] | None = None,
        **filters: Any,
    ) -> AsyncIterator[T]:
        """List items asynchronously."""
        ...


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
