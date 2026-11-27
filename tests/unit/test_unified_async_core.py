"""Comprehensive unit tests for the unified async-first core engine and symmetric SDK facades."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from imednet import (
    AsyncImednetSDK,
    AsyncMultiStudyOrchestrator,
    ImednetSDK,
    MultiStudyOrchestrator,
    SimulationState,
    SimulationTransport,
)
from imednet.datadict import async_load_from_api, load_from_api
from imednet.datadict.loaders import async_load_from_api as loaders_async_load_from_api
from imednet.integrations import async_export
from imednet.integrations.sink_base import (
    AsyncExportSink,
    SinkConfig,
    async_run_streaming_export,
    get_state_provider,
)
from imednet.models.records import Record
from imednet.utils.async_bridge import (
    SyncToAsyncFacadeAdapter,
    is_async_facade,
    run_sync_over_async,
)


def test_async_load_from_api_reexported_and_type_guards() -> None:
    """async_load_from_api is exported from datadict and loaders, and strict TypeErrors are raised."""
    assert async_load_from_api is loaders_async_load_from_api

    state = SimulationState()
    with ImednetSDK(simulation_mode=True, transport=SimulationTransport(state)) as sync_sdk:
        with pytest.raises(TypeError, match="AsyncImednetFacade"):
            asyncio.run(async_load_from_api(sync_sdk, "STUDY"))  # type: ignore[arg-type]

    async def _check_async_rejected_by_sync_loader() -> None:
        async with AsyncImednetSDK(
            simulation_mode=True,
            transport=SimulationTransport(state),
        ) as async_sdk:
            with pytest.raises(TypeError, match="async_load_from_api"):
                load_from_api(async_sdk, "STUDY")  # type: ignore[arg-type]

    asyncio.run(_check_async_rejected_by_sync_loader())


@pytest.mark.asyncio
async def test_load_from_api_and_async_load_from_api_share_builder_and_work_in_running_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_from_api and async_load_from_api invoke the same builder and work inside running loops."""
    import imednet.datadict.loaders.api as api_loader_mod

    builder_calls: list[str] = []
    real_builder = api_loader_mod._build_datadict_from_items

    def _spy_builder(*args: Any, **kwargs: Any) -> Any:
        builder_calls.append("called")
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(api_loader_mod, "_build_datadict_from_items", _spy_builder)

    state = SimulationState()
    async with AsyncImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(state),
    ) as async_sdk:
        async_dd = await async_load_from_api(async_sdk, "STUDY", no_raw_xml=True)

    # Calling synchronous load_from_api INSIDE an active asyncio event loop (Jupyter / FastAPI)
    with ImednetSDK(simulation_mode=True, transport=SimulationTransport(state)) as sync_sdk:
        sync_dd = load_from_api(sync_sdk, "STUDY", no_raw_xml=True)

        # Simulate single-threaded WebAssembly/Pyodide (sys.platform == "emscripten")
        monkeypatch.setattr(sys, "platform", "emscripten")
        emscripten_dd = load_from_api(sync_sdk, "STUDY", no_raw_xml=True)

    assert len(builder_calls) == 3
    assert async_dd.forms == sync_dd.forms == emscripten_dd.forms
    assert async_dd.form_for_key("F_VS") is not None


def test_sync_to_async_facade_adapter_never_instantiates_async_client() -> None:
    """SyncToAsyncFacadeAdapter reuses the synchronous SDK transport without creating AsyncClient."""
    state = SimulationState()
    with ImednetSDK(simulation_mode=True, transport=SimulationTransport(state)) as sync_sdk:
        adapter = SyncToAsyncFacadeAdapter(sync_sdk)
        assert is_async_facade(adapter) is True
        assert not hasattr(adapter, "_async_client")
        forms = run_sync_over_async(lambda: adapter.get_forms("STUDY"))
        studies = run_sync_over_async(lambda: adapter.get_studies())
        assert len(forms) >= 1
        assert len(studies) >= 1


@pytest.mark.asyncio
async def test_all_seventeen_convenience_methods_symmetric_and_deprecated_aliases() -> None:
    """All 17 convenience methods on AsyncImednetSDK work unprefixed and warn on async_* aliases."""
    state = SimulationState()
    async with AsyncImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(state),
    ) as sdk:
        list_pairs = [
            ("get_codings", "async_get_codings", ("STUDY",)),
            ("get_forms", "async_get_forms", ("STUDY",)),
            ("get_intervals", "async_get_intervals", ("STUDY",)),
            ("get_queries", "async_get_queries", ("STUDY",)),
            ("get_record_revisions", "async_get_record_revisions", ("STUDY",)),
            ("get_records", "async_get_records", ("STUDY",)),
            ("get_sites", "async_get_sites", ("STUDY",)),
            ("get_studies", "async_get_studies", ()),
            ("get_subjects", "async_get_subjects", ("STUDY",)),
            ("get_users", "async_get_users", ("STUDY",)),
            ("get_variables", "async_get_variables", ("STUDY",)),
            ("get_visits", "async_get_visits", ("STUDY",)),
        ]
        for canonical_name, legacy_name, args in list_pairs:
            canonical_res = await getattr(sdk, canonical_name)(*args)
            with pytest.deprecated_call():
                legacy_res = await getattr(sdk, legacy_name)(*args)
            assert len(canonical_res) == len(legacy_res)

        created = await sdk.create_record(
            "STUDY",
            [{"formKey": "F_VS", "subjectKey": "SUBJ-001", "data": {"V_WEIGHT": 70.5}}],
        )
        assert created.batch_id
        with pytest.deprecated_call():
            legacy_created = await sdk.async_create_record(
                "STUDY",
                [{"formKey": "F_VS", "subjectKey": "SUBJ-001", "data": {"V_WEIGHT": 71.0}}],
            )
        assert legacy_created.batch_id

        job_status = await sdk.get_job("STUDY", created.batch_id)
        with pytest.deprecated_call():
            legacy_job_status = await sdk.async_get_job("STUDY", created.batch_id)
        assert job_status.batch_id == legacy_job_status.batch_id

        polled = await sdk.poll_job("STUDY", created.batch_id, interval=0.01, timeout=5.0)
        with pytest.deprecated_call():
            legacy_polled = await sdk.async_poll_job(
                "STUDY", legacy_created.batch_id, interval=0.01, timeout=5.0
            )
        assert polled.state == "COMPLETED"
        assert legacy_polled.state == "COMPLETED"

        polled_many = await sdk.poll_jobs(
            "STUDY", [created.batch_id, legacy_created.batch_id], interval=0.01, timeout=5.0
        )
        with pytest.deprecated_call():
            legacy_polled_many = await sdk.async_poll_jobs(
                "STUDY", [created.batch_id], interval=0.01, timeout=5.0
            )
        assert len(polled_many.results) == 2
        assert len(legacy_polled_many.results) == 1

        batch_summary = await sdk.create_records_batch(
            "STUDY",
            [{"formKey": "F_VS", "subjectKey": "SUBJ-001", "data": {"V_WEIGHT": 72.0}}],
            wait_for_completion=True,
            interval=0.01,
        )
        with pytest.deprecated_call():
            legacy_batch_summary = await sdk.async_create_records_batch(
                "STUDY",
                [{"formKey": "F_VS", "subjectKey": "SUBJ-001", "data": {"V_WEIGHT": 73.0}}],
                wait_for_completion=True,
                interval=0.01,
            )
        assert batch_summary.all_successful is True
        assert legacy_batch_summary.all_successful is True


@pytest.mark.asyncio
async def test_all_nine_workflows_execute_symmetrically_and_sync_wrappers_work_in_event_loop(
    tmp_path: Path,
) -> None:
    """All 9 workflows execute via unprefixed coroutines on AsyncImednetSDK and sync inside an active loop."""
    async_state = SimulationState()
    sync_state = SimulationState()

    async with AsyncImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(async_state),
    ) as async_sdk:
        # 1. data_extraction
        extracted_async = await async_sdk.workflows.data_extraction.extract_records_by_criteria(
            "STUDY"
        )
        audit_async = await async_sdk.workflows.data_extraction.extract_audit_trail("STUDY")

        # 2. duckdb_centralizer
        ingested_async = await async_sdk.workflows.duckdb_centralizer.ingest_revisions("STUDY")
        await async_sdk.workflows.duckdb_centralizer.build_silver_view("STUDY")

        # 3. query_management
        open_q_async = await async_sdk.workflows.query_management.get_open_queries("STUDY")
        counts_async = await async_sdk.workflows.query_management.get_query_state_counts("STUDY")

        # 4. record_mapper
        df_async = await async_sdk.workflows.record_mapper.dataframe("STUDY")
        sdtm_async = await async_sdk.workflows.record_mapper.sdtm_dataframe("STUDY", domain="VS")
        chunks_async = [
            chunk async for chunk in async_sdk.workflows.record_mapper.iter_dataframes("STUDY")
        ]

        # 5. record_update
        upd_job_async = await async_sdk.workflows.record_update.create_or_update_records(
            "STUDY",
            [{"formKey": "F_VS", "subjectKey": "SUBJ-001", "data": {"V_WEIGHT": 68.5}}],
            wait_for_completion=True,
            poll_interval=1,
        )

        # 6. register_subjects
        reg_summary_async = await async_sdk.workflows.register_subjects.register_subjects_batch(
            "STUDY",
            [{"formKey": "F_DM", "siteName": "SITE", "subjectKey": "SUBJ-ASYNC-1", "data": {}}],
            wait_for_completion=True,
            poll_interval=1,
        )

        # 7. subject_data
        subj_async = await async_sdk.workflows.subject_data.get_all_subject_data(
            "STUDY", "SUBJ-001"
        )

        # 8. uat_inspector
        snap_async = await async_sdk.workflows.uat_inspector.inspect("STUDY")

        # 9. uat
        uat_res_async = await async_sdk.workflows.uat.run("STUDY", subject_count=1, dry_run=True)

    # Execute synchronous ImednetSDK workflows directly INSIDE this running asyncio test loop!
    with ImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(sync_state),
    ) as sync_sdk:
        extracted_sync = sync_sdk.workflows.data_extraction.extract_records_by_criteria("STUDY")
        audit_sync = sync_sdk.workflows.data_extraction.extract_audit_trail("STUDY")
        ingested_sync = sync_sdk.workflows.duckdb_centralizer.ingest_revisions("STUDY")
        sync_sdk.workflows.duckdb_centralizer.build_silver_view("STUDY")
        open_q_sync = sync_sdk.workflows.query_management.get_open_queries("STUDY")
        counts_sync = sync_sdk.workflows.query_management.get_query_state_counts("STUDY")
        df_sync = sync_sdk.workflows.record_mapper.dataframe("STUDY")
        sdtm_sync = sync_sdk.workflows.record_mapper.sdtm_dataframe("STUDY", domain="VS")
        chunks_sync = list(sync_sdk.workflows.record_mapper.iter_dataframes("STUDY"))
        upd_job_sync = sync_sdk.workflows.record_update.create_or_update_records(
            "STUDY",
            [{"formKey": "F_VS", "subjectKey": "SUBJ-001", "data": {"V_WEIGHT": 68.5}}],
            wait_for_completion=True,
            poll_interval=1,
        )
        reg_summary_sync = sync_sdk.workflows.register_subjects.register_subjects_batch(
            "STUDY",
            [{"formKey": "F_DM", "siteName": "SITE", "subjectKey": "SUBJ-SYNC-1", "data": {}}],
            wait_for_completion=True,
            poll_interval=1,
        )
        subj_sync = sync_sdk.workflows.subject_data.get_all_subject_data("STUDY", "SUBJ-001")
        snap_sync = sync_sdk.workflows.uat_inspector.inspect("STUDY")
        uat_res_sync = sync_sdk.workflows.uat.run("STUDY", subject_count=1, dry_run=True)

    assert len(extracted_async) == len(extracted_sync)
    assert len(audit_async) == len(audit_sync)
    assert ingested_async == ingested_sync
    assert len(open_q_async) == len(open_q_sync)
    assert counts_async == counts_sync
    assert list(df_async.columns) == list(df_sync.columns)
    assert list(sdtm_async.columns) == list(sdtm_sync.columns)
    assert len(chunks_async) == len(chunks_sync)
    assert upd_job_async.state == upd_job_sync.state == "COMPLETED"
    assert reg_summary_async.all_successful and reg_summary_sync.all_successful
    assert len(subj_async.visits) == len(subj_sync.visits)
    assert len(snap_async.forms) == len(snap_sync.forms)
    assert uat_res_async.dry_run is True and uat_res_sync.dry_run is True


@pytest.mark.asyncio
async def test_async_and_sync_multi_study_orchestrators_and_type_guards() -> None:
    """AsyncMultiStudyOrchestrator and MultiStudyOrchestrator enforce facade types and isolate contexts."""
    from imednet.core.context import get_study_context

    state = SimulationState()
    async with AsyncImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(state),
    ) as async_sdk:
        with pytest.raises(TypeError, match="AsyncMultiStudyOrchestrator"):
            MultiStudyOrchestrator(async_sdk)  # type: ignore[arg-type]

        active_studies = await AsyncMultiStudyOrchestrator(
            async_sdk, max_workers=2
        ).resolve_active_studies()
        assert len(active_studies) >= 1
        target_study = active_studies[0]

        seen_contexts: dict[str, str | None] = {}

        async def _async_worker(study_key: str, facade: Any, _logger: Any) -> int:
            seen_contexts[study_key] = get_study_context()
            forms = await facade.get_forms(study_key)
            return len(forms)

        orchestrator = AsyncMultiStudyOrchestrator(async_sdk, max_workers=2)
        results = await orchestrator.execute_pipeline(_async_worker, whitelist={target_study})
        assert results[target_study]["status"] == "SUCCESS"
        assert seen_contexts[target_study] == target_study

    with ImednetSDK(simulation_mode=True, transport=SimulationTransport(state)) as sync_sdk:
        with pytest.raises(TypeError, match="MultiStudyOrchestrator"):
            AsyncMultiStudyOrchestrator(sync_sdk)  # type: ignore[arg-type]

        sync_contexts: dict[str, str | None] = {}

        def _sync_worker(study_key: str, facade: Any, _logger: Any) -> int:
            sync_contexts[study_key] = get_study_context()
            return len(facade.get_forms(study_key))

        # Run synchronous MultiStudyOrchestrator INSIDE the active event loop!
        sync_results = MultiStudyOrchestrator(sync_sdk, max_workers=2).execute_pipeline(
            _sync_worker,
            whitelist={target_study},
        )
        assert sync_results[target_study]["status"] == "SUCCESS"
        assert sync_contexts[target_study] == target_study


@pytest.mark.asyncio
async def test_async_export_sink_streaming_phi_reconciliation_checkpoint_and_dispatcher(
    tmp_path: Path,
) -> None:
    """async_run_streaming_export enforces PHI redaction, voided reconciliation, and watermark commits."""

    class _RecordingAsyncSink(AsyncExportSink):
        reconciles_deleted_on_full_sync = True

        def __init__(self, config: SinkConfig) -> None:
            super().__init__(config)
            self.written_batches: list[list[Any]] = []
            self.reconciled_batches: list[list[Any]] = []
            self.flushed = False
            self.closed = False

        async def write_batch(self, records: Any, *, batch_id: str) -> int:
            batch = list(records)
            self.written_batches.append(batch)
            return len(batch)

        async def reconcile_deleted(self, records: Any, *, batch_id: str) -> int:
            batch = list(records)
            self.reconciled_batches.append(batch)
            return len(batch)

        async def flush(self) -> None:
            self.flushed = True

        async def close(self) -> None:
            self.closed = True

    ts1 = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    ts2 = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)

    active_rec = Record(
        study_key="STUDY",
        record_id=101,
        form_id=1,
        form_key="F_DM",
        subject_key="SUBJ-001",
        date_modified=ts1.isoformat(),
        deleted=False,
        record_data={"patient_name": "Jane Doe", "V_AGE": 34},
    )
    voided_rec = Record(
        study_key="STUDY",
        record_id=102,
        form_id=1,
        form_key="F_DM",
        subject_key="SUBJ-001",
        date_modified=ts2.isoformat(),
        deleted=True,
        record_data={"patient_name": "Jane Doe", "V_AGE": 35},
    )

    state = SimulationState()
    async with AsyncImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(state),
    ) as async_sdk:
        # Stub records.list to yield our active + voided records
        async def _fake_list(*_args: Any, **_kwargs: Any) -> Any:
            yield active_rec
            yield voided_rec

        async_sdk.records.list = _fake_list  # type: ignore[method-assign]
        provider = get_state_provider(tmp_path / "ledger.json")
        cfg = SinkConfig(
            study_key="STUDY",
            batch_size=10,
            mask_phi=True,
            incremental=True,
            idempotent=True,
            state_provider=provider,
        )
        sink_holder: list[_RecordingAsyncSink] = []

        def _make_sink() -> _RecordingAsyncSink:
            inst = _RecordingAsyncSink(cfg)
            sink_holder.append(inst)
            return inst

        written = await async_run_streaming_export(
            async_sdk,
            "STUDY",
            _make_sink,
            cfg,
            stream_name="custom_async_stream",
        )
        assert written == 1
        sink_inst = sink_holder[0]
        assert sink_inst.flushed is True
        assert sink_inst.closed is True
        assert len(sink_inst.written_batches) == 1
        assert sink_inst.written_batches[0][0].record_data["patient_name"] == "***MASKED***"
        assert len(sink_inst.reconciled_batches) == 1
        assert sink_inst.reconciled_batches[0][0].record_id == 102
        assert provider.get_last_timestamp("STUDY", "custom_async_stream") == ts2

        csv_out = tmp_path / "records_async.csv"
        await async_export("csv", async_sdk, "STUDY", path=csv_out)
        assert csv_out.is_file()

    with ImednetSDK(simulation_mode=True, transport=SimulationTransport(state)) as sync_sdk:
        with pytest.raises(TypeError, match="AsyncImednetFacade"):
            await async_run_streaming_export(
                sync_sdk,  # type: ignore[arg-type]
                "STUDY",
                lambda: _RecordingAsyncSink(SinkConfig(study_key="STUDY")),
                SinkConfig(study_key="STUDY"),
            )
        with pytest.raises(TypeError, match="AsyncImednetFacade"):
            await async_export(
                "csv",
                sync_sdk,  # type: ignore[arg-type]
                "STUDY",
                path=tmp_path / "bad.csv",
            )


@pytest.mark.asyncio
async def test_async_bridge_edge_cases_and_adapter_offload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise is_async_facade, call_async_facade_method, maybe_to_thread, and SyncToAsyncFacadeAdapter."""
    from unittest.mock import AsyncMock, MagicMock

    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
    from imednet.utils.async_bridge import call_async_facade_method, maybe_to_thread

    assert is_async_facade(None) is False
    assert is_async_facade(object()) is False

    mock_sync = MagicMock(spec=ImednetFacade)
    assert is_async_facade(mock_sync) is False

    mock_async = MagicMock(spec=AsyncImednetFacade)
    assert is_async_facade(mock_async) is True

    mock_legacy = MagicMock()
    mock_legacy.async_get_forms = AsyncMock(return_value=["f1"])
    assert is_async_facade(mock_legacy) is True
    assert await call_async_facade_method(mock_legacy, "get_forms", "S") == ["f1"]

    class _LegacyStub:
        async def async_get_forms(self, study_key: str) -> list[str]:
            return [study_key]

    stub = _LegacyStub()
    assert is_async_facade(stub) is True
    assert await call_async_facade_method(stub, "get_forms", "S1") == ["S1"]

    class _AsyncGenEpStub:
        class _Forms:
            async def list(self) -> Any:
                yield "item"

        forms = _Forms()

    assert is_async_facade(_AsyncGenEpStub()) is True

    class _SyncStub:
        def get_forms(self, study_key: str) -> list[str]:
            return [f"sync-{study_key}"]

    assert is_async_facade(_SyncStub()) is False
    assert await call_async_facade_method(_SyncStub(), "get_forms", "S2") == ["sync-S2"]

    with pytest.raises(AttributeError):
        await call_async_facade_method(object(), "get_forms")

    # maybe_to_thread on emscripten and thread-exhaustion fallback
    monkeypatch.setattr(sys, "platform", "emscripten")
    assert await maybe_to_thread(lambda x: x + 1, 4) == 5
    monkeypatch.undo()

    async def _raise_cant_start(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(asyncio, "to_thread", _raise_cant_start)
    assert await maybe_to_thread(lambda x: x * 2, 6) == 12
    monkeypatch.undo()

    # run_sync_over_async error propagation inside active loop
    with pytest.raises(ValueError, match="boom-bridge"):
        run_sync_over_async(lambda: asyncio.sleep(0, result=None) if False else _fail_coro())

    # SyncToAsyncFacadeAdapter offload_threads=True & endpoint/convenience delegation
    state = SimulationState()
    with ImednetSDK(simulation_mode=True, transport=SimulationTransport(state)) as sync_sdk:
        offload_adapter = SyncToAsyncFacadeAdapter(sync_sdk, offload_threads=True)
        assert offload_adapter.sync_sdk is sync_sdk
        _ = offload_adapter.auth
        _ = offload_adapter.config
        with offload_adapter.study_context("STUDY") as bound:
            assert bound is offload_adapter

        forms_via_ep = [f async for f in offload_adapter.forms.list(study_key="STUDY")]
        assert len(forms_via_ep) >= 1
        assert len(await offload_adapter.get_codings("STUDY")) >= 0
        assert len(await offload_adapter.get_intervals("STUDY")) >= 0
        assert len(await offload_adapter.get_queries("STUDY")) >= 0
        assert len(await offload_adapter.get_record_revisions("STUDY")) >= 0
        assert len(await offload_adapter.get_records("STUDY")) >= 0
        assert len(await offload_adapter.get_sites("STUDY")) >= 0
        assert len(await offload_adapter.get_subjects("STUDY")) >= 0
        assert len(await offload_adapter.get_users("STUDY")) >= 0
        assert len(await offload_adapter.get_variables("STUDY")) >= 1
        assert len(await offload_adapter.get_visits("STUDY")) >= 0

        created = await offload_adapter.records.create(
            "STUDY",
            [{"formKey": "F_VS", "subjectKey": "SUBJ-001", "data": {"V_WEIGHT": 70.0}}],
        )
        got = await offload_adapter.jobs.get("STUDY", created.batch_id)
        polled = await offload_adapter.jobs.poll("STUDY", created.batch_id, interval=0.01)
        polled_many = await offload_adapter.jobs.poll_many(
            "STUDY", [created.batch_id], interval=0.01
        )
        assert got.batch_id == polled.batch_id
        assert len(polled_many.results) == 1
        assert (await offload_adapter.get_job("STUDY", created.batch_id)).batch_id == got.batch_id
        assert (
            await offload_adapter.poll_job("STUDY", created.batch_id, interval=0.01)
        ).state == "COMPLETED"
        assert (
            len(
                (
                    await offload_adapter.poll_jobs("STUDY", [created.batch_id], interval=0.01)
                ).results
            )
            == 1
        )
        batch_res = await offload_adapter.create_records_batch(
            "STUDY",
            [{"formKey": "F_VS", "subjectKey": "SUBJ-001", "data": {"V_WEIGHT": 71.0}}],
            interval=0.01,
        )
        assert batch_res.all_successful is True

    # Mock study_context and missing endpoint attribute handling
    bare_mock = MagicMock(spec=ImednetFacade)
    mock_adapter = SyncToAsyncFacadeAdapter(bare_mock)
    with pytest.raises(AttributeError):
        _ = mock_adapter.forms
    with pytest.raises(AttributeError):
        _ = mock_adapter.nonexistent_attr


async def _fail_coro() -> None:
    raise ValueError("boom-bridge")


@pytest.mark.asyncio
async def test_orchestrator_backpressure_filters_failures_and_datadict_fallbacks(
    tmp_path: Path,
) -> None:
    """Cover AdaptiveConcurrencyLimiter, AsyncMultiStudyOrchestrator error/filter paths, and dispatcher targets."""
    from unittest.mock import AsyncMock, MagicMock

    from imednet.errors import FilterConflictError
    from imednet.integrations.dispatcher import (
        export,
        register_async_sink_target,
        register_async_tabular_target,
    )
    from imednet.orchestration.orchestrator import AdaptiveConcurrencyLimiter

    limiter = AdaptiveConcurrencyLimiter(3)
    limiter.acquire()
    limiter.release()
    await limiter.acquire_async()
    await limiter.release_async()
    limiter.record_latency(0.05)  # baseline
    limiter.record_latency(0.25)  # spike -> reduce concurrency from 3 -> 2
    limiter.record_latency(0.01)  # recovery -> increase concurrency back to 3

    with pytest.raises(ValueError, match="max_workers"):
        AsyncMultiStudyOrchestrator(MagicMock(spec=[]), max_workers=0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="AsyncMultiStudyOrchestrator"):
        AsyncMultiStudyOrchestrator(object())  # type: ignore[arg-type]

    class _StudyItem:
        def __init__(self, key: str) -> None:
            self.study_key = key

    class _FacadeOnlyAsyncSDK:
        async def get_studies(self) -> list[_StudyItem]:
            return [_StudyItem("S_OK"), _StudyItem("S_FAIL"), _StudyItem("S_SKIP")]

    facade_sdk: Any = _FacadeOnlyAsyncSDK()
    orch = AsyncMultiStudyOrchestrator(facade_sdk, max_workers=2)
    assert orch.sdk is facade_sdk
    assert orch.max_workers == 2

    with pytest.raises(FilterConflictError):
        await orch.resolve_active_studies(whitelist={"S_OK"}, blacklist={"S_SKIP"})

    filtered = await orch.resolve_active_studies(blacklist={"S_SKIP"})
    assert filtered == ["S_OK", "S_FAIL"]
    assert await orch.execute_pipeline(AsyncMock(), whitelist={"NO_MATCH"}) == {}

    async def _worker(study_key: str, _sdk: Any, _log: Any) -> str:
        if study_key == "S_FAIL":
            raise RuntimeError("study boom")
        return f"done-{study_key}"

    summary = await orch.execute_pipeline(_worker, blacklist={"S_SKIP"})
    assert summary["S_OK"]["status"] == "SUCCESS"
    assert summary["S_OK"]["data"] == "done-S_OK"
    assert summary["S_FAIL"]["status"] == "FAILED"
    assert "study boom" in str(summary["S_FAIL"]["error"])

    # Dispatcher async tabular, parquet prefetch, export TypeError, and unsupported target
    state = SimulationState()
    async with AsyncImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(state),
    ) as async_sdk:
        with pytest.raises(TypeError, match="async_export"):
            export("csv", async_sdk, "STUDY", path=tmp_path / "nope.csv")  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="Unsupported export target"):
            await async_export("unknown_target_xyz", async_sdk, "STUDY")

        pq_path = tmp_path / "out.parquet"
        await async_export("parquet", async_sdk, "STUDY", path=pq_path)
        assert pq_path.exists()

        register_async_tabular_target(
            "custom_async_tab",
            lambda _sdk, sk, **kw: f"{sk}:{kw.get('tag', 'ok')}",
        )
        assert await async_export("custom_async_tab", async_sdk, "STUDY", tag="v1") == "STUDY:v1"

        class _NoopAsyncSink(AsyncExportSink):
            async def write_batch(self, records: Any, *, batch_id: str) -> int:
                _ = batch_id
                return len(list(records))

            async def flush(self) -> None:
                pass

            async def close(self) -> None:
                pass

        register_async_sink_target("custom_async_sink", _NoopAsyncSink)
        sink_count = await async_export("custom_async_sink", async_sdk, "STUDY")
        assert sink_count >= 0

    # Data dictionary loader fallback paths (facade-only async stub & invalid object)
    with pytest.raises(TypeError):
        await async_load_from_api(object(), "STUDY")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        load_from_api(object(), "STUDY")  # type: ignore[arg-type]

    mock_dd_sdk = MagicMock()
    mock_dd_sdk.async_get_forms = AsyncMock(return_value=[])
    mock_dd_sdk.async_get_variables = AsyncMock(return_value=[])
    dd = await async_load_from_api(mock_dd_sdk, "STUDY")
    assert dd.forms == []


@pytest.mark.asyncio
async def test_async_bounded_form_buffer_router_and_scoped_seq_overrides() -> None:
    """Exercise AsyncBoundedFormBufferRouter batching, ceiling eviction, and _ScopedSeqOverrides."""
    from unittest.mock import AsyncMock

    from imednet.integrations.sink_base import (
        AsyncBoundedFormBufferRouter,
        _ScopedSeqOverrides,
    )

    with pytest.raises(ValueError, match="batch_size"):
        AsyncBoundedFormBufferRouter(["F1"], batch_size=0, on_flush_active=AsyncMock())
    with pytest.raises(ValueError, match="batch_size"):
        AsyncBoundedFormBufferRouter(["F1"], batch_size=True, on_flush_active=AsyncMock())  # type: ignore[arg-type]

    active_flushes: list[tuple[str, list[Any], int]] = []
    voided_flushes: list[tuple[str, list[Any], int]] = []

    async def _on_active(fk: str, batch: list[Any], idx: int) -> None:
        active_flushes.append((fk, list(batch), idx))

    async def _on_voided(fk: str, batch: list[Any], idx: int) -> None:
        voided_flushes.append((fk, list(batch), idx))

    # Router without voided callback ignores add_voided safely
    no_void_router = AsyncBoundedFormBufferRouter(
        ["F1", "F2"],
        batch_size=2,
        max_buffered_records=3,
        on_flush_active=_on_active,
        on_flush_voided=None,
    )
    await no_void_router.add_voided("F1", {"id": 999})
    await no_void_router._flush_voided_form("F1")
    await no_void_router._flush_active_form("F1")
    assert no_void_router.total_buffered == 0

    # Batch-size flush + global ceiling eviction for both active and voided buffers
    router = AsyncBoundedFormBufferRouter(
        ["F1", "F2", "F3"],
        batch_size=4,
        max_buffered_records=3,
        on_flush_active=_on_active,
        on_flush_voided=_on_voided,
    )
    await router._flush_voided_form("F1")  # empty buffer no-op
    await router.add_active("F1", "a1")
    await router.add_active("F1", "a2")
    # Adding 3rd item hits ceiling=3 -> evicts largest ("F1" active with 2 items)
    await router.add_active("F2", "b1")
    assert ("F1", ["a1", "a2"], 0) in active_flushes
    assert router.total_buffered == 1

    # Now make voided buffer largest when ceiling is hit
    await router.add_voided("F3", "v1")
    await router.add_voided("F3", "v2")
    assert ("F3", ["v1", "v2"], 0) in voided_flushes

    # Direct batch_size threshold flush on active and voided
    small_router = AsyncBoundedFormBufferRouter(
        ["FA", "FB"],
        batch_size=2,
        max_buffered_records=10,
        on_flush_active=_on_active,
        on_flush_voided=_on_voided,
    )
    await small_router.add_active("FA", "x1")
    await small_router.add_active("FA", "x2")
    await small_router.add_voided("FB", "vx1")
    await small_router.add_voided("FB", "vx2")
    await small_router.add_active("FB", "rem_a")
    await small_router.add_voided("FA", "rem_v")
    await small_router.flush_all()
    assert small_router.total_buffered == 0
    assert small_router.peak_buffered >= 2

    # _ScopedSeqOverrides unambiguous vs ambiguous bare record_id lookups
    overrides = _ScopedSeqOverrides()
    overrides_any: Any = overrides
    overrides.set_scoped("AE", "SUBJ-1", "100", 3)
    overrides.set_scoped("VS", "SUBJ-1", "100", 3)  # same seq_val -> stays unambiguous
    overrides.set_scoped("AE", "SUBJ-1", "200", 1)
    overrides.set_scoped("CM", "SUBJ-1", "200", 5)  # conflicting seq_val -> None for bare lookup

    assert ("AE", "SUBJ-1", "100") in overrides_any
    assert ("AE", "SUBJ-1", "999") not in overrides_any
    assert "AE:SUBJ-1:100" in overrides
    assert "100" in overrides
    assert overrides["100"] == 3
    assert overrides[("CM", "SUBJ-1", "200")] == 5
    assert "200" not in overrides
    assert 12345 not in overrides_any
    with pytest.raises(KeyError):
        _ = overrides["200"]
    assert overrides.get("200", -1) == -1
    assert overrides.get(("AE", "SUBJ-1", "100")) == 3


@pytest.mark.asyncio
async def test_async_export_sink_retry_sanitization_and_error_exit() -> None:
    """Verify AsyncExportSink._execute_with_retry error sanitization, retries, and __aexit__."""
    from imednet.errors import ExportBatchError, ExportConfigurationError

    class _RetryTestSink(AsyncExportSink):
        def __init__(self, config: SinkConfig) -> None:
            super().__init__(config)
            self.flushed = False
            self.closed = False

        async def write_batch(self, records: Any, *, batch_id: str) -> int:
            _ = batch_id
            return len(records)

        async def flush(self) -> None:
            self.flushed = True

        async def close(self) -> None:
            self.closed = True

    cfg = SinkConfig(study_key="STUDY", max_retries=2, retry_backoff=0.0)
    sink = _RetryTestSink(cfg)
    assert await sink.reconcile_deleted([{"id": 1}], batch_id="v0") == 0

    # Transient failure succeeds on 2nd attempt
    calls = 0

    async def _flaky_op() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("temporary network blip")
        return 7

    assert await sink._execute_with_retry("write", "b-ok", _flaky_op) == 7
    assert calls == 2

    # Non-retryable / terminal errors wrap in ExportBatchError with sanitized causes
    synth_pg = "postgresql://u:" + "sec" + "ret@db/prod"
    synth_mongo = "mongodb://admin:" + "pw" + "123@host:27017/db"
    for exc_to_raise, expected_cause_cls in [
        (ExportConfigurationError(f"bad dsn {synth_pg}"), ExportConfigurationError),
        (TypeError("bad type SSN 123-45-6789"), TypeError),
        (ValueError("bad value alice@example.com"), ValueError),
        (RuntimeError(f"driver failed {synth_mongo}"), RuntimeError),
    ]:

        async def _fail_op(err: Exception = exc_to_raise) -> int:
            raise err

        no_retry_sink = _RetryTestSink(
            SinkConfig(study_key="STUDY", max_retries=0, retry_backoff=0.0)
        )
        with pytest.raises(ExportBatchError) as exc_info:
            await no_retry_sink._execute_with_retry("write", "b-err", _fail_op)
        cause = exc_info.value.__cause__
        assert isinstance(cause, expected_cause_cls)
        cause_str = str(cause)
        assert "secret" not in cause_str
        assert "pw123" not in cause_str
        assert "123-45-6789" not in cause_str
        assert "alice@example.com" not in cause_str

    # __aexit__ skips flush() when an exception occurred inside context block
    err_sink = _RetryTestSink(cfg)
    with pytest.raises(RuntimeError, match="abort-export"):
        async with err_sink:
            raise RuntimeError("abort-export")
    assert err_sink.flushed is False
    assert err_sink.closed is True


@pytest.mark.asyncio
async def test_streaming_export_sdtm_standards_watermarks_and_sync_adapter(
    tmp_path: Path,
) -> None:
    """Cover SDTM domain filtering, standards readiness, watermark holdback, and sync adapter."""
    from unittest.mock import AsyncMock, MagicMock

    from imednet.errors import ExportError
    from imednet.integrations.sink_base import (
        ExportSink,
        _async_try_fetch_study_forms,
        run_streaming_export,
    )

    class _CollectAsyncSink(AsyncExportSink):
        reconciles_deleted_on_full_sync = True

        def __init__(self, config: SinkConfig) -> None:
            super().__init__(config)
            self.batches: list[list[Any]] = []
            self.voided: list[list[Any]] = []
            self.bound_meta: dict[str, Any] | None = None

        def bind_stream_metadata(self, metadata: dict[str, Any]) -> None:
            self.bound_meta = metadata

        async def write_batch(self, records: Any, *, batch_id: str) -> int:
            _ = batch_id
            items = list(records)
            self.batches.append(items)
            return len(items)

        async def reconcile_deleted(self, records: Any, *, batch_id: str) -> int:
            _ = batch_id
            items = list(records)
            self.voided.append(items)
            return len(items)

        async def flush(self) -> None:
            pass

        async def close(self) -> None:
            pass

    state = SimulationState()
    async with AsyncImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(state),
    ) as async_sdk:
        # 1. Matched SDTM domain ("VS") with concrete sink instance + standards_profile check
        with pytest.raises(ValueError, match="Unknown standards_profile"):
            SinkConfig(study_key="STUDY", standards_profile="INVALID_PROFILE_NAME")

        vs_cfg = SinkConfig(
            study_key="STUDY",
            sdtm_domain="VS",
            standards_profile="general",
            min_standards_score=0.0,
            batch_size=5,
        )
        vs_sink = _CollectAsyncSink(vs_cfg)
        vs_count = await async_run_streaming_export(async_sdk, "STUDY", vs_sink, vs_cfg)
        assert vs_count >= 0

        # 2. Unmatched SDTM domain ("ZZ") when study_forms exist -> empty stream
        zz_cfg = SinkConfig(study_key="STUDY", sdtm_domain="ZZ")
        zz_sink = _CollectAsyncSink(zz_cfg)
        assert await async_run_streaming_export(async_sdk, "STUDY", zz_sink, zz_cfg) == 0

        # 3. Failing min_standards_score raises ExportError
        strict_cfg = SinkConfig(
            study_key="STUDY",
            standards_profile="drug",
            min_standards_score=100.0,
        )
        bad_rec = {
            "recordId": 501,
            "formKey": "AE",
            "subjectKey": "SUBJ-001",
            "record_data": {},
        }
        with pytest.raises(ExportError, match="Standards readiness check failed"):
            await async_run_streaming_export(
                async_sdk,
                "STUDY",
                _CollectAsyncSink(strict_cfg),
                strict_cfg,
                records_source=lambda _wm, _excl: [bad_rec],
            )

        # 4. Voided batch threshold flush + rejected candidate watermark holdback
        ts1 = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
        ts3 = datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc)
        r_ok1 = Record(
            study_key="STUDY",
            record_id=1,
            form_id=1,
            form_key="F_VS",
            subject_key="S1",
            date_modified=ts1.isoformat(),
            deleted=False,
            record_data={"V_WEIGHT": 70},
        )
        r_reject = Record(
            study_key="STUDY",
            record_id=2,
            form_id=1,
            form_key="F_VS",
            subject_key="S1",
            date_modified=ts2.isoformat(),
            deleted=False,
            record_data={"V_WEIGHT": -999},
        )
        r_ok3 = Record(
            study_key="STUDY",
            record_id=3,
            form_id=1,
            form_key="F_VS",
            subject_key="S1",
            date_modified=ts3.isoformat(),
            deleted=False,
            record_data={"V_WEIGHT": 72},
        )
        v_rec1 = Record(
            study_key="STUDY",
            record_id=4,
            form_id=1,
            form_key="F_VS",
            subject_key="S1",
            date_modified=ts1.isoformat(),
            deleted=True,
            record_data={},
        )
        v_rec2 = Record(
            study_key="STUDY",
            record_id=5,
            form_id=1,
            form_key="F_VS",
            subject_key="S1",
            date_modified=ts1.isoformat(),
            deleted=True,
            record_data={},
        )

        provider = get_state_provider(tmp_path / "holdback_ledger.json")
        hb_cfg = SinkConfig(
            study_key="STUDY",
            batch_size=2,
            incremental=True,
            idempotent=True,
            state_provider=provider,
        )
        hb_sink = _CollectAsyncSink(hb_cfg)

        async def _custom_gate(_sdk: Any, _sk: str, stream: Any, _cfg: SinkConfig) -> Any:
            async for item in stream:
                # Yield a copy for id=3 to exercise fallback record_id matching in _pop_accepted_candidate
                if item.record_id == 2:
                    continue
                if item.record_id == 3:
                    yield item.model_copy()
                else:
                    yield item

        async def _custom_batches(stream: Any, size: int) -> Any:
            buf: list[Any] = []
            async for item in stream:
                buf.append(item)
                if len(buf) >= size:
                    yield list(buf)
                    buf.clear()
            if buf:
                yield list(buf)

        written_hb = await async_run_streaming_export(
            async_sdk,
            "STUDY",
            hb_sink,
            hb_cfg,
            stream_name="hb_stream",
            records_source=lambda _wm, _excl: [v_rec1, v_rec2, r_ok1, r_reject, r_ok3],
            quality_gate_fn=_custom_gate,
            iter_batches_fn=_custom_batches,
        )
        assert written_hb == 2
        assert len(hb_sink.voided) == 1
        assert hb_sink.bound_meta is not None
        # Watermark held back to <= ts2 (so ts1, NOT ts3!) because r_reject at ts2 was rejected
        assert provider.get_last_timestamp("STUDY", "hb_stream") == ts1

        # Rejecting a record without timestamp prevents advancing watermark at all
        no_ts_rec = Record(
            study_key="STUDY",
            record_id=9,
            form_id=1,
            form_key="F_VS",
            subject_key="S1",
            deleted=False,
            record_data={},
        )
        await async_run_streaming_export(
            async_sdk,
            "STUDY",
            _CollectAsyncSink(hb_cfg),
            hb_cfg,
            stream_name="no_ts_reject",
            records_source=lambda _wm, _excl: [r_ok1, no_ts_rec],
            quality_gate_fn=lambda _s, _sk, items, _c: [x for x in items if x.record_id != 9],
            iter_batches_fn=lambda items, _sz: [list(items)],
        )
        assert provider.get_last_timestamp("STUDY", "no_ts_reject") is None

        # 5. run_streaming_export rejects AsyncImednetSDK
        class _MiniSyncSink(ExportSink):
            def write_batch(self, records: Any, *, batch_id: str) -> int:
                _ = batch_id
                return len(records)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        with pytest.raises(TypeError, match="async_run_streaming_export"):
            run_streaming_export(
                async_sdk,  # type: ignore[arg-type]
                "STUDY",
                _MiniSyncSink(SinkConfig(study_key="STUDY")),
                SinkConfig(study_key="STUDY"),
            )

    # 6. Facade-only async stub with empty study_forms + sdtm_domain + OR filter deduplication
    class _FacadeRecordsSDK:
        async def get_forms(self, study_key: str) -> list[Any]:
            _ = study_key
            return []

        async def get_records(self, study_key: str, **_kwargs: Any) -> list[Any]:
            _ = study_key
            return [
                Record(
                    study_key="STUDY",
                    record_id=77,
                    form_id=10,
                    form_key="AE",
                    subject_key="S1",
                    deleted=False,
                    record_data={"AETERM": "Headache"},
                )
            ]

    facade_sdtm_cfg = SinkConfig(
        study_key="STUDY",
        sdtm_domain="AE",
        record_filter="formKey==AE,formKey==F_AE",
        record_data_filter="AETERM==Headache",
    )
    facade_sink = _CollectAsyncSink(facade_sdtm_cfg)
    assert (
        await async_run_streaming_export(
            _FacadeRecordsSDK(),  # type: ignore[arg-type]
            "STUDY",
            facade_sink,
            facade_sdtm_cfg,
        )
        == 1
    )

    # 7. _SyncExportSinkAdapter fallback without __enter__/__exit__
    class _BareSyncSink:
        reconciles_deleted_on_full_sync = True

        def __init__(self, cfg: SinkConfig) -> None:
            self.config = cfg
            self.flushed = False
            self.closed = False
            self.bound = False

        def bind_stream_metadata(self, _meta: dict[str, Any]) -> None:
            self.bound = True

        def write_batch(self, records: Any, *, batch_id: str) -> int:
            _ = batch_id
            return len(records)

        def reconcile_deleted(self, records: Any, *, batch_id: str) -> int:
            _ = batch_id
            return len(records)

        def flush(self) -> None:
            self.flushed = True

        def close(self) -> None:
            self.closed = True

    with ImednetSDK(simulation_mode=True, transport=SimulationTransport(state)) as sync_sdk:
        bare_cfg = SinkConfig(
            study_key="STUDY",
            incremental=True,
            idempotent=True,
            state_provider=get_state_provider(tmp_path / "sync_bare.json"),
        )
        bare_sink = _BareSyncSink(bare_cfg)
        cnt = run_streaming_export(
            sync_sdk,
            "STUDY",
            bare_sink,  # type: ignore[arg-type]
            bare_cfg,
            records_source=lambda _wm, _excl: [r_ok1, v_rec1],
        )
        assert cnt == 1
        assert bare_sink.flushed is True
        assert bare_sink.closed is True
        assert bare_sink.bound is True

        # _async_try_fetch_study_forms helper edge cases
        assert await _async_try_fetch_study_forms(None, "STUDY") == []
        assert (
            len(await _async_try_fetch_study_forms(SyncToAsyncFacadeAdapter(sync_sdk), "STUDY"))
            >= 1
        )

    mock_forms_sdk = MagicMock()
    mock_forms_sdk.async_get_forms = AsyncMock(return_value=[{"formKey": "F_MOCK"}])
    assert len(await _async_try_fetch_study_forms(mock_forms_sdk, "STUDY")) == 1

    class _AwaitableFormsEP:
        class _EP:
            async def list(self, study_key: str) -> list[dict[str, str]]:
                return [{"formKey": study_key}]

        forms = _EP()

    assert await _async_try_fetch_study_forms(_AwaitableFormsEP(), "S1") == [{"formKey": "S1"}]


def test_enrollment_utilities_all_normalization_validation_and_filtering_branches(
    tmp_path: Path,
) -> None:
    """Exercise all branches of imednet.utils.enrollment."""
    import json
    from unittest.mock import MagicMock

    import pandas as pd

    from imednet.models.records import RecordData, RegisterSubjectRequest
    from imednet.models.sites import ELIGIBLE_SITE_ENROLLMENT_STATUSES, Site
    from imednet.models.subjects import Subject
    from imednet.utils.enrollment import (
        extract_candidate_subject_key,
        filter_existing_subject_registrations,
        normalize_subject_registration_inputs,
        validate_local_subject_registration_sites,
        validate_subject_registration_sites,
    )

    # 1. extract_candidate_subject_key
    assert extract_candidate_subject_key({"subjectKey": "  S-01 "}) == "S-01"
    assert (
        extract_candidate_subject_key(
            {"data": {"CUSTOM_ID": " C-99 "}}, subject_key_field="CUSTOM_ID"
        )
        == "C-99"
    )
    assert (
        extract_candidate_subject_key({"data": {"CUSTOM_ID": "   "}}, subject_key_field="CUSTOM_ID")
        is None
    )
    assert extract_candidate_subject_key({"data": {"SUBJID": "SUBJ-77"}}) == "SUBJ-77"
    assert extract_candidate_subject_key({"data": {}}) is None

    # 2. normalize_subject_registration_inputs: JSON list, single dict, CLI envelope error, CSV, DataFrame
    cli_env_file = tmp_path / "cli_envelope.json"
    cli_env_file.write_text(
        json.dumps({"schemaVersion": "1.0", "resource": "subjects", "items": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="CLI list response envelope"):
        normalize_subject_registration_inputs(cli_env_file)

    json_single = tmp_path / "single_subj.json"
    json_single.write_text(
        json.dumps({"subjectKey": "S-JSON", "data": {"AGE": 41}}),
        encoding="utf-8",
    )
    norm_json = normalize_subject_registration_inputs(
        json_single,
        default_form_key="F_DM",
        default_site_name="SITE-A",
    )
    assert len(norm_json) == 1
    assert norm_json[0][1].form_key == "F_DM"
    assert norm_json[0][1].site_name == "SITE-A"

    csv_file = tmp_path / "subjs.csv"
    csv_file.write_text("subjectKey,AGE\nS-CSV,52\n", encoding="utf-8")
    norm_csv = normalize_subject_registration_inputs(
        csv_file,
        default_form_key="F_DM",
        default_site_id=10,
    )
    assert len(norm_csv) == 1
    assert norm_csv[0][1].site_id == 10

    df_in = pd.DataFrame([{"subjectKey": "S-DF", "AGE": 29}])
    norm_df = normalize_subject_registration_inputs(
        df_in,
        default_form_key="F_DM",
        default_site_name="SITE-DF",
    )
    assert len(norm_df) == 1
    assert norm_df[0][1].site_name == "SITE-DF"

    # Model patching & schema formId resolution & invalid item TypeError
    req_missing = RegisterSubjectRequest.model_construct(
        form_key="",
        site_name=None,
        site_id=None,
        data=RecordData(root={"AGE": 30}),
    )
    norm_model_name = normalize_subject_registration_inputs(
        [req_missing],
        default_form_key="F_DM",
        default_site_name="SITE-M",
    )
    assert norm_model_name[0][1].form_key == "F_DM"
    assert norm_model_name[0][1].site_name == "SITE-M"

    norm_model_id = normalize_subject_registration_inputs(
        [req_missing],
        default_form_key="F_DM",
        default_site_id=42,
    )
    assert norm_model_id[0][1].site_id == 42

    fake_schema = MagicMock()
    fake_schema.form_key_from_id.return_value = "F_ENROLL"
    norm_fid = normalize_subject_registration_inputs(
        [{"form_id": 55, "data": {"AGE": 44}}],
        default_site_id=99,
        schema=fake_schema,
    )
    assert norm_fid[0][1].form_key == "F_ENROLL"
    assert norm_fid[0][1].site_id == 99

    with pytest.raises(TypeError, match="RegisterSubjectRequest or dict"):
        normalize_subject_registration_inputs([12345])  # type: ignore[list-item]

    # 3. validate_local_subject_registration_sites & validate_subject_registration_sites
    validate_local_subject_registration_sites(norm_json)
    missing_site_pair: list[tuple[dict[str, Any], RegisterSubjectRequest]] = [
        (
            {},
            RegisterSubjectRequest.model_construct(
                form_key="F_DM", site_name=None, site_id=None, data={}
            ),
        )
    ]
    with pytest.raises(ValueError, match="siteName is required"):
        validate_local_subject_registration_sites(missing_site_pair)

    eligible_status = next(iter(ELIGIBLE_SITE_ENROLLMENT_STATUSES))
    sites = [
        Site(site_id=1, site_name="OPEN-SITE", site_enrollment_status=eligible_status),
        Site(site_id=2, site_name="CLOSED-SITE", site_enrollment_status="Closed"),
    ]
    ok_pairs = normalize_subject_registration_inputs(
        [
            {"formKey": "F_DM", "siteName": "OPEN-SITE", "data": {}},
            {"formKey": "F_DM", "siteId": 1, "data": {}},
        ]
    )
    validate_subject_registration_sites(ok_pairs, sites)

    bad_pairs: list[tuple[dict[str, Any], RegisterSubjectRequest]] = [
        (
            {},
            RegisterSubjectRequest.model_construct(
                form_key="F_DM", site_name="MISSING-SITE", site_id=None, data={}
            ),
        ),
        (
            {},
            RegisterSubjectRequest.model_construct(
                form_key="F_DM", site_name="CLOSED-SITE", site_id=None, data={}
            ),
        ),
        (
            {},
            RegisterSubjectRequest.model_construct(
                form_key="F_DM", site_name=None, site_id=999, data={}
            ),
        ),
        (
            {},
            RegisterSubjectRequest.model_construct(
                form_key="F_DM", site_name=None, site_id=2, data={}
            ),
        ),
        (
            {},
            RegisterSubjectRequest.model_construct(
                form_key="F_DM", site_name=None, site_id=None, data={}
            ),
        ),
    ]
    with pytest.raises(ValueError) as site_err:
        validate_subject_registration_sites(bad_pairs, sites)
    err_msg = str(site_err.value)
    assert "site 'MISSING-SITE' not found" in err_msg
    assert "site 'CLOSED-SITE' is not eligible" in err_msg
    assert "siteId 999 not found" in err_msg
    assert "siteId 2 is not eligible" in err_msg
    assert "Index 4: siteName is required" in err_msg

    # 4. filter_existing_subject_registrations (deleted ignored, active & batch-dups skipped)
    existing_subjs = [
        Subject.model_construct(
            subject_key="S-DELETED-BOOL", deleted=True, subject_status="Active"
        ),  # type: ignore[arg-type]
        Subject.model_construct(subject_key="S-DELETED-STATUS", subject_status="Deleted"),
        Subject.model_construct(
            subject_key="S-ACTIVE", subject_oid="OID-ACTIVE", subject_status="Enrolled"
        ),
    ]
    cand_pairs = [
        (
            {
                "formKey": "F_DM",
                "siteName": "OPEN-SITE",
                "subjectKey": "S-DELETED-BOOL",
                "subjectOid": " OID-1 ",
            },
            RegisterSubjectRequest.model_validate(
                {"formKey": "F_DM", "siteName": "OPEN-SITE", "subjectKey": "   ", "data": {}}
            ),
        ),
        (
            {"formKey": "F_DM", "siteName": "OPEN-SITE", "subjectKey": "S-DELETED-STATUS"},
            RegisterSubjectRequest.model_validate(
                {
                    "formKey": "F_DM",
                    "siteName": "OPEN-SITE",
                    "subjectKey": "S-DELETED-STATUS",
                    "data": {},
                }
            ),
        ),
        (
            {"formKey": "F_DM", "siteName": "OPEN-SITE", "subjectKey": "S-ACTIVE"},
            RegisterSubjectRequest.model_validate(
                {"formKey": "F_DM", "siteName": "OPEN-SITE", "subjectKey": "S-ACTIVE", "data": {}}
            ),
        ),
        (
            {"formKey": "F_DM", "siteName": "OPEN-SITE", "subjectKey": "S-DELETED-STATUS"},
            RegisterSubjectRequest.model_validate(
                {
                    "formKey": "F_DM",
                    "siteName": "OPEN-SITE",
                    "subjectKey": "S-DELETED-STATUS",
                    "data": {},
                }
            ),
        ),
    ]
    filtered_payloads, skipped = filter_existing_subject_registrations(
        cand_pairs,
        existing_subjs,
        should_filter=True,
    )
    assert skipped == 2  # S-ACTIVE + duplicate S-DELETED-STATUS
    assert len(filtered_payloads) == 2
    assert filtered_payloads[0]["subjectKey"] == "S-DELETED-BOOL"
    assert filtered_payloads[0]["subjectOid"] == "OID-1"


@pytest.mark.asyncio
async def test_datadict_api_loader_dispatcher_and_convenience_edge_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover remaining branches in datadict/loaders/api.py, dispatcher.py, and sdk_convenience.py."""
    import dataclasses
    from unittest.mock import MagicMock

    from pydantic import BaseModel

    import imednet.datadict.loaders.api as api_mod
    import imednet.integrations.dispatcher as disp_mod
    from imednet.integrations.dispatcher import (
        ExportRegistry,
        _PrefetchedListEndpoint,
        _PrefetchedSyncFacade,
        register_config_target,
    )

    # 1. datadict/loaders/api.py helpers and edge cases
    assert api_mod.can_load_from_api(None) is False
    assert api_mod._extract_attr({"formKey": "FK_CAMEL"}, "form_key") == "FK_CAMEL"
    assert api_mod._is_truthy_flag("Yes") is True
    assert api_mod._is_truthy_flag("false") is False
    assert api_mod._is_truthy_flag(0) is False

    class _ExtraModel(BaseModel):
        form_key: str = "F1"
        custom_tag: str = "tag-val"
        empty_field: str | None = None

    class _CustomObj:
        def __init__(self) -> None:
            self.form_key = "F2"
            self.extra_prop = "hello"
            self.null_prop = None
            self._hidden = "secret"

    assert api_mod._extract_extra_attributes(_ExtraModel(), api_mod._EXCLUDED_FORM_EXTRA_KEYS) == {
        "customTag": "tag-val"
    }
    assert api_mod._extract_extra_attributes(_CustomObj(), api_mod._EXCLUDED_FORM_EXTRA_KEYS) == {
        "extraProp": "hello"
    }
    assert api_mod._extract_extra_attributes(42, set()) == {}

    dd_built = api_mod._build_datadict_from_items(
        "STUDY",
        [{"formKey": "F_NO_ID", "formName": "No ID Form", "customMeta": None}],
        [
            # Retired variable for unknown form creates empty form container
            {"formKey": "F_RETIRED_NEW", "formId": 11, "variableName": "V_RET1", "deleted": "yes"},
            # Retired variable for existing form without formId backfills formId
            {"formKey": "F_NO_ID", "formId": 22, "variableName": "V_RET2", "disabled": True},
            # Missing variable name skipped
            {"formKey": "F_NO_ID", "variableName": ""},
            # String required + unassigned form
            {"formKey": "", "variableName": "V_UNASSIGNED", "required": "Conditional"},
            # Boolean required False
            {"formKey": "F_NO_ID", "variableName": "V_OPT", "required": False},
        ],
    )
    assert dd_built.form_for_key("F_RETIRED_NEW") is not None
    assert dd_built.form_for_key("F_RETIRED_NEW").variables == []  # type: ignore[union-attr]
    assert dd_built.form_for_key("F_NO_ID").formId == 22  # type: ignore[union-attr]
    assert dd_built.form_for_key("UNASSIGNED") is not None

    # _fetch_async_collection branches: Mock endpoints/facade, slotted objects, sync iterable, None
    mock_ep_sdk = MagicMock()
    mock_ep_sdk.forms.list.return_value = [{"formKey": "FM1"}]
    assert await api_mod._fetch_async_collection(
        mock_ep_sdk,
        "S",
        endpoint_attr="forms",
        facade_method="get_forms",
        legacy_async_method="async_get_forms",
    ) == [{"formKey": "FM1"}]

    mock_fac_sdk = MagicMock()
    mock_fac_sdk.get_forms.return_value = None
    assert (
        await api_mod._fetch_async_collection(
            mock_fac_sdk,
            "S",
            endpoint_attr="forms",
            facade_method="get_forms",
            legacy_async_method="async_get_forms",
        )
        == []
    )
    assert (
        await api_mod._fetch_async_collection(
            MagicMock(),
            "S",
            endpoint_attr="forms",
            facade_method="get_forms",
            legacy_async_method="async_get_forms",
        )
        == []
    )

    class _SlottedFacade:
        __slots__ = ()

        def get_forms(self, study_key: str) -> int:
            _ = study_key
            return 123  # non-iterable fallback

    assert (
        await api_mod._fetch_async_collection(
            _SlottedFacade(),
            "S",
            endpoint_attr="forms",
            facade_method="get_forms",
            legacy_async_method="async_get_forms",
        )
        == []
    )

    class _InstLegacySDK:
        pass

    inst_legacy = _InstLegacySDK()
    inst_legacy.__dict__["async_get_forms"] = lambda study_key: [{"formKey": study_key}]
    assert await api_mod._fetch_async_collection(
        inst_legacy,
        "S_INST",
        endpoint_attr="forms",
        facade_method="get_forms",
        legacy_async_method="async_get_forms",
    ) == [{"formKey": "S_INST"}]

    class _ClassLegacySDK:
        async def async_get_forms(self, study_key: str) -> list[dict[str, str]]:
            return [{"formKey": study_key}]

    assert await api_mod._fetch_async_collection(
        _ClassLegacySDK(),
        "S_CLS",
        endpoint_attr="forms",
        facade_method="get_forms",
        legacy_async_method="async_get_forms",
    ) == [{"formKey": "S_CLS"}]
    assert (
        await api_mod._fetch_async_collection(
            object(),
            "S",
            endpoint_attr="forms",
            facade_method="get_forms",
            legacy_async_method="async_get_forms",
        )
        == []
    )

    # 2. dispatcher.py: register_config_target, entry_points, _PrefetchedSyncFacade, async_export(config=...)
    @dataclasses.dataclass
    class _CustomCfg(SinkConfig):
        custom_opt: str = "default"

    register_config_target("custom_cfg_test_target", _CustomCfg)
    assert disp_mod._registry.get_config_class("custom_cfg_test_target") is _CustomCfg

    iso_reg = ExportRegistry()

    class _GoodEP:
        name = "ep_async_good"

        @staticmethod
        def load() -> Any:
            return AsyncExportSink

    class _BadEP:
        name = "ep_async_bad"

        @staticmethod
        def load() -> Any:
            raise ImportError("missing driver")

    monkeypatch.setattr(disp_mod, "entry_points", lambda group: [_GoodEP(), _BadEP()])
    assert iso_reg.get_async_sink("ep_async_good") is AsyncExportSink
    assert iso_reg.get_async_sink("ep_async_bad") is None
    monkeypatch.undo()

    ep_list = _PrefetchedListEndpoint(
        [
            {"recordId": 1, "formId": 10, "deleted": False},
            {"recordId": 2, "formId": 20, "deleted": False},
            {"recordId": 3, "formId": 10, "deleted": True},
        ]
    )
    assert len(list(ep_list.list(deleted=False, formId=10))) == 1

    state = SimulationState()
    async with AsyncImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(state),
    ) as async_sdk:
        pf = _PrefetchedSyncFacade(
            async_sdk,
            forms=[{"formKey": "F_VS"}],
            variables=[{"variableName": "V_WEIGHT"}],
            records=[
                {"recordId": 1, "deleted": False},
                {"recordId": 2, "deleted": True},
            ],
        )
        assert pf.config is async_sdk.config
        assert pf.auth is async_sdk.auth
        with pf.study_context("STUDY") as bound_pf:
            assert bound_pf is pf
        assert len(pf.get_forms("STUDY")) == 1
        assert len(pf.get_variables("STUDY")) == 1
        assert len(pf.get_records("STUDY", deleted=False)) == 1
        assert len(pf.get_records("STUDY")) == 2

        json_out = tmp_path / "prefetched.json"
        await async_export(
            "json",
            async_sdk,
            "STUDY",
            path=json_out,
            config=SinkConfig(study_key="STUDY"),
        )
        assert json_out.is_file()

        # 3. sdk_convenience.py: skip_validation=True and resolve_results=True
        j1 = await async_sdk.create_record(
            "STUDY",
            [{"formKey": "F_VS", "subjectKey": "SUBJ-001", "data": {"V_WEIGHT": 74.0}}],
            skip_validation=True,
        )
        with pytest.deprecated_call():
            j2 = await async_sdk.async_create_record(
                "STUDY",
                [{"formKey": "F_VS", "subjectKey": "SUBJ-001", "data": {"V_WEIGHT": 75.0}}],
                skip_validation=True,
            )
        assert j1.batch_id is not None and j2.batch_id is not None
        p1 = await async_sdk.poll_job(
            "STUDY", j1.batch_id, interval=0.01, timeout=5.0, resolve_results=True
        )
        pm1 = await async_sdk.poll_jobs(
            "STUDY", [j1.batch_id], interval=0.01, timeout=5.0, resolve_results=True
        )
        with pytest.deprecated_call():
            p2 = await async_sdk.async_poll_job(
                "STUDY", j2.batch_id, interval=0.01, timeout=5.0, resolve_results=True
            )
        with pytest.deprecated_call():
            pm2 = await async_sdk.async_poll_jobs(
                "STUDY", [j2.batch_id], interval=0.01, timeout=5.0, resolve_results=True
            )
        assert p1.state == p2.state == "COMPLETED"
        assert len(pm1.results) == len(pm2.results) == 1
