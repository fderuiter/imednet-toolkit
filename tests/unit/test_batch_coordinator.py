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
    """ImednetSDK.create_records_batch chunks at 100 Records and poll_jobs skips resultUrl."""
    post_payloads: list[list[dict[str, Any]]] = []
    results_fetched = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal results_fetched
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
                    "resultUrl": f"https://mock.imednet.test/api/v1/edc/studies/STUDY/jobs/{batch_id}/results",
                },
            )
        if request.method == "GET" and path.endswith("/results"):
            results_fetched = True
            return httpx.Response(500, json={"error": "resultUrl should not be polled"})
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
        assert results_fetched is False
        for job in jobs:
            assert poll_summary.results[job.batch_id].state == "COMPLETED"
            assert poll_summary.results[job.batch_id].results is None
    finally:
        sdk.close()


@pytest.mark.asyncio
async def test_async_sdk_create_records_batch_and_async_poll_jobs() -> None:
    """AsyncImednetSDK.async_create_records_batch chunks at 100 and skips resultUrl polling."""
    post_counts: list[int] = []
    results_fetched = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal results_fetched
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
                    "resultUrl": f"https://mock.imednet.test/api/v1/edc/studies/STUDY/jobs/{batch_id}/results",
                },
            )
        if request.method == "GET" and path.endswith("/results"):
            results_fetched = True
            return httpx.Response(500, json={"error": "resultUrl should not be polled"})
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
        assert results_fetched is False
        assert summary.results["ABATCH-1"].state == "COMPLETED"
        assert summary.results["ABATCH-1"].results is None


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
        with pytest.raises(ValidationError, match="integer") as val_exc:
            sdk.create_records_batch("STUDY", invalid_records, schema=dd)
        assert getattr(val_exc.value, "batch_summary", None) is not None
        assert val_exc.value.batch_summary.jobs == []
        assert val_exc.value.batch_summary.all_successful is False
        # Invalid chunk rejected before POST
        assert post_chunks == [100, 5]
    finally:
        sdk.close()


def test_batch_coordinator_attaches_partial_summary_on_post_and_poll_failures() -> None:
    """Sync BatchCoordinator attaches partial batch_summary on chunk POST and poll errors."""
    from imednet.errors import ApiError, ServerError
    from imednet.utils.job_poller import JobTimeoutError

    coordinator = BatchCoordinator(chunk_size=2)

    # 1. Chunk 1 commits B-1, Chunk 2 fails POST with ServerError (ApiError subclass)
    calls = 0

    def fail_second_post(chunk: list[dict[str, Any]]) -> Job:
        nonlocal calls
        calls += 1
        if calls == 1:
            return Job(jobId="J-1", batchId="B-1", state="QUEUED")
        raise ServerError("Upstream 502 on chunk 2", status_code=502)

    with pytest.raises(ApiError, match="Upstream 502") as post_exc:
        coordinator.submit(
            "STUDY",
            [{"a": 1}, {"a": 2}, {"a": 3}],
            fail_second_post,
            skipped_count=1,
        )
    partial_post = getattr(post_exc.value, "batch_summary", None)
    assert partial_post is not None
    assert partial_post.batch_ids == ["B-1"]
    assert partial_post.chunk_sizes == [2]
    assert partial_post.total_records == 3
    assert partial_post.skipped_count == 1
    assert partial_post.all_successful is False

    # 2. Single-chunk poll failure attaches committed job and terminal failed status
    failed_status = JobStatus(
        jobId="J-1",
        batchId="B-1",
        state="FAILED",
        recordTotalCount=2,
        failedRecords=2,
    )
    with pytest.raises(JobFailedError) as single_poll_exc:
        coordinator.submit(
            "STUDY",
            [{"a": 1}, {"a": 2}],
            lambda _chunk: Job(jobId="J-1", batchId="B-1", state="QUEUED"),
            wait_for_completion=True,
            poll_single=lambda _bid: (_ for _ in ()).throw(
                JobFailedError("B-1 failed", failed_status)
            ),
        )
    single_summary = getattr(single_poll_exc.value, "batch_summary", None)
    assert single_summary is not None
    assert single_summary.batch_ids == ["B-1"]
    assert single_summary.jobs[0].state == "FAILED"
    assert single_summary.all_successful is False

    # 3. Multi-chunk poll_single fallback retains completed siblings B-1 and B-3 when B-2 times out
    post_idx = 0

    def submit_three_chunks(_chunk: list[dict[str, Any]]) -> Job:
        nonlocal post_idx
        post_idx += 1
        return Job(jobId=f"J-{post_idx}", batchId=f"B-{post_idx}", state="QUEUED")

    def poll_sibling_failure(bid: str) -> JobStatus:
        if bid == "B-2":
            raise JobTimeoutError(f"Timed out waiting for {bid}")
        return JobStatus(
            jobId=f"J-{bid[-1]}",
            batchId=bid,
            state="COMPLETED",
            recordTotalCount=2,
            failedRecords=0,
        )

    with pytest.raises(JobTimeoutError, match="B-2") as multi_poll_exc:
        coordinator.submit(
            "STUDY",
            [{"a": i} for i in range(5)],
            submit_three_chunks,
            wait_for_completion=True,
            poll_single=poll_sibling_failure,
        )
    multi_summary = getattr(multi_poll_exc.value, "batch_summary", None)
    assert multi_summary is not None
    assert multi_summary.batch_ids == ["B-1", "B-2", "B-3"]
    assert multi_summary.poll_summary is not None
    assert set(multi_summary.poll_summary.results) == {"B-1", "B-3"}
    assert "B-2" in multi_summary.poll_summary.failures
    assert multi_summary.jobs[0].state == "COMPLETED"
    assert multi_summary.jobs[1].state == "QUEUED"
    assert multi_summary.jobs[2].state == "COMPLETED"
    assert multi_summary.all_successful is False


@pytest.mark.asyncio
async def test_async_batch_coordinator_and_poll_facade_retain_partial_summaries() -> None:
    """AsyncBatchCoordinator and poll_facade_many_sync/async retain completed sibling JobStatus items."""
    from imednet.errors import ApiError, ServerError
    from imednet.utils.batch_coordinator import poll_facade_many_async, poll_facade_many_sync

    coordinator = AsyncBatchCoordinator(chunk_size=2)
    calls = 0

    async def fail_second_async_post(_chunk: list[dict[str, Any]]) -> Job:
        nonlocal calls
        calls += 1
        if calls == 1:
            return Job(jobId="AJ-1", batchId="AB-1", state="QUEUED")
        raise ServerError("Async 503 on chunk 2", status_code=503)

    with pytest.raises(ApiError, match="Async 503") as async_post_exc:
        await coordinator.submit(
            "STUDY",
            [{"a": 1}, {"a": 2}, {"a": 3}],
            fail_second_async_post,
        )
    async_post_summary = getattr(async_post_exc.value, "batch_summary", None)
    assert async_post_summary is not None
    assert async_post_summary.batch_ids == ["AB-1"]
    assert async_post_summary.all_successful is False

    # Verify poll_facade_many_sync fallback retains B-1 and B-3 when B-2 raises JobFailedError
    class SyncFacadeStub:
        def poll_job(self, _study_key: str, batch_id: str, **_kwargs: Any) -> JobStatus:
            if batch_id == "B-2":
                raise JobFailedError(
                    "B-2 failed",
                    JobStatus(
                        jobId="J-2",
                        batchId="B-2",
                        state="FAILED",
                        recordTotalCount=2,
                        failedRecords=1,
                    ),
                )
            return JobStatus(
                jobId=f"J-{batch_id[-1]}",
                batchId=batch_id,
                state="COMPLETED",
                recordTotalCount=2,
                failedRecords=0,
            )

    sync_poll_summary = poll_facade_many_sync(
        SyncFacadeStub(),
        "STUDY",
        ["B-1", "B-2", "B-3"],
        timeout=5.0,
        poll_interval=0.0,
    )
    assert set(sync_poll_summary.results) == {"B-1", "B-3"}
    assert "B-2" in sync_poll_summary.failures

    # Verify poll_facade_many_async fallback retains AB-1 and AB-3 when AB-2 fails, and
    # AsyncBatchCoordinator attaches all three jobs onto JobFailedError.batch_summary
    class AsyncFacadeStub:
        async def async_poll_job(self, _study_key: str, batch_id: str, **_kwargs: Any) -> JobStatus:
            if batch_id == "AB-2":
                raise JobFailedError(
                    "AB-2 failed",
                    JobStatus(
                        jobId="AJ-2",
                        batchId="AB-2",
                        state="FAILED",
                        recordTotalCount=2,
                        failedRecords=2,
                    ),
                )
            return JobStatus(
                jobId=f"AJ-{batch_id[-1]}",
                batchId=batch_id,
                state="COMPLETED",
                recordTotalCount=2,
                failedRecords=0,
            )

    stub = AsyncFacadeStub()
    async_poll_summary = await poll_facade_many_async(
        stub,
        "STUDY",
        ["AB-1", "AB-2", "AB-3"],
        timeout=5.0,
        poll_interval=0.0,
    )
    assert set(async_poll_summary.results) == {"AB-1", "AB-3"}
    assert "AB-2" in async_poll_summary.failures

    chunk_counter = 0

    async def submit_three_async(_chunk: list[dict[str, Any]]) -> Job:
        nonlocal chunk_counter
        chunk_counter += 1
        return Job(jobId=f"AJ-{chunk_counter}", batchId=f"AB-{chunk_counter}", state="QUEUED")

    with pytest.raises(JobFailedError, match="AB-2") as async_poll_exc:
        await coordinator.submit(
            "STUDY",
            [{"a": i} for i in range(5)],
            submit_three_async,
            wait_for_completion=True,
            poll_many=lambda bids, on_progress=None: poll_facade_many_async(
                stub,
                "STUDY",
                bids,
                timeout=5.0,
                poll_interval=0.0,
                on_progress=on_progress,
            ),
        )
    attached = getattr(async_poll_exc.value, "batch_summary", None)
    assert attached is not None
    assert attached.batch_ids == ["AB-1", "AB-2", "AB-3"]
    assert [j.state for j in attached.jobs] == ["COMPLETED", "FAILED", "COMPLETED"]
    assert attached.poll_summary is not None
    assert set(attached.poll_summary.results) == {"AB-1", "AB-3"}
    assert attached.all_successful is False


@pytest.mark.asyncio
async def test_mid_batch_503_attaches_settled_jobs_and_masked_unsubmitted_chunks() -> None:
    """AC 1: HTTP 503 on chunk 2 settles chunk 1 when waiting and attaches sanitized unsubmitted chunks."""
    from imednet.errors import ServerError
    from imednet.utils.batch_coordinator import BatchSubmissionSummary

    poll_calls: list[str] = []
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "POST" and request.url.path.endswith("/records"):
            post_count += 1
            if post_count == 1:
                return httpx.Response(
                    200,
                    json={"jobId": "JOB-1", "batchId": "BATCH-1", "state": "PROCESSING"},
                )
            return httpx.Response(503, json={"message": "Service temporarily unavailable"})
        if request.method == "GET" and request.url.path.endswith("/jobs/BATCH-1"):
            poll_calls.append("BATCH-1")
            return httpx.Response(
                200,
                json={
                    "jobId": "JOB-1",
                    "batchId": "BATCH-1",
                    "state": "COMPLETED",
                    "progress": 100,
                    "results": [{"recordId": 1, "status": "SUCCESS", "ssn": "123-45-6789"}],
                },
            )
        return httpx.Response(404)

    records = [
        {"formKey": "F1", "subjectKey": "S1", "data": {"age": 30}},
        {
            "formKey": "F1",
            "subjectKey": "S2",
            "data": {
                "age": 31,
                "patient_name": "Jane Doe",
                "ssn": "999-00-1111",
                "api_key": "sk-live-secret",  # pragma: allowlist secret
            },
        },
        {"formKey": "F1", "subjectKey": "S3", "data": {"dob": "1985-04-12"}},
    ]

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://mock.imednet.test",
        retries=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ServerError) as exc_info:
            sdk.records.create_batch(
                "STUDY",
                records,
                chunk_size=1,
                wait_for_completion=True,
                interval=0,
                timeout=5,
            )
    finally:
        sdk.close()

    err = exc_info.value
    assert poll_calls == ["BATCH-1"]
    partial = getattr(err, "partial_summary", None)
    assert isinstance(partial, BatchSubmissionSummary)
    assert partial.all_successful is False
    assert partial.batch_ids == ["BATCH-1"]
    assert len(partial.jobs) == 1
    assert partial.jobs[0].state == "COMPLETED"
    assert partial.poll_summary is not None
    assert partial.poll_summary.results["BATCH-1"].state == "COMPLETED"
    assert partial.poll_summary.results["BATCH-1"].results[0]["ssn"] == "***MASKED***"

    assert len(partial.unsubmitted_chunks) == 2
    assert len(partial.unsubmitted_records) == 2
    chunk2_rec = partial.unsubmitted_chunks[0][0]
    assert chunk2_rec["subjectKey"] == "S2"
    assert chunk2_rec["data"]["patient_name"] == "***MASKED***"
    assert chunk2_rec["data"]["ssn"] == "***MASKED***"
    assert chunk2_rec["data"]["api_key"] == "***"
    assert partial.unsubmitted_chunks[1][0]["data"]["dob"] == "***MASKED***"
    # Caller input dictionary was not mutated in place
    assert records[1]["data"]["patient_name"] == "Jane Doe"
    assert records[1]["data"]["api_key"] == "sk-live-secret"  # pragma: allowlist secret

    # Async parity check on AsyncRecordsEndpoint.create_batch
    post_count = 0
    poll_calls.clear()

    class AsyncMockMidBatchTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return handler(request)

    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://mock.imednet.test",
        retries=0,
        transport=AsyncMockMidBatchTransport(),
    ) as async_sdk:
        with pytest.raises(ServerError) as async_exc_info:
            await async_sdk.records.create_batch(
                "STUDY",
                records,
                chunk_size=1,
                wait_for_completion=True,
                interval=0,
                timeout=5,
            )

    async_partial = getattr(async_exc_info.value, "partial_summary", None)
    assert isinstance(async_partial, BatchSubmissionSummary)
    assert async_partial.jobs[0].state == "COMPLETED"
    assert len(async_partial.unsubmitted_chunks) == 2
    assert async_partial.unsubmitted_records[0]["data"]["patient_name"] == "***MASKED***"


@pytest.mark.asyncio
async def test_sibling_job_batches_settle_before_raising_job_failed_error() -> None:
    """AC 4: Failing chunk Job settles all sibling batchIds and attaches them to JobFailedError."""
    from unittest.mock import AsyncMock, MagicMock

    from imednet.utils.batch_coordinator import poll_facade_many_async, poll_facade_many_sync
    from imednet.utils.job_poller import JobFailedError

    # 1. Test poll_facade_many_sync / poll_facade_many_async fallback loops directly
    polled_sync: list[str] = []

    def fake_poll_job(_study: str, bid: str, **_kwargs: Any) -> JobStatus:
        polled_sync.append(bid)
        if bid == "B1":
            failed = JobStatus(
                batchId="B1",
                jobId="J1",
                state="FAILED",
                results=[
                    {
                        "status": "FAILED",
                        "error": "Bad record token=tok-secret patient_name=Alice",
                        "ssn": "123-45-6789",
                    }
                ],
            )
            raise JobFailedError("Job B1 ended in state FAILED", failed)
        return JobStatus(batchId=bid, jobId=f"J-{bid}", state="COMPLETED", progress=100)

    sync_facade = MagicMock(spec=["poll_job"])
    sync_facade.poll_job.side_effect = fake_poll_job
    sync_many = poll_facade_many_sync(
        sync_facade, "STUDY", ["B1", "B2", "B3"], timeout=5, poll_interval=0
    )
    assert polled_sync == ["B1", "B2", "B3"]
    assert set(sync_many.terminal_statuses) == {"B1", "B2", "B3"}
    assert sync_many.terminal_statuses["B1"].state == "FAILED"
    assert sync_many.terminal_statuses["B2"].state == "COMPLETED"
    assert sync_many.terminal_statuses["B3"].state == "COMPLETED"

    polled_async: list[str] = []

    async def fake_async_poll_job(study: str, bid: str, **kwargs: Any) -> JobStatus:
        polled_async.append(bid)
        return fake_poll_job(study, bid, **kwargs)

    async_facade = MagicMock(spec=["async_poll_job"])
    async_facade.async_poll_job = AsyncMock(side_effect=fake_async_poll_job)
    async_many = await poll_facade_many_async(
        async_facade, "STUDY", ["B1", "B2", "B3"], timeout=5, poll_interval=0
    )
    assert polled_async == ["B1", "B2", "B3"]
    assert set(async_many.terminal_statuses) == {"B1", "B2", "B3"}

    # 2. Test BatchCoordinator & AsyncBatchCoordinator raise JobFailedError with all siblings settled
    coord = BatchCoordinator(chunk_size=1)
    idx_counter = 0

    def submit_chunk(_chunk: list[dict[str, Any]]) -> Job:
        nonlocal idx_counter
        idx_counter += 1
        return Job(jobId=f"J{idx_counter}", batchId=f"B{idx_counter}", state="PROCESSING")

    with pytest.raises(JobFailedError) as exc_info:
        coord.submit(
            "STUDY",
            [{"a": 1}, {"a": 2}, {"a": 3}],
            submit_chunk,
            wait_for_completion=True,
            poll_single=lambda bid: fake_poll_job("STUDY", bid),
        )

    jf_err = exc_info.value
    assert "tok-secret" not in str(jf_err)
    assert "Alice" not in str(jf_err)
    assert "123-45-6789" not in str(jf_err)
    assert jf_err.poll_summary is not None
    assert set(jf_err.poll_summary.terminal_statuses) == {"B1", "B2", "B3"}
    assert jf_err.poll_summary.results["B2"].state == "COMPLETED"
    assert jf_err.poll_summary.results["B3"].state == "COMPLETED"
    assert jf_err.partial_summary is not None
    assert [j.state for j in jf_err.partial_summary.jobs] == ["FAILED", "COMPLETED", "COMPLETED"]

    idx_counter = 0

    async def async_submit_chunk(chunk: list[dict[str, Any]]) -> Job:
        return submit_chunk(chunk)

    acoord = AsyncBatchCoordinator(chunk_size=1)
    with pytest.raises(JobFailedError) as async_exc_info:
        await acoord.submit(
            "STUDY",
            [{"a": 1}, {"a": 2}, {"a": 3}],
            async_submit_chunk,
            wait_for_completion=True,
            poll_single=lambda bid: fake_async_poll_job("STUDY", bid),
        )
    assert set(async_exc_info.value.poll_summary.terminal_statuses) == {"B1", "B2", "B3"}
    assert [j.state for j in async_exc_info.value.partial_summary.jobs] == [
        "FAILED",
        "COMPLETED",
        "COMPLETED",
    ]

    # 3. Even if custom poll_many raises during mid-batch settlement, the chunk error propagates intact
    chunk_idx = 0

    def fail_on_chunk_three(_chunk: list[dict[str, Any]]) -> Job:
        nonlocal chunk_idx
        chunk_idx += 1
        if chunk_idx == 3:
            raise RuntimeError("chunk 3 failed")
        return Job(jobId=f"JM{chunk_idx}", batchId=f"BM{chunk_idx}", state="PROCESSING")

    def broken_poll_many(_bids: list[str]) -> JobPollSummary:
        raise RuntimeError("poll_many transport error")

    with pytest.raises(RuntimeError, match="chunk 3 failed") as mid_exc_info:
        coord.submit(
            "STUDY",
            [{"a": 1}, {"a": 2}, {"a": 3}],
            fail_on_chunk_three,
            wait_for_completion=True,
            poll_many=broken_poll_many,
        )
    mid_partial = getattr(mid_exc_info.value, "partial_summary", None)
    assert mid_partial is not None
    assert mid_partial.batch_ids == ["BM1", "BM2"]
    assert mid_partial.unsubmitted_records == [{"a": 3}]
    assert mid_partial.poll_summary is not None
    assert "BM1" in mid_partial.poll_summary.failures


@pytest.mark.asyncio
async def test_sdk_convenience_batch_preserves_nested_spans_and_partial_mocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Convenience batch methods emit nested poll_jobs spans and preserve partial mock seams."""
    from contextlib import contextmanager
    from unittest.mock import AsyncMock

    import imednet.sdk_convenience as sdk_conv_mod

    span_events: list[tuple[str, tuple[str, ...]]] = []
    active_stack: list[str] = []

    class FakeTracer:
        @contextmanager
        def start_as_current_span(self, name: str) -> Any:
            active_stack.append(name)
            span_events.append((name, tuple(active_stack)))
            try:
                yield
            finally:
                active_stack.pop()

    monkeypatch.setattr(sdk_conv_mod, "tracer", FakeTracer())

    sdk = ImednetSDK(
        api_key="k",  # pragma: allowlist secret
        security_key="s",  # pragma: allowlist secret
        base_url="https://mock.imednet.test",
    )
    try:
        mock_create = MagicMock(
            return_value=Job(jobId="J-1", batchId="B-1", state="QUEUED"),
        )
        mock_poll_jobs = MagicMock(
            side_effect=lambda study_key, batch_ids, **_kw: JobPollSummary(
                study_key=study_key,
                results={
                    bid: JobStatus(jobId="J-1", batchId=bid, state="COMPLETED") for bid in batch_ids
                },
                failures={},
                elapsed_total=1.25,
            )
        )
        monkeypatch.setattr(sdk.records, "create", mock_create)
        monkeypatch.setattr(sdk, "poll_jobs", mock_poll_jobs)
        partial_summary = sdk.create_records_batch(
            "STUDY",
            [{"formKey": "F1", "data": {"age": 29}}],
            skip_validation=True,
            wait_for_completion=True,
        )
        assert partial_summary.poll_summary is not None
        assert partial_summary.poll_summary.elapsed_total == 1.25
        mock_create.assert_called_once()
        mock_poll_jobs.assert_called_once()
    finally:
        sdk.close()

    # Exercise tracedbound SDK methods directly with jobs.poll_many mocked
    span_events.clear()
    sdk2 = ImednetSDK(
        api_key="k",  # pragma: allowlist secret
        security_key="s",  # pragma: allowlist secret
        base_url="https://mock.imednet.test",
    )
    try:
        create_spy = MagicMock(return_value=Job(jobId="J-1", batchId="B-1", state="QUEUED"))
        poll_many_spy = MagicMock(
            return_value=JobPollSummary(
                study_key="STUDY",
                results={"B-1": JobStatus(jobId="J-1", batchId="B-1", state="COMPLETED")},
                failures={},
                elapsed_total=2.5,
            )
        )
        monkeypatch.setattr(sdk2.records, "create", create_spy)
        monkeypatch.setattr(sdk2.jobs, "poll_many", poll_many_spy)

        summary = sdk2.create_records_batch(
            "STUDY",
            [{"formKey": "F1", "data": {"age": 30}}],
            email_notify="ops@example.test",
            skip_validation=True,
            wait_for_completion=True,
            timeout=15.0,
            interval=0.5,
        )
        assert summary.poll_summary is not None
        assert summary.poll_summary.elapsed_total == 2.5
        assert summary.last_job is not None and summary.last_job.state == "COMPLETED"
        create_spy.assert_called_once_with(
            "STUDY",
            [{"formKey": "F1", "data": {"age": 30}}],
            email_notify="ops@example.test",
            schema=None,
            skip_validation=True,
        )
        poll_many_spy.assert_called_once_with(
            "STUDY",
            ["B-1"],
            interval=0.5,
            timeout=15.0,
            on_progress=None,
            fail_fast=False,
            resolve_results=True,
        )
        assert span_events == [
            ("create_records_batch", ("create_records_batch",)),
            ("poll_jobs", ("create_records_batch", "poll_jobs")),
        ]
    finally:
        sdk2.close()

    span_events.clear()
    async with AsyncImednetSDK(
        api_key="k",  # pragma: allowlist secret
        security_key="s",  # pragma: allowlist secret
        base_url="https://mock.imednet.test",
    ) as async_sdk:
        acreate_spy = AsyncMock(return_value=Job(jobId="AJ-1", batchId="AB-1", state="QUEUED"))
        apoll_many_spy = AsyncMock(
            return_value=JobPollSummary(
                study_key="STUDY",
                results={"AB-1": JobStatus(jobId="AJ-1", batchId="AB-1", state="COMPLETED")},
                failures={},
                elapsed_total=3.75,
            )
        )
        monkeypatch.setattr(async_sdk.records, "create", acreate_spy)
        monkeypatch.setattr(async_sdk.jobs, "poll_many", apoll_many_spy)

        asummary = await async_sdk.async_create_records_batch(
            "STUDY",
            [{"formKey": "F1", "data": {"age": 31}}],
            skip_validation=True,
            wait_for_completion=True,
            timeout=20.0,
            interval=1.0,
        )
        assert asummary.poll_summary is not None
        assert asummary.poll_summary.elapsed_total == 3.75
        acreate_spy.assert_awaited_once()
        apoll_many_spy.assert_awaited_once_with(
            "STUDY",
            ["AB-1"],
            interval=1.0,
            timeout=20.0,
            on_progress=None,
            fail_fast=False,
            resolve_results=True,
        )
        assert span_events == [
            ("async_create_records_batch", ("async_create_records_batch",)),
            ("async_poll_jobs", ("async_create_records_batch", "async_poll_jobs")),
        ]


@pytest.mark.asyncio
async def test_records_endpoint_create_batch_dispatches_poll_single_and_poll_many(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint create_batch uses JobsEndpoint.poll for 1 chunk and poll_many for >1 chunks."""
    from unittest.mock import AsyncMock

    from imednet.endpoints.jobs import AsyncJobsEndpoint, JobsEndpoint
    from imednet.errors.validation import PathTraversalValidationError

    sdk = ImednetSDK(
        api_key="k",  # pragma: allowlist secret
        security_key="s",  # pragma: allowlist secret
        base_url="https://mock.imednet.test",
    )
    try:
        seq = 0

        def _fake_sync_create(study_key: str, chunk: list[dict[str, Any]], **_kw: Any) -> Job:
            nonlocal seq
            seq += 1
            return Job(jobId=f"J-{seq}", batchId=f"B-{seq}", state="QUEUED")

        with pytest.raises(PathTraversalValidationError):
            sdk.records.create_batch("   ", [{"formKey": "F1", "data": {"age": 1}}])

        poll_single_spy = MagicMock(
            side_effect=lambda _sk, bid, **_kw: JobStatus(
                jobId=f"J-{bid}", batchId=bid, state="COMPLETED"
            )
        )
        poll_many_spy = MagicMock(
            side_effect=lambda sk, bids, **_kw: JobPollSummary(
                study_key=sk,
                results={
                    bid: JobStatus(jobId=f"J-{bid}", batchId=bid, state="COMPLETED") for bid in bids
                },
                failures={},
                elapsed_total=1.5,
            )
        )
        monkeypatch.setattr(sdk.records, "create", _fake_sync_create)
        monkeypatch.setattr(JobsEndpoint, "poll", poll_single_spy)
        monkeypatch.setattr(JobsEndpoint, "poll_many", poll_many_spy)

        single = sdk.records.create_batch(
            "STUDY",
            [{"formKey": "F1", "data": {"age": 1}}],
            chunk_size=2,
            skip_validation=True,
            wait_for_completion=True,
        )
        assert single.batch_ids == ["B-1"]
        assert poll_single_spy.call_count == 1
        assert poll_many_spy.call_count == 0

        multi = sdk.records.create_batch(
            "STUDY",
            [{"formKey": "F1", "data": {"age": i}} for i in range(3)],
            chunk_size=2,
            skip_validation=True,
            wait_for_completion=True,
        )
        assert multi.batch_ids == ["B-2", "B-3"]
        assert poll_single_spy.call_count == 1
        assert poll_many_spy.call_count == 1
    finally:
        sdk.close()

    async with AsyncImednetSDK(
        api_key="k",  # pragma: allowlist secret
        security_key="s",  # pragma: allowlist secret
        base_url="https://mock.imednet.test",
    ) as async_sdk:
        aseq = 0

        async def _fake_async_create(
            study_key: str, chunk: list[dict[str, Any]], **_kw: Any
        ) -> Job:
            nonlocal aseq
            aseq += 1
            return Job(jobId=f"AJ-{aseq}", batchId=f"AB-{aseq}", state="QUEUED")

        with pytest.raises(PathTraversalValidationError):
            await async_sdk.records.create_batch("   ", [{"formKey": "F1", "data": {"age": 1}}])

        apoll_single_spy = AsyncMock(
            side_effect=lambda _sk, bid, **_kw: JobStatus(
                jobId=f"AJ-{bid}", batchId=bid, state="COMPLETED"
            )
        )
        apoll_many_spy = AsyncMock(
            side_effect=lambda sk, bids, **_kw: JobPollSummary(
                study_key=sk,
                results={
                    bid: JobStatus(jobId=f"AJ-{bid}", batchId=bid, state="COMPLETED")
                    for bid in bids
                },
                failures={},
                elapsed_total=1.5,
            )
        )
        monkeypatch.setattr(async_sdk.records, "create", _fake_async_create)
        monkeypatch.setattr(AsyncJobsEndpoint, "poll", apoll_single_spy)
        monkeypatch.setattr(AsyncJobsEndpoint, "poll_many", apoll_many_spy)

        asingle = await async_sdk.records.create_batch(
            "STUDY",
            [{"formKey": "F1", "data": {"age": 1}}],
            chunk_size=2,
            skip_validation=True,
            wait_for_completion=True,
        )
        assert asingle.batch_ids == ["AB-1"]
        assert apoll_single_spy.await_count == 1
        assert apoll_many_spy.await_count == 0

        amulti = await async_sdk.records.create_batch(
            "STUDY",
            [{"formKey": "F1", "data": {"age": i}} for i in range(3)],
            chunk_size=2,
            skip_validation=True,
            wait_for_completion=True,
        )
        assert amulti.batch_ids == ["AB-2", "AB-3"]
        assert apoll_single_spy.await_count == 1
        assert apoll_many_spy.await_count == 1


@pytest.mark.asyncio
async def test_poll_facade_helpers_argument_preparation_and_fallback() -> None:
    """poll_facade_* helpers omit None on_progress and fall back sequentially when needed."""
    from unittest.mock import AsyncMock

    from imednet.utils.batch_coordinator import (
        poll_facade_many_async,
        poll_facade_many_sync,
        poll_facade_single_async,
        poll_facade_single_sync,
    )

    sync_facade = MagicMock(spec=["poll_job"])
    sync_facade.poll_job.side_effect = lambda _sk, bid, **_kw: JobStatus(
        jobId=f"J-{bid}", batchId=bid, state="COMPLETED"
    )
    single_res = poll_facade_single_sync(
        sync_facade, "STUDY", "B1", timeout=10, poll_interval=2, on_progress=None
    )
    assert single_res.batch_id == "B1"
    sync_facade.poll_job.assert_called_once_with("STUDY", "B1", timeout=10, interval=2)

    fallback_summary = poll_facade_many_sync(
        sync_facade, "STUDY", ["B1", "B2"], timeout=10, poll_interval=2
    )
    assert set(fallback_summary.results) == {"B1", "B2"}
    assert fallback_summary.elapsed_total == 0.0

    async_facade = MagicMock(spec=["async_poll_job"])
    async_facade.async_poll_job = AsyncMock(
        side_effect=lambda _sk, bid, **_kw: JobStatus(
            jobId=f"AJ-{bid}", batchId=bid, state="COMPLETED"
        )
    )
    asingle_res = await poll_facade_single_async(
        async_facade, "STUDY", "AB1", timeout=12, poll_interval=3, on_progress=None
    )
    assert asingle_res.batch_id == "AB1"
    async_facade.async_poll_job.assert_awaited_once_with("STUDY", "AB1", timeout=12, interval=3)

    afallback_summary = await poll_facade_many_async(
        async_facade, "STUDY", ["AB1", "AB2"], timeout=12, poll_interval=3
    )
    assert set(afallback_summary.results) == {"AB1", "AB2"}
    assert afallback_summary.elapsed_total == 0.0


@pytest.mark.asyncio
async def test_batch_coordinator_chunk_two_failure_attaches_committed_jobs_and_batch_ids() -> None:
    """AC 5 & Req 6: Failing on chunk 2 exposes committed chunk 1 Job and batch_id on raised error."""
    records = [
        {"formKey": "F1", "data": {"age": 1}},
        {"formKey": "F1", "data": {"age": 2}},
        {"formKey": "F1", "data": {"age": 3}},
    ]

    # Sync BatchCoordinator: chunk 1 succeeds (BATCH-1), chunk 2 raises RuntimeError
    sync_calls = 0

    def submit_sync(_chunk: list[dict[str, Any]]) -> Job:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            return Job(jobId="JOB-1", batchId="BATCH-1", state="PROCESSING")
        raise RuntimeError("Upstream 502 on chunk 2")

    sync_coord = BatchCoordinator(chunk_size=2)
    with pytest.raises(RuntimeError, match="Upstream 502 on chunk 2") as sync_exc:
        sync_coord.submit("STUDY", records, submit_sync, skip_validation=True)

    err_sync: Any = sync_exc.value
    assert len(err_sync.jobs) == 1
    assert err_sync.jobs[0].batch_id == "BATCH-1"
    assert err_sync.committed_jobs == err_sync.jobs
    assert err_sync.batch_ids == ["BATCH-1"]
    assert err_sync.committed_batch_ids == ["BATCH-1"]
    assert err_sync.batch_id == "BATCH-1"

    # AsyncBatchCoordinator: chunk 1 succeeds (ABATCH-1), chunk 2 raises RuntimeError
    async_calls = 0

    async def submit_async(_chunk: list[dict[str, Any]]) -> Job:
        nonlocal async_calls
        async_calls += 1
        if async_calls == 1:
            return Job(jobId="AJOB-1", batchId="ABATCH-1", state="PROCESSING")
        raise RuntimeError("Async upstream timeout on chunk 2")

    async_coord = AsyncBatchCoordinator(chunk_size=2)
    with pytest.raises(RuntimeError, match="Async upstream timeout on chunk 2") as async_exc:
        await async_coord.submit("STUDY", records, submit_async, skip_validation=True)

    err_async: Any = async_exc.value
    assert len(err_async.jobs) == 1
    assert err_async.jobs[0].batch_id == "ABATCH-1"
    assert err_async.committed_jobs == err_async.jobs
    assert err_async.batch_ids == ["ABATCH-1"]
    assert err_async.batch_id == "ABATCH-1"


@pytest.mark.asyncio
async def test_sdk_convenience_batch_validates_study_key_and_dry_run_data_dictionary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sdk.create_records_batch and async_create_records_batch enforce study_key and dry-run DataDictionary parity."""
    from unittest.mock import AsyncMock

    import httpx

    from imednet.core.async_client import AsyncClient
    from imednet.core.client import Client
    from imednet.errors.validation import ConfigurationError, PathTraversalValidationError
    from imednet.sdk_convenience import (
        _resolve_convenience_schema_adapter,
        _should_prefer_convenience_poll_many,
        _validate_convenience_study_key,
    )
    from imednet.simulation import SimulationTransport

    sim_transport = SimulationTransport()
    seen_methods: list[str] = []

    def tracking_sync_handler(req: httpx.Request) -> httpx.Response:
        seen_methods.append(req.method)
        return sim_transport.handle_request(req)

    async def tracking_async_handler(req: httpx.Request) -> httpx.Response:
        seen_methods.append(req.method)
        return await sim_transport.handle_async_request(req)

    sdk = ImednetSDK(
        client=Client(
            api_key="sim-k",  # pragma: allowlist secret
            security_key="sim-s",  # pragma: allowlist secret
            base_url="https://edc.sim.imednet.com",
            transport=httpx.MockTransport(tracking_sync_handler),
        )
    )
    async_sdk = AsyncImednetSDK(
        async_client=AsyncClient(
            api_key="sim-k",  # pragma: allowlist secret
            security_key="sim-s",  # pragma: allowlist secret
            base_url="https://edc.sim.imednet.com",
            transport=httpx.MockTransport(tracking_async_handler),
        )
    )
    invalid_records = [
        {"formKey": "NONEXISTENT_FORM", "subjectKey": "SUB1", "data": {"BAD_VAR": 123}},
        {"formKey": "F_DM", "subjectKey": "SUB1", "data": {"NONEXISTENT_VAR": "abc"}},
    ]
    try:
        # 1. Invalid study_key rejected before any HTTP call
        with pytest.raises(PathTraversalValidationError):
            sdk.create_records_batch("../secret", invalid_records, dry_run=True)
        with pytest.raises(ConfigurationError):
            sdk.create_records_batch(None, invalid_records, dry_run=True)  # type: ignore[arg-type]
        with pytest.raises(PathTraversalValidationError):
            await async_sdk.async_create_records_batch("../secret", invalid_records, dry_run=True)
        with pytest.raises(ConfigurationError):
            await async_sdk.async_create_records_batch(
                None,
                invalid_records,
                dry_run=True,  # type: ignore[arg-type]
            )
        assert seen_methods == []

        # 2. Dry-run without explicit schema loads Study DataDictionary and fails invalid Forms/Variables without POSTs
        sync_conv = sdk.create_records_batch("STUDY", invalid_records, dry_run=True)
        sync_ep = sdk.records.create_batch("STUDY", invalid_records, dry_run=True)
        assert sync_conv.dry_run is True
        assert sync_conv.all_successful is False
        assert sync_conv.validation_report is not None
        assert sync_conv.validation_report.is_valid is False
        assert sync_ep.validation_report is not None
        assert sync_conv.validation_report.error_count == sync_ep.validation_report.error_count
        assert "POST" not in seen_methods

        seen_methods.clear()
        async_conv = await async_sdk.async_create_records_batch(
            "STUDY", invalid_records, dry_run=True
        )
        async_ep = await async_sdk.records.create_batch("STUDY", invalid_records, dry_run=True)
        assert async_conv.dry_run is True
        assert async_conv.all_successful is False
        assert async_conv.validation_report is not None
        assert async_conv.validation_report.is_valid is False
        assert async_ep.validation_report is not None
        assert async_conv.validation_report.error_count == async_ep.validation_report.error_count
        assert "POST" not in seen_methods

        # 3. Single-job poll_job / async_poll_job mock seam works on 1-chunk convenience batches
        monkeypatch.setattr(
            sdk.records,
            "create",
            MagicMock(return_value=Job(jobId="J-SINGLE", batchId="B-SINGLE", state="QUEUED")),
        )
        mock_poll_job = MagicMock(
            return_value=JobStatus(jobId="J-SINGLE", batchId="B-SINGLE", state="COMPLETED")
        )
        monkeypatch.setattr(sdk, "poll_job", mock_poll_job)
        single_sync_res = sdk.create_records_batch(
            "STUDY",
            [{"formKey": "F_DM", "data": {"V_AGE": 42}}],
            skip_validation=True,
            wait_for_completion=True,
        )
        assert single_sync_res.all_successful is True
        mock_poll_job.assert_called_once()

        monkeypatch.setattr(
            async_sdk.records,
            "create",
            AsyncMock(return_value=Job(jobId="AJ-SINGLE", batchId="AB-SINGLE", state="QUEUED")),
        )
        mock_apoll_job = AsyncMock(
            return_value=JobStatus(jobId="AJ-SINGLE", batchId="AB-SINGLE", state="COMPLETED")
        )
        monkeypatch.setattr(async_sdk, "async_poll_job", mock_apoll_job)
        single_async_res = await async_sdk.async_create_records_batch(
            "STUDY",
            [{"formKey": "F_DM", "data": {"V_AGE": 42}}],
            skip_validation=True,
            wait_for_completion=True,
        )
        assert single_async_res.all_successful is True
        mock_apoll_job.assert_awaited_once()

        # 4. Safe attribute lookup never spawns child MagicMocks on unconfigured mock doubles
        bare_mock = MagicMock()
        assert _resolve_convenience_schema_adapter(bare_mock, is_async=False) is None
        assert _resolve_convenience_schema_adapter(bare_mock, is_async=True) is None
        assert _should_prefer_convenience_poll_many(bare_mock, is_async=False) is False
        assert _should_prefer_convenience_poll_many(bare_mock, is_async=True) is False
        _validate_convenience_study_key(bare_mock, "STUDY_OK")
        assert object.__getattribute__(bare_mock, "__dict__").get("_mock_children") == {}
    finally:
        sdk.close()
        await async_sdk.aclose()


@pytest.mark.asyncio
async def test_batch_coordinator_fail_fast_chunk_two_preserves_batch_one_completed_and_batch_two_failed() -> (
    None
):
    """When chunk 2 fails under fail_fast=True, partial_summary.jobs keeps BATCH-1 COMPLETED and BATCH-2 FAILED."""
    from imednet.utils.job_poller import AsyncJobPoller, JobPoller

    records = [{"formKey": "F1", "data": {"v": 1}}, {"formKey": "F1", "data": {"v": 2}}]

    # 1. Sync BatchCoordinator + JobPoller(fail_fast=True)
    sync_idx = 0

    def submit_sync(_chunk: list[dict[str, Any]]) -> Job:
        nonlocal sync_idx
        sync_idx += 1
        return Job(jobId=f"J-{sync_idx}", batchId=f"BATCH-{sync_idx}", state="QUEUED")

    def get_sync_job(_study_key: str, batch_id: str) -> JobStatus:
        if batch_id == "BATCH-1":
            return JobStatus(jobId="J-1", batchId="BATCH-1", state="COMPLETED", progress=100)
        return JobStatus(jobId="J-2", batchId="BATCH-2", state="FAILED", progress=100)

    sync_poller = JobPoller(get_sync_job)
    sync_coord = BatchCoordinator(chunk_size=1)
    with pytest.raises(JobFailedError) as sync_exc_info:
        sync_coord.submit(
            "STUDY",
            records,
            submit_sync,
            skip_validation=True,
            wait_for_completion=True,
            poll_single=lambda bid, **kw: sync_poller.run("STUDY", bid, interval=0, **kw),
            poll_many=lambda bids, **kw: sync_poller.poll_many(
                "STUDY", bids, interval=0, fail_fast=True, **kw
            ),
        )

    sync_partial = sync_exc_info.value.partial_summary
    assert sync_partial is not None
    assert [(j.batch_id, j.state) for j in sync_partial.jobs] == [
        ("BATCH-1", "COMPLETED"),
        ("BATCH-2", "FAILED"),
    ]
    assert sync_partial.poll_summary is not None
    assert "BATCH-2" in sync_partial.poll_summary.failures
    assert "BATCH-1" not in sync_partial.poll_summary.failures

    # 2. AsyncBatchCoordinator + AsyncJobPoller(fail_fast=True) (without poll_single to exercise attached poll_summary)
    async_idx = 0

    async def submit_async(_chunk: list[dict[str, Any]]) -> Job:
        nonlocal async_idx
        async_idx += 1
        return Job(jobId=f"AJ-{async_idx}", batchId=f"BATCH-{async_idx}", state="QUEUED")

    async def get_async_job(_study_key: str, batch_id: str) -> JobStatus:
        if batch_id == "BATCH-1":
            return JobStatus(jobId="AJ-1", batchId="BATCH-1", state="COMPLETED", progress=100)
        return JobStatus(jobId="AJ-2", batchId="BATCH-2", state="FAILED", progress=100)

    async_poller = AsyncJobPoller(get_async_job)
    async_coord = AsyncBatchCoordinator(chunk_size=1)
    with pytest.raises(JobFailedError) as async_exc_info:
        await async_coord.submit(
            "STUDY",
            records,
            submit_async,
            skip_validation=True,
            wait_for_completion=True,
            poll_many=lambda bids, **kw: async_poller.async_poll_many(
                "STUDY", bids, interval=0, fail_fast=True, **kw
            ),
        )

    async_partial = async_exc_info.value.partial_summary
    assert async_partial is not None
    assert [(j.batch_id, j.state) for j in async_partial.jobs] == [
        ("BATCH-1", "COMPLETED"),
        ("BATCH-2", "FAILED"),
    ]
    assert async_partial.poll_summary is not None
    assert "BATCH-2" in async_partial.poll_summary.failures
    assert "BATCH-1" not in async_partial.poll_summary.failures
