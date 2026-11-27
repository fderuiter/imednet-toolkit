# pylint: disable=duplicate-code
"""Record (form instance) models for iMedNet."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field, RootModel, ValidatorFunctionWrapHandler, field_validator

from imednet.models.base import ImednetBaseModel

class RecordStatus(str, Enum):
    """Normalized lifecycle statuses for eCRF form records."""

    RECORD_INCOMPLETE = "Record Incomplete"
    RECORD_COMPLETE = "Record Complete"
    INCOMPLETE = "Incomplete"
    COMPLETE = "Complete"
    PARTIAL = "Partial"
    LOCKED = "Locked"
    FROZEN = "Frozen"
    SIGNED = "Signed"
    VERIFIED = "Verified"
    APPROVED = "Approved"
    DELETED = "DELETED"

    def __str__(self) -> str: ...
    @classmethod
    def _missing_(cls, value: object) -> RecordStatus | None: ...
    def can_transition_to(self, target: RecordStatus | str) -> bool: ...
    def validate_transition_to(self, target: RecordStatus | str) -> bool: ...
    @property
    def allowed_transitions(self) -> set[RecordStatus]: ...

ALLOWED_RECORD_TRANSITIONS: dict[RecordStatus, set[RecordStatus]]

def normalize_record_status(value: RecordStatus | str | None) -> RecordStatus | str | None: ...
def is_valid_record_transition(
    from_status: RecordStatus | str | None,
    to_status: RecordStatus | str,
    *,
    deleted: bool | None = None,
) -> bool: ...
def validate_record_transition(
    from_status: RecordStatus | str | None,
    to_status: RecordStatus | str,
    *,
    deleted: bool | None = None,
) -> bool: ...

class Keyword(ImednetBaseModel):
    """A keyword or tag associated with a record."""

    keyword_name: str = Field("", alias="keywordName")
    keyword_key: str = Field("", alias="keywordKey")
    keyword_id: int | None = Field(default=None, alias="keywordId")
    date_added: datetime | None = Field(default=None, alias="dateAdded")

    def __init__(
        self,
        *,
        keyword_name: str = "",
        keywordName: str = "",
        keyword_key: str = "",
        keywordKey: str = "",
        keyword_id: int | None = None,
        keywordId: int | None = None,
        date_added: datetime | None = None,
        dateAdded: datetime | None = None,
    ) -> None: ...

class Record(ImednetBaseModel):
    """A data record for a subject, form, and visit."""

    keywords: list[Keyword] | None = Field(default_factory=list[Keyword], alias="keywords")
    record_data: dict[str, Any] | None = Field(default_factory=dict[str, Any], alias="recordData")
    record_status: RecordStatus | str | None = Field(default=None, alias="recordStatus")
    deleted: bool | None = Field(default=None, alias="deleted")
    study_key: str | None = Field(default=None, alias="studyKey")
    interval_id: int | None = Field(default=None, alias="intervalId")
    form_id: int | None = Field(default=None, alias="formId")
    form_key: str | None = Field(default=None, alias="formKey")
    site_id: int | None = Field(default=None, alias="siteId")
    record_id: int | None = Field(default=None, alias="recordId")
    record_oid: str | None = Field(default=None, alias="recordOid")
    record_type: str | None = Field(default=None, alias="recordType")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_modified: str | None = Field(default=None, alias="dateModified")
    subject_id: int | None = Field(default=None, alias="subjectId")
    subject_oid: str | None = Field(default=None, alias="subjectOid")
    subject_key: str | None = Field(default=None, alias="subjectKey")
    visit_id: int | None = Field(default=None, alias="visitId")
    parent_record_id: int | None = Field(default=None, alias="parentRecordId")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    def __init__(
        self,
        *,
        keywords: list[Keyword] | None = ...,
        record_data: dict[str, Any] | None = ...,
        recordData: dict[str, Any] | None = ...,
        record_status: RecordStatus | str | None = None,
        recordStatus: RecordStatus | str | None = None,
        deleted: bool | None = None,
        study_key: str | None = None,
        studyKey: str | None = None,
        interval_id: int | None = None,
        intervalId: int | None = None,
        form_id: int | None = None,
        formId: int | None = None,
        form_key: str | None = None,
        formKey: str | None = None,
        site_id: int | None = None,
        siteId: int | None = None,
        record_id: int | None = None,
        recordId: int | None = None,
        record_oid: str | None = None,
        recordOid: str | None = None,
        record_type: str | None = None,
        recordType: str | None = None,
        date_created: str | None = None,
        dateCreated: str | None = None,
        date_modified: str | None = None,
        dateModified: str | None = None,
        subject_id: int | None = None,
        subjectId: int | None = None,
        subject_oid: str | None = None,
        subjectOid: str | None = None,
        subject_key: str | None = None,
        subjectKey: str | None = None,
        visit_id: int | None = None,
        visitId: int | None = None,
        parent_record_id: int | None = None,
        parentRecordId: int | None = None,
        last_updated: str | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...
    @field_validator("record_status", mode="wrap")
    @classmethod
    def _validate_record_status(
        cls, value: Any, handler: ValidatorFunctionWrapHandler
    ) -> RecordStatus | str | None: ...
    @property
    def is_deleted(self) -> bool: ...
    @property
    def effective_status(self) -> RecordStatus | str | None: ...
    def can_transition_to(self, target_status: RecordStatus | str) -> bool: ...
    def transition_to(self, target_status: RecordStatus | str) -> RecordStatus | str: ...
    def update_status(self, target_status: RecordStatus | str) -> RecordStatus | str: ...
    def __setattr__(self, name: str, value: Any) -> None: ...

class RecordJobResponse(ImednetBaseModel):
    """Response for a record-related job (batch operations, etc)."""

    job_id: str = Field("", alias="jobId")
    batch_id: str = Field("", alias="batchId")
    state: str = Field("", alias="state")

    def __init__(
        self,
        *,
        job_id: str = "",
        jobId: str = "",
        batch_id: str = "",
        batchId: str = "",
        state: str = "",
    ) -> None: ...

class RecordData(RootModel[dict[str, Any]]):
    """Arbitrary record data as a dictionary."""

class BaseRecordRequest(ImednetBaseModel):
    """Base class for record creation and update requests.

    Declares all 14 optional iMednet REST API ``POST /records`` write attributes
    with ``None`` defaults, forbids undeclared keys (``extra="forbid"``), and
    serializes to camelCase keys omitting ``None`` and blank identifiers by default.
    """

    form_key: str | None = Field(default=None, alias="formKey")
    form_id: int | None = Field(default=None, alias="formId")
    site_name: str | None = Field(default=None, alias="siteName")
    site_id: int | None = Field(default=None, alias="siteId")
    subject_key: str | None = Field(default=None, alias="subjectKey")
    subject_id: int | None = Field(default=None, alias="subjectId")
    subject_oid: str | None = Field(default=None, alias="subjectOid")
    interval_name: str | None = Field(default=None, alias="intervalName")
    interval_id: int | None = Field(default=None, alias="intervalId")
    visit_id: int | None = Field(default=None, alias="visitId")
    record_id: int | None = Field(default=None, alias="recordId")
    record_oid: str | None = Field(default=None, alias="recordOid")
    record_status: RecordStatus | str | None = Field(default=None, alias="recordStatus")
    reason_for_change: str | None = Field(default=None, alias="reasonForChange")
    data: RecordData = Field(default_factory=lambda: RecordData({}), alias="data")

    def __init__(
        self,
        *,
        form_key: str | None = None,
        formKey: str | None = None,
        form_id: int | None = None,
        formId: int | None = None,
        site_name: str | None = None,
        siteName: str | None = None,
        site_id: int | None = None,
        siteId: int | None = None,
        subject_key: str | None = None,
        subjectKey: str | None = None,
        subject_id: int | None = None,
        subjectId: int | None = None,
        subject_oid: str | None = None,
        subjectOid: str | None = None,
        interval_name: str | None = None,
        intervalName: str | None = None,
        interval_id: int | None = None,
        intervalId: int | None = None,
        visit_id: int | None = None,
        visitId: int | None = None,
        record_id: int | None = None,
        recordId: int | None = None,
        record_oid: str | None = None,
        recordOid: str | None = None,
        record_status: RecordStatus | str | None = None,
        recordStatus: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        reasonForChange: str | None = None,
        data: RecordData | dict[str, Any] = ...,
    ) -> None: ...
    @field_validator("record_status", mode="wrap")
    @classmethod
    def _validate_request_record_status(
        cls, value: Any, handler: ValidatorFunctionWrapHandler
    ) -> RecordStatus | str | None: ...
    def model_dump(self, **kwargs: Any) -> dict[str, Any]: ...

class RegisterSubjectRequest(BaseRecordRequest):
    """Payload for registering (enrolling) a new subject.

    Typically supplies ``formKey`` (or ``formId``) and a site identifier
    (``siteName`` or ``siteId``). Optional ``subjectKey`` and ``subjectOid``
    attributes are omitted when ``None`` or blank so server-assigned subject
    keys continue working by default; when explicitly supplied, they are
    preserved for enrollment and deduplication.
    """

    def __init__(
        self,
        *,
        form_key: str | None = None,
        formKey: str | None = None,
        form_id: int | None = None,
        formId: int | None = None,
        site_name: str | None = None,
        siteName: str | None = None,
        site_id: int | None = None,
        siteId: int | None = None,
        subject_key: str | None = None,
        subjectKey: str | None = None,
        subject_id: int | None = None,
        subjectId: int | None = None,
        subject_oid: str | None = None,
        subjectOid: str | None = None,
        interval_name: str | None = None,
        intervalName: str | None = None,
        interval_id: int | None = None,
        intervalId: int | None = None,
        visit_id: int | None = None,
        visitId: int | None = None,
        record_id: int | None = None,
        recordId: int | None = None,
        record_oid: str | None = None,
        recordOid: str | None = None,
        record_status: RecordStatus | str | None = None,
        recordStatus: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        reasonForChange: str | None = None,
        data: RecordData | dict[str, Any] = ...,
    ) -> None: ...

class UpdateScheduledRecordRequest(BaseRecordRequest):
    """Payload for updating an existing scheduled record.

    Targets a scheduled form instance via ``formKey``/``formId``,
    ``subjectKey``/``subjectId``/``subjectOid``, and ``intervalName``/``intervalId``
    (or ``visitId``), with optional ``recordStatus`` and ``reasonForChange``.
    """

    def __init__(
        self,
        *,
        form_key: str | None = None,
        formKey: str | None = None,
        form_id: int | None = None,
        formId: int | None = None,
        site_name: str | None = None,
        siteName: str | None = None,
        site_id: int | None = None,
        siteId: int | None = None,
        subject_key: str | None = None,
        subjectKey: str | None = None,
        subject_id: int | None = None,
        subjectId: int | None = None,
        subject_oid: str | None = None,
        subjectOid: str | None = None,
        interval_name: str | None = None,
        intervalName: str | None = None,
        interval_id: int | None = None,
        intervalId: int | None = None,
        visit_id: int | None = None,
        visitId: int | None = None,
        record_id: int | None = None,
        recordId: int | None = None,
        record_oid: str | None = None,
        recordOid: str | None = None,
        record_status: RecordStatus | str | None = None,
        recordStatus: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        reasonForChange: str | None = None,
        data: RecordData | dict[str, Any] = ...,
    ) -> None: ...

class CreateNewRecordRequest(BaseRecordRequest):
    """Payload for creating a new unscheduled record.

    Targets a form and subject via ``formKey``/``formId`` and
    ``subjectKey``/``subjectId``/``subjectOid``, with optional site, status,
    and audit reason attributes.
    """

    def __init__(
        self,
        *,
        form_key: str | None = None,
        formKey: str | None = None,
        form_id: int | None = None,
        formId: int | None = None,
        site_name: str | None = None,
        siteName: str | None = None,
        site_id: int | None = None,
        siteId: int | None = None,
        subject_key: str | None = None,
        subjectKey: str | None = None,
        subject_id: int | None = None,
        subjectId: int | None = None,
        subject_oid: str | None = None,
        subjectOid: str | None = None,
        interval_name: str | None = None,
        intervalName: str | None = None,
        interval_id: int | None = None,
        intervalId: int | None = None,
        visit_id: int | None = None,
        visitId: int | None = None,
        record_id: int | None = None,
        recordId: int | None = None,
        record_oid: str | None = None,
        recordOid: str | None = None,
        record_status: RecordStatus | str | None = None,
        recordStatus: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        reasonForChange: str | None = None,
        data: RecordData | dict[str, Any] = ...,
    ) -> None: ...

class UpdateRecordRequest(BaseRecordRequest):
    """Payload for directly updating an existing record by identifier.

    Targets an existing record via ``recordId``, ``recordOid``, or ``visitId``,
    with optional ``formKey``/``formId``, ``recordStatus``, ``reasonForChange``,
    and clinical ``data`` updates.
    """

    def __init__(
        self,
        *,
        form_key: str | None = None,
        formKey: str | None = None,
        form_id: int | None = None,
        formId: int | None = None,
        site_name: str | None = None,
        siteName: str | None = None,
        site_id: int | None = None,
        siteId: int | None = None,
        subject_key: str | None = None,
        subjectKey: str | None = None,
        subject_id: int | None = None,
        subjectId: int | None = None,
        subject_oid: str | None = None,
        subjectOid: str | None = None,
        interval_name: str | None = None,
        intervalName: str | None = None,
        interval_id: int | None = None,
        intervalId: int | None = None,
        visit_id: int | None = None,
        visitId: int | None = None,
        record_id: int | None = None,
        recordId: int | None = None,
        record_oid: str | None = None,
        recordOid: str | None = None,
        record_status: RecordStatus | str | None = None,
        recordStatus: RecordStatus | str | None = None,
        reason_for_change: str | None = None,
        reasonForChange: str | None = None,
        data: RecordData | dict[str, Any] = ...,
    ) -> None: ...

__all__ = [
    "ALLOWED_RECORD_TRANSITIONS",
    "BaseRecordRequest",
    "CreateNewRecordRequest",
    "Keyword",
    "Record",
    "RecordData",
    "RecordJobResponse",
    "RecordStatus",
    "RegisterSubjectRequest",
    "UpdateRecordRequest",
    "UpdateScheduledRecordRequest",
    "is_valid_record_transition",
    "normalize_record_status",
    "validate_record_transition",
]
