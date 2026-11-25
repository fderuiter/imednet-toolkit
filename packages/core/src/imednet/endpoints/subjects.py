"""Endpoint for managing subjects in a study."""

from __future__ import annotations

from typing import Any, ClassVar

from imednet.core.endpoint._mixin_protocols import ListEndpointSelfProtocol
from imednet.core.endpoint.dispatch import Operation, execute_operation
from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.subjects import Subject
from imednet.utils.url import validate_path_segment


class ListBySiteOperation(Operation[list[Subject]]):
    """List by site operation."""

    def __init__(
        self,
        endpoint: ListEndpointSelfProtocol[Subject],
        study_key: str,
        site_id: str | int,
    ):
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
        return await self.endpoint._list_async(
            client,
            self.endpoint.ASYNC_PAGINATOR_CLS,
            study_key=self.study_key,
            site_id=self.site_id,
        )


class SubjectsOperationDef:
    """Definition for Subject operations."""

    PATH: ClassVar[str] = "subjects"
    MODEL = Subject
    _id_param = "subjectId"
    _lookup_key_hint: ClassVar[str | None] = "subjectKey"
    FILTER_ATTRIBUTES: ClassVar[frozenset[str] | None] = frozenset(
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

    @execute_operation
    def list_by_site(
        self: ListEndpointSelfProtocol[Subject],
        study_key: str,
        site_id: str | int,
    ) -> ListBySiteOperation:
        """List subjects by site ID."""
        self._validate_study_key(study_key)
        validate_path_segment(site_id, name="site_id")
        return ListBySiteOperation(self, study_key, site_id)


class SubjectsEndpoint(SubjectsOperationDef, EdcSyncListGetEndpoint[Subject]):
    """Synchronous endpoint for managing Subjects."""


class AsyncSubjectsEndpoint(SubjectsOperationDef, EdcAsyncListGetEndpoint[Subject]):
    """Asynchronous endpoint for managing Subjects."""
