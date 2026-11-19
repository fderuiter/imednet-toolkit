# pylint: disable=duplicate-code
"""Job and background operation models for iMedNet."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, field_validator

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
    job_id: str | None
    batch_id: str | None
    date_created: str | None
    date_finished: str | None
    date_started: str | None

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
    state: JobState | str | None
    job_id: str | None
    batch_id: str | None
    date_created: str | None
    date_finished: str | None
    date_started: str | None
    progress: int | None
    result_url: str | None

    @field_validator("progress", mode="before", check_fields=False)
    def _parse_progress(cls, v: Any) -> int: ...
    @property
    def total_records(self) -> int: ...
    @property
    def successful_records(self) -> int: ...
    @property
    def failed_records(self) -> int: ...
