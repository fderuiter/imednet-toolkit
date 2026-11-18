"""Endpoint for managing queries (dialogue/questions) in a study."""

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.queries import Query


class QueriesOperationDef:
    """Definition for Query operations."""

    PATH = "queries"
    MODEL = Query
    _id_param = "annotationId"


class QueriesEndpoint(QueriesOperationDef, EdcSyncListGetEndpoint[Query]):  # type: ignore[misc]
    """Synchronous endpoint for managing Queries."""


class AsyncQueriesEndpoint(QueriesOperationDef, EdcAsyncListGetEndpoint[Query]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Queries."""
