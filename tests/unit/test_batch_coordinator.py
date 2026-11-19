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


@pytest.mark.asyncio
async def test_async_batch_coordinator_concurrent_tasks_sharing_cache_validate_own_chunks() -> None:
    """Overlapping submissions sharing an AsyncSchemaCache still validate their own chunks."""
    import asyncio
    from unittest.mock import patch

    from imednet.errors import ValidationError
    from imednet.models.variables import Variable
    from imednet.validation.cache import AsyncSchemaCache, validate_record_data

    shared_cache = AsyncSchemaCache()
    shared_cache.populate(
        [
            Variable(
                study_key="STUDY",
                form_id=1,
                form_key="F1",
                form_name="Form 1",
                variable_id=1,
                variable_name="age",
                variable_type="integer",
            )
        ]
    )

    task1_suspended = asyncio.Event()
    release_task1 = asyncio.Event()
    submitted_payloads: list[list[dict[str, Any]]] = []
    child_task_error: BaseException | None = None

    coordinator = AsyncBatchCoordinator(chunk_size=2)

    async def slow_submit_task1(chunk: list[dict[str, Any]]) -> Job:
        nonlocal child_task_error
        submitted_payloads.append(chunk)
        if not task1_suspended.is_set():
            # Even a child task spawned inside Task 1's active context must validate new records
            async def child_probe() -> Job:
                return await coordinator.submit(
                    "STUDY",
                    [{"formKey": "F1", "data": {"age": "child-invalid"}}],
                    fast_submit,
                    schema=shared_cache,
                )  # type: ignore[return-value]

            try:
                await asyncio.create_task(child_probe())
            except ValidationError as exc:
                child_task_error = exc
            task1_suspended.set()
            await release_task1.wait()
        return Job(jobId="j1", batchId="b1", state="COMPLETED")

    async def fast_submit(chunk: list[dict[str, Any]]) -> Job:
        submitted_payloads.append(chunk)
        return Job(jobId="j2", batchId="b2", state="COMPLETED")

    valid_records_task1 = [
        {"formKey": "F1", "data": {"age": 21}},
        {"formKey": "F1", "data": {"age": 22}},
        {"formKey": "F1", "data": {"age": 23}},
    ]

    with patch(
        "imednet.validation.cache.validate_record_data",
        wraps=validate_record_data,
    ) as spy_validate:
        task1 = asyncio.create_task(
            coordinator.submit(
                "STUDY",
                valid_records_task1,
                slow_submit_task1,
                schema=shared_cache,
            )
        )
        await task1_suspended.wait()
        assert isinstance(child_task_error, ValidationError)

        # Overlapping Task 2 with an invalid item MUST NOT bypass validation while Task 1 awaits
        with pytest.raises(ValidationError, match="Value must be an integer"):
            await coordinator.submit(
                "STUDY",
                [{"formKey": "F1", "data": {"age": "invalid-string"}}],
                fast_submit,
                schema=shared_cache,
            )

        # Overlapping Task 3 with a valid item validates its own chunk once and succeeds
        task3_summary = await coordinator.submit(
            "STUDY",
            [{"formKey": "F1", "data": {"age": 45}}],
            fast_submit,
            schema=shared_cache,
        )
        assert task3_summary.total_records == 1

        release_task1.set()
        task1_summary = await task1
        assert task1_summary.total_records == 3
        assert task1_summary.chunk_sizes == [2, 1]

    # Invalid payloads were never passed to submit_chunk
    flattened = [rec["data"]["age"] for chunk in submitted_payloads for rec in chunk]
    assert flattened == [21, 22, 45, 23]
    # 3 valid in Task 1 + 1 child invalid + 1 Task 2 invalid + 1 Task 3 valid = 6 validations total
    assert spy_validate.call_count == 6


@pytest.mark.asyncio
async def test_prevalidated_without_records_does_not_leak_to_spawned_child_task() -> None:
    """Wildcard prevalidated() scopes remain bound to the owning task and never leak to child tasks."""
    import asyncio

    from imednet.core.endpoint.operations.record_create import RecordCreateOperation
    from imednet.errors import ValidationError
    from imednet.models.variables import Variable
    from imednet.validation.cache import AsyncSchemaCache

    cache = AsyncSchemaCache()
    cache.populate(
        [
            Variable(
                study_key="STUDY",
                form_id=1,
                form_key="F1",
                form_name="Form 1",
                variable_id=1,
                variable_name="age",
                variable_type="integer",
            )
        ]
    )

    shared_record: dict[str, Any] = {"formKey": "F1", "data": {"age": "invalid-in-child"}}

    async def child_attempt() -> None:
        assert cache.is_prevalidated is False
        assert cache.is_record_prevalidated(shared_record) is False
        RecordCreateOperation(
            path="/api/v1/edc/studies/STUDY/records",
            records_data=[shared_record],
            schema=cache,
        )

    with cache.prevalidated([shared_record]):
        assert cache.is_prevalidated is True
        assert cache.is_record_prevalidated(shared_record) is True
        with pytest.raises(ValidationError, match="Value must be an integer"):
            await asyncio.create_task(child_attempt())

    with cache.prevalidated():
        assert cache.is_prevalidated is True
        with pytest.raises(ValidationError, match="Value must be an integer"):
            await asyncio.create_task(child_attempt())
    assert cache.is_prevalidated is False


def test_sdk_create_records_batch_with_data_dictionary_prevalidates_once() -> None:
    """create_records_batch accepts DataDictionary and prevalidates each record once across chunks."""
    from unittest.mock import patch

    from imednet.datadict import DataDictionary, Form, Variable
    from imednet.errors import ValidationError
    from imednet.validation.cache import validate_record_data

    dd = DataDictionary(
        forms=[
            Form(
                formKey="F1",
                formName="Form 1",
                variables=[Variable(variableName="age", variableType="Integer", required="Yes")],
            )
        ]
    )

    post_chunks: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/records"):
            payload = json.loads(request.content.decode("utf-8"))
            post_chunks.append(len(payload))
            return httpx.Response(
                200,
                json={"jobId": f"J-{len(post_chunks)}", "batchId": f"B-{len(post_chunks)}"},
            )
        return httpx.Response(500, json={"error": f"Unexpected request {request.url.path}"})

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://mock.imednet.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        valid_records = [{"formKey": "F1", "data": {"age": i}} for i in range(105)]
        with patch(
            "imednet.validation.cache.validate_record_data",
            wraps=validate_record_data,
        ) as spy_dd_validate:
            jobs = sdk.create_records_batch("STUDY", valid_records, schema=dd)
        assert len(jobs) == 2
        assert post_chunks == [100, 5]
        # Validated once by BatchCoordinator; RecordCreateOperation skips re-validation via prevalidated_records
        assert spy_dd_validate.call_count == 105

        invalid_records = [
            {"formKey": "F1", "data": {"age": 20}},
            {"formKey": "F1", "data": {"age": "bad"}},
        ]
        with pytest.raises(ValidationError, match="integer"):
            sdk.create_records_batch("STUDY", invalid_records, schema=dd)
        # Invalid chunk rejected before POST
        assert post_chunks == [100, 5]
    finally:
        sdk.close()
