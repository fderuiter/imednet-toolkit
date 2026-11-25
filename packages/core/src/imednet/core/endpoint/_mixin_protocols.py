"""Internal structural protocols for endpoint mixins and dispatch descriptors.

Isolating these protocols in a dedicated private module keeps ``OperationDef``
mixins decoupled from ``base.py`` and ``dispatch.py`` implementation churn.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Concatenate,
    Literal,
    NoReturn,
    ParamSpec,
    Protocol,
    TypeVar,
    overload,
)

from imednet.core.endpoint.strategies import StudyKeyStrategy
from imednet.core.endpoint.structs import ListRequestState
from imednet.core.paginator import AsyncPaginator, Paginator
from imednet.core.protocols import AsyncRequesterProtocol, ParamProcessor, RequesterProtocol
from imednet.models.base import ImednetBaseModel
from imednet.utils.typing import ItemId

if TYPE_CHECKING:
    from imednet.core.endpoint.dispatch import AsyncEndpointContext, SyncEndpointContext

T = TypeVar("T", bound=ImednetBaseModel)
T_co = TypeVar("T_co", covariant=True)
P = ParamSpec("P")
_SyncFn_co = TypeVar("_SyncFn_co", covariant=True)
_AsyncFn_co = TypeVar("_AsyncFn_co", covariant=True)


class SyncSelfProtocol(Protocol[T_co]):  # pragma: no cover - static typing contract
    """Structural self protocol for synchronous endpoint methods."""

    _endpoint_mode: ClassVar[Literal["sync"]]

    @property
    def MODEL(self) -> type[T_co]:
        """Return the endpoint model class."""
        ...


class AsyncSelfProtocol(Protocol[T_co]):  # pragma: no cover - static typing contract
    """Structural self protocol for asynchronous endpoint methods."""

    _endpoint_mode: ClassVar[Literal["async"]]

    @property
    def MODEL(self) -> type[T_co]:
        """Return the endpoint model class."""
        ...


class MethodDescriptorProtocol(
    Protocol[T_co, _SyncFn_co, _AsyncFn_co]
):  # pragma: no cover - static typing contract
    """Structural descriptor protocol preserving callable variance for endpoint methods."""

    @overload
    def __get__(self, instance: SyncEndpointContext, owner: Any) -> _SyncFn_co: ...

    @overload
    def __get__(self, instance: AsyncEndpointContext, owner: Any) -> _AsyncFn_co: ...

    @overload
    def __get__(self, instance: None, owner: Any) -> Callable[..., Any]: ...


class ListMethodDescriptorProtocol(Protocol[P, T_co]):  # pragma: no cover - static typing contract
    """Structural descriptor protocol preserving covariant item types for list operations."""

    @overload
    def __get__(self, instance: SyncEndpointContext, owner: Any) -> Callable[P, Iterator[T_co]]: ...

    @overload
    def __get__(
        self, instance: AsyncEndpointContext, owner: Any
    ) -> Callable[P, AsyncIterator[T_co]]: ...

    @overload
    def __get__(
        self, instance: None, owner: type[AsyncEndpointContext]
    ) -> Callable[Concatenate[Any, P], AsyncIterator[T_co]]: ...

    @overload
    def __get__(
        self, instance: None, owner: type[SyncEndpointContext]
    ) -> Callable[Concatenate[Any, P], Iterator[T_co]]: ...

    @overload
    def __get__(self, instance: None, owner: Any) -> Callable[Concatenate[Any, P], Any]: ...


class EndpointSelfProtocol(Protocol[T]):  # pragma: no cover - static typing contract
    """Structural protocol for endpoint ``OperationDef`` methods accessing base helpers."""

    PATH: ClassVar[str]
    MODEL: type[T]
    _id_param: str
    requires_study_key: bool

    def _validate_study_key(self, study_key: str | None) -> None: ...

    def _get_endpoint_path(self, study_key: str | None, *extra_segments: Any) -> str: ...

    def _raise_not_found(self, study_key: str | None, item_id: Any = None) -> NoReturn: ...

    @staticmethod
    def _require_item_id(item_id: ItemId) -> None: ...

    def _parse_item(self, item: Any) -> T: ...

    def _resolve_parse_func(self) -> Callable[[Any], T]: ...

    def _require_sync_client(self) -> RequesterProtocol: ...

    def _require_async_client(self) -> AsyncRequesterProtocol: ...


class ListEndpointSelfProtocol(
    EndpointSelfProtocol[T], Protocol[T]
):  # pragma: no cover - static typing contract
    """Structural protocol for list-capable ``OperationDef`` methods."""

    PAGE_SIZE: ClassVar[int]
    PAGINATOR_CLS: ClassVar[type[Paginator]]
    ASYNC_PAGINATOR_CLS: ClassVar[type[AsyncPaginator]]
    FILTER_ATTRIBUTES: ClassVar[frozenset[str] | None]

    @property
    def study_key_strategy(self) -> StudyKeyStrategy:
        """Return the study key strategy for this endpoint."""
        ...

    @property
    def param_processor(self) -> ParamProcessor:
        """Return the parameter processor for this endpoint."""
        ...

    def _auto_filter(self, filters: dict[str, Any]) -> dict[str, Any]: ...

    def _validate_filter_attributes(
        self,
        raw_keys: Iterable[str],
        *,
        normalize_snake: bool,
    ) -> None: ...

    def _prepare_list_request(
        self,
        study_key: str | None,
        extra_params: dict[str, Any] | None,
        filters: dict[str, Any],
    ) -> ListRequestState[T]: ...

    def _list_sync(self, *a: Any, **k: Any) -> builtins.list[T]: ...

    async def _list_async(self, *a: Any, **k: Any) -> builtins.list[T]: ...
