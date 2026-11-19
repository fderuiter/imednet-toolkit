# pylint: disable=duplicate-code
"""Record (form instance) models for iMedNet."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field, RootModel, field_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


def _normalize_record_token(value: str) -> str:
    """Collapse whitespace, underscores, and hyphens into a lowercase token."""
    return re.sub(r"[\s_-]+", " ", value.strip()).lower()


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

    def __str__(self) -> str:
        """Return the canonical status string value."""
        return str(self.value)

    @classmethod
    def _missing_(cls, value: object) -> RecordStatus | None:
        """Resolve record status case- and separator-insensitively."""
        if isinstance(value, str):
            token = _normalize_record_token(value)
            if not token:
                return None
            for member in cls:
                if (
                    _normalize_record_token(member.value) == token
                    or _normalize_record_token(member.name) == token
                ):
                    return member
        return None

    def can_transition_to(self, target: RecordStatus | str) -> bool:
        """Return True if transitioning from this status to *target* is permitted."""
        return is_valid_record_transition(self, target)

    def validate_transition_to(self, target: RecordStatus | str) -> bool:
        """Validate transition to *target*, raising ValueError if disallowed."""
        return validate_record_transition(self, target)

    @property
    def allowed_transitions(self) -> set[RecordStatus]:
        """Return permissible next statuses from this status."""
        return set(ALLOWED_RECORD_TRANSITIONS.get(self, set()))


_NON_DELETED_RECORD_STATUSES: set[RecordStatus] = {
    s for s in RecordStatus if s != RecordStatus.DELETED
}

ALLOWED_RECORD_TRANSITIONS: dict[RecordStatus, set[RecordStatus]] = {
    **{
        status: (_NON_DELETED_RECORD_STATUSES - {status}) | {RecordStatus.DELETED}
        for status in _NON_DELETED_RECORD_STATUSES
    },
    RecordStatus.DELETED: set(),
}


def normalize_record_status(
    value: RecordStatus | str | None,
) -> RecordStatus | str | None:
    """Normalize known record statuses while preserving custom study status strings."""
    if value is None or isinstance(value, RecordStatus):
        return value
    if isinstance(value, str):
        try:
            return RecordStatus(value)
        except ValueError:
            return value
    return str(value)


def is_valid_record_transition(
    from_status: RecordStatus | str | None,
    to_status: RecordStatus | str,
    *,
    deleted: bool | None = None,
) -> bool:
    """Return True if transitioning a record from *from_status* to *to_status* is valid."""
    try:
        return validate_record_transition(from_status, to_status, deleted=deleted)
    except ValueError:
        return False


def validate_record_transition(
    from_status: RecordStatus | str | None,
    to_status: RecordStatus | str,
    *,
    deleted: bool | None = None,
) -> bool:
    """Validate a record status transition, raising ValueError if soft-deleted or invalid."""
    src = normalize_record_status(from_status)
    dst = normalize_record_status(to_status)
    if dst is None or (isinstance(dst, str) and not dst.strip()):
        raise ValueError(f"Invalid target record status: {to_status!r}")

    if deleted is True or src == RecordStatus.DELETED:
        raise ValueError(
            f"Cannot transition soft-deleted Record from {RecordStatus.DELETED.value} to {dst!s}."
        )

    if isinstance(src, RecordStatus) and isinstance(dst, RecordStatus):
        permitted = ALLOWED_RECORD_TRANSITIONS.get(src, set())
        if dst not in permitted:
            options = sorted(p.value for p in permitted)
            raise ValueError(
                f"Cannot transition record status from {src.value} to {dst.value}. "
                f"Permitted transitions from {src.value}: {options}"
            )
    elif src is not None and src == dst:
        raise ValueError(f"Cannot transition record status from {src!s} to {dst!s}.")
    return True


class Keyword(ImednetBaseModel):
    """A keyword or tag associated with a record."""

    keyword_name: str = Field("", alias="keywordName")
    keyword_key: str = Field("", alias="keywordKey")
    keyword_id: int = Field(0, alias="keywordId")
    date_added: datetime = Field(default_factory=datetime.now, alias="dateAdded")


Keyword = ModelEngine.get_model('Keyword', Keyword)


class Record(ImednetBaseModel):
    """A data record for a subject, form, and visit."""

    keywords: list[Keyword] = Field(default_factory=list, alias="keywords")
    record_data: dict[str, Any] = Field(default_factory=dict, alias="recordData")
    record_status: RecordStatus | str | None = Field(default=None, alias="recordStatus")
    deleted: bool | None = Field(default=None, alias="deleted")

    @field_validator("record_status", mode="before")
    @classmethod
    def _validate_record_status(cls, value: Any) -> RecordStatus | str | None:
        """Normalize known record statuses and preserve custom study strings."""
        if value is None or isinstance(value, (RecordStatus, str)):
            return normalize_record_status(value)
        return normalize_record_status(str(value))

    @property
    def is_deleted(self) -> bool:
        """Return True when the record is soft-deleted."""
        return self.deleted is True or self.record_status == RecordStatus.DELETED

    @property
    def effective_status(self) -> RecordStatus | str | None:
        """Return DELETED when ``deleted=True``, otherwise ``record_status``."""
        if self.is_deleted:
            return RecordStatus.DELETED
        return self.record_status

    def can_transition_to(self, target_status: RecordStatus | str) -> bool:
        """Return True if transitioning to *target_status* is allowed."""
        return is_valid_record_transition(
            self.record_status,
            target_status,
            deleted=self.is_deleted,
        )

    def transition_to(self, target_status: RecordStatus | str) -> RecordStatus | str:
        """Transition record to *target_status*, enforcing soft-delete invariants."""
        validate_record_transition(
            self.record_status,
            target_status,
            deleted=self.is_deleted,
        )
        normalized = normalize_record_status(target_status)
        assert normalized is not None
        super().__setattr__("record_status", normalized)
        if normalized == RecordStatus.DELETED:
            super().__setattr__("deleted", True)
        return normalized

    def update_status(self, target_status: RecordStatus | str) -> RecordStatus | str:
        """Alias for :meth:`transition_to`."""
        return self.transition_to(target_status)

    def __setattr__(self, name: str, value: Any) -> None:
        """Enforce terminal soft-delete invariants on direct attribute mutation."""
        initialized = "deleted" in self.__dict__ or "record_status" in self.__dict__
        currently_deleted = (
            self.__dict__.get("deleted") is True
            or self.__dict__.get("record_status") == RecordStatus.DELETED
        )
        if initialized and currently_deleted:
            if name == "deleted" and value is not True:
                raise ValueError("Cannot restore a soft-deleted Record by clearing deleted.")
            if name == "record_status":
                normalized = normalize_record_status(value)
                if normalized != RecordStatus.DELETED:
                    raise ValueError(
                        f"Cannot mutate soft-deleted Record to active status {value!r}."
                    )
                super().__setattr__(name, normalized)
                return
        if initialized and name == "record_status":
            normalized = normalize_record_status(value)
            super().__setattr__(name, normalized)
            if normalized == RecordStatus.DELETED:
                super().__setattr__("deleted", True)
            return
        super().__setattr__(name, value)


Record = ModelEngine.get_model('Record', Record)


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
