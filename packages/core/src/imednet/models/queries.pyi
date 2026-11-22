# pylint: disable=duplicate-code
"""Query and annotation models for iMedNet."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, overload

from pydantic import Field, model_validator

from imednet.models.base import ImednetBaseModel

class QueryStatus(str, Enum):
    """Enumeration of clinical query discrepancy lifecycle states."""

    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"

    @classmethod
    def _missing_(cls, value: object) -> QueryStatus | None: ...
    def can_transition_to(self, target: QueryStatus | str) -> bool: ...
    def validate_transition_to(self, target: QueryStatus | str) -> bool: ...
    @property
    def allowed_transitions(self) -> set[QueryStatus]: ...

ALLOWED_QUERY_TRANSITIONS: dict[QueryStatus, set[QueryStatus]]

ALLOWED_TRANSITIONS = ALLOWED_QUERY_TRANSITIONS

def _coerce_query_status(val: QueryStatus | str, param_name: str) -> QueryStatus: ...
def is_valid_query_transition(
    from_status: QueryStatus | str, to_status: QueryStatus | str
) -> bool: ...

is_valid_transition = is_valid_query_transition

def validate_query_transition(
    from_status: QueryStatus | str, to_status: QueryStatus | str
) -> bool: ...

validate_transition = validate_query_transition
_CLAUSE_SPLIT_RE = re.compile('[,;|/()]|\\bbut\\b')
_WORD_TOKEN_RE = re.compile("[a-z0-9']+")
_NEGATION_RE = re.compile("^(?:not|no|non|un|never|without|neither|nor|cannot|.*n't)$")
_ANSWER_TOKEN_RE = re.compile("^(?:re)?answer(?:s|ed|ing)?$")
_CANCEL_TOKEN_RE = re.compile("^(?:re)?cancel(?:s|ed|led|ing|ling|lation)?$")
_CLOSE_TOKEN_RE = re.compile("^(?:re)?(?:close|closes|closed|closing|closure)$")

def _has_non_negated_token(text: str, pattern: re.Pattern[str]) -> bool: ...
def calculate_legacy_status(query_comments: list[Any] | None) -> QueryStatus: ...

class QueryComment(ImednetBaseModel):
    """A comment or response within a data query thread."""

    closed: bool | None = Field(default=None, alias="closed")
    sequence: int | None = Field(default=None, alias="sequence")
    annotation_status: str | None = Field(default=None, alias="annotationStatus")
    user: str | None = Field(default=None, alias="user")
    comment: str | None = Field(default=None, alias="comment")
    date: str | None = Field(default=None, alias="date")

    @overload
    def __init__(
        self,
        *,
        closed: bool | None = None,
        sequence: int | None = None,
        annotation_status: str | None = None,
        user: str | None = None,
        comment: str | None = None,
        date: str | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        closed: bool | None = None,
        sequence: int | None = None,
        annotationStatus: str | None = None,
        user: str | None = None,
        comment: str | None = None,
        date: str | None = None,
    ) -> None: ...

class Query(ImednetBaseModel):
    """Represents a data query (discrepancy) raised on a record."""

    query_comments: list[QueryComment] = Field(default_factory=list, alias="queryComments")
    status: QueryStatus = Field(default=QueryStatus.OPEN, alias="status")
    study_key: str | None = Field(default=None, alias="studyKey")
    subject_id: int | None = Field(default=None, alias="subjectId")
    annotation_id: int | None = Field(default=None, alias="annotationId")
    description: str | None = Field(default=None, alias="description")
    record_id: int | None = Field(default=None, alias="recordId")
    variable: str | None = Field(default=None, alias="variable")
    subject_key: str | None = Field(default=None, alias="subjectKey")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_modified: str | None = Field(default=None, alias="dateModified")
    annotation_type: str | None = Field(default=None, alias="annotationType")
    subject_oid: str | None = Field(default=None, alias="subjectOid")
    type: str | None = Field(default=None, alias="type")
    last_updated: str | None = Field(default=None, alias="lastUpdated")

    @overload
    def __init__(
        self,
        *,
        query_comments: list[QueryComment] = ...,
        status: QueryStatus = QueryStatus.OPEN,
        study_key: str | None = None,
        subject_id: int | None = None,
        annotation_id: int | None = None,
        description: str | None = None,
        record_id: int | None = None,
        variable: str | None = None,
        subject_key: str | None = None,
        date_created: str | None = None,
        date_modified: str | None = None,
        annotation_type: str | None = None,
        subject_oid: str | None = None,
        type: str | None = None,
        last_updated: str | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        queryComments: list[QueryComment] = ...,
        status: QueryStatus = QueryStatus.OPEN,
        studyKey: str | None = None,
        subjectId: int | None = None,
        annotationId: int | None = None,
        description: str | None = None,
        recordId: int | None = None,
        variable: str | None = None,
        subjectKey: str | None = None,
        dateCreated: str | None = None,
        dateModified: str | None = None,
        annotationType: str | None = None,
        subjectOid: str | None = None,
        type: str | None = None,
        lastUpdated: str | None = None,
    ) -> None: ...
    @model_validator(mode="before")
    @classmethod
    def _resolve_legacy_status(cls, data: Any) -> Any: ...
    def transition_to(self, target_status: QueryStatus | str) -> QueryStatus: ...
    def update_status(self, target_status: QueryStatus | str) -> QueryStatus: ...
    def can_transition_to(self, target_status: QueryStatus | str) -> bool: ...
    def __setattr__(self, name: str, value: Any) -> None: ...

__all__ = [
    "ALLOWED_QUERY_TRANSITIONS",
    "ALLOWED_TRANSITIONS",
    "Query",
    "QueryComment",
    "QueryStatus",
    "calculate_legacy_status",
    "is_valid_query_transition",
    "is_valid_transition",
    "validate_query_transition",
    "validate_transition",
]
