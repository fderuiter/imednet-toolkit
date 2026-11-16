"""Unit tests for UATWorkflow orchestrator."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from imednet.spi.models import Form, Interval, Site, Variable
from imednet_workflows.uat.generator import GeneratedRecordSet
from imednet_workflows.uat.inspector import StudySnapshot
from imednet_workflows.uat.models import RecordTestType, UATSpecification
from imednet_workflows.uat.orchestrator import UATRunResult, UATSpecificationBuilder, UATWorkflow
from imednet_workflows.uat.submission import SubmissionResult


@pytest.fixture
def mock_sdk():
    sdk = MagicMock()
    sdk.get_job.return_value = MagicMock(state="COMPLETED")
    return sdk


@pytest.fixture
def snapshot():
    return StudySnapshot(
        study_key="TEST_STUDY",
        forms=[
            Form(form_key="F1", form_name="Enrollment", form_type="Enrollment"),
            Form(form_key="F2", form_name="Scheduled", form_type="DataEntry"),
            Form(
                form_key="F3",
                form_name="Unscheduled",
                form_type="DataEntry",
                unscheduled_visit=True,
            ),
        ],
        variables=[
            Variable(form_key="F1", variable_name="V1", variable_key="VK1", variable_type="Text"),
            Variable(form_key="F2", variable_name="V2", variable_key="VK2", variable_type="Number"),
            Variable(form_key="F3", variable_name="V3", variable_key="VK3", variable_type="Date"),
        ],
        intervals=[
            Interval(
                interval_name="Int1",
                forms=[{"form_id": 1, "form_key": "F2", "form_name": "Scheduled"}],
            ),
        ],
        sites=[
            Site(site_name="Site1", site_enrollment_status="Active"),
        ],
    )


def test_builder_builds_spec(snapshot):
    builder = UATSpecificationBuilder()
    spec = builder.build(snapshot, subject_count=3)

    assert spec.study_key == "TEST_STUDY"
    assert len(spec.subject_specs) == 1
    assert spec.subject_specs[0].subject_count == 3
    assert len(spec.form_specs) == 3

    f1_spec = next(f for f in spec.form_specs if f.form_key == "F1")
    assert f1_spec.test_type == RecordTestType.REGISTER_SUBJECT

    f2_spec = next(f for f in spec.form_specs if f.form_key == "F2")
    assert f2_spec.test_type == RecordTestType.UPDATE_SCHEDULED_RECORD
    assert f2_spec.interval_name == "Int1"

    f3_spec = next(f for f in spec.form_specs if f.form_key == "F3")
    assert f3_spec.test_type == RecordTestType.CREATE_NEW_RECORD


def test_builder_builds_spec_with_no_active_sites(snapshot):
    snapshot.sites[0].site_enrollment_status = "Closed"
    builder = UATSpecificationBuilder()
    spec = builder.build(snapshot)
    assert spec.subject_specs[0].site_name == "Site1"

    snapshot.sites = []
    spec = builder.build(snapshot)
    assert spec.subject_specs[0].site_name == "Default Site"


def test_workflow_run_pipeline(mock_sdk, snapshot):
    workflow = UATWorkflow(mock_sdk)

    # Mock all internal methods
    workflow.inspect = MagicMock(return_value=snapshot)

    mock_spec = MagicMock(spec=UATSpecification)
    workflow.build_spec = MagicMock(return_value=mock_spec)

    mock_record_sets = [MagicMock(spec=GeneratedRecordSet)]
    workflow.generate = MagicMock(return_value=mock_record_sets)

    mock_submission = MagicMock(spec=SubmissionResult)
    mock_submission.total_records_submitted = 10
    mock_submission.all_batch_ids = ["B1"]
    workflow.submit = MagicMock(return_value=mock_submission)

    mock_poll_summary = MagicMock()
    mock_poll_summary.all_successful = True
    mock_poll_summary.results = {"B1": MagicMock()}
    mock_poll_summary.failures = {}
    workflow.monitor = MagicMock(return_value=mock_poll_summary)

    result = workflow.run("TEST_STUDY")

    assert isinstance(result, UATRunResult)
    assert result.study_key == "TEST_STUDY"
    assert result.overall_success is True
    assert result.record_sets == mock_record_sets
    assert "inspect" in result.phase_durations
    assert "monitor" in result.phase_durations

    summary = result.summary()
    assert "TEST_STUDY" in summary
    assert "PASS" in summary


def test_workflow_run_with_custom_spec(mock_sdk, snapshot):
    workflow = UATWorkflow(mock_sdk)
    workflow.inspect = MagicMock(return_value=snapshot)

    # Need a real UATSpecification or a better mock for the generator
    custom_spec = UATSpecification(
        study_key="TEST_STUDY",
        study_name="TEST_STUDY",
        subject_specs=[],
        form_specs=[],
    )

    with patch.object(workflow, "build_spec") as mock_build_spec:
        with patch.object(workflow, "generate", return_value=[]) as mock_generate:
            with patch.object(workflow, "submit", return_value=MagicMock(spec=SubmissionResult)):
                with patch.object(workflow, "monitor", return_value=MagicMock()):
                    workflow.run("TEST_STUDY", spec=custom_spec)
                    mock_build_spec.assert_not_called()
                    mock_generate.assert_called_once()


def test_workflow_run_result_overall_success():
    poll_summary = MagicMock()
    poll_summary.all_successful = True

    result = UATRunResult(
        study_key="S",
        started_at=datetime.now(timezone.utc),
        spec=MagicMock(spec=UATSpecification),
        record_sets=[],
        submission_result=MagicMock(spec=SubmissionResult),
        poll_summary=poll_summary,
    )
    assert result.overall_success is True

    poll_summary.all_successful = False
    assert result.overall_success is False


def test_workflow_individual_phases(mock_sdk, snapshot):
    workflow = UATWorkflow(mock_sdk)
    with patch.object(workflow, "_inspector") as mock_insp:
        mock_insp.inspect.return_value = snapshot
        assert workflow.inspect("S") == snapshot

    spec = MagicMock(spec=UATSpecification)
    with patch.object(workflow, "_generator") as mock_gen:
        mock_gen.generate.return_value = []
        assert workflow.generate(spec, snapshot) == []

    with patch.object(workflow, "_submitter") as mock_sub:
        mock_sub.submit.return_value = MagicMock()
        assert workflow.submit("S", []) is not None

    with patch.object(workflow, "_poller") as mock_poll:
        mock_poll.poll_many.return_value = MagicMock()
        assert workflow.monitor("S", MagicMock()) is not None


def test_workflow_lifecycle_transitions_and_history(mock_sdk, snapshot):
    """Test full sequential lifecycle transitions and state history recording."""
    from imednet_workflows.uat.models import UATWorkflowState

    workflow = UATWorkflow(mock_sdk)
    assert workflow.state == UATWorkflowState.PENDING
    assert workflow.state_history == [UATWorkflowState.PENDING]

    workflow.inspect = MagicMock(return_value=snapshot)
    workflow.build_spec = MagicMock(return_value=MagicMock(spec=UATSpecification))
    workflow.generate = MagicMock(return_value=[MagicMock(spec=GeneratedRecordSet)])
    mock_submission = MagicMock(spec=SubmissionResult)
    mock_submission.total_records_submitted = 5
    mock_submission.all_batch_ids = ["B1"]
    workflow.submit = MagicMock(return_value=mock_submission)
    mock_poll = MagicMock()
    mock_poll.all_successful = True
    mock_poll.results = {"B1": MagicMock()}
    mock_poll.failures = {}
    workflow.monitor = MagicMock(return_value=mock_poll)

    result = workflow.run("TEST_STUDY")

    assert workflow.state == UATWorkflowState.COMPLETED
    assert result.state == UATWorkflowState.COMPLETED
    assert result.failure_phase is None
    assert result.failed_phase is None
    assert result.error_message is None
    assert result.is_terminal is True
    assert result.overall_success is True

    expected_history = [
        UATWorkflowState.PENDING,
        UATWorkflowState.INSPECTING,
        UATWorkflowState.BUILDING_SPEC,
        UATWorkflowState.GENERATING,
        UATWorkflowState.SUBMITTING,
        UATWorkflowState.MONITORING,
        UATWorkflowState.COMPLETED,
    ]
    assert workflow.state_history == expected_history
    assert result.state_history == expected_history
    assert workflow.last_result is result


def test_workflow_run_inspect_failure_transitions_to_failed(mock_sdk):
    """Test unexpected exception during INSPECT phase transitions to FAILED."""
    from imednet_workflows.uat.models import UATWorkflowState

    workflow = UATWorkflow(mock_sdk)
    workflow.inspect = MagicMock(side_effect=RuntimeError("Study schema unreachable"))

    result = workflow.run("TEST_STUDY")

    assert workflow.state == UATWorkflowState.FAILED
    assert result.state == UATWorkflowState.FAILED
    assert result.failure_phase == UATWorkflowState.INSPECTING
    assert result.failed_phase == UATWorkflowState.INSPECTING
    assert "Study schema unreachable" in (result.error_message or "")
    assert result.overall_success is False
    assert result.state_history == [
        UATWorkflowState.PENDING,
        UATWorkflowState.INSPECTING,
        UATWorkflowState.FAILED,
    ]
    summary = result.summary()
    assert "Overall: FAIL" in summary
    assert "Failed phase: INSPECTING" in summary
    assert "Error: Study schema unreachable" in summary


def test_workflow_run_generate_failure_transitions_to_failed(mock_sdk, snapshot):
    """Test unexpected exception during GENERATE phase transitions to FAILED."""
    from imednet_workflows.uat.models import UATWorkflowState

    workflow = UATWorkflow(mock_sdk)
    workflow.inspect = MagicMock(return_value=snapshot)
    workflow.build_spec = MagicMock(return_value=MagicMock(spec=UATSpecification))
    workflow.generate = MagicMock(side_effect=ValueError("Data generation crashed"))

    result = workflow.run("TEST_STUDY")

    assert workflow.state == UATWorkflowState.FAILED
    assert result.state == UATWorkflowState.FAILED
    assert result.failure_phase == UATWorkflowState.GENERATING
    assert "Data generation crashed" in (result.error_message or "")
    assert result.overall_success is False
    assert result.state_history == [
        UATWorkflowState.PENDING,
        UATWorkflowState.INSPECTING,
        UATWorkflowState.BUILDING_SPEC,
        UATWorkflowState.GENERATING,
        UATWorkflowState.FAILED,
    ]


def test_workflow_run_submit_failure_transitions_to_failed(mock_sdk, snapshot):
    """Test unexpected exception during SUBMIT phase transitions to FAILED."""
    from imednet_workflows.uat.models import UATWorkflowState
    from imednet_workflows.uat.submission import BulkSubmissionError

    workflow = UATWorkflow(mock_sdk)
    workflow.inspect = MagicMock(return_value=snapshot)
    workflow.build_spec = MagicMock(return_value=MagicMock(spec=UATSpecification))
    workflow.generate = MagicMock(return_value=[MagicMock(spec=GeneratedRecordSet)])
    workflow.submit = MagicMock(
        side_effect=BulkSubmissionError("2 registration batches failed", failed_batches=[])
    )

    result = workflow.run("TEST_STUDY")

    assert workflow.state == UATWorkflowState.FAILED
    assert result.state == UATWorkflowState.FAILED
    assert result.failure_phase == UATWorkflowState.SUBMITTING
    assert "2 registration batches failed" in (result.error_message or "")
    assert result.overall_success is False
    assert result.state_history == [
        UATWorkflowState.PENDING,
        UATWorkflowState.INSPECTING,
        UATWorkflowState.BUILDING_SPEC,
        UATWorkflowState.GENERATING,
        UATWorkflowState.SUBMITTING,
        UATWorkflowState.FAILED,
    ]


def test_workflow_run_monitor_failure_transitions_to_failed(mock_sdk, snapshot):
    """Test unexpected exception during MONITOR phase transitions to FAILED."""
    from imednet_workflows.uat.models import UATWorkflowState

    workflow = UATWorkflow(mock_sdk)
    workflow.inspect = MagicMock(return_value=snapshot)
    workflow.build_spec = MagicMock(return_value=MagicMock(spec=UATSpecification))
    workflow.generate = MagicMock(return_value=[MagicMock(spec=GeneratedRecordSet)])
    workflow.submit = MagicMock(return_value=MagicMock(spec=SubmissionResult))
    workflow.monitor = MagicMock(side_effect=TimeoutError("Poller timed out"))

    result = workflow.run("TEST_STUDY")

    assert workflow.state == UATWorkflowState.FAILED
    assert result.state == UATWorkflowState.FAILED
    assert result.failure_phase == UATWorkflowState.MONITORING
    assert "Poller timed out" in (result.error_message or "")
    assert result.overall_success is False
    assert result.state_history == [
        UATWorkflowState.PENDING,
        UATWorkflowState.INSPECTING,
        UATWorkflowState.BUILDING_SPEC,
        UATWorkflowState.GENERATING,
        UATWorkflowState.SUBMITTING,
        UATWorkflowState.MONITORING,
        UATWorkflowState.FAILED,
    ]


def test_workflow_attempt_submitting_to_inspecting_raises_invalid_transition(mock_sdk):
    """Test attempting to transition from SUBMITTING back to INSPECTING raises InvalidUATStateTransitionError."""
    from imednet_workflows.uat.models import InvalidUATStateTransitionError, UATWorkflowState

    workflow = UATWorkflow(mock_sdk)
    # Manually transition forward to SUBMITTING
    workflow.transition_to(UATWorkflowState.INSPECTING)
    workflow.transition_to(UATWorkflowState.BUILDING_SPEC)
    workflow.transition_to(UATWorkflowState.GENERATING)
    workflow.transition_to(UATWorkflowState.SUBMITTING)
    assert workflow.state == UATWorkflowState.SUBMITTING

    with pytest.raises(InvalidUATStateTransitionError) as exc_info:
        workflow.transition_to(UATWorkflowState.INSPECTING)

    assert "Cannot transition UAT workflow from SUBMITTING to INSPECTING" in str(exc_info.value)
    # State should remain SUBMITTING
    assert workflow.state == UATWorkflowState.SUBMITTING


def test_workflow_reset_and_rerun(mock_sdk, snapshot):
    """Test reset restores state to PENDING and allows re-running workflow."""
    from imednet_workflows.uat.models import InvalidUATStateTransitionError, UATWorkflowState

    workflow = UATWorkflow(mock_sdk)
    workflow.inspect = MagicMock(return_value=snapshot)
    workflow.build_spec = MagicMock(return_value=MagicMock(spec=UATSpecification))
    workflow.generate = MagicMock(return_value=[])
    workflow.submit = MagicMock(return_value=MagicMock(spec=SubmissionResult))
    workflow.monitor = MagicMock(return_value=MagicMock(all_successful=True))

    result1 = workflow.run("TEST_STUDY")
    assert result1.state == UATWorkflowState.COMPLETED
    assert workflow.state == UATWorkflowState.COMPLETED

    # Running again without reset raises InvalidUATStateTransitionError
    with pytest.raises(InvalidUATStateTransitionError):
        workflow.run("TEST_STUDY")

    # Reset allows rerun
    workflow.reset()
    assert workflow.state == UATWorkflowState.PENDING
    assert workflow.state_history == [UATWorkflowState.PENDING]
    result2 = workflow.run("TEST_STUDY")
    assert result2.state == UATWorkflowState.COMPLETED
