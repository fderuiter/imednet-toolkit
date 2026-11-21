"""Unit tests for airflow operators."""

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _setup_airflow(monkeypatch):
    """Helper function to  setup airflow."""
    airflow = ModuleType("airflow")
    airflow.__path__ = []
    hooks_pkg = ModuleType("airflow.hooks")
    hooks_pkg.__path__ = []
    hooks_base = ModuleType("airflow.sdk.bases.hook")
    operator_base = ModuleType("airflow.sdk.bases.operator")
    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.__path__ = []
    sdk_bases = ModuleType("airflow.sdk.bases")
    sdk_bases.__path__ = []
    sdk_defs = ModuleType("airflow.sdk.definitions")
    sdk_defs.__path__ = []
    sdk_ctx = ModuleType("airflow.sdk.definitions.context")
    sdk_dag = ModuleType("airflow.sdk.definitions.dag")
    models_mod = ModuleType("airflow.models")
    sensors_base = ModuleType("airflow.sensors.base")
    sensors_sdk = ModuleType("airflow.sdk.bases.sensor")
    exc_mod = ModuleType("airflow.exceptions")
    sdk_exc = ModuleType("airflow.sdk.exceptions")
    s3_mod = ModuleType("airflow.providers.amazon.aws.hooks.s3")

    class DummyBaseHook:
        """Test suite for DummyBaseHook."""

        @classmethod
        def get_connection(cls, conn_id):
            """Helper function to get connection."""
            raise NotImplementedError

    class DummyBaseOperator:
        """Test suite for DummyBaseOperator."""

        template_fields = ()

        def __init__(self, *a, **kw):
            """Initialize the test object."""

    class DummySensorOperator:
        """Test suite for DummySensorOperator."""

        template_fields = ()

        def __init__(self, *a, **kw):
            """Initialize the test object."""

    hooks_base.BaseHook = DummyBaseHook
    operator_base.BaseOperator = DummyBaseOperator
    models_mod.BaseOperator = DummyBaseOperator
    models_mod.DAG = DummyBaseOperator
    sensors_base.BaseSensorOperator = DummySensorOperator
    sensors_sdk.BaseSensorOperator = DummySensorOperator
    sdk_dag.DAG = DummyBaseOperator
    sdk_dag.dag = DummyBaseOperator

    class DummyAirflowError(Exception):
        """Test suite for DummyAirflowError."""

    exc_mod.AirflowException = DummyAirflowError
    sdk_exc.AirflowException = DummyAirflowError
    s3_mod.S3Hook = MagicMock

    sdk_bases.hook = hooks_base
    sdk_bases.operator = operator_base
    sdk_bases.sensor = sensors_sdk
    sdk_mod.bases = sdk_bases
    sdk_mod.exceptions = sdk_exc
    sdk_defs.context = sdk_ctx
    sdk_defs.dag = sdk_dag
    sdk_mod.definitions = sdk_defs
    airflow.sdk = sdk_mod
    airflow.hooks = hooks_pkg
    airflow.models = models_mod
    airflow.sensors = ModuleType("airflow.sensors")
    airflow.sensors.__path__ = []
    airflow.sensors.base = sensors_base
    airflow.providers = ModuleType("airflow.providers")
    airflow.providers.__path__ = []
    airflow.providers.amazon = ModuleType("airflow.providers.amazon")
    airflow.providers.amazon.__path__ = []
    airflow.providers.amazon.aws = ModuleType("airflow.providers.amazon.aws")
    airflow.providers.amazon.aws.__path__ = []
    airflow.providers.amazon.aws.hooks = ModuleType("airflow.providers.amazon.aws.hooks")
    airflow.providers.amazon.aws.hooks.__path__ = []
    airflow.providers.amazon.aws.hooks.s3 = s3_mod

    for mod_name in list(sys.modules):
        if mod_name == "apache_airflow_providers_imednet" or mod_name.startswith(
            "apache_airflow_providers_imednet."
        ):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    modules = {
        "airflow": airflow,
        "airflow.hooks": hooks_pkg,
        "airflow.sdk": sdk_mod,
        "airflow.sdk.bases": sdk_bases,
        "airflow.sdk.bases.hook": hooks_base,
        "airflow.sdk.bases.operator": operator_base,
        "airflow.sdk.bases.sensor": sensors_sdk,
        "airflow.sdk.definitions": sdk_defs,
        "airflow.sdk.definitions.context": sdk_ctx,
        "airflow.sdk.definitions.dag": sdk_dag,
        "airflow.models": models_mod,
        "airflow.sensors": airflow.sensors,
        "airflow.sensors.base": sensors_base,
        "airflow.providers": airflow.providers,
        "airflow.providers.amazon": airflow.providers.amazon,
        "airflow.providers.amazon.aws": airflow.providers.amazon.aws,
        "airflow.providers.amazon.aws.hooks": airflow.providers.amazon.aws.hooks,
        "airflow.providers.amazon.aws.hooks.s3": s3_mod,
        "airflow.exceptions": exc_mod,
        "airflow.sdk.exceptions": sdk_exc,
    }
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)


def _import_operators(monkeypatch):
    """Helper function to  import operators."""
    _setup_airflow(monkeypatch)
    import apache_airflow_providers_imednet.operators as ops

    return ops


def _import_sensors(monkeypatch):
    """Helper function to  import sensors."""
    _setup_airflow(monkeypatch)
    from apache_airflow_providers_imednet import sensors

    return sensors


def _patch_basehook(monkeypatch, extras=None):
    """Helper function to  patch basehook."""
    conn = SimpleNamespace(login=None, password=None, extra_dejson=extras or {})
    import apache_airflow_providers_imednet.hooks as hook_mod

    monkeypatch.setattr(
        hook_mod.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )
    return conn


def test_job_sensor(monkeypatch):
    """Test that job sensor."""
    _setup_airflow(monkeypatch)
    import apache_airflow_providers_imednet.operators as ops
    from apache_airflow_providers_imednet import sensors

    importlib.reload(sensors)
    _patch_basehook(monkeypatch)
    sdk = MagicMock()
    job = MagicMock(state="COMPLETED", is_failed=False, is_terminal=True, is_successful=True)
    sdk.jobs.get.return_value = job
    hook_inst = MagicMock(get_sdk_client=MagicMock(return_value=sdk))
    monkeypatch.setattr(sensors, "ImednetHook", MagicMock(return_value=hook_inst))

    sensor = sensors.ImednetJobSensor(task_id="t", study_key="S", batch_id="B")
    assert sensor.poke({}) is True
    sdk.jobs.get.assert_called_once_with("S", "B")
    sensors.ImednetHook.assert_called_once_with("imednet_default")
    hook_inst.get_sdk_client.assert_called_once_with()

    job.state = "FAILED"
    job.is_failed = True
    job.is_terminal = True
    with pytest.raises(ops.AirflowException):
        sensor.poke({})

    job.state = "RUNNING"
    job.is_failed = False
    job.is_terminal = False
    job.is_successful = False
    assert sensor.poke({}) is False


def test_job_sensor_fetches_result_url_and_fails_on_record_errors(monkeypatch):
    """ImednetJobSensor.poke fetches terminal resultUrl and raises AirflowException on failed Records."""
    import httpx

    from imednet import ImednetSDK

    _setup_airflow(monkeypatch)
    import apache_airflow_providers_imednet.operators as ops
    from apache_airflow_providers_imednet import sensors

    importlib.reload(sensors)
    _patch_basehook(monkeypatch)

    result_payloads: list[object] = [
        [{"recordKey": "REC-01", "status": "SUCCESS"}],
        [
            {"recordKey": "REC-01", "status": "SUCCESS"},
            {"recordKey": "REC-02", "status": "FAILED", "error": "Invalid Study Variable"},
        ],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs/B-1"):
            return httpx.Response(
                200,
                json={
                    "batchId": "B-1",
                    "state": "COMPLETED",
                    "resultUrl": "https://edc.trn.imednetapi.com/results/B-1.json",
                },
            )
        if request.url.path == "/results/B-1.json":
            return httpx.Response(200, json=result_payloads.pop(0))
        if request.url.path.endswith("/jobs/B-HTTP-ERR"):
            return httpx.Response(
                200,
                json={
                    "batchId": "B-HTTP-ERR",
                    "state": "COMPLETED",
                    "resultUrl": "https://edc.trn.imednetapi.com/results/err.json?api_key=RAW_SECRET_123",  # pragma: allowlist secret
                },
            )
        return httpx.Response(
            500,
            json={"message": "Internal error token=RAW_SECRET_123"},  # pragma: allowlist secret
        )

    def _make_sdk() -> ImednetSDK:
        return ImednetSDK(
            api_key="k",
            security_key="s",
            base_url="https://edc.trn.imednetapi.com",
            transport=httpx.MockTransport(handler),
        )

    hook_inst = MagicMock(get_sdk_client=MagicMock(side_effect=_make_sdk))
    monkeypatch.setattr(sensors, "ImednetHook", MagicMock(return_value=hook_inst))

    # 1. All Record items succeeded -> poke returns True
    sensor_ok = sensors.ImednetJobSensor(task_id="s_ok", study_key="S", batch_id="B-1")
    assert sensor_ok.poke({}) is True

    # 2. Terminal COMPLETED Job with failed Record item in resultUrl -> raises AirflowException
    sensor_rec_fail = sensors.ImednetJobSensor(task_id="s_fail", study_key="S", batch_id="B-1")
    with pytest.raises(ops.AirflowException, match="Invalid Study Variable"):
        sensor_rec_fail.poke({})

    # 3. HTTP 500 fetching resultUrl -> raises AirflowException with secrets redacted
    sensor_http_err = sensors.ImednetJobSensor(
        task_id="s_http_err",
        study_key="S",
        batch_id="B-HTTP-ERR",
    )
    with pytest.raises(ops.AirflowException, match="failed to fetch resultUrl") as exc_info:
        sensor_http_err.poke({})
    assert "RAW_SECRET_123" not in str(exc_info.value)


def test_provider_info_and_hook_metadata_contract():
    """Verify get_provider_info, entry point metadata, and ImednetHook attributes."""
    from importlib.metadata import distribution, entry_points

    import apache_airflow_providers_imednet as provider_pkg
    from apache_airflow_providers_imednet.hooks import ImednetHook

    assert ImednetHook.conn_name_attr == "imednet_conn_id"
    assert ImednetHook.default_conn_name == "imednet_default"
    assert ImednetHook.conn_type == "imednet"
    assert ImednetHook.hook_name == "iMednet"

    info = provider_pkg.get_provider_info()
    assert info["package-name"] == "apache-airflow-providers-imednet"
    assert info["name"] == "iMednet"
    assert info["connection-types"] == [
        {
            "connection-type": "imednet",
            "hook-class-name": "airflow.providers.imednet.hooks.ImednetHook",
        }
    ]

    eps = entry_points(group="apache_airflow_provider")
    matched = [ep for ep in eps if ep.value == "apache_airflow_providers_imednet:get_provider_info"]
    assert len(matched) == 1
    assert matched[0].load() is provider_pkg.get_provider_info

    dist = distribution("apache-airflow-providers-imednet")
    requires = dist.requires or []
    assert any(req.startswith("imednet-workflows") for req in requires)
    extras = set(dist.metadata.get_all("Provides-Extra") or [])
    assert {"duckdb", "export", "mongodb", "neo4j", "snowflake"}.issubset(extras)


def test_provider_alias_sync_and_finder_deduplication(monkeypatch):
    """Verify _sync_airflow_provider_aliases and finder deduplication across reloads."""
    _setup_airflow(monkeypatch)
    import apache_airflow_providers_imednet as provider_pkg

    importlib.reload(provider_pkg)
    alias_pkg = importlib.import_module("airflow.providers.imednet")
    alias_hooks = importlib.import_module("airflow.providers.imednet.hooks")
    alias_ops = importlib.import_module("airflow.providers.imednet.operators")

    assert alias_pkg is sys.modules["apache_airflow_providers_imednet"]
    assert alias_hooks is sys.modules["apache_airflow_providers_imednet.hooks"]
    assert alias_ops is sys.modules["apache_airflow_providers_imednet.operators"]
    assert sys.modules["airflow.providers"].__dict__["imednet"] is alias_pkg

    reloaded_ops = importlib.reload(sys.modules["apache_airflow_providers_imednet.operators"])
    assert sys.modules["airflow.providers.imednet.operators"] is reloaded_ops

    # Simulate duplicate stale finder & unloaded target module
    stale_finder = type("StaleFinder", (), {"_imednet_airflow_provider_finder": True})()
    sys.meta_path.insert(0, stale_finder)
    sys.meta_path.append(stale_finder)
    monkeypatch.setitem(sys.modules, "airflow.providers.imednet.ghost", ModuleType("ghost"))
    saved_root = sys.modules.pop("apache_airflow_providers_imednet")
    try:
        provider_pkg._ensure_airflow_provider_finder()
        assert "imednet" not in sys.modules["airflow.providers"].__dict__
    finally:
        sys.modules["apache_airflow_providers_imednet"] = saved_root
        provider_pkg._sync_airflow_provider_aliases()

    assert "airflow.providers.imednet.ghost" not in sys.modules
    finders = [f for f in sys.meta_path if getattr(f, "_imednet_airflow_provider_finder", False)]
    assert len(finders) == 1
    assert isinstance(finders[0], provider_pkg._ImednetAirflowProviderFinder)


@pytest.fixture(autouse=True, scope="module")
def _cleanup_mocked_airflow_provider_modules():
    """Evict provider modules reloaded against dummy Airflow stubs on module setup and teardown."""
    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow.providers.imednet")):
            sys.modules.pop(mod, None)
    yield
    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow.providers.imednet")):
            sys.modules.pop(mod, None)
    providers_pkg = sys.modules.get("airflow.providers")
    if providers_pkg is not None:
        providers_pkg.__dict__.pop("imednet", None)
