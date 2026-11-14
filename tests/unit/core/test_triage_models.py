"""Unit tests for triage models."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from imednet.models.triage import (
    ALLOWED_STATUS_TRANSITIONS,
    ALLOWED_TRANSITIONS,
    TriageAnnotation,
    TriageHistoryEntry,
    TriageItem,
    TriageStatus,
    can_transition,
    is_valid_transition,
    validate_transition,
)


def test_triage_models_parse_and_strip_whitespace() -> None:
    """Test that triage models parse and strip whitespace."""
    item = TriageItem.model_validate(
        {
            "item_id": "  AE-1001  ",
            "study_key": "  STUDY-1 ",
            "status": "UNDER_REVIEW",
            "assignee": "  analyst  ",
            "severity": "  critical ",
            "annotations": [
                {
                    "annotation_id": "  a-1 ",
                    "user_id": "  user-1 ",
                    "comment": "  needs follow-up  ",
                    "timestamp": "2024-01-01T00:00:00Z",
                }
            ],
            "history": [
                {
                    "transition_id": "  h-1  ",
                    "from_status": "NEW",
                    "to_status": "UNDER_REVIEW",
                    "user_id": "  user-1 ",
                    "comment": "  started triage  ",
                    "timestamp": "2024-01-01T01:00:00Z",
                }
            ],
        }
    )

    assert item.item_id == "AE-1001"
    assert item.study_key == "STUDY-1"
    assert item.status == TriageStatus.UNDER_REVIEW
    assert item.assignee == "analyst"
    assert item.severity == "critical"
    assert item.annotations[0].comment == "needs follow-up"
    assert item.history[0].comment == "started triage"


def test_triage_history_blank_comment_normalizes_to_none() -> None:
    """Test that triage history blank comment normalizes to none."""
    entry = TriageHistoryEntry.model_validate(
        {
            "transition_id": "h-2",
            "from_status": "UNDER_REVIEW",
            "to_status": "RESOLVED",
            "user_id": "reviewer",
            "comment": "   ",
            "timestamp": datetime(2024, 1, 1, 2, 0, 0),
        }
    )

    assert entry.comment is None


def test_triage_json_roundtrip_keeps_enum_values() -> None:
    """Test that triage json roundtrip keeps enum values."""
    annotation = TriageAnnotation(
        annotation_id="a-2",
        user_id="reviewer",
        comment="resolved",
        timestamp=datetime(2024, 1, 2, 0, 0, 0),
    )
    item = TriageItem(
        item_id="PD-44",
        study_key="STUDY-2",
        status=TriageStatus.RESOLVED,
        assignee="manager",
        severity="warning",
        annotations=[annotation],
        history=[],
    )

    dumped = item.model_dump(mode="json")

    assert dumped["status"] == "RESOLVED"
    assert dumped["annotations"][0]["annotation_id"] == "a-2"


def test_triage_models_enforce_schema_constraints() -> None:
    """Test that triage models enforce schema constraints."""
    with pytest.raises(ValidationError) as blank_id_error:
        TriageAnnotation.model_validate(
            {
                "annotation_id": "   ",
                "user_id": "reviewer",
                "comment": "valid",
                "timestamp": "2024-01-01T00:00:00Z",
            }
        )
    assert blank_id_error.value.errors()[0]["loc"] == ("annotation_id",)

    with pytest.raises(ValidationError) as status_error:
        TriageItem.model_validate(
            {
                "item_id": "AE-1002",
                "study_key": "STUDY-1",
                "status": "NOT_A_STATUS",
                "assignee": "reviewer",
                "severity": "critical",
                "annotations": [],
                "history": [],
            }
        )
    assert status_error.value.errors()[0]["loc"] == ("status",)


def test_triage_status_allowed_transitions_rules() -> None:
    """Test deterministic allowed transition matrix for triage status."""
    assert ALLOWED_TRANSITIONS == {
        TriageStatus.NEW: {TriageStatus.UNDER_REVIEW, TriageStatus.RESOLVED},
        TriageStatus.UNDER_REVIEW: {TriageStatus.RESOLVED, TriageStatus.NEW},
        TriageStatus.RESOLVED: {TriageStatus.UNDER_REVIEW},
    }
    assert ALLOWED_STATUS_TRANSITIONS is ALLOWED_TRANSITIONS

    # Check Enum properties and methods
    assert TriageStatus.NEW.allowed_transitions == {
        TriageStatus.UNDER_REVIEW,
        TriageStatus.RESOLVED,
    }
    assert TriageStatus.RESOLVED.allowed_transitions == {TriageStatus.UNDER_REVIEW}


def test_triage_status_transition_predicates() -> None:
    """Test guard predicates for permitted and forbidden status transitions."""
    # Permitted transitions
    assert is_valid_transition(TriageStatus.NEW, TriageStatus.UNDER_REVIEW) is True
    assert is_valid_transition(TriageStatus.NEW, TriageStatus.RESOLVED) is True
    assert is_valid_transition(TriageStatus.UNDER_REVIEW, TriageStatus.RESOLVED) is True
    assert is_valid_transition(TriageStatus.UNDER_REVIEW, TriageStatus.NEW) is True
    assert is_valid_transition(TriageStatus.RESOLVED, TriageStatus.UNDER_REVIEW) is True

    # Permitted using string representations and can_transition
    assert can_transition("NEW", "UNDER_REVIEW") is True
    assert can_transition("RESOLVED", "UNDER_REVIEW") is True
    assert TriageStatus.NEW.can_transition_to(TriageStatus.UNDER_REVIEW) is True
    assert TriageStatus.RESOLVED.can_transition_to("UNDER_REVIEW") is True

    # Forbidden transitions
    assert is_valid_transition(TriageStatus.RESOLVED, TriageStatus.NEW) is False
    assert is_valid_transition(TriageStatus.NEW, TriageStatus.NEW) is False
    assert is_valid_transition(TriageStatus.UNDER_REVIEW, TriageStatus.UNDER_REVIEW) is False
    assert is_valid_transition(TriageStatus.RESOLVED, TriageStatus.RESOLVED) is False
    assert is_valid_transition("UNKNOWN", TriageStatus.NEW) is False
    assert is_valid_transition(TriageStatus.NEW, "UNKNOWN") is False
    assert TriageStatus.RESOLVED.can_transition_to(TriageStatus.NEW) is False


def test_validate_transition_enforces_rules_and_raises() -> None:
    """Test validate_transition helper raises ValueError on forbidden transitions."""
    # Valid transitions return True
    assert validate_transition(TriageStatus.NEW, TriageStatus.UNDER_REVIEW) is True
    assert validate_transition(TriageStatus.UNDER_REVIEW, TriageStatus.RESOLVED) is True
    assert validate_transition("RESOLVED", "UNDER_REVIEW") is True
    assert TriageStatus.NEW.validate_transition_to(TriageStatus.RESOLVED) is True

    # Illegal transition: RESOLVED -> NEW
    with pytest.raises(ValueError, match="Cannot transition triage status from RESOLVED to NEW"):
        validate_transition(TriageStatus.RESOLVED, TriageStatus.NEW)

    with pytest.raises(ValueError, match="Cannot transition triage status from RESOLVED to NEW"):
        TriageStatus.RESOLVED.validate_transition_to(TriageStatus.NEW)

    # Illegal transitions: same status
    with pytest.raises(ValueError, match="Cannot transition triage status from NEW to NEW"):
        validate_transition(TriageStatus.NEW, TriageStatus.NEW)

    # Invalid status values
    with pytest.raises(ValueError, match="Invalid from_status: INVALID"):
        validate_transition("INVALID", TriageStatus.RESOLVED)

    with pytest.raises(ValueError, match="Invalid to_status: INVALID"):
        validate_transition(TriageStatus.RESOLVED, "INVALID")
