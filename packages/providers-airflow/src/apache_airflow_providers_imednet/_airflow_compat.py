"""Compatibility shim for different Airflow versions.

This module provides fallbacks for Airflow-specific classes and exceptions
to allow the package to be imported even when Airflow is not installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    try:
        from airflow.sdk.bases.hook import BaseHook
    except ImportError:
        try:
            from airflow.hooks.base import BaseHook  # type: ignore[attr-defined]
        except ImportError:

            class BaseHook:  # type: ignore[no-redef]
                """Fallback BaseHook typing stub."""

    try:
        from airflow.sdk.exceptions import AirflowException
    except ImportError:
        from airflow.exceptions import AirflowException  # type: ignore[attr-defined]
    from airflow.sdk.definitions.context import Context
else:  # pragma: no cover - typing fallback for optional Airflow dependency
    try:
        from airflow.sdk.definitions.context import Context  # type: ignore
    except (ImportError, ModuleNotFoundError):
        Context = dict[str, Any]
    try:
        from airflow.sdk.exceptions import AirflowException
    except (ImportError, ModuleNotFoundError):
        try:
            from airflow.exceptions import AirflowException
        except (ImportError, ModuleNotFoundError):

            class _FallbackAirflowError(Exception):
                """Fallback exception used when ``AirflowException`` is unavailable."""

            AirflowException = _FallbackAirflowError

    try:
        from airflow.sdk.bases.hook import BaseHook
    except (ImportError, ModuleNotFoundError):
        try:
            from airflow.hooks.base import BaseHook
        except (ImportError, ModuleNotFoundError):

            class _FallbackBaseHook:
                """Fallback base hook used when Airflow is unavailable."""

                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    """Initialize fallback hook."""

                @classmethod
                def get_connection(cls, conn_id: str) -> Any:
                    """Stub for retrieving Airflow connections offline."""
                    raise AirflowException(
                        f"Cannot retrieve connection '{conn_id}': Apache Airflow is not installed."
                    )

            BaseHook = _FallbackBaseHook

__all__ = ["AirflowException", "BaseHook", "Context"]
