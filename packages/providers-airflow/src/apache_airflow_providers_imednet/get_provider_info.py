"""Provider metadata entry point for Apache Airflow discovery."""

from __future__ import annotations

from typing import Any


def get_provider_info() -> dict[str, Any]:
    """Return Apache Airflow provider metadata for ``apache-airflow-providers-imednet``."""
    from . import _ensure_airflow_provider_finder

    _ensure_airflow_provider_finder()
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
                "tags": ["clinical", "edc", "healthcare", "imednet", "protocol"],
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
                    "airflow.providers.imednet.operators",
                    "airflow.providers.imednet.operators.export",
                ],
            }
        ],
        "sensors": [
            {
                "integration-name": hook_name,
                "python-modules": [
                    "airflow.providers.imednet.sensors",
                ],
            }
        ],
        "triggers": [
            {
                "integration-name": hook_name,
                "python-modules": [
                    "airflow.providers.imednet.triggers",
                ],
            }
        ],
        "connection-types": [
            {
                "connection-type": conn_type,
                "hook-class-name": "airflow.providers.imednet.hooks.ImednetHook",
                "hook-name": hook_name,
                "conn-fields": {
                    "base_url": {
                        "label": "Base URL",
                        "description": (
                            "Optional iMednet API base URL override "
                            "(defaults to https://edc.prod.imednetapi.com)."
                        ),
                        "schema": {
                            "type": ["string", "null"],
                        },
                    },
                    "oidc_token": {
                        "label": "OIDC Token",
                        "description": "Optional OIDC bearer token for token-based authentication.",
                        "schema": {
                            "type": ["string", "null"],
                            "format": "password",
                        },
                    },
                    "timeout": {
                        "label": "Request Timeout (seconds)",
                        "description": "Optional HTTP request timeout in seconds.",
                        "schema": {
                            "type": ["number", "null"],
                            "minimum": 0,
                        },
                    },
                    "strict_mode": {
                        "label": "Strict Mode",
                        "description": "Enable strict schema validation on API responses.",
                        "schema": {
                            "type": ["boolean", "null"],
                        },
                    },
                    "simulation_mode": {
                        "label": "Simulation Mode",
                        "description": "Enable dry-run simulation mode where supported.",
                        "schema": {
                            "type": ["boolean", "null"],
                        },
                    },
                },
                "ui-field-behaviour": {
                    "hidden-fields": ["host", "schema", "port", "extra"],
                    "relabeling": {
                        "login": "API Key",
                        "password": "Security Key",  # pragma: allowlist secret
                    },
                    "placeholders": {
                        "login": "iMednet API Key",
                        "password": "iMednet Security Key",  # pragma: allowlist secret
                        "extra__imednet__base_url": "https://edc.prod.imednetapi.com",
                        "extra__imednet__timeout": "30.0",
                    },
                },
            }
        ],
    }


__all__ = ["get_provider_info"]
