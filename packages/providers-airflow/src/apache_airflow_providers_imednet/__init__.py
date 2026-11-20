"""Airflow provider for iMednet.

This package provides hooks, operators, and sensors for integrating iMednet
EDC data workflows into Apache Airflow pipelines.
"""

from __future__ import annotations

import sys
from importlib import import_module
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import Any

_ALIAS_PREFIX = "airflow.providers.imednet"
_TARGET_PREFIX = "apache_airflow_providers_imednet"
_PACKAGE_ALIASES = frozenset(
    {
        "airflow.providers.imednet",
        "airflow.providers.imednet.hooks",
        "airflow.providers.imednet.operators",
    }
)


class _ImednetAirflowProviderFinder(MetaPathFinder, Loader):
    """Map ``airflow.providers.imednet`` imports to ``apache_airflow_providers_imednet``."""

    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        _ = (path, target)
        if fullname == _ALIAS_PREFIX or fullname.startswith(f"{_ALIAS_PREFIX}."):
            return ModuleSpec(
                fullname,
                self,
                is_package=fullname in _PACKAGE_ALIASES,
            )
        return None

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        _ = spec
        return None

    def exec_module(self, module: ModuleType) -> None:
        alias_name = module.__name__
        target_name = alias_name.replace(_ALIAS_PREFIX, _TARGET_PREFIX, 1)
        target_mod = import_module(target_name)
        for key, val in target_mod.__dict__.items():
            if key not in {"__name__", "__package__", "__spec__", "__loader__", "__path__"}:
                module.__dict__[key] = val
        if hasattr(target_mod, "__path__"):
            module.__path__ = list(target_mod.__path__)
        sys.modules[alias_name] = target_mod


def _ensure_airflow_provider_finder() -> None:
    """Ensure the ``airflow.providers.imednet`` import alias finder is registered."""
    if not any(isinstance(finder, _ImednetAirflowProviderFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _ImednetAirflowProviderFinder())


_ensure_airflow_provider_finder()

from . import export
from .get_provider_info import get_provider_info

try:  # pragma: no cover - optional Airflow dependencies may be missing
    from .hooks import ImednetHook
except (ImportError, ModuleNotFoundError):  # pragma: no cover - hook requires Airflow extras
    ImednetHook = None  # type: ignore

try:  # pragma: no cover - optional Airflow dependencies may be missing
    from .operators import ImednetExportOperator
except (ImportError, ModuleNotFoundError):  # pragma: no cover - operator requires Airflow extras
    ImednetExportOperator = None  # type: ignore

try:  # pragma: no cover - optional Airflow dependencies may be missing
    sensors = import_module("apache_airflow_providers_imednet.sensors")
    ImednetJobSensor = sensors.ImednetJobSensor
except (ImportError, ModuleNotFoundError):  # pragma: no cover - sensor requires Airflow extras
    ImednetJobSensor = None
    sensors = None  # type: ignore

try:  # pragma: no cover - optional Airflow dependencies may be missing
    triggers = import_module("apache_airflow_providers_imednet.triggers")
    ImednetJobTrigger = triggers.ImednetJobTrigger
except (ImportError, ModuleNotFoundError):  # pragma: no cover - trigger requires Airflow extras
    ImednetJobTrigger = None
    triggers = None  # type: ignore

__all__ = [
    "ImednetExportOperator",
    "ImednetHook",
    "ImednetJobSensor",
    "ImednetJobTrigger",
    "export",
    "get_provider_info",
]
