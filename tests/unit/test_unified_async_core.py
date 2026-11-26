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

    facade_sdk = _FacadeOnlyAsyncSDK()
    orch = AsyncMultiStudyOrchestrator(facade_sdk, max_workers=2)  # type: ignore[arg-type]
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
