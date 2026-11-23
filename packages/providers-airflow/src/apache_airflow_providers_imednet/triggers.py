"""Deferrable Airflow triggers for asynchronous iMednet operations."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import AsyncIterator
from typing import Any, cast

from imednet.spi.utils import (
    AsyncJobPoller,
    JobTimeoutError,
    validate_path_segment,
)

from ._airflow_compat import AirflowException, BaseTrigger, TriggerEvent
from ._guards import (
    build_job_summary,
    scrub_job_error_message,
    validate_non_secret_identifier,
)
from .hooks import ImednetHook


def _validate_trigger_identifier(
    value: str,
    *,
    field_name: str,
    path_segment: bool = False,
) -> str:
    """Validate a trigger identifier and reject blank, unsafe, or credential-bearing strings."""
    return validate_non_secret_identifier(
        value,
        field_name=field_name,
        path_segment=path_segment,
        parameter_kind="trigger parameter",
    )


def _make_trigger_event(payload: dict[str, Any]) -> TriggerEvent:
    """Instantiate ``TriggerEvent`` cleanly under strict MyPy."""
    event_cls: Any = TriggerEvent
    return cast(TriggerEvent, event_cls(payload))


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
        """Initialize the asynchronous iMednet ``Job`` trigger.

        Args:
            study_key: The ``Study`` key identifier owning the asynchronous ``Job``.
            batch_id: The batch or ``Job`` identifier to poll until terminal completion.
            imednet_conn_id: Airflow connection ID used to resolve iMednet API credentials
                at trigger execution time.
            poke_interval: Delay in seconds between asynchronous ``Job`` polling attempts.
            timeout: Optional maximum polling duration in seconds before failing.
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
            job_payload = build_job_summary(
                status,
                study_key=self.study_key,
                batch_id=self.batch_id,
            )
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
        except JobTimeoutError as exc:
            safe_timeout = await hook.redact_error_message(scrub_job_error_message(exc))
            yield _make_trigger_event(
                {
                    "status": "timeout",
                    "study_key": self.study_key,
                    "batch_id": self.batch_id,
                    "message": safe_timeout or exc.__class__.__name__,
                }
            )
        except Exception as exc:
            safe_message = await hook.redact_error_message(scrub_job_error_message(exc))
            yield _make_trigger_event(
                {
                    "status": "error",
                    "study_key": self.study_key,
                    "batch_id": self.batch_id,
                    "message": safe_message or exc.__class__.__name__,
                }
            )


__all__ = ["ImednetJobTrigger"]
