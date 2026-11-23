"""Unit tests for airflow operators."""

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.utils.airflow_mocks import setup_dummy_airflow


def _setup_airflow(monkeypatch):
    """Helper function to setup airflow."""
    setup_dummy_airflow(monkeypatch, include_sensors_and_aws=True)


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
    from apache_airflow_providers_imednet import _airflow_compat, sensors

    importlib.reload(_airflow_compat)
    importlib.reload(ops)
    importlib.reload(sensors)
    _patch_basehook(monkeypatch)
    sdk = MagicMock()
    job = MagicMock(state="COMPLETED", is_failed=False, is_terminal=True, is_successful=True)
    sdk.jobs.get.return_value = job
    hook_inst = MagicMock(get_sdk_client=MagicMock(return_value=sdk))
    monkeypatch.setattr(sensors, "ImednetHook", MagicMock(return_value=hook_inst))

    sensor = sensors.ImednetJobSensor(task_id="t", study_key="S", batch_id="B")
    poke_res = sensor.poke({})
    assert isinstance(poke_res, _airflow_compat.PokeReturnValue)
    assert bool(poke_res) is True
    assert poke_res.xcom_value == {"study_key": "S", "batch_id": "B", "state": "COMPLETED"}
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
    from apache_airflow_providers_imednet import _airflow_compat, sensors

    importlib.reload(_airflow_compat)
    importlib.reload(ops)
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

    # 1. All Record items succeeded -> poke returns PokeReturnValue with results excluded
    sensor_ok = sensors.ImednetJobSensor(task_id="s_ok", study_key="S", batch_id="B-1")
    poke_ok = sensor_ok.poke({})
    assert isinstance(poke_ok, _airflow_compat.PokeReturnValue)
    assert bool(poke_ok) is True
    assert "results" not in poke_ok.xcom_value

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
    assert info["triggers"] == [
        {
            "integration-name": "iMednet",
            "python-modules": ["airflow.providers.imednet.triggers"],
        }
    ]
    assert len(info["connection-types"]) == 1
    conn_type_meta = info["connection-types"][0]
    assert conn_type_meta["connection-type"] == "imednet"
    assert conn_type_meta["hook-class-name"] == "airflow.providers.imednet.hooks.ImednetHook"
    assert conn_type_meta["hook-name"] == "iMednet"
    assert conn_type_meta["ui-field-behaviour"]["hidden-fields"] == [
        "host",
        "schema",
        "port",
        "extra",
    ]
    assert ImednetHook.get_ui_field_behaviour()["hidden_fields"] == [
        "host",
        "schema",
        "port",
        "extra",
    ]
    assert (
        conn_type_meta["ui-field-behaviour"]["placeholders"]
        == ImednetHook.get_ui_field_behaviour()["placeholders"]
    )
    assert conn_type_meta["conn-fields"]["oidc_token"]["schema"]["format"] == "password"
    assert set(conn_type_meta["conn-fields"]) == {
        "base_url",
        "oidc_token",
        "timeout",
        "strict_mode",
        "simulation_mode",
    }

    eps = entry_points(group="apache_airflow_provider")
    matched = [ep for ep in eps if ep.value == "apache_airflow_providers_imednet:get_provider_info"]
    assert len(matched) == 1
    assert matched[0].load() is provider_pkg.get_provider_info

    dist = distribution("apache-airflow-providers-imednet")
    requires = dist.requires or []
    assert any(req.startswith("imednet-workflows") for req in requires)
    extras = set(dist.metadata.get_all("Provides-Extra") or [])
    assert {
        "all",
        "amazon",
        "duckdb",
        "export",
        "mongodb",
        "neo4j",
        "openlineage",
        "sinks",
        "snowflake",
        "workflows",
    }.issubset(extras)


def test_provider_alias_resyncs_on_package_reimport(monkeypatch):
    """Cached airflow.providers.imednet aliases stay identical across target re-imports."""
    _setup_airflow(monkeypatch)
    providers_pkg = importlib.import_module("airflow.providers")
    initial_pkg = importlib.import_module("apache_airflow_providers_imednet")
    initial_alias_hooks = importlib.import_module("airflow.providers.imednet.hooks")

    providers_pkg.imednet = initial_pkg  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "airflow.providers.imednet.stale_submod",
        ModuleType("airflow.providers.imednet.stale_submod"),
    )

    for mod_name in [
        name for name in list(sys.modules) if name.startswith("apache_airflow_providers_imednet")
    ]:
        monkeypatch.delitem(sys.modules, mod_name, raising=False)

    reloaded_pkg = importlib.import_module("apache_airflow_providers_imednet")
    reloaded_alias_hooks = importlib.import_module("airflow.providers.imednet.hooks")
    reloaded_target_hooks = importlib.import_module("apache_airflow_providers_imednet.hooks")

    assert reloaded_pkg is not initial_pkg
    assert reloaded_alias_hooks is not initial_alias_hooks
    assert reloaded_alias_hooks.ImednetHook is reloaded_target_hooks.ImednetHook
    assert providers_pkg.imednet is reloaded_pkg
    assert "airflow.providers.imednet.stale_submod" not in sys.modules


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


def test_airflow_compat_centralizes_sdk_exports_and_offline_fallbacks(monkeypatch):
    """_airflow_compat exports all SDK symbols and resolves _Fallback* stubs without Airflow 2.x."""
    import builtins

    from tests.conftest import isolate_airflow_provider_modules

    isolate_airflow_provider_modules(monkeypatch)

    real_import = builtins.__import__
    blocked_v2_imports: list[str] = []

    def _offline_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "airflow" or name.startswith("airflow."):
            if name.startswith(
                ("airflow.hooks", "airflow.models", "airflow.sensors", "airflow.exceptions")
            ):
                blocked_v2_imports.append(name)
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _offline_import)

    compat = importlib.import_module("apache_airflow_providers_imednet._airflow_compat")
    export_mod = importlib.import_module("apache_airflow_providers_imednet.operators.export")
    sensors_mod = importlib.import_module("apache_airflow_providers_imednet.sensors")

    assert compat.__all__ == [
        "AirflowException",
        "AirflowNotFoundException",
        "AirflowSecretsBackendAccessDenied",
        "AirflowSensorTimeout",
        "AirflowSkipException",
        "BaseHook",
        "BaseOperator",
        "BaseSensorOperator",
        "BaseTrigger",
        "Context",
        "PokeReturnValue",
        "TaskDeferralError",
        "TaskDeferralTimeout",
        "TaskDeferred",
        "TriggerEvent",
    ]
    # Verify offline PokeReturnValue and resume_execution stubs work without Airflow installed
    offline_poke = compat.PokeReturnValue(is_done=True, xcom_value={"state": "COMPLETED"})
    assert bool(offline_poke) is True
    assert offline_poke == compat.PokeReturnValue(is_done=True, xcom_value={"state": "COMPLETED"})
    offline_sensor_soft = sensors_mod.ImednetJobSensor(
        task_id="off_soft",
        study_key="S1",
        batch_id="B1",
        soft_fail=True,
    )
    with pytest.raises(compat.AirflowSkipException):
        offline_sensor_soft.resume_execution("__fail__", {"error": "Trigger timeout"}, {})
    with pytest.raises(compat.TaskDeferralError):
        offline_sensor_soft.resume_execution("__fail__", {"error": "Worker crashed"}, {})
    assert blocked_v2_imports == []
    assert export_mod.BaseOperator is compat.BaseOperator
    assert sensors_mod.BaseSensorOperator is compat.BaseSensorOperator
    assert issubclass(export_mod.ImednetExportOperator, compat.BaseOperator)
    assert issubclass(sensors_mod.ImednetJobSensor, compat.BaseSensorOperator)
    assert compat.BaseOperator.__module__ == "apache_airflow_providers_imednet._airflow_compat"
    assert (
        compat.BaseSensorOperator.__module__ == "apache_airflow_providers_imednet._airflow_compat"
    )


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
