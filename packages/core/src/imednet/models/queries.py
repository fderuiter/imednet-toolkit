# pylint: disable=duplicate-code
"""Query and annotation models for iMedNet."""

from __future__ import annotations

import re
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


_CLAUSE_SPLIT_RE = re.compile(r"[,;|/()]|\bbut\b")
_WORD_TOKEN_RE = re.compile(r"[a-z0-9']+")
_NEGATION_RE = re.compile(r"^(?:not|no|non|un|never|without|neither|nor|cannot|.*n't)$")
_ANSWER_TOKEN_RE = re.compile(r"^(?:re)?answer(?:s|ed|ing)?$")
_CANCEL_TOKEN_RE = re.compile(r"^(?:re)?cancel(?:s|ed|led|ing|ling|lation)?$")
_CLOSE_TOKEN_RE = re.compile(r"^(?:re)?(?:close|closes|closed|closing|closure)$")


def _has_non_negated_token(text: str, pattern: re.Pattern[str]) -> bool:
    """Check whether any clause in text contains a non-negated token matching pattern."""
    if not text:
        return False
    for clause in _CLAUSE_SPLIT_RE.split(text.lower()):
        tokens = _WORD_TOKEN_RE.findall(clause)
        for idx, token in enumerate(tokens):
            if pattern.fullmatch(token):
                window = tokens[max(0, idx - 3) : idx]
                if not any(_NEGATION_RE.fullmatch(prev) for prev in window):
                    return True
    return False


def calculate_legacy_status(query_comments: list[Any] | None) -> QueryStatus:
    """Calculate fallback QueryStatus for legacy payloads lacking explicit status."""
    if not query_comments:
        return QueryStatus.OPEN

    def _comment_sort_key(indexed_item: tuple[int, Any]) -> tuple[int, int]:
        idx, item = indexed_item
        val = item.get("sequence") if isinstance(item, dict) else getattr(item, "sequence", None)
        try:
            seq = int(val) if val is not None else -1
        except (TypeError, ValueError):
            seq = -1
        return (seq, idx)

    _, latest = max(enumerate(query_comments), key=_comment_sort_key)
    if isinstance(latest, dict):
        raw_closed = latest.get("closed")
        ann_status = latest.get("annotation_status")
        if ann_status is None:
            ann_status = latest.get("annotationStatus")
    else:
        raw_closed = getattr(latest, "closed", None)
        ann_status = getattr(latest, "annotation_status", None)
        if ann_status is None:
            ann_status = getattr(latest, "annotationStatus", None)

    if isinstance(raw_closed, bool):
        is_closed: bool | None = raw_closed
    elif isinstance(raw_closed, str) and raw_closed.strip().lower() in {"true", "false"}:
        is_closed = raw_closed.strip().lower() == "true"
    else:
        is_closed = None

    ann_str = str(ann_status).strip() if ann_status else ""

    if is_closed is False:
        if _has_non_negated_token(ann_str, _ANSWER_TOKEN_RE):
            return QueryStatus.ANSWERED
        return QueryStatus.OPEN

    if is_closed is True:
        if _has_non_negated_token(ann_str, _CANCEL_TOKEN_RE):
            return QueryStatus.CANCELLED
        return QueryStatus.CLOSED

    if _has_non_negated_token(ann_str, _CANCEL_TOKEN_RE):
        return QueryStatus.CANCELLED
    if _has_non_negated_token(ann_str, _ANSWER_TOKEN_RE):
        return QueryStatus.ANSWERED
    if _has_non_negated_token(ann_str, _CLOSE_TOKEN_RE):
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
            resolved = dict(data)
            status_val = resolved.get("status")
            if status_val is None:
                comments = resolved.get("query_comments")
                if comments is None:
                    comments = resolved.get("queryComments")
                resolved["status"] = calculate_legacy_status(comments)
            elif not isinstance(status_val, QueryStatus):
                resolved["status"] = QueryStatus(status_val)
            return resolved
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
