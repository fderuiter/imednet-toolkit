"""Unit tests for register subjects."""

from unittest.mock import MagicMock

import pytest

from imednet.models.jobs import Job
from imednet.models.records import RecordData, RegisterSubjectRequest
from imednet.models.sites import Site
from imednet.testing import fake_data
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow


def test_register_subjects_passes_records_correctly(schema) -> None:
    """Test that register subjects passes records correctly."""
    sdk = MagicMock()
    job = Job(jobId="1", batchId="1", state="PROCESSING")
    sdk.create_record.return_value = job
    sdk.get_sites.return_value = [
        Site(studyKey="S", siteId=1, siteName="SITE", siteEnrollmentStatus="Active")
    ]
    wf = RegisterSubjectsWorkflow(sdk)
    rec = fake_data.fake_record(schema)
    req = RegisterSubjectRequest(
        formKey=rec["formKey"], siteName="SITE", data=RecordData(rec["recordData"])
    )

    result = wf.register_subjects("STUDY", [req], email_notify="test@example.com")

    sdk.get_sites.assert_called_once_with(study_key="STUDY")
    sdk.subjects.get.assert_not_called()
    sdk.create_record.assert_called_once_with(
        study_key="STUDY",
        records_data=[req.model_dump(by_alias=True)],
        email_notify="test@example.com",
    )
    assert result == job


def test_register_subjects_validation_errors() -> None:
    """Test that register subjects validation errors."""
    sdk = MagicMock()
    sdk.get_sites.return_value = [
        Site(studyKey="S", siteId=1, siteName="VALID_SITE", siteEnrollmentStatus="Active")
    ]
    wf = RegisterSubjectsWorkflow(sdk)

    subjects = [
        RegisterSubjectRequest(formKey="F1", siteName=""),
        RegisterSubjectRequest(formKey="F2", siteName="INVALID_SITE"),
    ]

    with pytest.raises(ValueError) as excinfo:
        wf.register_subjects("STUDY", subjects)

    error_msg = str(excinfo.value)
    assert "Index 0: siteName is required" in error_msg
    assert "Index 1: site 'INVALID_SITE' not found" in error_msg

    sdk.create_record.assert_not_called()


@pytest.mark.asyncio
async def test_register_subject_request_omits_unset_keys_and_batch_rerun_skips_existing() -> None:
    """AC 4 & Req 5: Omit unset subjectKey/subjectOid and deduplicate explicit subjectKey reruns."""
    from imednet import AsyncImednetSDK, ImednetSDK
    from imednet.simulation import SimulationState, SimulationTransport

    unset_req = RegisterSubjectRequest(
        formKey="F_DM", siteName="SITE", data=RecordData({"AGE": 30})
    )
    dumped_unset = unset_req.model_dump(by_alias=True)
    assert "subjectKey" not in dumped_unset
    assert "subjectOid" not in dumped_unset

    explicit_req = RegisterSubjectRequest(
        formKey="F_DM",
        siteName="SITE",
        subjectKey="SUB-EXPL-1",
        subjectOid="OID-EXPL-1",
        data=RecordData({"V_AGE": 35}),
    )
    dumped_explicit = explicit_req.model_dump(by_alias=True)
    assert dumped_explicit["subjectKey"] == "SUB-EXPL-1"
    assert dumped_explicit["subjectOid"] == "OID-EXPL-1"

    state = SimulationState()
    batch_inputs = [
        RegisterSubjectRequest(
            formKey="F_DM",
            siteName="SITE",
            subjectKey="SUB-BATCH-1",
            data=RecordData({"V_AGE": 29}),
        ),
        {
            "formKey": "F_DM",
            "siteName": "SITE",
            "subjectKey": "SUB-BATCH-2",
            "data": {"V_AGE": 41},
        },
    ]

    with ImednetSDK(transport=SimulationTransport(state=state)) as sync_sdk:
        before = len(state.get_subjects("STUDY"))
        wf = RegisterSubjectsWorkflow(sync_sdk)
        first = wf.register_subjects_batch(
            "STUDY",
            batch_inputs,
            skip_existing=True,
            wait_for_completion=True,
            poll_interval=0,
        )
        assert first.total_records == 2
        assert first.skipped_count == 0
        assert len(state.get_subjects("STUDY")) == before + 2

        second = wf.register_subjects_batch(
            "STUDY",
            batch_inputs,
            skip_existing=True,
            wait_for_completion=True,
            poll_interval=0,
        )
        assert second.total_records == 0
        assert second.skipped_count == 2
        assert len(state.get_subjects("STUDY")) == before + 2

    async with AsyncImednetSDK(transport=SimulationTransport(state=state)) as async_sdk:
        awf = RegisterSubjectsWorkflow(async_sdk)
        third = await awf.async_register_subjects_batch(
            "STUDY",
            batch_inputs,
            skip_existing=True,
            wait_for_completion=True,
            poll_interval=0,
        )
        assert third.total_records == 0
        assert third.skipped_count == 2
        assert len(state.get_subjects("STUDY")) == before + 2
