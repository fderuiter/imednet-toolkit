"""Endpoint for retrieving record revision history in a study."""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import Any

from imednet.core.endpoint.dispatch import execute_list
from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.operations.list import ListOperation
from imednet.core.paginator import AsyncPaginator, Paginator, _parse_int_param
from imednet.core.protocols import AsyncRequesterProtocol, RequesterProtocol
from imednet.models.record_revisions import RecordRevision
from imednet.utils.filters import matches_revision_date_bounds, split_revision_date_bounds
from imednet.utils.typing import FilterValue


class _BoundedRevisionIterator(Iterator[RecordRevision]):
    """Synchronous iterator applying secondary revision date bounds locally."""

    def __init__(
        self,
        source: Iterator[RecordRevision],
        local_bounds: Sequence[tuple[str, str, Any]],
        limit: int | None = None,
    ) -> None:
        self._source = iter(source)
        self._local_bounds = local_bounds
        self._remaining = limit

    def __iter__(self) -> _BoundedRevisionIterator:
        return self

    def __next__(self) -> RecordRevision:
        if self._remaining is not None and self._remaining <= 0:
            raise StopIteration
        while True:
            item = next(self._source)
            if matches_revision_date_bounds(item, self._local_bounds):
                if self._remaining is not None:
                    self._remaining -= 1
                return item


class _AsyncBoundedRevisionIterator(AsyncIterator[RecordRevision]):
    """Asynchronous iterator applying secondary revision date bounds locally."""

    def __init__(
        self,
        source: AsyncIterator[RecordRevision],
        local_bounds: Sequence[tuple[str, str, Any]],
        limit: int | None = None,
    ) -> None:
        self._source = source.__aiter__()
        self._local_bounds = local_bounds
        self._remaining = limit

    def __aiter__(self) -> _AsyncBoundedRevisionIterator:
        return self

    async def __anext__(self) -> RecordRevision:
        if self._remaining is not None and self._remaining <= 0:
            raise StopAsyncIteration
        while True:
            item = await self._source.__anext__()
            if matches_revision_date_bounds(item, self._local_bounds):
                if self._remaining is not None:
                    self._remaining -= 1
                return item


class _BoundedRevisionListOperation(ListOperation[RecordRevision]):
    """List operation that sends one date bound upstream and evaluates remaining bounds locally."""

    def __init__(
        self,
        path: str,
        params: dict[str, Any],
        page_size: int,
        parse_func: Callable[[Any], RecordRevision],
        *,
        local_bounds: Sequence[tuple[str, str, Any]],
        local_limit: int | None = None,
    ) -> None:
        super().__init__(path=path, params=params, page_size=page_size, parse_func=parse_func)
        self._local_bounds = tuple(local_bounds)
        self._local_limit = local_limit

    def execute_sync(
        self,
        client: RequesterProtocol,
        paginator_cls: type[Paginator],
    ) -> Iterator[RecordRevision]:
        base_iter = super().execute_sync(client, paginator_cls)
        return _BoundedRevisionIterator(base_iter, self._local_bounds, self._local_limit)

    def execute_async(
        self,
        client: AsyncRequesterProtocol,
        paginator_cls: type[AsyncPaginator],
    ) -> AsyncIterator[RecordRevision]:
        base_aiter = super().execute_async(client, paginator_cls)
        return _AsyncBoundedRevisionIterator(base_aiter, self._local_bounds, self._local_limit)


class RecordRevisionsOperationDef:
    """Definition for Record Revision operations."""

    PATH = "recordRevisions"
    MODEL = RecordRevision
    _id_param = "recordRevisionId"
    _lookup_key_hint = "recordOid"
    FILTER_ATTRIBUTES = frozenset(
        {
            "recordRevisionId",
            "recordId",
            "recordOid",
            "recordRevision",
            "dataRevision",
            "recordStatus",
            "subjectId",
            "subjectOid",
            "subjectKey",
            "siteId",
            "formKey",
            "intervalId",
            "role",
            "user",
            "reasonForChange",
            "deleted",
            "dateCreated",
            "lastUpdated",
        }
    )

    def _build_revision_list_operation(
        self,
        study_key: str | None,
        raw_filters: dict[str, Any],
    ) -> ListOperation[RecordRevision]:
        """Build a standard or locally bounded revision list operation."""
        upstream_filters, local_bounds = split_revision_date_bounds(raw_filters)
        if not local_bounds:
            state = self._prepare_list_request(study_key, None, upstream_filters)  # type: ignore[attr-defined]
            return ListOperation[RecordRevision](
                path=state.path,
                params=state.params,
                page_size=self.PAGE_SIZE,  # type: ignore[attr-defined]
                parse_func=self._resolve_parse_func(),  # type: ignore[attr-defined]
            )

        local_limit: int | None = None
        raw_limit = upstream_filters.pop("limit", None)
        if raw_limit is not None:
            parsed_limit = _parse_int_param("limit", raw_limit)
            if parsed_limit < 1:
                raise ValueError(f"Pagination 'limit' must be >= 1, got {parsed_limit}.")
            local_limit = parsed_limit

        state = self._prepare_list_request(study_key, None, upstream_filters)  # type: ignore[attr-defined]
        return _BoundedRevisionListOperation(
            path=state.path,
            params=state.params,
            page_size=self.PAGE_SIZE,  # type: ignore[attr-defined]
            parse_func=self._resolve_parse_func(),  # type: ignore[attr-defined]
            local_bounds=local_bounds,
            local_limit=local_limit,
        )

    @execute_list  # type: ignore[arg-type]
    def list(
        self,
        study_key: str | None = None,
        **filters: FilterValue,
    ) -> ListOperation[RecordRevision]:
        """List record revisions matching filters, splitting secondary date bounds locally."""
        return self._build_revision_list_operation(study_key, dict(filters))

    def _list_sync(self, *a: Any, **k: Any) -> builtins.list[RecordRevision]:
        study_key = k.pop("study_key", None)
        op = self._build_revision_list_operation(study_key, k)
        return builtins.list(
            op.execute_sync(
                self._require_sync_client(),  # type: ignore[attr-defined]
                self.PAGINATOR_CLS,  # type: ignore[attr-defined]
            )
        )

    async def _list_async(self, *a: Any, **k: Any) -> builtins.list[RecordRevision]:
        study_key = k.pop("study_key", None)
        op = self._build_revision_list_operation(study_key, k)
        return [
            item
            async for item in op.execute_async(
                self._require_async_client(),  # type: ignore[attr-defined]
                self.ASYNC_PAGINATOR_CLS,  # type: ignore[attr-defined]
            )
        ]


class RecordRevisionsEndpoint(RecordRevisionsOperationDef, EdcSyncListGetEndpoint[RecordRevision]):  # type: ignore[misc]
    """Synchronous endpoint for managing Record Revisions."""


class AsyncRecordRevisionsEndpoint(  # type: ignore[misc]
    RecordRevisionsOperationDef, EdcAsyncListGetEndpoint[RecordRevision]
):
    """Asynchronous endpoint for managing Record Revisions."""
