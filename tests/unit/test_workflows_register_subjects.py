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
async def test_register_subjects_workflow_site_hook_collects_errors_in_dry_run(tmp_path) -> None:
    """Req 3 & 6: RegisterSubjectsWorkflow runs site checks via validate_batch_fn and applies defaults pre-coercion."""
    from unittest.mock import AsyncMock

    from imednet.datadict import DataDictionary, Form, Variable
    from imednet.utils.tabular import MASKED_SENTINEL

    dd = DataDictionary(
        forms=[
            Form(
                formKey="F_REG",
                variables=[
                    Variable(variableName="LOT_CODE", variableType="Text", required="Yes"),
                    Variable(variableName="AGE", variableType="Integer", required="Yes"),
                ],
            )
        ]
    )

    sites = [
        Site(studyKey="STUDY", siteId=10, siteName="OPEN_SITE", siteEnrollmentStatus="Active"),
        Site(
            studyKey="STUDY",
            siteId=20,
            siteName="CLOSED_SITE",
            siteEnrollmentStatus="Enrollment Closed",
        ),
    ]

    sdk = MagicMock()
    del sdk.async_create_record
    sdk.get_sites.return_value = sites
    sdk.create_record.return_value = Job(jobId="1", batchId="B1", state="PROCESSING")
    wf = RegisterSubjectsWorkflow(sdk)

    # CSV without form/site headers preserves LOT_CODE="12345" as str when defaults are passed
    csv_path = tmp_path / "enroll.csv"
    csv_path.write_text("LOT_CODE,AGE\n12345,35.0\n", encoding="utf-8")
    ok_summary = wf.register_subjects_batch(
        "STUDY",
        csv_path,
        default_form_key="F_REG",
        default_site_name="OPEN_SITE",
        schema=dd,
    )
    assert ok_summary.total_records == 1
    sent_records = sdk.create_record.call_args.kwargs["records_data"]
    assert sent_records[0]["data"] == {"LOT_CODE": "12345", "AGE": 35}
    assert isinstance(sent_records[0]["data"]["LOT_CODE"], str)

    # Dry-run with collect_errors aggregates site, identifier, masked, and schema errors
    bad_inputs = [
        {"formKey": "F_REG", "siteName": "MISSING_SITE", "data": {"LOT_CODE": "A", "AGE": "bad"}},
        {
            "formKey": "F_REG",
            "siteId": 20,
            "subjectId": "bad_id",
            "data": {"LOT_CODE": MASKED_SENTINEL, "AGE": 40},
        },
    ]
    dry_summary = wf.register_subjects_batch(
        "STUDY",
        bad_inputs,
        schema=dd,
        dry_run=True,
        collect_errors=True,
    )
    assert dry_summary.validation_report is not None
    assert dry_summary.validation_report.is_valid is False
    rule_ids = {err.rule_id for err in dry_summary.validation_report.errors}
    assert {
        "unknown_site",
        "ineligible_site",
        "invalid_identifier",
        "masked_phi",
        "type",
    } <= rule_ids

    # Async facade equivalent
    async_sdk = MagicMock()
    async_sdk.async_get_sites = AsyncMock(return_value=sites)
    async_sdk.async_create_record = AsyncMock()
    async_wf = RegisterSubjectsWorkflow(async_sdk)
    async_dry = await async_wf.async_register_subjects_batch(
        "STUDY",
        bad_inputs,
        schema=dd,
        dry_run=True,
        collect_errors=True,
    )
    assert async_dry.validation_report is not None
    assert async_dry.validation_report.to_dict()["errorCount"] == len(
        dry_summary.validation_report.errors
    )
    async_sdk.async_create_record.assert_not_called()

    # DataFrame with invalid site_id on row 0 and missing site on row 1 keeps row 0 invalid
    import pandas as pd

    df = pd.DataFrame(
        [
            {"site_id": "bad_site_id", "LOT_CODE": "111", "AGE": 25},
            {"site_id": None, "LOT_CODE": "222", "AGE": 26},
        ]
    )
    df_dry = wf.register_subjects_batch(
        "STUDY",
        df,
        default_form_key="F_REG",
        default_site_name="OPEN_SITE",
        schema=dd,
        dry_run=True,
        collect_errors=True,
    )
    assert df_dry.validation_report is not None
    assert df_dry.validation_report.invalid_records == 1
    assert df_dry.validation_report.valid_records == 1
    assert [e.rule_id for e in df_dry.validation_report.errors] == ["invalid_identifier"]
