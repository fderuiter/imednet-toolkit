# pylint: disable=duplicate-code
"""Record (form instance) models for iMedNet."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import (
    ConfigDict,
    Field,
    RootModel,
    ValidatorFunctionWrapHandler,
    field_validator,
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
    keyword_id: int | None = Field(default=None, alias="keywordId")
    date_added: datetime | None = Field(default=None, alias="dateAdded")


Keyword = ModelEngine.get_model('Keyword', Keyword)


class Record(ImednetBaseModel):
    """A data record for a subject, form, and visit."""

    keywords: list[Keyword] | None = Field(default_factory=list[Keyword], alias="keywords")
    record_data: dict[str, Any] | None = Field(default_factory=dict[str, Any], alias="recordData")
    record_status: RecordStatus | str | None = Field(default=None, alias="recordStatus")
    deleted: bool | None = Field(default=None, alias="deleted")

    @field_validator("record_status", mode="wrap")
    @classmethod
    def _validate_record_status(
        cls,
        value: Any,
        handler: ValidatorFunctionWrapHandler,
    ) -> RecordStatus | str | None:
        """Preserve verbatim record statuses unless matching an exact RecordStatus value."""
        return coerce_field_status(RecordStatus, value, handler)

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
            deleted=self.deleted,
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
    """Base class for record creation and update requests.

    Declares all 14 optional iMednet REST API ``POST /records`` write attributes
    with ``None`` defaults, forbids undeclared keys (``extra="forbid"``), and
    serializes to camelCase keys omitting ``None`` and blank identifiers by default.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

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

    @field_validator("record_status", mode="wrap")
    @classmethod
    def _validate_request_record_status(
        cls,
        value: Any,
        handler: ValidatorFunctionWrapHandler,
    ) -> RecordStatus | str | None:
        """Normalize blank strings to None and coerce matching RecordStatus values."""
        if isinstance(value, str) and not value.strip():
            return None
        return coerce_field_status(RecordStatus, value, handler)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """Serialize record request payload to camelCase keys, omitting None fields by default."""
        kwargs.setdefault("by_alias", True)
        kwargs.setdefault("exclude_none", True)
        dumped: dict[str, Any] = super().model_dump(**kwargs)
        for status_key in ("recordStatus", "record_status"):
            raw_status = dumped.get(status_key)
            if isinstance(raw_status, Enum):
                dumped[status_key] = str(raw_status.value)
        if kwargs.get("exclude_none"):
            for key in (
                "form_key",
                "formKey",
                "site_name",
                "siteName",
                "subject_key",
                "subjectKey",
                "subject_oid",
                "subjectOid",
                "interval_name",
                "intervalName",
                "record_oid",
                "recordOid",
                "record_status",
                "recordStatus",
                "reason_for_change",
                "reasonForChange",
            ):
                val = dumped.get(key)
                if isinstance(val, str) and not val.strip():
                    dumped.pop(key, None)
        return dumped


class RegisterSubjectRequest(BaseRecordRequest):
    """Payload for registering (enrolling) a new subject.

    Typically supplies ``formKey`` (or ``formId``) and a site identifier
    (``siteName`` or ``siteId``). Optional ``subjectKey`` and ``subjectOid``
    attributes are omitted when ``None`` or blank so server-assigned subject
    keys continue working by default; when explicitly supplied, they are
    preserved for enrollment and deduplication.
    """


class UpdateScheduledRecordRequest(BaseRecordRequest):
    """Payload for updating an existing scheduled record.

    Targets a scheduled form instance via ``formKey``/``formId``,
    ``subjectKey``/``subjectId``/``subjectOid``, and ``intervalName``/``intervalId``
    (or ``visitId``), with optional ``recordStatus`` and ``reasonForChange``.
    """


class CreateNewRecordRequest(BaseRecordRequest):
    """Payload for creating a new unscheduled record.

    Targets a form and subject via ``formKey``/``formId`` and
    ``subjectKey``/``subjectId``/``subjectOid``, with optional site, status,
    and audit reason attributes.
    """


class UpdateRecordRequest(BaseRecordRequest):
    """Payload for directly updating an existing record by identifier.

    Targets an existing record via ``recordId``, ``recordOid``, or ``visitId``,
    with optional ``formKey``/``formId``, ``recordStatus``, ``reasonForChange``,
    and clinical ``data`` updates.
    """


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
