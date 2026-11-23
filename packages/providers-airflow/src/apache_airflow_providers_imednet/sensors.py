"""Airflow sensors for iMednet operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import timedelta
from typing import Any

from imednet import ImednetSDK

from ._airflow_compat import (
    AirflowException,
    AirflowSensorTimeout,
    AirflowSkipException,
    BaseOperator,
    BaseSensorOperator,
    Context,
    PokeReturnValue,
    TaskDeferralError,
    TaskDeferralTimeout,
    TaskDeferred,
)
from ._guards import (
    build_job_summary,
    sanitize_job_summary,
    scrub_job_error_message,
    validate_non_secret_identifier,
)
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

    template_fields: Sequence[str] = ("study_key", "batch_id", "imednet_conn_id")

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
        self._terminal_summary: dict[str, Any] | None = None

    def _validate_rendered_identifiers(self) -> None:
        """Validate rendered ``study_key``, ``batch_id``, and ``imednet_conn_id`` before polling."""
        validate_non_secret_identifier(
            self.study_key,
            field_name="study_key",
            path_segment=True,
            parameter_kind="sensor parameter",
        )
        validate_non_secret_identifier(
            self.batch_id,
            field_name="batch_id",
            path_segment=True,
            parameter_kind="sensor parameter",
        )
        validate_non_secret_identifier(
            self.imednet_conn_id,
            field_name="imednet_conn_id",
            parameter_kind="sensor parameter",
        )

    def _get_sdk(self) -> ImednetSDK:
        """Get the Imednet SDK client."""
        return ImednetHook(self.imednet_conn_id).get_sdk_client()

    def poke(self, context: Context) -> bool | PokeReturnValue:
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

        self._validate_rendered_identifiers()

        with closing(self._get_sdk()) as sdk:
            job: JobStatus = sdk.jobs.get(self.study_key, self.batch_id)

            # In JobStatus instances, track state monotonically and guard against stale responses
            if isinstance(job, JobStatus):
                if self._terminal_reached and not getattr(job, "is_terminal", False):
                    return PokeReturnValue(is_done=True, xcom_value=self._terminal_summary)

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
                        self._terminal_summary = build_job_summary(
                            job,
                            study_key=self.study_key,
                            batch_id=self.batch_id,
                        )
                        return PokeReturnValue(is_done=True, xcom_value=self._terminal_summary)
                    return False
                except (JobFailedError, InvalidJobTransitionError, InvalidJobStateError) as e:
                    self._last_state = getattr(job, "state", None)
                    raise AirflowException(scrub_job_error_message(e)) from e

            try:
                if getattr(job, "is_terminal", False):
                    fetch_terminal_job_result(
                        job,
                        resolve_sdk_result_fetcher(sdk),
                        batch_id=self.batch_id,
                    )
                if evaluate_job_state(job):
                    summary = build_job_summary(
                        job,
                        study_key=self.study_key,
                        batch_id=self.batch_id,
                    )
                    return PokeReturnValue(is_done=True, xcom_value=summary)
                return False
            except (JobFailedError, InvalidJobTransitionError, InvalidJobStateError) as e:
                raise AirflowException(scrub_job_error_message(e)) from e

    def execute(self, context: Context) -> Any:
        """Execute sensor synchronously or defer polling to ``ImednetJobTrigger``."""
        self._validate_rendered_identifiers()
        if not self.deferrable:
            super_execute = getattr(super(), "execute", None)
            if callable(super_execute):
                return super_execute(context)
            poke_result = self.poke(context)
            if isinstance(poke_result, PokeReturnValue):
                return poke_result.xcom_value
            return poke_result

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

    def resume_execution(
        self,
        next_method: str,
        next_kwargs: dict[str, Any] | None,
        context: Context,
    ) -> Any:
        """Resume deferred sensor execution while restricting ``soft_fail`` to timeouts."""
        try:
            try:
                return BaseOperator.resume_execution(self, next_method, next_kwargs, context)
            except TaskDeferralTimeout as exc:
                raise AirflowSensorTimeout(*exc.args) from exc
        except AirflowSkipException:
            raise
        except AirflowSensorTimeout as exc:
            if getattr(self, "soft_fail", False):
                raise AirflowSkipException("Skipping due to soft_fail is set to True.") from exc
            if getattr(self, "never_fail", False):
                raise AirflowSkipException("Skipping due to never_fail is set to True.") from exc
            raise
        except (AirflowException, TaskDeferralError) as exc:
            if getattr(self, "never_fail", False):
                raise AirflowSkipException("Skipping due to never_fail is set to True.") from exc
            raise

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
        if status == "timeout":
            raw_timeout = str(event.get("message") or "Job timed out during deferred polling.")
            raise AirflowSensorTimeout(scrub_job_error_message(raw_timeout))
        if status == "error":
            raw_message = str(event.get("message") or "Job failed during deferred polling.")
            raise AirflowException(scrub_job_error_message(raw_message))
        if status != "success":
            raise AirflowException(
                f"Unexpected trigger event status '{status}' for job '{self.batch_id}'."
            )
        return sanitize_job_summary(event.get("job"))


__all__ = ["ImednetJobSensor"]
