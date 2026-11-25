"""Endpoint for managing sites (study locations) in a study."""

from typing import ClassVar

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.sites import Site


class SitesOperationDef:
    """Definition for Site operations."""

    PATH: ClassVar[str] = "sites"
    MODEL = Site
    _id_param = "siteId"
    _lookup_key_hint: ClassVar[str | None] = "siteName"
    FILTER_ATTRIBUTES: ClassVar[frozenset[str] | None] = frozenset(
        {
            "siteId",
            "siteName",
            "siteEnrollmentStatus",
            "dateCreated",
            "dateModified",
            "lastUpdated",
        }
    )


class SitesEndpoint(SitesOperationDef, EdcSyncListGetEndpoint[Site]):
    """Synchronous endpoint for managing Sites."""


class AsyncSitesEndpoint(SitesOperationDef, EdcAsyncListGetEndpoint[Site]):
    """Asynchronous endpoint for managing Sites."""
