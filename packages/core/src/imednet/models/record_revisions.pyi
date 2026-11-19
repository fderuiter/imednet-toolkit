# pylint: disable=duplicate-code
"""Record revision history models for iMedNet."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import Field, field_validator, model_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.records import RecordStatus

def _coerce_optional_int_for_bounds(value: Any, field_name: str) -> int | None: ...
def _validate_revision_bounds(record_revision: int | None, data_revision: int | None) -> None: ...

class RecordRevision(ImednetBaseModel):
    """Historical version of a record including change reason and user."""

    record_revision: int | None = Field(default=None, alias="recordRevision")
    data_revision: int | None = Field(default=None, alias="dataRevision")
    record_status: RecordStatus | str | None = Field(default=None, alias="recordStatus")
    study_key: str | None
    record_revision_id: int | None
    record_id: int | None
    subject_id: int | None
    subject_key: str | None
    site_id: int | None
    form_key: str | None
    interval_id: int | None
    deleted: bool | None
    date_created: str | None
    reason_for_change: str | None
    record_oid: str | None
    role: str | None
    subject_oid: str | None
    user: str | None
    last_updated: str | None

    @model_validator(mode="before")
    @classmethod
    def _validate_bounds(cls, data: Any) -> Any: ...
    @field_validator("record_status", mode="before")
    @classmethod
    def _validate_record_status(cls, value: Any) -> RecordStatus | str | None: ...
    def __setattr__(self, name: str, value: Any) -> None: ...
    def validate_follows(self, previous: RecordRevision) -> bool: ...
    def can_follow(self, previous: RecordRevision) -> bool: ...
    @classmethod
    def validate_chain(cls, revisions: Sequence[RevisionInput]) -> list[RecordRevision]: ...

RevisionInput = RecordRevision | dict[str, Any]

def validate_record_revision_chain(revisions: Sequence[RevisionInput]) -> list[RecordRevision]: ...
def is_valid_record_revision_chain(revisions: Sequence[RevisionInput]) -> bool: ...
