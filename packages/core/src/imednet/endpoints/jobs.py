"""Endpoint for checking job status in a study."""

from imednet.core.endpoint.dispatch import execute_operation
from imednet.core.endpoint.edc_mixin import EdcAsyncListGetEndpoint, EdcSyncListGetEndpoint
from imednet.core.endpoint.operations.get import PathGetOperation
from imednet.core.paginator import AsyncJsonListPaginator, JsonListPaginator
from imednet.models.jobs import JobStatus
from imednet.utils.job_poller import JobPollSummary, JobProgressHandler
from imednet.utils.typing import ItemId


class JobsOperationDef:
    """Definition for Job operations."""

    PATH = "jobs"
    MODEL = JobStatus
    PAGINATOR_CLS = JsonListPaginator
    ASYNC_PAGINATOR_CLS = AsyncJsonListPaginator

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


class JobsEndpoint(JobsOperationDef, EdcSyncListGetEndpoint[JobStatus]):  # type: ignore[misc]
    """Synchronous endpoint for managing Jobs."""

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

        poller = JobPoller(self.get, fetch_result=self._require_sync_client().get)
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

        return JobPoller(self.get, fetch_result=self._require_sync_client().get).poll_many(
            study_key, batch_ids, interval, timeout, on_progress=on_progress, fail_fast=fail_fast
        )


class AsyncJobsEndpoint(JobsOperationDef, EdcAsyncListGetEndpoint[JobStatus]):  # type: ignore[misc]
    """Asynchronous endpoint for managing Jobs."""

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

        poller = AsyncJobPoller(self.get, fetch_result=self._require_async_client().get)
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

        return await AsyncJobPoller(
            self.get, fetch_result=self._require_async_client().get
        ).async_poll_many(
            study_key, batch_ids, interval, timeout, on_progress=on_progress, fail_fast=fail_fast
        )
