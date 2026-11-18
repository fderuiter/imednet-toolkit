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

    modules = {
        "airflow": airflow,
        "airflow.hooks": hooks_pkg,
        "airflow.sdk": sdk_mod,
        "airflow.sdk.bases": sdk_bases,
        "airflow.sdk.bases.hook": hooks_base,
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
