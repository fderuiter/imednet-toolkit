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
from typing import TYPE_CHECKING, Any

from . import export

if TYPE_CHECKING:
    from .hooks import ImednetHook
    from .operators import ImednetExportOperator
    from .sensors import ImednetJobSensor

_ALIAS_PREFIX = "airflow.providers.imednet"
_REAL_PREFIX = "apache_airflow_providers_imednet"


class _ImednetProviderAliasLoader(Loader):
    """Module loader that aliases ``airflow.providers.imednet.*`` to this package."""

    def __init__(self, target_name: str) -> None:
        """Initialize the alias loader with the backing module name."""
        self._target_name = target_name

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        """Return the canonical module instance from ``apache_airflow_providers_imednet``."""
        target = import_module(self._target_name)
        sys.modules[spec.name] = target
        return target

    def exec_module(self, module: ModuleType) -> None:
        """No-op execution because ``create_module`` returns the already-loaded target."""


class _ImednetProviderAliasFinder(MetaPathFinder):
    """MetaPathFinder mapping ``airflow.providers.imednet`` to ``apache_airflow_providers_imednet``."""

    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        """Resolve specs for ``airflow.providers.imednet`` and its submodules."""
        _ = (path, target)
        if fullname == _ALIAS_PREFIX:
            real_name = _REAL_PREFIX
            is_pkg = True
        elif fullname.startswith(_ALIAS_PREFIX + "."):
            suffix = fullname[len(_ALIAS_PREFIX) :]
            real_name = _REAL_PREFIX + suffix
            is_pkg = suffix in {".hooks", ".operators"}
        else:
            return None
        return ModuleSpec(fullname, _ImednetProviderAliasLoader(real_name), is_package=is_pkg)


def _ensure_provider_alias_registered() -> None:
    """Ensure ``airflow.providers.imednet`` resolves to ``apache_airflow_providers_imednet``."""
    if not any(isinstance(finder, _ImednetProviderAliasFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _ImednetProviderAliasFinder())


_ensure_provider_alias_registered()


def get_provider_info() -> dict[str, Any]:
    """Return Apache Airflow provider metadata for ``ProvidersManager`` discovery."""
    _ensure_provider_alias_registered()
    return {
        "package-name": "apache-airflow-providers-imednet",
        "name": "iMednet",
        "description": "Apache Airflow provider for iMednet EDC clinical data workflows.",
        "integrations": [
            {
                "integration-name": "iMednet",
                "external-doc-url": "https://fderuiter.github.io/imednet-toolkit/",
                "tags": ["clinical", "edc", "imednet"],
            }
        ],
        "hooks": [
            {
                "integration-name": "iMednet",
                "python-modules": ["airflow.providers.imednet.hooks"],
            }
        ],
        "operators": [
            {
                "integration-name": "iMednet",
                "python-modules": [
                    "airflow.providers.imednet.operators",
                    "airflow.providers.imednet.operators.export",
                ],
            }
        ],
        "sensors": [
            {
                "integration-name": "iMednet",
                "python-modules": ["airflow.providers.imednet.sensors"],
            }
        ],
        "connection-types": [
            {
                "connection-type": "imednet",
                "hook-class-name": "airflow.providers.imednet.hooks.ImednetHook",
                "hook-name": "iMednet",
                "conn-fields": {
                    "api_key": {
                        "label": "API Key (Extra Override)",
                        "schema": {
                            "type": ["string", "null"],
                            "format": "password",
                        },
                    },
                    "security_key": {
                        "label": "Security Key (Extra Override)",
                        "schema": {
                            "type": ["string", "null"],
                            "format": "password",
                        },
                    },
                    "oidc_token": {
                        "label": "OIDC Token",
                        "schema": {
                            "type": ["string", "null"],
                            "format": "password",
                        },
                    },
                    "base_url": {
                        "label": "Base URL (Extra Override)",
                        "schema": {
                            "type": ["string", "null"],
                        },
                    },
                },
                "ui-field-behaviour": {
                    "hidden-fields": ["schema", "port"],
                    "relabeling": {
                        "host": "Base URL",
                        "login": "API Key",
                        "password": "Security Key",  # pragma: allowlist secret
                    },
                    "placeholders": {
                        "host": "https://edc.prod.imednetapi.com",
                        "login": "Your iMednet API key",
                        "password": "Your iMednet security key",  # pragma: allowlist secret
                        "extra": '{"timeout": 30.0, "strict_mode": false}',
                    },
                },
            }
        ],
    }


def __getattr__(name: str) -> Any:
    """Lazily resolve Airflow hook, operator, and sensor exports."""
    if name == "ImednetHook":
        try:
            hook_cls: Any = import_module("apache_airflow_providers_imednet.hooks").ImednetHook
        except (ImportError, ModuleNotFoundError):  # pragma: no cover
            hook_cls = None
        globals()["ImednetHook"] = hook_cls
        return hook_cls
    if name == "ImednetExportOperator":
        try:
            operator_cls: Any = import_module(
                "apache_airflow_providers_imednet.operators"
            ).ImednetExportOperator
        except (ImportError, ModuleNotFoundError):  # pragma: no cover
            operator_cls = None
        globals()["ImednetExportOperator"] = operator_cls
        return operator_cls
    if name in {"ImednetJobSensor", "sensors"}:
        try:
            sensors_mod: Any = import_module("apache_airflow_providers_imednet.sensors")
            sensor_cls: Any = sensors_mod.ImednetJobSensor
        except (ImportError, ModuleNotFoundError):  # pragma: no cover
            sensors_mod = None
            sensor_cls = None
        globals()["sensors"] = sensors_mod
        globals()["ImednetJobSensor"] = sensor_cls
        return sensors_mod if name == "sensors" else sensor_cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include lazy attributes in module directory listings."""
    return sorted({*globals().keys(), *__all__, "sensors"})


__all__ = [
    "ImednetExportOperator",
    "ImednetHook",
    "ImednetJobSensor",
    "export",
    "get_provider_info",
]
