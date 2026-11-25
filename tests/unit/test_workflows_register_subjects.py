"""Unit tests for workflows register subjects."""

from unittest.mock import MagicMock

import pytest

from imednet.models.jobs import Job
from imednet.models.records import RegisterSubjectRequest
from imednet.models.sites import Site
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow


def test_register_subjects_passes_records_correctly() -> None:
    """Test that register subjects passes records correctly."""
    sdk = MagicMock()
    job = Job(jobId="1", batchId="1", state="PROCESSING")
    sdk.create_record.return_value = job
    sdk.get_sites.return_value = [
        Site(studyKey="S", siteId=1, siteName="SITE", siteEnrollmentStatus="Active")
    ]
    wf = RegisterSubjectsWorkflow(sdk)
    req = RegisterSubjectRequest(formKey="F", siteName="SITE")

    result = wf.register_subjects("STUDY", [req], email_notify="test@example.com")

    sdk.get_sites.assert_called_once_with(study_key="STUDY")
    sdk.subjects.get.assert_not_called()
    sdk.create_record.assert_called_once_with(
        study_key="STUDY",
        records_data=[req.model_dump(by_alias=True)],
        email_notify="test@example.com",
    )
    assert result == job


def test_register_subjects_with_polling() -> None:
    """Test that register subjects with polling."""
    sdk = MagicMock()
    job = Job(jobId="1", batchId="BATCH", state="PROCESSING")
    completed_job = Job(jobId="1", batchId="BATCH", state="COMPLETED")
    sdk.create_record.return_value = job
    sdk.poll_job.return_value = completed_job
    sdk.get_sites.return_value = [
        Site(studyKey="S", siteId=1, siteName="SITE", siteEnrollmentStatus="Active")
    ]
    wf = RegisterSubjectsWorkflow(sdk)
    req = RegisterSubjectRequest(formKey="F", siteName="SITE")

    result = wf.register_subjects(
        "STUDY", [req], wait_for_completion=True, timeout=10, poll_interval=1
    )

    sdk.create_record.assert_called_once()
    sdk.poll_job.assert_called_once_with("STUDY", "BATCH", timeout=10, interval=1)
    assert result == completed_job


def test_register_subjects_missing_site() -> None:
    """Test that register subjects missing site."""
    sdk = MagicMock()
    sdk.get_sites.return_value = []
    wf = RegisterSubjectsWorkflow(sdk)
    req = RegisterSubjectRequest(formKey="F", siteName="SITE")

    with pytest.raises(ValueError, match="site 'SITE' not found"):
        wf.register_subjects("STUDY", [req])


def test_register_subjects_missing_site_name() -> None:
    """Test that register subjects missing site name."""
    sdk = MagicMock()
    sdk.get_sites.return_value = [
        Site(studyKey="S", siteId=1, siteName="SITE", siteEnrollmentStatus="Active")
    ]
    wf = RegisterSubjectsWorkflow(sdk)
    req = RegisterSubjectRequest(formKey="F", siteName="")

    with pytest.raises(ValueError, match="siteName is required"):
        wf.register_subjects("STUDY", [req])


def test_register_subjects_accepts_enrollment_open_and_rejects_closed_site() -> None:
    """RegisterSubjectsWorkflow accepts 'Enrollment Open' sites and rejects closed sites."""
    sdk = MagicMock()
    job = Job(jobId="1", batchId="B1", state="PROCESSING")
    sdk.create_record.return_value = job
    sdk.get_sites.return_value = [
        Site(
            studyKey="STUDY", siteId=1, siteName="OPEN_SITE", siteEnrollmentStatus="Enrollment Open"
        ),
        Site(
            studyKey="STUDY",
            siteId=2,
            siteName="CLOSED_SITE",
            siteEnrollmentStatus="Enrollment Closed",
        ),
    ]
    sdk.subjects.get.return_value = []
    wf = RegisterSubjectsWorkflow(sdk)

    result = wf.register_subjects(
        "STUDY", [RegisterSubjectRequest(formKey="F", siteName="OPEN_SITE", subjectKey="SUBJ-1")]
    )
    assert result == job

    with pytest.raises(ValueError, match="is not eligible for enrollment"):
        wf.register_subjects(
            "STUDY",
            [RegisterSubjectRequest(formKey="F", siteName="CLOSED_SITE", subjectKey="SUBJ-2")],
        )


def test_register_subjects_skips_soft_deleted_existing_subject_and_filters_active_duplicate() -> (
    None
):
    """Soft-deleted existing subjects do not block re-registration; active duplicates are skipped."""
    from imednet.models.subjects import Subject
    from imednet_workflows.uat.inspector import StudySnapshot

    sdk = MagicMock()
    job = Job(jobId="1", batchId="B2", state="PROCESSING")
    sdk.create_record.return_value = job
    open_site = Site(
        studyKey="STUDY", siteId=1, siteName="OPEN_SITE", siteEnrollmentStatus="Enrollment Open"
    )
    closed_site = Site(
        studyKey="STUDY", siteId=2, siteName="CLOSED_SITE", siteEnrollmentStatus="Enrollment Closed"
    )
    sdk.get_sites.return_value = [open_site, closed_site]

    deleted_subj = Subject(
        studyKey="STUDY", subjectKey="SUBJ-1", deleted=True, subjectStatus="Enrolled"
    )
    sdk.get_subjects.return_value = [deleted_subj]

    wf = RegisterSubjectsWorkflow(sdk)
    summary_deleted = wf.register_subjects_batch(
        "STUDY",
        [{"formKey": "F", "siteName": "OPEN_SITE", "subjectKey": "SUBJ-1"}],
        skip_existing=True,
    )
    assert summary_deleted.skipped_count == 0
    assert sdk.create_record.call_count == 1

    active_subj = Subject(
        studyKey="STUDY", subjectKey="SUBJ-1", deleted=False, subjectStatus="Enrolled"
    )
    sdk.get_subjects.return_value = [active_subj]
    summary_active = wf.register_subjects_batch(
        "STUDY",
        [{"formKey": "F", "siteName": "OPEN_SITE", "subjectKey": "SUBJ-1"}],
        skip_existing=True,
    )
    assert summary_active.skipped_count == 1
    assert sdk.create_record.call_count == 1

    snapshot = StudySnapshot(
        study_key="STUDY",
        sites=[open_site, closed_site],
        forms=[],
        intervals=[],
        variables=[],
        subjects=[deleted_subj, active_subj],
    )
    assert snapshot.active_sites() == [open_site]


@pytest.mark.asyncio
async def test_register_subjects_single_job_guards_and_empty_batch_id_when_waiting() -> None:
    """AC 2 & AC 3: register_subjects guards single-Job inputs and enforces non-empty batchId on wait."""
    from unittest.mock import AsyncMock

    from imednet.datadict import DataDictionary, Form
    from imednet.datadict import Variable as DDVariable
    from imednet.errors import ValidationError
    from imednet.utils.batch_coordinator import BatchSubmissionSummary

    dd = DataDictionary(
        forms=[
            Form(
                formKey="ENROLL",
                formName="Enrollment",
                variables=[DDVariable(variableName="AGE", variableType="Integer", required="Yes")],
            )
        ]
    )
    active_site = Site(studyKey="STUDY", siteId=1, siteName="SITE", siteEnrollmentStatus="Active")

    sdk = MagicMock()
    del sdk.async_create_record
    sdk.get_sites.return_value = [active_site]
    sdk.create_record.return_value = Job(jobId="1", batchId="", state="OPEN")
    wf = RegisterSubjectsWorkflow(sdk, batch_size=2)

    # AC 2: multi-chunk, dry_run=True, empty, and invalid DD raise ValidationError
    with pytest.raises(ValidationError, match="register_subjects_batch"):
        wf.register_subjects(
            "STUDY",
            [
                {"formKey": "ENROLL", "siteName": "SITE", "data": {"AGE": 20}},
                {"formKey": "ENROLL", "siteName": "SITE", "data": {"AGE": 21}},
                {"formKey": "ENROLL", "siteName": "SITE", "data": {"AGE": 22}},
            ],
        )
    with pytest.raises(ValidationError, match="dry_run=True"):
        wf.register_subjects(
            "STUDY",
            [{"formKey": "ENROLL", "siteName": "SITE", "data": {"AGE": 20}}],
            dry_run=True,
        )
    with pytest.raises(ValidationError, match="at least one record"):
        wf.register_subjects("STUDY", [])
    with pytest.raises(ValidationError):
        wf.register_subjects(
            "STUDY",
            [{"formKey": "ENROLL", "siteName": "SITE", "data": {"AGE": "bad"}}],
            schema=dd,
            collect_errors=True,
        )
    sdk.create_record.assert_not_called()

    # AC 3: empty batchId with wait_for_completion=True raises error instead of all_successful=True
    with pytest.raises(ValueError, match="no batch_id received"):
        wf.register_subjects_batch(
            "STUDY",
            [{"formKey": "ENROLL", "siteName": "SITE", "data": {"AGE": 30}}],
            wait_for_completion=True,
        )
    with pytest.raises(ValueError, match="no batch_id received"):
        wf.register_subjects(
            "STUDY",
            [{"formKey": "ENROLL", "siteName": "SITE", "data": {"AGE": 30}}],
            wait_for_completion=True,
        )

    # Even if a BatchSubmissionSummary is constructed with wait_for_completion=True on an OPEN Job,
    # all_successful is False because polling never settled the OPEN Job.
    unpolled_summary = BatchSubmissionSummary(
        study_key="STUDY",
        jobs=[Job(jobId="1", batchId="", state="OPEN")],
        chunk_sizes=[1],
        total_records=1,
        wait_for_completion=True,
    )
    assert unpolled_summary.all_successful is False

    # Async parity for AC 2 & AC 3
    async_sdk = MagicMock()
    async_sdk.async_get_sites = AsyncMock(return_value=[active_site])
    async_sdk.async_create_record = AsyncMock(return_value=Job(jobId="2", batchId="", state="OPEN"))
    async_wf = RegisterSubjectsWorkflow(async_sdk, batch_size=2)

    with pytest.raises(ValidationError, match="dry_run=True"):
        await async_wf.async_register_subjects(
            "STUDY",
            [{"formKey": "ENROLL", "siteName": "SITE", "data": {"AGE": 30}}],
            dry_run=True,
        )
    with pytest.raises(ValueError, match="no batch_id received"):
        await async_wf.async_register_subjects_batch(
            "STUDY",
            [{"formKey": "ENROLL", "siteName": "SITE", "data": {"AGE": 30}}],
            wait_for_completion=True,
        )


@pytest.mark.asyncio
async def test_register_subjects_workflow_offline_dry_run_skips_sites_and_subjects_reads() -> None:
    """Req 4: RegisterSubjectsWorkflow with dry_run=True and offline DataDictionary makes zero HTTP reads."""
    from unittest.mock import AsyncMock

    from imednet.datadict import DataDictionary

    dd = DataDictionary.model_validate(
        {
            "forms": [
                {
                    "formKey": "F_ENROLL",
                    "variables": [{"variableName": "V_AGE", "variableType": "integer"}],
                }
            ]
        }
    )

    # Sync workflow
    sync_sdk = MagicMock()
    del sync_sdk.async_create_record
    sync_wf = RegisterSubjectsWorkflow(sync_sdk, filter_enrolled=True)
    summary = sync_wf.register_subjects_batch(
        "STUDY",
        [{"formKey": "F_ENROLL", "siteName": "SITE", "subjectKey": "SUB-1", "data": {"V_AGE": 30}}],
        skip_existing=True,
        schema=dd,
        dry_run=True,
        collect_errors=True,
    )
    assert summary.dry_run is True
    assert summary.validation_report is not None
    assert summary.validation_report.is_valid is True
    sync_sdk.get_sites.assert_not_called()
    sync_sdk.get_subjects.assert_not_called()
    sync_sdk.create_record.assert_not_called()

    # Async workflow
    async_sdk = MagicMock()
    async_sdk.async_create_record = AsyncMock()
    async_sdk.async_get_sites = AsyncMock()
    async_sdk.async_get_subjects = AsyncMock()
    async_wf = RegisterSubjectsWorkflow(async_sdk, filter_enrolled=True)
    async_summary = await async_wf.async_register_subjects_batch(
        "STUDY",
        [{"formKey": "F_ENROLL", "siteId": 101, "subjectKey": "SUB-2", "data": {"V_AGE": 44}}],
        skip_existing=True,
        schema=dd,
        dry_run=True,
        collect_errors=True,
    )
    assert async_summary.dry_run is True
    assert async_summary.validation_report is not None
    assert async_summary.validation_report.is_valid is True
    async_sdk.async_get_sites.assert_not_called()
    async_sdk.async_get_subjects.assert_not_called()
    async_sdk.async_create_record.assert_not_called()


@pytest.mark.asyncio
async def test_register_subjects_preserves_form_id_without_schema_and_rejects_missing_form_target() -> (
    None
):
    """AC 5: RegisterSubjectsWorkflow preserves formId when schema=None and rejects missing formKey+formId."""
    from unittest.mock import AsyncMock

    from imednet.models.records import RegisterSubjectRequest

    active_site = Site(studyKey="STUDY", siteId=10, siteName="SITE", siteEnrollmentStatus="Active")

    sync_sdk = MagicMock()
    del sync_sdk.async_create_record
    sync_sdk.get_sites.return_value = [active_site]
    sync_sdk.create_record.return_value = Job(jobId="1", batchId="B-1", state="COMPLETED")
    sync_wf = RegisterSubjectsWorkflow(sync_sdk)

    sync_wf.register_subjects(
        "STUDY",
        [
            {"formId": 301, "siteId": 10, "subjectOid": "OID-1", "data": {"V_AGE": 31}},
            RegisterSubjectRequest(formId=301, siteName="SITE", data={"V_AGE": 32}),
        ],
    )
    sent_sync = sync_sdk.create_record.call_args.kwargs["records_data"]
    assert sent_sync == [
        {"formId": 301, "siteId": 10, "subjectOid": "OID-1", "data": {"V_AGE": 31}},
        {"formId": 301, "siteName": "SITE", "data": {"V_AGE": 32}},
    ]
    assert all("formKey" not in item for item in sent_sync)

    with pytest.raises(ValueError, match="formKey.*formId"):
        sync_wf.register_subjects("STUDY", [{"siteName": "SITE", "data": {"V_AGE": 30}}])
    with pytest.raises(ValueError, match="formKey.*formId"):
        sync_wf.register_subjects("STUDY", [RegisterSubjectRequest(siteName="SITE")])

    async_sdk = MagicMock()
    async_sdk.async_get_sites = AsyncMock(return_value=[active_site])
    async_sdk.async_create_record = AsyncMock(
        return_value=Job(jobId="2", batchId="B-2", state="COMPLETED")
    )
    async_wf = RegisterSubjectsWorkflow(async_sdk)
    await async_wf.async_register_subjects(
        "STUDY",
        [{"formId": 301, "siteName": "SITE", "data": {"V_AGE": 40}}],
    )
    sent_async = async_sdk.async_create_record.call_args.kwargs["records_data"]
    assert sent_async == [{"formId": 301, "siteName": "SITE", "data": {"V_AGE": 40}}]

    with pytest.raises(ValueError, match="formKey.*formId"):
        await async_wf.async_register_subjects("STUDY", [{"siteId": 10, "data": {}}])
