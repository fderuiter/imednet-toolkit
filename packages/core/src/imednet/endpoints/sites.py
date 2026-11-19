"""Endpoint for managing sites (study locations) in a study."""

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.sites import Site


class SitesOperationDef:
    """Definition for Site operations."""

    PATH = "sites"
    MODEL = Site
    _id_param = "siteId"
    FILTER_ATTRIBUTES = frozenset(
        {
            "siteId",
            "siteName",
            "siteEnrollmentStatus",
            "dateCreated",
            "dateModified",
            "lastUpdated",
        }
    )


class SitesEndpoint(SitesOperationDef, EdcSyncListGetEndpoint[Site]):  # type: ignore[misc]
    """Synchronous endpoint for managing Sites."""


class AsyncSitesEndpoint(SitesOperationDef, EdcAsyncListGetEndpoint[Site]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Sites."""
