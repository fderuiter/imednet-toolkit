# pylint: disable=duplicate-code
"""Models for subjects and participant keywords."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field, field_validator

from imednet.models.base import ImednetBaseModel

def _normalize_subject_token(value: str) -> str: ...

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

    def __str__(self) -> str: ...
    @classmethod
    def _missing_(cls, value: object) -> SubjectStatus | None: ...
    def can_transition_to(self, target: SubjectStatus | str) -> bool: ...
    def validate_transition_to(self, target: SubjectStatus | str) -> bool: ...
    @property
    def allowed_transitions(self) -> set[SubjectStatus]: ...

_NON_DELETED_SUBJECT_STATUSES: set[SubjectStatus]
ALLOWED_SUBJECT_TRANSITIONS: dict[SubjectStatus, set[SubjectStatus]]

def normalize_subject_status(value: SubjectStatus | str | None) -> SubjectStatus | str | None: ...
def is_valid_subject_transition(
    from_status: SubjectStatus | str | None,
    to_status: SubjectStatus | str,
    *,
    deleted: bool | None = None,
) -> bool: ...
def validate_subject_transition(
    from_status: SubjectStatus | str | None,
    to_status: SubjectStatus | str,
    *,
    deleted: bool | None = None,
) -> bool: ...

class SubjectKeyword(ImednetBaseModel):
    """A keyword or tag associated with a subject."""

    keyword_name: str = Field("", alias="keywordName")
    keyword_key: str = Field("", alias="keywordKey")
    keyword_id: int = Field(0, alias="keywordId")
    date_added: datetime = Field(default_factory=datetime.now, alias="dateAdded")

class Subject(ImednetBaseModel):
    """A subject (participant) in a study, with status and site info."""

    keywords: list[SubjectKeyword] = Field(default_factory=list, alias="keywords")
    subject_status: SubjectStatus | str | None = Field(default=None, alias="subjectStatus")
    deleted: bool | None = Field(default=None, alias="deleted")
    study_key: str | None
    subject_id: int | None
    subject_key: str | None
    site_id: int | None
    site_name: str | None
    date_created: str | None
    date_modified: str | None
    enrollment_start_date: str | None
    subject_oid: str | None
    last_updated: str | None

    @field_validator("subject_status", mode="before")
    @classmethod
    def _validate_subject_status(cls, value: Any) -> SubjectStatus | str | None: ...
    @property
    def is_deleted(self) -> bool: ...
    @property
    def effective_status(self) -> SubjectStatus | str | None: ...
    def can_transition_to(self, target_status: SubjectStatus | str) -> bool: ...
    def transition_to(self, target_status: SubjectStatus | str) -> SubjectStatus | str: ...
    def update_status(self, target_status: SubjectStatus | str) -> SubjectStatus | str: ...
    def __setattr__(self, name: str, value: Any) -> None: ...
