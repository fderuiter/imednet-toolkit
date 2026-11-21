"""Unit tests for mode-safe plugin protocols, bound sink helpers, and PEP 561 markers."""

from __future__ import annotations

import tomllib
import warnings
from importlib.metadata import EntryPoint
from importlib.metadata import entry_points as real_entry_points
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig
from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig
from imednet_sinks.plugin import SinksNamespace, create_sinks
from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

import imednet
import imednet_workflows
from imednet import (
    AsyncImednetSDK,
    AsyncSinksNamespaceProtocol,
    AsyncWorkflowsNamespaceProtocol,
    ImednetSDK,
    SinksNamespaceProtocol,
    SyncWorkflowsNamespaceProtocol,
)
from imednet.auth.api_key import ApiKeyAuth
from imednet.core.client import Client
from imednet.models.jobs import Job
from imednet.plugins import PluginProtocol, SinksPluginProtocol
from imednet.simulation import reset_default_simulation_state
from imednet.spi.validation import DataDictionary
from imednet.utils.batch_coordinator import BatchCoordinator
from imednet_workflows import (
    DataExtractionWorkflow,
    RecordUpdateWorkflow,
    RegisterSubjectsWorkflow,
    StudySchemaInspector,
    UATWorkflow,
    Workflows,
)

SYNC_ONLY_WORKFLOWS = (
    "data_extraction",
    "duckdb_centralizer",
    "query_management",
    "record_mapper",
    "subject_data",
    "uat",
)

ASYNC_CAPABLE_WORKFLOWS = (
    "record_update",
    "register_subjects",
    "uat_inspector",
)


@pytest.fixture(autouse=True)
def _reset_sim_state() -> Any:
    reset_default_simulation_state()
    yield
    reset_default_simulation_state()


def test_sync_sdk_workflows_and_sinks_satisfy_protocols() -> None:
    sdk = ImednetSDK(simulation_mode=True)
    try:
        assert isinstance(sdk.workflows, SyncWorkflowsNamespaceProtocol)
        assert not isinstance(sdk.workflows, AsyncWorkflowsNamespaceProtocol)
        for wf_name in (*SYNC_ONLY_WORKFLOWS, *ASYNC_CAPABLE_WORKFLOWS):
            wf = getattr(sdk.workflows, wf_name)
            assert wf is getattr(sdk.workflows, wf_name)

        ns = Workflows(sdk)
        assert isinstance(ns, SyncWorkflowsNamespaceProtocol)
        assert isinstance(Workflows, PluginProtocol)
        assert isinstance(create_sinks, SinksPluginProtocol)
        assert sdk.sinks is not None
        assert isinstance(sdk.sinks, SinksNamespaceProtocol)
    finally:
        sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_name", SYNC_ONLY_WORKFLOWS)
async def test_async_sdk_rejects_sync_only_workflows_before_instantiation(
    workflow_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    instantiated = False

    class _SyncOnlyProbe:
        def __init__(self, _sdk: object) -> None:
            nonlocal instantiated
            instantiated = True

    fake_ep = MagicMock(spec=EntryPoint)
    fake_ep.name = workflow_name
    fake_ep.load.return_value = _SyncOnlyProbe

    def _patched_eps(*, group: str, name: str | None = None) -> Any:
        if group != "imednet.workflows":
            return (
                real_entry_points(group=group, name=name)
                if name is not None
                else real_entry_points(group=group)
            )
        return [fake_ep] if name in (None, workflow_name) else []

    monkeypatch.setattr("imednet.sdk.entry_points", _patched_eps)

    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        with pytest.raises(TypeError, match=rf"Workflow '{workflow_name}' is synchronous-only"):
            getattr(async_sdk.workflows, workflow_name)
        assert not instantiated


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_name", SYNC_ONLY_WORKFLOWS)
async def test_async_sdk_rejects_builtin_sync_only_workflows(workflow_name: str) -> None:
    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        with pytest.raises(TypeError, match=rf"Workflow '{workflow_name}' is synchronous-only"):
            getattr(async_sdk.workflows, workflow_name)


@pytest.mark.asyncio
async def test_async_sdk_loads_async_capable_workflows_and_caches_them() -> None:
    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        assert isinstance(async_sdk.workflows, AsyncWorkflowsNamespaceProtocol)
        assert not isinstance(async_sdk.workflows, SyncWorkflowsNamespaceProtocol)
        for wf_name in ASYNC_CAPABLE_WORKFLOWS:
            first = getattr(async_sdk.workflows, wf_name)
            second = getattr(async_sdk.workflows, wf_name)
            assert first is second
        assert isinstance(async_sdk.workflows.record_update, RecordUpdateWorkflow)
        assert isinstance(async_sdk.workflows.register_subjects, RegisterSubjectsWorkflow)
        assert isinstance(async_sdk.workflows.uat_inspector, StudySchemaInspector)


@pytest.mark.asyncio
async def test_async_sdk_custom_workflow_requires_supports_async_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instantiated_sync_cls = False

    class _CustomSyncWorkflow:
        def __init__(self, _sdk: object) -> None:
            nonlocal instantiated_sync_cls
            instantiated_sync_cls = True

    def _sync_factory(_sdk: object) -> object:
        return object()

    class _AsyncCustomWorkflow:
        supports_async = True

        def __init__(self, sdk: object) -> None:
            self.sdk = sdk

    def _fake_eps(*, group: str, name: str | None = None) -> Any:
        if group != "imednet.workflows":
            return (
                real_entry_points(group=group, name=name)
                if name is not None
                else real_entry_points(group=group)
            )
        ep_sync_cls = MagicMock(spec=EntryPoint)
        ep_sync_cls.name = "custom_sync_cls"
        ep_sync_cls.load.return_value = _CustomSyncWorkflow
        ep_sync_fn = MagicMock(spec=EntryPoint)
        ep_sync_fn.name = "custom_sync_fn"
        ep_sync_fn.load.return_value = _sync_factory
        ep_async = MagicMock(spec=EntryPoint)
        ep_async.name = "custom_async"
        ep_async.load.return_value = _AsyncCustomWorkflow
        all_eps = [ep_sync_cls, ep_sync_fn, ep_async]
        return [ep for ep in all_eps if name is None or ep.name == name]

    monkeypatch.setattr("imednet.sdk.entry_points", _fake_eps)

    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        with pytest.raises(TypeError, match="custom_sync_cls"):
            _ = async_sdk.workflows.custom_sync_cls
        assert not instantiated_sync_cls

        with pytest.raises(TypeError, match="custom_sync_fn"):
            _ = async_sdk.workflows.custom_sync_fn

        loaded = async_sdk.workflows.custom_async
        assert isinstance(loaded, _AsyncCustomWorkflow)
        assert loaded.sdk is async_sdk


@pytest.mark.asyncio
async def test_register_subjects_sync_methods_raise_type_error_on_async_sdk() -> None:
    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        wf = async_sdk.workflows.register_subjects
        with pytest.raises(TypeError, match="async_register_subjects_batch"):
            wf.register_subjects_batch(
                "STUDY", [{"formKey": "F_DM", "siteName": "SITE", "data": {}}]
            )
        with pytest.raises(TypeError, match="async_register_subjects"):
            wf.register_subjects("STUDY", [{"formKey": "F_DM", "siteName": "SITE", "data": {}}])

        summary = await wf.async_register_subjects_batch(
            "STUDY",
            [{"formKey": "F_DM", "siteName": "SITE", "subjectKey": "SUBJ-NEW", "data": {}}],
            wait_for_completion=True,
            poll_interval=1,
        )
        assert summary.all_successful is True

        job = await wf.async_register_subjects(
            "STUDY",
            [{"formKey": "F_DM", "siteName": "SITE", "subjectKey": "SUBJ-NEW-2", "data": {}}],
            wait_for_completion=True,
            poll_interval=1,
        )
        assert isinstance(job, Job)


@pytest.mark.asyncio
async def test_record_update_sync_methods_raise_type_error_without_runtime_warning() -> None:
    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        wf = async_sdk.workflows.record_update
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            with pytest.raises(TypeError, match="_async_validate_form_key"):
                wf._validate_form_key("STUDY", [{"formKey": "F_VS", "data": {"V_WEIGHT": 70.0}}])
            with pytest.raises(TypeError, match="async_create_or_update_records_batch"):
                wf.create_or_update_records_batch(
                    "STUDY", [{"formKey": "F_VS", "data": {"V_WEIGHT": 70.0}}]
                )
            with pytest.raises(TypeError, match="async_create_or_update_records"):
                wf.create_or_update_records(
                    "STUDY", [{"formKey": "F_VS", "data": {"V_WEIGHT": 70.0}}]
                )
            with pytest.raises(TypeError, match="async_register_subject"):
                wf.register_subject("STUDY", "F_DM", "SITE", {"V_AGE": 30})
            with pytest.raises(TypeError, match="async_update_scheduled_record"):
                wf.update_scheduled_record(
                    "STUDY", "F_VS", "SUBJ-001", "Baseline", {"V_WEIGHT": 70}
                )
            with pytest.raises(TypeError, match="async_create_new_record"):
                wf.create_new_record("STUDY", "F_VS", "SUBJ-001", {"V_WEIGHT": 70})


@pytest.mark.asyncio
async def test_record_update_async_single_record_helpers_execute_and_poll() -> None:
    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        wf = async_sdk.workflows.record_update

        reg_job = await wf.async_register_subject(
            study_key="STUDY",
            form_identifier="F_DM",
            site_identifier="SITE",
            data={"V_AGE": 42, "V_GENDER": "F"},
            wait_for_completion=True,
            poll_interval=1,
        )
        assert isinstance(reg_job, Job)
        assert reg_job.batch_id

        sched_job = await wf.async_update_scheduled_record(
            study_key="STUDY",
            form_identifier="F_VS",
            subject_identifier="SUBJ-001",
            interval_identifier="Baseline",
            data={"V_WEIGHT": 71.2},
            wait_for_completion=True,
            poll_interval=1,
        )
        assert isinstance(sched_job, Job)
        assert sched_job.batch_id

        unsched_job = await wf.async_create_new_record(
            study_key="STUDY",
            form_identifier="F_VS",
            subject_identifier="SUBJ-001",
            data={"V_WEIGHT": 72.4},
            wait_for_completion=True,
            poll_interval=1,
        )
        assert isinstance(unsched_job, Job)
        assert unsched_job.batch_id


@pytest.mark.asyncio
async def test_record_update_single_record_helpers_forward_explicit_schema() -> None:
    dd = DataDictionary(forms=[], variables=[])

    sdk = ImednetSDK(simulation_mode=True)
    try:
        wf_sync = sdk.workflows.record_update
        spy_sync = MagicMock(return_value=Job(batch_id="B-SYNC", state="COMPLETED"))
        wf_sync.create_or_update_records = spy_sync  # type: ignore[method-assign]
        wf_sync.register_subject("STUDY", "F_DM", "SITE", {"V_AGE": 30}, schema=dd)
        assert spy_sync.call_args.kwargs["schema"] is dd
    finally:
        sdk.close()

    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        wf_async = async_sdk.workflows.record_update
        captured: dict[str, Any] = {}

        async def _fake_async_submit(**kwargs: Any) -> Job:
            captured.update(kwargs)
            return Job(batch_id="B-ASYNC", state="COMPLETED")

        wf_async.async_create_or_update_records = _fake_async_submit  # type: ignore[method-assign]
        res = await wf_async.async_create_new_record(
            "STUDY", "F_VS", "SUBJ-001", {"V_WEIGHT": 75.0}, schema=dd
        )
        assert res.batch_id == "B-ASYNC"
        assert captured["schema"] is dd


@pytest.mark.parametrize(
    "submit_kwargs",
    [
        {},
        {"collect_errors": True},
        {"dry_run": True},
        {"skip_validation": True},
    ],
)
def test_batch_coordinator_rejects_async_validate_batch_fn_across_all_branches(
    submit_kwargs: dict[str, Any],
) -> None:
    async def _async_validate(_study_key: str, _records: list[dict[str, Any]]) -> None:
        return None

    def _wrapped_coro_factory(study_key: str, records: list[dict[str, Any]]) -> Any:
        return _async_validate(study_key, records)

    coordinator = BatchCoordinator(chunk_size=10)
    records = [{"formKey": "F_VS", "data": {"V_WEIGHT": 70.0}}]
    submit_chunk = MagicMock(return_value=Job(batch_id="B1", state="COMPLETED"))

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with pytest.raises(TypeError, match="AsyncBatchCoordinator"):
            coordinator.submit(
                "STUDY",
                records,
                submit_chunk,
                validate_batch_fn=_async_validate,  # type: ignore[arg-type]
                **submit_kwargs,
            )
        if not submit_kwargs.get("skip_validation"):
            with pytest.raises(TypeError, match="AsyncBatchCoordinator"):
                coordinator.submit(
                    "STUDY",
                    records,
                    submit_chunk,
                    validate_batch_fn=_wrapped_coro_factory,
                    **submit_kwargs,
                )
    submit_chunk.assert_not_called()


@pytest.mark.asyncio
async def test_study_schema_inspector_inspect_rejects_async_sdk_even_when_cache_warm() -> None:
    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        inspector = async_sdk.workflows.uat_inspector
        snap = await inspector.async_inspect("STUDY")
        assert snap.study_key == "STUDY"

        with pytest.raises(TypeError, match="async_inspect"):
            inspector.inspect("STUDY")

    async_mock = MagicMock(spec=["async_get_forms"])
    with pytest.raises(TypeError, match="async_inspect"):
        StudySchemaInspector(async_mock).inspect("STUDY")


def test_sinks_namespace_bound_exports_deprecation_shims_and_invalid_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = ImednetSDK(simulation_mode=True)
    other_sdk = ImednetSDK(simulation_mode=True)
    try:
        sinks = SinksNamespace(sdk)
        assert sinks.MongoDbSinkConfig is MongoDbSinkConfig
        assert sinks.MongoDbExportSink is MongoDbExportSink
        assert sinks.Neo4jSinkConfig is Neo4jSinkConfig
        assert sinks.Neo4jExportSink is Neo4jExportSink
        assert sinks.SnowflakeSinkConfig is SnowflakeSinkConfig
        assert sinks.SnowflakeExportSink is SnowflakeExportSink

        mongo_spy = MagicMock(return_value=3)
        neo_spy = MagicMock(return_value=4)
        sf_spy = MagicMock(return_value=5)
        monkeypatch.setattr("imednet_sinks.document.export_to_mongodb", mongo_spy)
        monkeypatch.setattr("imednet_sinks.graph.export_to_neo4j", neo_spy)
        monkeypatch.setattr("imednet_sinks.warehouse.export_to_snowflake", sf_spy)

        mongo_cfg = MongoDbSinkConfig(
            study_key="STUDY",
            uri="mongodb://localhost:27017",
            database="db",
            collection="records",
        )
        neo_cfg = Neo4jSinkConfig(
            study_key="STUDY",
            uri="bolt://localhost:7687",
            auth=("neo4j", "pass"),
        )
        sf_cfg = SnowflakeSinkConfig(
            study_key="STUDY",
            account="acct",
            user="u",
            password="p",
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="TBL",
        )

        with warnings.catch_warnings(record=True) as caught_clean:
            warnings.simplefilter("always")
            assert sinks.export_to_mongodb("STUDY", config=mongo_cfg) == 3
            assert sinks.export_to_mongodb(study_key="STUDY", config=mongo_cfg) == 3
            assert (
                sinks.export_to_mongodb("STUDY", "mongodb://localhost:27017", "db", "records") == 3
            )
            assert sinks.export_to_neo4j("STUDY", config=neo_cfg) == 4
            assert sinks.export_to_snowflake("STUDY", config=sf_cfg) == 5
        assert not [w for w in caught_clean if issubclass(w.category, DeprecationWarning)]

        assert mongo_spy.call_args_list[0].args == (sdk, "STUDY")
        assert neo_spy.call_args.args == (sdk, "STUDY")
        assert sf_spy.call_args.args == (sdk, "STUDY")

        with pytest.deprecated_call(match="Passing 'sdk' to sdk.sinks.export_to_mongodb") as rec:
            assert sinks.export_to_mongodb(other_sdk, "STUDY", config=mongo_cfg) == 3
        assert Path(rec[0].filename).resolve() == Path(__file__).resolve()
        assert mongo_spy.call_args.args == (other_sdk, "STUDY")

        with pytest.deprecated_call(match="Passing 'sdk' to sdk.sinks.export_to_neo4j"):
            assert sinks.export_to_neo4j(sdk=other_sdk, study_key="STUDY", config=neo_cfg) == 4
        assert neo_spy.call_args.args == (other_sdk,)
        assert neo_spy.call_args.kwargs["study_key"] == "STUDY"

        with pytest.deprecated_call(match="Passing 'sdk' to sdk.sinks.export_to_snowflake"):
            assert sinks.export_to_snowflake(other_sdk, "STUDY", config=sf_cfg) == 5
        assert sf_spy.call_args.args == (other_sdk, "STUDY")

        with pytest.raises(TypeError, match="missing 1 required positional argument"):
            sinks.export_to_mongodb()  # type: ignore[call-overload]
        with pytest.raises(TypeError, match="'study_key' must be a str"):
            sinks.export_to_mongodb(123)  # type: ignore[call-overload]
        with pytest.deprecated_call():
            with pytest.raises(TypeError, match="'study_key' must be a str"):
                sinks.export_to_mongodb(other_sdk, 123)  # type: ignore[call-overload]
        with pytest.raises(TypeError, match="'study_key' must be a str"):
            sinks.export_to_mongodb(study_key=123)  # type: ignore[call-overload]
        with pytest.raises(TypeError):
            sinks.export_to_mongodb("STUDY", study_key="STUDY", config=mongo_cfg)  # type: ignore[call-overload]
    finally:
        sdk.close()
        other_sdk.close()


@pytest.mark.asyncio
async def test_async_sdk_sinks_reject_exports_but_expose_config_and_sink_classes() -> None:
    async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
        assert async_sdk.sinks is not None
        assert isinstance(async_sdk.sinks, AsyncSinksNamespaceProtocol)
        assert async_sdk.sinks.MongoDbSinkConfig is MongoDbSinkConfig
        assert async_sdk.sinks.MongoDbExportSink is MongoDbExportSink
        assert async_sdk.sinks.Neo4jSinkConfig is Neo4jSinkConfig
        assert async_sdk.sinks.Neo4jExportSink is Neo4jExportSink
        assert async_sdk.sinks.SnowflakeSinkConfig is SnowflakeSinkConfig
        assert async_sdk.sinks.SnowflakeExportSink is SnowflakeExportSink

        sf_cfg = async_sdk.sinks.SnowflakeSinkConfig(
            study_key="STUDY",
            account="acct",
            user="u",
            password="p",
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="TBL",
        )
        with pytest.raises(TypeError, match="requires a synchronous ImednetSDK"):
            async_sdk.sinks.export_to_mongodb("STUDY")
        with pytest.raises(TypeError, match="requires a synchronous ImednetSDK"):
            async_sdk.sinks.export_to_neo4j("STUDY")
        with pytest.raises(TypeError, match="requires a synchronous ImednetSDK"):
            async_sdk.sinks.export_to_snowflake("STUDY", config=sf_cfg)


def test_workflows_top_level_exports_and_sdk_auth_and_study_context_self() -> None:
    for symbol in ("DataExtractionWorkflow", "StudySchemaInspector", "UATWorkflow", "Workflows"):
        assert symbol in imednet_workflows.__all__
        assert getattr(imednet_workflows, symbol) is not None
    assert DataExtractionWorkflow is not None
    assert UATWorkflow is not None

    for proto_symbol in (
        "AsyncSinksNamespaceProtocol",
        "AsyncWorkflowsNamespaceProtocol",
        "SinksNamespaceProtocol",
        "SyncWorkflowsNamespaceProtocol",
    ):
        assert proto_symbol in imednet.__all__

    class _SubSyncSDK(ImednetSDK):
        pass

    sub_sdk = _SubSyncSDK(api_key="k", security_key="s", base_url="https://api.test")
    try:
        assert isinstance(sub_sdk.auth, ApiKeyAuth)
        with sub_sdk.study_context("STUDY") as ctx_sdk:
            assert ctx_sdk is sub_sdk
            assert isinstance(ctx_sdk, _SubSyncSDK)
    finally:
        sub_sdk.close()

    sim_sdk = ImednetSDK(simulation_mode=True)
    try:
        assert isinstance(sim_sdk.auth, ApiKeyAuth)
    finally:
        sim_sdk.close()

    from imednet.simulation import create_simulation_transport

    unauthed_client = Client(transport=create_simulation_transport(), auth=None)
    unauthed_client.auth = None
    injected_sdk = ImednetSDK(client=unauthed_client)
    try:
        assert injected_sdk.auth is None
    finally:
        injected_sdk.close()


def test_all_five_packages_ship_pep561_py_typed_and_typed_classifier() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    expected_packages = [
        ("core", "imednet"),
        ("plugins-workflows", "imednet_workflows"),
        ("plugins-sinks", "imednet_sinks"),
        ("plugins-tui", "imednet_tui"),
        ("providers-airflow", "apache_airflow_providers_imednet"),
    ]
    for dist_dir, pkg_name in expected_packages:
        pkg_root = repo_root / "packages" / dist_dir
        py_typed = pkg_root / "src" / pkg_name / "py.typed"
        assert py_typed.is_file(), f"Missing PEP 561 marker: {py_typed}"
        pyproject_data = tomllib.loads((pkg_root / "pyproject.toml").read_text(encoding="utf-8"))
        classifiers = pyproject_data["project"].get("classifiers", [])
        assert "Typing :: Typed" in classifiers, f"Missing Typing :: Typed in {dist_dir}"


def test_sink_config_factories_expose_max_buffered_records() -> None:
    sync_sdk = ImednetSDK(simulation_mode=True)
    try:
        mongo_cfg = sync_sdk.sinks.MongoDbSinkConfig(
            "STUDY_1",
            uri="mongodb://localhost:27017",
            database="imednet",
            collection="records",
            max_buffered_records=2500,
        )
        neo4j_cfg = sync_sdk.sinks.Neo4jSinkConfig(
            "STUDY_1",
            uri="bolt://localhost:7687",
            auth=("neo4j", "secret"),  # pragma: allowlist secret
            max_buffered_records=2500,
        )
        sf_cfg = sync_sdk.sinks.SnowflakeSinkConfig(
            "STUDY_1",
            max_buffered_records=2500,
        )
        assert mongo_cfg.max_buffered_records == 2500
        assert neo4j_cfg.max_buffered_records == 2500
        assert sf_cfg.max_buffered_records == 2500
    finally:
        sync_sdk.close()
