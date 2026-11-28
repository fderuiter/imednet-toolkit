"""Tests for expanded Record request subclasses and batch resultUrl resolution."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from imednet import AsyncImednetSDK, ImednetSDK, SimulationState, SimulationTransport
from imednet.cli import app as cli_app
from imednet.core.context import strict_mode_context
from imednet.errors import ValidationError
from imednet.models import UpdateRecordRequest as ModelsUpdateRecordRequest
from imednet.models.engine import _CANONICAL_MODEL_MODULES
from imednet.models.jobs import Job, JobStatus
from imednet.models.records import (
    BaseRecordRequest,
    CreateNewRecordRequest,
    RecordStatus,
    RegisterSubjectRequest,
    UpdateRecordRequest,
    UpdateScheduledRecordRequest,
)
from imednet.models.sites import Site
from imednet.models.variables import Variable
from imednet.spi.models import UpdateRecordRequest as SpiUpdateRecordRequest
from imednet.utils.job_poller import JobFailedError
from imednet.utils.pandas import import_records_csv
from imednet.utils.tabular import (
    _IDENTIFIER_COLUMNS,
    ensure_no_masked_sentinel,
    normalize_records_input,
)
from imednet.validation._base import _extract_form_ref
from imednet.validation.cache import AsyncSchemaValidator, SchemaCache, SchemaValidator
from imednet_workflows.record_update import AsyncRecordUpdateWorkflow, RecordUpdateWorkflow
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow
from imednet_workflows.uat.submission import (
    AsyncBulkRecordSubmissionWorkflow,
    BulkRecordSubmissionWorkflow,
)


def test_all_four_subclasses_preserve_all_14_post_record_attributes_and_omit_unset() -> None:
    """Serializing the four Record request subclasses preserves all 14 POST attributes and omits unset fields."""
    reg = RegisterSubjectRequest(
        formKey="DM",
        formId=10,
        siteName="Site Alpha",
        siteId=1,
        subjectKey="SUBJ-001",
        subjectId=101,
        subjectOid="OID-SUBJ-001",
        intervalName="Screening",
        intervalId=5,
        recordStatus=RecordStatus.RECORD_COMPLETE,
        reasonForChange="Initial enrollment",
        data={"AGE": 44},
    )
    sched = UpdateScheduledRecordRequest(
        form_key="VS",
        form_id=20,
        subject_key="SUBJ-001",
        subject_id=101,
        subject_oid="OID-SUBJ-001",
        interval_name="Visit 1",
        interval_id=11,
        record_status="Record Complete",
        reason_for_change="Scheduled entry",
        data={"SYSBP": 120},
    )
    unsched = CreateNewRecordRequest(
        formKey="AE",
        formId=30,
        subjectKey="SUBJ-001",
        subjectId=101,
        subjectOid="OID-SUBJ-001",
        intervalName="Unscheduled",
        intervalId=99,
        recordStatus=RecordStatus.COMPLETE,
        reasonForChange="Adverse event log",
        data={"AETERM": "Headache"},
    )
    upd = UpdateRecordRequest(
        formKey="VS",
        formId=20,
        siteName="Site Alpha",
        siteId=1,
        subjectKey="SUBJ-001",
        subjectId=101,
        subjectOid="OID-SUBJ-001",
        intervalName="Visit 1",
        intervalId=11,
        recordId=5001,
        recordOid="REC-OID-5001",
        visitId=77,
        recordStatus="Record Complete",
        reasonForChange="Audit correction",
        data={"SYSBP": 118},
    )

    serialized = normalize_records_input((reg, sched, unsched, upd))
    present_keys = set().union(*(item.keys() for item in serialized)) - {"data"}
    expected_camel = {col for col in _IDENTIFIER_COLUMNS if "_" not in col}
    assert expected_camel == present_keys
    assert len(expected_camel) == 14

    assert serialized[0]["recordStatus"] == "Record Complete"
    assert serialized[0]["reasonForChange"] == "Initial enrollment"
    assert serialized[2]["recordStatus"] == "Complete"
    assert serialized[3]["recordId"] == 5001
    assert serialized[3]["recordOid"] == "REC-OID-5001"
    assert serialized[3]["visitId"] == 77

    # Unset None and blank fallback strings ("") are omitted while data={} stays intact
    minimal_reg = RegisterSubjectRequest(formId=10, siteId=1)
    assert minimal_reg.model_dump(by_alias=True) == {"formId": 10, "siteId": 1, "data": {}}

    minimal_sched = UpdateScheduledRecordRequest(formId=20, subjectId=101, intervalId=11)
    assert minimal_sched.model_dump(by_alias=True) == {
        "formId": 20,
        "subjectId": 101,
        "intervalId": 11,
        "data": {},
    }

    minimal_upd = UpdateRecordRequest(recordId=999, data={"TEMP": 36.7})
    assert minimal_upd.model_dump(by_alias=True) == {"recordId": 999, "data": {"TEMP": 36.7}}


def test_strict_mode_context_accepts_all_subclasses_and_engine_registration() -> None:
    """Instantiating every BaseRecordRequest subclass with ID and audit fields passes strict_mode_context(True)."""
    assert ModelsUpdateRecordRequest is UpdateRecordRequest
    assert SpiUpdateRecordRequest is UpdateRecordRequest
    assert _CANONICAL_MODEL_MODULES.get("UpdateRecordRequest") == "imednet.models.records"

    with strict_mode_context(True):
        b = BaseRecordRequest(
            formId=10,
            recordStatus="Record Complete",
            reasonForChange="Strict base",
            data={"A": 1},
        )
        r = RegisterSubjectRequest(
            formId=10,
            siteId=1,
            subjectId=50,
            intervalId=2,
            recordStatus=RecordStatus.RECORD_COMPLETE,
            reasonForChange="Enrollment",
        )
        s = UpdateScheduledRecordRequest(
            formId=20,
            subjectId=50,
            intervalId=3,
            recordStatus="Record Complete",
            reasonForChange="Scheduled",
        )
        c = CreateNewRecordRequest(
            formId=30,
            subjectId=50,
            intervalId=4,
            recordStatus="Record Complete",
            reasonForChange="Unscheduled",
        )
        u = UpdateRecordRequest(
            formId=20,
            subjectId=50,
            intervalId=3,
            recordId=700,
            recordOid="OID-700",
            visitId=88,
            recordStatus="Record Complete",
            reasonForChange="Update",
        )

        for model in (b, r, s, c, u):
            dumped = model.model_dump(by_alias=True)
            assert "" not in dumped.values()
            assert None not in dumped.values()


def test_model_engine_check_cli_exits_zero() -> None:
    """Running `python -m imednet.models.engine --check` exits with code 0."""
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-m", "imednet.models.engine", "--check"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_masked_sentinel_and_schema_validation_accept_base_record_requests() -> None:
    """ensure_no_masked_sentinel and SchemaValidator accept BaseRecordRequest instances and reject ***MASKED***."""
    clean_req = UpdateRecordRequest(
        formKey="VS",
        recordId=12,
        reasonForChange="Routine check",
        data={"SYSBP": 120},
    )
    ensure_no_masked_sentinel([clean_req])
    ensure_no_masked_sentinel((clean_req,))

    assert _extract_form_ref(clean_req) == ("VS", None)
    assert _extract_form_ref(UpdateRecordRequest(formId=99, recordId=12)) == (None, 99)

    masked_in_data = CreateNewRecordRequest(
        formKey="VS",
        subjectKey="SUBJ-1",
        data={"NOTES": "Patient ***MASKED***"},
    )
    with pytest.raises(ValidationError, match="MASKED"):
        ensure_no_masked_sentinel([masked_in_data])

    masked_in_attr = UpdateRecordRequest(
        formKey="VS",
        recordId=12,
        reasonForChange="***MASKED***",
        data={"SYSBP": 120},
    )
    with pytest.raises(ValidationError, match="MASKED"):
        ensure_no_masked_sentinel((masked_in_attr,))

    # Ensure masked sentinel blocks HTTP calls before any POST is sent
    http_calls: list[str] = []

    def _unexpected_http(req: httpx.Request) -> httpx.Response:
        http_calls.append(str(req.url))
        return httpx.Response(200, json={})

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://mock.imednet.test",
        transport=httpx.MockTransport(_unexpected_http),
    )
    try:
        with pytest.raises(ValidationError, match="MASKED"):
            sdk.records.create("STUDY", [masked_in_data])
        with pytest.raises(ValidationError, match="MASKED"):
            sdk.records.create_batch("STUDY", (masked_in_attr,), skip_validation=True)
        assert http_calls == []
    finally:
        sdk.close()

    # SchemaCache / SchemaValidator validate BaseRecordRequest by formKey and formId
    mock_sdk = MagicMock()
    mock_sdk.variables.list.return_value = [
        Variable(
            studyKey="STUDY",
            formId=20,
            formKey="VS",
            formName="Vitals",
            variableId=1,
            variableName="SYSBP",
            variableType="integer",
        )
    ]
    validator = SchemaValidator(mock_sdk)
    assert isinstance(validator.schema, SchemaCache)

    validator.validate_batch(
        "STUDY",
        [
            UpdateScheduledRecordRequest(formId=20, subjectId=1, intervalId=2, data={"SYSBP": 120}),
            UpdateRecordRequest(formKey="VS", recordId=55, data={"SYSBP": 115}),
        ],
    )
    with pytest.raises(ValidationError):
        validator.validate_batch(
            "STUDY",
            [UpdateRecordRequest(formId=20, recordId=55, data={"SYSBP": "not-an-int"})],
        )
    assert AsyncSchemaValidator is not None


@pytest.mark.asyncio
async def test_sync_async_batch_and_workflow_writes_accept_request_lists_and_tuples() -> None:
    """Sync, async, batch, and workflow writes accept BaseRecordRequest lists and tuples end-to-end."""
    state = SimulationState()
    transport = SimulationTransport(state=state)
    sdk = ImednetSDK(transport=transport)
    async_sdk = AsyncImednetSDK(transport=SimulationTransport(state=SimulationState()))
    try:
        forms = sdk.get_forms("DEMO-STUDY")
        sites = sdk.get_sites("DEMO-STUDY")
        variables = sdk.get_variables("DEMO-STUDY")
        first_form = forms[0]
        first_site = sites[0]
        form_vars = [v for v in variables if v.form_key == first_form.form_key]
        sample_data: dict[str, Any] = {}
        if form_vars and form_vars[0].variable_name:
            vtype = (form_vars[0].variable_type or "").lower()
            sample_data[form_vars[0].variable_name] = (
                1 if "int" in vtype or "num" in vtype else "ok"
            )

        req_list = [
            RegisterSubjectRequest(
                formKey=first_form.form_key or "",
                siteName=first_site.site_name or "",
                subjectKey="SUBJ-REQ-LIST-1",
                recordStatus="Record Complete",
                reasonForChange="Enrollment list",
                data=sample_data,
            )
        ]
        req_tuple = (
            RegisterSubjectRequest(
                formKey=first_form.form_key or "",
                siteName=first_site.site_name or "",
                subjectKey="SUBJ-REQ-TUPLE-1",
                recordStatus="Record Complete",
                reasonForChange="Enrollment tuple",
                data=sample_data,
            ),
        )

        # Endpoint + convenience sync writes (list and tuple)
        job_sync_list = sdk.records.create("DEMO-STUDY", req_list)
        assert job_sync_list.batch_id
        batch_sync_tuple = sdk.records.create_batch(
            "DEMO-STUDY", req_tuple, wait_for_completion=True, interval=0.0
        )
        assert batch_sync_tuple.all_successful is True
        assert batch_sync_tuple.created_record_ids
        assert "SUBJ-REQ-TUPLE-1" in batch_sync_tuple.enrolled_subject_keys

        # Endpoint + convenience async writes (list and tuple)
        job_async_tuple = await async_sdk.records.create("DEMO-STUDY", req_tuple)
        assert job_async_tuple.batch_id
        batch_async_list = await async_sdk.create_records_batch(
            "DEMO-STUDY", req_list, wait_for_completion=True, interval=0.0
        )
        assert batch_async_list.all_successful is True
        assert batch_async_list.created_record_ids

        # Sync & Async RecordUpdateWorkflow writes with tuple of BaseRecordRequest
        wf_tuple = (
            CreateNewRecordRequest(
                formKey=first_form.form_key or "",
                subjectKey="SUBJ-REQ-LIST-1",
                recordStatus="Record Complete",
                reasonForChange="Workflow tuple",
                data=sample_data,
            ),
        )
        wf_sync = RecordUpdateWorkflow(sdk)
        wf_job = wf_sync.create_or_update_records(
            "DEMO-STUDY", wf_tuple, wait_for_completion=True, poll_interval=0
        )
        assert wf_job.batch_id
        wf_batch = wf_sync.create_or_update_records_batch(
            "DEMO-STUDY", list(wf_tuple), wait_for_completion=True, poll_interval=0
        )
        assert wf_batch.all_successful is True
        assert wf_batch.created_record_ids

        wf_async = AsyncRecordUpdateWorkflow(async_sdk)
        awf_job = await wf_async.create_or_update_records(
            "DEMO-STUDY", wf_tuple, wait_for_completion=True, poll_interval=0
        )
        assert awf_job.batch_id
        awf_batch = await wf_async.create_or_update_records_batch(
            "DEMO-STUDY", wf_tuple, wait_for_completion=True, poll_interval=0
        )
        assert awf_batch.all_successful is True
        assert awf_batch.created_record_ids

        # SimulationTransport already includes inline JobStatus.results, so fetch_result is never called
        spy_fetch = MagicMock(side_effect=AssertionError("External fetch_result should be skipped"))
        sdk.jobs.fetch_result = spy_fetch  # type: ignore[method-assign]
        inline_summary = sdk.records.create_batch(
            "DEMO-STUDY",
            [
                CreateNewRecordRequest(
                    formKey=first_form.form_key or "",
                    subjectKey="SUBJ-REQ-LIST-1",
                    data=sample_data,
                )
            ],
            wait_for_completion=True,
            interval=0.0,
        )
        assert inline_summary.created_record_ids
        spy_fetch.assert_not_called()
    finally:
        sdk.close()
        await async_sdk.aclose()


@pytest.mark.asyncio
async def test_six_attribute_jobs_response_fetches_result_url_strips_cross_origin_creds_and_populates_summary(
    tmp_path: Path,
) -> None:
    """create_batch(..., wait_for_completion=True) against a 6-attribute Job response fetches resultUrl safely."""
    seen_requests: list[tuple[str, str | None, dict[str, str]]] = []
    batch_counter = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal batch_counter
        seen_requests.append(
            (request.method, request.url.host, {k.lower(): v for k, v in request.headers.items()})
        )
        if request.method == "POST" and request.url.path.endswith("/records"):
            batch_counter += 1
            bid = f"BATCH-{batch_counter}"
            return httpx.Response(
                200,
                json={"jobId": f"JOB-{batch_counter}", "batchId": bid, "state": "OPEN"},
            )
        if request.method == "GET" and "/jobs/BATCH-" in request.url.path:
            bid = request.url.path.rsplit("/", 1)[-1]
            # Exact 6-attribute flat response from iMednet Jobs API (no inline results)
            return httpx.Response(
                200,
                json={
                    "jobId": f"JOB-{bid.split('-')[-1]}",
                    "batchId": bid,
                    "state": "COMPLETED",
                    "dateCreated": "2026-05-01 10:00:00",
                    "dateFinished": "2026-05-01 10:00:02",
                    "resultUrl": f"https://s3.amazonaws.com/imednet-job-results/{bid}.json",
                },
            )
        if request.url.host == "s3.amazonaws.com":
            # Cross-origin resultUrl MUST strip EDC API credentials
            lowered = {k.lower() for k in request.headers.keys()}
            assert "x-api-key" not in lowered
            assert "x-imn-security-key" not in lowered
            bid = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            idx = int(bid.split("-")[-1])
            return httpx.Response(
                200,
                json=[
                    {
                        "recordId": 9000 + idx,
                        "subjectKey": f"SUBJ-AUTO-{idx}",
                        "status": "Record Complete",
                    }
                ],
            )
        return httpx.Response(404)

    sdk = ImednetSDK(
        api_key="edc-api-key",  # pragma: allowlist secret
        security_key="edc-sec-key",  # pragma: allowlist secret
        base_url="https://edc.prod.imednet.test",
        transport=httpx.MockTransport(handler),
    )
    async_sdk = AsyncImednetSDK(
        api_key="edc-api-key",  # pragma: allowlist secret
        security_key="edc-sec-key",  # pragma: allowlist secret
        base_url="https://edc.prod.imednet.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        # 1. Sync create_batch with wait_for_completion=True fetches resultUrl by default
        summary = sdk.records.create_batch(
            "STUDY1",
            [
                RegisterSubjectRequest(formId=10, siteId=1),
                RegisterSubjectRequest(formId=10, siteId=1),
            ],
            chunk_size=1,
            skip_validation=True,
            wait_for_completion=True,
            interval=0.0,
        )
        assert summary.all_successful is True
        assert summary.created_record_ids == [9001, 9002]
        assert summary.enrolled_subject_keys == ["SUBJ-AUTO-1", "SUBJ-AUTO-2"]
        assert len(summary.successful_records) == 2
        assert summary.record_errors == []

        # 2. Async create_batch with wait_for_completion=True fetches resultUrl
        async_summary = await async_sdk.records.create_batch(
            "STUDY1",
            (UpdateRecordRequest(recordId=9001, data={"V1": "updated"}),),
            skip_validation=True,
            wait_for_completion=True,
            interval=0.0,
        )
        assert async_summary.created_record_ids == [9003]
        assert async_summary.enrolled_subject_keys == ["SUBJ-AUTO-3"]

        # 3. resolve_results=False skips fetching resultUrl
        s3_before = sum(1 for _, host, _ in seen_requests if host == "s3.amazonaws.com")
        no_res_summary = sdk.records.create_batch(
            "STUDY1",
            [UpdateRecordRequest(recordId=9001, data={"V1": "skip"})],
            skip_validation=True,
            wait_for_completion=True,
            resolve_results=False,
            interval=0.0,
        )
        s3_after = sum(1 for _, host, _ in seen_requests if host == "s3.amazonaws.com")
        assert s3_after == s3_before
        assert no_res_summary.created_record_ids == []

        # 4. JobsEndpoint.poll and poll_many preserve resolve_results=False default
        polled = sdk.jobs.poll("STUDY1", "BATCH-1", interval=0.0)
        assert polled.results is None
        polled_many = sdk.jobs.poll_many("STUDY1", ["BATCH-1"], interval=0.0)
        assert polled_many.results["BATCH-1"].results is None

        # 5. import_records_csv forwards resolve_results=True by default
        csv_file = tmp_path / "import.csv"
        csv_file.write_text("formKey,subjectKey,AGE\nDM,SUBJ-CSV,33\n", encoding="utf-8")
        csv_summary = import_records_csv(
            sdk,
            "STUDY1",
            csv_file,
            skip_validation=True,
            wait_for_completion=True,
            interval=0.0,
        )
        assert csv_summary.created_record_ids == [9005]
    finally:
        sdk.close()
        await async_sdk.aclose()


@pytest.mark.asyncio
async def test_completed_or_failed_job_with_variable_errors_at_result_url_raises_sanitized_job_failed_error() -> (
    None
):
    """Polling a COMPLETED or FAILED Job with Variable errors at resultUrl raises JobFailedError with sanitized record_errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/records"):
            body = json.loads(request.content.decode("utf-8"))
            bid = "B-COMP-ERR" if body[0].get("recordId") == 1 else "B-FAIL-ERR"
            return httpx.Response(200, json={"jobId": f"J-{bid}", "batchId": bid, "state": "OPEN"})
        if request.method == "GET" and path.endswith("/jobs/B-COMP-ERR"):
            return httpx.Response(
                200,
                json={
                    "jobId": "J-COMP",
                    "batchId": "B-COMP-ERR",
                    "state": "COMPLETED",
                    "dateCreated": "2026-05-01 10:00:00",
                    "dateFinished": "2026-05-01 10:00:01",
                    "resultUrl": "https://s3.amazonaws.com/imednet-job-results/B-COMP-ERR.json",
                },
            )
        if request.method == "GET" and path.endswith("/jobs/B-FAIL-ERR"):
            return httpx.Response(
                200,
                json={
                    "jobId": "J-FAIL",
                    "batchId": "B-FAIL-ERR",
                    "state": "FAILED",
                    "dateCreated": "2026-05-01 10:00:00",
                    "dateFinished": "2026-05-01 10:00:01",
                    "resultUrl": "https://s3.amazonaws.com/imednet-job-results/B-FAIL-ERR.json",
                },
            )
        if request.url.host == "s3.amazonaws.com" and request.url.path.endswith("B-COMP-ERR.json"):
            # COMPLETED job whose resultUrl reveals Variable-level errors (plus PHI SSN to verify sanitization)
            return httpx.Response(
                200,
                json=[
                    {
                        "recordId": 1,
                        "formKey": "VS",
                        "errors": [
                            {
                                "variable": "SYSBP",
                                "message": "Invalid value for ssn=123-45-6789",
                            },
                            {
                                "variable": "ssn",
                                "value": "123-45-6789",
                                "error": "Invalid SSN format",
                            },
                        ],
                    }
                ],
            )
        if request.url.host == "s3.amazonaws.com" and request.url.path.endswith("B-FAIL-ERR.json"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "subjectKey": "SUBJ-01",
                            "variableErrors": [{"variableName": "AGE", "error": "Must be >= 18"}],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://edc.prod.imednet.test",
        transport=httpx.MockTransport(handler),
    )
    async_sdk = AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://edc.prod.imednet.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        # 1. COMPLETED state with Variable errors at resultUrl raises JobFailedError
        with pytest.raises(JobFailedError) as comp_exc:
            sdk.jobs.poll("S1", "B-COMP-ERR", interval=0.0, resolve_results=True)
        assert comp_exc.value.record_errors
        serialized_err = json.dumps(comp_exc.value.record_errors)
        assert "SYSBP" in serialized_err
        assert "123-45-6789" not in serialized_err  # PHI redacted!

        # 2. FAILED state with Variable errors at resultUrl raises JobFailedError asynchronously
        with pytest.raises(JobFailedError) as fail_exc:
            await async_sdk.jobs.poll("S1", "B-FAIL-ERR", interval=0.0, resolve_results=True)
        assert fail_exc.value.record_errors
        assert "AGE" in json.dumps(fail_exc.value.record_errors)

        # 3. create_batch(..., wait_for_completion=True) attaches partial_summary with sanitized record_errors
        with pytest.raises(JobFailedError) as batch_exc:
            sdk.records.create_batch(
                "S1",
                [UpdateRecordRequest(recordId=1, data={"SYSBP": 999})],
                skip_validation=True,
                wait_for_completion=True,
                interval=0.0,
            )
        partial = getattr(batch_exc.value, "partial_summary", None)
        assert partial is not None
        assert partial.all_successful is False
        assert partial.record_errors
        assert "123-45-6789" not in json.dumps(partial.record_errors)
    finally:
        sdk.close()
        await async_sdk.aclose()


@pytest.mark.asyncio
async def test_workflow_helpers_support_expanded_identifiers_audit_fields_update_record_and_uat_resolve_results() -> (
    None
):
    """RecordUpdateWorkflow, RegisterSubjectsWorkflow, and BulkRecordSubmissionWorkflow forward new options."""
    sync_sdk = MagicMock()
    del sync_sdk.async_create_record
    wf = RecordUpdateWorkflow(sync_sdk)
    wf.create_or_update_records = MagicMock(return_value=Job(batchId="B-1", state="COMPLETED"))  # type: ignore[method-assign]

    # register_subject with subject_key, subject_oid, record_status, reason_for_change, resolve_results
    wf.register_subject(
        "STUDY",
        form_identifier=10,
        site_identifier=1,
        data={"V": 1},
        form_identifier_type="id",
        site_identifier_type="id",
        subject_key="SUBJ-10",
        subject_oid="OID-10",
        record_status=RecordStatus.RECORD_COMPLETE,
        reason_for_change="Enrollment",
        wait_for_completion=True,
        resolve_results=False,
    )
    reg_kwargs = wf.create_or_update_records.call_args.kwargs
    assert reg_kwargs["resolve_results"] is False
    assert reg_kwargs["records_data"] == [
        {
            "formId": 10,
            "siteId": 1,
            "subjectKey": "SUBJ-10",
            "subjectOid": "OID-10",
            "recordStatus": "Record Complete",
            "reasonForChange": "Enrollment",
            "data": {"V": 1},
        }
    ]

    # update_scheduled_record & create_new_record with record_status and reason_for_change
    wf.update_scheduled_record(
        "STUDY",
        form_identifier=20,
        subject_identifier=50,
        interval_identifier=3,
        data={"SYSBP": 120},
        form_identifier_type="id",
        subject_identifier_type="id",
        interval_identifier_type="id",
        record_status="Record Complete",
        reason_for_change="Scheduled update",
    )
    assert wf.create_or_update_records.call_args.kwargs["records_data"] == [
        {
            "formId": 20,
            "subjectId": 50,
            "intervalId": 3,
            "recordStatus": "Record Complete",
            "reasonForChange": "Scheduled update",
            "data": {"SYSBP": 120},
        }
    ]

    wf.create_new_record(
        "STUDY",
        form_identifier="AE",
        subject_identifier="OID-50",
        data={"AETERM": "Nausea"},
        subject_identifier_type="oid",
        record_status="Data Complete",
        reason_for_change="New AE",
    )
    assert wf.create_or_update_records.call_args.kwargs["records_data"] == [
        {
            "formKey": "AE",
            "subjectOid": "OID-50",
            "recordStatus": "Data Complete",
            "reasonForChange": "New AE",
            "data": {"AETERM": "Nausea"},
        }
    ]

    # update_record requires at least one target identifier
    with pytest.raises(ValueError, match="requires at least one target identifier"):
        wf.update_record("STUDY", {"A": 1})

    wf.update_record(
        "STUDY",
        {"SYSBP": 115},
        record_id=777,
        record_oid="REC-OID-777",
        visit_id=12,
        form_identifier=20,
        form_identifier_type="id",
        subject_identifier=50,
        subject_identifier_type="id",
        record_status="Record Complete",
        reason_for_change="Audit fix",
        wait_for_completion=True,
        resolve_results=True,
    )
    assert wf.create_or_update_records.call_args.kwargs["records_data"] == [
        {
            "formId": 20,
            "subjectId": 50,
            "recordId": 777,
            "recordOid": "REC-OID-777",
            "visitId": 12,
            "recordStatus": "Record Complete",
            "reasonForChange": "Audit fix",
            "data": {"SYSBP": 115},
        }
    ]

    # AsyncRecordUpdateWorkflow.update_record
    async_sdk_mock = MagicMock()
    awf = AsyncRecordUpdateWorkflow(async_sdk_mock)
    awf.create_or_update_records = AsyncMock(return_value=Job(batchId="AB-1", state="COMPLETED"))  # type: ignore[method-assign]
    await awf.update_record(
        "STUDY",
        {"SYSBP": 116},
        record_oid="REC-OID-888",
        record_status="Record Complete",
        reason_for_change="Async fix",
    )
    assert awf.create_or_update_records.await_args is not None
    assert awf.create_or_update_records.await_args.kwargs["records_data"] == [
        {
            "recordOid": "REC-OID-888",
            "recordStatus": "Record Complete",
            "reasonForChange": "Async fix",
            "data": {"SYSBP": 116},
        }
    ]

    # RegisterSubjectsWorkflow with RegisterSubjectRequest(formId=...) and resolve_results=False
    reg_sdk = MagicMock()
    reg_sdk.get_sites.return_value = [
        Site(studyKey="STUDY", siteId=1, siteName="Site 1", siteEnrollmentStatus="Active")
    ]
    reg_sdk.subjects.get.return_value = []
    reg_sdk.create_record.return_value = Job(jobId="J1", batchId="B-REG", state="PROCESSING")
    reg_sdk.poll_job.return_value = JobStatus(jobId="J1", batchId="B-REG", state="COMPLETED")
    reg_wf = RegisterSubjectsWorkflow(reg_sdk)
    reg_wf.register_subjects(
        "STUDY",
        [
            RegisterSubjectRequest(
                formId=10,
                siteId=1,
                recordStatus="Record Complete",
                reasonForChange="Enroll",
            )
        ],
        wait_for_completion=True,
        poll_interval=0,
    )
    posted_reg = reg_sdk.create_record.call_args.kwargs["records_data"]
    assert posted_reg == [
        {
            "formId": 10,
            "siteId": 1,
            "recordStatus": "Record Complete",
            "reasonForChange": "Enroll",
            "data": {},
        }
    ]
    reg_sdk.poll_job.assert_called_once_with(
        "STUDY", "B-REG", timeout=300, interval=0, resolve_results=True
    )

    reg_sdk.poll_job.reset_mock()
    reg_wf.register_subjects(
        "STUDY",
        [RegisterSubjectRequest(formId=10, siteId=1)],
        wait_for_completion=True,
        resolve_results=False,
        poll_interval=0,
    )
    reg_sdk.poll_job.assert_called_once_with("STUDY", "B-REG", timeout=300, interval=0)

    # BulkRecordSubmissionWorkflow & AsyncBulkRecordSubmissionWorkflow forward resolve_results
    from imednet.utils.job_poller import JobPollSummary

    uat_sdk = MagicMock()
    uat_sdk.poll_jobs = MagicMock(
        return_value=JobPollSummary(
            study_key="STUDY",
            results={"B-UAT": JobStatus(jobId="J-UAT", batchId="B-UAT", state="COMPLETED")},
            failures={},
            elapsed_total=0.1,
        )
    )
    bulk_wf = BulkRecordSubmissionWorkflow(uat_sdk, registration_poll_interval=0)
    bulk_wf._poll_registration_batches("STUDY", ["B-UAT"])
    assert uat_sdk.poll_jobs.call_args.kwargs["resolve_results"] is True
    bulk_wf._poll_registration_batches("STUDY", ["B-UAT"], resolve_results=False)
    assert "resolve_results" not in uat_sdk.poll_jobs.call_args.kwargs

    auat_sdk = MagicMock()
    auat_sdk.poll_jobs = AsyncMock(
        return_value=JobPollSummary(
            study_key="STUDY",
            results={"AB-UAT": JobStatus(jobId="AJ-UAT", batchId="AB-UAT", state="COMPLETED")},
            failures={},
            elapsed_total=0.1,
        )
    )
    abulk_wf = AsyncBulkRecordSubmissionWorkflow(auat_sdk, registration_poll_interval=0)
    await abulk_wf._poll_registration_batches("STUDY", ["AB-UAT"])
    assert auat_sdk.poll_jobs.await_args is not None
    assert auat_sdk.poll_jobs.await_args.kwargs["resolve_results"] is True


def test_cli_subjects_register_form_id_audit_flags_no_resolve_results_and_jobs_wait(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI supports `subjects register --form-id ... --record-status ... --reason-for-change ...`, `--no-resolve-results`, and `jobs wait --resolve-results`."""
    posted_records: list[list[dict[str, Any]]] = []
    result_fetches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/sites"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "DEMO",
                            "siteId": 1,
                            "siteName": "Site One",
                            "siteEnrollmentStatus": "Active",
                        }
                    ]
                },
            )
        if request.method == "GET" and path.endswith("/subjects"):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and path.endswith("/records"):
            body = json.loads(request.content.decode("utf-8"))
            posted_records.append(body)
            return httpx.Response(
                200,
                json={"jobId": "J-CLI-1", "batchId": "B-CLI-1", "state": "OPEN"},
            )
        if request.method == "GET" and path.endswith("/jobs/B-CLI-1"):
            return httpx.Response(
                200,
                json={
                    "jobId": "J-CLI-1",
                    "batchId": "B-CLI-1",
                    "state": "COMPLETED",
                    "dateCreated": "2026-05-01 10:00:00",
                    "dateFinished": "2026-05-01 10:00:01",
                    "resultUrl": "https://s3.amazonaws.com/imednet-job-results/B-CLI-1.json",
                },
            )
        if request.url.host == "s3.amazonaws.com":
            result_fetches.append(str(request.url))
            return httpx.Response(
                200,
                json=[{"recordId": 4242, "subjectKey": "SUBJ-CLI-42", "status": "Record Complete"}],
            )
        return httpx.Response(404)

    def _build_mock_sdk(*_args: Any, **_kwargs: Any) -> ImednetSDK:
        return ImednetSDK(
            api_key="k",
            security_key="s",
            base_url="https://edc.prod.imednet.test",
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _build_mock_sdk)
    monkeypatch.setenv("IMEDNET_STUDY_KEY", "DEMO")

    def _invoke_cli(argv: list[str]) -> int:
        try:
            cli_app(argv)
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    # Acceptance Criterion 6 exact flags
    rc = _invoke_cli(
        [
            "subjects",
            "register",
            "-s",
            "DEMO",
            "--form-id",
            "10",
            "--site-id",
            "1",
            "--record-status",
            "Record Complete",
            "--reason-for-change",
            "Enrollment",
            "--wait",
            "--interval",
            "0.01",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    item = out["items"][0]
    assert item["createdRecordIds"] == [4242]
    assert item["enrolledSubjectKeys"] == ["SUBJ-CLI-42"]
    assert posted_records[-1] == [
        {
            "formId": 10,
            "siteId": 1,
            "recordStatus": "Record Complete",
            "reasonForChange": "Enrollment",
            "data": {},
        }
    ]
    assert len(result_fetches) == 1

    # --no-resolve-results skips resultUrl fetch during --wait
    rc_no_res = _invoke_cli(
        [
            "subjects",
            "register",
            "DEMO",
            "--form-id",
            "10",
            "--site-id",
            "1",
            "--wait",
            "--no-resolve-results",
            "--interval",
            "0.01",
            "--format",
            "json",
        ]
    )
    assert rc_no_res == 0
    out_no_res = json.loads(capsys.readouterr().out)
    assert out_no_res["items"][0]["createdRecordIds"] == []
    assert out_no_res["items"][0]["enrolledSubjectKeys"] == []
    assert len(result_fetches) == 1

    # `imednet jobs wait DEMO B-CLI-1 --resolve-results --format json` fetches resultUrl
    rc_jobs_wait = _invoke_cli(
        [
            "jobs",
            "wait",
            "DEMO",
            "B-CLI-1",
            "--resolve-results",
            "--interval",
            "0",
            "--format",
            "json",
        ]
    )
    assert rc_jobs_wait == 0
    jobs_out = json.loads(capsys.readouterr().out)
    assert jobs_out["items"][0]["results"] == [
        {"recordId": 4242, "subjectKey": "SUBJ-CLI-42", "status": "Record Complete"}
    ]
    assert len(result_fetches) == 2


@pytest.mark.asyncio
async def test_enrollment_defaults_job_status_summary_and_tabular_edge_cases(
    tmp_path: Path,
) -> None:
    """Cover enrollment formId defaults, Mapping sequences, and JobStatus/BatchSubmissionSummary edge cases."""
    from types import MappingProxyType

    from imednet.utils.batch_coordinator import BatchSubmissionSummary
    from imednet.utils.enrollment import normalize_subject_registration_inputs
    from imednet.utils.job_poller import JobPollSummary, _extract_record_errors
    from imednet_workflows.register_subjects import AsyncRegisterSubjectsWorkflow

    # 1. Blank string normalization on BaseRecordRequest & subclasses
    blank_req = UpdateRecordRequest(
        formKey="VS",
        recordStatus="   ",
        reasonForChange="   ",
        recordOid="   ",
        subjectOid="   ",
        recordId=10,
    )
    assert blank_req.record_status is None
    assert blank_req.reason_for_change is None
    assert blank_req.record_oid is None
    assert blank_req.subject_oid is None
    assert UpdateScheduledRecordRequest(subjectOid="  ").subject_oid is None
    assert CreateNewRecordRequest(subjectOid="  ").subject_oid is None

    # 2. normalize_records_input handles mixed Mapping, dict, BaseRecordRequest, and rejects non-mappings
    mixed = normalize_records_input(
        [
            {"formKey": "DM", "data": {"AGE": 30}},
            MappingProxyType({"formId": 10, "data": {"AGE": 31}}),
            UpdateRecordRequest(recordId=7, data={"AGE": 32}),
        ]
    )
    assert len(mixed) == 3
    assert mixed[1] == {"formId": 10, "data": {"AGE": 31}}
    with pytest.raises(TypeError, match="BaseRecordRequest or Mapping"):
        normalize_records_input([123])  # type: ignore[list-item]

    # 3. normalize_subject_registration_inputs applies default_form_id / default_site_id and schema lookup
    csv_path = tmp_path / "subjects.csv"
    csv_path.write_text("subjectKey,AGE\nSUBJ-CSV-1,40\n", encoding="utf-8")
    mock_schema_sdk = MagicMock()
    mock_schema_sdk.variables.list.return_value = [
        Variable(
            studyKey="S",
            formId=10,
            formKey="DM",
            formName="Demographics",
            variableId=1,
            variableName="AGE",
            variableType="integer",
        )
    ]
    schema_validator = SchemaValidator(mock_schema_sdk)
    schema_validator.refresh("S")
    schema = schema_validator.schema
    csv_pairs = normalize_subject_registration_inputs(
        csv_path,
        default_form_id=10,
        default_site_id=1,
        schema=schema,
        study_key="S",
    )
    assert csv_pairs[0][1].form_id == 10
    assert csv_pairs[0][1].form_key == "DM"
    assert csv_pairs[0][1].site_id == 1

    model_pairs = normalize_subject_registration_inputs(
        [
            RegisterSubjectRequest(subjectKey="SUBJ-M1"),
            {"subjectKey": "SUBJ-D1"},
        ],
        default_form_id=10,
        default_site_id=2,
        schema=schema,
        study_key="S",
    )
    assert model_pairs[0][1].form_id == 10
    assert model_pairs[0][1].form_key == "DM"
    assert model_pairs[0][1].site_id == 2
    assert model_pairs[1][1].form_id == 10
    assert model_pairs[1][1].form_key == "DM"
    assert model_pairs[1][1].site_id == 2

    # 4. JobStatus & _extract_record_errors dict/scalar shapes + BatchSubmissionSummary properties
    dict_err_status = JobStatus(
        jobId="J-DICT",
        batchId="B-DICT",
        state="COMPLETED",
        results={
            "fieldErrors": {"SYSBP": "Out of range", "DIABP": "Missing"},
            "validationErrors": "Form incomplete",
            "records": [
                {"recordId": "105", "subjectKey": "SUBJ-105", "success": True},
                {"recordId": 106, "status": "CANCELLED"},
                {"recordId": 107, "success": False},
            ],
        },
    )
    assert dict_err_status.is_failed is True
    assert dict_err_status.total_records == 3
    assert dict_err_status.successful_records == 1
    extracted_errs = _extract_record_errors(dict_err_status)
    assert any(e.get("variable") == "SYSBP" for e in extracted_errs)
    assert any(e.get("error") == "Form incomplete" for e in extracted_errs)

    single_dict_status = JobStatus(
        jobId="J-SINGLE",
        batchId="B-SINGLE",
        state="COMPLETED",
        results={"recordId": "205", "subjectKey": "SUBJ-205"},
    )
    orphan_err = JobFailedError(
        "Failed",
        status=JobStatus(jobId="J-ORPH", batchId="B-ORPH", state="FAILED"),
        record_errors=[{"variable": "AGE", "error": "Invalid"}],
    )
    summary = BatchSubmissionSummary(
        study_key="S",
        total_records=4,
        chunk_sizes=[2, 2],
        jobs=[single_dict_status, single_dict_status],
        poll_summary=JobPollSummary(
            study_key="S",
            results={"B-DICT": dict_err_status},
            failures={"B-ORPH": orphan_err},
            elapsed_total=0.1,
        ),
    )
    assert summary.created_record_ids == [205, 105]
    assert summary.enrolled_subject_keys == ["SUBJ-205", "SUBJ-105"]
    assert any(e.get("variable") == "AGE" for e in summary.record_errors)

    # 5. AsyncRegisterSubjectsWorkflow forwards resolve_results=False
    async_reg_sdk = MagicMock()
    async_reg_sdk.get_sites = AsyncMock(
        return_value=[Site(studyKey="S", siteId=1, siteName="S1", siteEnrollmentStatus="Active")]
    )
    async_reg_sdk.create_record = AsyncMock(
        return_value=Job(jobId="AJ1", batchId="AB-REG", state="OPEN")
    )
    async_reg_sdk.poll_job = AsyncMock(
        return_value=JobStatus(jobId="AJ1", batchId="AB-REG", state="COMPLETED")
    )
    async_reg_wf = AsyncRegisterSubjectsWorkflow(async_reg_sdk)
    await async_reg_wf.register_subjects(
        "S",
        [RegisterSubjectRequest(formId=10, siteId=1)],
        wait_for_completion=True,
        resolve_results=False,
        poll_interval=0,
    )
    async_reg_sdk.poll_job.assert_awaited_once_with("S", "AB-REG", timeout=300, interval=0)
