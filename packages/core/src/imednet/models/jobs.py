# pylint: disable=duplicate-code
"""Job and background operation models for iMedNet."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, field_validator, model_validator

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


class JobState(str, Enum):
    """Enumeration of valid job states."""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    OPEN = "OPEN"
    RUNNING = "RUNNING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    COMPLETE = "COMPLETE"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"

    def can_transition_to(self, target: JobState | str) -> bool:
        """Check if this state can transition to target state."""
        return is_valid_job_transition(self, target)

    def validate_transition_to(self, target: JobState | str) -> bool:
        """Validate transition from this state to target state."""
        return validate_job_transition(self, target)

    def __str__(self) -> str:
        """Return the string value of the state."""
        return str(self.value)

    @property
    def allowed_transitions(self) -> set[JobState]:
        """Return the set of allowed target states from this state."""
        return ALLOWED_JOB_TRANSITIONS.get(self, set())


TERMINAL_JOB_STATES: set[JobState] = {
    JobState.COMPLETED,
    JobState.COMPLETE,
    JobState.SUCCESS,
    JobState.FAILED,
    JobState.CANCELLED,
    JobState.TIMEOUT,
}

ACTIVE_JOB_STATES: set[JobState] = {
    JobState.PENDING,
    JobState.QUEUED,
    JobState.OPEN,
    JobState.RUNNING,
    JobState.PROCESSING,
}

SUCCESSFUL_JOB_STATES: set[JobState] = {
    JobState.COMPLETED,
    JobState.COMPLETE,
    JobState.SUCCESS,
}

FAILED_JOB_STATES: set[JobState] = {
    JobState.FAILED,
    JobState.CANCELLED,
    JobState.TIMEOUT,
}

ALLOWED_JOB_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.PENDING: {
        JobState.PENDING,
        JobState.QUEUED,
        JobState.OPEN,
        JobState.RUNNING,
        JobState.PROCESSING,
        JobState.COMPLETED,
        JobState.COMPLETE,
        JobState.SUCCESS,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.TIMEOUT,
    },
    JobState.QUEUED: {
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.PROCESSING,
        JobState.COMPLETED,
        JobState.COMPLETE,
        JobState.SUCCESS,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.TIMEOUT,
    },
    JobState.OPEN: {
        JobState.OPEN,
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.PROCESSING,
        JobState.COMPLETED,
        JobState.COMPLETE,
        JobState.SUCCESS,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.TIMEOUT,
    },
    JobState.RUNNING: {
        JobState.RUNNING,
        JobState.PROCESSING,
        JobState.COMPLETED,
        JobState.COMPLETE,
        JobState.SUCCESS,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.TIMEOUT,
    },
    JobState.PROCESSING: {
        JobState.PROCESSING,
        JobState.RUNNING,
        JobState.COMPLETED,
        JobState.COMPLETE,
        JobState.SUCCESS,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.TIMEOUT,
    },
    JobState.COMPLETED: {JobState.COMPLETED, JobState.COMPLETE, JobState.SUCCESS},
    JobState.COMPLETE: {JobState.COMPLETED, JobState.COMPLETE, JobState.SUCCESS},
    JobState.SUCCESS: {JobState.SUCCESS, JobState.COMPLETED, JobState.COMPLETE},
    JobState.FAILED: {JobState.FAILED},
    JobState.CANCELLED: {JobState.CANCELLED},
    JobState.TIMEOUT: {JobState.TIMEOUT},
}


class InvalidJobStateError(ValueError):
    """Raised when an unrecognized job state string is encountered."""


class InvalidJobTransitionError(ValueError):
    """Raised when an invalid state transition is attempted."""


InvalidStateTransitionError = InvalidJobTransitionError


def normalize_job_state(state: JobState | str) -> JobState:
    """Normalize and validate a job state string into a JobState enum member.

    Args:
        state: The job state to normalize (JobState enum member or string).

    Returns:
        The normalized JobState enum member.

    Raises:
        InvalidJobStateError: If the state string is unrecognized.
    """
    if isinstance(state, JobState):
        return state
    if isinstance(state, str):
        cleaned = state.strip().upper()
        try:
            return JobState(cleaned)
        except ValueError as err:
            allowed = sorted(s.value for s in JobState)
            raise InvalidJobStateError(
                f"Invalid job state: '{state}'. Must be one of: {allowed}"
            ) from err
    raise InvalidJobStateError(f"Invalid job state type: {type(state).__name__}")


def is_valid_job_transition(
    from_state: JobState | str,
    to_state: JobState | str,
) -> bool:
    """Check if transitioning from from_state to to_state is permitted.

    Args:
        from_state: The current or originating job state.
        to_state: The desired or target job state.

    Returns:
        True if the transition is allowed, False otherwise.
    """
    try:
        from_enum = normalize_job_state(from_state)
        to_enum = normalize_job_state(to_state)
    except (InvalidJobStateError, ValueError):
        return False
    return to_enum in ALLOWED_JOB_TRANSITIONS.get(from_enum, set())


def validate_job_transition(
    from_state: JobState | str,
    to_state: JobState | str,
) -> bool:
    """Validate that transitioning from from_state to to_state is permitted.

    Args:
        from_state: The current or originating job state.
        to_state: The desired or target job state.

    Returns:
        True if the transition is valid.

    Raises:
        InvalidJobStateError: If either state is not a recognized JobState.
        InvalidJobTransitionError: If the transition moves backwards or is not permitted.
    """
    from_enum = normalize_job_state(from_state)
    to_enum = normalize_job_state(to_state)

    if from_enum in TERMINAL_JOB_STATES and to_enum in ACTIVE_JOB_STATES:
        raise InvalidJobTransitionError(
            f"Cannot transition terminal job state {from_enum.value} back to active state {to_enum.value}."
        )

    allowed = ALLOWED_JOB_TRANSITIONS.get(from_enum, set())
    if to_enum not in allowed:
        allowed_names = sorted(s.value for s in allowed)
        raise InvalidJobTransitionError(
            f"Invalid job state transition from {from_enum.value} to {to_enum.value}. "
            f"Allowed transitions from {from_enum.value}: {allowed_names}"
        )
    return True


class Job(ImednetBaseModel):
    """Represents an asynchronous background job."""

    state: JobState | str | None = Field(default=None, alias="state")

    @field_validator("state", mode="before")
    @classmethod
    def _validate_state(cls, v: Any) -> JobState | None:
        """Validate and normalize state to JobState enum."""
        if v is None:
            return None
        return normalize_job_state(v)

    def transition_to(self, target: JobState | str) -> Job:
        """Transition job to a new state along an approved lifecycle path.

        Args:
            target: Desired target state.

        Returns:
            The job instance updated with the target state.
        """
        target_state = normalize_job_state(target)
        if self.state is not None:
            validate_job_transition(self.state, target_state)
        self.state = target_state
        return self

    @property
    def is_terminal(self) -> bool:
        """Checks if the job has reached a final state (Success/Failed/Cancelled)."""
        return self.state in TERMINAL_JOB_STATES if self.state else False

    @property
    def is_successful(self) -> bool:
        """Checks if the job completed successfully."""
        return self.state in SUCCESSFUL_JOB_STATES if self.state else False

    @property
    def is_failed(self) -> bool:
        """Checks if the job failed or was cancelled."""
        return self.state in FAILED_JOB_STATES if self.state else False


Job = ModelEngine.get_model('Job', Job)


class JobStatus(Job):
    """Extended job information returned when polling."""

    results: Any = Field(default=None)
    record_total_count: int | None = Field(default=None, alias="totalRecords")
    record_success_count: int | None = Field(default=None, alias="successfulRecords")
    record_failure_count: int | None = Field(default=None, alias="failedRecords")
    result_error: str | None = Field(default=None, alias="resultError")

    @model_validator(mode="before")
    @classmethod
    def _normalize_record_count_keys(cls, data: Any) -> Any:
        """Accept snake_case record count kwargs without colliding with properties."""
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if "total_records" in normalized and "totalRecords" not in normalized:
            normalized["totalRecords"] = normalized.pop("total_records")
        if "successful_records" in normalized and "successfulRecords" not in normalized:
            normalized["successfulRecords"] = normalized.pop("successful_records")
        if "failed_records" in normalized and "failedRecords" not in normalized:
            normalized["failedRecords"] = normalized.pop("failed_records")
        return normalized

    @field_validator("progress", mode="before", check_fields=False)
    def _parse_progress(cls, v: Any) -> int:
        """Parse progress value as an integer.

        Args:
            v: The progress value from the API.

        Returns:
            The parsed integer progress.
        """
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    @property
    def total_records(self) -> int:
        """Return the total number of records processed."""
        if isinstance(self.results, list):
            return len(self.results)
        if isinstance(self.results, dict):
            if "total" in self.results:
                return int(self.results.get("total", 0))
            if self.record_total_count is not None:
                return int(self.record_total_count)
            return 0
        if self.record_total_count is not None:
            return int(self.record_total_count)
        if self.results:
            return 1
        return 0

    @property
    def successful_records(self) -> int:
        """Return the total number of successful records processed."""
        if isinstance(self.results, list):
            return sum(
                1
                for r in self.results
                if isinstance(r, dict)
                and (r.get("status") or "").upper() in {"PASS", "SUCCESS", "COMPLETED"}
            )
        if isinstance(self.results, dict):
            if "success" in self.results:
                return int(self.results.get("success", 0))
            if self.record_success_count is not None:
                return int(self.record_success_count)
            return 0
        if self.record_success_count is not None:
            return int(self.record_success_count)
        if super().is_successful and self.results:
            return 1
        return 0

    @property
    def failed_records(self) -> int:
        """Return the total number of failed records processed."""
        if isinstance(self.results, list):
            return sum(
                1
                for r in self.results
                if isinstance(r, dict)
                and (
                    (r.get("status") or "").upper() in {"FAIL", "FAILED", "ERROR"}
                    or (not r.get("status") and bool(r.get("error") or r.get("errors")))
                )
            )
        if isinstance(self.results, dict):
            if "failed" in self.results:
                return int(self.results.get("failed", 0))
            if self.record_failure_count is not None:
                return int(self.record_failure_count)
            return 0
        if self.record_failure_count is not None:
            return int(self.record_failure_count)
        if super().is_failed and self.results:
            return 1
        return 0

    @property
    def is_successful(self) -> bool:
        """Checks if the job completed in a success state with zero failed records."""
        return super().is_successful and self.failed_records == 0 and not self.result_error

    @property
    def is_failed(self) -> bool:
        """Checks if the job failed, was cancelled, or finished with record/result errors."""
        return super().is_failed or (
            self.is_terminal and (self.failed_records > 0 or bool(self.result_error))
        )


JobStatus = ModelEngine.get_model('JobStatus', JobStatus)
