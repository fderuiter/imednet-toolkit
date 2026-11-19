"""Unit tests for workflows record update."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from imednet import ImednetSDK
from imednet.datadict import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    parse_logic_xml,
)
from imednet.datadict import (
    Form as DDForm,
)
from imednet.datadict import (
    Variable as DDVariable,
)
from imednet.errors import ValidationError
from imednet.models.jobs import Job
from imednet.models.variables import Variable
from imednet_workflows.record_update import RecordUpdateWorkflow


def test_create_or_update_records_no_wait() -> None:
    """Test that create or update records no wait."""
    sdk = MagicMock()
    del sdk.async_create_record
    job = Job(jobId="1", batchId="1", state="PROCESSING")
    sdk.create_record.return_value = job

    wf = RecordUpdateWorkflow(sdk)
    result = wf.create_or_update_records("STUDY", [{"a": 1}])

    sdk.create_record.assert_called_once_with("STUDY", [{"a": 1}], schema=wf._schema)
    assert result == job


def test_create_or_update_records_wait_for_completion(monkeypatch) -> None:
    """Test that create or update records wait for completion."""
    sdk = MagicMock()
    del sdk.async_create_record
    initial_job = Job(jobId="1", batchId="1", state="PROCESSING")
    completed_job = Job(jobId="1", batchId="1", state="COMPLETED")
    sdk.create_record.return_value = initial_job
    sdk.poll_job.return_value = completed_job

    wf = RecordUpdateWorkflow(sdk)
    # patch sleep to avoid delay
    monkeypatch.setattr("time.sleep", lambda *args: None)
    result = wf.create_or_update_records(
        "STUDY",
        [{"a": 1}],
        wait_for_completion=True,
        poll_interval=0,
        timeout=5,
    )

    sdk.create_record.assert_called_once_with("STUDY", [{"a": 1}], schema=wf._schema)
    sdk.poll_job.assert_called_with("STUDY", "1", timeout=5, interval=0)
    assert result.state == "COMPLETED"


def test_update_scheduled_record_builds_payload() -> None:
    """Test that update scheduled record builds payload."""
    sdk = MagicMock()
    del sdk.async_create_record
    wf = RecordUpdateWorkflow(sdk)
    wf.create_or_update_records = MagicMock(return_value="job")  # type: ignore[method-assign]  # type: ignore[method-assign]

    result = wf.update_scheduled_record(
        "STUDY",
        form_identifier="F1",
        subject_identifier="SUBJ1",
        interval_identifier="INT1",
        data={"x": 1},
        subject_identifier_type="oid",
    )

    wf.create_or_update_records.assert_called_once()
    called = wf.create_or_update_records.call_args.kwargs
    assert called["records_data"] == [
        {
            "formKey": "F1",
            "subjectOid": "SUBJ1",
            "intervalName": "INT1",
            "data": {"x": 1},
        }
    ]
    assert result == "job"


def test_create_or_update_records_validation() -> None:
    """Test that create or update records validation."""
    sdk = MagicMock()
    del sdk.async_create_record
    var = Variable(
        studyKey="STUDY",
        variableId=1,
        sequence=1,
        revision=1,
        disabled=False,
        variableOid="OID1",
        deleted=False,
        formName="F1",
        label="Label",
        blinded=False,
        variableName="age",
        variableType="integer",
        formId=1,
        formKey="F1",
    )
    sdk.get_variables.return_value = [var]
    wf = RecordUpdateWorkflow(sdk)

    with pytest.raises(ValidationError):
        wf.create_or_update_records("STUDY", [{"formKey": "F1", "data": {"bad": 1}}])
    sdk.records.create.assert_not_called()

    sdk.create_record.return_value = Job(jobId="1", batchId="1", state="PROCESSING")
    wf.create_or_update_records("STUDY", [{"formKey": "F1", "data": {"age": 5}}])
    sdk.get_variables.assert_called_once_with(study_key="STUDY")
    sdk.create_record.assert_called_once_with(
        "STUDY", [{"formKey": "F1", "data": {"age": 5}}], schema=wf._schema
    )


def test_register_subject_builds_payload() -> None:
    """Test that register subject builds payload."""
    sdk = MagicMock()
    # Mock _async_client to prevent SchemaValidator from being async if not intended
    del sdk.async_create_record

    wf = RecordUpdateWorkflow(sdk)
    wf.create_or_update_records = MagicMock(return_value="job")  # type: ignore[method-assign]

    result = wf.register_subject(
        "STUDY",
        form_identifier="F1",
        site_identifier="SITE1",
        data={"x": 1},
        site_identifier_type="name",  # Default
    )

    wf.create_or_update_records.assert_called_once()
    call_kwargs = wf.create_or_update_records.call_args.kwargs
    assert call_kwargs["records_data"] == [
        {
            "formKey": "F1",
            "siteName": "SITE1",
            "data": {"x": 1},
        }
    ]
    assert result == "job"


def test_register_subject_with_site_id() -> None:
    """Test that register subject with site id."""
    sdk = MagicMock()
    del sdk.async_create_record
    wf = RecordUpdateWorkflow(sdk)
    wf.create_or_update_records = MagicMock(return_value="job")  # type: ignore[method-assign]

    result = wf.register_subject(
        "STUDY",
        form_identifier="F1",
        site_identifier=123,
        data={"x": 1},
        site_identifier_type="id",
    )

    wf.create_or_update_records.assert_called_once()
    call_kwargs = wf.create_or_update_records.call_args.kwargs
    assert call_kwargs["records_data"] == [
        {
            "formKey": "F1",
            "siteId": 123,
            "data": {"x": 1},
        }
    ]
    assert result == "job"


def test_create_new_record_builds_payload() -> None:
    """Test that create new record builds payload."""
    sdk = MagicMock()
    del sdk.async_create_record
    wf = RecordUpdateWorkflow(sdk)
    wf.create_or_update_records = MagicMock(return_value="job")  # type: ignore[method-assign]

    result = wf.create_new_record(
        "STUDY",
        form_identifier="F1",
        subject_identifier="SUBJ1",
        data={"x": 1},
        subject_identifier_type="key",  # Default
    )

    wf.create_or_update_records.assert_called_once()
    call_kwargs = wf.create_or_update_records.call_args.kwargs
    assert call_kwargs["records_data"] == [
        {
            "formKey": "F1",
            "subjectKey": "SUBJ1",
            "data": {"x": 1},
        }
    ]
    assert result == "job"


def test_create_new_record_with_subject_oid() -> None:
    """Test that create new record with subject oid."""
    sdk = MagicMock()
    del sdk.async_create_record
    wf = RecordUpdateWorkflow(sdk)
    wf.create_or_update_records = MagicMock(return_value="job")  # type: ignore[method-assign]

    result = wf.create_new_record(
        "STUDY",
        form_identifier="F1",
        subject_identifier="OID1",
        data={"x": 1},
        subject_identifier_type="oid",
    )

    wf.create_or_update_records.assert_called_once()
    call_kwargs = wf.create_or_update_records.call_args.kwargs
    assert call_kwargs["records_data"] == [
        {
            "formKey": "F1",
            "subjectOid": "OID1",
            "data": {"x": 1},
        }
    ]
    assert result == "job"


def test_invalid_subject_identifier_type_raises_keyerror() -> None:
    """Test that invalid subject identifier type raises keyerror."""
    sdk = MagicMock()
    del sdk.async_create_record
    wf = RecordUpdateWorkflow(sdk)

    with pytest.raises(KeyError):
        wf.create_new_record(
            "STUDY",
            form_identifier="F1",
            subject_identifier="SUBJ1",
            data={"x": 1},
            subject_identifier_type="invalid_type",  # type: ignore
        )


def test_invalid_site_identifier_type_raises_keyerror() -> None:
    """Test that invalid site identifier type raises keyerror."""
    sdk = MagicMock()
    del sdk.async_create_record
    wf = RecordUpdateWorkflow(sdk)

    with pytest.raises(KeyError):
        wf.register_subject(
            "STUDY",
            form_identifier="F1",
            site_identifier="SITE1",
            data={"x": 1},
            site_identifier_type="invalid_type",  # type: ignore
        )


def test_update_scheduled_record_invalid_interval_identifier_type() -> None:
    """Test that update scheduled record invalid interval identifier type."""
    sdk = MagicMock()
    del sdk.async_create_record
    wf = RecordUpdateWorkflow(sdk)

    with pytest.raises(KeyError):
        wf.update_scheduled_record(
            "STUDY",
            form_identifier="F1",
            subject_identifier="SUBJ1",
            interval_identifier="INT1",
            data={"x": 1},
            interval_identifier_type="invalid_type",  # type: ignore
        )


def test_create_or_update_records_wait_for_completion_no_batch_id() -> None:
    """Test that create or update records wait for completion no batch id."""
    sdk = MagicMock()
    del sdk.async_create_record

    # Return a job with no batch_id
    job = Job(jobId="1", batchId="", state="PROCESSING")
    sdk.create_record.return_value = job

    # Provide variable so form validation passes
    var = Variable(
        studyKey="STUDY",
        variableId=1,
        sequence=1,
        revision=1,
        disabled=False,
        variableOid="OID1",
        deleted=False,
        formName="F1",
        label="Label",
        blinded=False,
        variableName="a",
        variableType="integer",
        formId=1,
        formKey="F1",
    )
    sdk.get_variables.return_value = [var]

    wf = RecordUpdateWorkflow(sdk)

    with pytest.raises(ValueError, match="Submission successful but no batch_id received."):
        wf.create_or_update_records(
            "STUDY",
            [{"formKey": "F1", "data": {"a": 1}}],
            wait_for_completion=True,
        )


def test_create_or_update_records_form_key_not_found() -> None:
    """Test that create or update records form key not found."""
    sdk = MagicMock()
    del sdk.async_create_record

    # Return empty list for variables so the form key is not found even after refresh
    sdk.get_variables.return_value = []

    wf = RecordUpdateWorkflow(sdk)

    with pytest.raises(ValueError, match="Form key 'UNKNOWN_FORM' not found"):
        wf.create_or_update_records(
            "STUDY",
            [{"formKey": "UNKNOWN_FORM", "data": {"a": 1}}],
        )


@pytest.mark.asyncio
async def test_async_create_or_update_records_no_wait() -> None:
    """Test that async create or update records no wait asynchronously."""
    sdk = MagicMock()
    sdk._async_client = True
    job = Job(jobId="1", batchId="1", state="PROCESSING")
    sdk.async_create_record = AsyncMock(return_value=job)

    wf = RecordUpdateWorkflow(sdk)
    result = await wf.async_create_or_update_records("STUDY", [{"a": 1}])

    sdk.async_create_record.assert_called_once_with("STUDY", [{"a": 1}], schema=wf._schema)
    assert result == job


@pytest.mark.asyncio
async def test_async_create_or_update_records_wait_for_completion(monkeypatch) -> None:
    """Test that async create or update records wait for completion asynchronously."""
    sdk = MagicMock()
    sdk._async_client = True
    initial_job = Job(jobId="1", batchId="1", state="PROCESSING")
    completed_job = Job(jobId="1", batchId="1", state="COMPLETED")
    sdk.async_create_record = AsyncMock(return_value=initial_job)
    sdk.async_poll_job = AsyncMock(return_value=completed_job)

    wf = RecordUpdateWorkflow(sdk)
    import anyio

    monkeypatch.setattr(anyio, "sleep", AsyncMock())
    result = await wf.async_create_or_update_records(
        "STUDY",
        [{"a": 1}],
        wait_for_completion=True,
        poll_interval=0,
        timeout=5,
    )

    sdk.async_create_record.assert_called_once_with("STUDY", [{"a": 1}], schema=wf._schema)
    sdk.async_poll_job.assert_called_with("STUDY", "1", timeout=5, interval=0)
    assert result.state == "COMPLETED"


@pytest.mark.asyncio
async def test_async_create_or_update_records_validation() -> None:
    """Test that async create or update records validation asynchronously."""
    sdk = MagicMock()
    sdk._async_client = True
    var = Variable(
        studyKey="STUDY",
        variableId=1,
        sequence=1,
        revision=1,
        disabled=False,
        variableOid="OID1",
        deleted=False,
        formName="F1",
        label="Label",
        blinded=False,
        variableName="age",
        variableType="integer",
        formId=1,
        formKey="F1",
    )
    sdk.async_get_variables = AsyncMock(return_value=[var])
    sdk.async_create_record = AsyncMock()

    wf = RecordUpdateWorkflow(sdk)

    with pytest.raises(ValidationError):
        await wf.async_create_or_update_records("STUDY", [{"formKey": "F1", "data": {"bad": 1}}])
    sdk.async_create_record.assert_not_called()

    sdk.async_create_record = AsyncMock(
        return_value=Job(jobId="1", batchId="1", state="PROCESSING")
    )
    await wf.async_create_or_update_records("STUDY", [{"formKey": "F1", "data": {"age": 5}}])
    sdk.async_get_variables.assert_called_once_with(study_key="STUDY")
    sdk.async_create_record.assert_called_once_with(
        "STUDY", [{"formKey": "F1", "data": {"age": 5}}], schema=wf._schema
    )


@pytest.mark.asyncio
async def test_async_create_or_update_records_wait_for_completion_no_batch_id() -> None:
    """Test that async create or update records wait for completion no batch id asynchronously."""
    sdk = MagicMock()
    sdk._async_client = True
    job = Job(jobId="1", batchId="", state="PROCESSING")
    sdk.async_create_record = AsyncMock(return_value=job)
    var = Variable(
        studyKey="STUDY",
        variableId=1,
        sequence=1,
        revision=1,
        disabled=False,
        variableOid="OID1",
        deleted=False,
        formName="F1",
        label="Label",
        blinded=False,
        variableName="a",
        variableType="integer",
        formId=1,
        formKey="F1",
    )
    sdk.async_get_variables = AsyncMock(return_value=[var])

    wf = RecordUpdateWorkflow(sdk)

    with pytest.raises(ValueError, match="Submission successful but no batch_id received."):
        await wf.async_create_or_update_records(
            "STUDY",
            [{"formKey": "F1", "data": {"a": 1}}],
            wait_for_completion=True,
        )


@pytest.mark.asyncio
async def test_async_create_or_update_records_form_key_not_found() -> None:
    """Test that async create or update records form key not found asynchronously."""
    sdk = MagicMock()
    sdk._async_client = True
    sdk.async_get_variables = AsyncMock(return_value=[])

    wf = RecordUpdateWorkflow(sdk)

    with pytest.raises(ValueError, match="Form key 'UNKNOWN_FORM' not found"):
        await wf.async_create_or_update_records(
            "STUDY",
            [{"formKey": "UNKNOWN_FORM", "data": {"a": 1}}],
        )


def _sample_workflow_data_dictionary() -> DataDictionary:
    return DataDictionary(
        forms=[
            DDForm(
                formKey="F_DM",
                formId="301",
                variables=[
                    DDVariable(variableName="SUBJID", variableType="Text", required="Yes"),
                    DDVariable(variableName="AGE", variableType="Integer", required="Yes"),
                    DDVariable(
                        variableName="SEX",
                        variableType="Radio",
                        required="Yes",
                        choices=[
                            Choice(choiceText="Male", choiceValue="M"),
                            Choice(choiceText="Female", choiceValue="F"),
                        ],
                    ),
                ],
                businessLogic=[
                    BusinessLogicRule(
                        name="Adult Rule",
                        type="Form",
                        status="Active",
                        form="F_DM",
                        logic=parse_logic_xml(
                            "<rule><if><lt><variable>AGE</variable><value>18</value></lt></if>"
                            "<then><message>Subject must be at least 18</message></then></rule>"
                        ),
                    )
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_record_update_workflow_with_datadictionary_sync_and_async() -> None:
    """RecordUpdateWorkflow validates offline against DataDictionary without API calls."""
    dd = _sample_workflow_data_dictionary()

    # Sync constructor schema
    sdk = MagicMock()
    del sdk.async_create_record
    sdk.create_record.return_value = Job(jobId="1", batchId="B-SYNC", state="COMPLETED")
    wf = RecordUpdateWorkflow(sdk, schema=dd)

    with pytest.raises(ValidationError, match="Subject must be at least 18"):
        wf.create_or_update_records(
            "STUDY",
            [{"formKey": "F_DM", "data": {"SUBJID": "S1", "AGE": 15, "SEX": "F"}}],
        )
    sdk.get_variables.assert_not_called()
    sdk.create_record.assert_not_called()

    with pytest.raises(ValidationError, match="Unknown form ID 999"):
        wf.create_or_update_records(
            "STUDY",
            [{"formId": 999, "data": {"SUBJID": "S1", "AGE": 30, "SEX": "F"}}],
        )

    res = wf.create_new_record(
        "STUDY",
        form_identifier=301,
        subject_identifier="SUBJ1",
        data={"SUBJID": "S1", "AGE": 30, "SEX": "F"},
        form_identifier_type="id",
    )
    assert res.batch_id == "B-SYNC"
    sdk.get_variables.assert_not_called()
    sdk.create_record.assert_called_once()

    # Async per-method schema override
    async_sdk = MagicMock()
    async_sdk._async_client = True
    async_sdk.async_create_record = AsyncMock(
        return_value=Job(jobId="2", batchId="B-ASYNC", state="COMPLETED")
    )
    async_wf = RecordUpdateWorkflow(async_sdk)

    with pytest.raises(ValidationError, match="Invalid choice"):
        await async_wf.async_create_or_update_records(
            "STUDY",
            [{"formKey": "F_DM", "data": {"SUBJID": "S1", "AGE": 25, "SEX": "BAD"}}],
            schema=dd,
        )
    async_sdk.async_get_variables.assert_not_called()

    async_res = await async_wf.async_create_or_update_records(
        "STUDY",
        [{"formId": 301, "subjectKey": "SUBJ1", "data": {"SUBJID": "S1", "AGE": 25, "SEX": "M"}}],
        schema=dd,
    )
    assert async_res.batch_id == "B-ASYNC"
    async_sdk.async_get_variables.assert_not_called()


@pytest.mark.asyncio
async def test_record_update_workflow_cold_cache_form_id_refresh_and_unknown_form_id() -> None:
    """Cold SchemaCache refreshes on formId records and rejects unknown formId."""
    var = Variable(
        studyKey="STUDY",
        variableId=1,
        sequence=1,
        revision=1,
        disabled=False,
        variableOid="OID1",
        deleted=False,
        formName="F1",
        label="Label",
        blinded=False,
        variableName="age",
        variableType="integer",
        formId=101,
        formKey="F1",
    )

    sdk = MagicMock()
    del sdk.async_create_record
    sdk.get_variables.return_value = [var]
    sdk.create_record.return_value = Job(jobId="1", batchId="B1", state="PROCESSING")
    wf = RecordUpdateWorkflow(sdk)

    # Cold cache refreshes on formId=101 and validates data
    with pytest.raises(ValidationError, match="Value must be an integer"):
        wf.create_or_update_records("STUDY", [{"formId": 101, "data": {"age": True}}])
    sdk.get_variables.assert_called_once_with(study_key="STUDY")

    # Unknown formId raises ValidationError
    with pytest.raises(ValidationError, match="Unknown form ID 999"):
        wf.create_or_update_records("STUDY", [{"form_id": 999, "data": {"age": 30}}])

    # Async cold cache formId refresh and unknown formId
    async_sdk = MagicMock()
    async_sdk._async_client = True
    async_sdk.async_get_variables = AsyncMock(return_value=[var])
    async_sdk.async_create_record = AsyncMock(
        return_value=Job(jobId="2", batchId="B2", state="PROCESSING")
    )
    async_wf = RecordUpdateWorkflow(async_sdk)

    with pytest.raises(ValidationError, match="Unknown form ID 999"):
        await async_wf.async_create_or_update_records(
            "STUDY", [{"formId": 999, "data": {"age": 30}}]
        )
    async_sdk.async_get_variables.assert_called_once_with(study_key="STUDY")


def test_simulation_mode_f_dm_coded_submission_via_record_update_workflow() -> None:
    """RecordUpdateWorkflow submits simulated F_DM records with Coded V_GENDER and cold formId."""
    sdk = ImednetSDK(
        api_key="sim-key",  # pragma: allowlist secret
        security_key="sim-sec",  # pragma: allowlist secret
        simulation_mode=True,
    )
    try:
        wf_by_key = RecordUpdateWorkflow(sdk)
        job1 = wf_by_key.create_or_update_records(
            "STUDY",
            [
                {
                    "formKey": "F_DM",
                    "subjectKey": "SUBJ-001",
                    "data": {"V_AGE": 34, "V_GENDER": "F"},
                }
            ],
        )
        assert job1.batch_id

        # Cold cache using formId=301 from simulation/state.py
        wf_by_id = RecordUpdateWorkflow(sdk)
        job2 = wf_by_id.create_or_update_records(
            "STUDY",
            [
                {
                    "formId": 301,
                    "subjectKey": "SUBJ-001",
                    "data": {"V_AGE": 35, "V_GENDER": "M"},
                }
            ],
        )
        assert job2.batch_id

        with pytest.raises(ValidationError, match="Unknown form ID 9999"):
            wf_by_id.create_or_update_records(
                "STUDY",
                [{"formId": 9999, "subjectKey": "SUBJ-001", "data": {"V_AGE": 35}}],
            )
    finally:
        sdk.close()


@pytest.mark.asyncio
async def test_all_record_update_workflow_methods_accept_clinical_variable_types() -> None:
    """All 5 RecordUpdateWorkflow write methods (+ async variants) validate clinical Variable types."""
    clinical_variables = [
        Variable(variable_name="V_CODED", variable_type="Coded", form_id=301, form_key="F_CLIN"),
        Variable(variable_name="V_RADIO", variable_type="Radio", form_id=301, form_key="F_CLIN"),
        Variable(variable_name="V_DROP", variable_type="Dropdown", form_id=301, form_key="F_CLIN"),
        Variable(
            variable_name="V_SINGLE",
            variable_type="Single Select",
            form_id=301,
            form_key="F_CLIN",
        ),
        Variable(variable_name="V_YESNO", variable_type="Yes/No", form_id=301, form_key="F_CLIN"),
        Variable(
            variable_name="V_MULTI",
            variable_type="Multi Select",
            form_id=301,
            form_key="F_CLIN",
        ),
        Variable(variable_name="V_CHECK", variable_type="Checkbox", form_id=301, form_key="F_CLIN"),
        Variable(variable_name="V_MEMO", variable_type="Memo", form_id=301, form_key="F_CLIN"),
        Variable(
            variable_name="V_DTP",
            variable_type="Date/Time Precision",
            form_id=301,
            form_key="F_CLIN",
        ),
    ]
    clinical_data = {
        "V_CODED": 1,
        "V_RADIO": "OPT_A",
        "V_DROP": 2,
        "V_SINGLE": "CHOICE_1",
        "V_YESNO": 1,
        "V_MULTI": ["A", 2],
        "V_CHECK": True,
        "V_MEMO": "Patient tolerated procedure without complications.",
        "V_DTP": "2026-04",
    }

    sync_sdk = MagicMock()
    del sync_sdk.async_create_record
    sync_sdk.get_variables.return_value = clinical_variables
    sync_sdk.create_record.return_value = Job(jobId="10", batchId="B-CLIN", state="PROCESSING")

    wf = RecordUpdateWorkflow(sync_sdk)
    batch_summary = wf.create_or_update_records_batch(
        "STUDY", [{"formKey": "F_CLIN", "data": clinical_data}]
    )
    assert batch_summary.batch_ids == ["B-CLIN"]

    job_direct = wf.create_or_update_records(
        "STUDY", [{"formKey": "F_CLIN", "data": clinical_data}]
    )
    assert job_direct.batch_id == "B-CLIN"

    job_reg = wf.register_subject(
        "STUDY",
        form_identifier="F_CLIN",
        site_identifier="SITE-01",
        data=clinical_data,
    )
    assert job_reg.batch_id == "B-CLIN"

    job_sched = wf.update_scheduled_record(
        "STUDY",
        form_identifier="F_CLIN",
        subject_identifier="SUBJ-01",
        interval_identifier="Baseline",
        data=clinical_data,
    )
    assert job_sched.batch_id == "B-CLIN"

    job_new = wf.create_new_record(
        "STUDY",
        form_identifier="F_CLIN",
        subject_identifier="SUBJ-01",
        data=clinical_data,
    )
    assert job_new.batch_id == "B-CLIN"
    assert sync_sdk.create_record.call_count == 5

    async_sdk = MagicMock()
    async_sdk.async_get_variables = AsyncMock(return_value=clinical_variables)
    async_sdk.async_create_record = AsyncMock(
        return_value=Job(jobId="11", batchId="B-ASYNC-CLIN", state="PROCESSING")
    )
    async_wf = RecordUpdateWorkflow(async_sdk)

    async_summary = await async_wf.async_create_or_update_records_batch(
        "STUDY", [{"formKey": "F_CLIN", "data": clinical_data}]
    )
    assert async_summary.batch_ids == ["B-ASYNC-CLIN"]

    async_job = await async_wf.async_create_or_update_records(
        "STUDY", [{"formKey": "F_CLIN", "data": clinical_data}]
    )
    assert async_job.batch_id == "B-ASYNC-CLIN"
