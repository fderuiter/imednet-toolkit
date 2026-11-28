# pylint: disable=duplicate-code
"""Models for subjects and participant keywords."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field, ValidatorFunctionWrapHandler, field_validator

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


class SubjectStatus(str, Enum):
    """Normalized lifecycle statuses for clinical study subjects."""

    SCREENED = "Screened"
    REGISTERED = "Registered"
    BASELINE = "Baseline"
    ENROLLED = "Enrolled"
    ACTIVE = "Active"
    COMPLETED = "Completed"
    WITHDRAWN = "Withdrawn"
    SCREEN_FAILED = "Screen Failed"
    CLOSED = "Closed"
    INACTIVE = "Inactive"
    DELETED = "DELETED"

    def __str__(self) -> str:
        """Return the canonical status string value."""
        return str(self.value)

    @classmethod
    def _missing_(cls, value: object) -> SubjectStatus | None:
        """Resolve subject status case- and separator-insensitively."""
        return resolve_status_member(
            cls,
            value,
            aliases={"screen failure": cls.SCREEN_FAILED},
        )

    def can_transition_to(self, target: SubjectStatus | str) -> bool:
        """Return True if transitioning from this status to *target* is permitted."""
        return is_valid_subject_transition(self, target)

    def validate_transition_to(self, target: SubjectStatus | str) -> bool:
        """Validate transition to *target*, raising ValueError if disallowed."""
        return validate_subject_transition(self, target)

    @property
    def allowed_transitions(self) -> set[SubjectStatus]:
        """Return permissible next statuses from this status."""
        return set(ALLOWED_SUBJECT_TRANSITIONS.get(self, set()))


ALLOWED_SUBJECT_TRANSITIONS: dict[SubjectStatus, set[SubjectStatus]] = (
    build_terminal_transition_map(
        SubjectStatus,
        SubjectStatus.DELETED,
    )
)


def normalize_subject_status(
    value: SubjectStatus | str | None,
) -> SubjectStatus | str | None:
    """Normalize known subject statuses while preserving custom study status strings."""
    return coerce_status(SubjectStatus, value)


def is_valid_subject_transition(
    from_status: SubjectStatus | str | None,
    to_status: SubjectStatus | str,
    *,
    deleted: bool | None = None,
) -> bool:
    """Return True if transitioning a subject from *from_status* to *to_status* is valid."""
    return is_valid_status_transition(
        SubjectStatus,
        ALLOWED_SUBJECT_TRANSITIONS,
        SubjectStatus.DELETED,
        "Subject",
        from_status,
        to_status,
        deleted=deleted,
    )


def validate_subject_transition(
    from_status: SubjectStatus | str | None,
    to_status: SubjectStatus | str,
    *,
    deleted: bool | None = None,
) -> bool:
    """Validate a subject status transition, raising ValueError if soft-deleted or invalid."""
    return validate_status_transition(
        SubjectStatus,
        ALLOWED_SUBJECT_TRANSITIONS,
        SubjectStatus.DELETED,
        "Subject",
        from_status,
        to_status,
        deleted=deleted,
    )


class SubjectKeyword(ImednetBaseModel):
    """A keyword or tag associated with a subject."""

    keyword_name: str = Field("", alias="keywordName")
    keyword_key: str = Field("", alias="keywordKey")
    keyword_id: int | None = Field(default=None, alias="keywordId")
    date_added: datetime | None = Field(default=None, alias="dateAdded")


SubjectKeyword = ModelEngine.get_model('SubjectKeyword', SubjectKeyword)


class Subject(ImednetBaseModel):
    """A subject (participant) in a study, with status and site info."""

    keywords: list[SubjectKeyword] | None = Field(
        default_factory=list[SubjectKeyword], alias="keywords"
    )
    subject_status: SubjectStatus | str | None = Field(default=None, alias="subjectStatus")
    deleted: bool | None = Field(default=None, alias="deleted")

    @field_validator("subject_status", mode="wrap")
    @classmethod
    def _validate_subject_status(
        cls,
        value: Any,
        handler: ValidatorFunctionWrapHandler,
    ) -> SubjectStatus | str | None:
        """Preserve verbatim subject statuses unless matching an exact SubjectStatus value."""
        return coerce_field_status(SubjectStatus, value, handler)

    @property
    def is_deleted(self) -> bool:
        """Return True when the subject is soft-deleted."""
        return is_entity_deleted(self.deleted, self.subject_status, SubjectStatus.DELETED)

    @property
    def effective_status(self) -> SubjectStatus | str | None:
        """Return DELETED when ``deleted=True``, otherwise ``subject_status``."""
        return resolve_effective_status(self.deleted, self.subject_status, SubjectStatus.DELETED)

    def can_transition_to(self, target_status: SubjectStatus | str) -> bool:
        """Return True if transitioning to *target_status* is allowed."""
        return is_valid_subject_transition(
            self.subject_status,
            target_status,
            deleted=self.deleted,
        )

    def transition_to(self, target_status: SubjectStatus | str) -> SubjectStatus | str:
        """Transition subject to *target_status*, enforcing soft-delete invariants."""
        return apply_entity_transition(
            self,
            enum_cls=SubjectStatus,
            allowed_transitions=ALLOWED_SUBJECT_TRANSITIONS,
            deleted_member=SubjectStatus.DELETED,
            entity_label="Subject",
            status_field="subject_status",
            target_status=target_status,
        )

    def update_status(self, target_status: SubjectStatus | str) -> SubjectStatus | str:
        """Alias for :meth:`transition_to`."""
        return self.transition_to(target_status)

    def __setattr__(self, name: str, value: Any) -> None:
        """Enforce terminal soft-delete invariants on direct attribute mutation."""
        setattr_with_soft_delete_guard(
            self,
            name,
            value,
            enum_cls=SubjectStatus,
            deleted_member=SubjectStatus.DELETED,
            entity_label="Subject",
            status_field="subject_status",
        )


Subject = ModelEngine.get_model('Subject', Subject)

__all__ = [
    "ALLOWED_SUBJECT_TRANSITIONS",
    "Subject",
    "SubjectKeyword",
    "SubjectStatus",
    "is_valid_subject_transition",
    "normalize_subject_status",
    "validate_subject_transition",
]
