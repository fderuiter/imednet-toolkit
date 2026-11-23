# pylint: disable=duplicate-code
"""Record (form instance) models for iMedNet."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, overload

from pydantic import Field, RootModel, field_validator

from imednet.models.base import ImednetBaseModel

def _normalize_record_token(value: str) -> str: ...

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

_NON_DELETED_RECORD_STATUSES: set[RecordStatus]
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
    keyword_id: int = Field(0, alias="keywordId")
    date_added: datetime = Field(default_factory=datetime.now, alias="dateAdded")

    @overload
    def __init__(
        self,
        *,
        keyword_name: str = "",
        keyword_key: str = "",
        keyword_id: int = 0,
        date_added: datetime = ...,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        keywordName: str = "",
        keywordKey: str = "",
        keywordId: int = 0,
        dateAdded: datetime = ...,
    ) -> None: ...

class Record(ImednetBaseModel):
    """A data record for a subject, form, and visit."""

    keywords: list[Keyword] = Field(default_factory=list, alias="keywords")
    record_data: dict[str, Any] = Field(default_factory=dict, alias="recordData")
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

    @overload
    def __init__(
        self,
        *,
        keywords: list[Keyword] = ...,
        record_data: dict[str, Any] = ...,
        record_status: RecordStatus | str | None = None,
        deleted: bool | None = None,
        study_key: str | None = None,
        interval_id: int | None = None,
        form_id: int | None = None,
        form_key: str | None = None,
        site_id: int | None = None,
        record_id: int | None = None,
        record_oid: str | None = None,
        record_type: str | None = None,
        date_created: str | None = None,
        date_modified: str | None = None,
        subject_id: int | None = None,
        subject_oid: str | None = None,
        subject_key: str | None = None,
        visit_id: int | None = None,
        parent_record_id: int | None = None,
        last_updated: str | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        keywords: list[Keyword] = ...,
        recordData: dict[str, Any] = ...,
        recordStatus: RecordStatus | str | None = None,
        deleted: bool | None = None,
        studyKey: str | None = None,
        intervalId: int | None = None,
        formId: int | None = None,
        formKey: str | None = None,
        siteId: int | None = None,
        recordId: int | None = None,
        recordOid: str | None = None,
        recordType: str | None = None,
        dateCreated: str | None = None,
        dateModified: str | None = None,
        subjectId: int | None = None,
        subjectOid: str | None = None,
        subjectKey: str | None = None,
        visitId: int | None = None,
        parentRecordId: int | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...
    @field_validator("record_status", mode="before")
    @classmethod
    def _validate_record_status(cls, value: Any) -> RecordStatus | str | None: ...
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

    @overload
    def __init__(self, *, job_id: str = "", batch_id: str = "", state: str = "") -> None: ...
    @overload
    def __init__(self, *, jobId: str = "", batchId: str = "", state: str = "") -> None: ...

class RecordData(RootModel[dict[str, Any]]):
    """Arbitrary record data as a dictionary."""

class BaseRecordRequest(ImednetBaseModel):
    """Base class for record creation/update requests."""

    form_key: str = Field("", alias="formKey")
    data: RecordData = Field(default_factory=lambda: RecordData({}), alias="data")

    @overload
    def __init__(self, *, form_key: str = "", data: RecordData = ...) -> None: ...
    @overload
    def __init__(self, *, formKey: str = "", data: RecordData = ...) -> None: ...

class RegisterSubjectRequest(BaseRecordRequest):
    """Payload for registering (enrolling) a new subject.

    Requires ``formKey`` and a site identifier (``siteName`` or ``siteId``).
    Optional ``subjectKey`` and ``subjectOid`` attributes are omitted when
    ``None`` so server-assigned subject keys continue working by default.
    """

    site_name: str = Field(
        "", alias="siteName", description="Name of the site where the subject is enrolled"
    )
    site_id: int | None = Field(
        default=None,
        alias="siteId",
        description="Numeric ID of the site where the subject is enrolled",
    )
    subject_key: str | None = Field(
        default=None, alias="subjectKey", description="Optional client-assigned subject key"
    )
    subject_oid: str | None = Field(
        default=None, alias="subjectOid", description="Optional subject OID"
    )

    @overload
    def __init__(
        self,
        *,
        form_key: str = "",
        data: RecordData = ...,
        site_name: str = "",
        site_id: int | None = None,
        subject_key: str | None = None,
        subject_oid: str | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        formKey: str = "",
        data: RecordData = ...,
        siteName: str = "",
        siteId: int | None = None,
        subjectKey: str | None = None,
        subjectOid: str | None = None,
    ) -> None: ...
    def model_dump(self, **kwargs: Any) -> dict[str, Any]: ...

class UpdateScheduledRecordRequest(BaseRecordRequest):
    """Payload for updating an existing scheduled record."""

    subject_key: str = Field("", alias="subjectKey")
    interval_name: str = Field("", alias="intervalName")

    @overload
    def __init__(
        self,
        *,
        form_key: str = "",
        data: RecordData = ...,
        subject_key: str = "",
        interval_name: str = "",
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        formKey: str = "",
        data: RecordData = ...,
        subjectKey: str = "",
        intervalName: str = "",
    ) -> None: ...

class CreateNewRecordRequest(BaseRecordRequest):
    """Payload for creating a new unscheduled record."""

    subject_key: str = Field("", alias="subjectKey")

    @overload
    def __init__(
        self, *, form_key: str = "", data: RecordData = ..., subject_key: str = ""
    ) -> None: ...
    @overload
    def __init__(
        self, *, formKey: str = "", data: RecordData = ..., subjectKey: str = ""
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
    "UpdateScheduledRecordRequest",
    "is_valid_record_transition",
    "normalize_record_status",
    "validate_record_transition",
]
