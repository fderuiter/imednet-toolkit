"""Compatibility shim for different Airflow versions.

This module provides fallbacks for Airflow-specific classes and exceptions
to allow the package to be imported even when Airflow is not installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from airflow.exceptions import AirflowException
    from airflow.sdk.definitions.context import Context
else:  # pragma: no cover - typing fallback for optional Airflow dependency
    try:
        from airflow.sdk.definitions.context import Context  # type: ignore
    except (ImportError, ModuleNotFoundError):
        Context = dict[str, Any]
    try:
        from airflow.exceptions import AirflowException
    except (ImportError, ModuleNotFoundError):

        class _FallbackAirflowError(Exception):
            """Fallback exception used when ``airflow.exceptions.AirflowException`` is unavailable."""

        AirflowException = _FallbackAirflowError

__all__ = ["AirflowException", "Context"]
