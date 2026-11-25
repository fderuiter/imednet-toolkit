"""Endpoint for managing visits (interval instances) in a study."""

from typing import ClassVar

from imednet.core.endpoint.edc_mixin import EdcAsyncListEndpoint, EdcSyncListEndpoint
from imednet.models.visits import Visit


class VisitsOperationDef:
    """Definition for Visit operations."""

    PATH: ClassVar[str] = "visits"
    MODEL = Visit
    FILTER_ATTRIBUTES: ClassVar[frozenset[str] | None] = frozenset(
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


class VisitsEndpoint(VisitsOperationDef, EdcSyncListEndpoint[Visit]):
    """Synchronous endpoint for managing Visits."""


class AsyncVisitsEndpoint(VisitsOperationDef, EdcAsyncListEndpoint[Visit]):
    """Asynchronous endpoint for managing Visits."""
