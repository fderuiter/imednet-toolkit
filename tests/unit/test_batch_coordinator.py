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


def test_sdk_create_records_batch_single_chunk_bypasses_thread_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-chunk create_records_batch(wait_for_completion=True) polls via poll_jobs without ThreadPoolExecutor."""
    import concurrent.futures
    import json
    from unittest.mock import patch

    import httpx

    def _forbid_threads(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", _forbid_threads)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/records"):
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"jobId": f"J-{len(payload)}", "batchId": "BATCH-SINGLE"},
            )
        if request.method == "GET" and request.url.path.endswith("/jobs/BATCH-SINGLE"):
            return httpx.Response(
                200,
                json={"batchId": "BATCH-SINGLE", "state": "COMPLETED", "progress": 100},
            )
        return httpx.Response(500, json={"error": f"Unexpected {request.url.path}"})

    sdk = ImednetSDK(
        api_key="k",  # pragma: allowlist secret
        security_key="s",  # pragma: allowlist secret
        base_url="https://mock.imednet.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with patch.object(sdk, "poll_jobs", wraps=sdk.poll_jobs) as spy_many:
            summary = sdk.create_records_batch(
                "STUDY",
                [{"formKey": "F1", "data": {"age": 30}}],
                skip_validation=True,
                wait_for_completion=True,
                interval=0.0,
            )
        assert summary.batch_ids == ["BATCH-SINGLE"]
        assert summary.last_job is not None and summary.last_job.state == "COMPLETED"
        assert spy_many.call_count == 1
    finally:
        sdk.close()


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
