# pylint: disable=duplicate-code
"""Models for subjects and participant keywords."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field, field_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


def _normalize_subject_token(value: str) -> str:
    """Collapse whitespace, underscores, and hyphens into a lowercase token."""
    return re.sub(r"[\s_-]+", " ", value.strip()).lower()


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
        if isinstance(value, str):
            normalized = _normalize_subject_token(value)
            if not normalized:
                return None
            if normalized == "screen failure":
                return cls.SCREEN_FAILED
            for member in cls:
                if (
                    _normalize_subject_token(member.value) == normalized
                    or _normalize_subject_token(member.name) == normalized
                ):
                    return member
        return None

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


_NON_DELETED_SUBJECT_STATUSES: set[SubjectStatus] = {
    s for s in SubjectStatus if s != SubjectStatus.DELETED
}

ALLOWED_SUBJECT_TRANSITIONS: dict[SubjectStatus, set[SubjectStatus]] = {
    **{
        status: (_NON_DELETED_SUBJECT_STATUSES - {status}) | {SubjectStatus.DELETED}
        for status in _NON_DELETED_SUBJECT_STATUSES
    },
    SubjectStatus.DELETED: set(),
}


def normalize_subject_status(
    value: SubjectStatus | str | None,
) -> SubjectStatus | str | None:
    """Normalize known subject statuses while preserving custom study status strings."""
    if value is None or isinstance(value, SubjectStatus):
        return value
    if isinstance(value, str):
        try:
            return SubjectStatus(value)
        except ValueError:
            return value
    return str(value)


def is_valid_subject_transition(
    from_status: SubjectStatus | str | None,
    to_status: SubjectStatus | str,
    *,
    deleted: bool | None = None,
) -> bool:
    """Return True if transitioning a subject from *from_status* to *to_status* is valid."""
    try:
        return validate_subject_transition(from_status, to_status, deleted=deleted)
    except ValueError:
        return False


def validate_subject_transition(
    from_status: SubjectStatus | str | None,
    to_status: SubjectStatus | str,
    *,
    deleted: bool | None = None,
) -> bool:
    """Validate a subject status transition, raising ValueError if soft-deleted or invalid."""
    src = normalize_subject_status(from_status)
    dst = normalize_subject_status(to_status)
    if dst is None or (isinstance(dst, str) and not dst.strip()):
        raise ValueError(f"Invalid target subject status: {to_status!r}")

    if deleted is True or src == SubjectStatus.DELETED:
        raise ValueError(
            f"Cannot transition soft-deleted Subject from {SubjectStatus.DELETED.value} to {dst!s}."
        )

    if isinstance(src, SubjectStatus) and isinstance(dst, SubjectStatus):
        permitted = ALLOWED_SUBJECT_TRANSITIONS.get(src, set())
        if dst not in permitted:
            options = sorted(p.value for p in permitted)
            raise ValueError(
                f"Cannot transition subject status from {src.value} to {dst.value}. "
                f"Permitted transitions from {src.value}: {options}"
            )
    elif src is not None and src == dst:
        raise ValueError(f"Cannot transition subject status from {src!s} to {dst!s}.")
    return True


class SubjectKeyword(ImednetBaseModel):
    """A keyword or tag associated with a subject."""

    keyword_name: str = Field("", alias="keywordName")
    keyword_key: str = Field("", alias="keywordKey")
    keyword_id: int = Field(0, alias="keywordId")
    date_added: datetime = Field(default_factory=datetime.now, alias="dateAdded")


SubjectKeyword = ModelEngine.get_model('SubjectKeyword', SubjectKeyword)


class Subject(ImednetBaseModel):
    """A subject (participant) in a study, with status and site info."""

    keywords: list[SubjectKeyword] = Field(default_factory=list, alias="keywords")
    subject_status: SubjectStatus | str | None = Field(default=None, alias="subjectStatus")
    deleted: bool | None = Field(default=None, alias="deleted")

    @field_validator("subject_status", mode="before")
    @classmethod
    def _validate_subject_status(cls, value: Any) -> SubjectStatus | str | None:
        """Normalize known subject statuses and preserve custom study strings."""
        if value is None or isinstance(value, (SubjectStatus, str)):
            return normalize_subject_status(value)
        return normalize_subject_status(str(value))

    @property
    def is_deleted(self) -> bool:
        """Return True when the subject is soft-deleted."""
        return self.deleted is True or self.subject_status == SubjectStatus.DELETED

    @property
    def effective_status(self) -> SubjectStatus | str | None:
        """Return DELETED when ``deleted=True``, otherwise ``subject_status``."""
        if self.is_deleted:
            return SubjectStatus.DELETED
        return self.subject_status

    def can_transition_to(self, target_status: SubjectStatus | str) -> bool:
        """Return True if transitioning to *target_status* is allowed."""
        return is_valid_subject_transition(
            self.subject_status,
            target_status,
            deleted=self.is_deleted,
        )

    def transition_to(self, target_status: SubjectStatus | str) -> SubjectStatus | str:
        """Transition subject to *target_status*, enforcing soft-delete invariants."""
        validate_subject_transition(
            self.subject_status,
            target_status,
            deleted=self.is_deleted,
        )
        normalized = normalize_subject_status(target_status)
        assert normalized is not None
        super().__setattr__("subject_status", normalized)
        if normalized == SubjectStatus.DELETED:
            super().__setattr__("deleted", True)
        return normalized

    def update_status(self, target_status: SubjectStatus | str) -> SubjectStatus | str:
        """Alias for :meth:`transition_to`."""
        return self.transition_to(target_status)

    def __setattr__(self, name: str, value: Any) -> None:
        """Enforce terminal soft-delete invariants on direct attribute mutation."""
        initialized = "deleted" in self.__dict__ or "subject_status" in self.__dict__
        currently_deleted = (
            self.__dict__.get("deleted") is True
            or self.__dict__.get("subject_status") == SubjectStatus.DELETED
        )
        if initialized and currently_deleted:
            if name == "deleted" and value is not True:
                raise ValueError("Cannot restore a soft-deleted Subject by clearing deleted.")
            if name == "subject_status":
                normalized = normalize_subject_status(value)
                if normalized != SubjectStatus.DELETED:
                    raise ValueError(
                        f"Cannot mutate soft-deleted Subject to active status {value!r}."
                    )
                super().__setattr__(name, normalized)
                return
        if initialized and name == "subject_status":
            normalized = normalize_subject_status(value)
            super().__setattr__(name, normalized)
            if normalized == SubjectStatus.DELETED:
                super().__setattr__("deleted", True)
            return
        super().__setattr__(name, value)


Subject = ModelEngine.get_model('Subject', Subject)
