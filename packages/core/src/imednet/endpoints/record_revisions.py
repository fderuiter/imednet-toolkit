"""Endpoint for retrieving record revision history in a study."""

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.record_revisions import RecordRevision


class RecordRevisionsOperationDef:
    """Definition for Record Revision operations."""

    PATH = "recordRevisions"
    MODEL = RecordRevision
    _id_param = "recordRevisionId"
    FILTER_ATTRIBUTES = frozenset(
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


class RecordRevisionsEndpoint(RecordRevisionsOperationDef, EdcSyncListGetEndpoint[RecordRevision]):  # type: ignore[misc]
    """Synchronous endpoint for managing Record Revisions."""


class AsyncRecordRevisionsEndpoint(  # type: ignore[misc]
    RecordRevisionsOperationDef, EdcAsyncListGetEndpoint[RecordRevision]
):
    """Asynchronous endpoint for managing Record Revisions."""
