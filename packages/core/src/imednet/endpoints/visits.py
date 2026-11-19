"""Endpoint for managing visits (interval instances) in a study."""

from imednet.core.endpoint.edc_mixin import EdcAsyncListEndpoint, EdcSyncListEndpoint
from imednet.models.visits import Visit


class VisitsOperationDef:
    """Definition for Visit operations."""

    PATH = "visits"
    MODEL = Visit
    FILTER_ATTRIBUTES = frozenset(
        {
            "intervalId",
            "intervalName",
            "subjectId",
            "subjectKey",
            "startDate",
            "endDate",
            "dueDate",
            "visitDate",
            "visitDateForm",
            "visitDateQuestion",
            "deleted",
            "dateCreated",
            "dateModified",
            "lastUpdated",
        }
    )


class VisitsEndpoint(VisitsOperationDef, EdcSyncListEndpoint[Visit]):  # type: ignore[misc]
    """Synchronous endpoint for managing Visits."""


class AsyncVisitsEndpoint(VisitsOperationDef, EdcAsyncListEndpoint[Visit]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Visits."""
