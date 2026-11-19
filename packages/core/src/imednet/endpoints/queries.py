"""Endpoint for managing queries (dialogue/questions) in a study."""

from imednet.core.endpoint.edc_mixin import EdcAsyncListEndpoint, EdcSyncListEndpoint
from imednet.models.queries import Query


class QueriesOperationDef:
    """Definition for Query operations."""

    PATH = "queries"
    MODEL = Query
    FILTER_ATTRIBUTES = frozenset(
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


class QueriesEndpoint(QueriesOperationDef, EdcSyncListEndpoint[Query]):  # type: ignore[misc]
    """Synchronous endpoint for managing Queries."""


class AsyncQueriesEndpoint(QueriesOperationDef, EdcAsyncListEndpoint[Query]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Queries."""
