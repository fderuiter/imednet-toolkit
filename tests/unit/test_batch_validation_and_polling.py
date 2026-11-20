"""Tests for BatchValidationReport, required API schema checks, job polling, and async enrollment."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from imednet import AsyncImednetSDK, ImednetSDK
from imednet.core.async_client import AsyncClient
from imednet.core.client import Client
from imednet.core.context import reset_strict_mode_context, set_strict_mode_context
from imednet.datadict.models import DataDictionary
from imednet.errors import ValidationError
from imednet.models.forms import Form
from imednet.models.jobs import Job, JobStatus
from imednet.models.sites import Site
from imednet.models.subjects import Subject
from imednet.models.variables import Variable
from imednet.simulation import SimulationState, SimulationTransport
from imednet.utils.batch_coordinator import AsyncBatchCoordinator, BatchCoordinator
from imednet.utils.job_poller import JobStatusEvent
from imednet.validation.cache import (
    AsyncSchemaCache,
    BatchValidationReport,
    SchemaCache,
    calculate_readiness_score,
    validate_record_data,
    validate_records_batch,
)
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow


@pytest.fixture(autouse=True)
def disable_strict_mode():
    """Ensure contextvar strict mode does not leak across tests."""
    token = set_strict_mode_context(False)
    try:
        yield
    finally:
        reset_strict_mode_context(token)


def _build_dd_schema() -> DataDictionary:
    """Create a DataDictionary with required, range, length, and choice constraints."""
    return DataDictionary.model_validate(
        {
            "forms": [
                {
                    "formKey": "F_VITALS",
                    "variables": [
                        {
                            "variableName": "AGE",
                            "variableType": "integer",
                            "required": "Yes",
                            "extraAttributes": {"minValue": 18, "maxValue": 99},
                        },
                        {
                            "variableName": "SYSBP",
                            "variableType": "integer",
                            "required": "Yes",
                            "extraAttributes": {"minValue": 70, "maxValue": 220},
                        },
                        {
                            "variableName": "INITIALS",
                            "variableType": "text",
                            "required": "No",
                            "format": "3",
                        },
                        {
                            "variableName": "ARM",
                            "variableType": "radio",
                            "required": "No",
                            "choices": [
                                {"choiceValue": "A", "choiceText": "Arm A"},
                                {"choiceValue": "B", "choiceText": "Arm B"},
                            ],
                        },
                    ],
                }
            ]
        }
    )


def test_batch_validation_report_collects_all_four_errors_across_two_records() -> None:
    """AC 3: Validating 2 invalid records with 4 constraint errors returns all 4 in one report."""
    schema = _build_dd_schema()
    batch = [
        # Valid record
        {"formKey": "F_VITALS", "subjectKey": "SUB-1", "data": {"AGE": 35, "SYSBP": 120}},
        # Invalid record 1: AGE out of range (12 < 18) + missing required SYSBP -> 2 errors
        {"formKey": "F_VITALS", "subjectKey": "SUB-2", "data": {"AGE": 12}},
        # Invalid record 2: INITIALS exceeds length 3 + ARM invalid choice -> 2 errors
        {
            "formKey": "F_VITALS",
            "subjectKey": "SUB-3",
            "data": {"AGE": 40, "SYSBP": 115, "INITIALS": "SECRET_PHI_NAME", "ARM": "Z"},
        },
    ]

    report = validate_records_batch(schema, batch)
    assert isinstance(report, BatchValidationReport)
    assert not report.is_valid
    assert report.total_records == 3
    assert report.invalid_records == 2
    assert report.error_count == 4
    assert len(report.errors) == 4

    error_vars = {(issue.record_index, issue.variable_name) for issue in report.errors}
    assert error_vars == {
        (1, "AGE"),
        (1, "SYSBP"),
        (2, "INITIALS"),
        (2, "ARM"),
    }

    # Ensure PHI value is redacted in error messages
    formatted = report.format_summary()
    assert "SECRET_PHI_NAME" not in formatted

    with pytest.raises(ValidationError) as exc_info:
        report.raise_for_errors()
    assert exc_info.value.validation_report is report
    assert "SECRET_PHI_NAME" not in str(exc_info.value)


def test_base_schema_cache_enforces_required_variable_metadata() -> None:
    """Req 6: BaseSchemaCache enforces required variable flags when Variable metadata defines them."""
    forms_ep = MagicMock()
    forms_ep.list.return_value = [
        Form(form_id=10, form_key="F_DEMOG", form_name="Demographics", study_key="S1")
    ]
    vars_ep = MagicMock()
    vars_ep.list.return_value = [
        Variable(
            variable_id=1,
            variable_name="SUBJ_AGE",
            variable_type="integer",
            form_id=10,
            form_key="F_DEMOG",
            required=True,
        ),
        Variable(
            variable_id=2,
            variable_name="CONSENT_YN",
            variable_type="text",
            form_id=10,
            form_key="F_DEMOG",
            required="Yes",
        ),
        Variable(
            variable_id=3,
            variable_name="OPTIONAL_NOTE",
            variable_type="text",
            form_id=10,
            form_key="F_DEMOG",
            required=False,
        ),
    ]

    cache = SchemaCache()
    cache.refresh(forms_ep, vars_ep, study_key="S1")

    # Fail-fast validation catches missing required field on API schema
    with pytest.raises(ValidationError, match="Required variable 'SUBJ_AGE'"):
        validate_record_data(cache, "F_DEMOG", {"CONSENT_YN": "Y"}, study_key="S1")

    # Batch error collection gathers both missing required fields
    report = validate_record_data(
        cache,
        "F_DEMOG",
        {"OPTIONAL_NOTE": "ok"},
        study_key="S1",
        collect_errors=True,
    )
    assert isinstance(report, BatchValidationReport)
    assert report.error_count == 2
    assert {e.variable_name for e in report.errors} == {"SUBJ_AGE", "CONSENT_YN"}

    # Readiness score reflects missing required API variables
    score, reasons = calculate_readiness_score(cache, "F_DEMOG", {}, study_key="S1")
    assert score < 1.0
    assert any("SUBJ_AGE" in r for r in reasons)
    assert any("CONSENT_YN" in r for r in reasons)

    # Complete required fields pass cleanly
    ok_report = validate_record_data(
        cache,
        "F_DEMOG",
        {"SUBJ_AGE": 30, "CONSENT_YN": "Y"},
        study_key="S1",
        collect_errors=True,
    )
    assert ok_report is not None and ok_report.is_valid


@pytest.mark.asyncio
async def test_async_schema_cache_enforces_required_variables() -> None:
    """AsyncSchemaCache shares BaseSchemaCache required-variable enforcement."""

    async def _aiter(items):
        for item in items:
            yield item

    forms_ep = MagicMock()
    forms_ep.list.side_effect = lambda **_kw: _aiter(
        [Form(form_id=10, form_key="F_DEMOG", form_name="Demographics", study_key="S1")]
    )
    vars_ep = MagicMock()
    vars_ep.list.side_effect = lambda **_kw: _aiter(
        [
            Variable(
                variable_id=1,
                variable_name="REQ_FIELD",
                variable_type="text",
                form_id=10,
                form_key="F_DEMOG",
                required=True,
            )
        ]
    )

    cache = AsyncSchemaCache()
    await cache.refresh(forms_ep, vars_ep, study_key="S1")

    with pytest.raises(ValidationError, match="Required variable 'REQ_FIELD'"):
        validate_record_data(cache, "F_DEMOG", {}, study_key="S1")


@pytest.mark.asyncio
async def test_batch_coordinator_dry_run_and_collect_errors() -> None:
    """Req 5: BatchCoordinator and AsyncBatchCoordinator support dry_run and collect_errors."""
    schema = _build_dd_schema()
    submit_spy = MagicMock()

    coord = BatchCoordinator()
    valid_records = [
        {"formKey": "F_VITALS", "subjectKey": "S1", "data": {"AGE": 30, "SYSBP": 120}},
        {"formKey": "F_VITALS", "subjectKey": "S2", "data": {"AGE": 45, "SYSBP": 118}},
    ]
    summary = coord.submit(
        "STUDY",
        valid_records,
        submit_chunk=submit_spy,
        schema=schema,
        dry_run=True,
        collect_errors=True,
    )
    assert summary.dry_run is True
    assert summary.total_records == 2
    assert summary.batch_ids == []
    assert summary.validation_report is not None
    assert summary.validation_report.is_valid
    submit_spy.assert_not_called()

    # Async dry-run
    async_submit_spy = AsyncMock()
    acoord = AsyncBatchCoordinator()
    asummary = await acoord.submit(
        "STUDY",
        valid_records,
        submit_chunk=async_submit_spy,
        schema=schema,
        dry_run=True,
        collect_errors=True,
    )
    assert asummary.dry_run is True
    assert asummary.total_records == 2
    async_submit_spy.assert_not_called()

    # Collect-errors (dry_run=False, collect_errors=True) raises single ValidationError carrying all 4 issues
    invalid_records = [
        {"formKey": "F_VITALS", "subjectKey": "S1", "data": {"AGE": 10}},
        {"formKey": "F_VITALS", "subjectKey": "S2", "data": {}},
    ]
    with pytest.raises(ValidationError) as exc_info:
        coord.submit(
            "STUDY",
            invalid_records,
            submit_chunk=submit_spy,
            schema=schema,
            dry_run=False,
            collect_errors=True,
        )
    assert exc_info.value.validation_report is not None
    assert exc_info.value.validation_report.error_count == 4
    submit_spy.assert_not_called()


def test_sync_records_create_batch_polls_and_invokes_on_progress_every_cycle() -> None:
    """AC 5 (sync): RecordsEndpoint.create_batch polls jobs and calls on_progress each poll cycle."""
    state = SimulationState()
    base_transport = SimulationTransport(state=state)
    poll_states = ["QUEUED", "PROCESSING", "COMPLETED"]
    poll_idx = 0

    class StagedJobTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal poll_idx
            if request.method == "GET" and "/jobs/" in request.url.path:
                current_state = poll_states[min(poll_idx, len(poll_states) - 1)]
                poll_idx += 1
                batch_id = request.url.path.rstrip("/").split("/")[-1]
                return httpx.Response(
                    200,
                    json={
                        "jobId": f"JOB-{batch_id}",
                        "batchId": batch_id,
                        "state": current_state,
                        "progress": 33 * poll_idx if current_state != "COMPLETED" else 100,
                    },
                    request=request,
                )
            return base_transport.handle_request(request)

    sdk = ImednetSDK(
        client=Client(
            api_key="k",  # pragma: allowlist secret
            security_key="s",  # pragma: allowlist secret
            base_url="https://edc.sim.imednet.com",
            transport=StagedJobTransport(),
        )
    )

    observed_statuses: list[str] = []

    def on_progress(event: JobStatusEvent) -> None:
        observed_statuses.append(event.status.state)

    summary = sdk.records.create_batch(
        "STUDY",
        [{"formKey": "F_DM", "subjectKey": "SUB1", "siteName": "SITE", "data": {"V_AGE": 44}}],
        wait_for_completion=True,
        timeout=5,
        interval=0,
        on_progress=on_progress,
    )

    assert observed_statuses == ["QUEUED", "PROCESSING", "COMPLETED"]
    assert summary.poll_summary is not None
    assert summary.all_successful
    assert summary.last_job is not None
    assert summary.last_job.state == "COMPLETED"


@pytest.mark.asyncio
async def test_async_records_create_batch_polls_and_invokes_on_progress_every_cycle() -> None:
    """AC 5 (async): AsyncRecordsEndpoint.create_batch polls jobs and calls on_progress each cycle."""
    state = SimulationState()
    base_transport = SimulationTransport(state=state)
    poll_states = ["QUEUED", "PROCESSING", "COMPLETED"]
    poll_idx = 0

    class AsyncStagedJobTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal poll_idx
            if request.method == "GET" and "/jobs/" in request.url.path:
                current_state = poll_states[min(poll_idx, len(poll_states) - 1)]
                poll_idx += 1
                batch_id = request.url.path.rstrip("/").split("/")[-1]
                return httpx.Response(
                    200,
                    json={
                        "jobId": f"JOB-{batch_id}",
                        "batchId": batch_id,
                        "state": current_state,
                        "progress": 50 if current_state != "COMPLETED" else 100,
                    },
                    request=request,
                )
            return base_transport.handle_request(request)

    async_sdk = AsyncImednetSDK(
        async_client=AsyncClient(
            api_key="k",  # pragma: allowlist secret
            security_key="s",  # pragma: allowlist secret
            base_url="https://edc.sim.imednet.com",
            transport=AsyncStagedJobTransport(),
        )
    )

    observed_statuses: list[str] = []

    def on_progress(event: JobStatusEvent) -> None:
        observed_statuses.append(event.status.state)

    summary = await async_sdk.records.create_batch(
        "STUDY",
        [{"formKey": "F_DM", "subjectKey": "SUB1", "siteName": "SITE", "data": {"V_AGE": 44}}],
        wait_for_completion=True,
        timeout=5,
        interval=0,
        on_progress=on_progress,
    )

    assert observed_statuses == ["QUEUED", "PROCESSING", "COMPLETED"]
    assert summary.poll_summary is not None
    assert summary.all_successful
    assert summary.last_job is not None
    assert summary.last_job.state == "COMPLETED"


@pytest.mark.asyncio
async def test_register_subjects_workflow_supports_both_sync_and_async_facades() -> None:
    """AC 6: RegisterSubjectsWorkflow supports ImednetFacade and AsyncImednetFacade in async methods."""
    # 1. Sync ImednetFacade (via SimulationTransport ImednetSDK)
    sync_state = SimulationState()
    sync_sdk = ImednetSDK(
        client=Client(
            api_key="k",  # pragma: allowlist secret
            security_key="s",  # pragma: allowlist secret
            base_url="https://edc.sim.imednet.com",
            transport=SimulationTransport(state=sync_state),
        )
    )
    sync_wf = RegisterSubjectsWorkflow(sync_sdk, filter_enrolled=True)

    # SUB1 already exists in SimulationState; SUB-NEW-1 and SUB-NEW-2 are new
    single_res = await sync_wf.async_register_subjects(
        "STUDY",
        [
            {"formKey": "F_DM", "siteName": "SITE", "subjectKey": "SUB1", "data": {"V_AGE": 31}},
            {
                "formKey": "F_DM",
                "siteName": "SITE",
                "subjectKey": "SUB-NEW-1",
                "data": {"V_AGE": 32},
            },
        ],
        wait_for_completion=True,
        timeout=5,
        poll_interval=0,
    )
    assert isinstance(single_res, JobStatus)
    assert single_res.state == "COMPLETED"

    progress_events: list[str] = []
    batch_res = await sync_wf.async_register_subjects_batch(
        "STUDY",
        [
            {
                "formKey": "F_DM",
                "siteName": "SITE",
                "subjectKey": "SUB-NEW-2",
                "data": {"V_AGE": 33},
            }
        ],
        wait_for_completion=True,
        timeout=5,
        poll_interval=0,
        on_progress=lambda ev: progress_events.append(ev.status.state),
    )
    assert batch_res.total_records == 1
    assert progress_events == ["COMPLETED"]

    # 2. AsyncImednetFacade (via SimulationTransport AsyncImednetSDK)
    async_state = SimulationState()
    async_base = SimulationTransport(state=async_state)

    class SimpleAsyncSimTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return async_base.handle_request(request)

    async_sdk = AsyncImednetSDK(
        async_client=AsyncClient(
            api_key="k",  # pragma: allowlist secret
            security_key="s",  # pragma: allowlist secret
            base_url="https://edc.sim.imednet.com",
            transport=SimpleAsyncSimTransport(),
        )
    )
    async_wf = RegisterSubjectsWorkflow(async_sdk, filter_enrolled=True)

    async_single = await async_wf.async_register_subjects(
        "STUDY",
        [
            {"formKey": "F_DM", "siteName": "SITE", "subjectKey": "SUB1", "data": {"V_AGE": 40}},
            {
                "formKey": "F_DM",
                "siteName": "SITE",
                "subjectKey": "SUB-ASYNC-1",
                "data": {"V_AGE": 41},
            },
        ],
    )
    assert isinstance(async_single, Job)
    assert async_single.batch_id

    async_progress: list[str] = []
    async_batch = await async_wf.async_register_subjects_batch(
        "STUDY",
        [
            {
                "formKey": "F_DM",
                "siteName": "SITE",
                "subjectKey": "SUB-ASYNC-2",
                "data": {"V_AGE": 42},
            }
        ],
        wait_for_completion=True,
        timeout=5,
        poll_interval=0,
        on_progress=lambda ev: async_progress.append(ev.status.state),
    )
    assert async_batch.total_records == 1
    assert async_progress == ["COMPLETED"]

    # Inactive site rejection works identically in async workflow
    mock_async_facade = MagicMock()
    mock_async_facade.async_get_sites = AsyncMock(
        return_value=[Site(site_id=1, site_name="CLOSED_SITE", site_enrollment_status="Closed")]
    )
    mock_async_facade.async_get_subjects = AsyncMock(return_value=[Subject(subject_key="S0")])
    mock_async_facade.async_create_record = AsyncMock()

    closed_wf = RegisterSubjectsWorkflow(mock_async_facade)
    with pytest.raises(ValueError, match="not eligible for enrollment"):
        await closed_wf.async_register_subjects(
            "STUDY",
            [{"formKey": "F_DM", "siteName": "CLOSED_SITE", "subjectKey": "S1", "data": {}}],
        )
