"""Endpoint for managing visits (interval instances) in a study."""

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.visits import Visit


class VisitsOperationDef:
    """Definition for Visit operations."""

    PATH = "visits"
    MODEL = Visit
    _id_param = "visitId"


class VisitsEndpoint(VisitsOperationDef, EdcSyncListGetEndpoint[Visit]):  # type: ignore[misc]
    """Synchronous endpoint for managing Visits."""


class AsyncVisitsEndpoint(VisitsOperationDef, EdcAsyncListGetEndpoint[Visit]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Visits."""
