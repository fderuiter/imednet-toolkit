"""Unit tests for core SDK batch coordination, chunked submission, and multi-job polling."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from imednet import AsyncImednetSDK, ImednetSDK
from imednet.models.jobs import Job, JobStatus
from imednet.utils.batch_coordinator import (
    DEFAULT_BATCH_CHUNK_SIZE,
    AsyncBatchCoordinator,
    BatchCoordinator,
    chunk_records,
)
from imednet.utils.job_poller import JobFailedError, JobPollSummary


def test_chunk_records_splits_and_validates_chunk_size() -> None:
    """chunk_records splits lists at chunk_size and rejects non-positive sizes."""
    assert chunk_records([], chunk_size=100) == []
    records = [{"id": i} for i in range(250)]
    chunks = chunk_records(records)
    assert DEFAULT_BATCH_CHUNK_SIZE == 100
    assert [len(c) for c in chunks] == [100, 100, 50]

    with pytest.raises(ValueError, match="chunk_size must be a positive integer"):
        chunk_records(records, chunk_size=0)
    with pytest.raises(ValueError, match="chunk_size must be a positive integer"):
        BatchCoordinator(chunk_size=0)
    with pytest.raises(ValueError, match="chunk_size must be a positive integer"):
        AsyncBatchCoordinator(chunk_size=-1)


def test_batch_coordinator_sync_single_and_multi_chunk() -> None:
    """BatchCoordinator coordinates single and multi-chunk submissions and polling."""
    submitted_chunks: list[list[dict[str, Any]]] = []

    def submit_chunk(chunk: list[dict[str, Any]]) -> Job:
        idx = len(submitted_chunks) + 1
        submitted_chunks.append(chunk)
        return Job(jobId=f"j{idx}", batchId=f"b{idx}", state="PROCESSING")

    poll_single = MagicMock(
        side_effect=lambda bid: JobStatus(jobId="j1", batchId=bid, state="COMPLETED")
    )
    coordinator = BatchCoordinator(chunk_size=2)

    # Empty batch
    empty_summary = coordinator.submit("STUDY", [], submit_chunk)
    assert empty_summary.jobs == []
    assert empty_summary.total_records == 0
    assert empty_summary.chunk_count == 0
    assert len(empty_summary) == 0
    assert empty_summary.last_job is None

    # Single chunk with wait_for_completion=True uses poll_single
    single_summary = coordinator.submit(
        "STUDY",
        [{"x": 1}],
        submit_chunk,
        wait_for_completion=True,
        poll_single=poll_single,
    )
    assert single_summary.chunk_count == 1
    assert single_summary.batch_ids == ["b1"]
    assert single_summary.all_successful is True
    assert single_summary.last_job is not None
    assert single_summary.last_job.state == "COMPLETED"
    poll_single.assert_called_once_with("b1")

    # Multi-chunk with poll_single fallback when poll_many is not supplied
    submitted_chunks.clear()
    multi_summary = coordinator.submit(
        "STUDY",
        [{"x": 1}, {"x": 2}, {"x": 3}, {"x": 4}, {"x": 5}],
        submit_chunk,
        wait_for_completion=True,
        poll_single=lambda bid: JobStatus(jobId=f"j-{bid}", batchId=bid, state="COMPLETED"),
    )
    assert [len(c) for c in submitted_chunks] == [2, 2, 1]
    assert multi_summary.batch_ids == ["b1", "b2", "b3"]
    assert list(multi_summary) == multi_summary.jobs
    assert multi_summary[0].batch_id == "b1"
    assert multi_summary.poll_summary is not None
    assert multi_summary.poll_summary.all_successful is True
    assert set(multi_summary.poll_summary.results) == {"b1", "b2", "b3"}


def test_batch_coordinator_missing_batch_id_and_poll_failures() -> None:
    """BatchCoordinator raises on missing batch_id and surfaces partial-failure jobs."""
    coordinator = BatchCoordinator(chunk_size=2)
    with pytest.raises(ValueError, match="no batch_id received"):
        coordinator.submit(
            "STUDY",
            [{"a": 1}],
            lambda _chunk: Job(jobId="j1", batchId="", state="PROCESSING"),
            wait_for_completion=True,
            require_batch_id=True,
        )

    def poll_many_partial(batch_ids: list[str]) -> JobPollSummary:
        return JobPollSummary(
            study_key="STUDY",
            results={
                bid: JobStatus(
                    batchId=bid,
                    state="COMPLETED",
                    failedRecords=1,
                )
                for bid in batch_ids
            },
            failures={},
            elapsed_total=0.0,
        )

    with pytest.raises(JobFailedError):
        coordinator.submit(
            "STUDY",
            [{"a": 1}, {"a": 2}, {"a": 3}],
            lambda chunk: Job(jobId="j", batchId=f"b-{len(chunk)}", state="PROCESSING"),
            wait_for_completion=True,
            poll_many=poll_many_partial,
        )


@pytest.mark.asyncio
async def test_async_batch_coordinator_single_and_multi_chunk() -> None:
    """AsyncBatchCoordinator coordinates async chunked submissions and multi-job polling."""
    submitted_chunks: list[list[dict[str, Any]]] = []

    async def submit_chunk(chunk: list[dict[str, Any]]) -> Job:
        idx = len(submitted_chunks) + 1
        submitted_chunks.append(chunk)
        return Job(jobId=f"aj{idx}", batchId=f"ab{idx}", state="PROCESSING")

    async def poll_single(batch_id: str) -> JobStatus:
        return JobStatus(jobId=f"aj-{batch_id}", batchId=batch_id, state="COMPLETED")

    coordinator = AsyncBatchCoordinator(chunk_size=2)
    empty = await coordinator.submit("STUDY", [], submit_chunk)
    assert empty.jobs == []

    summary = await coordinator.submit(
        "STUDY",
        [{"x": i} for i in range(5)],
        submit_chunk,
        wait_for_completion=True,
        poll_single=poll_single,
    )
    assert [len(c) for c in submitted_chunks] == [2, 2, 1]
    assert summary.batch_ids == ["ab1", "ab2", "ab3"]
    assert summary.poll_summary is not None
    assert summary.poll_summary.all_successful is True


def test_sdk_create_records_batch_and_poll_jobs_with_transport() -> None:
    """ImednetSDK.create_records_batch chunks at 100 Records and poll_jobs fetches resultUrl."""
    post_payloads: list[list[dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/variables"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "STUDY",
                            "formId": 1,
                            "formKey": "F1",
                            "formName": "Form 1",
                            "variableId": 1,
                            "variableName": "age",
                            "variableType": "integer",
                            "sequence": 1,
                            "revision": 1,
                            "disabled": False,
                            "deleted": False,
                            "blinded": False,
                            "variableOid": "V1",
                            "label": "Age",
                        }
                    ]
                },
            )
        if request.method == "POST" and path.endswith("/records"):
            body = json.loads(request.content.decode("utf-8"))
            post_payloads.append(body)
            batch_id = f"BATCH-{len(post_payloads)}"
            return httpx.Response(
                200,
                json={"jobId": f"JOB-{len(post_payloads)}", "batchId": batch_id, "state": "QUEUED"},
            )
        if request.method == "GET" and "/jobs/BATCH-" in path and not path.endswith("/results"):
            batch_id = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "jobId": f"J-{batch_id}",
                    "batchId": batch_id,
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": f"https://mock.imednet.test/api/v1/edc/studies/STUDY/jobs/{batch_id}/results",
                },
            )
        if request.method == "GET" and path.endswith("/results"):
            batch_id = path.split("/")[-2]
            return httpx.Response(
                200,
                json=[{"batchId": batch_id, "status": "SUCCESS"}],
            )
        return httpx.Response(404, json={"error": "Not found"})

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://mock.imednet.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        records = [{"formKey": "F1", "data": {"age": i}} for i in range(250)]
        jobs = sdk.create_records_batch("STUDY", records)
        assert len(jobs) == 3
        assert [len(chunk) for chunk in post_payloads] == [100, 100, 50]

        poll_summary = sdk.poll_jobs("STUDY", [j.batch_id for j in jobs], interval=0)
        assert poll_summary.all_successful is True
        for job in jobs:
            assert poll_summary.results[job.batch_id].results == [
                {"batchId": job.batch_id, "status": "SUCCESS"}
            ]
    finally:
        sdk.close()


@pytest.mark.asyncio
async def test_async_sdk_create_records_batch_and_async_poll_jobs() -> None:
    """AsyncImednetSDK.async_create_records_batch chunks at 100 and async_poll_jobs fetches results."""
    post_counts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/records"):
            body = json.loads(request.content.decode("utf-8"))
            post_counts.append(len(body))
            batch_id = f"ABATCH-{len(post_counts)}"
            return httpx.Response(
                200,
                json={"jobId": f"AJOB-{len(post_counts)}", "batchId": batch_id, "state": "QUEUED"},
            )
        if request.method == "GET" and "/jobs/ABATCH-" in path and not path.endswith("/results"):
            batch_id = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "jobId": f"AJ-{batch_id}",
                    "batchId": batch_id,
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": f"https://mock.imednet.test/api/v1/edc/studies/STUDY/jobs/{batch_id}/results",
                },
            )
        if request.method == "GET" and path.endswith("/results"):
            batch_id = path.split("/")[-2]
            return httpx.Response(200, json=[{"batchId": batch_id, "status": "SUCCESS"}])
        return httpx.Response(404)

    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://mock.imednet.test",
        transport=httpx.MockTransport(handler),
    ) as sdk:
        records = [{"formKey": "F1", "data": {"age": i}} for i in range(205)]
        jobs = await sdk.async_create_records_batch("STUDY", records, skip_validation=True)
        assert len(jobs) == 3
        assert post_counts == [100, 100, 5]

        summary = await sdk.async_poll_jobs("STUDY", [j.batch_id for j in jobs], interval=0)
        assert summary.all_successful is True
        assert summary.results["ABATCH-1"].results == [{"batchId": "ABATCH-1", "status": "SUCCESS"}]
