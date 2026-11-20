"""Convenience mixins for the iMedNet SDK.

These mixins contain high-level helper methods that delegate to specific endpoints.
They are architecturally linked to the core execution engine.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    ParamSpec,
    Protocol,
    Self,
    TypeVar,
    overload,
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
from imednet.utils.batch_coordinator import (
    DEFAULT_BATCH_CHUNK_SIZE,
    AsyncBatchCoordinator,
    BatchCoordinator,
    BatchSubmissionSummary,
)
from imednet.utils.job_poller import JobPollSummary, JobProgressHandler
from imednet.utils.tabular import normalize_records_input
from imednet.utils.typing import FilterValue, RecordsInput

try:
    from opentelemetry import trace as _trace

    tracer: Any = _trace.get_tracer(__name__)
except Exception:
    tracer = None


P = ParamSpec("P")
R = TypeVar("R")


def _trace_method(func: Callable[P, R]) -> Callable[P, R]:
    """Decorator to trace synchronous method execution with OpenTelemetry."""
    from functools import wraps

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        """Wrapper to start a span around the function call."""
        if tracer:
            with tracer.start_as_current_span(func.__name__):
                return func(*args, **kwargs)
        return func(*args, **kwargs)

    return wrapper


def _async_trace_method(
    func: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Decorator to trace asynchronous method execution with OpenTelemetry."""
    from functools import wraps

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
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
R_co = TypeVar("R_co", covariant=True)


class _SyncListMethod(Protocol[T]):
    """Typed callable protocol for synchronous convenience list methods."""

    @overload
    def __call__(self, study_key: str | None = None, **filters: FilterValue) -> list[T]: ...

    @overload
    def __call__(self, **filters: FilterValue) -> list[T]: ...


class _AsyncListMethod(Protocol[T]):
    """Typed callable protocol for asynchronous convenience list methods."""

    @overload
    async def __call__(self, study_key: str | None = None, **filters: FilterValue) -> list[T]: ...

    @overload
    async def __call__(self, **filters: FilterValue) -> list[T]: ...


_ENDPOINT_MODELS: dict[str, type[Any]] = {
    "codings": Coding,
    "forms": Form,
    "intervals": Interval,
    "queries": Query,
    "record_revisions": RecordRevision,
    "records": Record,
    "sites": Site,
    "studies": Study,
    "subjects": Subject,
    "users": User,
    "variables": Variable,
    "visits": Visit,
}


def _configure_list_convenience_callable(
    wrapper: Callable[..., Any],
    template: Callable[..., Any],
    *,
    name: str,
    qualname: str,
    doc: str,
    model_cls: Any,
    bound: bool,
) -> Callable[..., Any]:
    """Attach wrapper metadata, resolved annotations, and signature to a convenience callable."""
    import functools
    import inspect

    functools.update_wrapper(wrapper, template)
    if not bound:
        wrapper.__dict__.pop("__wrapped__", None)

    return_tp = list[model_cls] if isinstance(model_cls, type) else list[Any]
    annotations: dict[str, Any] = {
        "study_key": str | None,
        "filters": FilterValue,
        "return": return_tp,
    }
    sig = inspect.signature(wrapper)
    updated_params = [
        param.replace(annotation=inspect.Parameter.empty)
        if param.name == "self"
        else param.replace(annotation=annotations[param.name])
        if param.name in annotations
        else param
        for param in sig.parameters.values()
    ]
    annotations.pop("self", None)
    wrapper.__module__ = __name__
    wrapper.__name__ = name
    wrapper.__qualname__ = qualname
    wrapper.__doc__ = doc
    wrapper.__annotations__ = annotations
    wrapper.__signature__ = sig.replace(  # type: ignore[attr-defined]
        parameters=updated_params,
        return_annotation=return_tp,
    )
    return wrapper


class _ListOperation(Generic[T, R_co]):
    """Descriptor that provides convenience list methods on the SDK."""

    def __init__(self, endpoint_name: str, name: str, is_async: bool = False) -> None:
        """Initialize the list operation descriptor."""
        import functools
        from typing import cast

        self.endpoint_name = endpoint_name
        self.name = name
        self.is_async = is_async
        self._model_cls: Any = _ENDPOINT_MODELS.get(endpoint_name, Any)
        doc = f"Asynchronously list {endpoint_name}." if is_async else f"List {endpoint_name}."
        owner_name = "AsyncSDKConvenienceMixin" if is_async else "SyncSDKConvenienceMixin"

        op = self
        if is_async:

            async def _template_async(
                self: Any, study_key: str | None = None, **filters: FilterValue
            ) -> list[Any]:
                return await op._invoke_async(self, study_key, filters)

            template_fn: Callable[..., Any] = _template_async
        else:

            def _template_sync(
                self: Any, study_key: str | None = None, **filters: FilterValue
            ) -> list[Any]:
                return op._invoke_sync(self, study_key, filters)

            template_fn = _template_sync

        self._unbound = _configure_list_convenience_callable(
            template_fn,
            template_fn,
            name=name,
            qualname=f"{owner_name}.{name}",
            doc=doc,
            model_cls=self._model_cls,
            bound=False,
        )
        functools.update_wrapper(cast(Any, self), self._unbound)

    def _invoke_sync(
        self, instance: Any, study_key: str | None, filters: dict[str, Any]
    ) -> list[T]:
        endpoint = getattr(instance, self.endpoint_name)
        _filters = dict(filters)
        if self.endpoint_name == "studies":
            if study_key is not None:
                _filters["study_key"] = study_key
            return list(endpoint.list(**_filters))
        return list(endpoint.list(study_key, **_filters))

    async def _invoke_async(
        self, instance: Any, study_key: str | None, filters: dict[str, Any]
    ) -> list[T]:
        endpoint = getattr(instance, self.endpoint_name)
        _filters = dict(filters)
        if self.endpoint_name == "studies":
            if study_key is not None:
                _filters["study_key"] = study_key
            res = endpoint.list(**_filters)
        else:
            res = endpoint.list(study_key, **_filters)
        items: list[T] = [item async for item in res] if hasattr(res, "__aiter__") else await res
        return items

    if TYPE_CHECKING:

        @overload
        def __call__(self, study_key: str | None = None, **filters: FilterValue) -> R_co: ...

        @overload
        def __call__(self, **filters: FilterValue) -> R_co: ...

        def __call__(self, *args: Any, **kwargs: Any) -> Any: ...
    else:

        def __call__(
            self, instance: Any, study_key: str | None = None, **filters: FilterValue
        ) -> Any:
            """Execute the convenience list operation against an SDK instance."""
            return self.__get__(instance, type(instance))(study_key, **filters)

    @overload
    def __get__(self, instance: None, owner: Any) -> Self: ...

    @overload
    def __get__(self, instance: SyncSDKConvenienceMixin, owner: Any) -> _SyncListMethod[T]: ...

    @overload
    def __get__(self, instance: AsyncSDKConvenienceMixin, owner: Any) -> _AsyncListMethod[T]: ...

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        """Return a wrapped list method bound to the SDK instance or unbound for class inspection."""
        if instance is None:
            if owner is None:
                return self
            return self._unbound

        owner_cls_name = type(instance).__name__
        doc = self._unbound.__doc__ or ""
        if self.is_async:

            @_async_trace_method
            async def async_wrapper(
                study_key: str | None = None, **filters: FilterValue
            ) -> list[T]:
                """Execute the asynchronous list operation and return a list of items."""
                return await self._invoke_async(instance, study_key, dict(filters))

            return _configure_list_convenience_callable(
                async_wrapper,
                self._unbound,
                name=self.name,
                qualname=f"{owner_cls_name}.{self.name}",
                doc=doc,
                model_cls=self._model_cls,
                bound=True,
            )

        @_trace_method
        def sync_wrapper(study_key: str | None = None, **filters: FilterValue) -> list[T]:
            """Execute the synchronous list operation and return a list of items."""
            return self._invoke_sync(instance, study_key, dict(filters))

        return _configure_list_convenience_callable(
            sync_wrapper,
            self._unbound,
            name=self.name,
            qualname=f"{owner_cls_name}.{self.name}",
            doc=doc,
            model_cls=self._model_cls,
            bound=True,
        )


class SyncSDKConvenienceMixin:
    """Synchronous SDK convenience methods."""

    if TYPE_CHECKING:
        records: RecordsEndpoint
        jobs: JobsEndpoint
        _client: Any

    get_codings = _ListOperation[Coding, list[Coding]]("codings", "get_codings")
    get_forms = _ListOperation[Form, list[Form]]("forms", "get_forms")
    get_intervals = _ListOperation[Interval, list[Interval]]("intervals", "get_intervals")
    get_queries = _ListOperation[Query, list[Query]]("queries", "get_queries")
    get_record_revisions = _ListOperation[RecordRevision, list[RecordRevision]](
        "record_revisions", "get_record_revisions"
    )
    get_records = _ListOperation[Record, list[Record]]("records", "get_records")
    get_sites = _ListOperation[Site, list[Site]]("sites", "get_sites")
    get_studies = _ListOperation[Study, list[Study]]("studies", "get_studies")
    get_subjects = _ListOperation[Subject, list[Subject]]("subjects", "get_subjects")
    get_users = _ListOperation[User, list[User]]("users", "get_users")
    get_variables = _ListOperation[Variable, list[Variable]]("variables", "get_variables")
    get_visits = _ListOperation[Visit, list[Visit]]("visits", "get_visits")

    @_trace_method
    def get_job(self, study_key: str, batch_id: str) -> JobStatus:
        """Get job status."""
        return self.jobs.get(study_key, batch_id)

    @_trace_method
    def create_record(
        self,
        study_key: str,
        records_data: RecordsInput,
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
        skip_validation: bool = False,
    ) -> Job:
        """Create records in the specified study."""
        normalized = normalize_records_input(records_data, schema=schema)
        if skip_validation:
            return self.records.create(
                study_key,
                normalized,
                email_notify=email_notify,
                schema=schema,
                skip_validation=True,
            )
        return self.records.create(
            study_key,
            normalized,
            email_notify=email_notify,
            schema=schema,
        )

    @_trace_method
    def create_records_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
        chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        skip_validation: bool = False,
        wait_for_completion: bool = False,
        timeout: float = 300.0,
        interval: float = 5.0,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> BatchSubmissionSummary:
        """Validate once and submit records in bounded chunks (default 100 per POST)."""
        normalized = normalize_records_input(records_data, schema=schema)
        coordinator = BatchCoordinator(chunk_size=chunk_size)
        return coordinator.submit(
            study_key,
            normalized,
            lambda chunk: self.records.create(
                study_key,
                chunk,
                email_notify=email_notify,
                schema=schema,
                skip_validation=True,
            ),
            schema=schema,
            skip_validation=skip_validation,
            wait_for_completion=wait_for_completion,
            poll_many=lambda batch_ids, *, on_progress=None: self.poll_jobs(
                study_key,
                batch_ids,
                interval=interval,
                timeout=timeout,
                on_progress=on_progress,
            ),
            on_progress=on_progress,
            require_batch_id=wait_for_completion,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )

    @_trace_method
    def poll_job(
        self,
        study_key: str,
        batch_id: str,
        *,
        interval: float = 5,
        timeout: float = 300,
        on_progress: JobProgressHandler | None = None,
    ) -> JobStatus:
        """Poll a job until it reaches a terminal state."""
        return self.jobs.poll(
            study_key, batch_id, interval=interval, timeout=timeout, on_progress=on_progress
        )

    @_trace_method
    def poll_jobs(
        self,
        study_key: str,
        batch_ids: list[str],
        *,
        interval: float = 5.0,
        timeout: float = 300.0,
        on_progress: JobProgressHandler | None = None,
        fail_fast: bool = False,
    ) -> JobPollSummary:
        """Poll multiple jobs concurrently and fetch terminal resultUrl payloads."""
        return self.jobs.poll_many(
            study_key,
            batch_ids,
            interval=interval,
            timeout=timeout,
            on_progress=on_progress,
            fail_fast=fail_fast,
        )


class AsyncSDKConvenienceMixin:
    """Asynchronous SDK convenience methods."""

    if TYPE_CHECKING:
        records: AsyncRecordsEndpoint
        jobs: AsyncJobsEndpoint
        _async_client: Any

    async_get_codings = _ListOperation[Coding, Coroutine[Any, Any, list[Coding]]](
        "codings", "async_get_codings", is_async=True
    )
    async_get_forms = _ListOperation[Form, Coroutine[Any, Any, list[Form]]](
        "forms", "async_get_forms", is_async=True
    )
    async_get_intervals = _ListOperation[Interval, Coroutine[Any, Any, list[Interval]]](
        "intervals", "async_get_intervals", is_async=True
    )
    async_get_queries = _ListOperation[Query, Coroutine[Any, Any, list[Query]]](
        "queries", "async_get_queries", is_async=True
    )
    async_get_record_revisions = _ListOperation[
        RecordRevision, Coroutine[Any, Any, list[RecordRevision]]
    ]("record_revisions", "async_get_record_revisions", is_async=True)
    async_get_records = _ListOperation[Record, Coroutine[Any, Any, list[Record]]](
        "records", "async_get_records", is_async=True
    )
    async_get_sites = _ListOperation[Site, Coroutine[Any, Any, list[Site]]](
        "sites", "async_get_sites", is_async=True
    )
    async_get_studies = _ListOperation[Study, Coroutine[Any, Any, list[Study]]](
        "studies", "async_get_studies", is_async=True
    )
    async_get_subjects = _ListOperation[Subject, Coroutine[Any, Any, list[Subject]]](
        "subjects", "async_get_subjects", is_async=True
    )
    async_get_users = _ListOperation[User, Coroutine[Any, Any, list[User]]](
        "users", "async_get_users", is_async=True
    )
    async_get_variables = _ListOperation[Variable, Coroutine[Any, Any, list[Variable]]](
        "variables", "async_get_variables", is_async=True
    )
    async_get_visits = _ListOperation[Visit, Coroutine[Any, Any, list[Visit]]](
        "visits", "async_get_visits", is_async=True
    )

    @_async_trace_method
    async def async_get_job(self, study_key: str, batch_id: str) -> JobStatus:
        """Asynchronously get job status."""
        return await self.jobs.get(study_key, batch_id)

    @_async_trace_method
    async def async_create_record(
        self,
        study_key: str,
        records_data: RecordsInput,
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
        skip_validation: bool = False,
    ) -> Job:
        """Asynchronously create records in the specified study."""
        normalized = normalize_records_input(records_data, schema=schema)
        if skip_validation:
            return await self.records.create(
                study_key,
                normalized,
                email_notify=email_notify,
                schema=schema,
                skip_validation=True,
            )
        return await self.records.create(
            study_key,
            normalized,
            email_notify=email_notify,
            schema=schema,
        )

    @_async_trace_method
    async def async_create_records_batch(
        self,
        study_key: str,
        records_data: RecordsInput,
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
        chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
        skip_validation: bool = False,
        wait_for_completion: bool = False,
        timeout: float = 300.0,
        interval: float = 5.0,
        on_progress: JobProgressHandler | None = None,
        dry_run: bool = False,
        collect_errors: bool = False,
    ) -> BatchSubmissionSummary:
        """Asynchronously validate once and submit records in bounded chunks."""
        normalized = normalize_records_input(records_data, schema=schema)
        coordinator = AsyncBatchCoordinator(chunk_size=chunk_size)
        return await coordinator.submit(
            study_key,
            normalized,
            lambda chunk: self.records.create(
                study_key,
                chunk,
                email_notify=email_notify,
                schema=schema,
                skip_validation=True,
            ),
            schema=schema,
            skip_validation=skip_validation,
            wait_for_completion=wait_for_completion,
            poll_many=lambda batch_ids, *, on_progress=None: self.async_poll_jobs(
                study_key,
                batch_ids,
                interval=interval,
                timeout=timeout,
                on_progress=on_progress,
            ),
            on_progress=on_progress,
            require_batch_id=wait_for_completion,
            dry_run=dry_run,
            collect_errors=collect_errors,
        )

    @_async_trace_method
    async def async_poll_job(
        self,
        study_key: str,
        batch_id: str,
        *,
        interval: float = 5,
        timeout: float = 300,
        on_progress: JobProgressHandler | None = None,
    ) -> JobStatus:
        """Asynchronously poll a job until it reaches a terminal state."""
        return await self.jobs.poll(
            study_key, batch_id, interval=interval, timeout=timeout, on_progress=on_progress
        )

    @_async_trace_method
    async def async_poll_jobs(
        self,
        study_key: str,
        batch_ids: list[str],
        *,
        interval: float = 5.0,
        timeout: float = 300.0,
        on_progress: JobProgressHandler | None = None,
        fail_fast: bool = False,
    ) -> JobPollSummary:
        """Asynchronously poll multiple jobs concurrently and fetch resultUrl payloads."""
        return await self.jobs.poll_many(
            study_key,
            batch_ids,
            interval=interval,
            timeout=timeout,
            on_progress=on_progress,
            fail_fast=fail_fast,
        )


SDKConvenienceMixin = SyncSDKConvenienceMixin
