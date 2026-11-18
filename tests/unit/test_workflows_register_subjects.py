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


def test_register_subjects_with_site_id() -> None:
    """Test subject registration with formId and siteId identifier pairs."""
    sdk = MagicMock()
    job = Job(jobId="1", batchId="1", state="CREATED")
    sdk.create_record.return_value = job
    sdk.get_sites.return_value = [
        Site(studyKey="S", siteId=101, siteName="SITE", siteEnrollmentStatus="Active")
    ]
    wf = RegisterSubjectsWorkflow(sdk)
    req = RegisterSubjectRequest(formId=10, siteId=101)
    assert wf.register_subjects("STUDY", [req]) == job

    bad_req = RegisterSubjectRequest(formId=10, siteId=999)
    with pytest.raises(ValueError, match="siteId '999' not found"):
        wf.register_subjects("STUDY", [bad_req])
