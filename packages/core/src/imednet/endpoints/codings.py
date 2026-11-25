"""Endpoint for managing codings (medical coding) in a study."""

from typing import ClassVar

from imednet.core.endpoint.edc_mixin import EdcAsyncListEndpoint, EdcSyncListEndpoint
from imednet.models.codings import Coding


class CodingsOperationDef:
    """Definition for Coding operations."""

    PATH: ClassVar[str] = "codings"
    MODEL = Coding
    FILTER_ATTRIBUTES: ClassVar[frozenset[str] | None] = frozenset(
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


class CodingsEndpoint(CodingsOperationDef, EdcSyncListEndpoint[Coding]):
    """Synchronous endpoint for managing Codings."""


class AsyncCodingsEndpoint(CodingsOperationDef, EdcAsyncListEndpoint[Coding]):
    """Asynchronous endpoint for managing Codings."""
