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
    """Test that extract audit trail builds filters and dates."""
    sdk = MagicMock()
    revision = RecordRevision.from_json(fake_data.fake_record_revision())
    sdk.get_record_revisions.return_value = [revision]

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
        dateCreated=[
            (">=", "2021-01-01T00:00:00Z"),
            ("<=", "2021-01-02T23:59:59Z"),
        ],
    )
    assert result == [revision]
