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


def test_extract_audit_trail_fractional_end_date_missing_dates_and_or_lists() -> None:
    """Verify fractional-second end_date retention, missing date_created exclusion, OR lists, and raw filters."""
    import pytest
    import respx

    from imednet import ImednetSDK
    from imednet.simulation import SimulationState, SimulationTransport
    from imednet.utils.filters import validate_filter_syntax

    sdk = MagicMock()
    revisions = [
        RecordRevision(
            studyKey="STUDY",
            recordRevisionId=10,
            recordId=1,
            subjectId=100,
            subjectOid="SUBJ-OID-100",
            subjectKey="SUBJ-100",
            role="CRC",
            dateCreated="2026-01-10T00:00:00Z",
        ),
        RecordRevision(
            studyKey="STUDY",
            recordRevisionId=11,
            recordId=1,
            subjectId=100,
            subjectOid="SUBJ-OID-100",
            subjectKey="SUBJ-100",
            role="CRC",
            dateCreated="2026-01-31T23:59:59.750000Z",
        ),
        RecordRevision(
            studyKey="STUDY",
            recordRevisionId=12,
            recordId=1,
            role="CRC",
            dateCreated="2026-02-01T00:00:00Z",
        ),
        RecordRevision(
            studyKey="STUDY",
            recordRevisionId=13,
            recordId=1,
            role="CRC",
            dateCreated=None,
        ),
        RecordRevision(
            studyKey="STUDY",
            recordRevisionId=14,
            recordId=1,
            role="CRC",
            dateCreated="   ",
        ),
    ]
    sdk.get_record_revisions.return_value = revisions
    wf = DataExtractionWorkflow(sdk)

    # 1. Dual start_date + end_date retains 23:59:59.750000Z and excludes None/blank/Feb 1
    bounded = wf.extract_audit_trail("STUDY", start_date="2026-01-01", end_date="2026-01-31")
    assert [r.record_revision_id for r in bounded] == [10, 11]

    # 2. Single end_date alone also excludes missing date_created and retains fractional end_date
    end_only = wf.extract_audit_trail("STUDY", end_date="2026-01-31")
    assert [r.record_revision_id for r in end_only] == [10, 11]
    assert sdk.get_record_revisions.call_args.kwargs == {
        "dateCreated": ("<", "2026-02-01T00:00:00Z")
    }

    # 3. Single start_date alone also excludes missing date_created locally
    start_only = wf.extract_audit_trail("STUDY", start_date="2026-01-31")
    assert [r.record_revision_id for r in start_only] == [11, 12]

    # 4. Scalar OR list on date_created works alone, with role AND kwarg, and with start_date/end_date
    or_alone = wf.extract_audit_trail(
        "STUDY",
        date_created=["2026-01-10T00:00:00Z", "2026-02-01T00:00:00Z"],
    )
    assert [r.record_revision_id for r in or_alone] == [10, 12]

    or_with_role_and_bounds = wf.extract_audit_trail(
        "STUDY",
        start_date="2026-01-01",
        end_date="2026-01-31",
        role="CRC",
        dateCreated=["2026-01-10T00:00:00Z", "2026-02-01T00:00:00Z"],
    )
    assert [r.record_revision_id for r in or_with_role_and_bounds] == [10]

    # 5. Raw filter clauses with dateCreated (AND ';' and OR ',') normalize to UTC and exclude None
    raw_and = wf.extract_audit_trail(
        "STUDY",
        filter="role==CRC;dateCreated>=2026-01-01;dateCreated<=2026-01-31",
    )
    assert [r.record_revision_id for r in raw_and] == [10, 11]
    assert sdk.get_record_revisions.call_args.kwargs == {
        "filter": "role==CRC;dateCreated>=2026-01-01T00:00:00Z"
    }

    raw_or = wf.extract_audit_trail(
        "STUDY",
        filter="dateCreated<=2026-01-31,dateCreated==2026-02-01T00:00:00Z",
    )
    assert [r.record_revision_id for r in raw_or] == [10, 11, 12]
    assert sdk.get_record_revisions.call_args.kwargs == {
        "filter": "dateCreated<2026-02-01T00:00:00Z,dateCreated==2026-02-01T00:00:00Z"
    }

    # 6. Empty list filters are rejected
    with pytest.raises(ValueError, match="cannot be empty"):
        wf.extract_audit_trail("STUDY", date_created=[])

    with respx.mock(assert_all_called=False):
        live_sdk = ImednetSDK(
            api_key="test-key",  # pragma: allowlist secret
            security_key="test-sec",  # pragma: allowlist secret
            base_url="https://api.test",
        )
        live_wf = DataExtractionWorkflow(live_sdk)
        with pytest.raises(ValueError, match="Filter list for 'user' cannot be empty"):
            live_wf.extract_audit_trail("STUDY", user=[])

    # 7. End-to-end SimulationTransport verification with fractional-second revision on end_date
    sim_state = SimulationState()
    sim_state.record_revisions.append(
        {
            "studyKey": "SIM_AUDIT",
            "recordRevisionId": 555,
            "recordId": 9,
            "recordOid": "REC-9",
            "formKey": "F_VS",
            "subjectId": 999,
            "subjectOid": "SUBJ-OID-999",
            "subjectKey": "SUBJ-999",
            "recordRevision": 1,
            "dataRevision": 1,
            "recordStatus": "Record Complete",
            "user": "sim_crc",
            "deleted": False,
            "dateCreated": "2026-01-31T23:59:59.750000Z",
            "lastUpdated": "2026-01-31T23:59:59.750000Z",
        }
    )
    with ImednetSDK(
        api_key="sim-key",  # pragma: allowlist secret
        security_key="sim-sec",  # pragma: allowlist secret
        transport=SimulationTransport(state=sim_state),
    ) as sim_sdk:
        sim_wf = DataExtractionWorkflow(sim_sdk)
        sim_res = sim_wf.extract_audit_trail(
            "SIM_AUDIT",
            end_date="2026-01-31",
        )
        assert any(r.record_revision_id == 555 for r in sim_res)
        validate_filter_syntax("dateCreated<2026-02-01T00:00:00Z")
