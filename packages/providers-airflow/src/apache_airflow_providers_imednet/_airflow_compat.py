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
            from airflow.hooks.base import BaseHook  # type: ignore[attr-defined,no-redef]
        except ImportError:

            class BaseHook:  # type: ignore[no-redef]
                """Fallback BaseHook typing stub."""

    try:
        from airflow.sdk.exceptions import AirflowException
    except ImportError:
        from airflow.exceptions import AirflowException  # type: ignore[attr-defined]
    try:
        from airflow.sdk.exceptions import TaskDeferred
    except ImportError:
        from airflow.exceptions import TaskDeferred  # type: ignore[attr-defined,no-redef]
    try:
        from airflow.triggers.base import BaseTrigger, TriggerEvent
    except ImportError:

        class BaseTrigger:  # type: ignore[no-redef]
            """Fallback BaseTrigger typing stub."""

        class TriggerEvent:  # type: ignore[no-redef]
            """Fallback TriggerEvent typing stub."""

            def __init__(self, payload: Any) -> None:
                """Initialize fallback trigger event."""
                self.payload = payload

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
        from airflow.sdk.exceptions import TaskDeferred
    except (ImportError, ModuleNotFoundError):
        try:
            from airflow.exceptions import TaskDeferred
        except (ImportError, ModuleNotFoundError):

            class _FallbackTaskDeferredError(Exception):
                """Fallback exception used when ``TaskDeferred`` is unavailable."""

                def __init__(
                    self,
                    *,
                    trigger: Any,
                    method_name: str,
                    kwargs: dict[str, Any] | None = None,
                    timeout: Any = None,
                ) -> None:
                    """Initialize fallback deferred task signal."""
                    super().__init__(f"Task deferred to {method_name}")
                    self.trigger = trigger
                    self.method_name = method_name
                    self.kwargs = kwargs or {}
                    self.timeout = timeout

            TaskDeferred = _FallbackTaskDeferredError

    try:
        from airflow.triggers.base import BaseTrigger, TriggerEvent
    except (ImportError, ModuleNotFoundError):

        class _FallbackTriggerEvent:
            """Fallback trigger event used when Airflow is unavailable."""

            __hash__ = None

            def __init__(self, payload: Any) -> None:
                """Initialize fallback trigger event."""
                self.payload = payload

            def __eq__(self, other: object) -> bool:
                """Compare trigger events by payload."""
                return isinstance(other, _FallbackTriggerEvent) and self.payload == other.payload

        class _FallbackBaseTrigger:
            """Fallback base trigger used when Airflow is unavailable."""

            def __init__(self, **kwargs: Any) -> None:
                """Initialize fallback base trigger."""
                _ = kwargs

            def serialize(self) -> tuple[str, dict[str, Any]]:
                """Serialize trigger class path and keyword arguments."""
                raise NotImplementedError

        BaseTrigger = _FallbackBaseTrigger
        TriggerEvent = _FallbackTriggerEvent

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

__all__ = [
    "AirflowException",
    "BaseHook",
    "BaseTrigger",
    "Context",
    "TaskDeferred",
    "TriggerEvent",
]
