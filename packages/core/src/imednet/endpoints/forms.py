"""Endpoint for managing forms in a study."""

from typing import ClassVar

from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.models.forms import Form


class FormsOperationDef:
    """Definition for Form operations."""

    PATH: ClassVar[str] = "forms"
    MODEL = Form
    _id_param = "formId"
    _lookup_key_hint: ClassVar[str | None] = "formKey"
    PAGE_SIZE: ClassVar[int] = 500
    FILTER_ATTRIBUTES: ClassVar[frozenset[str] | None] = frozenset(
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


class FormsEndpoint(FormsOperationDef, EdcSyncListGetEndpoint[Form]):
    """Synchronous endpoint for managing Forms."""


class AsyncFormsEndpoint(FormsOperationDef, EdcAsyncListGetEndpoint[Form]):
    """Asynchronous endpoint for managing Forms."""
