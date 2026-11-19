"""Unit and simulation tests for async workflow parity and client-mode guards."""

from __future__ import annotations

import asyncio
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from imednet.errors import ApiError, ClientError, ImednetError
from imednet.models import (
    Form,
    Interval,
    Job,
    JobStatus,
    Query,
    Record,
    RecordRevision,
    Subject,
    Variable,
    Visit,
)
from imednet.sdk import AsyncImednetSDK, ImednetSDK
from imednet_workflows._guards import (
    ensure_async_sdk,
    ensure_sync_sdk,
    is_async_only_sdk,
    is_async_sdk,
    is_sync_only_sdk,
)
from imednet_workflows.cached_loader import CachedRecordsLoader
from imednet_workflows.data_extraction import DataExtractionWorkflow
from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow
from imednet_workflows.query_management import QueryManagementWorkflow
from imednet_workflows.record_mapper import RecordMapper
from imednet_workflows.record_update import RecordUpdateWorkflow
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow
from imednet_workflows.study_structure import async_get_study_structure, get_study_structure
from imednet_workflows.subject_data import SubjectDataWorkflow
from imednet_workflows.uat.engine import EditCheckResultStatus, UATExecutionEngine
from imednet_workflows.uat.generator import GeneratedRecordSet
from imednet_workflows.uat.inspector import StudySchemaInspector
from imednet_workflows.uat.models import RecordTestType
from imednet_workflows.uat.orchestrator import UATWorkflow, UATWorkflowState
from imednet_workflows.uat.submission import (
    BulkRecordSubmissionWorkflow,
    BulkSubmissionError,
    SubmissionResult,
)


class _SyncOnlyStub:
    """Minimal synchronous-only facade stub for guard verification."""

    def get_records(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


class _AsyncOnlyStub:
    """Minimal asynchronous-only facade stub for guard verification."""

    async def async_get_records(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_all_nine_workflows_complete_in_async_simulation_mode() -> None:
    """Scenario 1: All 9 registered workflows execute cleanly on AsyncImednetSDK(simulation_mode=True)."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        async with AsyncImednetSDK(simulation_mode=True) as sdk:
            study_key = "DEMO-STUDY"
            sites = list(await sdk.async_get_sites(study_key))
            subjects = list(await sdk.async_get_subjects(study_key))
            forms = list(await sdk.async_get_forms(study_key))
            intervals = list(await sdk.async_get_intervals(study_key))

            site_name = sites[0].site_name or "North Site"
            subject_key = subjects[0].subject_key or "SUBJ-001"
            form_key = forms[0].form_key or "F_DM"
            interval_name = intervals[0].interval_name or "Baseline"

            # 1. record_update (seed records into simulation state first so downstream workflows see records)
            wf_update = sdk.workflows.record_update
            j_reg = await wf_update.async_register_subject(
                study_key,
                form_key,
                "SIM-SUB-101",
                site_name,
                {"V_AGE": 34},
                wait_for_completion=True,
                poll_interval=0,
            )
            j_sched = await wf_update.async_update_scheduled_record(
                study_key,
                form_key,
                subject_key,
                interval_name,
                {"V_AGE": 41},
                wait_for_completion=True,
                poll_interval=0,
            )
            j_new = await wf_update.async_create_new_record(
                study_key,
                form_key,
                subject_key,
                {"V_AGE": 42},
                wait_for_completion=True,
                poll_interval=0,
            )
            j_batch = await wf_update.async_create_or_update_records(
                study_key,
                [
                    {
                        "formKey": form_key,
                        "subjectKey": subject_key,
                        "siteName": site_name,
                        "data": {"V_AGE": 45},
                    }
                ],
                wait_for_completion=True,
                poll_interval=0,
            )
            assert j_reg.batch_id and j_sched.batch_id and j_new.batch_id and j_batch.batch_id

            # 2. data_extraction
            wf_extract = sdk.workflows.data_extraction
            extracted = await wf_extract.async_extract_records_by_criteria(
                study_key,
                subject_filter={"subject_key": subject_key},
                visit_filter={},
            )
            revisions = await wf_extract.async_extract_audit_trail(study_key)
            assert isinstance(extracted, list) and len(extracted) >= 1
            assert isinstance(revisions, list)

            # 3. duckdb_centralizer
            wf_duck = sdk.workflows.duckdb_centralizer
            ingested = await wf_duck.async_ingest_revisions(study_key)
            wf_duck.build_silver_view(study_key)
            assert isinstance(ingested, int)

            # 4. query_management
            wf_queries = sdk.workflows.query_management
            open_q = await wf_queries.async_get_open_queries(study_key)
            subj_q = await wf_queries.async_get_queries_for_subject(study_key, subject_key)
            site_q = await wf_queries.async_get_queries_by_site(study_key, site_name)
            state_counts = await wf_queries.async_get_query_state_counts(study_key)
            assert len(open_q) >= 1
            assert isinstance(subj_q, list)
            assert isinstance(site_q, list)
            assert state_counts["OPEN"] >= 1

            # 5. record_mapper
            wf_mapper = sdk.workflows.record_mapper
            df = await wf_mapper.async_dataframe(study_key, use_labels_as_columns=False)
            streamed = [
                chunk
                async for chunk in wf_mapper.async_iter_dataframes(
                    study_key, use_labels_as_columns=False
                )
            ]
            sdtm_df = await wf_mapper.async_sdtm_dataframe(study_key, domain="DM")
            sdtm_via_flag = await wf_mapper.async_dataframe(study_key, as_sdtm=True, domain="DM")
            sdtm_streamed = [
                chunk
                async for chunk in wf_mapper.async_iter_dataframes(
                    study_key, as_sdtm=True, domain="DM"
                )
            ]
            hierarchy = await wf_mapper.async_build_hierarchy(study_key)
            assert not df.empty
            assert len(streamed) >= 1
            assert not sdtm_df.empty
            assert len(sdtm_via_flag) == len(sdtm_df)
            assert len(sdtm_streamed) >= 1
            assert isinstance(hierarchy, list)

            # 6. register_subjects
            wf_reg = sdk.workflows.register_subjects
            reg_job = await wf_reg.async_register_subjects(
                study_key,
                [Subject(subject_key="SIM-REG-201", site_name=site_name)],
                wait_for_completion=True,
                poll_interval=0,
            )
            assert reg_job.batch_id is not None

            # 7. subject_data
            wf_subj = sdk.workflows.subject_data
            subj_bundle = await wf_subj.async_get_all_subject_data(study_key, subject_key)
            assert subj_bundle.subject_details is not None
            assert subj_bundle.subject_details.subject_key == subject_key
            assert len(subj_bundle.records) >= 1

            # 8. uat_inspector
            wf_inspector = sdk.workflows.uat_inspector
            snapshot = await wf_inspector.async_inspect(study_key)
            assert snapshot.study_key == study_key
            assert len(snapshot.forms) >= 1

            # 9. uat
            wf_uat = sdk.workflows.uat
            assert wf_uat.state == UATWorkflowState.PENDING
            uat_result = await wf_uat.async_run(study_key, subject_count=2)
            assert wf_uat.state == UATWorkflowState.COMPLETED
            assert uat_result.state == UATWorkflowState.COMPLETED
            assert uat_result.poll_summary is not None
            assert len(uat_result.poll_summary.results) >= 1
            assert uat_result.poll_summary.failures == {}


@pytest.mark.asyncio
async def test_record_update_sync_methods_on_async_sdk_fail_before_schema_validator() -> None:
    """Calling sync RecordUpdateWorkflow methods on AsyncImednetSDK raises TypeError before AsyncSchemaValidator."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
            wf = RecordUpdateWorkflow(async_sdk)
            wf._validator = MagicMock()
            wf._validator.refresh = AsyncMock()
            wf._validator.validate_batch = AsyncMock()

            with pytest.raises(TypeError, match="async_register_subject"):
                wf.register_subject("DEMO-STUDY", "F_DM", "SUB-1", "North Site", {"V_AGE": 30})

            with pytest.raises(TypeError, match="async_update_scheduled_record"):
                wf.update_scheduled_record(
                    "DEMO-STUDY", "F_DM", "SUBJ-001", "Baseline", {"V_AGE": 30}
                )

            with pytest.raises(TypeError, match="async_create_new_record"):
                wf.create_new_record("DEMO-STUDY", "F_DM", "SUBJ-001", {"V_AGE": 30})

            with pytest.raises(TypeError, match="async_create_or_update_records"):
                wf.create_or_update_records(
                    "DEMO-STUDY",
                    [{"formKey": "F_DM", "subjectKey": "SUBJ-001", "data": {"V_AGE": 30}}],
                )

            wf._validator.refresh.assert_not_called()
            wf._validator.validate_batch.assert_not_called()


@pytest.mark.asyncio
async def test_subject_data_and_data_extraction_fetch_resources_concurrently() -> None:
    """Verify SubjectDataWorkflow and DataExtractionWorkflow execute independent fetches concurrently."""
    sdk = MagicMock(spec=AsyncImednetSDK)
    all_started = asyncio.Event()
    in_flight = 0

    def make_barrier_mock(result: list[Any]) -> AsyncMock:
        async def _call(*args: Any, **kwargs: Any) -> list[Any]:
            nonlocal in_flight
            in_flight += 1
            if in_flight >= 4:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=0.5)
            return result

        return AsyncMock(side_effect=_call)

    sdk.async_get_subjects = make_barrier_mock([Subject(subject_key="S1", site_name="SiteA")])
    sdk.async_get_visits = make_barrier_mock([Visit(visit_id=10, interval_name="Baseline")])
    sdk.async_get_records = make_barrier_mock([Record(record_id=1, subject_key="S1")])
    sdk.async_get_queries = make_barrier_mock([Query(query_id=99, subject_key="S1")])

    subj_wf = SubjectDataWorkflow(sdk)
    bundle = await subj_wf.async_get_all_subject_data("ST", "S1")
    assert bundle.subject_details is not None
    assert len(bundle.visits) == 1
    assert len(bundle.records) == 1
    assert len(bundle.queries) == 1

    # DataExtractionWorkflow concurrent subject + visit criteria resolution
    criteria_started = asyncio.Event()
    criteria_in_flight = 0

    def make_criteria_mock(result: list[Any]) -> AsyncMock:
        async def _call(*args: Any, **kwargs: Any) -> list[Any]:
            nonlocal criteria_in_flight
            criteria_in_flight += 1
            if criteria_in_flight >= 2:
                criteria_started.set()
            await asyncio.wait_for(criteria_started.wait(), timeout=0.5)
            return result

        return AsyncMock(side_effect=_call)

    sdk.async_get_subjects = make_criteria_mock([Subject(subject_key="S1")])
    sdk.async_get_visits = make_criteria_mock([Visit(visit_id=10, subject_key="S1")])
    sdk.async_get_records = AsyncMock(
        return_value=[Record(record_id=1, subject_key="S1", visit_id=10)]
    )

    ext_wf = DataExtractionWorkflow(sdk)
    records = await ext_wf.async_extract_records_by_criteria(
        "ST",
        record_filter={"form_key": "F_DM"},
        subject_filter={"site_name": "SiteA"},
        visit_filter={"interval_name": "Baseline"},
    )
    assert len(records) == 1
    sdk.async_get_records.assert_awaited_once()

    # Empty subject match short-circuits without fetching records
    sdk.async_get_subjects = AsyncMock(return_value=[])
    sdk.async_get_records.reset_mock()
    empty = await ext_wf.async_extract_records_by_criteria(
        "ST", subject_filter={"site_name": "Missing"}
    )
    assert empty == []
    sdk.async_get_records.assert_not_awaited()


@pytest.mark.asyncio
async def test_data_extraction_async_audit_trail_date_filters() -> None:
    """Verify async_extract_audit_trail normalizes UTC dateCreated bounds and kwargs."""
    rev = RecordRevision(record_id=1, form_id=10, variable_name="V_AGE", revision_number=1)

    sdk = MagicMock(spec=AsyncImednetSDK)
    sdk.async_get_record_revisions = AsyncMock(return_value=[rev])

    wf = DataExtractionWorkflow(sdk)
    out = await wf.async_extract_audit_trail(
        "ST",
        start_date="2026-01-01",
        end_date="2026-02-01T00:00:00",
        user_filter={"role": "data"},
    )
    assert len(out) == 1
    _, call_kwargs = sdk.async_get_record_revisions.call_args
    assert call_kwargs == {
        "role": "data",
        "dateCreated": [
            (">=", "2026-01-01T00:00:00Z"),
            ("<=", "2026-02-01T00:00:00Z"),
        ],
    }

    out2 = await wf.async_extract_audit_trail("ST")
    assert len(out2) == 1


@pytest.mark.asyncio
async def test_record_mapper_async_whitelists_and_sdtm_projection() -> None:
    """Verify async RecordMapper batches multi-form filters, projects SDTM whitelists, and propagates ValueError."""
    sdk = MagicMock(spec=AsyncImednetSDK)
    sdk.async_get_variables = AsyncMock(
        return_value=[
            Variable(variable_name="AGE", label="Age", form_id=10),
            Variable(variable_name="WT", label="Weight", form_id=20),
            Variable(variable_name="IGNORED", label="Ignored", form_id=20),
        ]
    )

    async def _async_get_records(**kwargs: Any) -> list[Record]:
        assert "variableNames" not in kwargs
        assert "formIds" not in kwargs
        if kwargs.get("formId") == 10:
            return [
                Record(
                    record_id=1, subject_key="S1", visit_id=1, form_id=10, record_data={"AGE": 45}
                )
            ]
        return [
            Record(
                record_id=2,
                subject_key="S1",
                visit_id=1,
                form_id=20,
                record_data={"WT": 72, "IGNORED": "x"},
            )
        ]

    sdk.async_get_records = AsyncMock(side_effect=_async_get_records)
    mapper = RecordMapper(sdk)

    df = await mapper.async_dataframe(
        "STUDY",
        use_labels_as_columns=False,
        variable_whitelist=["AGE", "WT"],
        form_whitelist=[10, 20],
    )
    assert list(df.columns) == [
        "recordId",
        "subjectKey",
        "visitId",
        "formId",
        "recordStatus",
        "dateCreated",
        "AGE",
        "WT",
    ]
    assert len(df) == 2

    # Empty whitelists short-circuit without network calls
    sdk.async_get_variables.reset_mock()
    empty_df = await mapper.async_dataframe("STUDY", variable_whitelist=[])
    assert empty_df.empty
    sdk.async_get_variables.assert_not_awaited()

    empty_sdtm = await mapper.async_sdtm_dataframe("STUDY", domain="AE", variable_whitelist=[])
    assert empty_sdtm.empty

    # SDTM variable_whitelist projects record_data before transformation
    sdk.async_get_records = AsyncMock(
        return_value=[
            Record(
                record_id=3,
                subject_key="S1",
                visit_id=1,
                form_id=30,
                record_data={"aeTerm": "Headache", "extraUnwhitelisted": "secret"},
            )
        ]
    )
    sdtm_df = await mapper.async_sdtm_dataframe(
        "STUDY",
        domain="AE",
        variable_whitelist=["aeTerm"],
        form_whitelist=[30],
    )
    assert sdtm_df.iloc[0]["AETERM"] == "Headache"
    assert "extraUnwhitelisted" not in sdtm_df.columns

    # ValueError from invalid filter expression propagates instead of being swallowed
    sdk.async_get_records = AsyncMock(side_effect=ValueError("Invalid filter operator"))
    with pytest.raises(ValueError, match="Invalid filter operator"):
        await mapper._async_fetch_records("STUDY", extra_filters={"badKey!": "val"})


@pytest.mark.asyncio
async def test_cached_records_loader_and_record_mapper_async_integration(tmp_path: Path) -> None:
    """Verify CachedRecordsLoader async initial sync, delta sync, reconciliation, and RecordMapper integration."""
    rec1 = Record(
        study_key="ST",
        record_id=1,
        subject_key="S1",
        visit_id=10,
        form_id=100,
        form_key="F_DM",
        date_modified="2026-01-01 00:00:00",
        record_data={"V_AGE": 30},
    )
    rec2 = Record(
        study_key="ST",
        record_id=2,
        subject_key="S2",
        visit_id=10,
        form_id=100,
        form_key="F_DM",
        date_modified="2026-01-02 00:00:00",
        record_data={"V_AGE": 40},
    )

    sdk = MagicMock(spec=AsyncImednetSDK)
    sdk.records = MagicMock()
    sdk.records._auto_filter_fields = True
    sdk.async_get_records = AsyncMock(return_value=[rec1, rec2])
    sdk.async_get_variables = AsyncMock(
        return_value=[
            Variable(variable_name="V_AGE", label="Age", form_id=100, variable_type="Number")
        ]
    )
    sdk.async_get_intervals = AsyncMock(
        return_value=[
            Interval(
                interval_id=10,
                interval_name="Baseline",
                forms=[{"formId": 100, "formKey": "F_DM"}],
            )
        ]
    )
    sdk.async_get_forms = AsyncMock(
        return_value=[Form(form_id=100, form_key="F_DM", form_name="Demographics")]
    )

    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path, retry_attempts=2)
    loaded = await loader.async_load_records("ST", reconcile=False)
    assert [r.record_id for r in loaded] == [1, 2]

    # Delta sync + reconciliation pruning rec2 when upstream active set only has rec1
    rec1_updated = Record(
        study_key="ST",
        record_id=1,
        subject_key="S1",
        visit_id=10,
        form_id=100,
        form_key="F_DM",
        date_modified="2026-01-03 00:00:00",
        record_data={"V_AGE": 31},
    )
    sdk.records.list = AsyncMock(return_value=[rec1_updated])
    sdk.async_get_records = AsyncMock(return_value=[rec1_updated])
    reconciled = await loader.async_load_records("ST", reconcile=True)
    assert len(reconciled) == 1
    assert reconciled[0].record_data["V_AGE"] == 31

    # RecordMapper backed by CachedRecordsLoader in async mode
    sdk.records.list = AsyncMock(return_value=[])
    sdk.async_get_records = AsyncMock(return_value=[rec1_updated])
    mapper = RecordMapper(sdk, loader=loader)
    df = await mapper.async_dataframe("ST", use_labels_as_columns=True)
    assert list(df["Age"]) == [31]

    tree = await mapper.async_build_hierarchy("ST", use_labels_as_keys=True)
    assert len(tree) == 1
    assert tree[0]["subject_key"] == "S1"


@pytest.mark.asyncio
async def test_uat_submission_engine_and_orchestrator_async_paths() -> None:
    """Verify BulkRecordSubmissionWorkflow.async_submit, UATWorkflow failure capture, and UATExecutionEngine.async_run_verification."""
    # 1. BulkRecordSubmissionWorkflow Phase 1 + Phase 2 + existing subject skip
    sdk = MagicMock(spec=AsyncImednetSDK)
    sdk.async_get_subjects = AsyncMock(return_value=[Subject(subject_key="UAT-EXIST")])
    sdk.async_create_record = AsyncMock(
        side_effect=[
            Job(batch_id="B-REG"),
            Job(batch_id="B-CLIN"),
        ]
    )

    async def _get_completed_job(study_key: str, batch_id: str) -> JobStatus:
        return JobStatus(batch_id=batch_id, state="COMPLETED", progress=100)

    sdk.async_get_job = AsyncMock(side_effect=_get_completed_job)

    submitter = BulkRecordSubmissionWorkflow(sdk, batch_size=10)
    reg_existing = GeneratedRecordSet(
        form_key="F_REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"formKey": "F_REG", "subjectKey": "UAT-EXIST", "data": {}}],
        subject_keys=["UAT-EXIST"],
    )
    reg_new = GeneratedRecordSet(
        form_key="F_REG",
        form_name="Registration",
        test_type=RecordTestType.REGISTER_SUBJECT,
        payloads=[{"formKey": "F_REG", "subjectKey": "UAT-NEW", "data": {}}],
        subject_keys=["UAT-NEW"],
    )
    clin_set = GeneratedRecordSet(
        form_key="F_VS",
        form_name="Vitals",
        test_type=RecordTestType.CREATE_NEW_RECORD,
        payloads=[{"formKey": "F_VS", "subjectKey": "UAT-NEW", "data": {"V_WEIGHT": 70}}],
        subject_keys=["UAT-NEW"],
    )
    sub_res: SubmissionResult = await submitter.async_submit(
        "ST", [reg_existing, reg_new, clin_set]
    )
    assert [b.job.batch_id for b in sub_res.registration_batches] == ["B-REG"]
    assert [b.job.batch_id for b in sub_res.data_batches] == ["B-CLIN"]

    # Phase 1 failure raises BulkSubmissionError
    sdk_fail = MagicMock(spec=AsyncImednetSDK)
    sdk_fail.async_get_subjects = AsyncMock(return_value=[])
    sdk_fail.async_create_record = AsyncMock(return_value=Job(batch_id="B-FAIL"))
    sdk_fail.async_get_job = AsyncMock(
        return_value=JobStatus(batch_id="B-FAIL", state="FAILED", progress=100)
    )
    fail_submitter = BulkRecordSubmissionWorkflow(sdk_fail)
    with pytest.raises(BulkSubmissionError):
        await fail_submitter.async_submit("ST", [reg_new, clin_set])

    # 2. UATWorkflow.async_run captures mid-pipeline failure into FAILED UATRunResult
    uat_wf = UATWorkflow(sdk_fail, poll_interval=0.0)
    sdk_fail.async_get_forms = AsyncMock(side_effect=RuntimeError("boom"))
    sdk_fail.async_get_variables = AsyncMock(return_value=[])
    sdk_fail.async_get_intervals = AsyncMock(return_value=[])
    sdk_fail.async_get_sites = AsyncMock(return_value=[])
    failed_run = await uat_wf.async_run("ST")
    assert uat_wf.state == UATWorkflowState.FAILED
    assert failed_run.state == UATWorkflowState.FAILED
    assert "boom" in (failed_run.error_message or "")

    # 3. UATExecutionEngine.async_run_verification covers Pass, Fail, Error, Skipped
    data_dict = MagicMock()
    data_dict.business_logic = [
        {
            "Name": "R-PASS",
            "Form": "F_VS",
            "Variable": "V_SYSBP",
            "Operator": "<=",
            "Value": "200",
        },
        {
            "Name": "R-FAIL",
            "Form": "F_VS",
            "Variable": "V_SYSBP",
            "Operator": "<=",
            "Value": "200",
        },
        {
            "Name": "R-ERR",
            "Form": "F_VS",
            "Variable": "V_SYSBP",
            "Operator": ">=",
            "Value": "50",
        },
        {
            "Name": "R-SKIP",
            "Form": "F_VS",
            "Variable": "",
            "Operator": "==",
            "Value": "1",
        },
    ]
    eng_sdk = MagicMock(spec=AsyncImednetSDK)
    eng_sdk.async_create_record = AsyncMock(
        side_effect=[
            ClientError("V_SYSBP out of range", status_code=400),
            Job(batch_id="B-OK"),
            ApiError("Internal Server Error", status_code=500),
        ]
    )
    engine = UATExecutionEngine(eng_sdk, data_dict)
    report = await engine.async_run_verification("ST")
    statuses = {r["rule_name"]: r["status"] for r in report.results}
    assert statuses == {
        "R-PASS": EditCheckResultStatus.PASS,
        "R-FAIL": EditCheckResultStatus.FAIL,
        "R-ERR": EditCheckResultStatus.ERROR,
        "R-SKIP": EditCheckResultStatus.SKIPPED,
    }


def test_guards_helper_detection_with_custom_stubs() -> None:
    """Verify _guards helper functions classify real SDKs, mocks, and custom stubs accurately."""
    sync_stub = _SyncOnlyStub()
    async_stub = _AsyncOnlyStub()

    assert is_sync_only_sdk(sync_stub) is True
    assert is_async_only_sdk(sync_stub) is False
    assert is_async_sdk(sync_stub) is False

    assert is_async_only_sdk(async_stub) is True
    assert is_sync_only_sdk(async_stub) is False

    with pytest.raises(TypeError, match="async_do_work"):
        ensure_sync_sdk(async_stub, "MyWorkflow", "do_work", "async_do_work")

    with pytest.raises(TypeError, match="do_work"):
        ensure_async_sdk(sync_stub, "MyWorkflow", "async_do_work", "do_work")


@pytest.mark.asyncio
async def test_bidirectional_client_mode_type_error_guards_across_all_workflows(
    tmp_path: Path,
) -> None:
    """Scenario 3: Every sync method rejects AsyncImednetSDK and every async_* method rejects ImednetSDK."""
    sync_sdk = ImednetSDK(simulation_mode=True)
    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        # 1. DataExtractionWorkflow
        with pytest.raises(TypeError, match="async_extract_records_by_criteria"):
            DataExtractionWorkflow(async_sdk).extract_records_by_criteria("DEMO-STUDY")
        with pytest.raises(TypeError, match="async_extract_audit_trail"):
            DataExtractionWorkflow(async_sdk).extract_audit_trail("DEMO-STUDY")
        with pytest.raises(TypeError, match="extract_records_by_criteria"):
            await DataExtractionWorkflow(sync_sdk).async_extract_records_by_criteria("DEMO-STUDY")
        with pytest.raises(TypeError, match="extract_audit_trail"):
            await DataExtractionWorkflow(sync_sdk).async_extract_audit_trail("DEMO-STUDY")

        # 2. DuckDBIngestionWorkflow
        with pytest.raises(TypeError, match="async_ingest_revisions"):
            DuckDBIngestionWorkflow(async_sdk).ingest_revisions("DEMO-STUDY")
        with pytest.raises(TypeError, match="ingest_revisions"):
            await DuckDBIngestionWorkflow(sync_sdk).async_ingest_revisions("DEMO-STUDY")

        # 3. QueryManagementWorkflow
        with pytest.raises(TypeError, match="async_get_open_queries"):
            QueryManagementWorkflow(async_sdk).get_open_queries("DEMO-STUDY")
        with pytest.raises(TypeError, match="async_get_queries_for_subject"):
            QueryManagementWorkflow(async_sdk).get_queries_for_subject("DEMO-STUDY", "SUBJ-001")
        with pytest.raises(TypeError, match="async_get_queries_by_site"):
            QueryManagementWorkflow(async_sdk).get_queries_by_site("DEMO-STUDY", "North Site")
        with pytest.raises(TypeError, match="async_get_query_state_counts"):
            QueryManagementWorkflow(async_sdk).get_query_state_counts("DEMO-STUDY")

        with pytest.raises(TypeError, match="get_open_queries"):
            await QueryManagementWorkflow(sync_sdk).async_get_open_queries("DEMO-STUDY")
        with pytest.raises(TypeError, match="get_queries_for_subject"):
            await QueryManagementWorkflow(sync_sdk).async_get_queries_for_subject(
                "DEMO-STUDY", "SUBJ-001"
            )
        with pytest.raises(TypeError, match="get_queries_by_site"):
            await QueryManagementWorkflow(sync_sdk).async_get_queries_by_site(
                "DEMO-STUDY", "North Site"
            )
        with pytest.raises(TypeError, match="get_query_state_counts"):
            await QueryManagementWorkflow(sync_sdk).async_get_query_state_counts("DEMO-STUDY")

        # 4. RecordMapper
        with pytest.raises(TypeError, match="async_dataframe"):
            RecordMapper(async_sdk).dataframe("DEMO-STUDY")
        with pytest.raises(TypeError, match="async_iter_dataframes"):
            RecordMapper(async_sdk).iter_dataframes("DEMO-STUDY")
        with pytest.raises(TypeError, match="async_sdtm_dataframe"):
            RecordMapper(async_sdk).sdtm_dataframe("DEMO-STUDY", domain="DM")
        with pytest.raises(TypeError, match="async_iter_sdtm_dataframes"):
            RecordMapper(async_sdk).iter_sdtm_dataframes("DEMO-STUDY", domain="DM")
        with pytest.raises(TypeError, match="async_build_hierarchy"):
            RecordMapper(async_sdk).build_hierarchy("DEMO-STUDY")

        with pytest.raises(TypeError, match="dataframe"):
            await RecordMapper(sync_sdk).async_dataframe("DEMO-STUDY")
        with pytest.raises(TypeError, match="iter_dataframes"):
            async for _ in RecordMapper(sync_sdk).async_iter_dataframes("DEMO-STUDY"):
                pass
        with pytest.raises(TypeError, match="sdtm_dataframe"):
            await RecordMapper(sync_sdk).async_sdtm_dataframe("DEMO-STUDY", domain="DM")
        with pytest.raises(TypeError, match="iter_sdtm_dataframes"):
            async for _ in RecordMapper(sync_sdk).async_iter_sdtm_dataframes(
                "DEMO-STUDY", domain="DM"
            ):
                pass
        with pytest.raises(TypeError, match="build_hierarchy"):
            await RecordMapper(sync_sdk).async_build_hierarchy("DEMO-STUDY")

        # 5. RecordUpdateWorkflow (reverse direction: async_* on sync SDK)
        with pytest.raises(TypeError, match="register_subject"):
            await RecordUpdateWorkflow(sync_sdk).async_register_subject(
                "DEMO-STUDY", "F_DM", "S1", "North Site", {}
            )
        with pytest.raises(TypeError, match="update_scheduled_record"):
            await RecordUpdateWorkflow(sync_sdk).async_update_scheduled_record(
                "DEMO-STUDY", "F_DM", "S1", "Baseline", {}
            )
        with pytest.raises(TypeError, match="create_new_record"):
            await RecordUpdateWorkflow(sync_sdk).async_create_new_record(
                "DEMO-STUDY", "F_DM", "S1", {}
            )
        with pytest.raises(TypeError, match="create_or_update_records"):
            await RecordUpdateWorkflow(sync_sdk).async_create_or_update_records("DEMO-STUDY", [])

        # 6. RegisterSubjectsWorkflow
        with pytest.raises(TypeError, match="async_register_subjects"):
            RegisterSubjectsWorkflow(async_sdk).register_subjects("DEMO-STUDY", [])
        with pytest.raises(TypeError, match="register_subjects"):
            await RegisterSubjectsWorkflow(sync_sdk).async_register_subjects("DEMO-STUDY", [])

        # 7. SubjectDataWorkflow
        with pytest.raises(TypeError, match="async_get_all_subject_data"):
            SubjectDataWorkflow(async_sdk).get_all_subject_data("DEMO-STUDY", "SUBJ-001")
        with pytest.raises(TypeError, match="get_all_subject_data"):
            await SubjectDataWorkflow(sync_sdk).async_get_all_subject_data("DEMO-STUDY", "SUBJ-001")

        # 8. StudySchemaInspector & study_structure
        with pytest.raises(TypeError, match="async_inspect"):
            StudySchemaInspector(async_sdk).inspect("DEMO-STUDY")
        with pytest.raises(TypeError, match="inspect"):
            await StudySchemaInspector(sync_sdk).async_inspect("DEMO-STUDY")

        with pytest.raises(TypeError, match="async_get_study_structure"):
            get_study_structure(async_sdk, "DEMO-STUDY")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="get_study_structure"):
            await async_get_study_structure(sync_sdk, "DEMO-STUDY")  # type: ignore[arg-type]

        # 9. UATWorkflow, BulkRecordSubmissionWorkflow, UATExecutionEngine, CachedRecordsLoader
        empty_sub = SubmissionResult(study_key="DEMO-STUDY", started_at=datetime(2026, 1, 1))
        uat_async = UATWorkflow(async_sdk)
        with pytest.raises(TypeError, match="async_run"):
            uat_async.run("DEMO-STUDY")
        assert uat_async.state == UATWorkflowState.PENDING
        with pytest.raises(TypeError, match="async_inspect"):
            uat_async.inspect("DEMO-STUDY")
        with pytest.raises(TypeError, match="async_submit"):
            uat_async.submit("DEMO-STUDY", [])
        with pytest.raises(TypeError, match="async_monitor"):
            uat_async.monitor("DEMO-STUDY", empty_sub)

        uat_sync = UATWorkflow(sync_sdk)
        with pytest.raises(TypeError, match="run"):
            await uat_sync.async_run("DEMO-STUDY")
        assert uat_sync.state == UATWorkflowState.PENDING
        with pytest.raises(TypeError, match="inspect"):
            await uat_sync.async_inspect("DEMO-STUDY")
        with pytest.raises(TypeError, match="submit"):
            await uat_sync.async_submit("DEMO-STUDY", [])
        with pytest.raises(TypeError, match="monitor"):
            await uat_sync.async_monitor("DEMO-STUDY", empty_sub)

        with pytest.raises(TypeError, match="async_submit"):
            BulkRecordSubmissionWorkflow(async_sdk).submit("DEMO-STUDY", [])
        with pytest.raises(TypeError, match="submit"):
            await BulkRecordSubmissionWorkflow(sync_sdk).async_submit("DEMO-STUDY", [])

        empty_dict = MagicMock()
        empty_dict.business_logic = []
        with pytest.raises(TypeError, match="async_run_verification"):
            UATExecutionEngine(async_sdk, empty_dict).run_verification("DEMO-STUDY")
        with pytest.raises(TypeError, match="run_verification"):
            await UATExecutionEngine(sync_sdk, empty_dict).async_run_verification("DEMO-STUDY")

        with pytest.raises(TypeError, match="async_load_records"):
            CachedRecordsLoader(async_sdk, cache_dir=tmp_path).load_records("DEMO-STUDY")
        with pytest.raises(TypeError, match="async_sync_records"):
            CachedRecordsLoader(async_sdk, cache_dir=tmp_path).sync_records("DEMO-STUDY")
        with pytest.raises(TypeError, match="load_records"):
            await CachedRecordsLoader(sync_sdk, cache_dir=tmp_path).async_load_records("DEMO-STUDY")
        with pytest.raises(TypeError, match="sync_records"):
            await CachedRecordsLoader(sync_sdk, cache_dir=tmp_path).async_sync_records("DEMO-STUDY")

        # Ensure study_structure wraps runtime endpoint errors as ImednetError (not TypeError)
        bad_async = MagicMock(spec=AsyncImednetSDK)
        bad_async.async_get_intervals = AsyncMock(side_effect=RuntimeError("network down"))
        bad_async.async_get_forms = AsyncMock(return_value=[])
        bad_async.async_get_variables = AsyncMock(return_value=[])
        with pytest.raises(ImednetError, match="network down"):
            await async_get_study_structure(bad_async, "DEMO-STUDY")

    sync_sdk.close()
