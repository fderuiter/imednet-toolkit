"""Airflow provider for iMednet.

This package provides hooks, operators, and sensors for integrating iMednet
EDC data workflows into Apache Airflow pipelines.
"""

from __future__ import annotations

import sys
from contextlib import suppress
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


def _sync_airflow_provider_aliases() -> None:
    """Keep ``airflow.providers.imednet`` aliases bound to active target modules."""
    _ensure_airflow_provider_finder()
    for alias_name in list(sys.modules):
        if alias_name != _ALIAS_PREFIX and not alias_name.startswith(f"{_ALIAS_PREFIX}."):
            continue
        target_name = alias_name.replace(_ALIAS_PREFIX, _TARGET_PREFIX, 1)
        target_mod = sys.modules.get(target_name)
        parent_name, _, attr = alias_name.rpartition(".")
        parent_mod = sys.modules.get(parent_name) if parent_name else None
        if target_mod is None:
            old_mod = sys.modules.pop(alias_name, None)
            if parent_mod is not None and attr and getattr(parent_mod, attr, None) is old_mod:
                with suppress(AttributeError):
                    delattr(parent_mod, attr)
        else:
            sys.modules[alias_name] = target_mod
            if parent_mod is not None and attr:
                with suppress(AttributeError):
                    setattr(parent_mod, attr, target_mod)


def get_provider_info() -> dict[str, Any]:
    """Return Apache Airflow provider metadata for discovery."""
    _sync_airflow_provider_aliases()
    hook_name = "iMednet"
    conn_type = "imednet"
    return {
        "package-name": "apache-airflow-providers-imednet",
        "name": hook_name,
        "description": "Apache Airflow provider for iMednet EDC clinical data workflows.",
        "integrations": [
            {
                "integration-name": hook_name,
                "external-doc-url": "https://fderuiter.github.io/imednet-toolkit/",
                "tags": ["clinical", "edc", "imednet"],
            }
        ],
        "hooks": [
            {
                "integration-name": hook_name,
                "python-modules": [
                    "airflow.providers.imednet.hooks",
                ],
            }
        ],
        "operators": [
            {
                "integration-name": hook_name,
                "python-modules": [
                    "apache_airflow_providers_imednet.operators.export",
                ],
            }
        ],
        "sensors": [
            {
                "integration-name": hook_name,
                "python-modules": [
                    "apache_airflow_providers_imednet.sensors",
                ],
            }
        ],
        "connection-types": [
            {
                "connection-type": conn_type,
                "hook-class-name": "airflow.providers.imednet.hooks.ImednetHook",
            }
        ],
    }


_ensure_airflow_provider_finder()

from . import export

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

_sync_airflow_provider_aliases()

__all__ = [
    "ImednetExportOperator",
    "ImednetHook",
    "ImednetJobSensor",
    "export",
    "get_provider_info",
]
