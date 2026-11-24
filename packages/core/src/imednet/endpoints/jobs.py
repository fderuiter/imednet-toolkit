"""Endpoint for checking job status in a study."""

from typing import Any

from imednet.core.endpoint.dispatch import execute_operation
from imednet.core.endpoint.edc_mixin import EdcAsyncGetEndpoint, EdcSyncGetEndpoint
from imednet.core.endpoint.operations.get import PathGetOperation
from imednet.models.jobs import JobStatus
from imednet.utils.job_poller import JobPollSummary, JobProgressHandler
from imednet.utils.typing import ItemId
from imednet.utils.url import validate_path_segment


class JobsOperationDef:
    """Definition for Job operations."""

    PATH = "jobs"
    MODEL = JobStatus

    @execute_operation  # type: ignore
    def get(self, study_key: str | None, item_id: ItemId) -> PathGetOperation[JobStatus]:
        """Get a single Job by ID."""
        self._require_item_id(item_id)  # type: ignore
        self._validate_study_key(study_key)  # type: ignore
        validate_path_segment(item_id, name="batch_id")
        path = self._get_endpoint_path(study_key, item_id)  # type: ignore
        return PathGetOperation[JobStatus](
            path=path,
            parse_func=self._parse_item,  # type: ignore
            not_found_func=lambda: self._raise_not_found(study_key, item_id),  # type: ignore
        )


class JobsEndpoint(JobsOperationDef, EdcSyncGetEndpoint[JobStatus]):  # type: ignore[misc]
    """Synchronous endpoint for managing Jobs."""

    def _fetch_result_url(self, url: str) -> Any:
        """Fetch a job resultUrl via hardened external HTTPS helper or relative EDC path."""
        client = self._require_sync_client()
        if url.startswith("/") and not url.startswith("//"):
            return client.get(url)
        fetch_external = getattr(client, "fetch_external_url", None)
        if callable(fetch_external):
            return fetch_external(url)
        return client.get(url)

    def fetch_result(self, result_url: str) -> Any:
        """Fetch a terminal Job ``resultUrl`` payload via the hardened endpoint helper."""
        return self._fetch_result_url(result_url)

    def poll(
        self,
        study_key: str,
        batch_id: str,
        *,
        interval: float = 5.0,
        timeout: float = 300.0,
        on_progress: JobProgressHandler | None = None,
        resolve_results: bool = False,
    ) -> JobStatus:
        """Wait for a terminal Job, optionally inspecting its ``resultUrl`` when ``resolve_results`` is True."""
        from imednet.utils.job_poller import JobPoller

        self._validate_study_key(study_key)
        validate_path_segment(batch_id, name="batch_id")
        poller = JobPoller(self.get, fetch_result=self.fetch_result if resolve_results else None)
        if on_progress is None:
            return poller.run(study_key, batch_id, interval, timeout)
        return poller.run(study_key, batch_id, interval, timeout, on_progress=on_progress)

    def poll_many(
        self,
        study_key: str,
        batch_ids: list[str],
        *,
        interval: float = 5.0,
        timeout: float = 300.0,
        on_progress: JobProgressHandler | None = None,
        fail_fast: bool = False,
        resolve_results: bool = False,
    ) -> JobPollSummary:
        """Collect terminal statuses and per-job failures, optionally resolving ``resultUrl`` payloads."""
        from imednet.utils.job_poller import JobPoller

        self._validate_study_key(study_key)
        for batch_id in batch_ids:
            validate_path_segment(batch_id, name="batch_id")
        return JobPoller(
            self.get,
            fetch_result=self.fetch_result if resolve_results else None,
        ).poll_many(
            study_key, batch_ids, interval, timeout, on_progress=on_progress, fail_fast=fail_fast
        )


class AsyncJobsEndpoint(JobsOperationDef, EdcAsyncGetEndpoint[JobStatus]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Jobs."""

    async def _fetch_result_url(self, url: str) -> Any:
        """Asynchronously fetch a job resultUrl via hardened external HTTPS helper."""
        client = self._require_async_client()
        if url.startswith("/") and not url.startswith("//"):
            return await client.get(url)
        fetch_external = getattr(client, "fetch_external_url", None)
        if callable(fetch_external):
            return await fetch_external(url)  # type: ignore[misc]
        return await client.get(url)

    async def fetch_result(self, result_url: str) -> Any:
        """Asynchronously fetch a terminal Job ``resultUrl`` payload via the hardened helper."""
        return await self._fetch_result_url(result_url)

    async def poll(
        self,
        study_key: str,
        batch_id: str,
        *,
        interval: float = 5.0,
        timeout: float = 300.0,
        on_progress: JobProgressHandler | None = None,
        resolve_results: bool = False,
    ) -> JobStatus:
        """Wait asynchronously for a terminal Job, optionally inspecting ``resultUrl`` payloads."""
        from imednet.utils.job_poller import AsyncJobPoller

        self._validate_study_key(study_key)
        validate_path_segment(batch_id, name="batch_id")
        poller = AsyncJobPoller(
            self.get,
            fetch_result=self.fetch_result if resolve_results else None,
        )
        if on_progress is None:
            return await poller.run(study_key, batch_id, interval, timeout)
        return await poller.run(study_key, batch_id, interval, timeout, on_progress=on_progress)

    async def poll_many(
        self,
        study_key: str,
        batch_ids: list[str],
        *,
        interval: float = 5.0,
        timeout: float = 300.0,
        on_progress: JobProgressHandler | None = None,
        fail_fast: bool = False,
        resolve_results: bool = False,
    ) -> JobPollSummary:
        """Collect async terminal statuses and failures, optionally resolving ``resultUrl`` payloads."""
        from imednet.utils.job_poller import AsyncJobPoller

        self._validate_study_key(study_key)
        for batch_id in batch_ids:
            validate_path_segment(batch_id, name="batch_id")
        return await AsyncJobPoller(
            self.get,
            fetch_result=self.fetch_result if resolve_results else None,
        ).async_poll_many(
            study_key, batch_ids, interval, timeout, on_progress=on_progress, fail_fast=fail_fast
        )
