"""Unit tests for workflows subject data."""

from unittest.mock import MagicMock

from imednet.models.codings import Coding
from imednet.models.queries import Query
from imednet.models.records import Record
from imednet.models.subjects import Subject
from imednet.models.visits import Visit
from imednet.spi import endpoints as spi_endpoints
from imednet.spi import models as spi_models
from imednet_workflows.subject_data import SubjectDataWorkflow


def test_get_all_subject_data_aggregates_across_endpoints() -> None:
    """Test that get all subject data aggregates across endpoints."""
    sdk = MagicMock()
    subject = Subject(subject_key="S1")
    visit = Visit(visit_id=1, subject_key="S1")
    record = Record(record_id=1, subject_key="S1", visit_id=1)
    query = Query(query_comments=[])
    coding = Coding(record_id=1, subject_key="S1", variable="aeTerm", value="HEADACHE")

    sdk.get_subjects.return_value = [subject]
    sdk.get_visits.return_value = [visit]
    sdk.get_records.return_value = [record]
    sdk.get_queries.return_value = [query]
    sdk.get_codings.return_value = [coding]

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
    sdk.get_codings.assert_called_once_with("STUDY", subject_key="S1")
    assert sdk.get_codings.call_args.kwargs == {"subject_key": "S1"}

    assert result.subject_details == subject
    assert result.visits == [visit]
    assert result.records == [record]
    assert result.queries == [query]
    assert result.codings == [coding]


def test_spi_reexports_lookup_and_reporting_models() -> None:
    """Verify Coding, Visit, Interval, and reporting models are re-exported via imednet.spi."""
    expected_symbols = (
        "Coding",
        "Visit",
        "Interval",
        "ConcomitantMedication",
        "LaboratoryResult",
        "Exposure",
        "MedicalHistory",
        "Disposition",
        "EcgResult",
        "PhysicalExam",
        "InclusionExclusion",
    )
    for sym in expected_symbols:
        assert hasattr(spi_endpoints, sym), f"{sym} missing from imednet.spi.endpoints"
        assert hasattr(spi_models, sym), f"{sym} missing from imednet.spi.models"
