"""Global test configuration and shared fixtures for the iMedNet SDK test suite.

This module provides common fixtures for mocking network requests, managing study context,
and simulating API responses across unit and integration tests.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

# Ensure a default security key is set for testing encryption/decryption
os.environ.setdefault("IMEDNET_SECURITY_KEY", "test_sec_key")
os.environ.setdefault("LC_MESSAGES", "C")

ROOT = Path(__file__).resolve().parents[1]
LIVE_TESTS_DIR = (ROOT / "tests" / "live").resolve()
for source_root in (
    ROOT / "packages" / "core" / "src",
    ROOT / "packages" / "plugins-workflows" / "src",
    ROOT / "packages" / "providers-airflow" / "src",
):
    sys.path.insert(0, str(source_root))

import importlib.metadata as _importlib_metadata
import logging as _logging
import ssl as _ssl

import respx
import respx.mocks as _respx_mocks


def _is_closed_handler(handler: _logging.Handler) -> bool:
    """Return True if a logging handler wraps a closed I/O stream."""
    stream = getattr(handler, "stream", None)
    if stream is not None and getattr(stream, "closed", False):
        return True
    console = getattr(handler, "console", None)
    console_file = getattr(console, "_file", None) if console is not None else None
    return bool(console_file is not None and getattr(console_file, "closed", False))


_INITIAL_ROOT_HANDLERS: list[_logging.Handler] = [
    h for h in _logging.getLogger().handlers if not _is_closed_handler(h)
]
_INITIAL_NAMED_LOGGERS: dict[str, tuple[list[_logging.Handler], int, bool, bool]] = {
    name: (
        [h for h in lg.handlers if not _is_closed_handler(h)],
        lg.level,
        lg.propagate,
        lg.disabled,
    )
    for name, lg in _logging.Logger.manager.loggerDict.items()
    if isinstance(lg, _logging.Logger)
}
_post_collection_logging_sanitized = False


def _sanitize_post_collection_loggers() -> None:
    """Strip non-pytest handlers attached as import side effects during test collection."""
    global _post_collection_logging_sanitized
    if _post_collection_logging_sanitized:
        return
    _post_collection_logging_sanitized = True

    root_logger = _logging.getLogger()
    root_logger.handlers[:] = [
        h
        for h in root_logger.handlers
        if not _is_closed_handler(h)
        and (h in _INITIAL_ROOT_HANDLERS or type(h).__module__.startswith("_pytest"))
    ]
    for name, lg in list(_logging.Logger.manager.loggerDict.items()):
        if not isinstance(lg, _logging.Logger):
            continue
        if name in _INITIAL_NAMED_LOGGERS:
            init_handlers, init_level, init_prop, init_dis = _INITIAL_NAMED_LOGGERS[name]
            target_handlers = [h for h in init_handlers if not _is_closed_handler(h)]
            if lg.handlers != target_handlers:
                lg.handlers[:] = target_handlers
            if lg.level != init_level:
                lg.setLevel(init_level)
            if lg.propagate != init_prop:
                lg.propagate = init_prop
            if lg.disabled != init_dis:
                lg.disabled = init_dis
        else:
            if any(not isinstance(h, _logging.NullHandler) for h in lg.handlers):
                lg.handlers[:] = [h for h in lg.handlers if isinstance(h, _logging.NullHandler)]
            if lg.level != _logging.NOTSET:
                lg.setLevel(_logging.NOTSET)
            if not lg.propagate:
                lg.propagate = True
            if lg.disabled:
                lg.disabled = False


_orig_entry_points = _importlib_metadata.entry_points
_entry_points_cache: dict[tuple[tuple[str, ...], tuple[tuple[str, object], ...]], object] = {}


def _cached_entry_points(**params: object):  # type: ignore[no-untyped-def]
    """Cache entry point lookups across tests when sys.path is unchanged."""
    key = (tuple(sys.path), tuple(sorted(params.items())))
    cached = _entry_points_cache.get(key)
    if cached is None:
        cached = _orig_entry_points(**params)
        _entry_points_cache[key] = cached
    return cached


_importlib_metadata.entry_points = _cached_entry_points  # type: ignore[assignment]

_orig_ssl_create_default_context = _ssl.create_default_context
_ssl_context_cache: dict[tuple[_ssl.Purpose, str | None], _ssl.SSLContext] = {}


def _cached_ssl_create_default_context(
    purpose: _ssl.Purpose = _ssl.Purpose.SERVER_AUTH,
    *,
    cafile: str | None = None,
    capath: str | None = None,
    cadata: str | bytes | None = None,
) -> _ssl.SSLContext:
    """Reuse default SERVER_AUTH SSLContext instances across HTTP clients in tests."""
    if purpose == _ssl.Purpose.SERVER_AUTH and capath is None and cadata is None:
        cache_key = (purpose, cafile)
        cached_ctx = _ssl_context_cache.get(cache_key)
        if cached_ctx is None:
            cached_ctx = _orig_ssl_create_default_context(purpose, cafile=cafile)
            _ssl_context_cache[cache_key] = cached_ctx
        return cached_ctx
    return _orig_ssl_create_default_context(purpose, cafile=cafile, capath=capath, cadata=cadata)


_ssl.create_default_context = _cached_ssl_create_default_context  # type: ignore[assignment]

_orig_httpcore_mocker_stop = _respx_mocks.HTTPCoreMocker.stop
_keep_respx_patches = False


def _warm_httpcore_mocker_stop(force: bool = False) -> None:
    """Keep httpcore transport patches warm between non-live tests."""
    if _keep_respx_patches and not force:
        return
    _orig_httpcore_mocker_stop(force=force)


_respx_mocks.HTTPCoreMocker.stop = _warm_httpcore_mocker_stop  # type: ignore[method-assign]

_cli_parser_cache: dict[tuple[object, ...], object] = {}


def _ensure_cli_parser_cached() -> None:
    """Wrap imednet.cli.get_parser with an entry-point-aware cache during tests."""
    cli_mod = sys.modules.get("imednet.cli")
    if cli_mod is None:
        return
    current_get_parser = getattr(cli_mod, "get_parser", None)
    if current_get_parser is None or getattr(current_get_parser, "_is_test_cached", False):
        return

    def _cached_get_parser():  # type: ignore[no-untyped-def]
        app_fn = getattr(cli_mod, "app", None)
        if app_fn is None or sys._getframe(1).f_code is not app_fn.__code__:
            return current_get_parser()
        if _importlib_metadata.entry_points is not _cached_entry_points:
            return current_get_parser()
        if any(type(f).__module__.startswith("tests.") for f in sys.meta_path):
            return current_get_parser()
        wf_mod = sys.modules.get("imednet_workflows")
        wf_cli = sys.modules.get("imednet_workflows.cli")
        tui_mod = sys.modules.get("imednet_tui")
        tui_cli = sys.modules.get("imednet_tui.cli")
        if "imednet_workflows" in sys.modules and wf_mod is None:
            return current_get_parser()
        if "imednet_tui" in sys.modules and tui_mod is None:
            return current_get_parser()
        ledger_default = str(getattr(wf_cli, "DEFAULT_LEDGER_PATH", ""))
        prog_key = sys.argv[0] if sys.argv else ""
        key = (
            id(cli_mod),
            id(current_get_parser),
            id(wf_mod),
            id(wf_cli),
            id(getattr(wf_cli, "setup_parser", None)),
            id(tui_mod),
            id(tui_cli),
            id(getattr(tui_cli, "setup_parser", None)),
            prog_key,
            ledger_default,
        )
        cached_parser = _cli_parser_cache.get(key)
        if cached_parser is None:
            built_parser = current_get_parser()
            sub_actions = [a for a in built_parser._actions if hasattr(a, "choices") and a.choices]
            choices = sub_actions[0].choices if sub_actions else {}
            wf_func = choices["workflows"].get_default("func") if "workflows" in choices else None
            tui_func = choices["tui"].get_default("func") if "tui" in choices else None
            if getattr(wf_func, "__name__", "") == "workflows_stub" or (
                getattr(tui_func, "__name__", "") == "tui_stub"
            ):
                return built_parser
            _cli_parser_cache[key] = built_parser
            cached_parser = built_parser
        return cached_parser

    _cached_get_parser._is_test_cached = True  # type: ignore[attr-defined]
    cli_mod.get_parser = _cached_get_parser  # type: ignore[attr-defined]


from imednet.core.async_client import AsyncClient
from imednet.core.client import Client
from imednet.core.context import clear_study_context
from imednet.core.operations.circuit_breaker import get_global_circuit_breaker
from imednet.core.retry import RetryConfig
from imednet.utils import db as _imednet_db

_GLOBAL_CIRCUIT_BREAKER = get_global_circuit_breaker()
_DEFAULT_RESPX_BASES = dict(respx.mock._bases)
_respx_session_guard_active = False

_orig_core_get_sqlite_connection = _imednet_db.get_sqlite_connection


def _fast_core_get_sqlite_connection(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Disable fsync on test SQLite connections while preserving WAL mode."""
    conn = _orig_core_get_sqlite_connection(*args, **kwargs)
    conn.execute("PRAGMA synchronous=OFF;")
    return conn


_imednet_db.get_sqlite_connection = _fast_core_get_sqlite_connection  # type: ignore[assignment]
try:
    import imednet.spi.utils as _imednet_spi_utils

    _imednet_spi_utils.get_sqlite_connection = _fast_core_get_sqlite_connection  # type: ignore[assignment]
except ImportError:
    pass
try:
    import imednet_workflows.cached_loader as _wf_cached_loader

    _orig_wf_get_sqlite_connection = _wf_cached_loader.get_sqlite_connection

    def _fast_wf_get_sqlite_connection(*args, **kwargs):  # type: ignore[no-untyped-def]
        conn = _orig_wf_get_sqlite_connection(*args, **kwargs)
        conn.execute("PRAGMA synchronous=OFF;")
        return conn

    _wf_cached_loader.get_sqlite_connection = _fast_wf_get_sqlite_connection  # type: ignore[assignment]
except ImportError:
    pass
try:
    import sqlite3 as _sqlite3

    from sqlalchemy import event as _sa_event
    from sqlalchemy.engine import Engine as _SAEngine

    @_sa_event.listens_for(_SAEngine, "connect")
    def _fast_sqlalchemy_sqlite_connect(
        dbapi_connection: object, _connection_record: object
    ) -> None:
        if isinstance(dbapi_connection, _sqlite3.Connection):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA synchronous=OFF;")
            cursor.close()
except ImportError:
    pass
try:
    import duckdb as _duckdb

    _orig_duckdb_connect = _duckdb.connect

    def _fast_duckdb_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        cfg = dict(kwargs.get("config") or {})
        cfg.setdefault("threads", "1")
        kwargs["config"] = cfg
        return _orig_duckdb_connect(*args, **kwargs)

    _duckdb.connect = _fast_duckdb_connect  # type: ignore[assignment]
except ImportError:
    pass


def pytest_addoption(parser):
    """Add custom command-line options for running specific test tracks."""
    parser.addoption("--run-fuzzing", action="store_true", default=False, help="run fuzzing tests")
    parser.addoption(
        "--run-performance", action="store_true", default=False, help="run performance tests"
    )


def pytest_collection_modifyitems(config, items):
    """Skip fuzzing and performance tests unless explicitly requested via CLI options or marker filter."""
    skip_fuzzing = pytest.mark.skip(reason="need --run-fuzzing option or -m fuzzing to run")
    skip_performance = pytest.mark.skip(
        reason="need --run-performance option or -m performance to run"
    )

    marker_expr = config.getoption("-m")

    for item in items:
        if "fuzzing" in item.keywords:
            if not config.getoption("--run-fuzzing") and "fuzzing" not in marker_expr:
                item.add_marker(skip_fuzzing)
        if "performance" in item.keywords:
            if not config.getoption("--run-performance") and "performance" not in marker_expr:
                item.add_marker(skip_performance)


@pytest.fixture(scope="session", autouse=True)
def _warm_respx_transport_patches():
    """Keep respx transport patches and strict mock router active across the session."""
    global _keep_respx_patches, _respx_session_guard_active
    _keep_respx_patches = True
    _respx_mocks.HTTPCoreMocker.start()
    respx.mock.start()
    _respx_session_guard_active = True
    try:
        yield
    finally:
        if _respx_session_guard_active:
            respx.mock.stop()
            _respx_session_guard_active = False
        _keep_respx_patches = False
        _orig_httpcore_mocker_stop(force=True)


@pytest.fixture
def block_external_requests(request: pytest.FixtureRequest):
    """Block real network calls in all non-live tests.

    Uses the ``respx_mock`` pytest-plugin fixture so that all non-live tests
    share a single strict router; tests cannot bypass the guard by opening
    their own ``respx.mock`` context or decorator.
    """
    if _is_live_test(request.node):
        yield
        return
    request.getfixturevalue("respx_mock")
    yield


def _is_live_test(node: object) -> bool:
    """Check if a test node is marked as a live test or resides in the live tests directory.

    Args:
        node: The pytest node to check.

    Returns:
        True if the test is a live test, False otherwise.
    """
    if not hasattr(node, "get_closest_marker"):
        return False

    if node.get_closest_marker("live"):
        return True

    node_path = getattr(node, "path", None)
    if node_path is None:
        return False

    path = node_path if isinstance(node_path, Path) else Path(str(node_path))
    if path.is_absolute() and ".." not in path.parts and path.is_relative_to(ROOT):
        return path.is_relative_to(LIVE_TESTS_DIR)
    return path.resolve().is_relative_to(LIVE_TESTS_DIR)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):  # type: ignore[no-untyped-def]
    """Snapshot and restore logging handlers and levels across each full test lifecycle."""
    del item, nextitem
    _sanitize_post_collection_loggers()
    root_logger = _logging.getLogger()
    saved_root_handlers = tuple(root_logger.handlers)
    saved_root_level = root_logger.level
    saved_root_disabled = root_logger.disabled
    saved_named_loggers: dict[str, tuple[tuple[_logging.Handler, ...], int, bool, bool]] = {
        name: (tuple(lg.handlers), lg.level, lg.propagate, lg.disabled)
        for name, lg in _logging.Logger.manager.loggerDict.items()
        if isinstance(lg, _logging.Logger)
    }
    try:
        yield
    finally:
        target_root_handlers = [h for h in saved_root_handlers if not _is_closed_handler(h)]
        if root_logger.handlers != target_root_handlers:
            root_logger.handlers[:] = target_root_handlers
        if root_logger.level != saved_root_level:
            root_logger.setLevel(saved_root_level)
        if root_logger.disabled != saved_root_disabled:
            root_logger.disabled = saved_root_disabled
        for name, lg in list(_logging.Logger.manager.loggerDict.items()):
            if not isinstance(lg, _logging.Logger):
                continue
            saved = saved_named_loggers.get(name)
            if saved is not None:
                h_tuple, lvl, prop, dis = saved
                if not lg.handlers and not h_tuple:
                    pass
                elif tuple(lg.handlers) != h_tuple or any(_is_closed_handler(h) for h in h_tuple):
                    lg.handlers[:] = [h for h in h_tuple if not _is_closed_handler(h)]
                if lg.level != lvl:
                    lg.setLevel(lvl)
                if lg.propagate != prop:
                    lg.propagate = prop
                if lg.disabled != dis:
                    lg.disabled = dis
            else:
                if any(not isinstance(h, _logging.NullHandler) for h in lg.handlers):
                    lg.handlers[:] = [h for h in lg.handlers if isinstance(h, _logging.NullHandler)]
                if lg.level != _logging.NOTSET:
                    lg.setLevel(_logging.NOTSET)
                if not lg.propagate:
                    lg.propagate = True
                if lg.disabled:
                    lg.disabled = False


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Reset study context, circuit breaker, CLI parser cache, and network guard before each test."""
    global _respx_session_guard_active
    if _keep_respx_patches:
        if _is_live_test(item):
            if _respx_session_guard_active:
                respx.mock.stop()
                _orig_httpcore_mocker_stop(force=True)
                _respx_session_guard_active = False
        elif not _respx_session_guard_active:
            _respx_mocks.HTTPCoreMocker.start()
            respx.mock.start()
            _respx_session_guard_active = True

    _ensure_cli_parser_cached()
    clear_study_context()
    _GLOBAL_CIRCUIT_BREAKER.reset()


def pytest_runtest_teardown(item: pytest.Item) -> None:
    """Reset study context, circuit breaker, and dirty respx router state after each test."""
    if _respx_session_guard_active and (
        respx.mock.routes._routes or respx.mock.calls or respx.mock._bases != _DEFAULT_RESPX_BASES
    ):
        respx.mock.clear()
        respx.mock.reset()
        respx.mock._bases = dict(_DEFAULT_RESPX_BASES)

    clear_study_context()
    _GLOBAL_CIRCUIT_BREAKER.reset()


class DummyResponse:
    """A simple mock response object that mimics the httpx.Response JSON interface."""

    def __init__(self, data):
        """Initialize the dummy response with data.

        Args:
            data: The data to be returned by the json() method.
        """
        self._data = data

    def json(self):
        """Return the stored data as JSON.

        Returns:
            The data provided during initialization.
        """
        return self._data


@pytest.fixture
def dummy_client():
    """Provide a MagicMock to simulate an iMedNet client."""
    return MagicMock()


@pytest.fixture
def response_factory():
    """Provide a factory function to create DummyResponse instances."""

    def factory(data):
        """Create a DummyResponse with the given data."""
        return DummyResponse(data)

    return factory


@pytest.fixture
def paginator_factory(monkeypatch):
    """Provide a factory to mock synchronous pagination for iMedNet endpoints.

    This fixture patches the endpoint classes to use a dummy paginator and
    captures the arguments passed to it.
    """
    from imednet.core.endpoint.operations.list import ListOperation
    from tests.utils.streaming import StreamingMockWrapper

    def factory(module, items):
        """Create a dummy paginator and patch the specified module.

        Args:
            module: The module containing endpoint classes to patch.
            items: The list of items the dummy paginator should return.

        Returns:
            A dictionary containing captured arguments from the paginator initialization.
        """
        captured = {"count": 0}

        class DummyPaginator:
            """A mock paginator for synchronous list operations."""

            def __init__(self, client, path, params=None, page_size=100, **kwargs):
                """Initialize the dummy paginator and capture arguments."""
                captured["client"] = client
                captured["path"] = path
                captured["params"] = params or {}
                captured["page_size"] = page_size
                captured["count"] += 1
                self._items = items
                if hasattr(client, "get") and items:
                    from tests.conftest import DummyResponse

                    if isinstance(client.get, MagicMock):
                        client.get.return_value = DummyResponse(items[0])

            def __iter__(self):
                """Iterate over the provided items."""
                yield from self._items

        from imednet.core.endpoint.base import SyncListEndpoint, SyncListGetEndpoint

        for obj in module.__dict__.values():
            try:
                if issubclass(obj, (SyncListEndpoint, SyncListGetEndpoint)):
                    monkeypatch.setattr(obj, "PAGINATOR_CLS", DummyPaginator, raising=False)
            except TypeError:
                pass

        def fake_execute_sync(self, client, paginator_cls):
            """Fake implementation of the synchronous list operation execution."""
            paginator = paginator_cls(
                client, self.path, params=self.params, page_size=self.page_size
            )
            parsed_items = [self.parse_func(item) for item in paginator._items]
            return StreamingMockWrapper(parsed_items)

        monkeypatch.setattr(ListOperation, "execute_sync", fake_execute_sync)
        return captured

    return factory


@pytest.fixture
def async_paginator_factory(monkeypatch):
    """Provide a factory to mock asynchronous pagination for iMedNet endpoints.

    This fixture patches the endpoint classes to use an async dummy paginator
    and captures the arguments passed to it.
    """
    from imednet.core.endpoint.operations.list import ListOperation
    from tests.utils.streaming import StreamingMockWrapper

    def factory(module, items):
        """Create an async dummy paginator and patch the specified module.

        Args:
            module: The module containing endpoint classes to patch.
            items: The list of items the dummy paginator should return.

        Returns:
            A dictionary containing captured arguments from the paginator initialization.
        """
        captured = {"count": 0}

        class DummyPaginator:
            """A mock paginator for asynchronous list operations."""

            def __init__(self, client, path, params=None, page_size=100, **kwargs):
                """Initialize the dummy paginator and capture arguments."""
                captured["client"] = client
                captured["path"] = path
                captured["params"] = params or {}
                captured["page_size"] = page_size
                captured["count"] += 1
                self._items = items
                if hasattr(client, "get") and items:
                    from unittest.mock import AsyncMock

                    from tests.conftest import DummyResponse

                    if isinstance(client.get, MagicMock) and not isinstance(client.get, AsyncMock):
                        client.get = AsyncMock(return_value=DummyResponse(items[0]))

            async def __aiter__(self):
                """Asynchronously iterate over the provided items."""
                for item in self._items:
                    yield item

        from imednet.core.endpoint.base import AsyncListEndpoint, AsyncListGetEndpoint

        for obj in module.__dict__.values():
            try:
                if issubclass(obj, (AsyncListEndpoint, AsyncListGetEndpoint)):
                    monkeypatch.setattr(obj, "ASYNC_PAGINATOR_CLS", DummyPaginator, raising=False)
            except TypeError:
                pass

        def fake_execute_async(self, client, paginator_cls):
            """Fake implementation of the asynchronous list operation execution."""
            paginator = paginator_cls(
                client, self.path, params=self.params, page_size=self.page_size
            )
            parsed_items = [self.parse_func(item) for item in paginator._items]
            return StreamingMockWrapper(parsed_items)

        monkeypatch.setattr(ListOperation, "execute_async", fake_execute_async)

        return captured

    return factory


@pytest.fixture
def patch_build_filter(monkeypatch):
    """Provide a helper to mock the filter string builder and capture input filters."""

    def patch(module):
        """Patch the filter builder in the specified module.

        Args:
            module: The module where the filter builder should be patched.

        Returns:
            A dictionary that will contain the captured filters.
        """
        captured = {}

        def fake(filters):
            """Fake filter builder that captures filters and returns a constant."""
            captured["filters"] = filters
            return "FILTERED"

        if hasattr(module, "build_filter_string"):
            monkeypatch.setattr(module, "build_filter_string", fake)
        else:
            import imednet.core.endpoint.base as endpoint_base

            monkeypatch.setattr(endpoint_base, "build_filter_string", fake)
        return captured

    return patch


@pytest.fixture
def http_client():
    """Provide a synchronous Client instance configured for testing."""
    return Client(
        "key",
        "secret",
        base_url="https://api.test",
        retry_config=RetryConfig(backoff_factor=0.0),
    )


@pytest_asyncio.fixture
async def async_http_client():
    """Provide an asynchronous AsyncClient instance configured for testing.

    This fixture ensures the client is properly closed after use.
    """
    client = AsyncClient(
        "key",
        "secret",
        base_url="https://api.test",
        retry_config=RetryConfig(backoff_factor=0.0),
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def respx_mock_client(http_client, respx_mock):
    """Configure respx_mock with the base URL of the synchronous test client."""
    respx_mock.base_url = http_client.base_url
    return respx_mock


@pytest_asyncio.fixture
async def respx_mock_async_client(async_http_client, respx_mock):
    """Configure respx_mock with the base URL of the asynchronous test client."""
    respx_mock.base_url = async_http_client.base_url
    return respx_mock


@pytest.fixture
def sample_data():
    """Provide a simple dictionary for testing data handling."""
    return {"data": [1]}
