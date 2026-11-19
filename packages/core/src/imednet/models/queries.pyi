# pylint: disable=duplicate-code
"""Query and annotation models for iMedNet."""

from __future__ import annotations

from enum import Enum
from typing import Any

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

def calculate_legacy_status(query_comments: list[Any] | None) -> QueryStatus: ...

class QueryComment(ImednetBaseModel):
    """A comment or response within a data query thread."""

    closed: bool | None = Field(default=None, alias="closed")
    sequence: int | None = Field(default=None, alias="sequence")
    annotation_status: str | None = Field(default=None, alias="annotationStatus")
    user: str | None = Field(default=None, alias="user")
    comment: str | None = Field(default=None, alias="comment")
    date: str | None = Field(default=None, alias="date")

class Query(ImednetBaseModel):
    """Represents a data query (discrepancy) raised on a record."""

    query_comments: list[QueryComment] = Field(default_factory=list, alias="queryComments")
    status: QueryStatus = Field(default=QueryStatus.OPEN, alias="status")
    study_key: str | None
    subject_id: int | None
    annotation_id: int | None
    description: str | None
    record_id: int | None
    variable: str | None
    subject_key: str | None
    date_created: str | None
    date_modified: str | None
    annotation_type: str | None
    subject_oid: str | None
    type: str | None
    last_updated: str | None

    @model_validator(mode="before")
    @classmethod
    def _resolve_legacy_status(cls, data: Any) -> Any: ...
    def transition_to(self, target_status: QueryStatus | str) -> QueryStatus: ...
    def update_status(self, target_status: QueryStatus | str) -> QueryStatus: ...
    def can_transition_to(self, target_status: QueryStatus | str) -> bool: ...
    def __setattr__(self, name: str, value: Any) -> None: ...
