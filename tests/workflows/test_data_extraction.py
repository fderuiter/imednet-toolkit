"""Unit tests for data extraction."""

from unittest.mock import MagicMock

from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.subjects import Subject
from imednet.models.visits import Visit
from imednet.testing import fake_data
from imednet_workflows.data_extraction import DataExtractionWorkflow


def test_extract_records_by_criteria_filters_subject_and_visit(schema) -> None:
    """Test that extract records by criteria filters subject and visit."""
    sdk = MagicMock()
    s1 = Subject.from_json(fake_data.fake_subject())
    s2 = Subject.from_json(fake_data.fake_subject())
    s1.subject_key = "S1"
    s2.subject_key = "S2"
    sdk.get_subjects.return_value = [s1, s2]

    v1 = Visit.from_json(fake_data.fake_visit())
    v2 = Visit.from_json(fake_data.fake_visit())
    v1.subject_key = "S1"
    v1.visit_id = 1
    v2.subject_key = "S2"
    v2.visit_id = 2
    sdk.get_visits.return_value = [v1, v2]

    r1 = Record.from_json(fake_data.fake_record(schema))
    r2 = Record.from_json(fake_data.fake_record(schema))
    r3 = Record.from_json(fake_data.fake_record(schema))
    r1.subject_key = "S1"
    r1.visit_id = 1
    r1.record_id = 1
    r2.subject_key = "S2"
    r2.visit_id = 2
    r2.record_id = 2
    r3.subject_key = "S1"
    r3.visit_id = 99
    r3.record_id = 3
    sdk.get_records.return_value = [r1, r2, r3]

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


def test_extract_audit_trail_builds_filters_and_dates() -> None:
    """Test extract_audit_trail sends single upstream dateCreated bound and checks end_date locally."""
    sdk = MagicMock()
    in_range_payload = fake_data.fake_record_revision()
    in_range_payload["dateCreated"] = "2021-01-01 15:30:00"
    out_of_range_payload = fake_data.fake_record_revision()
    out_of_range_payload["dateCreated"] = "2021-01-05 08:00:00"

    revision = RecordRevision.from_json(in_range_payload)
    later_revision = RecordRevision.from_json(out_of_range_payload)
    sdk.get_record_revisions.return_value = [revision, later_revision]

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_audit_trail(
        "STUDY",
        start_date="2021-01-01",
        end_date="2021-01-02",
        user_filter={"role": "data"},
        status="open",
    )

    sdk.get_record_revisions.assert_called_once_with(
        "STUDY",
        role="data",
        status="open",
        dateCreated=(">=", "2021-01-01T00:00:00Z"),
    )
    assert result == [revision]


def test_extract_audit_trail_fractional_end_date_missing_dates_and_or_clauses() -> None:
    """Test fractional-second end_date inclusion, missing date_created exclusion, and OR lists."""
    sdk = MagicMock()
    rev_early = RecordRevision(
        studyKey="STUDY",
        recordRevisionId=1,
        recordId=10,
        subjectId=501,
        subjectOid="SUBJ-OID-501",
        subjectKey="SUBJ-01",
        dateCreated="2026-01-15T08:00:00Z",
    )
    rev_fractional_end = RecordRevision(
        studyKey="STUDY",
        recordRevisionId=2,
        recordId=10,
        subjectId=501,
        subjectOid="SUBJ-OID-501",
        subjectKey="SUBJ-01",
        dateCreated="2026-01-31T23:59:59.750000Z",
    )
    rev_missing_date = RecordRevision(
        studyKey="STUDY",
        recordRevisionId=3,
        recordId=10,
        dateCreated=None,
    )
    rev_next_day = RecordRevision(
        studyKey="STUDY",
        recordRevisionId=4,
        recordId=10,
        dateCreated="2026-02-01T00:00:00Z",
    )
    sdk.get_record_revisions.return_value = [
        rev_early,
        rev_fractional_end,
        rev_missing_date,
        rev_next_day,
    ]

    wf = DataExtractionWorkflow(sdk)
    bounded = wf.extract_audit_trail("STUDY", start_date="2026-01-01", end_date="2026-01-31")
    assert [r.record_revision_id for r in bounded] == [1, 2]

    or_filtered = wf.extract_audit_trail(
        "STUDY",
        user_filter={"date_created": ["2026-01-15T08:00:00Z", "2026-02-01T00:00:00Z"]},
        role="CRC",
    )
    assert [r.record_revision_id for r in or_filtered] == [1, 4]
