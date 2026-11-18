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
    assert (
        sdk.create_record(
            "S1", [{"formKey": "F1", "data": {"a": 1}}], email_notify="notify@example.com"
        )
        == "JOB"
    )
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
    assert calls["create"] == ("S1", [{"formKey": "F1", "data": {"a": 1}}], "notify@example.com")
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
