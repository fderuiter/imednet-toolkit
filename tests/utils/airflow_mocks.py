"""Shared dummy Apache Airflow module fixtures for offline unit tests."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

_DUMMY_CACHE: dict[bool, tuple[dict[str, ModuleType | None], type, ModuleType | None]] = {}


def _build_dummy_airflow_modules(
    include_sensors_and_aws: bool,
) -> tuple[dict[str, ModuleType | None], type, ModuleType | None]:
    cached = _DUMMY_CACHE.get(include_sensors_and_aws)
    if cached is not None:
        return cached

    airflow_mod = ModuleType("airflow")
    airflow_mod.__path__ = []
    hooks_pkg = ModuleType("airflow.hooks")
    hooks_pkg.__path__ = []
    hooks_mod = ModuleType("airflow.sdk.bases.hook")
    operator_mod = ModuleType("airflow.sdk.bases.operator")
    sensor_mod = ModuleType("airflow.sdk.bases.sensor")
    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.__path__ = []
    sdk_bases = ModuleType("airflow.sdk.bases")
    sdk_bases.__path__ = []
    sdk_defs = ModuleType("airflow.sdk.definitions")
    sdk_defs.__path__ = []
    sdk_ctx = ModuleType("airflow.sdk.definitions.context")
    sdk_dag = ModuleType("airflow.sdk.definitions.dag")
    sdk_taskgroup = ModuleType("airflow.sdk.definitions.taskgroup")
    sdk_xcom_arg = ModuleType("airflow.sdk.definitions.xcom_arg")
    models_mod = ModuleType("airflow.models")

    class DummyBaseHook:
        """Minimal BaseHook stub."""

        @classmethod
        def get_connection(cls, conn_id: str) -> Any:  # pragma: no cover
            """Raise unless patched by test."""
            raise NotImplementedError

        def get_conn(self) -> Any:  # pragma: no cover
            """Airflow BaseHook contract interface."""
            raise NotImplementedError

    class DummyBaseOperator:
        """Minimal BaseOperator stub."""

        template_fields = ()

        def __init__(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
            """Initialize dummy operator."""

    class DummyXComArg:
        """Minimal XComArg stub for Airflow 3.4+ operator initialization."""

        @staticmethod
        def apply_upstream_relationship(*args: Any, **kwargs: Any) -> None:
            """No-op upstream XCom relationship registration."""

    hooks_mod.BaseHook = DummyBaseHook  # type: ignore[attr-defined]
    operator_mod.BaseOperator = DummyBaseOperator  # type: ignore[attr-defined]
    sensor_mod.BaseSensorOperator = DummyBaseOperator  # type: ignore[attr-defined]
    models_mod.BaseOperator = DummyBaseOperator  # type: ignore[attr-defined]
    models_mod.DAG = DummyBaseOperator  # type: ignore[attr-defined]
    sdk_dag.DAG = DummyBaseOperator  # type: ignore[attr-defined]
    sdk_dag.dag = DummyBaseOperator  # type: ignore[attr-defined]
    sdk_taskgroup.TaskGroup = DummyBaseOperator  # type: ignore[attr-defined]
    sdk_xcom_arg.XComArg = DummyXComArg  # type: ignore[attr-defined]

    sdk_bases.hook = hooks_mod  # type: ignore[attr-defined]
    sdk_bases.operator = operator_mod  # type: ignore[attr-defined]
    sdk_bases.sensor = sensor_mod  # type: ignore[attr-defined]
    sdk_mod.bases = sdk_bases  # type: ignore[attr-defined]
    sdk_defs.context = sdk_ctx  # type: ignore[attr-defined]
    sdk_defs.dag = sdk_dag  # type: ignore[attr-defined]
    sdk_defs.taskgroup = sdk_taskgroup  # type: ignore[attr-defined]
    sdk_defs.xcom_arg = sdk_xcom_arg  # type: ignore[attr-defined]
    sdk_mod.definitions = sdk_defs  # type: ignore[attr-defined]
    airflow_mod.sdk = sdk_mod  # type: ignore[attr-defined]
    airflow_mod.hooks = hooks_pkg  # type: ignore[attr-defined]
    airflow_mod.models = models_mod  # type: ignore[attr-defined]

    modules: dict[str, ModuleType | None] = {
        "airflow": airflow_mod,
        "airflow.hooks": hooks_pkg,
        "airflow.hooks.base": None,
        "airflow.triggers": None,
        "airflow.triggers.base": None,
        "airflow.configuration": None,
        "airflow.sdk": sdk_mod,
        "airflow.sdk.bases": sdk_bases,
        "airflow.sdk.bases.hook": hooks_mod,
        "airflow.sdk.bases.operator": operator_mod,
        "airflow.sdk.bases.sensor": sensor_mod,
        "airflow.sdk.definitions": sdk_defs,
        "airflow.sdk.definitions.context": sdk_ctx,
        "airflow.sdk.definitions.dag": sdk_dag,
        "airflow.sdk.definitions.taskgroup": sdk_taskgroup,
        "airflow.sdk.definitions.xcom_arg": sdk_xcom_arg,
        "airflow.models": models_mod,
        "airflow.exceptions": None,
        "airflow.sdk.exceptions": None,
        "airflow.sensors": None,
        "airflow.sensors.base": None,
        "airflow.providers": None,
        "airflow.providers.openlineage": None,
        "airflow.providers.openlineage.extractors": None,
        "airflow.providers.common": None,
        "airflow.providers.common.compat": None,
        "airflow.providers.common.compat.openlineage": None,
        "airflow.providers.common.compat.openlineage.facet": None,
    }

    s3_mod: ModuleType | None = None
    if include_sensors_and_aws:
        sensors_pkg = ModuleType("airflow.sensors")
        sensors_pkg.__path__ = []
        sensors_base = ModuleType("airflow.sensors.base")
        sensors_sdk = ModuleType("airflow.sdk.bases.sensor")
        exc_mod = ModuleType("airflow.exceptions")
        sdk_exc = ModuleType("airflow.sdk.exceptions")
        providers_pkg = ModuleType("airflow.providers")
        providers_pkg.__path__ = []
        amazon_pkg = ModuleType("airflow.providers.amazon")
        amazon_pkg.__path__ = []
        aws_pkg = ModuleType("airflow.providers.amazon.aws")
        aws_pkg.__path__ = []
        aws_hooks_pkg = ModuleType("airflow.providers.amazon.aws.hooks")
        aws_hooks_pkg.__path__ = []
        s3_mod = ModuleType("airflow.providers.amazon.aws.hooks.s3")

        class DummySensorOperator:
            """Minimal BaseSensorOperator stub."""

            template_fields = ()

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                """Initialize dummy sensor."""

        class DummyAirflowError(Exception):
            """Minimal AirflowException stub."""

        sensors_base.BaseSensorOperator = DummySensorOperator  # type: ignore[attr-defined]
        sensors_sdk.BaseSensorOperator = DummySensorOperator  # type: ignore[attr-defined]
        exc_mod.AirflowException = DummyAirflowError  # type: ignore[attr-defined]
        sdk_exc.AirflowException = DummyAirflowError  # type: ignore[attr-defined]
        s3_mod.S3Hook = MagicMock  # type: ignore[attr-defined]

        sdk_bases.sensor = sensors_sdk  # type: ignore[attr-defined]
        sdk_mod.exceptions = sdk_exc  # type: ignore[attr-defined]
        sensors_pkg.base = sensors_base  # type: ignore[attr-defined]
        airflow_mod.sensors = sensors_pkg  # type: ignore[attr-defined]
        aws_hooks_pkg.s3 = s3_mod  # type: ignore[attr-defined]
        aws_pkg.hooks = aws_hooks_pkg  # type: ignore[attr-defined]
        amazon_pkg.aws = aws_pkg  # type: ignore[attr-defined]
        providers_pkg.amazon = amazon_pkg  # type: ignore[attr-defined]
        airflow_mod.providers = providers_pkg  # type: ignore[attr-defined]

        modules.update(
            {
                "airflow.sdk.bases.sensor": sensors_sdk,
                "airflow.sensors": sensors_pkg,
                "airflow.sensors.base": sensors_base,
                "airflow.providers": providers_pkg,
                "airflow.providers.amazon": amazon_pkg,
                "airflow.providers.amazon.aws": aws_pkg,
                "airflow.providers.amazon.aws.hooks": aws_hooks_pkg,
                "airflow.providers.amazon.aws.hooks.s3": s3_mod,
                "airflow.exceptions": exc_mod,
                "airflow.sdk.exceptions": sdk_exc,
            }
        )

    result = (modules, DummyBaseHook, s3_mod)
    _DUMMY_CACHE[include_sensors_and_aws] = result
    return result


def setup_dummy_airflow(
    monkeypatch: Any,
    *,
    include_sensors_and_aws: bool = False,
) -> None:
    """Populate ``sys.modules`` with lightweight Airflow 3.x SDK stubs."""
    modules, expected_hook_cls, s3_mod = _build_dummy_airflow_modules(include_sensors_and_aws)
    if s3_mod is not None:
        s3_mod.S3Hook = MagicMock  # type: ignore[attr-defined]

    compat_mod = sys.modules.get("apache_airflow_providers_imednet._airflow_compat")
    provider_needs_eviction = (
        compat_mod is not None and getattr(compat_mod, "BaseHook", None) is not expected_hook_cls
    ) or (compat_mod is None and "apache_airflow_providers_imednet" in sys.modules)
    if provider_needs_eviction:
        for mod_name in list(sys.modules):
            if mod_name.startswith(
                ("apache_airflow_providers_imednet", "airflow.providers.imednet")
            ):
                monkeypatch.delitem(sys.modules, mod_name, raising=False)

    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)
