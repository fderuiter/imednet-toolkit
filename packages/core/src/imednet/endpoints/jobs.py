"""Endpoint for checking job status in a study."""

from typing import Any
from urllib.parse import urlsplit

import httpx

from imednet.core.endpoint.dispatch import execute_operation
from imednet.core.endpoint.edc_mixin import EdcAsyncGetEndpoint, EdcSyncGetEndpoint
from imednet.core.endpoint.operations.get import PathGetOperation
from imednet.core.http_client_base import (
    _MAX_EXTERNAL_REDIRECTS,
    ExternalUrlError,
    _url_origin,
)
from imednet.models.jobs import JobStatus
from imednet.utils.job_poller import JobPollSummary, JobProgressHandler
from imednet.utils.typing import ItemId
from imednet.utils.url import validate_path_segment


def _is_same_origin_result_url(client: Any, url: str) -> bool:
    """Return True when *url* is a relative EDC path or matches the configured base_url origin."""
    if not isinstance(url, str):
        return False
    stripped = url.strip()
    if not stripped or any(ord(ch) <= 32 or ord(ch) == 127 for ch in stripped):
        return False
    if stripped.startswith("/") and not stripped.startswith("//"):
        return True
    try:
        parts = urlsplit(stripped)
    except ValueError:
        return False
    if (
        parts.scheme.lower() not in {"http", "https"}
        or parts.username is not None
        or parts.password is not None
        or not parts.hostname
        or "%" in parts.netloc
    ):
        return False
    base_url = getattr(client, "_base_url", None) or getattr(client, "base_url", None)
    if not isinstance(base_url, (httpx.URL, str)) or not str(base_url).strip():
        return False
    try:
        return _url_origin(stripped) == _url_origin(base_url)
    except Exception:
        return False


def _resolve_result_redirect_target(
    client: Any, current_url: str, response: Any, hop: int
) -> str | None:
    """Extract and resolve a 3xx redirect Location header from a same-origin resultUrl response."""
    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int) or not (300 <= status_code < 400) or status_code == 304:
        return None
    if hop >= _MAX_EXTERNAL_REDIRECTS:
        raise ExternalUrlError("Maximum redirects exceeded for resultUrl")
    headers = getattr(response, "headers", None)
    get_header = getattr(headers, "get", None)
    location = get_header("Location") if callable(get_header) else None
    if not isinstance(location, str) or not location.strip():
        raise ExternalUrlError(f"Redirect response {status_code} missing Location header")
    base_url = getattr(client, "_base_url", None) or getattr(client, "base_url", None)
    try:
        if current_url.startswith("/") and isinstance(base_url, (httpx.URL, str)):
            anchor = httpx.URL(base_url).join(current_url)
        else:
            anchor = httpx.URL(current_url)
        return str(anchor.join(location.strip()))
    except httpx.InvalidURL as exc:
        raise ExternalUrlError(f"Malformed redirect Location header: {location!r}") from exc


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
        """Fetch a job resultUrl via same-origin client.get or hardened external HTTPS helper."""
        client = self._require_sync_client()
        current_url = url
        for hop in range(_MAX_EXTERNAL_REDIRECTS + 1):
            if _is_same_origin_result_url(client, current_url):
                target = current_url.strip()
                try:
                    response = client.get(target)
                except httpx.InvalidURL as exc:
                    raise ExternalUrlError(f"Malformed resultUrl: {current_url!r}") from exc
                next_url = _resolve_result_redirect_target(client, target, response, hop)
                if next_url is None:
                    return response
                current_url = next_url
                continue
            fetch_external = getattr(client, "fetch_external_url", None)
            if callable(fetch_external):
                return fetch_external(current_url)
            try:
                return client.get(current_url)
            except httpx.InvalidURL as exc:
                raise ExternalUrlError(f"Malformed resultUrl: {current_url!r}") from exc
        raise ExternalUrlError("Maximum redirects exceeded for resultUrl")

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
        """Asynchronously fetch a job resultUrl via same-origin client.get or external HTTPS helper."""
        client = self._require_async_client()
        current_url = url
        for hop in range(_MAX_EXTERNAL_REDIRECTS + 1):
            if _is_same_origin_result_url(client, current_url):
                target = current_url.strip()
                try:
                    response = await client.get(target)
                except httpx.InvalidURL as exc:
                    raise ExternalUrlError(f"Malformed resultUrl: {current_url!r}") from exc
                next_url = _resolve_result_redirect_target(client, target, response, hop)
                if next_url is None:
                    return response
                current_url = next_url
                continue
            fetch_external = getattr(client, "fetch_external_url", None)
            if callable(fetch_external):
                return await fetch_external(current_url)  # type: ignore[misc]
            try:
                return await client.get(current_url)
            except httpx.InvalidURL as exc:
                raise ExternalUrlError(f"Malformed resultUrl: {current_url!r}") from exc
        raise ExternalUrlError("Maximum redirects exceeded for resultUrl")

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
