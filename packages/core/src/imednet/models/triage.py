"""Models for triage and reviewer workflows."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field, field_validator

from imednet.models.base import ImednetBaseModel


class TriageStatus(str, Enum):
    """Enumeration of possible triage statuses."""

    NEW = "NEW"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED = "RESOLVED"

    def can_transition_to(self, target: TriageStatus | str) -> bool:
        """Check if this status can transition to target status."""
        return is_valid_transition(self, target)

    def validate_transition_to(self, target: TriageStatus | str) -> bool:
        """Validate transition from this status to target status, raising ValueError if invalid."""
        return validate_transition(self, target)

    @property
    def allowed_transitions(self) -> set[TriageStatus]:
        """Return the set of allowed target statuses from this status."""
        return ALLOWED_TRANSITIONS.get(self, set())


ALLOWED_TRANSITIONS: dict[TriageStatus, set[TriageStatus]] = {
    TriageStatus.NEW: {TriageStatus.UNDER_REVIEW, TriageStatus.RESOLVED},
    TriageStatus.UNDER_REVIEW: {TriageStatus.RESOLVED, TriageStatus.NEW},
    TriageStatus.RESOLVED: {TriageStatus.UNDER_REVIEW},
}
ALLOWED_STATUS_TRANSITIONS = ALLOWED_TRANSITIONS


def is_valid_transition(
    from_status: TriageStatus | str,
    to_status: TriageStatus | str,
) -> bool:
    """Return whether transitioning from from_status to to_status is permitted.

    Args:
        from_status: The source triage status.
        to_status: The target triage status.

    Returns:
        True if the transition is allowed, False otherwise.
    """
    try:
        from_enum = (
            from_status if isinstance(from_status, TriageStatus) else TriageStatus(str(from_status))
        )
        to_enum = to_status if isinstance(to_status, TriageStatus) else TriageStatus(str(to_status))
    except (ValueError, KeyError):
        return False
    return to_enum in ALLOWED_TRANSITIONS.get(from_enum, set())


def validate_transition(
    from_status: TriageStatus | str,
    to_status: TriageStatus | str,
) -> bool:
    """Validate that transitioning from from_status to to_status is permitted.

    Args:
        from_status: The source triage status.
        to_status: The target triage status.

    Returns:
        True if the transition is permitted.

    Raises:
        ValueError: If either status is invalid or the transition is forbidden.
    """
    try:
        from_enum = (
            from_status if isinstance(from_status, TriageStatus) else TriageStatus(str(from_status))
        )
    except (ValueError, KeyError) as err:
        raise ValueError(f"Invalid from_status: {from_status}") from err

    try:
        to_enum = to_status if isinstance(to_status, TriageStatus) else TriageStatus(str(to_status))
    except (ValueError, KeyError) as err:
        raise ValueError(f"Invalid to_status: {to_status}") from err

    allowed = ALLOWED_TRANSITIONS.get(from_enum, set())
    if to_enum not in allowed:
        allowed_names = sorted(s.value for s in allowed)
        raise ValueError(
            f"Cannot transition triage status from {from_enum.value} to {to_enum.value}. "
            f"Allowed transitions from {from_enum.value}: {allowed_names}"
        )
    return True


validate_status_transition = validate_transition
is_valid_status_transition = is_valid_transition
can_transition = is_valid_transition


class TriageAnnotation(ImednetBaseModel):
    """Reviewer note attached to a triage item."""

    annotation_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    comment: str = Field(..., min_length=1)
    timestamp: datetime


class TriageHistoryEntry(ImednetBaseModel):
    """Status transition audit entry for a triage item."""

    transition_id: str = Field(..., min_length=1)
    from_status: TriageStatus
    to_status: TriageStatus
    user_id: str = Field(..., min_length=1)
    comment: str | None = None
    timestamp: datetime

    @field_validator("comment", check_fields=False, mode="before")
    @classmethod
    def _normalise_comment(cls, value: object) -> object:
        """Normalize comment string, returning None if empty or whitespace."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class TriageItem(ImednetBaseModel):
    """Core triage item used by queue and reviewer workflows."""

    item_id: str = Field(..., min_length=1)
    study_key: str = Field(..., min_length=1)
    status: TriageStatus = TriageStatus.NEW
    assignee: str | None = None
    severity: str = Field(..., min_length=1)
    annotations: list[TriageAnnotation] = Field(default_factory=list)
    history: list[TriageHistoryEntry] = Field(default_factory=list)
