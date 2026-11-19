"""Convenience mixins for the iMedNet SDK.

These mixins contain high-level helper methods that delegate to specific endpoints.
They are architecturally linked to the core execution engine.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    TypeVar,
)

from imednet.models.codings import Coding
from imednet.models.forms import Form
from imednet.models.intervals import Interval
from imednet.models.jobs import Job, JobStatus
from imednet.models.queries import Query
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.studies import Study
from imednet.models.subjects import Subject
from imednet.models.users import User
from imednet.models.variables import Variable
from imednet.models.visits import Visit
from imednet.utils.job_poller import JobProgressHandler
from imednet.utils.typing import FilterValue, JsonDict

try:
    from opentelemetry import trace as _trace

    tracer: Any = _trace.get_tracer(__name__)
except Exception:
    tracer = None


def _trace_method(func: Any) -> Any:
    """Decorator to trace synchronous method execution with OpenTelemetry."""
    from functools import wraps

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        """Wrapper to start a span around the function call."""
        if tracer:
            with tracer.start_as_current_span(func.__name__):
                return func(*args, **kwargs)
        return func(*args, **kwargs)

    return wrapper


def _async_trace_method(func: Any) -> Any:
    """Decorator to trace asynchronous method execution with OpenTelemetry."""
    from functools import wraps

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        """Wrapper to start a span around the async function call."""
        if tracer:
            with tracer.start_as_current_span(func.__name__):
                return await func(*args, **kwargs)
        return await func(*args, **kwargs)

    return wrapper


if TYPE_CHECKING:
    from imednet.endpoints.jobs import AsyncJobsEndpoint, JobsEndpoint
    from imednet.endpoints.records import AsyncRecordsEndpoint, RecordsEndpoint
    from imednet.validation.cache import ValidationSchemaProtocol


T = TypeVar("T")


class _ListOperation(Generic[T]):
    """Descriptor that provides convenience list methods on the SDK."""

    def __init__(self, endpoint_name: str, name: str, is_async: bool = False):
        """Initialize the list operation descriptor."""
        self.endpoint_name = endpoint_name
        self.name = name
        self.is_async = is_async

    def __get__(self, instance: Any, owner: Any) -> Any:
        """Return a wrapped list method bound to the SDK instance."""
        if instance is None:
            return self
        endpoint = getattr(instance, self.endpoint_name)

        if self.is_async:

            @_async_trace_method
            async def wrapper(study_key: str | None = None, **filters: FilterValue) -> list[T]:
                """Execute the asynchronous list operation and return a list of items."""
                _filters: dict[str, Any] = dict(filters)
                if self.endpoint_name == "studies":
                    res = endpoint.list(**_filters)
                else:
                    res = endpoint.list(study_key, **_filters)
                return [item async for item in res] if hasattr(res, "__aiter__") else await res

            wrapper.__doc__ = f"Asynchronously list {self.endpoint_name}."
        else:

            @_trace_method
            def wrapper(study_key: str | None = None, **filters: FilterValue) -> list[T]:
                """Execute the synchronous list operation and return a list of items."""
                _filters: dict[str, Any] = dict(filters)
                if self.endpoint_name == "studies":
                    return list(endpoint.list(**_filters))
                return list(endpoint.list(study_key, **_filters))

            wrapper.__doc__ = f"List {self.endpoint_name}."

        wrapper.__name__ = self.name
        return wrapper


class SyncSDKConvenienceMixin:
    """Synchronous SDK convenience methods."""

    if TYPE_CHECKING:
        records: RecordsEndpoint
        jobs: JobsEndpoint
        _client: Any

    get_codings = _ListOperation[Coding]("codings", "get_codings")
    get_forms = _ListOperation[Form]("forms", "get_forms")
    get_intervals = _ListOperation[Interval]("intervals", "get_intervals")
    get_queries = _ListOperation[Query]("queries", "get_queries")
    get_record_revisions = _ListOperation[RecordRevision](
        "record_revisions", "get_record_revisions"
    )
    get_records = _ListOperation[Record]("records", "get_records")
    get_sites = _ListOperation[Site]("sites", "get_sites")
    get_studies = _ListOperation[Study]("studies", "get_studies")
    get_subjects = _ListOperation[Subject]("subjects", "get_subjects")
    get_users = _ListOperation[User]("users", "get_users")
    get_variables = _ListOperation[Variable]("variables", "get_variables")
    get_visits = _ListOperation[Visit]("visits", "get_visits")

    @_trace_method
    def get_job(self, study_key: str, batch_id: str) -> JobStatus:
        """Get job status."""
        return self.jobs.get(study_key, batch_id)

    @_trace_method
    def create_record(
        self,
        study_key: str,
        records_data: list[JsonDict],
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Create records in the specified study."""
        return self.records.create(
            study_key, records_data, email_notify=email_notify, schema=schema
        )

    @_trace_method
    def poll_job(
        self,
        study_key: str,
        batch_id: str,
        *,
        interval: int = 5,
        timeout: int = 300,
        on_progress: JobProgressHandler | None = None,
    ) -> JobStatus:
        """Poll a job until it reaches a terminal state."""
        return self.jobs.poll(
            study_key, batch_id, interval=interval, timeout=timeout, on_progress=on_progress
        )


class AsyncSDKConvenienceMixin:
    """Asynchronous SDK convenience methods."""

    if TYPE_CHECKING:
        records: AsyncRecordsEndpoint
        jobs: AsyncJobsEndpoint
        _async_client: Any

    async_get_codings = _ListOperation[Coding]("codings", "async_get_codings", is_async=True)
    async_get_forms = _ListOperation[Form]("forms", "async_get_forms", is_async=True)
    async_get_intervals = _ListOperation[Interval](
        "intervals", "async_get_intervals", is_async=True
    )
    async_get_queries = _ListOperation[Query]("queries", "async_get_queries", is_async=True)
    async_get_record_revisions = _ListOperation[RecordRevision](
        "record_revisions", "async_get_record_revisions", is_async=True
    )
    async_get_records = _ListOperation[Record]("records", "async_get_records", is_async=True)
    async_get_sites = _ListOperation[Site]("sites", "async_get_sites", is_async=True)
    async_get_studies = _ListOperation[Study]("studies", "async_get_studies", is_async=True)
    async_get_subjects = _ListOperation[Subject]("subjects", "async_get_subjects", is_async=True)
    async_get_users = _ListOperation[User]("users", "async_get_users", is_async=True)
    async_get_variables = _ListOperation[Variable](
        "variables", "async_get_variables", is_async=True
    )
    async_get_visits = _ListOperation[Visit]("visits", "async_get_visits", is_async=True)

    @_async_trace_method
    async def async_get_job(self, study_key: str, batch_id: str) -> JobStatus:
        """Asynchronously get job status."""
        return await self.jobs.get(study_key, batch_id)

    @_async_trace_method
    async def async_create_record(
        self,
        study_key: str,
        records_data: list[JsonDict],
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
    ) -> Job:
        """Asynchronously create records in the specified study."""
        return await self.records.create(
            study_key, records_data, email_notify=email_notify, schema=schema
        )

    @_async_trace_method
    async def async_poll_job(
        self,
        study_key: str,
        batch_id: str,
        *,
        interval: int = 5,
        timeout: int = 300,
        on_progress: JobProgressHandler | None = None,
    ) -> JobStatus:
        """Asynchronously poll a job until it reaches a terminal state."""
        return await self.jobs.poll(
            study_key, batch_id, interval=interval, timeout=timeout, on_progress=on_progress
        )


SDKConvenienceMixin = SyncSDKConvenienceMixin
