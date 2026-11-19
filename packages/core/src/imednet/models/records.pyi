# pylint: disable=duplicate-code
"""Record (form instance) models for iMedNet."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

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

class Record(ImednetBaseModel):
    """A data record for a subject, form, and visit."""

    keywords: list[Keyword] = Field(default_factory=list, alias="keywords")
    record_data: dict[str, Any] = Field(default_factory=dict, alias="recordData")
    record_status: RecordStatus | str | None = Field(default=None, alias="recordStatus")
    deleted: bool | None = Field(default=None, alias="deleted")
    study_key: str | None
    interval_id: int | None
    form_id: int | None
    form_key: str | None
    site_id: int | None
    record_id: int | None
    record_oid: str | None
    record_type: str | None
    date_created: str | None
    date_modified: str | None
    subject_id: int | None
    subject_oid: str | None
    subject_key: str | None
    visit_id: int | None
    parent_record_id: int | None
    last_updated: str | None

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

class RecordData(RootModel[dict[str, Any]]):
    """Arbitrary record data as a dictionary."""

class BaseRecordRequest(ImednetBaseModel):
    """Base class for record creation/update requests."""

    form_key: str = Field("", alias="formKey")
    data: RecordData = Field(default_factory=lambda: RecordData({}), alias="data")

class RegisterSubjectRequest(BaseRecordRequest):
    """Payload for registering (enrolling) a new subject.

    Per the API documentation, registering a subject only requires ``formKey``
    and ``siteName``.  The system assigns the subject identifier upon creation.
    Do **not** include a ``subjectKey`` here — doing so causes the server to
    treat the request as an update and reject it when the key is unknown.
    """

    site_name: str = Field(
        "", alias="siteName", description="Name of the site where the subject is enrolled"
    )

class UpdateScheduledRecordRequest(BaseRecordRequest):
    """Payload for updating an existing scheduled record."""

    subject_key: str = Field("", alias="subjectKey")
    interval_name: str = Field("", alias="intervalName")

class CreateNewRecordRequest(BaseRecordRequest):
    """Payload for creating a new unscheduled record."""

    subject_key: str = Field("", alias="subjectKey")
