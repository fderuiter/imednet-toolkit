"""Apache Airflow provider metadata for ``apache-airflow-providers-imednet``."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any


def _provider_version() -> str:
    """Resolve the installed distribution version with a safe fallback."""
    try:
        return version("apache-airflow-providers-imednet")
    except PackageNotFoundError:
        return "1.1.0"


def get_provider_info() -> dict[str, Any]:
    """Return Apache Airflow provider discovery metadata."""
    return {
        "package-name": "apache-airflow-providers-imednet",
        "name": "iMedNet",
        "description": "Apache Airflow provider for iMedNet EDC.",
        "versions": [_provider_version()],
        "integrations": [
            {
                "integration-name": "iMedNet",
                "external-doc-url": "https://fderuiter.github.io/imednet-toolkit/",
                "how-to-guide": ["/docs/how-to/airflow.rst"],
                "tags": ["service"],
            }
        ],
        "hooks": [
            {
                "integration-name": "iMedNet",
                "python-modules": ["airflow.providers.imednet.hooks"],
            }
        ],
        "operators": [
            {
                "integration-name": "iMedNet",
                "python-modules": ["airflow.providers.imednet.operators"],
            }
        ],
        "sensors": [
            {
                "integration-name": "iMedNet",
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
