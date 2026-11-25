"""Endpoint for retrieving record revision history in a study."""

from typing import ClassVar

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.record_revisions import RecordRevision


class RecordRevisionsOperationDef:
    """Definition for Record Revision operations."""

    PATH: ClassVar[str] = "recordRevisions"
    MODEL = RecordRevision
    _id_param = "recordRevisionId"
    _lookup_key_hint: ClassVar[str | None] = "recordOid"
    FILTER_ATTRIBUTES: ClassVar[frozenset[str] | None] = frozenset(
        {
            "recordRevisionId",
            "recordId",
            "recordOid",
            "recordRevision",
            "dataRevision",
            "recordStatus",
            "subjectId",
            "subjectOid",
            "subjectKey",
            "siteId",
            "formKey",
            "intervalId",
            "role",
            "user",
            "reasonForChange",
            "deleted",
            "dateCreated",
            "lastUpdated",
        }
    )


class RecordRevisionsEndpoint(RecordRevisionsOperationDef, EdcSyncListGetEndpoint[RecordRevision]):
    """Synchronous endpoint for managing Record Revisions."""


class AsyncRecordRevisionsEndpoint(
    RecordRevisionsOperationDef, EdcAsyncListGetEndpoint[RecordRevision]
):
    """Asynchronous endpoint for managing Record Revisions."""
