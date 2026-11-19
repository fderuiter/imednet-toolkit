# pylint: disable=duplicate-code
"""Record (form instance) models for iMedNet."""

from __future__ import annotations

from typing import Any

from pydantic import (
    ConfigDict,
    Field,
    RootModel,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine

_INT_ID_ALIASES: dict[str, str] = {
    "form_id": "formId",
    "site_id": "siteId",
    "subject_id": "subjectId",
    "interval_id": "intervalId",
    "record_id": "recordId",
}
_STR_ID_ALIASES: dict[str, str] = {
    "form_key": "formKey",
    "site_name": "siteName",
    "subject_key": "subjectKey",
    "subject_oid": "subjectOid",
    "interval_name": "intervalName",
    "record_oid": "recordOid",
}


class Keyword(ImednetBaseModel):
    """A keyword or tag associated with a record."""


Keyword = ModelEngine.get_model('Keyword', Keyword)


class Record(ImednetBaseModel):
    """A data record for a subject, form, and visit."""


Record = ModelEngine.get_model('Record', Record)


class RecordJobResponse(ImednetBaseModel):
    """Response for a record-related job (batch operations, etc)."""

    job_id: str = Field("", alias="jobId")
    batch_id: str = Field("", alias="batchId")
    state: str = Field("", alias="state")


class RecordData(RootModel[dict[str, Any]]):
    """Arbitrary record data as a dictionary."""


class BaseRecordRequest(ImednetBaseModel):
    """Base class for strict record creation/update requests."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    form_key: StrictStr | None = Field(default=None, alias="formKey")
    form_id: StrictInt | None = Field(default=None, alias="formId")
    data: RecordData = Field(default_factory=lambda: RecordData({}), alias="data")

    @model_validator(mode="before")
    @classmethod
    def _detect_drift(cls, data: Any) -> Any:
        """Disable response drift warnings on client request models."""
        return data

    @field_validator("*", check_fields=False, mode="before")
    @classmethod
    def _normalise(cls, v: Any, info: Any) -> Any:
        """Disable lenient response coercion so request types are validated strictly."""
        field_name = getattr(info, "field_name", None)
        if v is None or not field_name:
            return v
        if field_name in _INT_ID_ALIASES:
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"{_INT_ID_ALIASES[field_name]} must be an integer")
            return v
        if field_name in _STR_ID_ALIASES:
            if not isinstance(v, str):
                raise ValueError(f"{_STR_ID_ALIASES[field_name]} must be a string")
            return v
        if field_name == "data" and not isinstance(v, (dict, RecordData)):
            raise ValueError("data must be a dictionary")
        return v

    @field_validator("*", check_fields=False)
    @classmethod
    def _validate_positive_id(cls, v: Any, info: Any) -> Any:
        """Validate positive integer identifiers."""
        field_name = getattr(info, "field_name", "")
        if v is not None and field_name in _INT_ID_ALIASES and isinstance(v, int) and v <= 0:
            raise ValueError(f"{_INT_ID_ALIASES[field_name]} must be a positive integer")
        return v


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
