"""Endpoint for managing codings (medical coding) in a study."""

from imednet.core.endpoint.edc_mixin import EdcAsyncListEndpoint, EdcSyncListEndpoint
from imednet.models.codings import Coding


class CodingsOperationDef:
    """Definition for Coding operations."""

    PATH = "codings"
    MODEL = Coding
    FILTER_ATTRIBUTES = frozenset(
        {
            "siteName",
            "siteId",
            "subjectId",
            "subjectKey",
            "formId",
            "formName",
            "formKey",
            "revision",
            "recordId",
            "variable",
            "value",
            "code",
            "codedBy",
            "reason",
            "dictionaryName",
            "dictionaryVersion",
            "dateCoded",
            "lastUpdated",
        }
    )


class CodingsEndpoint(CodingsOperationDef, EdcSyncListEndpoint[Coding]):  # type: ignore[misc]
    """Synchronous endpoint for managing Codings."""


class AsyncCodingsEndpoint(CodingsOperationDef, EdcAsyncListEndpoint[Coding]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Codings."""
