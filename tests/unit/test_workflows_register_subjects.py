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
async def test_register_subjects_require_batch_id_and_raise_on_poll_failure_contracts() -> None:
    """RegisterSubjectsWorkflow sets require_batch_id=wait_for_completion and forwards raise_on_poll_failure."""
    from unittest.mock import AsyncMock

    from imednet.models.jobs import JobStatus
    from imednet.utils.job_poller import JobFailedError

    active_site = Site(studyKey="STUDY", siteId=1, siteName="SITE", siteEnrollmentStatus="Active")
    req = RegisterSubjectRequest(formKey="F", siteName="SITE", subjectKey="SUBJ-1")

    # 1. Sync: empty batchId is non-blocking when wait_for_completion=False, raises ValueError when True
    sync_sdk = MagicMock()
    del sync_sdk.async_create_record
    sync_sdk.get_sites.return_value = [active_site]
    sync_sdk.create_record.return_value = Job(jobId="1", batchId="", state="PROCESSING")

    wf = RegisterSubjectsWorkflow(sync_sdk)
    non_blocking = wf.register_subjects_batch("STUDY", [req], wait_for_completion=False)
    assert len(non_blocking.jobs) == 1
    assert non_blocking.batch_ids == []

    with pytest.raises(ValueError, match="no batch_id received"):
        wf.register_subjects_batch("STUDY", [req], wait_for_completion=True)

    # 2. Sync: raise_on_poll_failure=False returns summary/terminal job; True attaches summaries
    sync_sdk.create_record.return_value = Job(jobId="2", batchId="RB-FAIL", state="PROCESSING")
    sync_sdk.poll_job.side_effect = lambda *_a, **_kw: (_ for _ in ()).throw(
        JobFailedError("RB-FAIL", JobStatus(batchId="RB-FAIL", state="FAILED"))
    )
    suppressed = wf.register_subjects_batch(
        "STUDY",
        [req],
        wait_for_completion=True,
        raise_on_poll_failure=False,
    )
    assert suppressed.all_successful is False
    assert suppressed.jobs[0].state == "FAILED"

    terminal_job = wf.register_subjects(
        "STUDY",
        [req],
        wait_for_completion=True,
        raise_on_poll_failure=False,
    )
    assert terminal_job.state == "FAILED"

    with pytest.raises(JobFailedError) as exc_info:
        wf.register_subjects(
            "STUDY",
            [req],
            wait_for_completion=True,
            raise_on_poll_failure=True,
        )
    assert exc_info.value.batch_summary is not None
    assert exc_info.value.poll_summary is not None

    # 3. Async native + sync fallback (functools.partial): empty batchId & raise_on_poll_failure=False
    fallback_summary = await wf.async_register_subjects_batch(
        "STUDY",
        [req],
        wait_for_completion=True,
        raise_on_poll_failure=False,
    )
    assert fallback_summary.all_successful is False

    async_sdk = MagicMock()
    async_sdk.async_get_sites = AsyncMock(return_value=[active_site])
    async_sdk.async_create_record = AsyncMock(
        return_value=Job(jobId="3", batchId="", state="PROCESSING")
    )
    async_wf = RegisterSubjectsWorkflow(async_sdk)

    async_non_blocking = await async_wf.async_register_subjects_batch(
        "STUDY", [req], wait_for_completion=False
    )
    assert async_non_blocking.batch_ids == []

    with pytest.raises(ValueError, match="no batch_id received"):
        await async_wf.async_register_subjects_batch("STUDY", [req], wait_for_completion=True)

    async_sdk.async_create_record = AsyncMock(
        return_value=Job(jobId="4", batchId="ARB-FAIL", state="PROCESSING")
    )
    async_sdk.async_poll_job = AsyncMock(
        side_effect=lambda *_a, **_kw: (_ for _ in ()).throw(
            JobFailedError("ARB-FAIL", JobStatus(batchId="ARB-FAIL", state="FAILED"))
        )
    )
    async_job = await async_wf.async_register_subjects(
        "STUDY",
        [req],
        wait_for_completion=True,
        raise_on_poll_failure=False,
    )
    assert async_job.state == "FAILED"
