"""Unit tests for BulkRecordSubmissionWorkflow."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from imednet.spi.models import Job, JobStatus, Subject
from imednet.spi.utils import JobTimeoutError
from imednet_workflows.uat import (
    BatchSubmission,
    BulkRecordSubmissionWorkflow,
    BulkSubmissionError,
    GeneratedRecordSet,
    RecordTestType,
    SubmissionResult,
)


@pytest.fixture
def mock_sdk():
    """Fixture to provide a mocked SDK facade."""
    return MagicMock()


def test_submission_result_properties():
    """Test the properties of SubmissionResult model."""
    job1 = Job(batch_id="batch1", state="COMPLETED")
    job2 = Job(batch_id="batch2", state="COMPLETED")

    result = SubmissionResult(
        study_key="test_study",
        started_at=datetime.now(timezone.utc),
        registration_batches=[BatchSubmission(phase="registration", record_count=2, job=job1)],
        data_batches=[BatchSubmission(phase="data", record_count=3, job=job2)],
    )

    assert result.total_records_submitted == 5
    assert result.all_batch_ids == ["batch1", "batch2"]
    assert len(result.all_batches) == 2


def test_chunk_payloads():
    """Test the payload chunking logic."""
    workflow = BulkRecordSubmissionWorkflow(MagicMock(), batch_size=2)
    payloads = [{"a": 1}, {"b": 2}, {"c": 3}]
    chunks = list(workflow._chunk_payloads(payloads))

    assert len(chunks) == 2
    assert len(chunks[0]) == 2
    assert len(chunks[1]) == 1


def test_phase1_only(mock_sdk):
    """Test submission with only phase 1 (registration) records."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.create_record.return_value = Job(batch_id="job1", state="PENDING")
    mock_sdk.get_job.return_value = JobStatus(batch_id="job1", state="COMPLETED")

    rs = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 1}],
        subject_keys=["S1"],
    )

    result = workflow.submit("study1", [rs])

    assert len(result.registration_batches) == 1
    assert len(result.data_batches) == 0
    assert result.registration_batches[0].record_count == 1
    mock_sdk.create_record.assert_called_once()


def test_phase2_only(mock_sdk):
    """Test submission with only phase 2 (data) records."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, await_registration=False)
    mock_sdk.create_record.return_value = Job(batch_id="job2", state="PENDING")

    rs = GeneratedRecordSet(
        form_key="DATA",
        form_name="Data Form",
        test_type=RecordTestType.UPDATE_SCHEDULED_RECORD,
        payloads=[{"data": 2}],
        subject_keys=["S1"],
    )

    result = workflow.submit("study1", [rs])

    assert len(result.registration_batches) == 0
    assert len(result.data_batches) == 1
    mock_sdk.create_record.assert_called_once()


def test_full_two_phase_flow(mock_sdk):
    """Test the complete two-phase submission flow."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)

    # Mock Phase 1 call
    job1 = Job(batch_id="job1", state="PENDING")
    # Mock Phase 2 call
    job2 = Job(batch_id="job2", state="PENDING")

    mock_sdk.create_record.side_effect = [job1, job2]

    # Mock polling for Phase 1
    mock_sdk.get_job.return_value = JobStatus(batch_id="job1", state="COMPLETED")

    rs1 = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 1}],
        subject_keys=["S1"],
    )
    rs2 = GeneratedRecordSet(
        form_key="DATA",
        form_name="Data Form",
        test_type=RecordTestType.UPDATE_SCHEDULED_RECORD,
        payloads=[{"data": 2}],
        subject_keys=["S1"],
    )

    result = workflow.submit("study1", [rs1, rs2])

    assert len(result.registration_batches) == 1
    assert len(result.data_batches) == 1
    assert mock_sdk.create_record.call_count == 2
    mock_sdk.get_job.assert_called_with("study1", "job1")


def test_bulk_submission_error_on_phase1_failure(mock_sdk):
    """Test that BulkSubmissionError is raised when phase 1 fails."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)

    job1 = Job(batch_id="job1", state="PENDING")
    mock_sdk.create_record.return_value = job1

    # Mock polling failure
    mock_sdk.get_job.return_value = JobStatus(batch_id="job1", state="FAILED")

    rs1 = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 1}],
        subject_keys=["S1"],
    )

    with pytest.raises(BulkSubmissionError) as excinfo:
        workflow.submit("study1", [rs1])

    assert "1 registration batches failed" in str(excinfo.value)
    assert len(excinfo.value.failed_batches) == 1


def test_batch_size_logic(mock_sdk):
    """Test that payloads are correctly batched according to batch_size."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, batch_size=2)
    mock_sdk.create_record.return_value = Job(batch_id="job", state="PENDING")

    rs = GeneratedRecordSet(
        form_key="DATA",
        form_name="Data Form",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[{"d": 1}, {"d": 2}, {"d": 3}],
        subject_keys=["S1", "S2", "S3"],
    )

    result = workflow.submit("study1", [rs])

    assert len(result.data_batches) == 2
    assert result.data_batches[0].record_count == 2
    assert result.data_batches[1].record_count == 1
    assert mock_sdk.create_record.call_count == 2


def test_skip_existing_subjects_true(mock_sdk):
    """Test that existing subjects are skipped during phase 1."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=True)

    # Mock existing subject
    subj = MagicMock(spec=Subject)
    subj.subject_key = "S1"
    mock_sdk.get_subjects.return_value = [subj]

    rs = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": "for S1"}],
        subject_keys=["S1"],
    )

    result = workflow.submit("study1", [rs])

    assert len(result.registration_batches) == 0
    assert "REG" in result.skipped_forms
    mock_sdk.get_subjects.assert_called_once_with("study1", keyword="UAT")
    mock_sdk.create_record.assert_not_called()


def test_skip_existing_subjects_false(mock_sdk):
    """Test that registration is attempted even if subjects exist when skip_existing_subjects=False."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.create_record.return_value = Job(batch_id="job1", state="PENDING")
    mock_sdk.get_job.return_value = JobStatus(batch_id="job1", state="COMPLETED")

    rs = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": "for S1"}],
        subject_keys=["S1"],
    )

    result = workflow.submit("study1", [rs])

    assert len(result.registration_batches) == 1
    mock_sdk.get_subjects.assert_not_called()
    mock_sdk.create_record.assert_called_once()


def test_concurrent_registration_polling_multi_batch_success(mock_sdk):
    """Test concurrent polling across multiple registration batches succeeding."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)

    job1 = Job(batch_id="reg_job_1", state="PENDING")
    job2 = Job(batch_id="reg_job_2", state="PENDING")
    mock_sdk.create_record.side_effect = [job1, job2]

    def mock_get_job(study_key, batch_id):
        return JobStatus(batch_id=batch_id, state="COMPLETED")

    mock_sdk.get_job.side_effect = mock_get_job

    rs1 = GeneratedRecordSet(
        form_key="REG1",
        form_name="Registration 1",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 1}],
        subject_keys=["S1"],
    )
    rs2 = GeneratedRecordSet(
        form_key="REG2",
        form_name="Registration 2",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 2}],
        subject_keys=["S2"],
    )

    result = workflow.submit("study1", [rs1, rs2])

    assert len(result.registration_batches) == 2
    assert result.registration_batches[0].job.state == "COMPLETED"
    assert result.registration_batches[1].job.state == "COMPLETED"
    assert mock_sdk.get_job.call_count == 2


def test_concurrent_registration_polling_partial_failure(mock_sdk):
    """Test concurrent polling with one batch succeeding and one batch failing."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)

    job1 = Job(batch_id="reg_job_1", state="PENDING")
    job2 = Job(batch_id="reg_job_2", state="PENDING")
    mock_sdk.create_record.side_effect = [job1, job2]

    def mock_get_job(study_key, batch_id):
        if batch_id == "reg_job_1":
            return JobStatus(batch_id="reg_job_1", state="COMPLETED")
        return JobStatus(batch_id="reg_job_2", state="FAILED")

    mock_sdk.get_job.side_effect = mock_get_job

    rs1 = GeneratedRecordSet(
        form_key="REG1",
        form_name="Registration 1",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 1}],
        subject_keys=["S1"],
    )
    rs2 = GeneratedRecordSet(
        form_key="REG2",
        form_name="Registration 2",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 2}],
        subject_keys=["S2"],
    )

    with pytest.raises(BulkSubmissionError) as excinfo:
        workflow.submit("study1", [rs1, rs2])

    assert "1 registration batches failed." in str(excinfo.value)
    assert len(excinfo.value.failed_batches) == 1
    assert excinfo.value.failed_batches[0].job.batch_id == "reg_job_2"
    assert excinfo.value.failed_batches[0].job.state == "FAILED"
    assert job1.state == "COMPLETED"


def test_concurrent_registration_polling_timeout(mock_sdk):
    """Test concurrent polling when a batch times out."""
    workflow = BulkRecordSubmissionWorkflow(
        mock_sdk,
        skip_existing_subjects=False,
        registration_timeout=0.0,
    )

    job1 = Job(batch_id="reg_job_1", state="PENDING")
    mock_sdk.create_record.return_value = job1
    mock_sdk.get_job.return_value = JobStatus(batch_id="reg_job_1", state="PENDING")

    rs = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 1}],
        subject_keys=["S1"],
    )

    with pytest.raises(BulkSubmissionError) as excinfo:
        workflow.submit("study1", [rs])

    assert "1 registration batches failed." in str(excinfo.value)
    assert len(excinfo.value.failed_batches) == 1
    assert excinfo.value.failed_batches[0].job.batch_id == "reg_job_1"
    assert excinfo.value.failed_batches[0].job.state == "TIMEOUT"


def test_concurrent_registration_polling_multi_batch_mixed_failures(mock_sdk):
    """Test concurrent polling with multiple failures including a timeout and a failed job."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)

    job1 = Job(batch_id="reg_job_1", state="PENDING")
    job2 = Job(batch_id="reg_job_2", state="PENDING")
    job3 = Job(batch_id="reg_job_3", state="PENDING")
    mock_sdk.create_record.side_effect = [job1, job2, job3]

    def mock_get_job(study_key, batch_id):
        if batch_id == "reg_job_1":
            return JobStatus(batch_id="reg_job_1", state="COMPLETED")
        if batch_id == "reg_job_2":
            return JobStatus(batch_id="reg_job_2", state="FAILED")
        raise JobTimeoutError("Simulated timeout")

    mock_sdk.get_job.side_effect = mock_get_job

    rs1 = GeneratedRecordSet(
        form_key="REG1",
        form_name="Registration 1",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 1}],
        subject_keys=["S1"],
    )
    rs2 = GeneratedRecordSet(
        form_key="REG2",
        form_name="Registration 2",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 2}],
        subject_keys=["S2"],
    )
    rs3 = GeneratedRecordSet(
        form_key="REG3",
        form_name="Registration 3",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 3}],
        subject_keys=["S3"],
    )

    with pytest.raises(BulkSubmissionError) as excinfo:
        workflow.submit("study1", [rs1, rs2, rs3])

    assert "2 registration batches failed." in str(excinfo.value)
    assert len(excinfo.value.failed_batches) == 2
    failed_ids = {b.job.batch_id for b in excinfo.value.failed_batches}
    assert failed_ids == {"reg_job_2", "reg_job_3"}
    assert job1.state == "COMPLETED"
    assert job2.state == "FAILED"
    assert job3.state == "TIMEOUT"


def test_concurrent_registration_polling_skips_empty_batch_ids(mock_sdk):
    """Test that batches with missing or empty batch_id are not polled."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)

    batch_none = BatchSubmission(
        phase="registration",
        record_count=1,
        job=Job(batch_id=None, state="PENDING"),
    )
    batch_empty = BatchSubmission(
        phase="registration",
        record_count=1,
        job=Job(batch_id="", state="PENDING"),
    )
    batch_valid = BatchSubmission(
        phase="registration",
        record_count=1,
        job=Job(batch_id="valid_job", state="PENDING"),
    )

    mock_sdk.get_job.return_value = JobStatus(batch_id="valid_job", state="COMPLETED")

    workflow._await_registration_jobs("study1", [batch_none, batch_empty, batch_valid])

    mock_sdk.get_job.assert_called_once_with("study1", "valid_job")
    assert batch_valid.job.state == "COMPLETED"


def test_submission_phase_lifecycle_success(mock_sdk):
    """Test BulkRecordSubmissionWorkflow transitions through REGISTRATION -> AWAITING -> DATA -> COMPLETED."""
    from imednet_workflows.uat.submission import SubmissionPhase

    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    assert workflow.phase == SubmissionPhase.PENDING

    mock_sdk.create_record.side_effect = [
        Job(batch_id="reg_1", state="PENDING"),
        Job(batch_id="data_1", state="PENDING"),
    ]
    mock_sdk.get_job.return_value = JobStatus(batch_id="reg_1", state="COMPLETED")

    rs_reg = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 1}],
        subject_keys=["S1"],
    )
    rs_data = GeneratedRecordSet(
        form_key="DATA",
        form_name="Data Form",
        test_type=RecordTestType.UPDATE_SCHEDULED_RECORD,
        payloads=[{"data": 2}],
        subject_keys=["S1"],
    )

    result = workflow.submit("study1", [rs_reg, rs_data])
    assert workflow.phase == SubmissionPhase.COMPLETED
    assert len(result.registration_batches) == 1
    assert len(result.data_batches) == 1


def test_submission_phase_lifecycle_without_await(mock_sdk):
    """Test when await_registration=False, workflow skips awaiting and completes DATA."""
    from imednet_workflows.uat.submission import SubmissionPhase

    workflow = BulkRecordSubmissionWorkflow(
        mock_sdk,
        await_registration=False,
        skip_existing_subjects=False,
    )
    assert workflow.phase == SubmissionPhase.PENDING

    mock_sdk.create_record.return_value = Job(batch_id="job", state="PENDING")

    rs = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 1}],
        subject_keys=["S1"],
    )

    workflow.submit("study1", [rs])
    assert workflow.phase == SubmissionPhase.COMPLETED
    mock_sdk.get_job.assert_not_called()


def test_submission_phase_lifecycle_on_failure(mock_sdk):
    """Test workflow phase is marked FAILED when registration polling fails."""
    from imednet_workflows.uat.submission import SubmissionPhase

    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.create_record.return_value = Job(batch_id="reg_1", state="PENDING")
    mock_sdk.get_job.return_value = JobStatus(batch_id="reg_1", state="FAILED")

    rs = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"data": 1}],
        subject_keys=["S1"],
    )

    with pytest.raises(BulkSubmissionError):
        workflow.submit("study1", [rs])

    assert workflow.phase == SubmissionPhase.FAILED


def test_submission_poller_uses_job_transition_to_and_job_state(mock_sdk):
    """Verify Job.transition_to is invoked and sets JobState enum values."""
    from unittest.mock import patch

    from imednet.spi.models import JobState

    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)

    real_job = Job(batch_id="job_spy", state="PENDING")
    batch = BatchSubmission(phase="registration", record_count=1, job=real_job)

    mock_sdk.get_job.return_value = JobStatus(batch_id="job_spy", state="COMPLETED")

    with patch.object(
        Job, "transition_to", autospec=True, wraps=Job.transition_to
    ) as mock_transition:
        workflow._await_registration_jobs("study1", [batch])
        mock_transition.assert_called_once_with(real_job, JobState.COMPLETED)

    assert batch.job.state == JobState.COMPLETED
    assert isinstance(batch.job.state, JobState)
    assert batch.job.is_successful is True


def test_submission_poller_rejects_illegal_job_transition(mock_sdk):
    """Verify that attempting an illegal transition on Job raises InvalidJobTransitionError."""
    from imednet.models.jobs import InvalidJobTransitionError

    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)

    # Job is already terminal COMPLETED
    job = Job(batch_id="job_completed", state="COMPLETED")
    batch = BatchSubmission(phase="registration", record_count=1, job=job)

    # Poller returns FAILED, which is an illegal transition from COMPLETED
    mock_sdk.get_job.return_value = JobStatus(batch_id="job_completed", state="FAILED")

    with pytest.raises(InvalidJobTransitionError):
        workflow._await_registration_jobs("study1", [batch])
