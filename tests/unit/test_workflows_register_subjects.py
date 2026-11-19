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
