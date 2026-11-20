"""Endpoint for checking job status in a study."""

from typing import Any

from imednet.core.endpoint.dispatch import execute_operation
from imednet.core.endpoint.edc_mixin import EdcAsyncGetEndpoint, EdcSyncGetEndpoint
from imednet.core.endpoint.operations.get import PathGetOperation
from imednet.models.jobs import JobStatus
from imednet.utils.job_poller import JobPollSummary, JobProgressHandler
from imednet.utils.typing import ItemId


class JobsOperationDef:
    """Definition for Job operations."""

    PATH = "jobs"
    MODEL = JobStatus

    @execute_operation  # type: ignore
    def get(self, study_key: str | None, item_id: ItemId) -> PathGetOperation[JobStatus]:
        """Get a single Job by ID."""
        self._require_item_id(item_id)  # type: ignore
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

    def poll(
        self,
        study_key: str,
        batch_id: str,
        *,
        interval: float = 5.0,
        timeout: float = 300.0,
        on_progress: JobProgressHandler | None = None,
    ) -> JobStatus:
        """Wait for a terminal Job using the shared polling implementation."""
        from imednet.utils.job_poller import JobPoller

        poller = JobPoller(self.get, fetch_result=self._fetch_result_url)
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
    ) -> JobPollSummary:
        """Collect terminal statuses and per-job failures through the shared poller."""
        from imednet.utils.job_poller import JobPoller

        return JobPoller(self.get, fetch_result=self._fetch_result_url).poll_many(
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

    async def poll(
        self,
        study_key: str,
        batch_id: str,
        *,
        interval: float = 5.0,
        timeout: float = 300.0,
        on_progress: JobProgressHandler | None = None,
    ) -> JobStatus:
        """Wait asynchronously using the shared polling implementation."""
        from imednet.utils.job_poller import AsyncJobPoller

        poller = AsyncJobPoller(self.get, fetch_result=self._fetch_result_url)
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
    ) -> JobPollSummary:
        """Collect async terminal statuses and per-job failures through the shared poller."""
        from imednet.utils.job_poller import AsyncJobPoller

        return await AsyncJobPoller(self.get, fetch_result=self._fetch_result_url).async_poll_many(
            study_key, batch_ids, interval, timeout, on_progress=on_progress, fail_fast=fail_fast
        )
