"""Tests for JobPoller and AsyncJobPoller."""

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from imednet.errors import PathTraversalValidationError
from imednet.models.jobs import JobStatus
from imednet.utils.job_poller import (
    AsyncJobPoller,
    BaseJobPoller,
    JobFailedError,
    JobPoller,
    JobStatusEvent,
    JobTimeoutError,
    _raise_fetch_result_error,
    fetch_terminal_job_result,
)


def test_job_poller_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test successful synchronous job polling."""
    states = [
        JobStatus(batchId="1", state="PROCESSING", progress=10, jobId="1", resultUrl=""),
        JobStatus(batchId="1", state="COMPLETED", progress=100, jobId="1", resultUrl=""),
    ]
    get_job = MagicMock(side_effect=lambda s, b: states.pop(0))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    poller = JobPoller(get_job)
    result = poller.run("ST", "1", interval=0, timeout=5)
    assert result.state == "COMPLETED"


@pytest.mark.asyncio
async def test_async_job_poller_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test successful asynchronous job polling."""
    states = [
        JobStatus(batchId="1", state="PROCESSING", progress=10, jobId="1", resultUrl=""),
        JobStatus(batchId="1", state="COMPLETED", progress=100, jobId="1", resultUrl=""),
    ]
    get_job = AsyncMock(side_effect=lambda s, b: states.pop(0))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    poller = AsyncJobPoller(get_job)
    result = await poller.run("ST", "1", interval=0, timeout=5)
    assert result.state == "COMPLETED"


def test_job_poller_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test synchronous job polling timeout."""
    job = JobStatus(batchId="1", state="PROCESSING", progress=10, jobId="1", resultUrl="")
    get_job = MagicMock(return_value=job)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    poller = JobPoller(get_job)

    tick = {"v": 0.0}

    def monotonic() -> float:
        tick["v"] += 10.0
        return tick["v"]

    monkeypatch.setattr("time.monotonic", monotonic)
    with pytest.raises(JobTimeoutError):
        poller.run("S", "1", interval=0, timeout=2)


@pytest.mark.asyncio
async def test_async_job_poller_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test asynchronous job polling timeout."""
    job = JobStatus(batchId="1", state="PROCESSING", progress=10, jobId="1", resultUrl="")
    get_job = AsyncMock(return_value=job)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    poller = AsyncJobPoller(get_job)

    tick = {"v": 0.0}

    def monotonic() -> float:
        tick["v"] += 10.0
        return tick["v"]

    monkeypatch.setattr("time.monotonic", monotonic)
    with pytest.raises(JobTimeoutError):
        await poller.run("S", "1", interval=0, timeout=2)


def test_job_poller_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test synchronous job polling failure."""
    states = [
        JobStatus(batchId="1", state="PROCESSING", progress=10, jobId="1", resultUrl=""),
        JobStatus(batchId="1", state="FAILED", progress=10, jobId="1", resultUrl=""),
    ]
    get_job = MagicMock(side_effect=lambda s, b: states.pop(0))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    poller = JobPoller(get_job)

    with pytest.raises(JobFailedError) as exc_info:
        poller.run("S", "1", interval=0, timeout=5)
    assert exc_info.value.status.state == "FAILED"


@pytest.mark.asyncio
async def test_async_job_poller_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test asynchronous job polling failure."""
    states = [
        JobStatus(batchId="1", state="PROCESSING", progress=10, jobId="1", resultUrl=""),
        JobStatus(batchId="1", state="FAILED", progress=10, jobId="1", resultUrl=""),
    ]
    get_job = AsyncMock(side_effect=lambda s, b: states.pop(0))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    poller = AsyncJobPoller(get_job)

    with pytest.raises(JobFailedError) as exc_info:
        await poller.run("S", "1", interval=0, timeout=5)
    assert exc_info.value.status.state == "FAILED"


def test_run_on_progress_new_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test on_progress callback with JobStatusEvent signature."""
    states = [
        JobStatus(batchId="1", state="PROCESSING", progress=10, jobId="1", resultUrl=""),
        JobStatus(batchId="1", state="COMPLETED", progress=100, jobId="1", resultUrl=""),
    ]
    get_job = MagicMock(side_effect=lambda s, b: states.pop(0))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    events = []

    def callback(event: JobStatusEvent) -> None:
        events.append(event)

    poller = JobPoller(get_job)
    poller.run("ST", "1", interval=0, on_progress=callback)

    assert len(events) == 2
    assert isinstance(events[0], JobStatusEvent)
    assert events[0].batch_id == "1"
    assert events[0].status.state == "PROCESSING"
    assert events[0].poll_number == 1
    assert events[0].is_terminal is False

    assert events[1].status.state == "COMPLETED"
    assert events[1].poll_number == 2
    assert events[1].is_terminal is True


def test_run_on_progress_deprecated_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test on_progress callback with deprecated (batch_id, status, elapsed) signature."""
    states = [
        JobStatus(batchId="1", state="PROCESSING", progress=10, jobId="1", resultUrl=""),
        JobStatus(batchId="1", state="COMPLETED", progress=100, jobId="1", resultUrl=""),
    ]
    get_job = MagicMock(side_effect=lambda s, b: states.pop(0))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    calls = []

    def callback(batch_id, status, elapsed) -> None:
        calls.append((batch_id, status, elapsed))

    poller = JobPoller(get_job)
    poller.run("ST", "1", interval=0, on_progress=callback)

    assert len(calls) == 2
    assert calls[0][0] == "1"
    assert calls[0][1].state == "PROCESSING"
    assert isinstance(calls[0][2], float)


def test_poll_many_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test poll_many with multiple successful jobs."""
    jobs_states = {
        "1": [
            JobStatus(batchId="1", state="PROCESSING", progress=50, jobId="1", resultUrl=""),
            JobStatus(batchId="1", state="COMPLETED", progress=100, jobId="1", resultUrl=""),
        ],
        "2": [
            JobStatus(batchId="2", state="COMPLETED", progress=100, jobId="2", resultUrl=""),
        ],
    }

    def get_job(study_key, batch_id):
        return jobs_states[batch_id].pop(0)

    monkeypatch.setattr("time.sleep", lambda *_: None)
    poller = JobPoller(get_job)
    summary = poller.poll_many("ST", ["1", "2"], interval=0)

    assert summary.all_successful is True
    assert summary.any_failed is False
    assert len(summary.results) == 2
    assert summary.results["1"].state == "COMPLETED"
    assert summary.results["2"].state == "COMPLETED"
    assert len(summary.failures) == 0


def test_poll_many_with_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test poll_many where some jobs fail."""
    jobs_states = {
        "1": [JobStatus(batchId="1", state="COMPLETED", progress=100, jobId="1", resultUrl="")],
        "2": [JobStatus(batchId="2", state="FAILED", progress=0, jobId="2", resultUrl="")],
    }

    def get_job(study_key, batch_id):
        return jobs_states[batch_id].pop(0)

    monkeypatch.setattr("time.sleep", lambda *_: None)
    poller = JobPoller(get_job)
    summary = poller.poll_many("ST", ["1", "2"], interval=0)

    assert summary.all_successful is False
    assert summary.any_failed is True
    assert summary.results["1"].state == "COMPLETED"
    assert "2" not in summary.results
    assert len(summary.failures) == 1
    assert isinstance(summary.failures["2"], JobFailedError)


def test_poll_many_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test poll_many with fail_fast=True."""

    def get_job(study_key, batch_id):
        if batch_id == "FAIL":
            raise ValueError("Immediate failure")
        return JobStatus(batchId="OK", state="COMPLETED", progress=100, jobId="2", resultUrl="")

    monkeypatch.setattr("time.sleep", lambda *_: None)
    poller = JobPoller(get_job)

    with pytest.raises(ValueError, match="Immediate failure"):
        poller.poll_many("ST", ["OK", "FAIL"], interval=0, fail_fast=True)


@pytest.mark.asyncio
async def test_async_poll_many_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test async_poll_many with multiple successful jobs."""
    jobs_states = {
        "1": [
            JobStatus(batchId="1", state="PROCESSING", progress=50, jobId="1", resultUrl=""),
            JobStatus(batchId="1", state="COMPLETED", progress=100, jobId="1", resultUrl=""),
        ],
        "2": [
            JobStatus(batchId="2", state="COMPLETED", progress=100, jobId="2", resultUrl=""),
        ],
    }

    async def get_job(study_key, batch_id):
        return jobs_states[batch_id].pop(0)

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    poller = AsyncJobPoller(get_job)
    summary = await poller.async_poll_many("ST", ["1", "2"], interval=0)

    assert summary.all_successful is True
    assert len(summary.results) == 2
    assert summary.results["1"].state == "COMPLETED"
    assert summary.results["2"].state == "COMPLETED"


def test_logging_output(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Test structured logging output."""
    states = [
        JobStatus(batchId="LOG", state="PROCESSING", progress=10, jobId="1", resultUrl=""),
        JobStatus(batchId="LOG", state="COMPLETED", progress=100, jobId="1", resultUrl=""),
    ]
    get_job = MagicMock(side_effect=lambda s, b: states.pop(0))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    poller = JobPoller(get_job)
    with caplog.at_level(logging.DEBUG):
        poller.run("ST", "LOG", interval=0)

    assert "[JobPoller] batch_id=LOG state=PROCESSING progress=10% elapsed=" in caplog.text
    assert "[JobPoller] batch_id=LOG COMPLETED after 2 polls" in caplog.text


def test_logging_error(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Test logging output on failure."""
    states = [
        JobStatus(batchId="ERR", state="FAILED", progress=0, jobId="1", resultUrl=""),
    ]
    get_job = MagicMock(side_effect=lambda s, b: states.pop(0))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    poller = JobPoller(get_job)
    with caplog.at_level(logging.DEBUG), contextlib.suppress(JobFailedError):
        poller.run("ST", "ERR", interval=0)

    assert "[JobPoller] batch_id=ERR FAILED: state=FAILED" in caplog.text


def test_job_status_event_immutable() -> None:
    """Test that JobStatusEvent is frozen."""
    status = JobStatus(batchId="1", state="COMPLETED", progress=100, jobId="1", resultUrl="")
    event = JobStatusEvent(
        batch_id="1", status=status, poll_number=1, elapsed=1.0, is_terminal=True
    )
    with pytest.raises(AttributeError):
        event.poll_number = 2  # type: ignore


def test_job_poller_fetch_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test fetch_result functionality."""

    def get_job(study_key, batch_id):
        return JobStatus(
            batchId=batch_id, state="COMPLETED", progress=100, jobId="j1", resultUrl="http://test"
        )

    class MockResponse:
        def json(self):
            return {"data": "ok"}

    def fetch_res(url):
        if url == "fail":
            raise Exception("fetch fail")
        return MockResponse()

    monkeypatch.setattr("time.sleep", lambda *_: None)

    # Success path
    poller = JobPoller(get_job, fetch_result=fetch_res)
    st = poller.run("ST", "1", interval=0)
    assert st.results == {"data": "ok"}

    # Failure path
    def get_job_fail(study_key, batch_id):
        return JobStatus(
            batchId=batch_id, state="COMPLETED", progress=100, jobId="j1", resultUrl="fail"
        )

    poller_fail = JobPoller(get_job_fail, fetch_result=fetch_res)
    with pytest.raises(JobFailedError, match="failed to fetch resultUrl") as exc_info:
        poller_fail.run("ST", "1", interval=0)
    assert exc_info.value.status is not None
    assert exc_info.value.status.results is None
    assert exc_info.value.status.result_error == "fetch fail"
    assert exc_info.value.result_error == "fetch fail"

    # Text fallback
    class MockTextResponse:
        def json(self):
            raise ValueError()

        @property
        def text(self):
            return "text data"

    poller_text = JobPoller(get_job, fetch_result=lambda url: MockTextResponse())
    st3 = poller_text.run("ST", "1", interval=0)
    assert st3.results == "text data"

    # Raw string
    poller_str = JobPoller(get_job, fetch_result=lambda url: "raw string")
    st4 = poller_str.run("ST", "1", interval=0)
    assert st4.results == "raw string"


@pytest.mark.asyncio
async def test_async_job_poller_fetch_result() -> None:
    """Test async fetch_result functionality."""

    async def get_job(study_key, batch_id):
        return JobStatus(
            batchId=batch_id, state="COMPLETED", progress=100, jobId="j1", resultUrl="http://test"
        )

    class MockResponse:
        def json(self):
            return {"data": "ok"}

    async def fetch_res(url):
        if url == "fail":
            raise Exception("fetch fail")
        return MockResponse()

    # Success path
    poller = AsyncJobPoller(get_job, fetch_result=fetch_res)
    st = await poller.run("ST", "1", interval=0)
    assert st.results == {"data": "ok"}

    # Failure path
    async def get_job_fail(study_key, batch_id):
        return JobStatus(
            batchId=batch_id, state="COMPLETED", progress=100, jobId="j1", resultUrl="fail"
        )

    poller_fail = AsyncJobPoller(get_job_fail, fetch_result=fetch_res)
    with pytest.raises(JobFailedError, match="failed to fetch resultUrl") as exc_info:
        await poller_fail.run("ST", "1", interval=0)
    assert exc_info.value.status is not None
    assert exc_info.value.status.results is None
    assert exc_info.value.status.result_error == "fetch fail"
    assert exc_info.value.result_error == "fetch fail"


@pytest.mark.asyncio
async def test_async_poll_many_with_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test async_poll_many where some jobs fail."""
    jobs_states = {
        "1": [JobStatus(batchId="1", state="COMPLETED", progress=100, jobId="1", resultUrl="")],
        "2": [JobStatus(batchId="2", state="FAILED", progress=0, jobId="2", resultUrl="")],
    }

    async def get_job(study_key, batch_id):
        return jobs_states[batch_id].pop(0)

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    poller = AsyncJobPoller(get_job)
    summary = await poller.async_poll_many("ST", ["1", "2"], interval=0)

    assert summary.all_successful is False
    assert summary.any_failed is True
    assert summary.results["1"].state == "COMPLETED"
    assert "2" not in summary.results
    assert len(summary.failures) == 1
    assert isinstance(summary.failures["2"], JobFailedError)


@pytest.mark.asyncio
async def test_async_poll_many_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test async_poll_many with fail_fast=True."""

    async def get_job(study_key, batch_id):
        if batch_id == "FAIL":
            raise ValueError("Immediate failure")
        return JobStatus(batchId="OK", state="COMPLETED", progress=100, jobId="2", resultUrl="")

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    poller = AsyncJobPoller(get_job)

    with pytest.raises(ValueError, match="Immediate failure"):
        await poller.async_poll_many("ST", ["OK", "FAIL"], interval=0, fail_fast=True)


def test_job_status_and_summary_partial_record_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Completed JobStatus with nonzero failed_records marks is_successful and all_successful False."""
    monkeypatch.setattr("time.sleep", lambda *_: None)

    partial_list_status = JobStatus(
        batchId="B1",
        state="COMPLETED",
        progress=100,
        results=[
            {"recordId": 1, "status": "SUCCESS"},
            {"recordId": 2, "status": "FAILED", "error": "DEM_AGE out of range"},
        ],
    )
    assert partial_list_status.total_records == 2
    assert partial_list_status.successful_records == 1
    assert partial_list_status.failed_records == 1
    assert partial_list_status.is_successful is False
    assert partial_list_status.is_failed is True

    partial_dict_status = JobStatus(
        batchId="B2",
        state="COMPLETED",
        progress=100,
        results={"total": 4, "success": 3, "failed": 1, "errors": [{"message": "Bad site"}]},
    )
    assert partial_dict_status.total_records == 4
    assert partial_dict_status.successful_records == 3
    assert partial_dict_status.failed_records == 1
    assert partial_dict_status.is_successful is False
    assert partial_dict_status.is_failed is True

    partial_field_status = JobStatus.model_validate(
        {
            "batchId": "B3",
            "state": "COMPLETED",
            "progress": 100,
            "totalRecords": 10,
            "successfulRecords": 8,
            "failedRecords": 2,
        }
    )
    assert partial_field_status.total_records == 10
    assert partial_field_status.successful_records == 8
    assert partial_field_status.failed_records == 2
    assert partial_field_status.is_successful is False
    assert partial_field_status.is_failed is True

    # Direct summary construction with a partial-failure status in results still reports False
    from imednet.utils.job_poller import JobPollSummary

    direct_summary = JobPollSummary(
        study_key="ST",
        results={"B1": partial_list_status},
        failures={},
        elapsed_total=0.0,
    )
    assert direct_summary.all_successful is False
    assert direct_summary.any_failed is True

    # Polling surfaces JobFailedError with Record failure diagnostics attached
    poller = JobPoller(lambda _s, _b: partial_list_status)
    with pytest.raises(JobFailedError, match="failed_records=1") as exc_info:
        poller.run("ST", "B1", interval=0)
    assert exc_info.value.record_errors == [
        {"recordId": 2, "status": "FAILED", "error": "DEM_AGE out of range"}
    ]
    assert "DEM_AGE out of range" in str(exc_info.value)

    summary = poller.poll_many("ST", ["B1"], interval=0)
    assert summary.all_successful is False
    assert summary.any_failed is True
    assert "B1" in summary.failures
    assert summary.failures["B1"].record_errors


@pytest.mark.asyncio
async def test_poll_many_records_result_url_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Polling multiple jobs records and surfaces resultUrl fetch errors in sync and async modes."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    def get_sync_job(study_key: str, batch_id: str) -> JobStatus:
        return JobStatus(
            batchId=batch_id,
            state="COMPLETED",
            progress=100,
            resultUrl="https://example.test/results/secret",
        )

    def failing_sync_fetch(url: str) -> object:
        raise RuntimeError("502 Bad Gateway for Authorization: Bearer top-secret-token")

    sync_poller = JobPoller(get_sync_job, fetch_result=failing_sync_fetch)
    sync_summary = sync_poller.poll_many("ST", ["B_ERR"], interval=0)
    assert sync_summary.all_successful is False
    assert sync_summary.any_failed is True
    err = sync_summary.failures["B_ERR"]
    assert isinstance(err, JobFailedError)
    assert "top-secret-token" not in str(err)
    assert err.status is not None
    assert err.status.is_successful is False
    assert err.status.result_error is not None
    assert "502 Bad Gateway" in err.status.result_error

    async def get_async_job(study_key: str, batch_id: str) -> JobStatus:
        return JobStatus(
            batchId=batch_id,
            state="COMPLETED",
            progress=100,
            resultUrl="https://example.test/results/async",
        )

    async def failing_async_fetch(url: str) -> object:
        raise RuntimeError("Connection reset fetching resultUrl")

    async_poller = AsyncJobPoller(get_async_job, fetch_result=failing_async_fetch)
    async_summary = await async_poller.async_poll_many("ST", ["B_ASYNC_ERR"], interval=0)
    assert async_summary.all_successful is False
    assert async_summary.any_failed is True
    async_err = async_summary.failures["B_ASYNC_ERR"]
    assert isinstance(async_err, JobFailedError)
    assert async_err.result_error == "Connection reset fetching resultUrl"


@pytest.mark.asyncio
async def test_raise_fetch_result_error_redaction_parity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_raise_fetch_result_error redacts secrets identically across helper, sync, and async paths."""
    raw_failure_text = "403 Forbidden with Authorization: Bearer top-secret-token"

    direct_status = JobStatus(batchId="B_DIR", state="COMPLETED", progress=100)
    cause = RuntimeError(raw_failure_text)
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(JobFailedError, match="Job B_DIR failed to fetch resultUrl") as direct_exc,
    ):
        _raise_fetch_result_error("B_DIR", direct_status, cause)

    assert direct_exc.value.__cause__ is cause
    assert direct_status.result_error is not None
    assert "top-secret-token" not in direct_status.result_error
    assert "top-secret-token" not in str(direct_exc.value)
    assert "top-secret-token" not in caplog.text
    assert "[JobPoller] batch_id=B_DIR FAILED fetching resultUrl:" in caplog.text

    caplog.clear()
    sync_status = JobStatus(
        batchId="B_SYNC",
        state="COMPLETED",
        progress=100,
        resultUrl="https://example.test/res",
    )

    def fail_sync(_url: str) -> object:
        raise RuntimeError(raw_failure_text)

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(JobFailedError) as sync_exc,
    ):
        fetch_terminal_job_result(sync_status, fail_sync)

    assert sync_exc.value.result_error == direct_status.result_error
    assert "top-secret-token" not in caplog.text

    caplog.clear()

    async def get_async_job(_study: str, batch_id: str) -> JobStatus:
        return JobStatus(
            batchId=batch_id,
            state="COMPLETED",
            progress=100,
            resultUrl="https://example.test/res-async",
        )

    async def fail_async(_url: str) -> object:
        raise RuntimeError(raw_failure_text)

    async_poller = AsyncJobPoller(get_async_job, fetch_result=fail_async)
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(JobFailedError) as async_exc,
    ):
        await async_poller.run("ST", "B_ASYNC", interval=0)

    assert async_exc.value.result_error == direct_status.result_error
    assert async_exc.value.status.result_error == direct_status.result_error
    assert "top-secret-token" not in str(async_exc.value)
    assert "top-secret-token" not in caplog.text
    assert "[JobPoller] batch_id=B_ASYNC FAILED fetching resultUrl:" in caplog.text


@pytest.mark.asyncio
async def test_process_fetch_result_hook_and_async_cancellation() -> None:
    """Custom _process_fetch_result hook transforms results and CancelledError propagates unwrapped."""
    hook_calls: list[tuple[str, object]] = []

    class CustomAsyncJobPoller(AsyncJobPoller):
        def _process_fetch_result(self, status: JobStatus, res: object) -> JobStatus:
            hook_calls.append((status.batch_id, res))
            processed = super()._process_fetch_result(status, res)
            if isinstance(processed.results, dict):
                processed.results = {**processed.results, "hook_applied": True}
            return processed

    async def get_job(_study: str, batch_id: str) -> JobStatus:
        return JobStatus(
            batchId=batch_id,
            state="COMPLETED",
            progress=100,
            resultUrl="https://example.test/out",
        )

    async def fetch_ok(_url: str) -> dict[str, object]:
        return {"rows": 3}

    poller = CustomAsyncJobPoller(get_job, fetch_result=fetch_ok)
    final_status = await poller.run("ST", "B_HOOK", interval=0)
    assert hook_calls == [("B_HOOK", {"rows": 3})]
    assert final_status.results == {"rows": 3, "hook_applied": True}

    async def fetch_cancelled(_url: str) -> object:
        raise asyncio.CancelledError

    cancelled_poller = CustomAsyncJobPoller(get_job, fetch_result=fetch_cancelled)
    with pytest.raises(asyncio.CancelledError):
        await cancelled_poller.run("ST", "B_CANCEL", interval=0)


@pytest.mark.asyncio
async def test_poll_step_events_and_batch_helpers_parity(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sync and async pollers emit identical poll-step events, logs, and batch guard semantics."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    sync_states = [
        JobStatus(batchId="B_PAR", state="PROCESSING", progress=25, jobId="1", resultUrl=""),
        JobStatus(batchId="B_PAR", state="COMPLETED", progress=100, jobId="1", resultUrl=""),
    ]
    async_states = [
        JobStatus(batchId="B_PAR", state="PROCESSING", progress=25, jobId="1", resultUrl=""),
        JobStatus(batchId="B_PAR", state="COMPLETED", progress=100, jobId="1", resultUrl=""),
    ]

    sync_events: list[JobStatusEvent] = []
    async_events: list[JobStatusEvent] = []

    sync_poller = JobPoller(lambda _s, _b: sync_states.pop(0))
    async_poller = AsyncJobPoller(AsyncMock(side_effect=lambda _s, _b: async_states.pop(0)))

    with caplog.at_level(logging.DEBUG):
        sync_res = sync_poller.run("ST", "B_PAR", interval=0, on_progress=sync_events.append)
    sync_logs = caplog.text
    caplog.clear()

    with caplog.at_level(logging.DEBUG):
        async_res = await async_poller.run(
            "ST", "B_PAR", interval=0, on_progress=async_events.append
        )
    async_logs = caplog.text

    assert sync_res.state == async_res.state == "COMPLETED"
    assert [(e.batch_id, e.poll_number, e.status.state, e.is_terminal) for e in sync_events] == [
        (e.batch_id, e.poll_number, e.status.state, e.is_terminal) for e in async_events
    ]
    assert "[JobPoller] batch_id=B_PAR state=PROCESSING progress=25% elapsed=" in sync_logs
    assert "[JobPoller] batch_id=B_PAR state=PROCESSING progress=25% elapsed=" in async_logs
    assert "[JobPoller] batch_id=B_PAR COMPLETED after 2 polls" in sync_logs
    assert "[JobPoller] batch_id=B_PAR COMPLETED after 2 polls" in async_logs

    # Path segment validation applies uniformly across run, poll_many, and async_poll_many
    with pytest.raises(PathTraversalValidationError):
        sync_poller.run("../bad", "B1")
    with pytest.raises(PathTraversalValidationError):
        await async_poller.run("ST", "../bad")
    with pytest.raises(PathTraversalValidationError):
        sync_poller.poll_many("ST", ["../bad"])
    with pytest.raises(PathTraversalValidationError):
        await async_poller.async_poll_many("ST", ["../bad"])

    # _record_batch_result preserves terminal results against non-terminal regressions
    base = BaseJobPoller()
    recorded: dict[str, JobStatus] = {}
    terminal = JobStatus(batchId="B1", state="COMPLETED", progress=100)
    stale = JobStatus(batchId="B1", state="RUNNING", progress=50)
    base._record_batch_result(recorded, "B1", terminal)
    base._record_batch_result(recorded, "B1", stale)
    assert recorded["B1"] is terminal


def test_poll_many_bypasses_thread_pool_on_single_batch_and_emscripten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """poll_many runs sequentially without ThreadPoolExecutor on Emscripten or <=1 batch ID."""
    import concurrent.futures
    import contextvars
    import sys

    ctx_var: contextvars.ContextVar[str] = contextvars.ContextVar("poll_ctx", default="unset")
    seen_ctx: dict[str, str] = {}

    def failing_executor(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", failing_executor)

    def get_job(study_key: str, batch_id: str) -> JobStatus:
        seen_ctx[batch_id] = ctx_var.get()
        return JobStatus(
            batchId=batch_id,
            state="COMPLETED",
            progress=100,
            resultUrl=f"https://example.test/res/{batch_id}",
        )

    poller = JobPoller(get_job, fetch_result=lambda url: {"url": url})
    token = ctx_var.set("trace-123")
    try:
        single_summary = poller.poll_many("ST", ["B1"], interval=0)
        assert single_summary.all_successful is True
        assert single_summary.results["B1"].results == {"url": "https://example.test/res/B1"}
        assert seen_ctx["B1"] == "trace-123"

        monkeypatch.setattr(sys, "platform", "emscripten")
        multi_summary = poller.poll_many("ST", ["B2", "B3"], interval=0)
        assert multi_summary.all_successful is True
        assert set(multi_summary.results) == {"B2", "B3"}
        assert seen_ctx["B2"] == "trace-123"
        assert seen_ctx["B3"] == "trace-123"
    finally:
        ctx_var.reset(token)


def test_poll_many_emscripten_fail_fast_stops_subsequent_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sequential Emscripten poll_many aborts remaining jobs when fail_fast=True."""
    import sys

    monkeypatch.setattr(sys, "platform", "emscripten")
    polled_batches: list[str] = []

    def get_job(study_key: str, batch_id: str) -> JobStatus:
        polled_batches.append(batch_id)
        if batch_id == "B_FAIL":
            return JobStatus(batchId=batch_id, state="FAILED", progress=100)
        return JobStatus(batchId=batch_id, state="COMPLETED", progress=100)

    poller = JobPoller(get_job)
    with pytest.raises(JobFailedError):
        poller.poll_many("ST", ["B_FAIL", "B_NEVER"], interval=0, fail_fast=True)

    assert polled_batches == ["B_FAIL"]


def test_poll_many_cpython_multi_batch_preserves_thread_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standard CPython poll_many continues using ThreadPoolExecutor when len(batch_ids) > 1."""
    import concurrent.futures
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    real_tpe = concurrent.futures.ThreadPoolExecutor
    tpe_calls = 0

    def spy_tpe(*args: object, **kwargs: object) -> concurrent.futures.ThreadPoolExecutor:
        nonlocal tpe_calls
        tpe_calls += 1
        return real_tpe(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", spy_tpe)
    poller = JobPoller(lambda _sk, bid: JobStatus(batchId=bid, state="COMPLETED", progress=100))
    summary = poller.poll_many("ST", ["B1", "B2"], interval=0)
    assert summary.all_successful is True
    assert tpe_calls == 1
