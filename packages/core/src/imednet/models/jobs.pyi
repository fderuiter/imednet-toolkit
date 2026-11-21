# pylint: disable=duplicate-code
"""Job and background operation models for iMedNet."""

from __future__ import annotations

from enum import Enum
from typing import Any, overload

from pydantic import Field, field_validator, model_validator

from imednet.models.base import ImednetBaseModel

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

    def can_transition_to(self, target: JobState | str) -> bool: ...
    def validate_transition_to(self, target: JobState | str) -> bool: ...
    def __str__(self) -> str: ...
    @property
    def allowed_transitions(self) -> set[JobState]: ...

TERMINAL_JOB_STATES: set[JobState]
ACTIVE_JOB_STATES: set[JobState]
SUCCESSFUL_JOB_STATES: set[JobState]
FAILED_JOB_STATES: set[JobState]
ALLOWED_JOB_TRANSITIONS: dict[JobState, set[JobState]]

class InvalidJobStateError(ValueError):
    """Raised when an unrecognized job state string is encountered."""

class InvalidJobTransitionError(ValueError):
    """Raised when an invalid state transition is attempted."""

InvalidStateTransitionError = InvalidJobTransitionError

def normalize_job_state(state: JobState | str) -> JobState: ...
def is_valid_job_transition(from_state: JobState | str, to_state: JobState | str) -> bool: ...
def validate_job_transition(from_state: JobState | str, to_state: JobState | str) -> bool: ...

class Job(ImednetBaseModel):
    """Represents an asynchronous background job."""

    state: JobState | str | None = Field(default=None, alias="state")
    job_id: str | None = Field(default=None, alias="jobId")
    batch_id: str | None = Field(default=None, alias="batchId")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_finished: str | None = Field(default=None, alias="dateFinished")
    date_started: str | None = Field(default=None, alias="dateStarted")

    @overload
    def __init__(
        self,
        *,
        state: JobState | str | None = None,
        job_id: str | None = None,
        batch_id: str | None = None,
        date_created: str | None = None,
        date_finished: str | None = None,
        date_started: str | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        state: JobState | str | None = None,
        jobId: str | None = None,
        batchId: str | None = None,
        dateCreated: str | None = None,
        dateFinished: str | None = None,
        dateStarted: str | None = None,
    ) -> None: ...
    @field_validator("state", mode="before")
    @classmethod
    def _validate_state(cls, v: Any) -> JobState | None: ...
    def transition_to(self, target: JobState | str) -> Job: ...
    @property
    def is_terminal(self) -> bool: ...
    @property
    def is_successful(self) -> bool: ...
    @property
    def is_failed(self) -> bool: ...

class JobStatus(Job):
    """Extended job information returned when polling."""

    results: Any = Field(default=None)
    record_total_count: int | None = Field(default=None, alias="totalRecords")
    record_success_count: int | None = Field(default=None, alias="successfulRecords")
    record_failure_count: int | None = Field(default=None, alias="failedRecords")
    result_error: str | None = Field(default=None, alias="resultError")
    state: JobState | str | None = Field(default=None, alias="state")
    job_id: str | None = Field(default=None, alias="jobId")
    batch_id: str | None = Field(default=None, alias="batchId")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_finished: str | None = Field(default=None, alias="dateFinished")
    date_started: str | None = Field(default=None, alias="dateStarted")
    progress: int | None = Field(default=None, alias="progress")
    result_url: str | None = Field(default=None, alias="resultUrl")

    @overload
    def __init__(
        self,
        *,
        state: JobState | str | None = None,
        job_id: str | None = None,
        batch_id: str | None = None,
        date_created: str | None = None,
        date_finished: str | None = None,
        date_started: str | None = None,
        results: Any = None,
        record_total_count: int | None = None,
        record_success_count: int | None = None,
        record_failure_count: int | None = None,
        result_error: str | None = None,
        progress: int | None = None,
        result_url: str | None = None,
        total_records: int | None = None,
        successful_records: int | None = None,
        failed_records: int | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self,
        *,
        state: JobState | str | None = None,
        jobId: str | None = None,
        batchId: str | None = None,
        dateCreated: str | None = None,
        dateFinished: str | None = None,
        dateStarted: str | None = None,
        results: Any = None,
        totalRecords: int | None = None,
        successfulRecords: int | None = None,
        failedRecords: int | None = None,
        resultError: str | None = None,
        progress: int | None = None,
        resultUrl: str | None = None,
    ) -> None: ...
    @model_validator(mode="before")
    @classmethod
    def _normalize_record_count_keys(cls, data: Any) -> Any: ...
    @field_validator("progress", mode="before", check_fields=False)
    def _parse_progress(cls, v: Any) -> int | None: ...
    @property
    def total_records(self) -> int: ...
    @property
    def successful_records(self) -> int: ...
    @property
    def failed_records(self) -> int: ...
    @property
    def is_successful(self) -> bool: ...
    @property
    def is_failed(self) -> bool: ...

__all__ = [
    "ACTIVE_JOB_STATES",
    "ALLOWED_JOB_TRANSITIONS",
    "FAILED_JOB_STATES",
    "SUCCESSFUL_JOB_STATES",
    "TERMINAL_JOB_STATES",
    "InvalidJobStateError",
    "InvalidJobTransitionError",
    "InvalidStateTransitionError",
    "Job",
    "JobState",
    "JobStatus",
    "is_valid_job_transition",
    "normalize_job_state",
    "validate_job_transition",
]
