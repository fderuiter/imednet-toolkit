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

            def resume_execution(
                self,
                next_method: str,
                next_kwargs: dict[str, Any] | None,
                context: Any,
            ) -> Any:
                """Resume deferred execution on fallback BaseOperator."""

    try:
        from airflow.sdk.bases.sensor import BaseSensorOperator, PokeReturnValue
    except ImportError:

        class PokeReturnValue:  # type: ignore[no-redef]
            """Fallback PokeReturnValue typing stub."""

            is_done: bool
            xcom_value: Any | None

            def __init__(self, is_done: bool, xcom_value: Any | None = None) -> None:
                """Initialize fallback PokeReturnValue."""
                self.is_done = is_done
                self.xcom_value = xcom_value

            def __bool__(self) -> bool:
                """Return sensor completion state."""
                return self.is_done

        class BaseSensorOperator(BaseOperator):  # type: ignore[no-redef]
            """Fallback BaseSensorOperator typing stub."""

            template_fields: Sequence[str] = ()
            soft_fail: bool = False
            never_fail: bool = False

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                """Initialize fallback BaseSensorOperator."""

    try:
        from airflow.sdk.exceptions import (
            AirflowException,
            AirflowNotFoundException,
            AirflowSecretsBackendAccessDenied,
            AirflowSensorTimeout,
            AirflowSkipException,
            TaskDeferralError,
            TaskDeferralTimeout,
            TaskDeferred,
        )
    except ImportError:

        class AirflowException(Exception):  # type: ignore[no-redef]  # noqa: N818
            """Fallback AirflowException typing stub."""

        class AirflowNotFoundException(AirflowException):  # type: ignore[no-redef]
            """Fallback AirflowNotFoundException typing stub."""

        class AirflowSecretsBackendAccessDenied(PermissionError):  # type: ignore[no-redef]  # noqa: N818
            """Fallback AirflowSecretsBackendAccessDenied typing stub."""

        class AirflowSensorTimeout(AirflowException):  # type: ignore[no-redef]
            """Fallback AirflowSensorTimeout typing stub."""

        class AirflowSkipException(AirflowException):  # type: ignore[no-redef]
            """Fallback AirflowSkipException typing stub."""

        class TaskDeferralError(AirflowException):  # type: ignore[no-redef]
            """Fallback TaskDeferralError typing stub."""

        class TaskDeferralTimeout(AirflowException):  # type: ignore[no-redef]
            """Fallback TaskDeferralTimeout typing stub."""

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
        from airflow.sdk.exceptions import AirflowNotFoundException
    except (ImportError, ModuleNotFoundError):

        class _FallbackAirflowNotFoundError(AirflowException):
            """Fallback exception used when ``AirflowNotFoundException`` is unavailable."""

        AirflowNotFoundException = _FallbackAirflowNotFoundError

    try:
        from airflow.sdk.exceptions import AirflowSecretsBackendAccessDenied
    except (ImportError, ModuleNotFoundError):

        class _FallbackSecretsBackendAccessDeniedError(PermissionError):
            """Fallback exception used when ``AirflowSecretsBackendAccessDenied`` is unavailable."""

        AirflowSecretsBackendAccessDenied = _FallbackSecretsBackendAccessDeniedError

    try:
        from airflow.sdk.exceptions import AirflowSensorTimeout
    except (ImportError, ModuleNotFoundError):

        class _FallbackAirflowSensorTimeoutError(AirflowException):
            """Fallback exception used when ``AirflowSensorTimeout`` is unavailable."""

        AirflowSensorTimeout = _FallbackAirflowSensorTimeoutError

    try:
        from airflow.sdk.exceptions import AirflowSkipException
    except (ImportError, ModuleNotFoundError):

        class _FallbackAirflowSkipError(AirflowException):
            """Fallback exception used when ``AirflowSkipException`` is unavailable."""

        AirflowSkipException = _FallbackAirflowSkipError

    try:
        from airflow.sdk.exceptions import TaskDeferralError
    except (ImportError, ModuleNotFoundError):

        class _FallbackTaskDeferralError(AirflowException):
            """Fallback exception used when ``TaskDeferralError`` is unavailable."""

        TaskDeferralError = _FallbackTaskDeferralError

    try:
        from airflow.sdk.exceptions import TaskDeferralTimeout
    except (ImportError, ModuleNotFoundError):

        class _FallbackTaskDeferralTimeoutError(AirflowException):
            """Fallback exception used when ``TaskDeferralTimeout`` is unavailable."""

        TaskDeferralTimeout = _FallbackTaskDeferralTimeoutError

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

            def resume_execution(
                self,
                next_method: str,
                next_kwargs: dict[str, Any] | None,
                context: Any,
            ) -> Any:
                """Execute deferred callback or raise trigger failure offline."""
                resolved_kwargs = next_kwargs or {}
                if next_method == "__fail__":
                    error = resolved_kwargs.get("error", "Unknown")
                    if error == "Trigger timeout":
                        raise TaskDeferralTimeout(error)
                    raise TaskDeferralError(error)
                execute_callable = getattr(self, next_method)
                return execute_callable(context, **resolved_kwargs)

        BaseOperator = _FallbackBaseOperator

    try:
        from airflow.sdk.bases.sensor import BaseSensorOperator
    except (ImportError, ModuleNotFoundError):

        class _FallbackBaseSensorOperator(BaseOperator):
            """Fallback base sensor operator used when Airflow is unavailable."""

            template_fields: Sequence[str] = ()

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                """Initialize fallback base sensor operator."""
                super().__init__(*args, **kwargs)
                self.soft_fail = bool(kwargs.get("soft_fail", False))
                self.never_fail = bool(kwargs.get("never_fail", False))

        BaseSensorOperator = _FallbackBaseSensorOperator

    try:
        from airflow.sdk.bases.sensor import PokeReturnValue
    except (ImportError, ModuleNotFoundError):

        class _FallbackPokeReturnValue:
            """Fallback ``PokeReturnValue`` used when Airflow is unavailable."""

            __hash__ = None

            def __init__(self, is_done: bool, xcom_value: Any | None = None) -> None:
                """Initialize fallback poke return value."""
                self.is_done = is_done
                self.xcom_value = xcom_value

            def __bool__(self) -> bool:
                """Return True when the sensor has completed poking."""
                return self.is_done

            def __eq__(self, other: object) -> bool:
                """Compare fallback poke return values by state and XCom payload."""
                return (
                    isinstance(other, _FallbackPokeReturnValue)
                    and self.is_done == other.is_done
                    and self.xcom_value == other.xcom_value
                )

        PokeReturnValue = _FallbackPokeReturnValue

__all__ = [
    "AirflowException",
    "AirflowNotFoundException",
    "AirflowSecretsBackendAccessDenied",
    "AirflowSensorTimeout",
    "AirflowSkipException",
    "BaseHook",
    "BaseOperator",
    "BaseSensorOperator",
    "BaseTrigger",
    "Context",
    "PokeReturnValue",
    "TaskDeferralError",
    "TaskDeferralTimeout",
    "TaskDeferred",
    "TriggerEvent",
]
