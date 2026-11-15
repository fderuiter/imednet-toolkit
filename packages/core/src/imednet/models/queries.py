"""Query and annotation models for iMedNet."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, model_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


class QueryStatus(str, Enum):
    """Enumeration of clinical query discrepancy lifecycle states."""

    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"

    @classmethod
    def _missing_(cls, value: object) -> QueryStatus | None:
        """Resolve query status case-insensitively from string representations."""
        if isinstance(value, str):
            candidate = value.strip().upper()
            for member in cls:
                if member.value == candidate:
                    return member
        return None

    def can_transition_to(self, target: QueryStatus | str) -> bool:
        """Check whether a transition to target query status is allowed."""
        return is_valid_query_transition(self, target)

    def validate_transition_to(self, target: QueryStatus | str) -> bool:
        """Validate transition to target status, raising ValueError if disallowed."""
        return validate_query_transition(self, target)

    @property
    def allowed_transitions(self) -> set[QueryStatus]:
        """Set of permissible next statuses from the current query state."""
        return ALLOWED_QUERY_TRANSITIONS.get(self, set())


ALLOWED_QUERY_TRANSITIONS: dict[QueryStatus, set[QueryStatus]] = {
    QueryStatus.OPEN: {QueryStatus.ANSWERED, QueryStatus.CLOSED, QueryStatus.CANCELLED},
    QueryStatus.ANSWERED: {QueryStatus.OPEN, QueryStatus.CLOSED, QueryStatus.CANCELLED},
    QueryStatus.CLOSED: {QueryStatus.OPEN},
    QueryStatus.CANCELLED: set(),
}

ALLOWED_TRANSITIONS = ALLOWED_QUERY_TRANSITIONS


def _coerce_query_status(val: QueryStatus | str, param_name: str) -> QueryStatus:
    """Helper to convert status input into QueryStatus enum."""
    if isinstance(val, QueryStatus):
        return val
    try:
        return QueryStatus(val)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Invalid {param_name}: {val}") from exc


def is_valid_query_transition(
    from_status: QueryStatus | str,
    to_status: QueryStatus | str,
) -> bool:
    """Check if transitioning between query discrepancy statuses is permitted."""
    try:
        src = _coerce_query_status(from_status, "from_status")
        dst = _coerce_query_status(to_status, "to_status")
    except ValueError:
        return False
    return dst in ALLOWED_QUERY_TRANSITIONS.get(src, set())


is_valid_transition = is_valid_query_transition


def validate_query_transition(
    from_status: QueryStatus | str,
    to_status: QueryStatus | str,
) -> bool:
    """Validate query status change, raising ValueError on forbidden transitions."""
    src = _coerce_query_status(from_status, "from_status")
    dst = _coerce_query_status(to_status, "to_status")

    permitted = ALLOWED_QUERY_TRANSITIONS.get(src, set())
    if dst not in permitted:
        options = sorted(p.value for p in permitted)
        raise ValueError(
            f"Cannot transition query status from {src.value} to {dst.value}. "
            f"Permitted transitions from {src.value}: {options}"
        )
    return True


validate_transition = validate_query_transition


def calculate_legacy_status(query_comments: list[Any] | None) -> QueryStatus:
    """Calculate fallback QueryStatus for legacy payloads lacking explicit status."""
    if not query_comments:
        return QueryStatus.OPEN

    def _extract_seq(item: Any) -> int:
        val = item.get("sequence") if isinstance(item, dict) else getattr(item, "sequence", None)
        return int(val) if val is not None else -1

    latest = max(query_comments, key=_extract_seq)
    if isinstance(latest, dict):
        is_closed = latest.get("closed")
        ann_status = latest.get("annotation_status") or latest.get("annotationStatus")
    else:
        is_closed = getattr(latest, "closed", None)
        ann_status = getattr(latest, "annotation_status", None)

    if is_closed is True:
        return QueryStatus.CLOSED

    if ann_status:
        ann_str = str(ann_status).strip().lower()
        if "cancel" in ann_str:
            return QueryStatus.CANCELLED
        if "answer" in ann_str:
            return QueryStatus.ANSWERED
        if "close" in ann_str:
            return QueryStatus.CLOSED

    return QueryStatus.OPEN


class QueryComment(ImednetBaseModel):
    """A comment or response within a data query thread."""

    closed: bool | None = Field(default=None, alias="closed")
    sequence: int | None = Field(default=None, alias="sequence")
    annotation_status: str | None = Field(default=None, alias="annotationStatus")
    user: str | None = Field(default=None, alias="user")
    comment: str | None = Field(default=None, alias="comment")
    date: str | None = Field(default=None, alias="date")


QueryComment = ModelEngine.get_model('QueryComment', QueryComment)


class Query(ImednetBaseModel):
    """Represents a data query (discrepancy) raised on a record."""

    query_comments: list[QueryComment] = Field(default_factory=list, alias="queryComments")
    status: QueryStatus = Field(default=QueryStatus.OPEN, alias="status")

    @model_validator(mode="before")
    @classmethod
    def _resolve_legacy_status(cls, data: Any) -> Any:
        """Resolve query status using explicit value or fallback legacy calculation."""
        if isinstance(data, dict):
            status_val = data.get("status")
            if status_val is None:
                comments = data.get("query_comments")
                if comments is None:
                    comments = data.get("queryComments")
                data["status"] = calculate_legacy_status(comments)
            elif not isinstance(status_val, QueryStatus):
                data["status"] = QueryStatus(status_val)
        return data

    def transition_to(self, target_status: QueryStatus | str) -> QueryStatus:
        """Transition query to a new status with validation."""
        target_enum = _coerce_query_status(target_status, "target_status")
        validate_query_transition(self.status, target_enum)
        super().__setattr__("status", target_enum)
        return self.status

    def update_status(self, target_status: QueryStatus | str) -> QueryStatus:
        """Update query discrepancy status, validating state lifecycle rules."""
        return self.transition_to(target_status)

    def can_transition_to(self, target_status: QueryStatus | str) -> bool:
        """Determine if transitioning to target_status is permissible."""
        return is_valid_query_transition(self.status, target_status)

    def __setattr__(self, name: str, value: Any) -> None:
        """Guard direct attribute assignment to enforce valid state transitions."""
        if name == "status" and "status" in self.__dict__:
            current_status = self.__dict__["status"]
            if current_status is not None:
                target_enum = _coerce_query_status(value, "target_status")
                validate_query_transition(current_status, target_enum)
                super().__setattr__(name, target_enum)
                return
        super().__setattr__(name, value)


Query = ModelEngine.get_model('Query', Query)
