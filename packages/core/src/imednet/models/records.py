# pylint: disable=duplicate-code
"""Record (form instance) models for iMedNet."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import (
    Field,
    RootModel,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)

from imednet.models._lifecycle import (
    apply_entity_transition,
    build_terminal_transition_map,
    coerce_field_status,
    coerce_status,
    is_entity_deleted,
    is_valid_status_transition,
    resolve_effective_status,
    resolve_status_member,
    setattr_with_soft_delete_guard,
    validate_status_transition,
)
from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


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
        return resolve_status_member(cls, value)

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


ALLOWED_RECORD_TRANSITIONS: dict[RecordStatus, set[RecordStatus]] = build_terminal_transition_map(
    RecordStatus,
    RecordStatus.DELETED,
)


def normalize_record_status(
    value: RecordStatus | str | None,
) -> RecordStatus | str | None:
    """Normalize known record statuses while preserving custom study status strings."""
    return coerce_status(RecordStatus, value)


def is_valid_record_transition(
    from_status: RecordStatus | str | None,
    to_status: RecordStatus | str,
    *,
    deleted: bool | None = None,
) -> bool:
    """Return True if transitioning a record from *from_status* to *to_status* is valid."""
    return is_valid_status_transition(
        RecordStatus,
        ALLOWED_RECORD_TRANSITIONS,
        RecordStatus.DELETED,
        "Record",
        from_status,
        to_status,
        deleted=deleted,
    )


def validate_record_transition(
    from_status: RecordStatus | str | None,
    to_status: RecordStatus | str,
    *,
    deleted: bool | None = None,
) -> bool:
    """Validate a record status transition, raising ValueError if soft-deleted or invalid."""
    return validate_status_transition(
        RecordStatus,
        ALLOWED_RECORD_TRANSITIONS,
        RecordStatus.DELETED,
        "Record",
        from_status,
        to_status,
        deleted=deleted,
    )


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
        return coerce_field_status(RecordStatus, value)

    @property
    def is_deleted(self) -> bool:
        """Return True when the record is soft-deleted."""
        return is_entity_deleted(self.deleted, self.record_status, RecordStatus.DELETED)

    @property
    def effective_status(self) -> RecordStatus | str | None:
        """Return DELETED when ``deleted=True``, otherwise ``record_status``."""
        return resolve_effective_status(self.deleted, self.record_status, RecordStatus.DELETED)

    def can_transition_to(self, target_status: RecordStatus | str) -> bool:
        """Return True if transitioning to *target_status* is allowed."""
        return is_valid_record_transition(
            self.record_status,
            target_status,
            deleted=self.is_deleted,
        )

    def transition_to(self, target_status: RecordStatus | str) -> RecordStatus | str:
        """Transition record to *target_status*, enforcing soft-delete invariants."""
        return apply_entity_transition(
            self,
            enum_cls=RecordStatus,
            allowed_transitions=ALLOWED_RECORD_TRANSITIONS,
            deleted_member=RecordStatus.DELETED,
            entity_label="Record",
            status_field="record_status",
            target_status=target_status,
        )

    def update_status(self, target_status: RecordStatus | str) -> RecordStatus | str:
        """Alias for :meth:`transition_to`."""
        return self.transition_to(target_status)

    def __setattr__(self, name: str, value: Any) -> None:
        """Enforce terminal soft-delete invariants on direct attribute mutation."""
        setattr_with_soft_delete_guard(
            self,
            name,
            value,
            enum_cls=RecordStatus,
            deleted_member=RecordStatus.DELETED,
            entity_label="Record",
            status_field="record_status",
        )


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
    form_id: int | None = Field(default=None, alias="formId")
    subject_id: int | None = Field(default=None, alias="subjectId")
    subject_oid: str | None = Field(default=None, alias="subjectOid")
    interval_id: int | None = Field(default=None, alias="intervalId")
    record_id: int | None = Field(default=None, alias="recordId")
    data: RecordData = Field(default_factory=lambda: RecordData({}), alias="data")

    @field_validator("subject_oid", mode="before")
    @classmethod
    def _normalize_optional_base_oid(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @model_serializer(mode="wrap")
    def _serialize_omitting_blank_and_unset_identifiers(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        serialized: dict[str, Any] = handler(self)
        if not self.form_key or not self.form_key.strip():
            serialized.pop("form_key", None)
            serialized.pop("formKey", None)
        for attr_name, alias_name in (
            ("form_id", "formId"),
            ("subject_id", "subjectId"),
            ("subject_oid", "subjectOid"),
            ("interval_id", "intervalId"),
            ("record_id", "recordId"),
            ("site_id", "siteId"),
        ):
            if getattr(self, attr_name, None) is None:
                serialized.pop(attr_name, None)
                serialized.pop(alias_name, None)
        for str_attr, str_alias in (
            ("site_name", "siteName"),
            ("subject_key", "subjectKey"),
            ("interval_name", "intervalName"),
        ):
            raw_val = getattr(self, str_attr, None)
            if raw_val is None or not str(raw_val).strip():
                serialized.pop(str_attr, None)
                serialized.pop(str_alias, None)
        return serialized

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """Serialize record request payload, omitting ``None`` and blank identifier defaults."""
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(**kwargs)


class RegisterSubjectRequest(BaseRecordRequest):
    """Payload for registering (enrolling) a new subject.

    Requires ``formKey`` (or ``formId``) and a site identifier (``siteName`` or ``siteId``).
    Optional ``subjectKey``, ``subjectId``, and ``subjectOid`` attributes are omitted when
    ``None`` so server-assigned subject keys continue working by default; when
    explicitly supplied, they are preserved for enrollment and deduplication.
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
        default=None,
        alias="subjectKey",
        description="Optional client-assigned subject key",
    )

    @field_validator("subject_key", mode="before")
    @classmethod
    def _normalize_optional_subject_identifier(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class UpdateScheduledRecordRequest(BaseRecordRequest):
    """Payload for updating an existing scheduled record."""

    subject_key: str = Field("", alias="subjectKey")
    interval_name: str = Field("", alias="intervalName")


class CreateNewRecordRequest(BaseRecordRequest):
    """Payload for creating a new unscheduled record."""

    subject_key: str = Field("", alias="subjectKey")


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
