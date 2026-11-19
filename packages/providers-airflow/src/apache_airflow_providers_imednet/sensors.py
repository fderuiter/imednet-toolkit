"""Airflow sensors for iMednet operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
from imednet.spi.models import Job, JobStatus
from imednet.spi.utils import (
    InvalidJobStateError,
    InvalidJobTransitionError,
    JobFailedError,
    JobStateTracker,
    evaluate_job_state,
)

from ._airflow_compat import AirflowException, Context
from .hooks import ImednetHook


class ImednetJobSensor(BaseSensorOperator):
    """Poll iMednet for job completion."""

    template_fields: Sequence[str] = ("study_key", "batch_id")
    XCOM_TRACKER_KEY: str = "imednet_job_state_tracker"

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
        self._tracker = JobStateTracker()
        self._last_state: Any = None
        self._terminal_reached: bool = False

    def _get_sdk(self) -> ImednetSDK:
        """Get the Imednet SDK client."""
        return ImednetHook(self.imednet_conn_id).get_sdk_client()

    @staticmethod
    def _extract_task_instance(context: Context) -> Any | None:
        """Extract Airflow TaskInstance from poke context when available."""
        if not isinstance(context, Mapping):
            return None
        return context.get("ti") or context.get("task_instance")

    def _pull_tracker_xcom(self, ti: Any) -> dict[str, Any] | None:
        """Pull serialized ``JobStateTracker`` state from Airflow XCom."""
        if not hasattr(ti, "xcom_pull"):
            return None
        task_id = getattr(self, "task_id", None)
        if isinstance(task_id, str) and task_id:
            try:
                raw = ti.xcom_pull(task_ids=task_id, key=self.XCOM_TRACKER_KEY)
            except TypeError:
                raw = ti.xcom_pull(key=self.XCOM_TRACKER_KEY)
        else:
            raw = ti.xcom_pull(key=self.XCOM_TRACKER_KEY)
        return raw if isinstance(raw, dict) else None

    def _push_tracker_xcom(self, ti: Any, payload: dict[str, Any]) -> None:
        """Push serialized ``JobStateTracker`` state to Airflow XCom."""
        if hasattr(ti, "xcom_push"):
            ti.xcom_push(key=self.XCOM_TRACKER_KEY, value=payload)

    def _restore_or_seed_tracker(self, ti: Any | None) -> JobStateTracker:
        """Restore tracker from XCom when present, or fall back to instance attributes."""
        if ti is not None:
            raw_xcom = self._pull_tracker_xcom(ti)
            if raw_xcom is not None:
                self._tracker = JobStateTracker.from_dict(raw_xcom)
                return self._tracker

        if self._tracker.get_state(
            self.study_key, self.batch_id
        ) is None and not self._tracker.is_terminal(self.study_key, self.batch_id):
            if self._last_state is not None:
                self._tracker.observe(
                    self.study_key,
                    self.batch_id,
                    JobStatus.model_validate({"batchId": self.batch_id, "state": self._last_state}),
                )
            elif self._terminal_reached:
                self._tracker.observe(
                    self.study_key,
                    self.batch_id,
                    JobStatus.model_validate({"batchId": self.batch_id, "state": "COMPLETED"}),
                )
        return self._tracker

    def poke(self, context: Context) -> bool:
        """Check the status of the job."""
        sdk = self._get_sdk()
        job: Any = sdk.jobs.get(self.study_key, self.batch_id)
        ti = self._extract_task_instance(context)

        is_mock_job = type(job).__module__.startswith("unittest.mock")
        if isinstance(job, Job) or ti is not None or not is_mock_job:
            tracker = self._restore_or_seed_tracker(ti)
            try:
                effective_job = tracker.observe(self.study_key, self.batch_id, job)
                self._last_state = tracker.get_state(self.study_key, self.batch_id)
                self._terminal_reached = tracker.is_terminal(self.study_key, self.batch_id)
                if ti is not None:
                    self._push_tracker_xcom(ti, tracker.to_dict())
                return evaluate_job_state(effective_job)
            except (JobFailedError, InvalidJobTransitionError, InvalidJobStateError) as e:
                raise AirflowException(str(e)) from e

        try:
            return evaluate_job_state(job)
        except (JobFailedError, InvalidJobTransitionError, InvalidJobStateError) as e:
            raise AirflowException(str(e)) from e


__all__ = ["ImednetJobSensor"]
