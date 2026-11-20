"""Apache Airflow provider metadata for ``apache-airflow-providers-imednet``."""

from __future__ import annotations

import sys
from importlib import import_module
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from importlib.metadata import PackageNotFoundError, version
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


def _provider_version() -> str:
    """Resolve the installed distribution version with a safe fallback."""
    try:
        return version("apache-airflow-providers-imednet")
    except PackageNotFoundError:  # pragma: no cover
        return "1.1.0"


def get_provider_info() -> dict[str, Any]:
    """Return Apache Airflow provider discovery metadata."""
    _ensure_airflow_provider_finder()
    hook_name = "iMednet"
    return {
        "package-name": "apache-airflow-providers-imednet",
        "name": hook_name,
        "description": "Apache Airflow provider for iMednet EDC clinical data workflows.",
        "versions": [_provider_version()],
        "integrations": [
            {
                "integration-name": hook_name,
                "external-doc-url": "https://fderuiter.github.io/imednet-toolkit/",
                "how-to-guide": ["/docs/how-to/airflow.rst"],
                "tags": ["service"],
            }
        ],
        "hooks": [
            {
                "integration-name": hook_name,
                "python-modules": ["airflow.providers.imednet.hooks"],
            }
        ],
        "operators": [
            {
                "integration-name": hook_name,
                "python-modules": ["airflow.providers.imednet.operators"],
            }
        ],
        "sensors": [
            {
                "integration-name": hook_name,
                "python-modules": ["airflow.providers.imednet.sensors"],
            }
        ],
        "connection-types": [
            {
                "connection-type": "imednet",
                "hook-class-name": "airflow.providers.imednet.hooks.ImednetHook",
            }
        ],
    }


__all__ = ["get_provider_info"]
