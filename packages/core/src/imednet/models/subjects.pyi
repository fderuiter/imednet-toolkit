# pylint: disable=duplicate-code
"""Models for subjects and participant keywords."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field, ValidatorFunctionWrapHandler, field_validator

from imednet.models.base import ImednetBaseModel

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

    def __init__(
        self,
        *,
        keyword_name: str = "",
        keywordName: str = "",
        keyword_key: str = "",
        keywordKey: str = "",
        keyword_id: int = 0,
        keywordId: int = 0,
        date_added: datetime = ...,
        dateAdded: datetime = ...,
    ) -> None: ...

class Subject(ImednetBaseModel):
    """A subject (participant) in a study, with status and site info."""

    keywords: list[SubjectKeyword] = Field(default_factory=list, alias="keywords")
    subject_status: SubjectStatus | str | None = Field(default=None, alias="subjectStatus")
    deleted: bool | None = Field(default=None, alias="deleted")
    study_key: str | None = Field(default=None, alias="studyKey")
    subject_id: int | None = Field(default=None, alias="subjectId")
    subject_key: str | None = Field(default=None, alias="subjectKey")
    site_id: int | None = Field(default=None, alias="siteId")
    site_name: str | None = Field(default=None, alias="siteName")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_modified: str | None = Field(default=None, alias="dateModified")
    enrollment_start_date: str | None = Field(default=None, alias="enrollmentStartDate")
    subject_oid: str | None = Field(default=None, alias="subjectOid")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    def __init__(
        self,
        *,
        keywords: list[SubjectKeyword] = ...,
        subject_status: SubjectStatus | str | None = None,
        subjectStatus: SubjectStatus | str | None = None,
        deleted: bool | None = None,
        study_key: str | None = None,
        studyKey: str | None = None,
        subject_id: int | None = None,
        subjectId: int | None = None,
        subject_key: str | None = None,
        subjectKey: str | None = None,
        site_id: int | None = None,
        siteId: int | None = None,
        site_name: str | None = None,
        siteName: str | None = None,
        date_created: str | None = None,
        dateCreated: str | None = None,
        date_modified: str | None = None,
        dateModified: str | None = None,
        enrollment_start_date: str | None = None,
        enrollmentStartDate: str | None = None,
        subject_oid: str | None = None,
        subjectOid: str | None = None,
        last_updated: str | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...
    @field_validator("subject_status", mode="wrap")
    @classmethod
    def _validate_subject_status(
        cls, value: Any, handler: ValidatorFunctionWrapHandler
    ) -> SubjectStatus | str | None: ...
    @property
    def is_deleted(self) -> bool: ...
    @property
    def effective_status(self) -> SubjectStatus | str | None: ...
    def can_transition_to(self, target_status: SubjectStatus | str) -> bool: ...
    def transition_to(self, target_status: SubjectStatus | str) -> SubjectStatus | str: ...
    def update_status(self, target_status: SubjectStatus | str) -> SubjectStatus | str: ...
    def __setattr__(self, name: str, value: Any) -> None: ...

__all__ = [
    "ALLOWED_SUBJECT_TRANSITIONS",
    "Subject",
    "SubjectKeyword",
    "SubjectStatus",
    "is_valid_subject_transition",
    "normalize_subject_status",
    "validate_subject_transition",
]
