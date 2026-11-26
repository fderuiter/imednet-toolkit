"""Async-first execution bridge and sync-to-async facade adapter.

Provides runtime helpers that allow synchronous wrappers on :class:`ImednetSDK`
to delegate to canonical ``async`` engines safely across standard scripts,
Jupyter notebooks, FastAPI/asyncio services, and WebAssembly Pyodide runtimes.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import sys
import threading
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Any, TypeVar, cast
from unittest.mock import NonCallableMock

from imednet.utils.typing import FilterValue, RecordsInput

if TYPE_CHECKING:
    from imednet.auth.strategy import AuthStrategy
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
    from imednet.spi.facade import ImednetFacade
    from imednet.utils.batch_coordinator import BatchSubmissionSummary
    from imednet.utils.job_poller import JobPollSummary, JobProgressHandler
    from imednet.validation.cache import ValidationSchemaProtocol

T = TypeVar("T")

_CANONICAL_FACADE_METHODS: tuple[str, ...] = (
    "get_forms",
    "get_records",
    "get_studies",
    "create_record",
    "get_variables",
    "get_sites",
    "get_subjects",
    "get_intervals",
    "get_queries",
    "get_codings",
    "get_users",
    "get_visits",
    "get_record_revisions",
    "get_job",
    "poll_job",
    "poll_jobs",
    "create_records_batch",
)

_LEGACY_ASYNC_FACADE_METHODS: tuple[str, ...] = tuple(
    f"async_{name}" for name in _CANONICAL_FACADE_METHODS
)

_ENDPOINT_NAMES: frozenset[str] = frozenset(
    {
        "codings",
        "forms",
        "intervals",
        "jobs",
        "queries",
        "record_revisions",
        "records",
        "sites",
        "studies",
        "subjects",
        "users",
        "variables",
        "visits",
    }
)


def _safe_instance_dict(obj: object) -> dict[str, Any]:
    """Return ``obj.__dict__`` without triggering custom ``__getattr__`` hooks."""
    try:
        raw = object.__getattribute__(obj, "__dict__")
        return raw if isinstance(raw, dict) else {}
    except AttributeError:
        return {}


def _mock_has_configured_attr(mock_obj: NonCallableMock, attr: str) -> bool:
    """Return True if ``attr`` was explicitly set or declared via ``spec`` on a Mock."""
    inst_dict = _safe_instance_dict(mock_obj)
    if attr in inst_dict:
        return True
    children = inst_dict.get("_mock_children")
    if isinstance(children, dict) and attr in children:
        return True
    methods = inst_dict.get("_mock_methods")
    if isinstance(methods, (list, tuple, set, frozenset)):
        return attr in methods
    return False


def is_async_facade(sdk: object) -> bool:
    """Return True when ``sdk`` is an :class:`AsyncImednetSDK` or :class:`AsyncImednetFacade`.

    Correctly distinguishes synchronous and asynchronous SDK instances, mocks with
    ``spec=ImednetFacade`` vs ``spec=AsyncImednetFacade``, and minimal test stubs.
    """
    if sdk is None:
        return False
    if isinstance(sdk, SyncToAsyncFacadeAdapter):
        return True

    cls = type(sdk)
    if cls.__name__ == "ImednetSDK":
        return False
    if cls.__name__ == "AsyncImednetSDK":
        return True

    inst_dict = _safe_instance_dict(sdk)
    has_async_client = inst_dict.get("_async_client") is not None
    has_sync_client = inst_dict.get("_client") is not None
    if has_async_client and not has_sync_client:
        return True
    if has_sync_client and not has_async_client:
        return False
    if "_async_client" in inst_dict and inst_dict["_async_client"] is None:
        return False

    if isinstance(sdk, NonCallableMock):
        for name in _CANONICAL_FACADE_METHODS:
            if name in inst_dict and inspect.iscoroutinefunction(inst_dict[name]):
                return True
        for name in _LEGACY_ASYNC_FACADE_METHODS:
            if name in inst_dict and inspect.iscoroutinefunction(inst_dict[name]):
                return True
        methods = inst_dict.get("_mock_methods")
        if isinstance(methods, (list, tuple, set, frozenset)):
            for name in _CANONICAL_FACADE_METHODS:
                if name in methods:
                    candidate = getattr(sdk, name, None)
                    return bool(inspect.iscoroutinefunction(candidate))
            for legacy_name, sync_name in zip(
                _LEGACY_ASYNC_FACADE_METHODS, _CANONICAL_FACADE_METHODS, strict=True
            ):
                if legacy_name in methods and sync_name not in methods:
                    return True
        return False

    for name in _CANONICAL_FACADE_METHODS:
        candidate = getattr(sdk, name, None)
        if candidate is not None:
            if inspect.iscoroutinefunction(candidate):
                return True
            if callable(candidate):
                # Check if a legacy stub also defines async_<name> without coroutine <name>
                break

    for legacy_name, sync_name in zip(
        _LEGACY_ASYNC_FACADE_METHODS, _CANONICAL_FACADE_METHODS, strict=True
    ):
        legacy_fn = getattr(sdk, legacy_name, None)
        sync_fn = getattr(sdk, sync_name, None)
        if inspect.iscoroutinefunction(legacy_fn) and not callable(sync_fn):
            return True

    for ep_name in ("forms", "records", "studies", "variables"):
        ep = getattr(sdk, ep_name, None)
        if ep is not None and not isinstance(ep, NonCallableMock):
            list_fn = getattr(ep, "list", None)
            if inspect.isasyncgenfunction(list_fn) or inspect.iscoroutinefunction(list_fn):
                return True

    return False


async def call_async_facade_method(
    sdk: Any,
    canonical_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Invoke ``await sdk.<canonical_name>(...)`` with legacy ``async_*`` mock fallback."""
    resolved_legacy = f"async_{canonical_name}"
    inst_dict = _safe_instance_dict(sdk)

    # 1. Explicit instance-level legacy override (e.g. ``sdk.async_get_forms = AsyncMock(...)``)
    if resolved_legacy in inst_dict and canonical_name not in inst_dict:
        legacy_override = inst_dict[resolved_legacy]
        if callable(legacy_override):
            res = legacy_override(*args, **kwargs)
            return await res if inspect.isawaitable(res) else res

    # 2. Canonical unprefixed coroutine method on AsyncImednetSDK / AsyncImednetFacade
    canonical_fn = getattr(sdk, canonical_name, None)
    if inspect.iscoroutinefunction(canonical_fn) or (
        canonical_name in inst_dict and callable(canonical_fn)
    ):
        res = canonical_fn(*args, **kwargs)
        return await res if inspect.isawaitable(res) else res

    # 3. Legacy async_* coroutine method on minimal custom test stubs
    legacy_fn = getattr(sdk, resolved_legacy, None)
    if callable(legacy_fn):
        res = legacy_fn(*args, **kwargs)
        return await res if inspect.isawaitable(res) else res

    # 4. Synchronous fallback (e.g. lightweight test stubs, never a raw ImednetSDK)
    if callable(canonical_fn) and type(sdk).__name__ != "ImednetSDK":
        res = canonical_fn(*args, **kwargs)
        return await res if inspect.isawaitable(res) else res

    raise AttributeError(
        f"SDK facade {type(sdk).__name__!r} exposes neither {canonical_name!r} "
        f"nor {resolved_legacy!r}."
    )


async def maybe_to_thread(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run ``func(*args, **kwargs)`` in a worker thread with ``ContextVar`` propagation.

    Falls back to inline execution on WebAssembly/Emscripten (Pyodide) or constrained
    environments where OS threads cannot be spawned.
    """
    if sys.platform == "emscripten":
        return func(*args, **kwargs)
    ctx = contextvars.copy_context()
    bound = functools.partial(func, *args, **kwargs)
    try:
        return await asyncio.to_thread(ctx.run, bound)
    except RuntimeError as exc:
        if "can't start new thread" in str(exc).lower():
            return func(*args, **kwargs)
        raise


def _run_in_isolated_selector_loop(coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Execute ``coro_factory()`` in an isolated ``SelectorEventLoop`` on the current thread.

    Used on single-threaded WebAssembly (Pyodide/Emscripten) when an outer ``WebLoop``
    is already active and OS threads cannot be started.
    """
    prev_loop = asyncio.events._get_running_loop()
    loop = asyncio.SelectorEventLoop()
    asyncio.events._set_running_loop(None)
    try:
        return loop.run_until_complete(coro_factory())
    finally:
        try:
            loop.close()
        finally:
            asyncio.events._set_running_loop(prev_loop)


def run_sync_over_async(coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Execute an async coroutine factory synchronously, even inside active event loops.

    - When no event loop is running on the current thread, calls ``asyncio.run`` directly.
    - When an event loop is already running (e.g. Jupyter, FastAPI, ``pytest-asyncio``):

      - On CPython, executes ``asyncio.run`` inside a dedicated worker thread with full
        ``contextvars`` propagation so the outer event loop is never re-entered.
      - On Pyodide / Emscripten (where OS threads are unavailable), runs an isolated
        ``SelectorEventLoop`` to completion and restores the active ``WebLoop``.
    """
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if running_loop is None:
        return asyncio.run(coro_factory())

    if sys.platform == "emscripten":
        return _run_in_isolated_selector_loop(coro_factory)

    ctx = contextvars.copy_context()
    result_box: list[T] = []
    error_box: list[BaseException] = []

    def _worker() -> None:
        try:
            result_box.append(ctx.run(asyncio.run, coro_factory()))
        except BaseException as exc:
            error_box.append(exc)

    thread = threading.Thread(target=_worker, name="imednet-sync-bridge", daemon=True)
    try:
        thread.start()
    except RuntimeError as exc:
        if "can't start new thread" in str(exc).lower():
            return _run_in_isolated_selector_loop(coro_factory)
        raise

    thread.join()
    if error_box:
        raise error_box[0]
    return result_box[0]


class _SyncEndpointAsyncAdapter:
    """Wraps a synchronous SDK endpoint to expose async ``list`` and action methods."""

    def __init__(self, sync_endpoint: Any, *, offload_threads: bool = False) -> None:
        self._endpoint = sync_endpoint
        self._offload_threads = offload_threads

    async def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if self._offload_threads:
            return await maybe_to_thread(fn, *args, **kwargs)
        return fn(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        """Iterate synchronously fetched endpoint items via an async generator."""
        list_fn = self._endpoint.list
        offload = self._offload_threads

        async def _gen() -> AsyncIterator[Any]:
            if offload:
                items = await maybe_to_thread(lambda: list(list_fn(*args, **kwargs)))
                for item in items:
                    yield item
            else:
                for item in list_fn(*args, **kwargs):
                    yield item

        return _gen()

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate ``get`` to the underlying synchronous endpoint."""
        return await self._call(self._endpoint.get, *args, **kwargs)

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate ``create`` to the underlying synchronous endpoint."""
        return await self._call(self._endpoint.create, *args, **kwargs)

    async def create_batch(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate ``create_batch`` to the underlying synchronous endpoint."""
        return await self._call(self._endpoint.create_batch, *args, **kwargs)

    async def poll(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate ``poll`` to the underlying synchronous endpoint."""
        return await self._call(self._endpoint.poll, *args, **kwargs)

    async def poll_many(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate ``poll_many`` to the underlying synchronous endpoint."""
        return await self._call(self._endpoint.poll_many, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._endpoint, name)


class SyncToAsyncFacadeAdapter:
    """Adapts a synchronous :class:`ImednetFacade` to satisfy :class:`AsyncImednetFacade`.

    Never instantiates :class:`httpx.AsyncClient`; all operations delegate to the
    caller's synchronous SDK or mock facade.
    """

    def __init__(self, sync_sdk: ImednetFacade, *, offload_threads: bool = False) -> None:
        """Initialize the adapter around ``sync_sdk``."""
        self._sync_sdk = sync_sdk
        self._offload_threads = offload_threads

    @property
    def sync_sdk(self) -> ImednetFacade:
        """Return the wrapped synchronous SDK facade."""
        return self._sync_sdk

    @property
    def auth(self) -> AuthStrategy | None:
        """Return the authentication provider from the wrapped SDK."""
        return cast("AuthStrategy | None", getattr(self._sync_sdk, "auth", None))

    @property
    def config(self) -> Any:
        """Return the configuration object from the wrapped SDK if available."""
        return getattr(self._sync_sdk, "config", None)

    @contextmanager
    def study_context(self, study_key: str) -> Iterator[SyncToAsyncFacadeAdapter]:
        """Enter the wrapped SDK's ``study_context`` if available, else core ``study_context``."""
        from imednet.core.context import sdk_governance_context
        from imednet.core.context import study_context as core_study_context

        sc_fn = getattr(self._sync_sdk, "study_context", None)
        if callable(sc_fn) and not isinstance(sc_fn, NonCallableMock):
            cm = cast(AbstractContextManager[Any], sc_fn(study_key))
            with cm:
                yield self
            return
        if isinstance(self._sync_sdk, NonCallableMock) and _mock_has_configured_attr(
            self._sync_sdk, "study_context"
        ):
            cm_candidate = self._sync_sdk.study_context(study_key)
            if hasattr(cm_candidate, "__enter__") and hasattr(cm_candidate, "__exit__"):
                with cm_candidate, core_study_context(study_key):
                    yield self
                return
        with sdk_governance_context(self._sync_sdk), core_study_context(study_key):
            yield self

    async def _invoke(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._sync_sdk, method_name)
        if self._offload_threads:
            return await maybe_to_thread(fn, *args, **kwargs)
        return fn(*args, **kwargs)

    if TYPE_CHECKING:

        async def get_codings(
            self, study_key: str | None = None, **filters: FilterValue
        ) -> list[Coding]:
            """Delegate ``get_codings`` to the wrapped synchronous facade."""
            ...

        async def get_forms(
            self, study_key: str | None = None, **filters: FilterValue
        ) -> list[Form]:
            """Delegate ``get_forms`` to the wrapped synchronous facade."""
            ...

        async def get_intervals(
            self, study_key: str | None = None, **filters: FilterValue
        ) -> list[Interval]:
            """Delegate ``get_intervals`` to the wrapped synchronous facade."""
            ...

        async def get_job(self, study_key: str, batch_id: str) -> JobStatus:
            """Delegate ``get_job`` to the wrapped synchronous facade."""
            ...

        async def get_queries(
            self, study_key: str | None = None, **filters: FilterValue
        ) -> list[Query]:
            """Delegate ``get_queries`` to the wrapped synchronous facade."""
            ...

        async def get_record_revisions(
            self, study_key: str | None = None, **filters: FilterValue
        ) -> list[RecordRevision]:
            """Delegate ``get_record_revisions`` to the wrapped synchronous facade."""
            ...

        async def get_records(
            self, study_key: str | None = None, **filters: FilterValue
        ) -> list[Record]:
            """Delegate ``get_records`` to the wrapped synchronous facade."""
            ...

        async def get_sites(
            self, study_key: str | None = None, **filters: FilterValue
        ) -> list[Site]:
            """Delegate ``get_sites`` to the wrapped synchronous facade."""
            ...

        async def get_studies(self, **filters: FilterValue) -> list[Study]:
            """Delegate ``get_studies`` to the wrapped synchronous facade."""
            ...

        async def get_subjects(
            self, study_key: str | None = None, **filters: FilterValue
        ) -> list[Subject]:
            """Delegate ``get_subjects`` to the wrapped synchronous facade."""
            ...

        async def get_users(
            self, study_key: str | None = None, **filters: FilterValue
        ) -> list[User]:
            """Delegate ``get_users`` to the wrapped synchronous facade."""
            ...

        async def get_variables(
            self, study_key: str | None = None, **filters: FilterValue
        ) -> list[Variable]:
            """Delegate ``get_variables`` to the wrapped synchronous facade."""
            ...

        async def get_visits(
            self, study_key: str | None = None, **filters: FilterValue
        ) -> list[Visit]:
            """Delegate ``get_visits`` to the wrapped synchronous facade."""
            ...

        async def create_record(
            self,
            study_key: str,
            records_data: RecordsInput,
            email_notify: bool | str | None = None,
            *,
            schema: ValidationSchemaProtocol | None = None,
            skip_validation: bool = False,
        ) -> Job:
            """Delegate ``create_record`` to the wrapped synchronous facade."""
            ...

        async def create_records_batch(
            self,
            study_key: str,
            records_data: RecordsInput,
            email_notify: bool | str | None = None,
            *,
            schema: ValidationSchemaProtocol | None = None,
            chunk_size: int = 100,
            skip_validation: bool = False,
            wait_for_completion: bool = False,
            timeout: float = 300.0,
            interval: float = 5.0,
            on_progress: JobProgressHandler | None = None,
            dry_run: bool = False,
            collect_errors: bool = False,
        ) -> BatchSubmissionSummary:
            """Delegate ``create_records_batch`` to the wrapped synchronous facade."""
            ...

        async def poll_job(
            self,
            study_key: str,
            batch_id: str,
            *,
            interval: float = 5,
            timeout: float = 300,
            on_progress: JobProgressHandler | None = None,
            resolve_results: bool = False,
        ) -> JobStatus:
            """Delegate ``poll_job`` to the wrapped synchronous facade."""
            ...

        async def poll_jobs(
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
            """Delegate ``poll_jobs`` to the wrapped synchronous facade."""
            ...

    else:

        async def get_codings(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_codings`` to the wrapped synchronous facade."""
            return await self._invoke("get_codings", *args, **kwargs)

        async def get_forms(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_forms`` to the wrapped synchronous facade."""
            return await self._invoke("get_forms", *args, **kwargs)

        async def get_intervals(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_intervals`` to the wrapped synchronous facade."""
            return await self._invoke("get_intervals", *args, **kwargs)

        async def get_job(self, *args: Any, **kwargs: Any) -> Any:
            """Delegate ``get_job`` to the wrapped synchronous facade."""
            return await self._invoke("get_job", *args, **kwargs)

        async def get_queries(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_queries`` to the wrapped synchronous facade."""
            return await self._invoke("get_queries", *args, **kwargs)

        async def get_record_revisions(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_record_revisions`` to the wrapped synchronous facade."""
            return await self._invoke("get_record_revisions", *args, **kwargs)

        async def get_records(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_records`` to the wrapped synchronous facade."""
            return await self._invoke("get_records", *args, **kwargs)

        async def get_sites(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_sites`` to the wrapped synchronous facade."""
            return await self._invoke("get_sites", *args, **kwargs)

        async def get_studies(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_studies`` to the wrapped synchronous facade."""
            return await self._invoke("get_studies", *args, **kwargs)

        async def get_subjects(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_subjects`` to the wrapped synchronous facade."""
            return await self._invoke("get_subjects", *args, **kwargs)

        async def get_users(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_users`` to the wrapped synchronous facade."""
            return await self._invoke("get_users", *args, **kwargs)

        async def get_variables(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_variables`` to the wrapped synchronous facade."""
            return await self._invoke("get_variables", *args, **kwargs)

        async def get_visits(self, *args: Any, **kwargs: Any) -> list[Any]:
            """Delegate ``get_visits`` to the wrapped synchronous facade."""
            return await self._invoke("get_visits", *args, **kwargs)

        async def create_record(self, *args: Any, **kwargs: Any) -> Any:
            """Delegate ``create_record`` to the wrapped synchronous facade."""
            return await self._invoke("create_record", *args, **kwargs)

        async def create_records_batch(self, *args: Any, **kwargs: Any) -> Any:
            """Delegate ``create_records_batch`` to the wrapped synchronous facade."""
            return await self._invoke("create_records_batch", *args, **kwargs)

        async def poll_job(self, *args: Any, **kwargs: Any) -> Any:
            """Delegate ``poll_job`` to the wrapped synchronous facade."""
            return await self._invoke("poll_job", *args, **kwargs)

        async def poll_jobs(self, *args: Any, **kwargs: Any) -> Any:
            """Delegate ``poll_jobs`` to the wrapped synchronous facade."""
            return await self._invoke("poll_jobs", *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Expose wrapped endpoint adapters when present on the underlying sync SDK."""
        if name in _ENDPOINT_NAMES:
            if isinstance(self._sync_sdk, NonCallableMock) and not _mock_has_configured_attr(
                self._sync_sdk, name
            ):
                raise AttributeError(name)
            ep = getattr(self._sync_sdk, name)
            return _SyncEndpointAsyncAdapter(ep, offload_threads=self._offload_threads)
        if isinstance(self._sync_sdk, NonCallableMock) and not _mock_has_configured_attr(
            self._sync_sdk, name
        ):
            raise AttributeError(name)
        return getattr(self._sync_sdk, name)


__all__ = [
    "SyncToAsyncFacadeAdapter",
    "call_async_facade_method",
    "is_async_facade",
    "maybe_to_thread",
    "run_sync_over_async",
]
