# pylint: disable=duplicate-code
"""Record (form instance) models for iMedNet."""

from __future__ import annotations

from typing import Any

from pydantic import Field, RootModel

from imednet.models.base import ImednetBaseModel

class Keyword(ImednetBaseModel):
    """A keyword or tag associated with a record."""

class Record(ImednetBaseModel):
    """A data record for a subject, form, and visit."""

    study_key: str | None
    interval_id: int | None
    form_id: int | None
    form_key: str | None
    site_id: int | None
    record_id: int | None
    record_oid: str | None
    record_type: str | None
    record_status: str | None
    deleted: bool | None
    date_created: str | None
    date_modified: str | None
    subject_id: int | None
    subject_oid: str | None
    subject_key: str | None
    visit_id: int | None
    parent_record_id: int | None
    record_data: Any
    last_updated: str | None
    keywords: str | None

class RecordJobResponse(ImednetBaseModel):
    """Response for a record-related job (batch operations, etc)."""

    job_id: str = Field("", alias="jobId")
    batch_id: str = Field("", alias="batchId")
    state: str = Field("", alias="state")

class RecordData(RootModel[dict[str, Any]]):
    """Arbitrary record data as a dictionary."""

class BaseRecordRequest(ImednetBaseModel):
    """Base class for strict record creation/update requests."""

    form_key: str | None = Field(default=None, alias="formKey")
    form_id: int | None = Field(default=None, alias="formId")
    data: RecordData = Field(default_factory=lambda: RecordData({}), alias="data")

class RegisterSubjectRequest(BaseRecordRequest):
    """Payload for registering (enrolling) a new subject."""

    site_name: str | None = Field(
        default=None, alias="siteName", description="Name of the site where the subject is enrolled"
    )
    site_id: int | None = Field(
        default=None, alias="siteId", description="ID of the site where the subject is enrolled"
    )

class CreateNewRecordRequest(RegisterSubjectRequest):
    """Payload for creating a new unscheduled record or updating a record by identifier."""

    subject_key: str | None = Field(default=None, alias="subjectKey")
    subject_id: int | None = Field(default=None, alias="subjectId")
    subject_oid: str | None = Field(default=None, alias="subjectOid")
    record_id: int | None = Field(default=None, alias="recordId")
    record_oid: str | None = Field(default=None, alias="recordOid")

class UpdateScheduledRecordRequest(CreateNewRecordRequest):
    """Payload for updating an existing scheduled record."""

    interval_name: str | None = Field(default=None, alias="intervalName")
    interval_id: int | None = Field(default=None, alias="intervalId")

class RecordRequest(UpdateScheduledRecordRequest):
    """Strict request envelope supporting all documented POST /records identifier pairs."""
