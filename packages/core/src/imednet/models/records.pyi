# pylint: disable=duplicate-code
"""Record (form instance) models for iMedNet."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, RootModel, StrictInt, StrictStr, field_validator, model_validator

from imednet.models.base import ImednetBaseModel

_INT_ID_ALIASES: dict[str, str]
_STR_ID_ALIASES: dict[str, str]

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
    last_updated: str | None

class RecordJobResponse(ImednetBaseModel):
    """Response for a record-related job (batch operations, etc)."""

    job_id: str = Field("", alias="jobId")
    batch_id: str = Field("", alias="batchId")
    state: str = Field("", alias="state")

class RecordData(RootModel[dict[str, Any]]):
    """Arbitrary record data as a dictionary."""

class BaseRecordRequest(ImednetBaseModel):
    """Base class for strict record creation/update requests."""

    form_key: StrictStr | None = Field(default=None, alias="formKey")
    form_id: StrictInt | None = Field(default=None, alias="formId")
    data: RecordData = Field(default_factory=lambda: RecordData({}), alias="data")

    @model_validator(mode="before")
    @classmethod
    def _detect_drift(cls, data: Any) -> Any: ...
    @field_validator("*", check_fields=False, mode="before")
    @classmethod
    def _normalise(cls, v: Any, info: Any) -> Any: ...
    @field_validator("*", check_fields=False)
    @classmethod
    def _validate_positive_id(cls, v: Any, info: Any) -> Any: ...

class RegisterSubjectRequest(BaseRecordRequest):
    """Payload for registering (enrolling) a new subject.

    Per the API documentation, registering a subject only requires ``formKey``
    (or ``formId``) and ``siteName`` (or ``siteId``). The system assigns the
    subject identifier upon creation. Do **not** include a ``subjectKey`` here
    — doing so causes the server to treat the request as an update and reject
    it when the key is unknown.
    """

    site_name: StrictStr | None = Field(
        default=None, alias="siteName", description="Name of the site where the subject is enrolled"
    )
    site_id: StrictInt | None = Field(
        default=None, alias="siteId", description="ID of the site where the subject is enrolled"
    )

class CreateNewRecordRequest(RegisterSubjectRequest):
    """Payload for creating a new unscheduled record or updating a record by identifier."""

    subject_key: StrictStr | None = Field(default=None, alias="subjectKey")
    subject_id: StrictInt | None = Field(default=None, alias="subjectId")
    subject_oid: StrictStr | None = Field(default=None, alias="subjectOid")
    record_id: StrictInt | None = Field(default=None, alias="recordId")
    record_oid: StrictStr | None = Field(default=None, alias="recordOid")

class UpdateScheduledRecordRequest(CreateNewRecordRequest):
    """Payload for updating an existing scheduled record."""

    interval_name: StrictStr | None = Field(default=None, alias="intervalName")
    interval_id: StrictInt | None = Field(default=None, alias="intervalId")

class RecordRequest(UpdateScheduledRecordRequest):
    """Strict request envelope supporting all documented POST /records identifier pairs."""
