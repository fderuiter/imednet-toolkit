"""Unit tests for workflows query management."""

from typing import Any
from unittest.mock import MagicMock

from imednet.models.queries import Query, QueryComment, QueryStatus
from imednet.models.subjects import Subject
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
    sdk.get_subjects.return_value = [Subject(subject_key="S1"), Subject(subject_key="S2")]
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
    sdk.get_subjects.return_value = [Subject(subject_key="S1")]
    wf = QueryManagementWorkflow(sdk)

    wf.get_queries_by_site("STUDY", "Mock Site")

    sdk.get_subjects.assert_called_once_with("STUDY", site_name="Mock Site")
    sdk.get_queries.assert_called_once_with("STUDY", subject_key=["S1"])
    assert sdk.get_queries.call_args.kwargs == {"subject_key": ["S1"]}


def test_query_state_counts_dict_lookups() -> None:
    """Test that QueryStateCounts supports enum, case-insensitive, and missing keys."""
    from imednet_workflows.query_management import QueryStateCounts

    counts = QueryStateCounts({"OPEN": 5, "CLOSED": 2})
    assert counts[QueryStatus.OPEN] == 5
    assert counts["open"] == 5
    assert counts["OPEN"] == 5
    assert counts["closed"] == 2
    assert counts.get("cancelled", 0) == 0
    assert counts.get("NONEXISTENT", 99) == 99


def test_query_management_duck_typed_objects_fallback() -> None:
    """Test that legacy or duck-typed objects lacking status attribute fall back correctly."""
    sdk = MagicMock()

    class LegacyQuery:
        def __init__(self, closed: bool | None) -> None:
            self.query_comments = (
                [QueryComment(sequence=1, closed=closed)] if closed is not None else []
            )

    legacy_open = LegacyQuery(closed=False)
    legacy_closed = LegacyQuery(closed=True)
    sdk.get_queries.return_value = [legacy_open, legacy_closed]

    wf = QueryManagementWorkflow(sdk)
    open_queries = wf.get_open_queries("STUDY")
    assert open_queries == [legacy_open]

    counts = wf.get_query_state_counts("STUDY")
    assert counts["OPEN"] == 1
    assert counts["CLOSED"] == 1


def test_get_queries_by_site_multi_batch_interleaved_global_sort_and_page() -> None:
    """Multi-batch (>50 subjects) site queries apply global ordering and page boundaries."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key=f"S{i:03d}") for i in range(65)]
    batch_1 = [
        Query(annotation_id=904, subject_key="S001", record_id=602),
        Query(annotation_id=903, subject_key="S010", record_id=601),
    ]
    batch_2 = [
        Query(annotation_id=901, subject_key="S052", record_id=601),
        Query(annotation_id=902, subject_key="S060", record_id=602),
        Query(annotation_id=903, subject_key="S010", record_id=601),
    ]
    sdk.get_queries.side_effect = [batch_1, batch_2, batch_1, batch_2, batch_1, batch_2]

    wf = QueryManagementWorkflow(sdk)
    all_site = wf.get_queries_by_site("STUDY", "BIG_SITE")
    assert [q.annotation_id for q in all_site] == [901, 902, 903, 904]

    page_0 = wf.get_queries_by_site("STUDY", "BIG_SITE", page=0, size=2)
    page_1 = wf.get_queries_by_site("STUDY", "BIG_SITE", sort="annotationId,desc", page=1, limit=2)
    assert [q.annotation_id for q in page_0] == [901, 902]
    assert [q.annotation_id for q in page_1] == [902, 901]
    assert "page" not in sdk.get_queries.call_args_list[2].kwargs


def test_get_queries_by_site_single_subject_pushdown_and_over_250_fallback() -> None:
    """Single-subject pure-AND pushes subject_key, OR falls back, and >250 subjects skips batching."""
    sdk_and = MagicMock()
    sdk_and.get_subjects.return_value = [Subject(subject_key="S1")]
    sdk_and.get_queries.return_value = [Query(annotation_id=7, subject_key="S1")]
    wf_and = QueryManagementWorkflow(sdk_and)
    res_and = wf_and.get_queries_by_site(
        "STUDY", "SITE", additional_filter={"record_id": 601, "filter": "variable==V_AGE"}
    )
    assert [q.annotation_id for q in res_and] == [7]
    sdk_and.get_queries.assert_called_once_with(
        "STUDY", subject_key="S1", record_id=601, filter="variable==V_AGE"
    )

    sdk_or = MagicMock()
    sdk_or.get_subjects.return_value = [Subject(subject_key="S1")]
    sdk_or.get_queries.return_value = [
        Query(annotation_id=8, subject_key="OTHER"),
        Query(annotation_id=9, subject_key="S1"),
    ]
    wf_or = QueryManagementWorkflow(sdk_or)
    res_or = wf_or.get_queries_by_site(
        "STUDY", "SITE", additional_filter={"record_id": [601, 602]}, page=0, size=1
    )
    assert [q.annotation_id for q in res_or] == [9]
    sdk_or.get_queries.assert_called_once_with("STUDY", record_id=[601, 602])

    sdk_huge = MagicMock()
    sdk_huge.get_subjects.return_value = [Subject(subject_key=f"S{i}") for i in range(255)]
    sdk_huge.get_queries.return_value = [
        Query(annotation_id=30, subject_key="S200"),
        Query(annotation_id=10, subject_key="OTHER"),
        Query(annotation_id=20, subject_key="S5"),
    ]
    wf_huge = QueryManagementWorkflow(sdk_huge)
    res_huge = wf_huge.get_queries_by_site("STUDY", "HUGE_SITE", page=0, size=2)
    assert [q.annotation_id for q in res_huge] == [20, 30]
    sdk_huge.get_queries.assert_called_once_with("STUDY")
