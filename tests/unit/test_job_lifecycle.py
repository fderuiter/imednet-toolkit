"""Comprehensive test suite for Pydantic job lifecycle state machine.

Verifies:
1. Job status models validate states against an allowed state enum.
2. Terminal states (COMPLETED, FAILED, CANCELLED) reject transitions back to RUNNING or QUEUED.
3. JobPoller.run and JobPoller.poll_many ignore stale responses when a terminal state is already set.
4. Function evaluate_job_state verifies monotonic state progression before raising errors.
5. Airflow sensor poke calls in ImednetJobSensor.poke succeed deterministically when terminal states occur.
6. Monotonic state validation for both synchronous and asynchronous pollers.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from imednet.models.jobs import (
    ACTIVE_JOB_STATES,
    FAILED_JOB_STATES,
    SUCCESSFUL_JOB_STATES,
    TERMINAL_JOB_STATES,
    InvalidJobStateError,
    InvalidJobTransitionError,
    Job,
    JobState,
    JobStatus,
    is_valid_job_transition,
    normalize_job_state,
    validate_job_transition,
)
from imednet.utils.job_poller import (
    AsyncJobPoller,
    JobFailedError,
    JobPoller,
    evaluate_job_state,
)

# ==============================================================================
# Criterion 1: Job status models validate states against an allowed state enum.
# ==============================================================================


def test_job_state_enum_members() -> None:
    """Verify that all required states are present in JobState enum."""
    expected_states = {
        "CREATED",
        "PENDING",
        "QUEUED",
        "RUNNING",
        "PROCESSING",
        "COMPLETED",
        "SUCCESS",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
    }
    actual_states = {s.value for s in JobState}
    assert expected_states.issubset(actual_states)
    assert JobState.COMPLETED in TERMINAL_JOB_STATES
    assert JobState.CREATED in ACTIVE_JOB_STATES
    assert JobState.RUNNING in ACTIVE_JOB_STATES
    assert JobState.COMPLETED in SUCCESSFUL_JOB_STATES
    assert JobState.FAILED in FAILED_JOB_STATES
    assert normalize_job_state("created") == JobState.CREATED
    assert normalize_job_state("running") == JobState.RUNNING
    created_job = Job.from_json({"jobId": "J1", "batchId": "B1", "state": "created"})
    assert created_job.state == JobState.CREATED
    assert created_job.is_terminal is False
    assert created_job.is_successful is False
    assert created_job.is_failed is False


def test_job_model_validates_and_normalizes_casing() -> None:
    """Verify casing normalization and enum validation in Job model."""
    job_lower = Job(state="completed")
    assert job_lower.state == JobState.COMPLETED
    assert job_lower.state == "COMPLETED"
    assert str(job_lower.state) == "COMPLETED"

    job_mixed = Job(state="  Running  ")
    assert job_mixed.state == JobState.RUNNING

    job_queued = Job(state="Queued")
    assert job_queued.state == JobState.QUEUED

    job_status = JobStatus(batchId="B1", state="processing")
    assert job_status.state == JobState.PROCESSING


def test_job_model_rejects_unknown_state_strings() -> None:
    """Verify that unknown state strings trigger explicit validation errors."""
    with pytest.raises((ValidationError, ValueError)):
        Job(state="UNKNOWN")

    with pytest.raises((ValidationError, ValueError)):
        Job(state="INVALID_STATE")

    with pytest.raises((ValidationError, ValueError)):
        JobStatus(batchId="B1", state="NOT_A_REAL_STATE")


def test_job_terminal_and_success_properties() -> None:
    """Verify terminal, successful, and failed properties on Job and JobStatus."""
    completed = JobStatus(batchId="1", state="COMPLETED")
    assert completed.is_terminal is True
    assert completed.is_successful is True
    assert completed.is_failed is False

    failed = JobStatus(batchId="2", state="FAILED")
    assert failed.is_terminal is True
    assert failed.is_successful is False
    assert failed.is_failed is True

    cancelled = JobStatus(batchId="3", state="CANCELLED")
    assert cancelled.is_terminal is True
    assert cancelled.is_failed is True

    timeout = JobStatus(batchId="4", state="TIMEOUT")
    assert timeout.is_terminal is True
    assert timeout.is_failed is True

    running = JobStatus(batchId="5", state="RUNNING")
    assert running.is_terminal is False
    assert running.is_successful is False
    assert running.is_failed is False


# ==============================================================================
# Criterion 2: Terminal states reject transitions back to active states.
# ==============================================================================


@pytest.mark.parametrize(
    "terminal_state", ["COMPLETED", "COMPLETE", "SUCCESS", "FAILED", "CANCELLED", "TIMEOUT"]
)
@pytest.mark.parametrize(
    "active_state", ["CREATED", "RUNNING", "QUEUED", "PROCESSING", "PENDING", "OPEN"]
)
def test_terminal_states_reject_transitions_to_active(
    terminal_state: str, active_state: str
) -> None:
    """Criterion 2: Terminal states reject transitions back to RUNNING or QUEUED."""
    assert is_valid_job_transition(terminal_state, active_state) is False

    with pytest.raises(InvalidJobTransitionError) as exc_info:
        validate_job_transition(terminal_state, active_state)
    assert "Cannot transition terminal job state" in str(exc_info.value)


@pytest.mark.parametrize("active_state", ["PENDING", "QUEUED", "OPEN", "RUNNING", "PROCESSING"])
def test_active_states_reject_backward_transitions_to_created(active_state: str) -> None:
    """Active states reject moving backwards to CREATED."""
    assert is_valid_job_transition(active_state, "CREATED") is False
    with pytest.raises(InvalidJobTransitionError):
        validate_job_transition(active_state, "CREATED")


@pytest.mark.parametrize(
    "target_state",
    [
        "CREATED",
        "PENDING",
        "QUEUED",
        "OPEN",
        "RUNNING",
        "PROCESSING",
        "COMPLETED",
        "COMPLETE",
        "SUCCESS",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
    ],
)
def test_created_allows_forward_transitions(target_state: str) -> None:
    """CREATED allows self-transition and forward transitions to active and terminal states."""
    assert is_valid_job_transition("CREATED", target_state) is True
    assert validate_job_transition("CREATED", target_state) is True
    job = Job(state="created")
    job.transition_to(target_state)
    assert job.state == JobState(target_state)


@pytest.mark.parametrize("active_state", ["RUNNING", "PROCESSING"])
def test_active_states_reject_backward_transitions_to_queued(active_state: str) -> None:
    """Active execution states reject moving backwards to QUEUED or PENDING."""
    assert is_valid_job_transition(active_state, "QUEUED") is False
    assert is_valid_job_transition(active_state, "PENDING") is False

    with pytest.raises(InvalidJobTransitionError):
        validate_job_transition(active_state, "QUEUED")


def test_approved_lifecycle_forward_transitions() -> None:
    """Verify forward state transitions along approved lifecycle paths."""
    # PENDING -> QUEUED -> RUNNING -> COMPLETED
    job = Job(state="PENDING")
    job.transition_to("QUEUED")
    assert job.state == JobState.QUEUED

    job.transition_to("RUNNING")
    assert job.state == JobState.RUNNING

    job.transition_to("COMPLETED")
    assert job.state == JobState.COMPLETED

    # Self transitions are idempotent and allowed
    assert is_valid_job_transition("RUNNING", "RUNNING") is True
    assert is_valid_job_transition("QUEUED", "QUEUED") is True
    assert is_valid_job_transition("COMPLETED", "COMPLETED") is True


# ==============================================================================
# Criterion 3: JobPoller.run & poll_many ignore stale responses when terminal.
# ==============================================================================


def test_job_poller_run_ignores_stale_response_after_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 3: Synchronous run ignores stale response when terminal state is already set."""
    responses = [
        JobStatus(batchId="B1", state="QUEUED"),
        JobStatus(batchId="B1", state="RUNNING"),
        JobStatus(batchId="B1", state="COMPLETED"),
    ]
    poller = JobPoller(lambda s, b: responses.pop(0))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    result = poller.run("ST", "B1", interval=0, timeout=10)
    assert result.state == JobState.COMPLETED

    # If run is called again for B1 and receives a stale non-terminal payload
    stale_poller = JobPoller(lambda s, b: JobStatus(batchId="B1", state="RUNNING"))
    stale_poller._terminal_states["B1"] = result  # Terminal state already set
    recovered = stale_poller.run("ST", "B1", interval=0, timeout=10)
    assert recovered.state == JobState.COMPLETED


def test_job_poller_run_rejects_active_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synchronous poller rejects backward transitions during active polling."""
    responses = [
        JobStatus(batchId="B1", state="RUNNING"),
        JobStatus(batchId="B1", state="QUEUED"),  # Regressive transition!
    ]
    poller = JobPoller(lambda s, b: responses.pop(0))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    with pytest.raises(InvalidJobTransitionError):
        poller.run("ST", "B1", interval=0, timeout=10)


def test_job_poller_poll_many_ignores_stale_responses() -> None:
    """Criterion 3: JobPoller.poll_many preserves terminal states across concurrent threads."""
    completed_status = JobStatus(batchId="B1", state="COMPLETED")
    poller = JobPoller(lambda s, b: completed_status)

    # Seed terminal state
    poller._terminal_states["B1"] = completed_status

    # Now poll_many with B1 returning stale running responses
    poller._get_job = lambda s, b: JobStatus(batchId="B1", state="RUNNING")
    summary = poller.poll_many("ST", ["B1"], interval=0, timeout=5)

    assert summary.results["B1"].state == JobState.COMPLETED
    assert summary.results["B1"].is_terminal is True


# ==============================================================================
# Criterion 4: evaluate_job_state verifies monotonic state progression.
# ==============================================================================


def test_evaluate_job_state_verifies_monotonic_progression_before_raising_errors() -> None:
    """Criterion 4: evaluate_job_state verifies monotonic transitions before raising failure errors."""
    # Valid forward transition from RUNNING to FAILED -> raises JobFailedError
    failed_job = JobStatus(batchId="B1", state="FAILED")
    with pytest.raises(JobFailedError):
        evaluate_job_state(failed_job, previous_state="RUNNING")

    # Invalid regressive transition from COMPLETED to FAILED -> raises InvalidJobTransitionError FIRST!
    with pytest.raises(InvalidJobTransitionError):
        evaluate_job_state(failed_job, previous_state="COMPLETED")

    # Invalid regressive transition from COMPLETED to RUNNING -> raises InvalidJobTransitionError!
    running_job = JobStatus(batchId="B1", state="RUNNING")
    with pytest.raises(InvalidJobTransitionError):
        evaluate_job_state(running_job, previous_state="COMPLETED")

    # Valid in-progress transition -> returns False
    assert evaluate_job_state(running_job, previous_state="QUEUED") is False

    # Valid completion transition -> returns True
    completed_job = JobStatus(batchId="B1", state="COMPLETED")
    assert evaluate_job_state(completed_job, previous_state="RUNNING") is True


def test_evaluate_job_state_rejects_unvalidated_state_strings() -> None:
    """evaluate_job_state rejects invalid state strings."""
    mock_job = MagicMock()
    mock_job.state = "BOGUS_STATE"
    with pytest.raises(InvalidJobStateError):
        evaluate_job_state(mock_job)


# ==============================================================================
# Criterion 5: Airflow sensor poke succeeds deterministically on terminal states.
# ==============================================================================


def test_airflow_job_sensor_stale_response_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 5: ImednetJobSensor.poke succeeds deterministically when terminal states occur."""
    import importlib

    from tests.unit.test_airflow_operators import _patch_basehook, _setup_airflow

    _setup_airflow(monkeypatch)
    _patch_basehook(monkeypatch)

    from apache_airflow_providers_imednet import sensors

    importlib.reload(sensors)

    sdk = MagicMock()
    hook_inst = MagicMock(get_sdk_client=MagicMock(return_value=sdk))
    monkeypatch.setattr(sensors, "ImednetHook", MagicMock(return_value=hook_inst))

    sensor = sensors.ImednetJobSensor(task_id="t", study_key="S", batch_id="B")

    # 1. Active poke -> returns False
    running_job = JobStatus(batchId="B", state="RUNNING")
    sdk.jobs.get.return_value = running_job
    assert sensor.poke({}) is False

    # 2. Terminal completion poke -> returns True
    completed_job = JobStatus(batchId="B", state="COMPLETED")
    sdk.jobs.get.return_value = completed_job
    assert sensor.poke({}) is True

    # 3. Subsequent stale non-terminal poke -> succeeds deterministically (returns True)
    stale_job = JobStatus(batchId="B", state="RUNNING")
    sdk.jobs.get.return_value = stale_job
    assert sensor.poke({}) is True


# ==============================================================================
# Criterion 6: Automated tests for both synchronous and asynchronous pollers.
# ==============================================================================


@pytest.mark.asyncio
async def test_async_job_poller_ignores_stale_response_after_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 6: Asynchronous poller ignores stale response when terminal state is already set."""
    responses = [
        JobStatus(batchId="A1", state="QUEUED"),
        JobStatus(batchId="A1", state="RUNNING"),
        JobStatus(batchId="A1", state="COMPLETED"),
    ]
    poller = AsyncJobPoller(AsyncMock(side_effect=lambda s, b: responses.pop(0)))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    result = await poller.run("ST", "A1", interval=0, timeout=10)
    assert result.state == JobState.COMPLETED

    # Next call with stale non-terminal payload preserves terminal state
    stale_poller = AsyncJobPoller(AsyncMock(return_value=JobStatus(batchId="A1", state="RUNNING")))
    stale_poller._terminal_states["A1"] = result
    recovered = await stale_poller.run("ST", "A1", interval=0, timeout=10)
    assert recovered.state == JobState.COMPLETED


@pytest.mark.asyncio
async def test_async_job_poller_rejects_active_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 6: Asynchronous poller rejects backward transitions during active polling."""
    responses = [
        JobStatus(batchId="A1", state="RUNNING"),
        JobStatus(batchId="A1", state="QUEUED"),
    ]
    poller = AsyncJobPoller(AsyncMock(side_effect=lambda s, b: responses.pop(0)))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    with pytest.raises(InvalidJobTransitionError):
        await poller.run("ST", "A1", interval=0, timeout=10)


@pytest.mark.asyncio
async def test_async_poll_many_ignores_stale_responses() -> None:
    """Criterion 6: AsyncJobPoller.async_poll_many preserves terminal states across concurrent tasks."""
    completed_status = JobStatus(batchId="A1", state="COMPLETED")
    poller = AsyncJobPoller(AsyncMock(return_value=completed_status))
    poller._terminal_states["A1"] = completed_status

    poller._get_job = AsyncMock(return_value=JobStatus(batchId="A1", state="RUNNING"))
    summary = await poller.async_poll_many("ST", ["A1"], interval=0, timeout=5)

    assert summary.results["A1"].state == JobState.COMPLETED
    assert summary.results["A1"].is_terminal is True


def test_job_poller_accepts_created_progression_and_rejects_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JobPoller accepts CREATED -> QUEUED -> RUNNING -> PROCESSING -> COMPLETED/FAILED and rejects -> CREATED."""
    monkeypatch.setattr("time.sleep", lambda *_: None)

    seq_ok = [
        JobStatus(batchId="B1", state="created"),
        JobStatus(batchId="B1", state="QUEUED"),
        JobStatus(batchId="B1", state="RUNNING"),
        JobStatus(batchId="B1", state="PROCESSING"),
        JobStatus(batchId="B1", state="COMPLETED"),
    ]
    poller = JobPoller(lambda s, b: seq_ok.pop(0))
    res = poller.run("ST", "B1", interval=0, timeout=10)
    assert res.state == JobState.COMPLETED

    seq_fail = [
        JobStatus(batchId="B2", state="CREATED"),
        JobStatus(batchId="B2", state="FAILED"),
    ]
    fail_poller = JobPoller(lambda s, b: seq_fail.pop(0))
    with pytest.raises(JobFailedError) as exc_info:
        fail_poller.run("ST", "B2", interval=0, timeout=10)
    assert exc_info.value.status.state == JobState.FAILED

    seq_regress = [
        JobStatus(batchId="B3", state="QUEUED"),
        JobStatus(batchId="B3", state="CREATED"),
    ]
    reg_poller = JobPoller(lambda s, b: seq_regress.pop(0))
    with pytest.raises(InvalidJobTransitionError):
        reg_poller.run("ST", "B3", interval=0, timeout=10)


@pytest.mark.asyncio
async def test_async_job_poller_accepts_created_progression_and_rejects_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncJobPoller accepts CREATED -> QUEUED -> RUNNING -> PROCESSING -> COMPLETED/FAILED and rejects -> CREATED."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    seq_ok = [
        JobStatus(batchId="A2", state="created"),
        JobStatus(batchId="A2", state="QUEUED"),
        JobStatus(batchId="A2", state="RUNNING"),
        JobStatus(batchId="A2", state="PROCESSING"),
        JobStatus(batchId="A2", state="COMPLETED"),
    ]
    poller = AsyncJobPoller(AsyncMock(side_effect=lambda s, b: seq_ok.pop(0)))
    res = await poller.run("ST", "A2", interval=0, timeout=10)
    assert res.state == JobState.COMPLETED

    seq_fail = [
        JobStatus(batchId="A3", state="CREATED"),
        JobStatus(batchId="A3", state="FAILED"),
    ]
    fail_poller = AsyncJobPoller(AsyncMock(side_effect=lambda s, b: seq_fail.pop(0)))
    with pytest.raises(JobFailedError) as exc_info:
        await fail_poller.run("ST", "A3", interval=0, timeout=10)
    assert exc_info.value.status.state == JobState.FAILED

    seq_regress = [
        JobStatus(batchId="A4", state="RUNNING"),
        JobStatus(batchId="A4", state="CREATED"),
    ]
    reg_poller = AsyncJobPoller(AsyncMock(side_effect=lambda s, b: seq_regress.pop(0)))
    with pytest.raises(InvalidJobTransitionError):
        await reg_poller.run("ST", "A4", interval=0, timeout=10)
