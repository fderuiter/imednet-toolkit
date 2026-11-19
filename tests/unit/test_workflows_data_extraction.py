"""Unit tests for workflows data extraction."""

from unittest.mock import MagicMock

from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.subjects import Subject
from imednet.models.visits import Visit
from imednet_workflows.data_extraction import DataExtractionWorkflow


def test_extract_records_by_criteria_filters_subject_and_visit() -> None:
    """Test that extract records by criteria filters subject and visit."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1"), Subject(subject_key="S2")]
    sdk.get_visits.return_value = [
        Visit(visit_id=1, subject_key="S1"),
        Visit(visit_id=2, subject_key="S2"),
    ]
    records = [
        Record(record_id=1, subject_key="S1", visit_id=1),
        Record(record_id=2, subject_key="S2", visit_id=2),
        Record(record_id=3, subject_key="S1", visit_id=99),
    ]
    sdk.get_records.return_value = records

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria(
        "STUDY",
        subject_filter={"status": "active"},
        visit_filter={"visit_id": 1},
    )

    sdk.get_subjects.assert_called_once_with("STUDY", status="active")
    assert sdk.get_subjects.call_args.kwargs == {"status": "active"}
    sdk.get_visits.assert_called_once_with("STUDY", visit_id=1)
    assert sdk.get_visits.call_args.kwargs == {"visit_id": 1}
    sdk.get_records.assert_called_once_with(study_key="STUDY", record_data_filter=None)
    assert sdk.get_records.call_args.kwargs == {"study_key": "STUDY", "record_data_filter": None}

    assert [r.record_id for r in result] == [1, 2]


def test_extract_records_by_criteria_empty_subjects() -> None:
    """Test early return of empty list when subject filter returns no subjects."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = []

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria("STUDY", subject_filter={"site": "SITE01"})

    assert result == []
    sdk.get_subjects.assert_called_once_with("STUDY", site="SITE01")
    sdk.get_visits.assert_not_called()
    sdk.get_records.assert_not_called()


def test_extract_records_by_criteria_empty_visits() -> None:
    """Test early return of empty list when visit filter returns no visits."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1")]
    sdk.get_visits.return_value = []

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria(
        "STUDY",
        subject_filter={"site": "SITE01"},
        visit_filter={"name": "V1"},
    )

    assert result == []
    sdk.get_subjects.assert_called_once_with("STUDY", site="SITE01")
    sdk.get_visits.assert_called_once_with("STUDY", name="V1")
    sdk.get_records.assert_not_called()


def test_extract_records_by_criteria_no_visits_match_subjects() -> None:
    """Test early return when queried visits belong to different subjects."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1")]
    sdk.get_visits.return_value = [Visit(visit_id=10, subject_key="S99")]

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria(
        "STUDY",
        subject_filter={"site": "SITE01"},
        visit_filter={"name": "V1"},
    )

    assert result == []
    sdk.get_records.assert_not_called()


def test_extract_records_by_criteria_subject_filter_only() -> None:
    """Test filtering by subject criteria only when visit filter is None."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1")]
    sdk.get_records.return_value = [
        Record(record_id=1, subject_key="S1", visit_id=10),
        Record(record_id=2, subject_key="S2", visit_id=20),
    ]

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria("STUDY", subject_filter={"site": "SITE01"})

    assert [r.record_id for r in result] == [1]
    sdk.get_visits.assert_not_called()


def test_extract_records_by_criteria_visit_filter_only() -> None:
    """Test filtering by visit criteria only when subject filter is None."""
    sdk = MagicMock()
    sdk.get_visits.return_value = [Visit(visit_id=10, subject_key="S1")]
    sdk.get_records.return_value = [
        Record(record_id=1, subject_key="S1", visit_id=10),
        Record(record_id=2, subject_key="S1", visit_id=20),
    ]

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria("STUDY", visit_filter={"name": "V1"})

    assert [r.record_id for r in result] == [1]
    sdk.get_subjects.assert_not_called()


def test_extract_records_by_criteria_no_filters() -> None:
    """Test extract records when no subject or visit filters are provided."""
    sdk = MagicMock()
    records = [
        Record(record_id=1, subject_key="S1", visit_id=10),
        Record(record_id=2, subject_key="S2", visit_id=20),
    ]
    sdk.get_records.return_value = records

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria("STUDY")

    assert [r.record_id for r in result] == [1, 2]
    sdk.get_subjects.assert_not_called()
    sdk.get_visits.assert_not_called()


def test_extract_records_by_criteria_scaling_benchmark() -> None:
    """Benchmark constant-time set lookups with 1,000 subjects, 2,000 visits, and 10,000 records."""
    import time

    sdk = MagicMock()
    subjects = [Subject(subject_key=f"SUBJ_{i}") for i in range(1000)]
    visits = [Visit(visit_id=i, subject_key=f"SUBJ_{i % 1000}") for i in range(2000)]
    records = [
        Record(record_id=i, subject_key=f"SUBJ_{i % 1200}", visit_id=i % 2500) for i in range(10000)
    ]

    sdk.get_subjects.return_value = subjects
    sdk.get_visits.return_value = visits
    sdk.get_records.return_value = records

    wf = DataExtractionWorkflow(sdk)
    start = time.perf_counter()
    result = wf.extract_records_by_criteria(
        "STUDY",
        subject_filter={"site": "SITE01"},
        visit_filter={"type": "scheduled"},
    )
    elapsed = time.perf_counter() - start

    assert isinstance(result, list)
    expected_subjects = {s.subject_key for s in subjects}
    expected_visits = {v.visit_id for v in visits}
    for record in result:
        assert record.subject_key in expected_subjects
        assert record.visit_id in expected_visits

    # Expected count:
    # subject_key SUBJ_{i % 1200} in expected_subjects when (i % 1200) < 1000
    # visit_id (i % 2500) in expected_visits when (i % 2500) < 2000
    expected_count = sum(1 for i in range(10000) if (i % 1200) < 1000 and (i % 2500) < 2000)
    assert len(result) == expected_count

    # Set lookups execute membership filtering in sub-0.1s (typically ~0.01s),
    # eliminating the 0.31s+ quadratic linear scan latency.
    assert elapsed < 0.15


def test_extract_audit_trail_builds_filters_and_dates() -> None:
    """Test that extract_audit_trail sends one UTC date_created tuple and filters bounds in Python."""
    sdk = MagicMock()
    matching_start = RecordRevision(
        record_revision_id=1,
        record_id=10,
        date_created="2026-01-01T00:00:00Z",
    )
    matching_end_of_day = RecordRevision(
        record_revision_id=2,
        record_id=10,
        date_created="2026-02-01T23:59:59Z",
    )
    before_start = RecordRevision(
        record_revision_id=3,
        record_id=11,
        date_created="2025-12-31T23:59:59Z",
    )
    after_end = RecordRevision(
        record_revision_id=4,
        record_id=12,
        date_created="2026-02-02T00:00:00Z",
    )
    missing_date = RecordRevision(
        record_revision_id=5,
        record_id=13,
        date_created=None,
    )
    sdk.get_record_revisions.return_value = [
        matching_start,
        matching_end_of_day,
        before_start,
        after_end,
        missing_date,
    ]

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_audit_trail(
        "STUDY",
        start_date="2026-01-01",
        end_date="2026-02-01",
        user_filter={"role": "data"},
        status="open",
    )

    sdk.get_record_revisions.assert_called_once_with(
        "STUDY",
        role="data",
        status="open",
        date_created=(">=", "2026-01-01T00:00:00Z"),
    )
    assert [r.record_revision_id for r in result] == [1, 2]


def test_extract_audit_trail_end_date_only_and_caller_date_created_predicates() -> None:
    """Test end_date-only tuple pushdown and caller date_created predicates in user_filter."""
    sdk = MagicMock()
    rev_early = RecordRevision(record_revision_id=1, date_created="2026-01-05T12:00:00Z")
    rev_mid = RecordRevision(record_revision_id=2, date_created="2026-01-15T14:30:00+02:00")
    rev_late = RecordRevision(record_revision_id=3, date_created="2026-01-25T12:00:00Z")
    rev_blank = RecordRevision(record_revision_id=4, date_created="   ")
    sdk.get_record_revisions.return_value = [rev_early, rev_mid, rev_late, rev_blank]

    wf = DataExtractionWorkflow(sdk)
    end_only = wf.extract_audit_trail("STUDY", end_date="2026-01-15")
    sdk.get_record_revisions.assert_called_once_with(
        "STUDY",
        date_created=("<=", "2026-01-15T23:59:59Z"),
    )
    assert [r.record_revision_id for r in end_only] == [1, 2]

    sdk.get_record_revisions.reset_mock()
    combined = wf.extract_audit_trail(
        "STUDY",
        start_date="2026-01-01",
        end_date="2026-01-31",
        user_filter={
            "date_created": [(">", "2026-01-10T00:00:00Z"), ("!=", "2026-01-25T12:00:00Z")]
        },
    )
    sdk.get_record_revisions.assert_called_once_with(
        "STUDY",
        date_created=(">", "2026-01-10T00:00:00Z"),
    )
    assert [r.record_revision_id for r in combined] == [2]


def test_extract_audit_trail_emits_unique_date_created_http_filter_and_filters_range() -> None:
    """Test that extract_audit_trail sends a single dateCreated filter attribute over HTTP."""
    import respx

    from imednet.sdk import ImednetSDK

    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://api.test/api/v1/edc/studies/STUDY/recordRevisions").respond(
            200,
            json={
                "data": [
                    {
                        "recordRevisionId": 5,
                        "recordId": 10,
                        "recordRevision": 1,
                        "dateCreated": "2026-01-15T10:00:00Z",
                    },
                    {
                        "recordRevisionId": 6,
                        "recordId": 10,
                        "recordRevision": 2,
                        "dateCreated": "2026-02-05T00:00:00Z",
                    },
                    {
                        "recordRevisionId": 7,
                        "recordId": 11,
                        "recordRevision": 1,
                    },
                ]
            },
        )
        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
        wf = DataExtractionWorkflow(sdk)

        revisions = wf.extract_audit_trail(
            "STUDY",
            start_date="2026-01-01",
            end_date="2026-02-01",
        )

        sent_filter = route.calls.last.request.url.params["filter"]
        assert sent_filter == "dateCreated>=2026-01-01T00:00:00Z;studyKey==STUDY"
        assert sent_filter.count("dateCreated") == 1
        assert "startDate" not in sent_filter
        assert "endDate" not in sent_filter
        assert [r.record_revision_id for r in revisions] == [5]


def test_extract_audit_trail_edge_cases_and_simulation_mode() -> None:
    """Test no-date passthrough, scalar/list/operator user_filter variants, and simulation mode."""
    from datetime import UTC, datetime

    import pytest

    from imednet.sdk import ImednetSDK

    sdk = MagicMock()
    rev_valid = RecordRevision(
        record_revision_id=10,
        date_created="2026-01-15T12:00:00Z",
    )
    rev_other = RecordRevision(
        record_revision_id=11,
        date_created="2026-01-20T12:00:00Z",
    )
    rev_unparseable = RecordRevision(
        record_revision_id=12,
        date_created="not-a-timestamp",
    )
    rev_missing = RecordRevision(
        record_revision_id=13,
        date_created=None,
    )
    sdk.get_record_revisions.return_value = [rev_valid, rev_other, rev_unparseable, rev_missing]

    wf = DataExtractionWorkflow(sdk)

    # No date filters: returns all revisions untouched (including missing date_created)
    assert len(wf.extract_audit_trail("STUDY", user_filter={"role": "CRC"})) == 4

    # Scalar equality in user_filter with camelCase key
    eq_res = wf.extract_audit_trail(
        "STUDY",
        start_date="2026-01-01",
        user_filter={"dateCreated": datetime(2026, 1, 15, 12, 0, tzinfo=UTC)},
    )
    assert sdk.get_record_revisions.call_args.kwargs["date_created"] == (
        "==",
        "2026-01-15T12:00:00Z",
    )
    assert [r.record_revision_id for r in eq_res] == [10]

    # Scalar OR list in user_filter
    or_res = wf.extract_audit_trail(
        "STUDY",
        user_filter={"date_created": ["2026-01-15T12:00:00Z", "2026-01-20T12:00:00Z"]},
    )
    assert [r.record_revision_id for r in or_res] == [10, 11]

    # Strict '<' and substring '=~' operators
    lt_res = wf.extract_audit_trail("STUDY", user_filter={"date_created": ("<", "2026-01-18")})
    assert sdk.get_record_revisions.call_args.kwargs["date_created"] == (
        "<",
        "2026-01-18T23:59:59Z",
    )
    assert [r.record_revision_id for r in lt_res] == [10]

    contains_res = wf.extract_audit_trail(
        "STUDY", user_filter={"date_created": ("=~", "2026-01-20")}
    )
    assert sdk.get_record_revisions.call_args.kwargs["date_created"] == ("=~", "2026-01-20")
    assert [r.record_revision_id for r in contains_res] == [11]

    # Validation errors on malformed date_created filters
    with pytest.raises(ValueError, match="Allowed operators"):
        wf.extract_audit_trail("STUDY", user_filter={"date_created": ("<>", "2026-01-01")})
    with pytest.raises(ValueError, match="exactly 2 elements"):
        wf.extract_audit_trail("STUDY", user_filter={"date_created": (">=", "2026-01-01", "extra")})
    with pytest.raises(ValueError, match="cannot mix"):
        wf.extract_audit_trail(
            "STUDY",
            user_filter={"date_created": [(">=", "2026-01-01"), "2026-01-02"]},
        )

    # Offline simulation mode executes cleanly with combined start_date and end_date
    sim_sdk = ImednetSDK(simulation_mode=True)
    sim_wf = DataExtractionWorkflow(sim_sdk)
    assert sim_wf.extract_audit_trail("STUDY", start_date="2026-01-01", end_date="2026-01-31") == []
