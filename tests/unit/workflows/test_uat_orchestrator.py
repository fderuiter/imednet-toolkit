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
    with pytest.raises(ValueError, match="open sites"):
        builder.build(snapshot)

    with pytest.raises(ValueError, match="not eligible for enrollment"):
        builder.build(snapshot, site_name="Site1")

    with pytest.raises(ValueError, match="not found"):
        builder.build(snapshot, site_name="UnknownSite")

    snapshot.sites = []
    spec = builder.build(snapshot)
    assert spec.subject_specs[0].site_name == "Default Site"


def test_builder_upstream_subject_forms_exclusions_and_active_intervals():
    snap = StudySnapshot(
        study_key="UPSTREAM_STUDY",
        forms=[
            Form(
                form_key="F_DM",
                form_name="Demographics",
                form_type="Subject",
                subject_record_report=True,
            ),
            Form(form_key="F_VS", form_name="Vital Signs", form_type="Subject"),
            Form(
                form_key="F_AE",
                form_name="Adverse Events",
                form_type="Subject",
                unscheduled_visit=True,
            ),
            Form(
                form_key="F_LOG",
                form_name="Other Log",
                form_type="Subject",
                other_forms=True,
            ),
            Form(form_key="F_SITE", form_name="Site Setup", form_type="Site"),
            Form(form_key="F_STUDY", form_name="Study Setup", form_type="Study"),
            Form(form_key="F_ADMIN", form_name="Admin Log", form_type="Admin"),
            Form(
                form_key="F_DISABLED",
                form_name="Disabled Form",
                form_type="Subject",
                disabled=True,
            ),
        ],
        variables=[
            Variable(
                form_key="F_DM",
                variable_name="AGE",
                variable_key="VK_AGE",
                variable_type="Number",
            ),
            Variable(
                form_key="F_DM",
                variable_name="COUNTRY",
                variable_key="VK_COUNTRY",
                variable_type="Dropdown",
            ),
            Variable(
                form_key="F_DM",
                variable_name="DEL_VAR",
                variable_key="VK_DEL",
                variable_type="Text",
                deleted=True,
            ),
            Variable(
                form_key="F_DM",
                variable_name="DIS_VAR",
                variable_key="VK_DIS",
                variable_type="Text",
                disabled=True,
            ),
            Variable(
                form_key="F_VS",
                variable_name="SYSBP",
                variable_key="VK_BP",
                variable_type="Number",
            ),
        ],
        intervals=[
            Interval(
                interval_name="DisabledVisit",
                disabled=True,
                forms=[{"form_id": 2, "form_key": "F_VS", "form_name": "Vital Signs"}],
            ),
            Interval(
                interval_name="ActiveBaseline",
                disabled=False,
                forms=[{"form_id": 2, "form_key": "F_VS", "form_name": "Vital Signs"}],
            ),
        ],
        sites=[
            Site(site_name="ClosedSite", site_enrollment_status="Enrollment Closed"),
            Site(site_name="OpenSite", site_enrollment_status="Enrollment Open"),
        ],
    )

    spec = UATSpecificationBuilder().build(snap, subject_count=2)
    assert spec.spec_version == "1.0"
    assert spec.subject_specs[0].site_name == "OpenSite"
    assert [f.form_key for f in spec.form_specs] == ["F_DM", "F_VS", "F_AE"]

    dm_spec = next(f for f in spec.form_specs if f.form_key == "F_DM")
    assert dm_spec.test_type == RecordTestType.REGISTER_SUBJECT
    assert [v.variable_name for v in dm_spec.variables] == ["AGE", "COUNTRY"]
    country_spec = next(v for v in dm_spec.variables if v.variable_name == "COUNTRY")
    assert country_spec.variable_type == "Dropdown"

    vs_spec = next(f for f in spec.form_specs if f.form_key == "F_VS")
    assert vs_spec.test_type == RecordTestType.UPDATE_SCHEDULED_RECORD
    assert vs_spec.interval_name == "ActiveBaseline"

    ae_spec = next(f for f in spec.form_specs if f.form_key == "F_AE")
    assert ae_spec.test_type == RecordTestType.CREATE_NEW_RECORD
    assert ae_spec.interval_name is None


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
    """Test consecutive run() calls use isolated contexts and reset() restores PENDING."""
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

    # Illegal manual transition from terminal COMPLETED still raises
    with pytest.raises(InvalidUATStateTransitionError):
        workflow.transition_to(UATWorkflowState.INSPECTING)

    # Running again without reset allocates an isolated execution context and succeeds
    result2 = workflow.run("TEST_STUDY")
    assert result2.state == UATWorkflowState.COMPLETED
    assert result1.run_id != result2.run_id
    assert result1.state_history is not result2.state_history
    assert result1.state_history == result2.state_history

    # Explicit reset restores state to PENDING and clears last_result
    workflow.reset()
    assert workflow.state == UATWorkflowState.PENDING
    assert workflow.state_history == [UATWorkflowState.PENDING]
    assert workflow.last_result is None
    result3 = workflow.run("TEST_STUDY")
    assert result3.state == UATWorkflowState.COMPLETED


def test_builder_and_workflow_consume_data_dictionary(mock_sdk):
    """UATSpecificationBuilder and UATWorkflow populate choices, bounds, and required flags from DataDictionary."""
    from imednet.datadict import Choice, DataDictionary
    from imednet.datadict import Form as DDForm
    from imednet.datadict import Variable as DDVariable
    from imednet.validation import validate_record_entry

    snap = StudySnapshot(
        study_key="TEST_STUDY",
        forms=[
            Form(form_key="F_DM", form_name="Demographics", form_type="Enrollment"),
        ],
        variables=[
            Variable(
                form_key="F_DM", variable_name="SEX", variable_key="VK_SEX", variable_type="Coded"
            ),
            Variable(
                form_key="F_DM", variable_name="AGE", variable_key="VK_AGE", variable_type="Number"
            ),
            Variable(
                form_key="F_DM", variable_name="RACE", variable_key="VK_RACE", variable_type="Coded"
            ),
        ],
        intervals=[],
        sites=[Site(site_name="Site1", site_enrollment_status="Active")],
    )

    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="F_DM",
                formName="Demographics",
                variables=[
                    DDVariable(
                        variableName="SEX",
                        variableType="Radio",
                        required="Yes",
                        choices=[
                            Choice(choiceText="Male", choiceValue="M"),
                            Choice(choiceText="Female", choiceValue="F"),
                        ],
                    ),
                    DDVariable(
                        variableName="AGE",
                        variableType="Integer",
                        required="Yes",
                        extraAttributes={"minValue": 21, "maxValue": 65},
                    ),
                    DDVariable(
                        variableName="RACE",
                        variableType="Multi Select",
                        required="Auto Query",
                        choices=[
                            Choice(choiceText="Asian", choiceValue="ASIAN"),
                            Choice(choiceText="White", choiceValue="WHITE"),
                        ],
                    ),
                ],
            )
        ]
    )

    builder = UATSpecificationBuilder(data_dictionary=dd)
    spec = builder.build(snap, subject_count=2)
    dm_spec = spec.form_specs[0]
    assert dm_spec.required_variable_names == ["SEX", "AGE"]

    sex_spec = next(v for v in dm_spec.variables if v.variable_name == "SEX")
    assert sex_spec.variable_type == "Radio"
    assert sex_spec.coded_values == ["M", "F"]
    assert sex_spec.required is True

    age_spec = next(v for v in dm_spec.variables if v.variable_name == "AGE")
    assert age_spec.min_value == pytest.approx(21.0)
    assert age_spec.max_value == pytest.approx(65.0)
    assert age_spec.required is True

    race_spec = next(v for v in dm_spec.variables if v.variable_name == "RACE")
    assert race_spec.variable_type == "Multi Select"
    assert race_spec.coded_values == ["ASIAN", "WHITE"]
    assert race_spec.required is False

    # Run UATWorkflow end-to-end with DataDictionary
    workflow = UATWorkflow(mock_sdk, seed=7, data_dictionary=dd)
    workflow.inspect = MagicMock(return_value=snap)
    mock_submission = MagicMock(spec=SubmissionResult)
    mock_submission.total_records_submitted = 1
    mock_submission.all_batch_ids = ["B1"]
    workflow.submit = MagicMock(return_value=mock_submission)
    mock_poll = MagicMock(all_successful=True, results={"B1": MagicMock()}, failures={})
    workflow.monitor = MagicMock(return_value=mock_poll)

    res = workflow.run("TEST_STUDY")
    assert res.overall_success is True
    workflow.submit.assert_called_once()
    assert workflow.submit.call_args.kwargs["schema"] is dd
    payload = res.record_sets[0].payloads[0]
    assert payload["data"]["SEX"] in {"M", "F"}
    assert isinstance(payload["data"]["AGE"], int)
    assert 21 <= payload["data"]["AGE"] <= 65
    for token in payload["data"]["RACE"].split("`"):
        assert token in {"ASIAN", "WHITE"}
    validate_record_entry(
        dd,
        {
            "formKey": "F_DM",
            "data": payload["data"],
        },
    )


def test_workflow_run_tags_subjects_forwards_schema_and_reuses_uat_subjects_on_rerun():
    """Repeated UATWorkflow.run calls with skip_existing_subjects=True enroll zero duplicate subjects."""
    import httpx

    from imednet import ImednetSDK
    from imednet.datadict import Choice, DataDictionary
    from imednet.datadict import Form as DDForm
    from imednet.datadict import Variable as DDVariable
    from imednet.simulation import SimulationState, create_simulation_transport
    from imednet_workflows.uat.submission import subject_has_keyword

    state = SimulationState()
    state.forms = [
        {
            "studyKey": "STUDY",
            "formId": 1,
            "formKey": "F_DM",
            "formName": "Demographics",
            "formType": "Subject",
            "subjectRecordReport": True,
            "unscheduledVisit": False,
            "otherForms": False,
            "disabled": False,
        },
        {
            "studyKey": "STUDY",
            "formId": 2,
            "formKey": "F_VS",
            "formName": "Vital Signs",
            "formType": "Subject",
            "subjectRecordReport": False,
            "unscheduledVisit": False,
            "otherForms": False,
            "disabled": False,
        },
        {
            "studyKey": "STUDY",
            "formId": 3,
            "formKey": "F_OTHER",
            "formName": "Other Form",
            "formType": "Subject",
            "subjectRecordReport": False,
            "unscheduledVisit": False,
            "otherForms": True,
            "disabled": False,
        },
        {
            "studyKey": "STUDY",
            "formId": 4,
            "formKey": "F_SITE",
            "formName": "Site Form",
            "formType": "Site",
            "subjectRecordReport": False,
            "unscheduledVisit": False,
            "otherForms": False,
            "disabled": False,
        },
    ]
    state.variables = [
        {
            "studyKey": "STUDY",
            "variableId": 10,
            "formId": 1,
            "formKey": "F_DM",
            "variableName": "AGE",
            "variableType": "Number",
            "deleted": False,
            "disabled": False,
        },
        {
            "studyKey": "STUDY",
            "variableId": 11,
            "formId": 1,
            "formKey": "F_DM",
            "variableName": "COUNTRY",
            "variableType": "Dropdown",
            "deleted": False,
            "disabled": False,
        },
        {
            "studyKey": "STUDY",
            "variableId": 12,
            "formId": 1,
            "formKey": "F_DM",
            "variableName": "OBS_DELETED",
            "variableType": "Text",
            "deleted": True,
            "disabled": False,
        },
        {
            "studyKey": "STUDY",
            "variableId": 20,
            "formId": 2,
            "formKey": "F_VS",
            "variableName": "WEIGHT",
            "variableType": "Float",
            "deleted": False,
            "disabled": False,
        },
    ]
    state.intervals = [
        {
            "studyKey": "STUDY",
            "intervalId": 1,
            "intervalName": "DisabledVisit",
            "disabled": True,
            "forms": [{"formId": 2, "formKey": "F_VS", "formName": "Vital Signs"}],
        },
        {
            "studyKey": "STUDY",
            "intervalId": 2,
            "intervalName": "Baseline",
            "disabled": False,
            "forms": [{"formId": 2, "formKey": "F_VS", "formName": "Vital Signs"}],
        },
    ]

    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="F_DM",
                variables=[
                    DDVariable(
                        variableName="AGE",
                        variableType="Integer",
                        required="Yes",
                        extraAttributes={"minValue": 18, "maxValue": 80},
                    ),
                    DDVariable(
                        variableName="COUNTRY",
                        variableType="Dropdown",
                        required="Yes",
                        choices=[
                            Choice(choiceText="United States", choiceValue="US"),
                            Choice(choiceText="Canada", choiceValue="CA"),
                        ],
                    ),
                ],
            ),
            DDForm(
                formKey="F_VS",
                variables=[
                    DDVariable(variableName="WEIGHT", variableType="Float", required="Yes"),
                ],
            ),
        ]
    )

    sim_transport = create_simulation_transport(state)
    sdk = ImednetSDK(
        transport=httpx.MockTransport(sim_transport.handle_request),
        simulation_mode=True,
    )

    pre_run_count = len(list(sdk.subjects.list("STUDY")))
    wf = UATWorkflow(sdk, poll_interval=0.01, seed=11, data_dictionary=dd)

    res1 = wf.run("STUDY", subject_count=2)
    assert res1.overall_success is True
    assert res1.submission_result is not None
    assert len(res1.submission_result.registration_batches) == 1
    assert len(res1.submission_result.enrolled_subject_keys) == 2

    subjects_after_first = list(sdk.subjects.list("STUDY"))
    assert len(subjects_after_first) == pre_run_count + 2
    uat_subjects = [s for s in subjects_after_first if subject_has_keyword(s, "UAT")]
    assert len(uat_subjects) == 2

    res2 = wf.run("STUDY", subject_count=2)
    assert res2.overall_success is True
    assert res2.submission_result is not None
    assert len(res2.submission_result.registration_batches) == 0
    assert "F_DM" in res2.submission_result.skipped_forms

    subjects_after_second = list(sdk.subjects.list("STUDY"))
    assert len(subjects_after_second) == len(subjects_after_first)


def test_sdk_singleton_consecutive_uat_runs_without_reset():
    """Calling sdk.workflows.uat.run() twice succeeds without reset() and isolates run state."""
    from imednet import ImednetSDK
    from imednet_workflows.uat import BulkRecordSubmissionWorkflow
    from imednet_workflows.uat.models import UATWorkflowState

    sdk = ImednetSDK(simulation_mode=True)
    singleton_uat = sdk.workflows.uat
    assert sdk.workflows.uat is singleton_uat

    created_submitters: list[BulkRecordSubmissionWorkflow] = []
    orig_init = BulkRecordSubmissionWorkflow.__init__

    def tracking_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        created_submitters.append(self)

    with patch.object(BulkRecordSubmissionWorkflow, "__init__", tracking_init):
        res1 = sdk.workflows.uat.run("STUDY", subject_count=1)
        res2 = sdk.workflows.uat.run("STUDY", subject_count=1)

    assert res1.state == UATWorkflowState.COMPLETED
    assert res2.state == UATWorkflowState.COMPLETED
    assert res1.overall_success is True
    assert res2.overall_success is True
    assert res1.state_history is not res2.state_history
    res1.state_history.append(UATWorkflowState.FAILED)
    assert res2.state_history[-1] == UATWorkflowState.COMPLETED
    assert len(created_submitters) == 2
    assert created_submitters[0] is not created_submitters[1]
    assert sdk.workflows.uat is singleton_uat


def test_workflow_run_monitoring_poll_summary_any_failed_transitions_to_failed(mock_sdk, snapshot):
    """When poll_summary.any_failed is True, workflow transitions MONITORING -> FAILED."""
    from imednet.spi.models import JobStatus
    from imednet.spi.utils import JobFailedError, JobPollSummary, JobTimeoutError
    from imednet_workflows.uat.models import UATWorkflowState

    observed_phases: list[UATWorkflowState] = []
    workflow = UATWorkflow(mock_sdk, on_phase_change=observed_phases.append)
    workflow.inspect = MagicMock(return_value=snapshot)
    workflow.build_spec = MagicMock(return_value=MagicMock(spec=UATSpecification))
    workflow.generate = MagicMock(return_value=[MagicMock(spec=GeneratedRecordSet)])
    mock_sub = MagicMock(spec=SubmissionResult)
    mock_sub.total_records_submitted = 4
    mock_sub.all_batch_ids = ["batch-ok", "batch-fail-1", "batch-timeout-2"]
    workflow.submit = MagicMock(return_value=mock_sub)

    poll_summary = JobPollSummary(
        study_key="TEST_STUDY",
        results={"batch-ok": JobStatus(batch_id="batch-ok", state="COMPLETED")},
        failures={
            "batch-fail-1": JobFailedError(
                "batch-fail-1", JobStatus(batch_id="batch-fail-1", state="FAILED")
            ),
            "batch-timeout-2": JobTimeoutError("Batch timed out"),
        },
        elapsed_total=1.5,
    )
    assert poll_summary.any_failed is True
    workflow.monitor = MagicMock(return_value=poll_summary)

    result = workflow.run("TEST_STUDY")

    assert result.state == UATWorkflowState.FAILED
    assert workflow.state == UATWorkflowState.FAILED
    assert result.failure_phase == UATWorkflowState.MONITORING
    assert result.overall_success is False
    assert "batch-fail-1" in (result.error_message or "")
    assert "batch-timeout-2" in (result.error_message or "")
    assert result.state_history == [
        UATWorkflowState.PENDING,
        UATWorkflowState.INSPECTING,
        UATWorkflowState.BUILDING_SPEC,
        UATWorkflowState.GENERATING,
        UATWorkflowState.SUBMITTING,
        UATWorkflowState.MONITORING,
        UATWorkflowState.FAILED,
    ]
    assert observed_phases[-1] == UATWorkflowState.FAILED
    assert UATWorkflowState.COMPLETED not in observed_phases
    summary_text = result.summary()
    assert "Overall: FAIL" in summary_text
    assert "Failed phase: MONITORING" in summary_text
    assert "batch-fail-1" in summary_text

    # Also verify nonzero failed_records on a COMPLETED job triggers MONITORING -> FAILED
    rec_err_summary = JobPollSummary(
        study_key="TEST_STUDY",
        results={
            "batch-rec-err": JobStatus(
                batch_id="batch-rec-err", state="COMPLETED", failed_records=2
            )
        },
        failures={},
        elapsed_total=0.5,
    )
    assert rec_err_summary.any_failed is True
    workflow.monitor = MagicMock(return_value=rec_err_summary)

    result2 = workflow.run("TEST_STUDY")
    assert result2.state == UATWorkflowState.FAILED
    assert result2.failure_phase == UATWorkflowState.MONITORING
    assert result2.overall_success is False
    assert "batch-rec-err" in (result2.error_message or "")
