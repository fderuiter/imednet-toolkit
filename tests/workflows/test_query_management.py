"""Unit tests for query management."""

from typing import Any
from unittest.mock import MagicMock

from imednet.models.queries import Query, QueryComment, QueryStatus
from imednet.models.subjects import Subject
from imednet.testing import fake_data
from imednet_workflows.query_management import QueryManagementWorkflow


def make_query(
    sequence_closed: list[tuple[int, bool]],
    status: QueryStatus | str | None = None,
) -> Query:
    """Helper function to make query."""
    comments = [QueryComment(sequence=seq, closed=closed) for seq, closed in sequence_closed]
    kwargs: dict[str, Any] = {"query_comments": comments}
    if status is not None:
        kwargs["status"] = status
    return Query(**kwargs)


def test_get_open_queries_filters_latest_comment() -> None:
    """Test that get open queries filters latest comment."""
    sdk = MagicMock()
    query_closed = make_query([(1, False), (2, True)])
    query_open = make_query([(1, False)])
    query_cancelled = Query(status=QueryStatus.CANCELLED)
    sdk.get_queries.return_value = [query_closed, query_open, query_cancelled]

    wf = QueryManagementWorkflow(sdk)
    result = wf.get_open_queries("STUDY", additional_filter={"state": "new"})

    sdk.get_queries.assert_called_once_with("STUDY", state="new")
    assert sdk.get_queries.call_args.kwargs == {"state": "new"}
    assert result == [query_open]


def test_get_queries_for_subject_builds_combined_filter() -> None:
    """Test that get queries for subject builds combined filter."""
    sdk = MagicMock()
    wf = QueryManagementWorkflow(sdk)
    wf.get_queries_for_subject("STUDY", "SUBJ1", additional_filter={"type": "x"})

    sdk.get_queries.assert_called_once_with("STUDY", subject_key="SUBJ1", type="x")
    assert sdk.get_queries.call_args.kwargs == {"subject_key": "SUBJ1", "type": "x"}


def test_get_query_state_counts_aggregates_states() -> None:
    """Test that get query state counts aggregates states."""
    sdk = MagicMock()
    open_query = make_query([(1, False)])
    closed_query = make_query([(1, True)])
    answered_query = Query(status=QueryStatus.ANSWERED)
    cancelled_query = Query(status=QueryStatus.CANCELLED)
    sdk.get_queries.return_value = [open_query, closed_query, answered_query, cancelled_query]

    wf = QueryManagementWorkflow(sdk)
    counts = wf.get_query_state_counts("STUDY")

    sdk.get_queries.assert_called_once_with("STUDY")
    assert sdk.get_queries.call_args.kwargs == {}
    assert counts == {
        QueryStatus.OPEN.value: 1,
        QueryStatus.ANSWERED.value: 1,
        QueryStatus.CLOSED.value: 1,
        QueryStatus.CANCELLED.value: 1,
    }


def test_get_queries_by_site_filters_using_subjects() -> None:
    """Test that get queries by site avoids mixing AND/OR when additional_filter is given."""
    sdk = MagicMock()
    s1 = Subject.from_json(fake_data.fake_subject())
    s2 = Subject.from_json(fake_data.fake_subject())
    s1.subject_key = "S1"
    s2.subject_key = "S2"
    sdk.get_subjects.return_value = [s1, s2]
    sdk.get_queries.return_value = [
        Query(annotation_id=1, subject_key="S1"),
        Query(annotation_id=2, subject_key="S2"),
        Query(annotation_id=3, subject_key="OTHER"),
    ]
    wf = QueryManagementWorkflow(sdk)

    result = wf.get_queries_by_site("STUDY", "SITE", additional_filter={"state": "open"})

    sdk.get_subjects.assert_called_once_with("STUDY", site_name="SITE")
    sdk.get_queries.assert_called_once_with("STUDY", state="open")
    assert sdk.get_queries.call_args.kwargs == {"state": "open"}
    assert [q.subject_key for q in result] == ["S1", "S2"]


def test_get_queries_by_site_returns_empty_if_no_subjects() -> None:
    """Test that get queries by site returns empty if no subjects."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = []
    wf = QueryManagementWorkflow(sdk)

    result = wf.get_queries_by_site("STUDY", "SITE")

    sdk.get_subjects.assert_called_once_with("STUDY", site_name="SITE")
    sdk.get_queries.assert_not_called()
    assert result == []


def test_get_queries_by_site_with_space_in_name() -> None:
    """Test that get queries by site with space in name."""
    sdk = MagicMock()
    s = Subject.from_json(fake_data.fake_subject())
    s.subject_key = "S1"
    sdk.get_subjects.return_value = [s]
    wf = QueryManagementWorkflow(sdk)

    wf.get_queries_by_site("STUDY", "Mock Site")

    sdk.get_subjects.assert_called_once_with("STUDY", site_name="Mock Site")
    sdk.get_queries.assert_called_once_with("STUDY", subject_key=["S1"])
    assert sdk.get_queries.call_args.kwargs == {"subject_key": ["S1"]}
