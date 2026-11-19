"""Endpoint for managing forms in a study."""

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.forms import Form


class FormsOperationDef:
    """Definition for Form operations."""

    PATH = "forms"
    MODEL = Form
    _id_param = "formId"
    PAGE_SIZE = 500
    FILTER_ATTRIBUTES = frozenset(
        {
            "formId",
            "formKey",
            "formName",
            "formType",
            "revision",
            "embeddedLog",
            "enforceOwnership",
            "userAgreement",
            "subjectRecordReport",
            "unscheduledVisit",
            "otherForms",
            "eproForm",
            "allowCopy",
            "disabled",
            "dateCreated",
            "dateModified",
            "lastUpdated",
        }
    )


class FormsEndpoint(FormsOperationDef, EdcSyncListGetEndpoint[Form]):  # type: ignore[misc]
    """Synchronous endpoint for managing Forms."""


class AsyncFormsEndpoint(FormsOperationDef, EdcAsyncListGetEndpoint[Form]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Forms."""
