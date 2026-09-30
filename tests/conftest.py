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

ROOT = Path(__file__).resolve().parents[1]
LIVE_TESTS_DIR = (ROOT / "tests" / "live").resolve()
for source_root in (
    ROOT / "packages" / "core" / "src",
    ROOT / "packages" / "plugins-workflows" / "src",
    ROOT / "packages" / "providers-airflow" / "src",
):
    sys.path.insert(0, str(source_root))

from imednet.core.async_client import AsyncClient
from imednet.core.client import Client
from imednet.core.context import Context, clear_study_context


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


@pytest.fixture(autouse=True)
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

    return Path(str(node_path)).resolve().is_relative_to(LIVE_TESTS_DIR)


@pytest.fixture(autouse=True)
def reset_study_context_between_tests():
    """Reset the global study context before and after each test to ensure isolation."""
    clear_study_context()
    yield
    clear_study_context()


@pytest.fixture(autouse=True)
def reset_circuit_breaker_between_tests():
    """Reset the global circuit breaker before and after each test to ensure state isolation."""
    from imednet.core.operations.circuit_breaker import get_global_circuit_breaker

    get_global_circuit_breaker().reset()
    yield
    get_global_circuit_breaker().reset()


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
def context():
    """Provide a fresh Context instance for each test."""
    return Context()


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

            def __iter__(self):
                """Iterate over the provided items."""
                yield from self._items

        from imednet.core.endpoint.base import SyncListGetEndpoint

        for obj in module.__dict__.values():
            try:
                if issubclass(obj, SyncListGetEndpoint):
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

            async def __aiter__(self):
                """Asynchronously iterate over the provided items."""
                for item in self._items:
                    yield item

        from imednet.core.endpoint.base import AsyncListGetEndpoint

        for obj in module.__dict__.values():
            try:
                if issubclass(obj, AsyncListGetEndpoint):
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
    return Client("key", "secret", base_url="https://api.test")


@pytest_asyncio.fixture
async def async_http_client():
    """Provide an asynchronous AsyncClient instance configured for testing.

    This fixture ensures the client is properly closed after use.
    """
    client = AsyncClient("key", "secret", base_url="https://api.test")
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
