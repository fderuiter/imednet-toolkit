"""Base endpoint mix-in for all API resource endpoints."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from typing import Any, TypeVar

from imednet.constants import DEFAULT_PAGE_SIZE
from imednet.core.endpoint.abc import EndpointABC
from imednet.core.endpoint.dispatch import (
    AsyncEndpointContext,
    SyncEndpointContext,
    execute_get,
    execute_list,
)
from imednet.core.endpoint.operations import FilterGetOperation, ListOperation
from imednet.core.endpoint.strategies import (
    DefaultParamProcessor,
    KeepStudyKeyStrategy,
    OptionalStudyKeyStrategy,
    StudyKeyStrategy,
)
from imednet.core.endpoint.structs import ListRequestState, ParamState
from imednet.core.paginator import AsyncPaginator, Paginator
from imednet.core.parsing import get_model_parser
from imednet.core.protocols import AsyncRequesterProtocol, ParamProcessor, RequesterProtocol
from imednet.models.base import ImednetBaseModel
from imednet.utils.filters import build_filter_string
from imednet.utils.typing import FilterValue, ItemId
from imednet.utils.url import build_safe_path

T = TypeVar("T", bound=ImednetBaseModel)


class GenericEndpoint(EndpointABC[T]):
    """Generic base for endpoint wrappers.

    Handles context injection and basic path building.
    Does NOT include EDC-specific logic.
    """

    BASE_PATH = ""
    _client: RequesterProtocol | None
    _async_client: AsyncRequesterProtocol | None

    def __init__(
        self,
        client: RequesterProtocol | None = None,
        async_client: AsyncRequesterProtocol | None = None,
    ) -> None:
        """Initialize the generic endpoint.

        Args:
            client: Synchronous requester instance.
            async_client: Asynchronous requester instance.
        """
        self._client = client
        self._async_client = async_client

    def _auto_filter(self, filters: dict[str, Any]) -> dict[str, Any]:
        """Pass-through for filters in generic endpoints."""
        return filters

    def _build_path(self, *segments: Any) -> str:
        """Return an API path joined with :data:`BASE_PATH`.

        Args:
            *segments: URL path segments to append.

        Returns:
            The full API path string.
        """
        return "/" + build_safe_path(self.BASE_PATH, *segments)

    def _require_sync_client(self) -> RequesterProtocol:
        """Return the configured sync client or raise if missing."""
        if self._client is None:
            raise RuntimeError("Sync client not configured")
        return self._client

    def _require_async_client(self) -> AsyncRequesterProtocol:
        """Return the configured async client or raise if missing."""
        if self._async_client is None:
            raise RuntimeError("Async client not configured")
        return self._async_client


class _ListGetEndpointBase(GenericEndpoint[T]):
    """Generic base for endpoints that provide list and get-by-filter functionality.

    Uses composable operations to provide standard list/get read operations.
    """

    PAGE_SIZE: int = DEFAULT_PAGE_SIZE
    PAGINATOR_CLS: type[Paginator] = Paginator
    ASYNC_PAGINATOR_CLS: type[AsyncPaginator] = AsyncPaginator
    PARAM_PROCESSOR: ParamProcessor | None = None
    PARAM_PROCESSOR_CLS: type[ParamProcessor] = DefaultParamProcessor
    STUDY_KEY_STRATEGY: StudyKeyStrategy | None = None

    @property
    def study_key_strategy(self) -> StudyKeyStrategy:
        """Return the strategy for handling study keys in this endpoint."""
        if self.STUDY_KEY_STRATEGY:
            return self.STUDY_KEY_STRATEGY
        if self.requires_study_key:
            return KeepStudyKeyStrategy()
        return OptionalStudyKeyStrategy()

    @property
    def param_processor(self) -> ParamProcessor:
        """Return the parameter processor for this endpoint."""
        if self.PARAM_PROCESSOR:
            return self.PARAM_PROCESSOR
        return self.PARAM_PROCESSOR_CLS()

    def _parse_item(self, item: Any) -> T:
        """Parse a raw API item into a Pydantic model instance."""
        parse_func = get_model_parser(self.MODEL)
        return parse_func(item)

    def _resolve_parse_func(self) -> Callable[[Any], T]:
        """Return the function used to parse items for this endpoint."""
        return self._parse_item

    def _resolve_params(
        self,
        study_key: str | None,
        extra_params: dict[str, Any] | None,
        filters: dict[str, Any],
    ) -> ParamState:
        """Resolve filters and extra parameters into a ParamState.

        Args:
            study_key: The study key to use.
            extra_params: Additional query parameters.
            filters: Key-value filters to apply.

        Returns:
            A ParamState containing the resolved study, parameters, and other filters.
        """
        filters = self._auto_filter(filters.copy())

        processed_filters, special_params = self.param_processor.process_filters(filters)
        if special_params:
            if extra_params is None:  # noqa: SIM108
                extra_params = {}
            else:
                extra_params = extra_params.copy()
            extra_params.update(special_params)

        if study_key:
            processed_filters["studyKey"] = study_key

        study, processed_filters = self.study_key_strategy.process(processed_filters)
        self._validate_study_key(study)

        other_filters = {k: v for k, v in processed_filters.items() if k != "studyKey"}

        params: dict[str, Any] = {}
        if processed_filters:
            params["filter"] = build_filter_string(processed_filters)
        if extra_params:
            params.update(extra_params)

        return ParamState(study=study, params=params, other_filters=other_filters)

    def _prepare_list_request(
        self,
        study_key: str | None,
        extra_params: dict[str, Any] | None,
        filters: dict[str, Any],
    ) -> ListRequestState[T]:
        """Prepare the state required for a list request.

        Args:
            study_key: Optional study key override.
            extra_params: Additional query parameters.
            filters: Resource filters.

        Returns:
            A ListRequestState containing path and resolved parameters.
        """
        param_state = self._resolve_params(study_key, extra_params, filters)
        study = param_state.study
        params = param_state.params

        path = self._get_endpoint_path(study)
        return ListRequestState(
            path=path,
            params=params,
            study=study,
        )

    def _validate_get_result(self, items: list[T], study_key: str | None, item_id: ItemId) -> T:
        """Validate that a get operation returned exactly one item.

        Args:
            items: The list of items returned by the filter.
            study_key: The study key used for the request.
            item_id: The ID of the item being requested.

        Returns:
            The first item in the list.

        Raises:
            NotFoundError: If the list is empty.
        """
        if not items:
            self._raise_not_found(study_key, item_id)
        return items[0]

    @staticmethod
    def _require_item_id(item_id: ItemId) -> None:
        """Ensure that an item ID was provided."""
        if item_id is None:
            raise TypeError("Missing required argument: item_id")

    @execute_list  # type: ignore
    def list(self, study_key: str | None = None, **filters: FilterValue) -> ListOperation[T]:
        """List resources matching the given filters.

        Args:
            study_key: Optional study key to override the default.
            **filters: Resource filters.

        Returns:
            An iterator over the matching resources.
        """
        _filters: dict[str, Any] = dict(filters)
        state = self._prepare_list_request(study_key, None, _filters)
        from imednet.core.endpoint.operations.list import ListOperation

        return ListOperation[T](
            path=state.path,
            params=state.params,
            page_size=self.PAGE_SIZE,
            parse_func=self._resolve_parse_func(),
        )

    def _list_sync(self, *a: Any, **k: Any) -> builtins.list[T]:
        from imednet.core.endpoint.operations.list import ListOperation

        study_key = k.pop("study_key", None)
        state = self._prepare_list_request(study_key, None, k)
        op = ListOperation[T](
            path=state.path,
            params=state.params,
            page_size=self.PAGE_SIZE,
            parse_func=self._resolve_parse_func(),
        )
        return list(op.execute_sync(self._require_sync_client(), self.PAGINATOR_CLS))

    async def _list_async(self, *a: Any, **k: Any) -> builtins.list[T]:
        from imednet.core.endpoint.operations.list import ListOperation

        study_key = k.pop("study_key", None)
        state = self._prepare_list_request(study_key, None, k)
        op = ListOperation[T](
            path=state.path,
            params=state.params,
            page_size=self.PAGE_SIZE,
            parse_func=self._resolve_parse_func(),
        )
        res = []
        async for item in op.execute_async(self._require_async_client(), self.ASYNC_PAGINATOR_CLS):
            res.append(item)
        return res

    @execute_get
    def get(self, study_key: str | None, item_id: ItemId) -> FilterGetOperation[T]:
        """Retrieve a single resource by its ID.

        Args:
            study_key: The study key.
            item_id: The ID of the resource to retrieve.

        Returns:
            The requested resource.
        """
        self._require_item_id(item_id)
        filters: dict[str, Any] = {self._id_param: item_id}

        from imednet.core.endpoint.operations.filter_get import FilterGetOperation

        return FilterGetOperation[T](
            study_key=study_key,
            item_id=item_id,
            filters=filters,
            validate_func=self._validate_get_result,
            list_sync_func=self._list_sync,
            list_async_func=self._list_async,
        )


class SyncListGetEndpoint(_ListGetEndpointBase[T], SyncEndpointContext):
    """Synchronous endpoint providing list and get functionality."""

    def __init__(
        self,
        client: RequesterProtocol,
    ) -> None:
        """Initialize the synchronous endpoint."""
        super().__init__(client=client)


class AsyncListGetEndpoint(_ListGetEndpointBase[T], AsyncEndpointContext):
    """Asynchronous endpoint providing list and get functionality."""

    def __init__(
        self,
        async_client: AsyncRequesterProtocol,
    ) -> None:
        """Initialize the asynchronous endpoint."""
        super().__init__(async_client=async_client)


# Backward-compatible alias. New code should use SyncListGetEndpoint / AsyncListGetEndpoint.
GenericListGetEndpoint = SyncListGetEndpoint
