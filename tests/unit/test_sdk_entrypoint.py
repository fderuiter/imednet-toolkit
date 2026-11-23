"""Unit tests for sdk entrypoint."""

from collections.abc import Callable
from typing import Any

import pytest

import imednet.sdk as sdk_mod
from imednet.core.client import Client
from imednet.endpoints.codings import CodingsEndpoint
from imednet.endpoints.forms import FormsEndpoint
from imednet.endpoints.intervals import IntervalsEndpoint
from imednet.endpoints.jobs import JobsEndpoint
from imednet.endpoints.queries import QueriesEndpoint
from imednet.endpoints.record_revisions import RecordRevisionsEndpoint
from imednet.endpoints.records import RecordsEndpoint
from imednet.endpoints.sites import SitesEndpoint
from imednet.endpoints.studies import StudiesEndpoint
from imednet.endpoints.subjects import SubjectsEndpoint
from imednet.endpoints.users import UsersEndpoint
from imednet.endpoints.variables import VariablesEndpoint
from imednet.endpoints.visits import VisitsEndpoint
from imednet.errors import PluginLoadError
from imednet.errors.orchestration import FilterConflictError, OrchestratorError
from imednet.orchestration import MultiStudyOrchestrator, OrchestratorResult, StudyWorkerCallable
from imednet_workflows.data_extraction import DataExtractionWorkflow
from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow
from imednet_workflows.query_management import QueryManagementWorkflow
from imednet_workflows.record_mapper import RecordMapper
from imednet_workflows.record_update import RecordUpdateWorkflow
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow
from imednet_workflows.subject_data import SubjectDataWorkflow
from imednet_workflows.uat import StudySchemaInspector


class _FakeEntryPoint:
    """Minimal importlib.metadata.EntryPoint stand-in for plugin loader tests."""

    def __init__(
        self,
        loader: Callable[[], Any],
        value: str = "tests.fake_plugin:Workflows",
        name: str = "fake_workflow",
    ) -> None:
        """Initialize the test object."""
        self._loader = loader
        self.value = value
        self.name = name

    def load(self):
        """Helper function to load."""
        return self._loader()


def test_top_level_orchestration_exports() -> None:
    """Test that top level orchestration exports."""
    from imednet import FilterConflictError as TopLevelFilterConflictError
    from imednet import MultiStudyOrchestrator as TopLevelMultiStudyOrchestrator
    from imednet import OrchestratorError as TopLevelOrchestratorError
    from imednet import OrchestratorResult as TopLevelOrchestratorResult
    from imednet import StudyWorkerCallable as TopLevelStudyWorkerCallable

    assert TopLevelMultiStudyOrchestrator is MultiStudyOrchestrator
    assert TopLevelOrchestratorResult is OrchestratorResult
    assert TopLevelStudyWorkerCallable is StudyWorkerCallable
    assert TopLevelOrchestratorError is OrchestratorError
    assert TopLevelFilterConflictError is FilterConflictError


def test_sdk_workflows_uses_entry_point_discovery(monkeypatch) -> None:
    """Test that sdk workflows uses entry point discovery."""
    loaded = {"called": False}

    def load_workflows():
        """Helper function to load workflows."""
        loaded["called"] = True

        class FakeWorkflows:
            """Test suite for FakeWorkflows."""

            def __init__(self, sdk_instance):
                """Initialize the test object."""
                self.sdk_instance = sdk_instance

        return FakeWorkflows

    monkeypatch.setattr(
        sdk_mod,
        "entry_points",
        lambda *, group, name=None: (
            [_FakeEntryPoint(load_workflows, name="my_workflow")]
            if group == "imednet.workflows"
            else []
        ),
    )

    sdk = _create_sdk()

    workflow = sdk.workflows.my_workflow
    assert loaded["called"]
    assert workflow.sdk_instance is sdk


@pytest.mark.parametrize("exception_class", [AttributeError, ImportError, ModuleNotFoundError])
def test_sdk_workflows_invalid_entry_point_load_raises_import_error(
    monkeypatch, exception_class
) -> None:
    """Test that sdk workflows invalid entry point load raises import error."""

    def failing_loader():
        """Helper function to failing loader."""
        raise exception_class("boom")

    monkeypatch.setattr(
        sdk_mod,
        "entry_points",
        lambda *, group, name=None: (
            [_FakeEntryPoint(failing_loader, name="my_workflow")]
            if group == "imednet.workflows"
            else []
        ),
    )

    sdk = _create_sdk()
    with pytest.raises(PluginLoadError, match="Failed to load workflow plugin from entry point"):
        _ = sdk.workflows.my_workflow


def test_sdk_workflows_entry_point_must_be_callable(monkeypatch) -> None:
    """Test that sdk workflows entry point must be callable."""
    monkeypatch.setattr(
        sdk_mod,
        "entry_points",
        lambda *, group, name=None: (
            [_FakeEntryPoint(lambda: object(), name="my_workflow")]
            if group == "imednet.workflows"
            else []
        ),
    )

    sdk = _create_sdk()
    with pytest.raises(PluginLoadError, match="must be a callable"):
        _ = sdk.workflows.my_workflow


def test_sdk_workflows_multiple_plugins_raises_import_error(monkeypatch) -> None:
    """Test that sdk workflows multiple plugins raises import error."""
    monkeypatch.setattr(
        sdk_mod,
        "entry_points",
        lambda *, group, name=None: (
            [
                _FakeEntryPoint(lambda: object(), name="my_workflow"),
                _FakeEntryPoint(lambda: object(), name="my_workflow"),
            ]
            if group == "imednet.workflows"
            else []
        ),
    )

    with pytest.raises(
        PluginLoadError, match="Multiple workflows registered under the name 'my_workflow'"
    ):
        _create_sdk()


def test_sdk_workflows_instantiation_failure_raises_import_error(monkeypatch) -> None:
    """Test that sdk workflows instantiation failure raises import error."""

    class BrokenWorkflows:
        """Test suite for BrokenWorkflows."""

        def __init__(self, sdk_instance):
            """Initialize the test object."""
            raise TypeError("broken")

    monkeypatch.setattr(
        sdk_mod,
        "entry_points",
        lambda *, group, name=None: (
            [_FakeEntryPoint(lambda: BrokenWorkflows, name="my_workflow")]
            if group == "imednet.workflows"
            else []
        ),
    )

    sdk = _create_sdk()
    with pytest.raises(PluginLoadError, match="Failed to instantiate workflows"):
        _ = sdk.workflows.my_workflow


def _create_sdk() -> sdk_mod.ImednetSDK:
    """Helper function to  create sdk."""
    return sdk_mod.ImednetSDK(
        api_key="key",
        security_key="secret",
        base_url="https://example.com",
    )


def test_env_var_credentials(monkeypatch) -> None:
    """Test that env var credentials."""
    monkeypatch.setenv("IMEDNET_API_KEY", "env_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "env_secret")

    sdk = sdk_mod.ImednetSDK()

    assert isinstance(sdk._client, Client)


def test_sdk_initialization_wires_endpoints_and_workflows() -> None:
    """Test that sdk initialization wires endpoints and workflows."""
    sdk = _create_sdk()

    assert isinstance(sdk._client, Client)

    endpoints = {
        "codings": CodingsEndpoint,
        "forms": FormsEndpoint,
        "intervals": IntervalsEndpoint,
        "jobs": JobsEndpoint,
        "queries": QueriesEndpoint,
        "record_revisions": RecordRevisionsEndpoint,
        "records": RecordsEndpoint,
        "sites": SitesEndpoint,
        "studies": StudiesEndpoint,
        "subjects": SubjectsEndpoint,
        "users": UsersEndpoint,
        "variables": VariablesEndpoint,
        "visits": VisitsEndpoint,
    }
    for name, cls in endpoints.items():
        assert isinstance(getattr(sdk, name), cls)

    assert isinstance(sdk.workflows.data_extraction, DataExtractionWorkflow)
    assert isinstance(sdk.workflows.duckdb_centralizer, DuckDBIngestionWorkflow)
    assert isinstance(sdk.workflows.query_management, QueryManagementWorkflow)
    assert isinstance(sdk.workflows.record_mapper, RecordMapper)
    assert isinstance(sdk.workflows.record_update, RecordUpdateWorkflow)
    assert isinstance(sdk.workflows.register_subjects, RegisterSubjectsWorkflow)
    assert isinstance(sdk.workflows.subject_data, SubjectDataWorkflow)
    assert isinstance(sdk.workflows.uat_inspector, StudySchemaInspector)


def test_context_management_closes_client(monkeypatch) -> None:
    """Test that context management closes client."""
    called = {"close": False}

    def fake_close(self) -> None:
        """Helper function to fake close."""
        called["close"] = True

    monkeypatch.setattr(Client, "close", fake_close)

    with _create_sdk() as sdk:
        assert isinstance(sdk, sdk_mod.ImednetSDK)

    assert called["close"]


def test_convenience_methods_delegate_to_endpoints(monkeypatch) -> None:
    """Test that convenience methods delegate to endpoints."""
    sdk = _create_sdk()
    calls = {}

    def fake_studies_list(study_key=None, **kw):
        """Helper function to fake studies list."""
        calls["studies"] = kw
        return ["STUDY"]

    def fake_records_list(study_key, record_data_filter=None, **kw):
        """Helper function to fake records list."""
        calls["records"] = (study_key, record_data_filter, kw)
        return ["REC"]

    def fake_sites_list(study_key, **kw):
        """Helper function to fake sites list."""
        calls["sites"] = (study_key, kw)
        return ["SITE"]

    def fake_subjects_list(study_key, **kw):
        """Helper function to fake subjects list."""
        calls["subjects"] = (study_key, kw)
        return ["SUB"]

    def fake_create(study_key, records_data, email_notify=None, schema=None):
        """Helper function to fake create."""
        calls["create"] = (study_key, records_data, email_notify)
        return "JOB"

    def fake_forms_list(study_key, **kw):
        """Helper function to fake forms list."""
        calls["forms"] = (study_key, kw)
        return ["FORM"]

    def fake_intervals_list(study_key, **kw):
        """Helper function to fake intervals list."""
        calls["intervals"] = (study_key, kw)
        return ["INT"]

    def fake_variables_list(study_key, **kw):
        """Helper function to fake variables list."""
        calls["variables"] = (study_key, kw)
        return ["VAR"]

    def fake_visits_list(study_key, **kw):
        """Helper function to fake visits list."""
        calls["visits"] = (study_key, kw)
        return ["VIS"]

    def fake_codings_list(study_key, **kw):
        """Helper function to fake codings list."""
        calls["codings"] = (study_key, kw)
        return ["COD"]

    def fake_queries_list(study_key, **kw):
        """Helper function to fake queries list."""
        calls["queries"] = (study_key, kw)
        return ["QUERY"]

    def fake_record_revisions_list(study_key, **kw):
        """Helper function to fake record revisions list."""
        calls["record_revisions"] = (study_key, kw)
        return ["REV"]

    def fake_users_list(study_key, include_inactive=False):
        """Helper function to fake users list."""
        calls["users"] = (study_key, include_inactive)
        return ["USER"]

    def fake_get_job(study_key, batch_id):
        """Helper function to fake get job."""
        calls["job"] = (study_key, batch_id)
        return "JOBOBJ"

    monkeypatch.setattr(sdk.studies, "list", fake_studies_list)
    monkeypatch.setattr(sdk.records, "list", fake_records_list)
    monkeypatch.setattr(sdk.sites, "list", fake_sites_list)
    monkeypatch.setattr(sdk.subjects, "list", fake_subjects_list)
    monkeypatch.setattr(sdk.records, "create", fake_create)
    monkeypatch.setattr(sdk.forms, "list", fake_forms_list)
    monkeypatch.setattr(sdk.intervals, "list", fake_intervals_list)
    monkeypatch.setattr(sdk.variables, "list", fake_variables_list)
    monkeypatch.setattr(sdk.visits, "list", fake_visits_list)
    monkeypatch.setattr(sdk.codings, "list", fake_codings_list)
    monkeypatch.setattr(sdk.queries, "list", fake_queries_list)
    monkeypatch.setattr(sdk.record_revisions, "list", fake_record_revisions_list)
    monkeypatch.setattr(sdk.users, "list", fake_users_list)
    monkeypatch.setattr(sdk.jobs, "get", fake_get_job)

    assert sdk.get_studies(status="active") == ["STUDY"]
    assert sdk.get_records("S1", record_data_filter="x") == ["REC"]
    assert sdk.get_sites("S1", country="US") == ["SITE"]
    assert sdk.get_subjects("S1", status="new") == ["SUB"]
    assert sdk.create_record("S1", [{"a": 1}], email_notify=True) == "JOB"
    assert sdk.get_forms("S1", page=2) == ["FORM"]
    assert sdk.get_intervals("S1") == ["INT"]
    assert sdk.get_variables("S1") == ["VAR"]
    assert sdk.get_visits("S1") == ["VIS"]
    assert sdk.get_codings("S1") == ["COD"]
    assert sdk.get_queries("S1", status="open") == ["QUERY"]
    assert sdk.get_record_revisions("S1") == ["REV"]
    assert sdk.get_users("S1", include_inactive=True) == ["USER"]
    assert sdk.get_job("S1", "B1") == "JOBOBJ"

    assert calls["studies"] == {"status": "active"}
    assert calls["records"] == ("S1", "x", {})
    assert calls["sites"] == ("S1", {"country": "US"})
    assert calls["subjects"] == ("S1", {"status": "new"})
    assert calls["create"] == ("S1", [{"a": 1}], True)
    assert calls["forms"] == ("S1", {"page": 2})
    assert calls["intervals"] == ("S1", {})
    assert calls["variables"] == ("S1", {})
    assert calls["visits"] == ("S1", {})
    assert calls["codings"] == ("S1", {})
    assert calls["queries"] == ("S1", {"status": "open"})
    assert calls["record_revisions"] == ("S1", {})
    assert calls["users"] == ("S1", True)
    assert calls["job"] == ("S1", "B1")


def test_poll_job_convenience_sync(monkeypatch) -> None:
    """Test that poll job convenience sync."""
    sdk = _create_sdk()
    calls = {}

    class FakePoller:
        """Test suite for FakePoller."""

        def __init__(self, get_func, **kwargs):
            """Initialize the test object."""
            calls["init"] = get_func

        def run(self, study_key, batch_id, interval, timeout):
            """Helper function to run."""
            calls["run"] = (study_key, batch_id, interval, timeout)
            return "JOBOBJ"

    monkeypatch.setattr("imednet.utils.job_poller.JobPoller", FakePoller)

    assert sdk.poll_job("S1", "B1", interval=10, timeout=100) == "JOBOBJ"
    assert calls["run"] == ("S1", "B1", 10, 100)


@pytest.mark.asyncio
async def test_steady_state_sdk_init_executes_zero_metadata_or_tls_file_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify warmed ImednetSDK and AsyncImednetSDK builds perform 0 Path.read_text and 0 SSL context builds."""
    import ssl
    from pathlib import Path

    warm_sync = _create_sdk()
    warm_sync.close()
    async with sdk_mod.AsyncImednetSDK(
        api_key="key",
        security_key="secret",
        base_url="https://example.com",
    ):
        pass

    read_text_calls = 0
    ssl_calls = 0
    orig_read_text = Path.read_text
    orig_create_default_context = ssl.create_default_context

    def _spy_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal read_text_calls
        read_text_calls += 1
        return orig_read_text(self, *args, **kwargs)

    def _spy_ssl_context(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        nonlocal ssl_calls
        ssl_calls += 1
        return orig_create_default_context(*args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy_read_text)
    monkeypatch.setattr(ssl, "create_default_context", _spy_ssl_context)

    sync_sdk = _create_sdk()
    try:
        assert "sinks" not in sync_sdk.__dict__
        assert "sinks" in dir(sync_sdk)
    finally:
        sync_sdk.close()

    async with sdk_mod.AsyncImednetSDK(
        api_key="key",
        security_key="secret",
        base_url="https://example.com",
    ) as async_sdk:
        assert "sinks" not in async_sdk.__dict__
        assert "sinks" in dir(async_sdk)

    assert read_text_calls == 0
    assert ssl_calls == 0


@pytest.mark.asyncio
async def test_sinks_lazy_discovery_instance_cache_direct_assignment_and_metadata_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sdk.sinks defers discovery until read, caches per instance, reuses metadata, and allows assignment."""
    from pathlib import Path
    from types import SimpleNamespace

    init_sinks_calls = 0
    orig_init_sinks = sdk_mod._BaseSDK._init_sinks

    def _spy_init_sinks(self: sdk_mod._BaseSDK) -> Any:
        nonlocal init_sinks_calls
        init_sinks_calls += 1
        return orig_init_sinks(self)

    monkeypatch.setattr(sdk_mod._BaseSDK, "_init_sinks", _spy_init_sinks)

    sdk1 = _create_sdk()
    sdk2 = _create_sdk()
    assigned_sdk = _create_sdk()
    try:
        assert init_sinks_calls == 0

        # Direct assignment before first read skips _init_sinks completely
        custom_sinks = SimpleNamespace(marker="custom")
        assigned_sdk.sinks = custom_sinks  # type: ignore[assignment]
        assert assigned_sdk.sinks is custom_sinks
        assert init_sinks_calls == 0

        # First read on sdk1 resolves and caches per instance
        sinks1 = sdk1.sinks
        assert sinks1 is not None
        assert init_sinks_calls == 1
        assert sdk1.sinks is sinks1
        assert init_sinks_calls == 1

        # Second SDK instance reuses cached sinks plugin metadata with 0 Path.read_text calls
        read_text_calls = 0
        orig_read_text = Path.read_text

        def _spy_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            nonlocal read_text_calls
            read_text_calls += 1
            return orig_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _spy_read_text)
        sinks2 = sdk2.sinks
        assert sinks2 is not None
        assert sinks2 is not sinks1
        assert getattr(sinks1, "_sdk", None) is sdk1
        assert getattr(sinks2, "_sdk", None) is sdk2
        assert read_text_calls == 0

        # Direct assignment after read overrides cached instance attribute
        replacement_sinks = SimpleNamespace(marker="replacement")
        sdk1.sinks = replacement_sinks  # type: ignore[assignment]
        assert sdk1.sinks is replacement_sinks

        # Missing attribute still raises standard AttributeError
        with pytest.raises(AttributeError, match="'ImednetSDK' object has no attribute 'unknown'"):
            _ = sdk1.unknown
    finally:
        sdk1.close()
        sdk2.close()
        assigned_sdk.close()

    async with sdk_mod.AsyncImednetSDK(
        api_key="key",
        security_key="secret",
        base_url="https://example.com",
    ) as async_sdk:
        with pytest.raises(
            AttributeError, match="'AsyncImednetSDK' object has no attribute 'unknown'"
        ):
            _ = async_sdk.unknown


def test_sinks_missing_and_duplicate_plugin_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify None sinks caching when uninstalled and PluginLoadError on duplicate sinks plugins."""
    plugin_scans = 0

    def _empty_eps(*, group: str, name: str | None = None) -> list[Any]:
        nonlocal plugin_scans
        if group == "imednet.plugins":
            plugin_scans += 1
        return []

    monkeypatch.setattr(sdk_mod, "entry_points", _empty_eps)
    sdk = _create_sdk()
    try:
        assert plugin_scans == 0
        assert sdk.sinks is None
        assert plugin_scans == 1
        # Cached None in instance __dict__ avoids re-scanning on second read
        assert sdk.sinks is None
        assert plugin_scans == 1
    finally:
        sdk.close()

    monkeypatch.setattr(
        sdk_mod,
        "entry_points",
        lambda *, group, name=None: (
            [
                _FakeEntryPoint(lambda: object(), value="b.plugin:create_sinks", name="sinks"),
                _FakeEntryPoint(lambda: object(), value="a.plugin:create_sinks", name="sinks"),
            ]
            if group == "imednet.plugins" and name == "sinks"
            else []
        ),
    )
    dup_sdk = _create_sdk()
    try:
        with pytest.raises(
            PluginLoadError,
            match=(
                r"Multiple 'sinks' plugins were found in the 'imednet\.plugins' entry-point "
                r"group \(a\.plugin:create_sinks, b\.plugin:create_sinks\)\. "
                r"Please keep only one sinks plugin installed\."
            ),
        ):
            _ = dup_sdk.sinks
    finally:
        dup_sdk.close()


def test_entry_point_cache_respects_sys_path_mutations_and_importlib_patches(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify unpatched entry-point cache refreshes on sys.path changes and importlib.metadata patches."""
    import importlib.metadata as importlib_metadata

    pkg_dir = tmp_path / "site_pkgs"
    mod_file = pkg_dir / "temp_wf_mod.py"
    pkg_dir.mkdir(parents=True)
    mod_file.write_text(
        "class TempWorkflow:\n"
        "    supports_async = True\n"
        "    def __init__(self, sdk):\n"
        "        self.sdk = sdk\n",
        encoding="utf-8",
    )
    dist_info = pkg_dir / "temp_wf_pkg-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: temp-wf-pkg\nVersion: 0.1.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "[imednet.workflows]\ntemp_custom_wf = temp_wf_mod:TempWorkflow\n",
        encoding="utf-8",
    )

    # Warm default sys.path cache first
    base_sdk = _create_sdk()
    base_sdk.close()

    monkeypatch.syspath_prepend(str(pkg_dir))
    ext_sdk = _create_sdk()
    try:
        wf = ext_sdk.workflows.temp_custom_wf
        assert wf.sdk is ext_sdk
    finally:
        ext_sdk.close()

    # Duplicate builtin workflow name via sys.path still raises PluginLoadError on SDK creation
    dup_dir = tmp_path / "dup_pkgs"
    dup_info = dup_dir / "dup_wf_pkg-0.1.0.dist-info"
    dup_info.mkdir(parents=True)
    (dup_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: dup-wf-pkg\nVersion: 0.1.0\n",
        encoding="utf-8",
    )
    (dup_info / "entry_points.txt").write_text(
        "[imednet.workflows]\ndata_extraction = temp_wf_mod:TempWorkflow\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(dup_dir))
    with pytest.raises(
        PluginLoadError, match="Multiple workflows registered under the name 'data_extraction'"
    ):
        _create_sdk()

    # Patching importlib.metadata.entry_points directly also bypasses the cache
    monkeypatch.setattr(
        importlib_metadata,
        "entry_points",
        lambda *, group, name=None: (
            [_FakeEntryPoint(lambda: lambda s: ("patched", s), name="meta_wf")]
            if group == "imednet.workflows"
            else []
        ),
    )
    meta_sdk = _create_sdk()
    try:
        assert meta_sdk.workflows.meta_wf == ("patched", meta_sdk)
        assert meta_sdk.sinks is None
    finally:
        meta_sdk.close()

    # Exercise bounded cache eviction for both workflow and plugin entry-point caches
    monkeypatch.undo()
    monkeypatch.setattr(sdk_mod, "_CACHE_MAX_ENTRIES", 1)
    sdk_mod._WORKFLOW_ENTRY_POINTS_CACHE.clear()
    sdk_mod._PLUGIN_ENTRY_POINT_CACHE.clear()

    evict_sdk_1 = _create_sdk()
    _ = evict_sdk_1.sinks
    evict_sdk_1.close()
    assert len(sdk_mod._WORKFLOW_ENTRY_POINTS_CACHE) == 1
    assert len(sdk_mod._PLUGIN_ENTRY_POINT_CACHE) == 1

    monkeypatch.syspath_prepend(str(pkg_dir))
    evict_sdk_2 = _create_sdk()
    _ = evict_sdk_2.sinks
    evict_sdk_2.close()
    assert len(sdk_mod._WORKFLOW_ENTRY_POINTS_CACHE) == 1
    assert len(sdk_mod._PLUGIN_ENTRY_POINT_CACHE) == 1
