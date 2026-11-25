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
    """Test extract_audit_trail sends one dateCreated bound upstream and filters end_date locally."""
    sdk = MagicMock()
    in_window = RecordRevision(
        record_revision_id=1,
        date_created="2026-01-15T12:00:00Z",
    )
    after_window = RecordRevision(
        record_revision_id=2,
        date_created="2026-02-05T00:00:00Z",
    )
    sdk.get_record_revisions.return_value = [in_window, after_window]

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
        dateCreated=(">=", "2026-01-01T00:00:00Z"),
    )
    assert result == [in_window]


def test_extract_audit_trail_emits_single_date_created_http_filter_and_filters_locally() -> None:
    """Test extract_audit_trail sends a valid single-bound filter over HTTP and filters end_date locally."""
    import respx

    from imednet.sdk import ImednetSDK
    from imednet.utils.filters import validate_filter_syntax

    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://api.test/api/v1/edc/studies/STUDY/recordRevisions").respond(
            200,
            json={
                "data": [
                    {
                        "recordRevisionId": 5,
                        "recordId": 10,
                        "recordRevision": 1,
                        "dateCreated": "2026-01-20 09:30:00",
                    },
                    {
                        "recordRevisionId": 6,
                        "recordId": 10,
                        "recordRevision": 2,
                        "dateCreated": "2026-02-03 10:00:00",
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
        validate_filter_syntax(sent_filter)
        assert sent_filter == "dateCreated>=2026-01-01T00:00:00Z"
        assert "studyKey" not in sent_filter
        assert "startDate" not in sent_filter
        assert "endDate" not in sent_filter
        assert [r.record_revision_id for r in revisions] == [5]


def test_data_extraction_date_helpers_and_progress_callbacks() -> None:
    """Test date parsing/comparison helpers, progress callbacks, and compound date bounds."""
    from datetime import UTC, date, datetime

    import pytest

    from imednet_workflows.data_extraction import (
        _matches_local_date_bound,
        _parse_date_created_utc,
    )

    assert _parse_date_created_utc(None) is None
    assert _parse_date_created_utc(datetime(2026, 1, 2, 3, 4)) == datetime(
        2026, 1, 2, 3, 4, tzinfo=UTC
    )
    assert _parse_date_created_utc(date(2026, 1, 2)) == datetime(2026, 1, 2, tzinfo=UTC)
    assert _parse_date_created_utc("   ") is None
    assert _parse_date_created_utc("not-a-date") is None
    assert _parse_date_created_utc(12345) is None

    rev_dt = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    assert _matches_local_date_bound(None, "==", rev_dt) is False
    assert _matches_local_date_bound(rev_dt, "==", datetime(2026, 1, 15, 12, 0)) is True
    assert _matches_local_date_bound(rev_dt, "!=", date(2026, 1, 16)) is True
    assert _matches_local_date_bound(rev_dt, "<", "2026-01-16") is True
    assert _matches_local_date_bound(rev_dt, ">", "2026-01-14") is True
    assert _matches_local_date_bound(rev_dt, ">=", "2026-01-15T12:00:00Z") is True
    assert _matches_local_date_bound(rev_dt, "<=", "bad-bound") is False
    assert _matches_local_date_bound(rev_dt, "==", object()) is False
    with pytest.raises(ValueError, match="Unsupported dateCreated filter operator"):
        _matches_local_date_bound(rev_dt, "~=", rev_dt)

    progress_events: list[str] = []
    sdk = MagicMock()
    sdk.get_subjects.return_value = []
    wf = DataExtractionWorkflow(sdk)
    assert (
        wf.extract_records_by_criteria(
            "STUDY",
            subject_filter={"status": "active"},
            on_progress=progress_events.append,
        )
        == []
    )
    assert any("No subjects matched" in msg for msg in progress_events)

    progress_events.clear()
    sdk.get_subjects.return_value = [Subject(subject_key="S1")]
    sdk.get_visits.return_value = []
    assert (
        wf.extract_records_by_criteria(
            "STUDY",
            subject_filter={"status": "active"},
            visit_filter={"name": "V1"},
            on_progress=progress_events.append,
        )
        == []
    )
    assert any("No visits matched" in msg for msg in progress_events)

    progress_events.clear()
    sdk.get_visits.return_value = [Visit(visit_id=10, subject_key="S1")]
    sdk.get_records.return_value = [Record(record_id=1, subject_key="S1", visit_id=10)]
    matched = wf.extract_records_by_criteria(
        "STUDY",
        subject_filter={"status": "active"},
        visit_filter={"name": "V1"},
        on_progress=progress_events.append,
    )
    assert [r.record_id for r in matched] == [1]
    assert any("Extraction complete" in msg for msg in progress_events)

    sdk.get_record_revisions.return_value = [
        RecordRevision(record_revision_id=1, date_created="2026-01-15T12:00:00Z")
    ]
    revs = wf.extract_audit_trail(
        "STUDY",
        user_filter={"date_created": [(">=", "2026-01-01T00:00:00Z"), "2026-01-15T12:00:00Z"]},
        dateCreated=("<=", "2026-02-01T00:00:00Z"),
    )
    assert [r.record_revision_id for r in revs] == [1]


def test_data_extraction_or_branching_date_bounds_and_ordered_dedup_over_http() -> None:
    """Verify extract_records_by_criteria and extract_audit_trail branch OR+AND filters safely."""
    import httpx
    import respx

    from imednet.sdk import ImednetSDK
    from imednet.utils.filters import validate_filter_syntax

    base_url = "https://api.example.com"
    record_filters_sent: list[str] = []
    revision_filters_sent: list[str] = []

    def _subjects_handler(request: httpx.Request) -> httpx.Response:
        flt = request.url.params.get("filter", "")
        validate_filter_syntax(flt)
        assert not (";" in flt and "," in flt)
        data = [
            {"studyKey": "STUDY", "subjectKey": "SUB1", "siteName": "Site A", "status": "Active"},
            {"studyKey": "STUDY", "subjectKey": "SUB2", "siteName": "Site B", "status": "Active"},
        ]
        return httpx.Response(200, json={"data": data})

    def _visits_handler(request: httpx.Request) -> httpx.Response:
        flt = request.url.params.get("filter", "")
        validate_filter_syntax(flt)
        assert not (";" in flt and "," in flt)
        data = [
            {"studyKey": "STUDY", "visitId": 10, "subjectKey": "SUB1", "intervalName": "Baseline"},
            {"studyKey": "STUDY", "visitId": 20, "subjectKey": "SUB2", "intervalName": "Week 4"},
        ]
        return httpx.Response(200, json={"data": data})

    def _records_handler(request: httpx.Request) -> httpx.Response:
        flt = request.url.params.get("filter", "")
        validate_filter_syntax(flt)
        assert not (";" in flt and "," in flt)
        record_filters_sent.append(flt)
        if "DEMO" in flt:
            data = [
                {
                    "studyKey": "STUDY",
                    "recordId": 1001,
                    "subjectKey": "SUB1",
                    "visitId": 10,
                    "formKey": "DEMO",
                    "deleted": False,
                },
                {
                    "studyKey": "STUDY",
                    "recordId": 1002,
                    "subjectKey": "SUB2",
                    "visitId": 20,
                    "formKey": "DEMO",
                    "deleted": False,
                },
            ]
        else:
            data = [
                {
                    "studyKey": "STUDY",
                    "recordId": 1002,
                    "subjectKey": "SUB2",
                    "visitId": 20,
                    "formKey": "VITALS",
                    "deleted": False,
                },
                {
                    "studyKey": "STUDY",
                    "recordId": 1003,
                    "subjectKey": "SUB1",
                    "visitId": 10,
                    "formKey": "VITALS",
                    "deleted": False,
                },
            ]
        return httpx.Response(200, json={"data": data})

    def _revisions_handler(request: httpx.Request) -> httpx.Response:
        flt = request.url.params.get("filter", "")
        validate_filter_syntax(flt)
        assert not (";" in flt and "," in flt)
        assert flt.count("dateCreated") == 1
        revision_filters_sent.append(flt)
        if "DEMO" in flt:
            data = [
                {
                    "studyKey": "STUDY",
                    "recordRevisionId": 901,
                    "recordId": 1001,
                    "formKey": "DEMO",
                    "user": "alice",
                    "dateCreated": "2026-01-10T10:00:00Z",
                },
                {
                    "studyKey": "STUDY",
                    "recordRevisionId": 902,
                    "recordId": 1002,
                    "formKey": "DEMO",
                    "user": "alice",
                    "dateCreated": "2026-02-15T10:00:00Z",
                },  # out of upper bound
            ]
        else:
            data = [
                {
                    "studyKey": "STUDY",
                    "recordRevisionId": 901,
                    "recordId": 1001,
                    "formKey": "DEMO",
                    "user": "alice",
                    "dateCreated": "2026-01-10T10:00:00Z",
                },  # duplicate across branch
                {
                    "studyKey": "STUDY",
                    "recordRevisionId": 903,
                    "recordId": 1003,
                    "formKey": "VITALS",
                    "user": "bob",
                    "dateCreated": "2026-01-20T15:00:00Z",
                },
            ]
        return httpx.Response(200, json={"data": data})

    with respx.mock(base_url=base_url, assert_all_called=False) as router:
        subj_route = router.get("/api/v1/edc/studies/STUDY/subjects").mock(
            side_effect=_subjects_handler
        )
        vis_route = router.get("/api/v1/edc/studies/STUDY/visits").mock(side_effect=_visits_handler)
        rec_route = router.get("/api/v1/edc/studies/STUDY/records").mock(
            side_effect=_records_handler
        )
        rev_route = router.get("/api/v1/edc/studies/STUDY/recordRevisions").mock(
            side_effect=_revisions_handler
        )

        with ImednetSDK(api_key="k", security_key="s", base_url=base_url, retries=0) as sdk:
            wf = sdk.workflows.data_extraction

            # Empty OR lists short-circuit with zero HTTP calls
            assert wf.extract_records_by_criteria("STUDY", subject_filter={"site_name": []}) == []
            assert wf.extract_records_by_criteria("STUDY", visit_filter={"interval_name": []}) == []
            assert wf.extract_records_by_criteria("STUDY", record_filter={"form_key": []}) == []
            assert (
                wf.extract_audit_trail(
                    "STUDY", start_date="2026-01-01", user_filter={"form_key": []}
                )
                == []
            )
            assert subj_route.call_count == 0
            assert vis_route.call_count == 0
            assert rec_route.call_count == 0
            assert rev_route.call_count == 0

            # Branched subject, visit, and record extraction with ordered record_id deduplication
            records = wf.extract_records_by_criteria(
                "STUDY",
                subject_filter={"site_name": ["Site A", "Site B"], "subject_status": "Active"},
                visit_filter={"interval_name": ["Baseline", "Week 4"]},
                record_filter={"form_key": ["DEMO", "VITALS"]},
                deleted=False,
            )
            assert [r.record_id for r in records] == [1001, 1002, 1003]
            assert record_filters_sent == [
                "deleted==false;formKey==DEMO",
                "deleted==false;formKey==VITALS",
            ]

            # Branched audit trail with start_date + end_date + OR lists + ordered record_revision_id dedup
            revisions = wf.extract_audit_trail(
                "STUDY",
                start_date="2026-01-01",
                end_date="2026-01-31",
                user_filter={"form_key": ["DEMO", "VITALS"]},
                deleted=False,
            )
            assert [r.record_revision_id for r in revisions] == [901, 903]
            assert revision_filters_sent == [
                "deleted==false;formKey==DEMO;dateCreated>=2026-01-01T00:00:00Z",
                "deleted==false;formKey==VITALS;dateCreated>=2026-01-01T00:00:00Z",
            ]
