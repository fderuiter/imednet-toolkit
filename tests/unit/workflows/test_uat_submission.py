"""Unit tests for BulkRecordSubmissionWorkflow."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from imednet.spi.models import Job, JobStatus, Subject, SubjectKeyword
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
    """Test that existing UAT-tagged subjects are skipped during phase 1."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=True)

    tagged_subj = Subject(
        subjectKey="S1",
        deleted=False,
        keywords=[SubjectKeyword(keywordKey="UAT", keywordName="User Acceptance Testing")],
    )
    untagged_subj = Subject(
        subjectKey="S2",
        deleted=False,
        keywords=[SubjectKeyword(keywordKey="PROD", keywordName="Production")],
    )
    mock_sdk.get_subjects.return_value = [tagged_subj, untagged_subj]

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
    mock_sdk.get_subjects.assert_called_once_with("study1", deleted=False)
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


def test_omitted_subject_keys_fails_closed_instead_of_taking_preexisting_subjects(mock_sdk):
    """Pre-existing study subjects never receive Phase 2 records when job results omit subjectKey."""
    from imednet_workflows.uat.submission import SubmissionPhase

    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.create_record.return_value = Job(batch_id="reg_1", state="PENDING")
    mock_sdk.get_job.return_value = JobStatus(
        batch_id="reg_1",
        state="COMPLETED",
        results={"status": "Completed"},
    )
    mock_sdk.get_subjects.return_value = [
        Subject(subject_id=1, subject_key="PRE-001", site_name="Site1"),
        Subject(subject_id=2, subject_key="PRE-002", site_name="Site1"),
    ]
    mock_sdk.get_records.return_value = []

    rs_reg = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"formKey": "REG", "siteName": "Site1", "data": {"DEM_INIT": "NEWVAL"}}],
        subject_keys=["UAT-TEST-001"],
    )
    rs_data = GeneratedRecordSet(
        form_key="VIS",
        form_name="Visit",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[{"formKey": "VIS", "subjectKey": "UAT-TEST-001", "data": {"BP": "120"}}],
        subject_keys=["UAT-TEST-001"],
    )

    with pytest.raises(BulkSubmissionError, match="correlate"):
        workflow.submit("study1", [rs_reg, rs_data])

    assert workflow.phase == SubmissionPhase.FAILED
    # Only the single Phase 1 call occurred; Phase 2 never wrote to PRE-001 or PRE-002
    assert mock_sdk.create_record.call_count == 1


def test_omitted_subject_keys_correlates_by_record_id_and_ignores_concurrent_and_preexisting(
    mock_sdk,
):
    """When results omit subjectKey but include recordId, only this batch's records are matched."""
    from imednet.models.records import Record

    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    submitted_calls = []

    def create_record_side_effect(study_key, payloads, email_notify=None):
        submitted_calls.append(payloads)
        idx = len(submitted_calls)
        return Job(batch_id=f"batch_{idx}", state="PENDING")

    mock_sdk.create_record.side_effect = create_record_side_effect
    mock_sdk.get_job.return_value = JobStatus(
        batch_id="batch_1",
        state="COMPLETED",
        results=[{"recordId": 502, "status": "SUCCESS"}],
    )
    subjects_state = [Subject(subject_id=10, subject_key="PRE-001", site_name="Site1")]

    def get_subjects_side_effect(_study_key):
        if len(submitted_calls) == 0:
            return list(subjects_state)
        return [
            *subjects_state,
            Subject(subject_id=11, subject_key="OUR-SUBJ-502", site_name="Site1"),
            Subject(subject_id=12, subject_key="CONCURRENT-999", site_name="Site1"),
        ]

    mock_sdk.get_subjects.side_effect = get_subjects_side_effect
    mock_sdk.get_records.return_value = [
        Record(
            record_id=501,
            form_key="REG",
            subject_key="PRE-001",
            record_data={"DEM_INIT": "ALPHA"},
        ),
        Record(
            record_id=502,
            form_key="REG",
            subject_key="OUR-SUBJ-502",
            record_data={"DEM_INIT": "ALPHA"},
        ),
        Record(
            record_id=503,
            form_key="REG",
            subject_key="CONCURRENT-999",
            record_data={"DEM_INIT": "ALPHA"},
        ),
    ]

    rs_reg = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"formKey": "REG", "siteName": "Site1", "data": {"DEM_INIT": "ALPHA"}}],
        subject_keys=["UAT-TEST-001"],
    )
    rs_data = GeneratedRecordSet(
        form_key="VIS",
        form_name="Visit",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[{"formKey": "VIS", "subjectKey": "UAT-TEST-001", "data": {"BP": "120"}}],
        subject_keys=["UAT-TEST-001"],
    )

    result = workflow.submit("study1", [rs_reg, rs_data])
    assert result.enrolled_subject_keys == ["OUR-SUBJ-502"]
    assert len(submitted_calls) == 2
    assert [p["subjectKey"] for p in submitted_calls[1]] == ["OUR-SUBJ-502"]


def test_omitted_identifiers_ambiguous_concurrent_registration_fails_closed(mock_sdk):
    """Indistinguishable concurrent registration fails closed without writing Phase 2 records."""
    from imednet.models.records import Record
    from imednet_workflows.uat.submission import SubmissionPhase

    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    submitted_calls = []

    def create_record_side_effect(study_key, payloads, email_notify=None):
        submitted_calls.append(payloads)
        return Job(batch_id=f"b{len(submitted_calls)}", state="PENDING")

    mock_sdk.create_record.side_effect = create_record_side_effect
    mock_sdk.get_job.return_value = JobStatus(
        batch_id="b1",
        state="COMPLETED",
        results=[{"status": "SUCCESS"}],
    )
    mock_sdk.get_subjects.side_effect = [
        [Subject(subject_id=1, subject_key="PRE-001", site_name="Site1")],
        [
            Subject(subject_id=1, subject_key="PRE-001", site_name="Site1"),
            Subject(subject_id=2, subject_key="SUBJ-A", site_name="Site1"),
            Subject(subject_id=3, subject_key="SUBJ-B", site_name="Site1"),
        ],
    ]
    # Both SUBJ-A (ours) and SUBJ-B (concurrent batch) have identical registration data
    mock_sdk.get_records.return_value = [
        Record(record_id=201, form_key="REG", subject_key="SUBJ-A", record_data={"V": "SAME"}),
        Record(record_id=202, form_key="REG", subject_key="SUBJ-B", record_data={"V": "SAME"}),
    ]

    rs_reg = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"formKey": "REG", "siteName": "Site1", "data": {"V": "SAME"}}],
        subject_keys=["UAT-TEST-001"],
    )
    rs_data = GeneratedRecordSet(
        form_key="VIS",
        form_name="Visit",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[{"formKey": "VIS", "subjectKey": "UAT-TEST-001", "data": {"BP": "120"}}],
        subject_keys=["UAT-TEST-001"],
    )

    with pytest.raises(BulkSubmissionError, match="Ambiguous"):
        workflow.submit("study1", [rs_reg, rs_data])

    assert workflow.phase == SubmissionPhase.FAILED
    assert len(submitted_calls) == 1


def test_correlates_by_subject_id_and_rejects_preexisting_or_duplicate_results(mock_sdk):
    """Correlates via subjectId/subjectOid and rejects pre-existing or duplicate keys."""
    from imednet_workflows.uat.submission import SubmissionPhase

    # 1. Valid subjectId lookup from nested dict results={"subjects": [...]}
    workflow = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.create_record.return_value = Job(batch_id="b_subj", state="PENDING")
    mock_sdk.get_job.return_value = JobStatus(
        batch_id="b_subj",
        state="COMPLETED",
        results={"subjects": [{"subjectId": "42", "status": "SUCCESS"}]},
    )
    mock_sdk.get_subjects.side_effect = [
        [Subject(subject_id=1, subject_key="PRE-001", subject_oid="OID-1", site_name="Site1")],
        [
            Subject(subject_id=1, subject_key="PRE-001", subject_oid="OID-1", site_name="Site1"),
            Subject(subject_id=42, subject_key="SUBJ-042", subject_oid="OID-42", site_name="Site1"),
            Subject(subject_id=99, subject_key="OTHER-SITE", site_name="Site2"),
        ],
    ]

    rs_reg = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"formKey": "REG", "siteName": "Site1", "data": {"AGE": 30}}],
        subject_keys=["UAT-TEST-001"],
    )
    rs_data = GeneratedRecordSet(
        form_key="VIS",
        form_name="Visit",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[{"formKey": "VIS", "subjectKey": "UAT-TEST-001", "data": {"BP": 120}}],
        subject_keys=["UAT-TEST-001"],
    )

    res = workflow.submit("study1", [rs_reg, rs_data])
    assert res.enrolled_subject_keys == ["SUBJ-042"]
    assert rs_data.payloads[0]["subjectKey"] == "SUBJ-042"

    # 2. Pre-existing subject key in results fails closed before Phase 2
    wf_pre = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.create_record.reset_mock()
    mock_sdk.get_job.return_value = JobStatus(
        batch_id="b_subj",
        state="COMPLETED",
        results=["PRE-001"],
    )
    mock_sdk.get_subjects.side_effect = [
        [Subject(subject_id=1, subject_key="PRE-001", site_name="Site1")],
    ]
    rs_data_pre = GeneratedRecordSet(
        form_key="VIS",
        form_name="Visit",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[{"formKey": "VIS", "subjectKey": "UAT-TEST-001", "data": {"BP": 120}}],
        subject_keys=["UAT-TEST-001"],
    )
    with pytest.raises(BulkSubmissionError, match="pre-existing"):
        wf_pre.submit("study1", [rs_reg, rs_data_pre])
    assert wf_pre.phase == SubmissionPhase.FAILED
    assert mock_sdk.create_record.call_count == 1

    # 3. Duplicate subject keys across 2 submitted items fail closed
    wf_dup = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.create_record.reset_mock()
    mock_sdk.get_subjects.side_effect = [[], []]
    mock_sdk.get_job.return_value = JobStatus(
        batch_id="b_subj",
        state="COMPLETED",
        results=[{"subjectKey": "DUP-1"}, {"subjectKey": "DUP-1"}],
    )
    rs_reg_two = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[
            {"formKey": "REG", "siteName": "Site1", "data": {"AGE": 30}},
            {"formKey": "REG", "siteName": "Site1", "data": {"AGE": 31}},
        ],
        subject_keys=["UAT-1", "UAT-2"],
    )
    rs_data_two = GeneratedRecordSet(
        form_key="VIS",
        form_name="Visit",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[
            {"formKey": "VIS", "subjectKey": "UAT-1", "data": {"BP": 120}},
            {"formKey": "VIS", "subjectKey": "UAT-2", "data": {"BP": 121}},
        ],
        subject_keys=["UAT-1", "UAT-2"],
    )
    with pytest.raises(BulkSubmissionError, match="Duplicate|duplicate"):
        wf_dup.submit("study1", [rs_reg_two, rs_data_two])
    assert wf_dup.phase == SubmissionPhase.FAILED
    assert mock_sdk.create_record.call_count == 1


def test_await_registration_false_with_subject_key_phase2_fails_closed(mock_sdk):
    """Skipping registration polling fails closed when Phase 2 requires enrolled subject keys."""
    from imednet_workflows.uat.submission import SubmissionPhase

    workflow = BulkRecordSubmissionWorkflow(
        mock_sdk, await_registration=False, skip_existing_subjects=False
    )
    mock_sdk.create_record.return_value = Job(batch_id="reg_no_await", state="PENDING")

    rs_reg = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"formKey": "REG", "siteName": "Site1", "data": {"V": "1"}}],
        subject_keys=["UAT-TEST-001"],
    )
    rs_data = GeneratedRecordSet(
        form_key="VIS",
        form_name="Visit",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[{"formKey": "VIS", "subjectKey": "UAT-TEST-001", "data": {"BP": "120"}}],
        subject_keys=["UAT-TEST-001"],
    )

    with pytest.raises(BulkSubmissionError, match="await_registration"):
        workflow.submit("study1", [rs_reg, rs_data])

    assert workflow.phase == SubmissionPhase.FAILED
    assert mock_sdk.create_record.call_count == 1


def test_result_count_mismatch_and_submitted_subject_oid_correlation(mock_sdk):
    """Mismatched result counts fail closed; submitted subjectOid correlates via get_subjects."""
    # 1. Result count mismatch fails closed
    wf_mismatch = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.create_record.return_value = Job(batch_id="b_mis", state="PENDING")
    mock_sdk.get_subjects.side_effect = [[]]
    mock_sdk.get_job.return_value = JobStatus(
        batch_id="b_mis",
        state="COMPLETED",
        results=[{"subjectKey": "ONLY-ONE"}],
    )
    rs_reg_two = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[
            {"formKey": "REG", "siteName": "Site1", "data": {"V": "1"}},
            {"formKey": "REG", "siteName": "Site1", "data": {"V": "2"}},
        ],
        subject_keys=["UAT-1", "UAT-2"],
    )
    rs_data_two = GeneratedRecordSet(
        form_key="VIS",
        form_name="Visit",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[
            {"formKey": "VIS", "subjectKey": "UAT-1", "data": {"BP": 120}},
            {"formKey": "VIS", "subjectKey": "UAT-2", "data": {"BP": 121}},
        ],
        subject_keys=["UAT-1", "UAT-2"],
    )
    with pytest.raises(BulkSubmissionError, match="returned 1 result entries for 2"):
        wf_mismatch.submit("study1", [rs_reg_two, rs_data_two])

    # 2. Submitted subjectOid correlates uniquely via get_subjects when results omit keys
    wf_oid = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.get_job.return_value = JobStatus(batch_id="b_mis", state="COMPLETED", results=None)
    mock_sdk.get_subjects.side_effect = [
        [],
        [Subject(subject_id=77, subject_key="OID-RESOLVED-77", subject_oid="CLIENT-OID-77")],
    ]
    rs_reg_oid = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"formKey": "REG", "subjectOid": "CLIENT-OID-77", "data": {}}],
        subject_keys=["UAT-OID-1"],
    )
    rs_data_oid = GeneratedRecordSet(
        form_key="VIS",
        form_name="Visit",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[{"formKey": "VIS", "subjectKey": "UAT-OID-1", "data": {"BP": 120}}],
        subject_keys=["UAT-OID-1"],
    )
    res = wf_oid.submit("study1", [rs_reg_oid, rs_data_oid])
    assert res.enrolled_subject_keys == ["OID-RESOLVED-77"]
    assert rs_data_oid.payloads[0]["subjectKey"] == "OID-RESOLVED-77"


def test_static_mock_preexisting_subjects_and_cross_site_records_fail_closed(mock_sdk):
    """Static return_value pre-existing subjects and cross-site records never receive Phase 2 writes."""
    from imednet.models.records import Record
    from imednet_workflows.uat.submission import SubmissionPhase

    # 1. Static get_subjects.return_value + matching pre-existing record still fails closed
    wf_static = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.create_record.return_value = Job(batch_id="b_static", state="PENDING")
    mock_sdk.get_job.return_value = JobStatus(batch_id="b_static", state="COMPLETED", results=None)
    mock_sdk.get_subjects.return_value = [
        Subject(subject_id=1, subject_key="STATIC-PRE-001", site_name="Site1")
    ]
    mock_sdk.get_records.return_value = [
        Record(
            record_id=10,
            form_key="REG",
            subject_key="STATIC-PRE-001",
            record_data={"V": "SAME"},
        )
    ]

    rs_reg = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"formKey": "REG", "siteName": "Site1", "data": {"V": "SAME"}}],
        subject_keys=["UAT-1"],
    )
    rs_data = GeneratedRecordSet(
        form_key="VIS",
        form_name="Visit",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[{"formKey": "VIS", "subjectKey": "UAT-1", "data": {"BP": 120}}],
        subject_keys=["UAT-1"],
    )

    with pytest.raises(BulkSubmissionError, match="no matching enrollment record"):
        wf_static.submit("study1", [rs_reg, rs_data])
    assert wf_static.phase == SubmissionPhase.FAILED
    assert mock_sdk.create_record.call_count == 1

    # 2. Cross-site record (site_name mismatch on record itself) fails closed when get_subjects lags
    mock_sdk.create_record.reset_mock()
    wf_site = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.get_subjects.side_effect = [[], []]
    mock_sdk.get_records.return_value = [
        {
            "recordId": 99,
            "formKey": "REG",
            "subjectKey": "OTHER-SITE-SUBJ",
            "siteName": "Site2",
            "recordData": {"V": "SAME"},
        }
    ]
    with pytest.raises(BulkSubmissionError, match="no matching enrollment record"):
        wf_site.submit("study1", [rs_reg, rs_data])
    assert wf_site.phase == SubmissionPhase.FAILED
    assert mock_sdk.create_record.call_count == 1

    # 3. Two indistinguishable submitted payloads without result identifiers fail closed
    mock_sdk.create_record.reset_mock()
    wf_indist = BulkRecordSubmissionWorkflow(mock_sdk, skip_existing_subjects=False)
    mock_sdk.get_subjects.side_effect = [[], []]
    mock_sdk.get_records.return_value = [
        Record(record_id=101, form_key="REG", subject_key="SUBJ-1", record_data={"V": "SAME"}),
        Record(record_id=102, form_key="REG", subject_key="SUBJ-2", record_data={"V": "SAME"}),
    ]
    rs_reg_dup = GeneratedRecordSet(
        form_key="REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[
            {"formKey": "REG", "siteName": "Site1", "data": {"V": "SAME"}},
            {"formKey": "REG", "siteName": "Site1", "data": {"V": "SAME"}},
        ],
        subject_keys=["UAT-1", "UAT-2"],
    )
    rs_data_dup = GeneratedRecordSet(
        form_key="VIS",
        form_name="Visit",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[
            {"formKey": "VIS", "subjectKey": "UAT-1", "data": {"BP": 120}},
            {"formKey": "VIS", "subjectKey": "UAT-2", "data": {"BP": 130}},
        ],
        subject_keys=["UAT-1", "UAT-2"],
    )
    with pytest.raises(BulkSubmissionError, match="Ambiguous"):
        wf_indist.submit("study1", [rs_reg_dup, rs_data_dup])
    assert wf_indist.phase == SubmissionPhase.FAILED
    assert mock_sdk.create_record.call_count == 1


def test_find_existing_uat_subjects_local_keyword_filtering(mock_sdk):
    """Verify local Subject keyword matching across models, dicts, deleted flags, and empty tags."""
    workflow = BulkRecordSubmissionWorkflow(mock_sdk)
    mock_sdk.get_subjects.return_value = [
        Subject(
            subjectKey="BY-NAME",
            deleted=False,
            keywords=[SubjectKeyword(keywordKey="TAG_1", keywordName="UAT")],
        ),
        Subject(
            subjectKey="DELETED-UAT",
            deleted=True,
            keywords=[SubjectKeyword(keywordKey="UAT", keywordName="UAT")],
        ),
        Subject(subjectKey="NO-KEYWORDS", deleted=False, keywords=[]),
        {"subjectKey": "DICT-KEY", "deleted": False, "keywords": [{"keywordKey": "UAT"}]},
        {"subject_key": "STR-KW", "deleted": False, "keywords": ["UAT"]},
    ]

    matched = workflow._find_existing_uat_subjects("study1", "UAT")
    assert matched == {"BY-NAME", "DICT-KEY", "STR-KW"}
    mock_sdk.get_subjects.assert_called_once_with("study1", deleted=False)

    all_non_deleted = workflow._find_existing_uat_subjects("study1", "")
    assert all_non_deleted == {"BY-NAME", "NO-KEYWORDS", "DICT-KEY", "STR-KW"}


def test_default_uat_submission_with_tagged_synthetic_subject_and_real_sdk():
    """Verify default UAT submission and UATWorkflow against real ImednetSDK filter registry."""
    import httpx

    from imednet import ImednetSDK
    from imednet.simulation import SimulationState, create_simulation_transport
    from imednet_workflows.uat import UATWorkflow

    state = SimulationState()
    state.subjects.extend(
        [
            {
                "subjectId": 290,
                "subjectKey": "UAT-TEST-001",
                "studyKey": "STUDY",
                "siteId": 101,
                "siteName": "SITE",
                "subjectStatus": "Enrolled",
                "deleted": False,
                "keywords": [
                    {
                        "keywordName": "UAT",
                        "keywordKey": "UAT",
                        "keywordId": 10,
                        "dateAdded": "2026-01-05T08:00:00Z",
                    }
                ],
                "dateCreated": "2026-01-05T08:00:00Z",
                "dateModified": "2026-01-05T08:00:00Z",
                "lastUpdated": "2026-01-05T08:00:00Z",
            },
            {
                "subjectId": 291,
                "subjectKey": "UAT-TEST-002",
                "studyKey": "STUDY",
                "siteId": 101,
                "siteName": "SITE",
                "subjectStatus": "Enrolled",
                "deleted": False,
                "keywords": [
                    {
                        "keywordName": "Cohort A",
                        "keywordKey": "COHORT_A",
                        "keywordId": 11,
                        "dateAdded": "2026-01-05T08:00:00Z",
                    }
                ],
                "dateCreated": "2026-01-05T08:00:00Z",
                "dateModified": "2026-01-05T08:00:00Z",
                "lastUpdated": "2026-01-05T08:00:00Z",
            },
        ]
    )

    sim_transport = create_simulation_transport(state)
    captured_requests: list[httpx.Request] = []

    def tracking_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return sim_transport.handle_request(request)

    sdk = ImednetSDK(transport=httpx.MockTransport(tracking_handler), simulation_mode=True)

    # 1. Default BulkRecordSubmissionWorkflow skips UAT-TEST-001 (tagged UAT)
    #    while still registering UAT-TEST-002 (untagged for UAT).
    rs_reg = GeneratedRecordSet(
        form_key="F_DM",
        form_name="Demographics",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[
            {"formKey": "F_DM", "siteName": "SITE", "data": {"V_AGE": "30"}},
            {"formKey": "F_DM", "siteName": "SITE", "data": {"V_AGE": "41"}},
        ],
        subject_keys=["UAT-TEST-001", "UAT-TEST-002"],
    )
    rs_data = GeneratedRecordSet(
        form_key="F_VS",
        form_name="Vital Signs",
        test_type=RecordTestType.UPDATE_SCHEDULED_RECORD,
        payloads=[
            {
                "formKey": "F_VS",
                "subjectKey": "UAT-TEST-001",
                "intervalName": "Baseline",
                "data": {"V_WEIGHT": "70.0"},
            },
            {
                "formKey": "F_VS",
                "subjectKey": "UAT-TEST-002",
                "intervalName": "Baseline",
                "data": {"V_WEIGHT": "72.5"},
            },
        ],
        subject_keys=["UAT-TEST-001", "UAT-TEST-002"],
    )

    submitter = BulkRecordSubmissionWorkflow(sdk)
    sub_result = submitter.submit("STUDY", [rs_reg, rs_data])

    subject_requests = [
        r for r in captured_requests if r.method == "GET" and r.url.path.endswith("/subjects")
    ]
    assert len(subject_requests) == 1
    assert dict(subject_requests[0].url.params)["filter"] == "deleted==false"
    assert len(sub_result.registration_batches) == 1
    assert sub_result.registration_batches[0].record_count == 1
    assert len(sub_result.data_batches) == 1
    assert sub_result.data_batches[0].record_count == 2

    # 2. End-to-end default UATWorkflow.run with subject_count=1 skips registration form
    #    entirely when UAT-TEST-001 is already tagged and completes Phase 2 cleanly.
    uat_workflow = UATWorkflow(sdk, poll_interval=0.01, seed=42)
    run_result = uat_workflow.run("STUDY", subject_count=1)
    assert run_result.overall_success is True
    assert run_result.submission_result is not None
    assert "F_DM" in run_result.submission_result.skipped_forms
    assert len(run_result.submission_result.registration_batches) == 0
    assert len(run_result.submission_result.data_batches) == 1
