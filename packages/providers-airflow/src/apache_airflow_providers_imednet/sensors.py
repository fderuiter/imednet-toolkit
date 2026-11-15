"""Airflow sensors for iMednet operations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

try:  # pragma: no cover - optional Airflow dependency
    try:
        from airflow.sdk.bases.sensor import BaseSensorOperator
    except ImportError:
        from airflow.sensors.base import BaseSensorOperator
except (ImportError, ModuleNotFoundError):  # pragma: no cover - placeholder fallback

    class BaseSensorOperator:  # type: ignore
        """Fallback BaseSensorOperator."""

        template_fields: Sequence[str] = ()

        def __init__(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
            """Initialize fallback BaseSensorOperator."""


from imednet import ImednetSDK

from ._airflow_compat import AirflowException, Context
from .hooks import ImednetHook


class ImednetJobSensor(BaseSensorOperator):
    """Poll iMednet for job completion."""

    template_fields: Sequence[str] = ("study_key", "batch_id")

    def __init__(
        self,
        *,
        study_key: str,
        batch_id: str,
        imednet_conn_id: str = "imednet_default",
        poke_interval: float = 60,
        **kwargs: Any,
    ) -> None:
        """Initialize the job sensor.

        :param study_key: The study key identifier.
        :param batch_id: The batch or job identifier to monitor.
        :param imednet_conn_id: Airflow connection ID to use for credentials.
        :param poke_interval: Seconds between polling attempts.
        :param kwargs: Additional Airflow BaseSensorOperator arguments.
        """
        super().__init__(poke_interval=poke_interval, **kwargs)
        self.study_key = study_key
        self.batch_id = batch_id
        self.imednet_conn_id = imednet_conn_id
        self._last_state: Any = None
        self._terminal_reached: bool = False

    def _get_sdk(self) -> ImednetSDK:
        """Get the Imednet SDK client."""
        return ImednetHook(self.imednet_conn_id).get_sdk_client()

    def poke(self, context: Context) -> bool:
        """Check the status of the job."""
        from imednet.spi.models import JobStatus
        from imednet.spi.utils import (
            InvalidJobStateError,
            InvalidJobTransitionError,
            JobFailedError,
            evaluate_job_state,
        )

        sdk = self._get_sdk()
        job: JobStatus = sdk.jobs.get(self.study_key, self.batch_id)

        # In JobStatus instances, track state monotonically and guard against stale responses
        if isinstance(job, JobStatus):
            if self._terminal_reached and not getattr(job, "is_terminal", False):
                return True

            try:
                is_done = evaluate_job_state(job, previous_state=self._last_state)
                self._last_state = getattr(job, "state", None)
                if is_done:
                    self._terminal_reached = True
                return is_done
            except (JobFailedError, InvalidJobTransitionError, InvalidJobStateError) as e:
                self._last_state = getattr(job, "state", None)
                raise AirflowException(str(e)) from e

        try:
            return evaluate_job_state(job)
        except (JobFailedError, InvalidJobTransitionError, InvalidJobStateError) as e:
            raise AirflowException(str(e)) from e


__all__ = ["ImednetJobSensor"]
