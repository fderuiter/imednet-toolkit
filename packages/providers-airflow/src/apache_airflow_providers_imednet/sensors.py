"""Airflow sensors for iMednet operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import timedelta
from typing import Any

try:  # pragma: no cover - optional Airflow dependency
    try:
        from airflow.sdk.bases.sensor import BaseSensorOperator
    except ImportError:
        from airflow.sensors.base import BaseSensorOperator  # type: ignore[no-redef]
except (ImportError, ModuleNotFoundError):  # pragma: no cover - placeholder fallback

    class BaseSensorOperator:  # type: ignore
        """Fallback BaseSensorOperator."""

        template_fields: Sequence[str] = ()

        def __init__(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
            """Initialize fallback BaseSensorOperator."""


from imednet import ImednetSDK
from imednet.spi.utils import (
    redact_sensitive_payload,
    redact_sensitive_text,
    validate_path_segment,
)

from ._airflow_compat import AirflowException, Context, TaskDeferred
from .hooks import ImednetHook
from .triggers import ImednetJobTrigger


def _default_deferrable() -> bool:
    """Resolve ``[operators] default_deferrable`` when Airflow config is available."""
    try:
        from airflow.configuration import conf

        return bool(conf.getboolean("operators", "default_deferrable", fallback=False))
    except Exception:
        return False


class ImednetJobSensor(BaseSensorOperator):
    """Poll iMednet for job completion synchronously or via the Airflow triggerer."""

    template_fields: Sequence[str] = ("study_key", "batch_id")

    def __init__(
        self,
        *,
        study_key: str,
        batch_id: str,
        imednet_conn_id: str = "imednet_default",
        poke_interval: float = 60,
        deferrable: bool | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the job sensor.

        :param study_key: The study key identifier.
        :param batch_id: The batch or job identifier to monitor.
        :param imednet_conn_id: Airflow connection ID to use for credentials.
        :param poke_interval: Seconds between polling attempts.
        :param deferrable: Run asynchronously in the Airflow triggerer instead of occupying a worker slot.
        :param kwargs: Additional Airflow BaseSensorOperator arguments.
        """
        super().__init__(poke_interval=poke_interval, **kwargs)
        if not hasattr(self, "poke_interval"):
            self.poke_interval = poke_interval
        if "timeout" in kwargs and not hasattr(self, "timeout"):
            self.timeout = kwargs["timeout"]
        self.study_key = study_key
        self.batch_id = batch_id
        self.imednet_conn_id = imednet_conn_id
        self.deferrable: bool = _default_deferrable() if deferrable is None else bool(deferrable)
        self._last_state: Any = None
        self._terminal_reached: bool = False

    def _get_sdk(self) -> ImednetSDK:
        """Get the Imednet SDK client."""
        return ImednetHook(self.imednet_conn_id).get_sdk_client()

    def poke(self, context: Context) -> bool:
        """Check the status of the Job and verify terminal ``resultUrl`` output."""
        _ = context
        from imednet.spi.models import JobStatus
        from imednet.spi.utils import (
            InvalidJobStateError,
            InvalidJobTransitionError,
            JobFailedError,
            evaluate_job_state,
            fetch_terminal_job_result,
            resolve_sdk_result_fetcher,
        )

        validate_path_segment(self.study_key, name="study_key")
        validate_path_segment(self.batch_id, name="batch_id")

        with closing(self._get_sdk()) as sdk:
            job: JobStatus = sdk.jobs.get(self.study_key, self.batch_id)

            # In JobStatus instances, track state monotonically and guard against stale responses
            if isinstance(job, JobStatus):
                if self._terminal_reached and not getattr(job, "is_terminal", False):
                    return True

                try:
                    if getattr(job, "is_terminal", False):
                        fetch_terminal_job_result(
                            job,
                            resolve_sdk_result_fetcher(sdk),
                            batch_id=self.batch_id,
                        )
                    is_done = evaluate_job_state(job, previous_state=self._last_state)
                    self._last_state = getattr(job, "state", None)
                    if is_done:
                        self._terminal_reached = True
                    return is_done
                except (JobFailedError, InvalidJobTransitionError, InvalidJobStateError) as e:
                    self._last_state = getattr(job, "state", None)
                    raise AirflowException(str(e)) from e

            try:
                if getattr(job, "is_terminal", False):
                    fetch_terminal_job_result(
                        job,
                        resolve_sdk_result_fetcher(sdk),
                        batch_id=self.batch_id,
                    )
                return evaluate_job_state(job)
            except (JobFailedError, InvalidJobTransitionError, InvalidJobStateError) as e:
                raise AirflowException(str(e)) from e

    def execute(self, context: Context) -> Any:
        """Execute sensor synchronously or defer polling to ``ImednetJobTrigger``."""
        if not self.deferrable:
            super_execute = getattr(super(), "execute", None)
            if callable(super_execute):
                return super_execute(context)
            return self.poke(context)

        validate_path_segment(self.study_key, name="study_key")
        validate_path_segment(self.batch_id, name="batch_id")

        raw_timeout = getattr(self, "timeout", None)
        trigger_timeout: float | None = None
        defer_timeout: timedelta | None = None
        if isinstance(raw_timeout, timedelta):
            trigger_timeout = raw_timeout.total_seconds()
            defer_timeout = raw_timeout
        elif isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool):
            trigger_timeout = float(raw_timeout)
            defer_timeout = timedelta(seconds=float(raw_timeout))

        trigger = ImednetJobTrigger(
            study_key=self.study_key,
            batch_id=self.batch_id,
            imednet_conn_id=self.imednet_conn_id,
            poke_interval=float(self.poke_interval),
            timeout=trigger_timeout,
        )
        defer_fn = getattr(self, "defer", None)
        if callable(defer_fn):
            defer_fn(
                trigger=trigger,
                method_name="execute_complete",
                timeout=defer_timeout,
            )
            return None
        raise TaskDeferred(
            trigger=trigger,
            method_name="execute_complete",
            timeout=defer_timeout,
        )

    def execute_complete(
        self,
        context: Context,
        event: Mapping[str, Any] | None = None,
    ) -> Any:
        """Resume sensor execution once ``ImednetJobTrigger`` emits a terminal event."""
        _ = context
        if not isinstance(event, Mapping):
            raise AirflowException(
                f"Did not receive a valid trigger event for job '{self.batch_id}' in study '{self.study_key}'."
            )
        status = event.get("status")
        if status == "error":
            raw_message = str(event.get("message") or "Job failed during deferred polling.")
            safe_message = str(redact_sensitive_payload(redact_sensitive_text(raw_message)))
            raise AirflowException(safe_message)
        if status != "success":
            raise AirflowException(
                f"Unexpected trigger event status '{status}' for job '{self.batch_id}'."
            )
        return event.get("job")


__all__ = ["ImednetJobSensor"]
