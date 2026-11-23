"""Deferrable Airflow triggers for asynchronous iMednet operations."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncIterator
from typing import Any, cast

from imednet.spi.sinks import redact_uri
from imednet.spi.utils import (
    AsyncJobPoller,
    redact_sensitive_payload,
    redact_sensitive_text,
    validate_path_segment,
)

from ._airflow_compat import AirflowException, BaseTrigger, TriggerEvent
from .hooks import ImednetHook


def _validate_trigger_identifier(
    value: str,
    *,
    field_name: str,
    path_segment: bool = False,
) -> str:
    """Validate a trigger identifier and reject blank, unsafe, or credential-bearing strings."""
    if not isinstance(value, str) or not value.strip():
        raise AirflowException(f"'{field_name}' must be a non-empty string.")
    if "://" in value and "://***@" not in value and redact_uri(value) != value:
        raise AirflowException(
            f"Plaintext URI credentials are not allowed in trigger parameter '{field_name}'."
        )
    existing_masks = value.count("***")
    if (
        redact_sensitive_text(value).count("***") > existing_masks
        or str(redact_sensitive_payload(value)).count("***") > existing_masks
    ):
        raise AirflowException(
            f"Sensitive credentials or tokens are not allowed in trigger parameter '{field_name}'."
        )
    if path_segment:
        validate_path_segment(value, name=field_name)
    return value


def _make_trigger_event(payload: dict[str, Any]) -> TriggerEvent:
    """Instantiate ``TriggerEvent`` cleanly under strict MyPy."""
    event_cls: Any = TriggerEvent
    return cast(TriggerEvent, event_cls(payload))


def _sanitize_job_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact pre-signed URLs and inline credentials from serialized job metadata."""
    sanitized = dict(payload)
    for field_name in ("result_url", "resultUrl", "result_error", "resultError"):
        raw_val = sanitized.get(field_name)
        if isinstance(raw_val, str) and raw_val:
            sanitized[field_name] = str(redact_sensitive_payload(redact_sensitive_text(raw_val)))
    return sanitized


class ImednetJobTrigger(BaseTrigger):
    """Asynchronously poll an iMednet batch job in the Airflow triggerer."""

    def __init__(
        self,
        study_key: str,
        batch_id: str,
        imednet_conn_id: str = "imednet_default",
        poke_interval: float = 60.0,
        timeout: float | None = None,
    ) -> None:
        """Initialize the asynchronous iMednet job trigger.

        :param study_key: Study key identifier.
        :param batch_id: Batch or job identifier to poll.
        :param imednet_conn_id: Airflow connection ID used to resolve credentials at runtime.
        :param poke_interval: Delay in seconds between polling attempts.
        :param timeout: Optional maximum polling duration in seconds.
        """
        cast(Any, super()).__init__()
        self.study_key = _validate_trigger_identifier(
            study_key, field_name="study_key", path_segment=True
        )
        self.batch_id = _validate_trigger_identifier(
            batch_id, field_name="batch_id", path_segment=True
        )
        self.imednet_conn_id = _validate_trigger_identifier(
            imednet_conn_id, field_name="imednet_conn_id"
        )
        if (
            isinstance(poke_interval, bool)
            or not isinstance(poke_interval, (int, float))
            or not math.isfinite(poke_interval)
            or poke_interval <= 0
        ):
            raise AirflowException("'poke_interval' must be a positive finite number.")
        self.poke_interval = float(poke_interval)

        if timeout is not None:
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise AirflowException("'timeout' must be a positive finite number when provided.")
            self.timeout: float | None = float(timeout)
        else:
            self.timeout = None

    def serialize(self) -> tuple[str, dict[str, Any]]:
        """Serialize trigger class path and non-secret parameters for Airflow's DB."""
        return (
            "apache_airflow_providers_imednet.triggers.ImednetJobTrigger",
            {
                "study_key": self.study_key,
                "batch_id": self.batch_id,
                "imednet_conn_id": self.imednet_conn_id,
                "poke_interval": self.poke_interval,
                "timeout": self.timeout,
            },
        )

    async def run(self) -> AsyncIterator[TriggerEvent]:
        """Poll the iMednet job asynchronously via ``AsyncImednetSDK`` and ``AsyncJobPoller``."""
        validate_path_segment(self.study_key, name="study_key")
        validate_path_segment(self.batch_id, name="batch_id")
        hook = ImednetHook(imednet_conn_id=self.imednet_conn_id)
        try:
            sdk = await hook.get_async_conn()
            async with sdk:
                jobs_ep = getattr(sdk, "jobs", None)
                raw_fetch_result = getattr(jobs_ep, "fetch_result", None)
                fetch_result = (
                    raw_fetch_result if inspect.iscoroutinefunction(raw_fetch_result) else None
                )
                poller = AsyncJobPoller(sdk.jobs.get, fetch_result=fetch_result)
                effective_timeout = float(self.timeout) if self.timeout is not None else math.inf
                status = await poller.run(
                    self.study_key,
                    self.batch_id,
                    interval=self.poke_interval,
                    timeout=effective_timeout,
                )
            job_payload: dict[str, Any] | None = None
            model_dump = getattr(status, "model_dump", None)
            if callable(model_dump):
                dumped = model_dump(mode="json")
                if isinstance(dumped, dict):
                    job_payload = _sanitize_job_payload(dumped)
            yield _make_trigger_event(
                {
                    "status": "success",
                    "study_key": self.study_key,
                    "batch_id": self.batch_id,
                    "state": getattr(status, "state", None),
                    "job": job_payload,
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            hook_message = await hook.redact_error_message(str(exc))
            safe_message = str(redact_sensitive_payload(redact_sensitive_text(hook_message)))
            yield _make_trigger_event(
                {
                    "status": "error",
                    "study_key": self.study_key,
                    "batch_id": self.batch_id,
                    "message": safe_message or exc.__class__.__name__,
                }
            )


__all__ = ["ImednetJobTrigger"]
