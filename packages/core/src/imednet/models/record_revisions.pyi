# pylint: disable=duplicate-code
"""Record revision history models for iMedNet."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator, model_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.records import RecordStatus

def _coerce_optional_int_for_bounds(value: Any, field_name: str) -> int | None: ...
def _parse_revision_timestamp(value: Any) -> datetime | None: ...
def _validate_revision_bounds(record_revision: int | None, data_revision: int | None) -> None: ...

class RecordRevision(ImednetBaseModel):
    """Historical version of a record including change reason and user."""

    record_revision: int | None = Field(default=None, alias="recordRevision")
    data_revision: int | None = Field(default=None, alias="dataRevision")
    record_status: RecordStatus | str | None = Field(default=None, alias="recordStatus")
    study_key: str | None = Field(default=None, alias="studyKey")
    record_revision_id: int | None = Field(default=None, alias="recordRevisionId")
    record_id: int | None = Field(default=None, alias="recordId")
    subject_id: int | None = Field(default=None, alias="subjectId")
    subject_key: str | None = Field(default=None, alias="subjectKey")
    site_id: int | None = Field(default=None, alias="siteId")
    form_key: str | None = Field(default=None, alias="formKey")
    interval_id: int | None = Field(default=None, alias="intervalId")
    deleted: bool | None = Field(default=None, alias="deleted")
    date_created: str | None = Field(default=None, alias="dateCreated")
    reason_for_change: str | None = Field(default=None, alias="reasonForChange")
    record_oid: str | None = Field(default=None, alias="recordOid")
    role: str | None = Field(default=None, alias="role")
    subject_oid: str | None = Field(default=None, alias="subjectOid")
    user: str | None = Field(default=None, alias="user")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    def __init__(
        self,
        *,
        record_revision: int | None = None,
        recordRevision: int | None = None,
        data_revision: int | None = None,
        dataRevision: int | None = None,
        record_status: RecordStatus | str | None = None,
        recordStatus: RecordStatus | str | None = None,
        study_key: str | None = None,
        studyKey: str | None = None,
        record_revision_id: int | None = None,
        recordRevisionId: int | None = None,
        record_id: int | None = None,
        recordId: int | None = None,
        subject_id: int | None = None,
        subjectId: int | None = None,
        subject_key: str | None = None,
        subjectKey: str | None = None,
        site_id: int | None = None,
        siteId: int | None = None,
        form_key: str | None = None,
        formKey: str | None = None,
        interval_id: int | None = None,
        intervalId: int | None = None,
        deleted: bool | None = None,
        date_created: str | None = None,
        dateCreated: str | None = None,
        reason_for_change: str | None = None,
        reasonForChange: str | None = None,
        record_oid: str | None = None,
        recordOid: str | None = None,
        role: str | None = None,
        subject_oid: str | None = None,
        subjectOid: str | None = None,
        user: str | None = None,
        last_updated: str | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...
    @model_validator(mode="before")
    @classmethod
    def _validate_bounds(cls, data: Any) -> Any: ...
    @field_validator("record_status", mode="before")
    @classmethod
    def _validate_record_status(cls, value: Any) -> RecordStatus | str | None: ...
    def __setattr__(self, name: str, value: Any) -> None: ...
    def validate_follows(
        self, previous: RecordRevision, *, allow_partial: bool = False
    ) -> bool: ...
    def can_follow(self, previous: RecordRevision, *, allow_partial: bool = False) -> bool: ...
    @classmethod
    def validate_chain(
        cls, revisions: Sequence[RevisionInput], *, allow_partial: bool = False
    ) -> list[RecordRevision]: ...

RevisionInput = RecordRevision | dict[str, Any]

def validate_record_revision_chain(
    revisions: Sequence[RevisionInput], *, allow_partial: bool = False
) -> list[RecordRevision]: ...
def is_valid_record_revision_chain(
    revisions: Sequence[RevisionInput], *, allow_partial: bool = False
) -> bool: ...

__all__ = [
    "RecordRevision",
    "RevisionInput",
    "is_valid_record_revision_chain",
    "validate_record_revision_chain",
]
