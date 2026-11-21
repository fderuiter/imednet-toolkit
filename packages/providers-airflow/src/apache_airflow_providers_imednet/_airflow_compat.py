"""Compatibility shim for Airflow 3 Task SDK and offline test environments.

This module provides fallbacks for Airflow-specific classes and exceptions
to allow the package to be imported even when Airflow is not installed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    try:
        from airflow.sdk.bases.hook import BaseHook
    except ImportError:

        class BaseHook:  # type: ignore[no-redef]
            """Fallback BaseHook typing stub."""

    try:
        from airflow.sdk.bases.operator import BaseOperator
    except ImportError:

        class BaseOperator:  # type: ignore[no-redef]
            """Fallback BaseOperator typing stub."""

            template_fields: Sequence[str] = ()

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                """Initialize fallback BaseOperator."""

    try:
        from airflow.sdk.bases.sensor import BaseSensorOperator
    except ImportError:

        class BaseSensorOperator:  # type: ignore[no-redef]
            """Fallback BaseSensorOperator typing stub."""

            template_fields: Sequence[str] = ()

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                """Initialize fallback BaseSensorOperator."""

    try:
        from airflow.sdk.exceptions import AirflowException
    except ImportError:

        class AirflowException(Exception):  # type: ignore[no-redef]  # noqa: N818
            """Fallback AirflowException typing stub."""

    try:
        from airflow.sdk.exceptions import TaskDeferred
    except ImportError:

        class TaskDeferred(Exception):  # type: ignore[no-redef]  # noqa: N818
            """Fallback TaskDeferred typing stub."""

            def __init__(
                self,
                *,
                trigger: Any,
                method_name: str,
                kwargs: dict[str, Any] | None = None,
                timeout: Any = None,
            ) -> None:
                """Initialize fallback deferred task signal."""
                _ = (trigger, method_name, kwargs, timeout)

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

        class _FallbackAirflowError(Exception):
            """Fallback exception used when ``AirflowException`` is unavailable."""

        AirflowException = _FallbackAirflowError

    try:
        from airflow.sdk.exceptions import TaskDeferred
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

    try:
        from airflow.sdk.bases.operator import BaseOperator
    except (ImportError, ModuleNotFoundError):

        class _FallbackBaseOperator:
            """Fallback base operator used when Airflow is unavailable."""

            template_fields: Sequence[str] = ()

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                """Initialize fallback base operator."""

        BaseOperator = _FallbackBaseOperator

    try:
        from airflow.sdk.bases.sensor import BaseSensorOperator
    except (ImportError, ModuleNotFoundError):

        class _FallbackBaseSensorOperator:
            """Fallback base sensor operator used when Airflow is unavailable."""

            template_fields: Sequence[str] = ()

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                """Initialize fallback base sensor operator."""

        BaseSensorOperator = _FallbackBaseSensorOperator

__all__ = [
    "AirflowException",
    "BaseHook",
    "BaseOperator",
    "BaseSensorOperator",
    "BaseTrigger",
    "Context",
    "TaskDeferred",
    "TriggerEvent",
]
