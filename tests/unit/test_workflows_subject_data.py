"""Unit tests for workflows subject data."""

from unittest.mock import MagicMock

from imednet.models.queries import Query
from imednet.models.records import Record
from imednet.models.subjects import Subject
from imednet.models.visits import Visit
from imednet_workflows.subject_data import SubjectDataWorkflow


def test_get_all_subject_data_aggregates_across_endpoints() -> None:
    """Test that get all subject data aggregates across endpoints."""
    sdk = MagicMock()
    subject = Subject(subject_key="S1")
    visit = Visit(visit_id=1, subject_key="S1")
    record = Record(record_id=1, subject_key="S1", visit_id=1)
    query = Query(query_comments=[])

    sdk.get_subjects.return_value = [subject]
    sdk.get_visits.return_value = [visit]
    sdk.get_records.return_value = [record]
    sdk.get_queries.return_value = [query]

    wf = SubjectDataWorkflow(sdk)
    result = wf.get_all_subject_data("STUDY", "S1")

    sdk.get_subjects.assert_called_once_with("STUDY", subject_key="S1")
    assert sdk.get_subjects.call_args.kwargs == {"subject_key": "S1"}
    sdk.get_visits.assert_called_once_with("STUDY", subject_key="S1")
    assert sdk.get_visits.call_args.kwargs == {"subject_key": "S1"}
    sdk.get_records.assert_called_once_with("STUDY", subject_key="S1")
    assert sdk.get_records.call_args.kwargs == {"subject_key": "S1"}
    sdk.get_queries.assert_called_once_with("STUDY", subject_key="S1")
    assert sdk.get_queries.call_args.kwargs == {"subject_key": "S1"}

    assert result.subject_details == subject
    assert result.visits == [visit]
    assert result.records == [record]
    assert result.queries == [query]


def test_get_all_subject_data_omits_soft_deleted_items_by_default_and_includes_when_requested() -> (
    None
):
    """Verify include_deleted=False omits soft-deleted Subject, Visit, and Record instances."""
    sdk = MagicMock()
    active_visit = Visit(visit_id=1, subject_key="S1", deleted=False)
    deleted_visit = Visit(visit_id=2, subject_key="S1", deleted=True)
    active_record = Record(record_id=10, subject_key="S1", visit_id=1, deleted=False)
    deleted_record = Record(record_id=11, subject_key="S1", visit_id=1, deleted=True)
    query = Query(annotation_id=100, subject_key="S1", query_comments=[])

    # Active subject, mixed active/deleted visits & records
    active_subject = Subject(subject_key="S1", deleted=False, subject_status="Enrolled")
    sdk.get_subjects.return_value = [active_subject]
    sdk.get_visits.return_value = [active_visit, deleted_visit]
    sdk.get_records.return_value = [active_record, deleted_record]
    sdk.get_queries.return_value = [query]

    wf = SubjectDataWorkflow(sdk)
    filtered = wf.get_all_subject_data("STUDY", "S1")
    assert filtered.subject_details == active_subject
    assert filtered.visits == [active_visit]
    assert filtered.records == [active_record]
    assert filtered.queries == [query]

    unfiltered = wf.get_all_subject_data("STUDY", "S1", include_deleted=True)
    assert unfiltered.subject_details == active_subject
    assert unfiltered.visits == [active_visit, deleted_visit]
    assert unfiltered.records == [active_record, deleted_record]

    # Soft-deleted Subject (deleted=True or subject_status='Deleted') omitted when include_deleted=False
    for soft_deleted_subject in (
        Subject(subject_key="S1", deleted=True),
        Subject(subject_key="S1", deleted=False, subject_status="Deleted"),
    ):
        sdk.get_subjects.return_value = [soft_deleted_subject]
        omitted = wf.get_all_subject_data("STUDY", "S1", include_deleted=False)
        assert omitted.subject_details is None
        retained = wf.get_all_subject_data("STUDY", "S1", include_deleted=True)
        assert retained.subject_details == soft_deleted_subject
