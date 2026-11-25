"""Endpoint for managing queries (dialogue/questions) in a study."""

from typing import ClassVar

from imednet.core.endpoint.edc_mixin import EdcAsyncListEndpoint, EdcSyncListEndpoint
from imednet.models.queries import Query


class QueriesOperationDef:
    """Definition for Query operations."""

    PATH: ClassVar[str] = "queries"
    MODEL = Query
    PAGE_SIZE: ClassVar[int] = 500
    FILTER_ATTRIBUTES: ClassVar[frozenset[str] | None] = frozenset(
        {
            "subjectId",
            "subjectOid",
            "annotationType",
            "type",
            "description",
            "recordId",
            "variable",
            "subjectKey",
            "dateCreated",
            "dateModified",
            "lastUpdated",
        }
    )


class QueriesEndpoint(QueriesOperationDef, EdcSyncListEndpoint[Query]):
    """Synchronous endpoint for managing Queries."""


class AsyncQueriesEndpoint(QueriesOperationDef, EdcAsyncListEndpoint[Query]):
    """Asynchronous endpoint for managing Queries."""
