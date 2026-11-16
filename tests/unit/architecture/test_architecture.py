"""Unit tests for architecture."""

from pathlib import Path

import pytest
from httpx import AsyncClient, Client

from imednet.sdk import AsyncImednetSDK, ImednetSDK


@pytest.mark.parametrize(
    ("distribution", "package"),
    [
        ("core", "imednet"),
        ("plugins-workflows", "imednet_workflows"),
        ("plugins-sinks", "imednet_sinks"),
        ("providers-airflow", "apache_airflow_providers_imednet"),
    ],
)
def test_package_boundaries(distribution, package):
    from import_rules import scan_package

    root = Path(__file__).resolve().parents[3]
    errors = scan_package(root / "packages" / distribution / "src", package)
    assert not errors, "\n".join(errors)


def test_endpoint_no_shared_mutable_state():
    """Test that endpoint no shared mutable state."""
    sdk = ImednetSDK(api_key="1", security_key="2", base_url="http://x")

    assert sdk.records is not sdk.forms

    if hasattr(sdk.records, "_cache"):
        assert sdk.records._cache is not getattr(sdk.forms, "_cache", None)

    # Let's instantiate another SDK and ensure endpoint instances are distinct
    sdk2 = ImednetSDK(api_key="1", security_key="2", base_url="http://x")
    assert sdk.records is not sdk2.records
    assert sdk._client is not sdk2._client


def test_sync_sdk_no_async_client(monkeypatch):
    """Test that sync sdk no async client."""
    instantiated = False
    original_init = AsyncClient.__init__

    def mock_init(self, *args, **kwargs):
        """Helper function to mock init."""
        nonlocal instantiated
        instantiated = True
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(AsyncClient, "__init__", mock_init)

    with ImednetSDK(api_key="1", security_key="2", base_url="http://x"):
        pass
    assert not instantiated, "ImednetSDK should not instantiate AsyncClient"


def test_async_sdk_no_sync_client(monkeypatch):
    """Test that async sdk no sync client."""
    instantiated = False
    original_init = Client.__init__

    def mock_init(self, *args, **kwargs):
        """Helper function to mock init."""
        nonlocal instantiated
        instantiated = True
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(Client, "__init__", mock_init)

    AsyncImednetSDK(api_key="1", security_key="2", base_url="http://x")
    assert not instantiated, "AsyncImednetSDK should not instantiate Client"


def test_plugin_discovery_failure(monkeypatch):
    """Test that plugin discovery failure."""
    monkeypatch.setattr("imednet.sdk.entry_points", lambda *, group, name=None: [])

    sdk = ImednetSDK(api_key="1", security_key="2", base_url="http://x")
    with pytest.raises(
        ImportError,
        match="Workflow 'some_workflow' not found. Please install the required package.",
    ):
        sdk.workflows.some_workflow
