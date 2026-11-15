"""Unit tests for Query and QueryStatus discrepancy state machine models."""

from __future__ import annotations

import pytest

from imednet.models.queries import (
    ALLOWED_QUERY_TRANSITIONS,
    Query,
    QueryComment,
    QueryStatus,
    calculate_legacy_status,
    is_valid_query_transition,
    validate_query_transition,
)


def test_query_status_enum_values() -> None:
    """Verify QueryStatus enum members and values."""
    assert QueryStatus.OPEN.value == "OPEN"
    assert QueryStatus.ANSWERED.value == "ANSWERED"
    assert QueryStatus.CLOSED.value == "CLOSED"
    assert QueryStatus.CANCELLED.value == "CANCELLED"

    # Case-insensitive resolution
    assert QueryStatus("open") is QueryStatus.OPEN
    assert QueryStatus("OPEN") is QueryStatus.OPEN
    assert QueryStatus("answered") is QueryStatus.ANSWERED
    assert QueryStatus("closed") is QueryStatus.CLOSED
    assert QueryStatus("cancelled") is QueryStatus.CANCELLED


def test_query_status_transitions_properties() -> None:
    """Verify allowed_transitions and helper methods on QueryStatus."""
    assert ALLOWED_QUERY_TRANSITIONS[QueryStatus.OPEN] == {
        QueryStatus.ANSWERED,
        QueryStatus.CLOSED,
        QueryStatus.CANCELLED,
    }
    assert QueryStatus.OPEN.allowed_transitions == {
        QueryStatus.ANSWERED,
        QueryStatus.CLOSED,
        QueryStatus.CANCELLED,
    }
    assert QueryStatus.ANSWERED.allowed_transitions == {
        QueryStatus.OPEN,
        QueryStatus.CLOSED,
        QueryStatus.CANCELLED,
    }
    assert QueryStatus.CLOSED.allowed_transitions == {QueryStatus.OPEN}
    assert QueryStatus.CANCELLED.allowed_transitions == set()

    assert QueryStatus.OPEN.can_transition_to(QueryStatus.ANSWERED) is True
    assert QueryStatus.OPEN.can_transition_to(QueryStatus.CLOSED) is True
    assert QueryStatus.OPEN.can_transition_to("CANCELLED") is True
    assert QueryStatus.OPEN.can_transition_to(QueryStatus.OPEN) is False

    assert QueryStatus.OPEN.validate_transition_to(QueryStatus.ANSWERED) is True
    with pytest.raises(ValueError):
        QueryStatus.CLOSED.validate_transition_to(QueryStatus.CLOSED)


def test_transition_validation_helpers() -> None:
    """Verify is_valid_query_transition and validate_query_transition."""
    # Valid transitions
    assert is_valid_query_transition(QueryStatus.OPEN, QueryStatus.ANSWERED) is True
    assert is_valid_query_transition(QueryStatus.OPEN, QueryStatus.CLOSED) is True
    assert is_valid_query_transition(QueryStatus.OPEN, QueryStatus.CANCELLED) is True
    assert is_valid_query_transition(QueryStatus.ANSWERED, QueryStatus.CLOSED) is True
    assert is_valid_query_transition(QueryStatus.ANSWERED, QueryStatus.OPEN) is True
    assert is_valid_query_transition(QueryStatus.ANSWERED, QueryStatus.CANCELLED) is True
    assert is_valid_query_transition(QueryStatus.CLOSED, QueryStatus.OPEN) is True

    assert validate_query_transition(QueryStatus.OPEN, QueryStatus.ANSWERED) is True
    assert validate_query_transition(QueryStatus.CLOSED, QueryStatus.OPEN) is True

    # Invalid transitions
    assert is_valid_query_transition(QueryStatus.CLOSED, QueryStatus.CLOSED) is False
    assert is_valid_query_transition(QueryStatus.CLOSED, QueryStatus.ANSWERED) is False
    assert is_valid_query_transition(QueryStatus.CLOSED, QueryStatus.CANCELLED) is False
    assert is_valid_query_transition(QueryStatus.CANCELLED, QueryStatus.OPEN) is False
    assert is_valid_query_transition(QueryStatus.OPEN, QueryStatus.OPEN) is False
    assert is_valid_query_transition("INVALID", QueryStatus.OPEN) is False
    assert is_valid_query_transition(QueryStatus.OPEN, "INVALID") is False

    with pytest.raises(ValueError, match="Cannot transition query status from CLOSED to CLOSED"):
        validate_query_transition(QueryStatus.CLOSED, QueryStatus.CLOSED)

    with pytest.raises(ValueError, match="Cannot transition query status from CANCELLED to OPEN"):
        validate_query_transition(QueryStatus.CANCELLED, QueryStatus.OPEN)

    with pytest.raises(ValueError, match="Invalid from_status"):
        validate_query_transition("NON_EXISTENT", QueryStatus.OPEN)

    with pytest.raises(ValueError, match="Invalid to_status"):
        validate_query_transition(QueryStatus.OPEN, "NON_EXISTENT")


def test_query_model_default_status() -> None:
    """Criterion 1: Query model defaults status to OPEN for new queries."""
    query = Query()
    assert query.status == QueryStatus.OPEN
    assert query.query_comments == []

    query_with_empty_comments = Query(query_comments=[])
    assert query_with_empty_comments.status == QueryStatus.OPEN


def test_query_model_explicit_status() -> None:
    """Verify that explicit status is respected when constructing Query."""
    query = Query(status=QueryStatus.ANSWERED)
    assert query.status == QueryStatus.ANSWERED

    query_str = Query(status="CLOSED")
    assert query_str.status == QueryStatus.CLOSED


def test_legacy_payload_fallback_calculation() -> None:
    """Requirement 5: Fallback calculation for legacy payloads without status field."""
    # Closed query payload
    closed_legacy = {
        "queryComments": [
            {"sequence": 1, "closed": False},
            {"sequence": 2, "closed": True},
        ]
    }
    q_closed = Query.model_validate(closed_legacy)
    assert q_closed.status == QueryStatus.CLOSED

    # Open query payload
    open_legacy = {
        "queryComments": [
            {"sequence": 1, "closed": False},
        ]
    }
    q_open = Query.model_validate(open_legacy)
    assert q_open.status == QueryStatus.OPEN

    # Annotation status fallback
    answered_legacy = {
        "queryComments": [
            {"sequence": 1, "closed": False, "annotationStatus": "Site Answered Query"},
        ]
    }
    q_answered = Query.model_validate(answered_legacy)
    assert q_answered.status == QueryStatus.ANSWERED

    cancelled_legacy = {
        "queryComments": [
            {"sequence": 1, "closed": False, "annotationStatus": "Cancelled discrepancy"},
        ]
    }
    q_cancelled = Query.model_validate(cancelled_legacy)
    assert q_cancelled.status == QueryStatus.CANCELLED

    closed_ann_legacy = {
        "queryComments": [
            {"sequence": 1, "closed": False, "annotationStatus": "Closed discrepancy"},
        ]
    }
    q_closed_ann = Query.model_validate(closed_ann_legacy)
    assert q_closed_ann.status == QueryStatus.CLOSED


def test_calculate_legacy_status_function() -> None:
    """Verify calculate_legacy_status helper directly."""
    assert calculate_legacy_status(None) == QueryStatus.OPEN
    assert calculate_legacy_status([]) == QueryStatus.OPEN

    comments = [
        QueryComment(sequence=1, closed=False),
        QueryComment(sequence=3, closed=True),
        QueryComment(sequence=2, closed=False),
    ]
    assert calculate_legacy_status(comments) == QueryStatus.CLOSED


def test_transition_to_and_guard_direct_mutation() -> None:
    """Criterion 2: Transition validation blocks illegal moves."""
    query = Query(status=QueryStatus.OPEN)
    assert query.can_transition_to(QueryStatus.ANSWERED) is True
    assert query.can_transition_to(QueryStatus.OPEN) is False

    # Valid direct assignment with string
    query.status = "ANSWERED"
    assert query.status == QueryStatus.ANSWERED

    # Valid transition to CLOSED
    query.transition_to(QueryStatus.CLOSED)
    assert query.status == QueryStatus.CLOSED

    # Closing an already closed query must fail
    with pytest.raises(ValueError, match="Cannot transition query status from CLOSED to CLOSED"):
        query.transition_to(QueryStatus.CLOSED)

    # Direct mutation to closed must also be guarded and raise ValueError
    with pytest.raises(ValueError, match="Cannot transition query status from CLOSED to CLOSED"):
        query.status = QueryStatus.CLOSED

    # Valid reopen transition from CLOSED to OPEN
    query.transition_to(QueryStatus.OPEN)
    assert query.status == QueryStatus.OPEN

    # Reopening an open query must fail
    with pytest.raises(ValueError, match="Cannot transition query status from OPEN to OPEN"):
        query.status = QueryStatus.OPEN

    # Valid transition to CANCELLED
    query.update_status(QueryStatus.CANCELLED)
    assert query.status == QueryStatus.CANCELLED

    # Transitioning from CANCELLED must fail
    with pytest.raises(ValueError, match="Cannot transition query status from CANCELLED"):
        query.transition_to(QueryStatus.OPEN)


def test_status_change_preserves_comment_sequences() -> None:
    """Constraint: Status changes must not alter existing comment sequence numbers."""
    comments = [
        QueryComment(sequence=1, closed=False, comment="First observation"),
        QueryComment(sequence=2, closed=False, comment="Investigator response"),
    ]
    query = Query(query_comments=comments, status=QueryStatus.OPEN)

    original_sequences = [c.sequence for c in query.query_comments]
    assert original_sequences == [1, 2]

    query.transition_to(QueryStatus.ANSWERED)
    query.transition_to(QueryStatus.CLOSED)

    after_sequences = [c.sequence for c in query.query_comments]
    assert after_sequences == original_sequences
    assert len(query.query_comments) == 2


def test_pydantic_serialization_roundtrip() -> None:
    """Constraint: Core model must remain compatible with Pydantic serialization."""
    query = Query(
        status=QueryStatus.ANSWERED,
        query_comments=[QueryComment(sequence=1, closed=False, comment="Test comment")],
    )

    dumped = query.model_dump(mode="json")
    assert dumped["status"] == "ANSWERED"
    assert dumped["query_comments"][0]["sequence"] == 1

    restored = Query.model_validate(dumped)
    assert restored.status == QueryStatus.ANSWERED
    assert restored.query_comments[0].sequence == 1
