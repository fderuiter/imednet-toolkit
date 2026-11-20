"""Unit tests for mode-safe plugin protocols, async workflow methods, and bound sink exports."""

from __future__ import annotations

import gc
import json
import tomllib
import warnings
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import imednet_sinks
import pytest
import respx
from imednet_sinks.plugin import SinksNamespace, create_sinks

import apache_airflow_providers_imednet
import imednet
import imednet.sdk as sdk_mod
import imednet_tui
import imednet_workflows
from imednet import (
    AsyncImednetSDK,
    AsyncSinksNamespaceProtocol,
    AsyncWorkflowsNamespaceProtocol,
    ImednetSDK,
    SinksNamespaceProtocol,
    SyncWorkflowsNamespaceProtocol,
)
from imednet.auth import AuthStrategy
from imednet.spi.facade import AsyncImednetFacade
from imednet.utils.batch_coordinator import BatchCoordinator
from imednet.validation.cache import BaseSchemaCache
from imednet_workflows import (
    DataExtractionWorkflow,
    RecordUpdateWorkflow,
    StudySchemaInspector,
    UATWorkflow,
    Workflows,
)
from imednet_workflows.namespace import Workflows as NamespaceWorkflows

BASE_URL = "https://edc.test.imednet.com"


def _build_offline_cache(study_key: str = "STUDY1") -> BaseSchemaCache[Any]:
    """Create a populated offline BaseSchemaCache for record submission tests."""
    from imednet.models.variables import Variable

    cache: BaseSchemaCache[Any] = BaseSchemaCache(is_async=False)
    variables = [
        Variable.model_validate(
            {
                "studyKey": study_key,
                "variableId": 101,
                "variableName": "AGE",
                "variableType": "integer",
                "formId": 10,
                "formKey": "DEMOG",
            }
        ),
        Variable.model_validate(
            {
                "studyKey": study_key,
                "variableId": 201,
                "variableName": "SYSBP",
                "variableType": "integer",
                "formId": 20,
                "formKey": "VITALS",
            }
        ),
        Variable.model_validate(
            {
                "studyKey": study_key,
                "variableId": 301,
                "variableName": "AETERM",
                "variableType": "text",
                "formId": 30,
                "formKey": "AE",
            }
        ),
    ]
    cache.populate(variables, study_key=study_key)
    return cache


class TestWorkflowRegistryAsyncGuards:
    """Verify WorkflowRegistry blocks sync-only workflows on AsyncImednetSDK."""

    @pytest.mark.parametrize(
        "workflow_name",
        [
            "data_extraction",
            "duckdb_centralizer",
            "query_management",
            "record_mapper",
            "register_subjects",
            "subject_data",
            "uat",
        ],
    )
    @pytest.mark.asyncio
    async def test_sync_only_workflows_raise_type_error_on_async_sdk(
        self, workflow_name: str
    ) -> None:
        async with AsyncImednetSDK(api_key="k", security_key="s", base_url=BASE_URL) as async_sdk:
            with pytest.raises(TypeError, match="synchronous-only"):
                getattr(async_sdk.workflows, workflow_name)

            with pytest.raises(TypeError, match="synchronous-only"):
                _ = async_sdk.workflows[workflow_name]  # type: ignore[index]

            with pytest.raises(TypeError, match="synchronous-only"):
                async_sdk.workflows.get(workflow_name)  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_sync_only_entry_point_is_rejected_before_class_instantiation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instantiated = False

        class CustomSyncOnlyWorkflow:
            def __init__(self, sdk: Any) -> None:
                nonlocal instantiated
                instantiated = True
                self._sdk = sdk

            def run_sync(self) -> str:
                return "sync"

        ep = MagicMock(spec=EntryPoint)
        ep.name = "custom_sync"
        ep.value = "pkg:CustomSyncOnlyWorkflow"
        ep.load.return_value = CustomSyncOnlyWorkflow

        monkeypatch.setattr(
            sdk_mod,
            "entry_points",
            lambda *, group, name=None: [ep] if group == "imednet.workflows" else [],
        )

        async with AsyncImednetSDK(api_key="k", security_key="s", base_url=BASE_URL) as async_sdk:
            with pytest.raises(TypeError, match="custom_sync"):
                _ = async_sdk.workflows.custom_sync  # type: ignore[attr-defined]

        assert instantiated is False

    @pytest.mark.asyncio
    async def test_sync_only_function_factory_rejected_on_async_sdk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _SyncHelper:
            def execute(self) -> None:
                pass

        def make_sync_helper(_sdk: Any) -> _SyncHelper:
            return _SyncHelper()

        ep = MagicMock(spec=EntryPoint)
        ep.name = "fn_sync"
        ep.value = "pkg:make_sync_helper"
        ep.load.return_value = make_sync_helper

        monkeypatch.setattr(
            sdk_mod,
            "entry_points",
            lambda *, group, name=None: [ep] if group == "imednet.workflows" else [],
        )

        async with AsyncImednetSDK(api_key="k", security_key="s", base_url=BASE_URL) as async_sdk:
            with pytest.raises(TypeError, match="fn_sync"):
                _ = async_sdk.workflows.fn_sync  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_async_capable_workflows_load_on_async_sdk(self) -> None:
        async with AsyncImednetSDK(api_key="k", security_key="s", base_url=BASE_URL) as async_sdk:
            assert isinstance(async_sdk.workflows.record_update, RecordUpdateWorkflow)
            assert isinstance(async_sdk.workflows.uat_inspector, StudySchemaInspector)
            # Cached lookup returns the same instance
            assert async_sdk.workflows.record_update is async_sdk.workflows.record_update


class TestRecordUpdateAsyncModeAndGuards:
    """Verify sync guards, zero unawaited coroutines, and async single-record helpers."""

    @pytest.mark.asyncio
    async def test_sync_methods_on_async_sdk_raise_type_error_with_zero_runtime_warnings(
        self,
    ) -> None:
        schema = _build_offline_cache("STUDY1")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            async with AsyncImednetSDK(
                api_key="k", security_key="s", base_url=BASE_URL
            ) as async_sdk:
                wf = async_sdk.workflows.record_update

                # Also verify default live-validator path (schema=None) raises TypeError
                # immediately without unawaited AsyncSchemaValidator.validate_batch coroutines
                with pytest.raises(TypeError, match="async_create_or_update_records_batch"):
                    wf.create_or_update_records_batch(
                        "STUDY1", [{"formKey": "DEMOG", "data": {"AGE": 30}}]
                    )

                with pytest.raises(TypeError, match="async_create_or_update_records_batch"):
                    wf.create_or_update_records_batch(
                        "STUDY1", [{"formKey": "DEMOG", "data": {"AGE": 30}}], schema=schema
                    )

                with pytest.raises(TypeError, match="async_create_or_update_records"):
                    wf.create_or_update_records(
                        "STUDY1", [{"formKey": "DEMOG", "data": {"AGE": 30}}], schema=schema
                    )

                with pytest.raises(TypeError, match="async_register_subject"):
                    wf.register_subject("STUDY1", "DEMOG", "SITE-1", {"AGE": 30}, schema=schema)

                with pytest.raises(TypeError, match="async_update_scheduled_record"):
                    wf.update_scheduled_record(
                        "STUDY1",
                        "VITALS",
                        "SUBJ-1",
                        "VISIT-1",
                        {"SYSBP": 120},
                        schema=schema,
                    )

                with pytest.raises(TypeError, match="async_create_new_record"):
                    wf.create_new_record(
                        "STUDY1", "AE", "SUBJ-1", {"AETERM": "Pain"}, schema=schema
                    )

                with pytest.raises(TypeError, match="_async_validate_form_key"):
                    wf._validate_form_key("STUDY1", [{"formKey": "DEMOG", "data": {"AGE": 30}}])

            gc.collect()

        runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
        assert runtime_warnings == []

    def test_batch_coordinator_submit_rejects_coroutine_validate_batch_fn(self) -> None:
        coordinator = BatchCoordinator(chunk_size=10)
        schema = _build_offline_cache("STUDY1")
        submitted = False

        async def async_validate(_study_key: str, _records: list[dict[str, Any]]) -> None:
            return None

        def submit_chunk(_chunk: list[dict[str, Any]]) -> Any:
            nonlocal submitted
            submitted = True
            return MagicMock(batch_id="B1")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(TypeError, match="AsyncBatchCoordinator"):
                coordinator.submit(
                    "STUDY1",
                    [{"formKey": "DEMOG", "data": {"AGE": 30}}],
                    submit_chunk,
                    schema=schema,
                    validate_batch_fn=async_validate,  # type: ignore[arg-type]
                )
            gc.collect()

        assert submitted is False
        assert [w for w in caught if issubclass(w.category, RuntimeWarning)] == []

    @pytest.mark.asyncio
    async def test_async_single_record_methods_submit_expected_payloads(self) -> None:
        schema = _build_offline_cache("STUDY1")
        captured_payloads: list[list[dict[str, Any]]] = []

        def handle_post(request: httpx.Request) -> httpx.Response:
            captured_payloads.append(json.loads(request.content.decode("utf-8")))
            idx = len(captured_payloads)
            return httpx.Response(
                200,
                json={"batchId": f"BATCH-{idx}", "state": "COMPLETED"},
            )

        with respx.mock(base_url=BASE_URL, assert_all_called=True) as router:
            router.post("/api/v1/edc/studies/STUDY1/records").mock(side_effect=handle_post)

            async with AsyncImednetSDK(
                api_key="k", security_key="s", base_url=BASE_URL
            ) as async_sdk:
                wf = async_sdk.workflows.record_update

                reg_job = await wf.async_register_subject(
                    "STUDY1",
                    "DEMOG",
                    "SITE-01",
                    {"AGE": 42},
                    schema=schema,
                )
                sched_job = await wf.async_update_scheduled_record(
                    "STUDY1",
                    20,
                    "OID-99",
                    5,
                    {"SYSBP": 118},
                    form_identifier_type="id",
                    subject_identifier_type="oid",
                    interval_identifier_type="id",
                    schema=schema,
                )
                new_job = await wf.async_create_new_record(
                    "STUDY1",
                    "AE",
                    1001,
                    {"AETERM": "Fatigue"},
                    subject_identifier_type="id",
                    schema=schema,
                )

        assert reg_job.batch_id == "BATCH-1"
        assert sched_job.batch_id == "BATCH-2"
        assert new_job.batch_id == "BATCH-3"
        assert captured_payloads == [
            [{"formKey": "DEMOG", "siteName": "SITE-01", "data": {"AGE": 42}}],
            [
                {
                    "formId": 20,
                    "subjectOid": "OID-99",
                    "intervalId": 5,
                    "data": {"SYSBP": 118},
                }
            ],
            [{"formKey": "AE", "subjectId": 1001, "data": {"AETERM": "Fatigue"}}],
        ]


class TestStudySchemaInspectorGuards:
    """Verify StudySchemaInspector.inspect rejects AsyncImednetSDK before cache or network."""

    @pytest.mark.asyncio
    async def test_inspect_raises_type_error_even_when_async_cache_warm(self) -> None:
        with respx.mock(base_url=BASE_URL, assert_all_called=True) as router:
            router.get("/api/v1/edc/studies/STUDY1/forms").mock(
                return_value=httpx.Response(200, json={"data": [], "pagination": {"totalPages": 1}})
            )
            router.get("/api/v1/edc/studies/STUDY1/variables").mock(
                return_value=httpx.Response(200, json={"data": [], "pagination": {"totalPages": 1}})
            )
            router.get("/api/v1/edc/studies/STUDY1/intervals").mock(
                return_value=httpx.Response(200, json={"data": [], "pagination": {"totalPages": 1}})
            )
            router.get("/api/v1/edc/studies/STUDY1/sites").mock(
                return_value=httpx.Response(200, json={"data": [], "pagination": {"totalPages": 1}})
            )

            async with AsyncImednetSDK(
                api_key="k", security_key="s", base_url=BASE_URL
            ) as async_sdk:
                snapshot = await async_sdk.workflows.uat_inspector.async_inspect("STUDY1")
                assert snapshot.study_key == "STUDY1"

                with pytest.raises(TypeError, match="async_inspect"):
                    async_sdk.workflows.uat_inspector.inspect("STUDY1")

    def test_inspect_raises_type_error_for_async_facade_mock(self) -> None:
        async_facade_mock = MagicMock(spec=AsyncImednetFacade)
        inspector = StudySchemaInspector(async_facade_mock)
        with pytest.raises(TypeError, match="async_inspect"):
            inspector.inspect("STUDY1")


class TestSinksNamespaceBindingAndAsyncGuards:
    """Verify bound sink export calls, deprecated (sdk, study_key) calls, and async guards."""

    def test_bound_sink_exports_inject_self_sdk_without_deprecation_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import imednet_sinks.document as doc_mod
        import imednet_sinks.graph as graph_mod
        import imednet_sinks.warehouse as wh_mod

        calls: list[tuple[str, Any, str, tuple[Any, ...], dict[str, Any]]] = []

        monkeypatch.setattr(
            doc_mod,
            "export_to_mongodb",
            lambda sdk, study_key, *a, **kw: calls.append(("mongo", sdk, study_key, a, kw)) or 11,
        )
        monkeypatch.setattr(
            graph_mod,
            "export_to_neo4j",
            lambda sdk, study_key, *a, **kw: calls.append(("neo4j", sdk, study_key, a, kw)) or 22,
        )
        monkeypatch.setattr(
            wh_mod,
            "export_to_snowflake",
            lambda sdk, study_key, *a, **kw: calls.append(("snow", sdk, study_key, a, kw)) or 33,
        )

        with ImednetSDK(api_key="k", security_key="s", base_url=BASE_URL) as sdk:
            assert sdk.sinks is not None
            snow_cfg = sdk.sinks.SnowflakeSinkConfig(study_key="STUDY1")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                res_m = sdk.sinks.export_to_mongodb(
                    "STUDY1", "mongodb://localhost:27017", "db", "col", mask_phi=False
                )
                res_n = sdk.sinks.export_to_neo4j(
                    "STUDY1", "bolt://localhost:7687", ("neo4j", "secret")
                )
                res_s = sdk.sinks.export_to_snowflake("STUDY1", config=snow_cfg)
                res_kw = sdk.sinks.export_to_mongodb(
                    study_key="STUDY_KW", uri="mongodb://localhost:27017"
                )

        assert (res_m, res_n, res_s, res_kw) == (11, 22, 33, 11)
        assert [w for w in caught if issubclass(w.category, DeprecationWarning)] == []
        assert calls[0] == (
            "mongo",
            sdk,
            "STUDY1",
            ("mongodb://localhost:27017", "db", "col"),
            {"mask_phi": False},
        )
        assert calls[1] == (
            "neo4j",
            sdk,
            "STUDY1",
            ("bolt://localhost:7687", ("neo4j", "secret")),
            {},
        )
        assert calls[2] == ("snow", sdk, "STUDY1", (), {"config": snow_cfg})
        assert calls[3] == (
            "mongo",
            sdk,
            "STUDY_KW",
            (),
            {"uri": "mongodb://localhost:27017"},
        )

    def test_deprecated_sdk_first_sink_calls_emit_deprecation_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import imednet_sinks.document as doc_mod
        import imednet_sinks.graph as graph_mod
        import imednet_sinks.warehouse as wh_mod

        monkeypatch.setattr(doc_mod, "export_to_mongodb", lambda _sdk, _sk, *_a, **_kw: 5)
        monkeypatch.setattr(graph_mod, "export_to_neo4j", lambda _sdk, _sk, *_a, **_kw: 6)
        monkeypatch.setattr(wh_mod, "export_to_snowflake", lambda _sdk, _sk, *_a, **_kw: 7)

        with ImednetSDK(api_key="k", security_key="s", base_url=BASE_URL) as sdk:
            assert sdk.sinks is not None
            snow_cfg = sdk.sinks.SnowflakeSinkConfig(study_key="STUDY1")

            with pytest.warns(DeprecationWarning, match="export_to_mongodb") as rec_m:
                assert sdk.sinks.export_to_mongodb(sdk, "STUDY1", "mongodb://localhost:27017") == 5
            assert Path(rec_m[0].filename).name == Path(__file__).name

            with pytest.warns(DeprecationWarning, match="export_to_neo4j"):
                assert sdk.sinks.export_to_neo4j(sdk, "STUDY1", "bolt://localhost:7687") == 6

            with pytest.warns(DeprecationWarning, match="export_to_snowflake"):
                assert sdk.sinks.export_to_snowflake(sdk, "STUDY1", config=snow_cfg) == 7

            with pytest.warns(DeprecationWarning, match="export_to_mongodb"):
                assert sdk.sinks.export_to_mongodb(sdk=sdk, study_key="STUDY1") == 5  # type: ignore[call-overload]

            with pytest.warns(DeprecationWarning, match="export_to_mongodb"):
                assert sdk.sinks.export_to_mongodb("STUDY1", sdk=sdk) == 5  # type: ignore[call-overload]

            with pytest.warns(DeprecationWarning, match="export_to_mongodb"):
                assert sdk.sinks.export_to_mongodb(sdk, study_key="STUDY1") == 5

    def test_sinks_invalid_argument_shapes_raise_type_error(self) -> None:
        with ImednetSDK(api_key="k", security_key="s", base_url=BASE_URL) as sdk:
            sinks = create_sinks(sdk)
            with pytest.raises(TypeError, match="missing 1 required positional argument"):
                sinks.export_to_mongodb()  # type: ignore[call-overload]
            with pytest.raises(TypeError, match="expected 'study_key'"):
                sinks.export_to_mongodb(123)  # type: ignore[call-overload]
            with pytest.raises(TypeError, match="expected 'study_key' to be a str"):
                sinks.export_to_mongodb(sdk, 123)  # type: ignore[call-overload]
            with pytest.raises(TypeError, match="expected 'study_key' to be a str"):
                sinks.export_to_mongodb(study_key=123)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_async_sdk_sinks_keep_classes_accessible_and_reject_exports(self) -> None:
        async with AsyncImednetSDK(api_key="k", security_key="s", base_url=BASE_URL) as async_sdk:
            assert async_sdk.sinks is not None
            assert isinstance(async_sdk.sinks, SinksNamespace)
            assert isinstance(async_sdk.sinks, AsyncSinksNamespaceProtocol)

            # Config and sink classes remain accessible on AsyncImednetSDK
            mongo_cfg = async_sdk.sinks.MongoDbSinkConfig(
                study_key="STUDY1",
                uri="mongodb://localhost:27017",
                database="db",
                collection="col",
            )
            neo4j_cfg = async_sdk.sinks.Neo4jSinkConfig(
                study_key="STUDY1",
                uri="bolt://localhost:7687",
                auth=("neo4j", "secret"),
            )
            snow_cfg = async_sdk.sinks.SnowflakeSinkConfig(study_key="STUDY1")
            assert mongo_cfg.study_key == "STUDY1"
            assert neo4j_cfg.study_key == "STUDY1"
            assert snow_cfg.study_key == "STUDY1"
            assert async_sdk.sinks.MongoDbExportSink is not None
            assert async_sdk.sinks.Neo4jExportSink is not None
            assert async_sdk.sinks.SnowflakeExportSink is not None

            with pytest.raises(TypeError, match="AsyncImednetSDK"):
                async_sdk.sinks.export_to_mongodb("STUDY1", config=mongo_cfg)

            with pytest.raises(TypeError, match="AsyncImednetSDK"):
                async_sdk.sinks.export_to_neo4j("STUDY1", config=neo4j_cfg)

            with pytest.raises(TypeError, match="AsyncImednetSDK"):
                async_sdk.sinks.export_to_snowflake("STUDY1", config=snow_cfg)


class TestExportsProtocolsAndPackagingMarkers:
    """Verify public workflow exports, narrowed SDK types, and PEP 561 py.typed markers."""

    def test_imednet_workflows_top_level_exports_and_all(self) -> None:
        for symbol in (
            "DataExtractionWorkflow",
            "StudySchemaInspector",
            "UATWorkflow",
            "Workflows",
        ):
            assert symbol in imednet_workflows.__all__
            assert hasattr(imednet_workflows, symbol)

        assert Workflows is NamespaceWorkflows
        assert imednet_workflows.DataExtractionWorkflow is DataExtractionWorkflow
        assert imednet_workflows.StudySchemaInspector is StudySchemaInspector
        assert imednet_workflows.UATWorkflow is UATWorkflow
        assert "Workflows" in imednet_workflows.namespace.__all__

    @pytest.mark.asyncio
    async def test_sdk_auth_study_context_self_and_runtime_protocols(self) -> None:
        class CustomSyncSDK(ImednetSDK):
            pass

        class CustomAsyncSDK(AsyncImednetSDK):
            pass

        with CustomSyncSDK(api_key="k", security_key="s", base_url=BASE_URL) as sync_sdk:
            assert isinstance(sync_sdk.auth, AuthStrategy)
            assert isinstance(Workflows(sync_sdk), SyncWorkflowsNamespaceProtocol)
            assert isinstance(create_sinks(sync_sdk), SinksNamespaceProtocol)
            with sync_sdk.study_context("STUDY1") as scoped:
                assert scoped is sync_sdk
                assert isinstance(scoped, CustomSyncSDK)

        async with CustomAsyncSDK(api_key="k", security_key="s", base_url=BASE_URL) as async_sdk:
            assert isinstance(async_sdk.auth, AuthStrategy)
            assert AsyncWorkflowsNamespaceProtocol is not None
            with async_sdk.study_context("STUDY1") as scoped_async:
                assert scoped_async is async_sdk
                assert isinstance(scoped_async, CustomAsyncSDK)

    def test_all_five_workspace_packages_ship_py_typed_and_scoped_mypy_config(self) -> None:
        for pkg in (
            imednet,
            imednet_workflows,
            imednet_sinks,
            apache_airflow_providers_imednet,
            imednet_tui,
        ):
            pkg_dir = Path(pkg.__file__).resolve().parent
            assert (pkg_dir / "py.typed").is_file(), f"Missing py.typed in {pkg.__name__}"

        repo_root = Path(__file__).resolve().parents[2]
        with (repo_root / "pyproject.toml").open("rb") as fh:
            root_cfg = tomllib.load(fh)

        mypy_cfg = root_cfg["tool"]["mypy"]
        assert "ignore_missing_imports" not in mypy_cfg
        verify_static = root_cfg["tool"]["hatch"]["envs"]["default"]["scripts"]["verify-static"]
        assert "mypy packages/plugins-sinks/src/imednet_sinks" in verify_static
