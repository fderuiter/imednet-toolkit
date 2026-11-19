"""Endpoint for managing subjects in a study."""

from __future__ import annotations

from typing import Any

from imednet.core.endpoint.dispatch import execute_operation
from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.subjects import Subject
from imednet.utils.typing import ItemId


class ListBySiteOperation:
    """List by site operation."""

    def __init__(self, endpoint: Any, study_key: str, site_id: str | int):
        """Initialize."""
        self.endpoint = endpoint
        self.study_key = study_key
        self.site_id = site_id

    def execute_sync(self, client: Any, parse_func: Any = None) -> list[Subject]:
        """Execute sync."""
        return list(
            self.endpoint._list_sync(
                client, self.endpoint.PAGINATOR_CLS, study_key=self.study_key, site_id=self.site_id
            )
        )

    async def execute_async(self, client: Any, parse_func: Any = None) -> list[Subject]:
        """Execute async."""
        return await self.endpoint._list_async(  # type: ignore[no-any-return]
            client,
            self.endpoint.ASYNC_PAGINATOR_CLS,
            study_key=self.study_key,
            site_id=self.site_id,
        )


class SubjectsOperationDef:
    """Definition for Subject operations."""

    PATH = "subjects"
    MODEL = Subject
    _id_param = "subjectId"
    FILTER_ATTRIBUTES = frozenset(
        {
            "subjectId",
            "subjectOid",
            "subjectKey",
            "subjectStatus",
            "siteId",
            "siteName",
            "deleted",
            "enrollmentStartDate",
            "dateCreated",
            "dateModified",
            "lastUpdated",
        }
    )

    def _validate_item_id(self, item_id: ItemId) -> ItemId:
        """Validate that single-subject retrieval uses a non-negative integer subjectId."""
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 0:
            raise ValueError(
                f"Subjects endpoint get() requires a non-negative integer subjectId, got {item_id!r}. "
                "To look up subjects by subjectKey, use list(study_key, subjectKey=...)."
            )
        return item_id

    @execute_operation  # type: ignore[arg-type]
    def list_by_site(self, study_key: str, site_id: str | int) -> ListBySiteOperation:
        """List subjects by site ID."""
        return ListBySiteOperation(self, study_key, site_id)


class SubjectsEndpoint(SubjectsOperationDef, EdcSyncListGetEndpoint[Subject]):  # type: ignore[misc]
    """Synchronous endpoint for managing Subjects."""


class AsyncSubjectsEndpoint(SubjectsOperationDef, EdcAsyncListGetEndpoint[Subject]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Subjects."""
