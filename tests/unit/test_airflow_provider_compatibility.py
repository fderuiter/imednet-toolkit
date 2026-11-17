"""Compatibility regressions for restored ImednetHook API with real Airflow dependencies.

Validates ImednetHook.get_conn() and ImednetHook.get_sdk_client() against the installed
Apache Airflow provider package without dummy module monkeypatching.
Ensures connection resolution is isolated from external networks and secrets managers,
verifies delegation and subclass override contracts, and cleans up created SDK instances.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

# Guard to isolate optional provider dependencies so core-only installs remain usable.
pytest.importorskip("airflow")

if TYPE_CHECKING:
    from imednet import ImednetSDK


@pytest.fixture(autouse=True, scope="module")
def _cleanup_airflow_provider_modules():
    """Unload provider modules upon module teardown to ensure other test suites stay hermetic."""
    yield
    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow")):
            sys.modules.pop(mod, None)


@pytest.fixture
def managed_sdk():
    """Track and cleanly close all created ImednetSDK instances during test teardown."""
    clients: list[Any] = []

    def _register(client: Any) -> Any:
        clients.append(client)
        return client

    yield _register

    for client in clients:
        try:
            client.close()
        except Exception:  # pragma: no cover
            pass


@pytest.fixture
def real_airflow():
    """Import and return real Airflow and provider components dynamically."""
    import airflow
    from airflow.models.connection import Connection

    try:
        from airflow.sdk.bases.hook import BaseHook
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - Airflow 2.x fallback
        from airflow.hooks.base import BaseHook

    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
    from imednet import ImednetSDK

    return {
        "airflow": airflow,
        "Connection": Connection,
        "BaseHook": BaseHook,
        "ImednetHook": ImednetHook,
        "ImednetExportOperator": ImednetExportOperator,
        "ImednetSDK": ImednetSDK,
    }


@pytest.fixture
def synthetic_connection(monkeypatch, real_airflow):
    """Provide a synthetic Airflow Connection isolated from real secrets and networks."""
    connection_cls = real_airflow["Connection"]
    base_hook_cls = real_airflow["BaseHook"]

    conn = connection_cls(
        conn_id="imednet_synthetic",
        conn_type="generic",
        login="synthetic-api-key",  # pragma: allowlist secret
        password="synthetic-security-key",  # pragma: allowlist secret
        extra=json.dumps({"base_url": "https://api.synthetic.example/v1"}),
    )
    monkeypatch.setattr(base_hook_cls, "get_connection", classmethod(lambda cls, conn_id: conn))
    return conn


def test_real_airflow_hook_get_conn_builds_sdk(real_airflow, synthetic_connection, managed_sdk):
    """Assert get_conn() builds an authenticated ImednetSDK instance from resolved connection."""
    hook_cls = real_airflow["ImednetHook"]
    sdk_cls = real_airflow["ImednetSDK"]

    hook = hook_cls(imednet_conn_id=synthetic_connection.conn_id)
    client = managed_sdk(hook.get_conn())

    assert isinstance(client, sdk_cls)
    assert client.config.api_key == "synthetic-api-key"  # pragma: allowlist secret
    assert client.config.security_key == "synthetic-security-key"  # pragma: allowlist secret
    assert str(client.config.base_url).rstrip("/") == "https://api.synthetic.example/v1"


def test_real_airflow_hook_get_conn_with_json_extra(monkeypatch, real_airflow, managed_sdk):
    """Assert connection resolution parses config when credentials are provided in extra dict."""
    connection_cls = real_airflow["Connection"]
    base_hook_cls = real_airflow["BaseHook"]
    hook_cls = real_airflow["ImednetHook"]
    sdk_cls = real_airflow["ImednetSDK"]

    conn = connection_cls(
        conn_id="imednet_extra_config",
        conn_type="generic",
        extra=json.dumps(
            {
                "api_key": "extra-api-key",  # pragma: allowlist secret
                "security_key": "extra-sec-key",  # pragma: allowlist secret
                "base_url": "https://extra.synthetic.example/api",
            }
        ),
    )
    monkeypatch.setattr(base_hook_cls, "get_connection", classmethod(lambda cls, conn_id: conn))

    hook = hook_cls(imednet_conn_id="imednet_extra_config")
    client = managed_sdk(hook.get_conn())

    assert isinstance(client, sdk_cls)
    assert client.config.api_key == "extra-api-key"  # pragma: allowlist secret
    assert client.config.security_key == "extra-sec-key"  # pragma: allowlist secret
    assert str(client.config.base_url).rstrip("/") == "https://extra.synthetic.example/api"


def test_real_airflow_hook_get_sdk_client_delegates_exactly_once(
    real_airflow, synthetic_connection, managed_sdk
):
    """Assert get_sdk_client() delegates to get_conn() exactly once."""
    hook_cls = real_airflow["ImednetHook"]
    sdk_cls = real_airflow["ImednetSDK"]

    hook = hook_cls(imednet_conn_id=synthetic_connection.conn_id)

    with patch.object(hook, "get_conn", wraps=hook.get_conn) as spy_get_conn:
        client = managed_sdk(hook.get_sdk_client())

        assert spy_get_conn.call_count == 1
        assert isinstance(client, sdk_cls)
        assert client.config.api_key == "synthetic-api-key"  # pragma: allowlist secret


def test_real_airflow_export_operator_normal_resolution(
    real_airflow, synthetic_connection, managed_sdk
):
    """Cover the normal hook resolution path of ImednetExportOperator._get_sdk()."""
    operator_cls = real_airflow["ImednetExportOperator"]
    sdk_cls = real_airflow["ImednetSDK"]

    operator = operator_cls(
        task_id="test_export_task",
        study_key="STUDY_COMPAT_01",
        imednet_conn_id=synthetic_connection.conn_id,
        output_path="/tmp/output.csv",
    )

    sdk = managed_sdk(operator._get_sdk())
    assert isinstance(sdk, sdk_cls)
    assert sdk.config.api_key == "synthetic-api-key"  # pragma: allowlist secret
    assert sdk.config.security_key == "synthetic-security-key"  # pragma: allowlist secret


def test_real_airflow_hook_subclass_override_get_sdk_client(real_airflow, managed_sdk):
    """Cover existing subclass overrides that specialize get_sdk_client()."""
    hook_cls = real_airflow["ImednetHook"]
    sdk_cls = real_airflow["ImednetSDK"]

    custom_client = managed_sdk(
        sdk_cls(
            api_key="custom-sdk-key",  # pragma: allowlist secret
            security_key="custom-sdk-sec",  # pragma: allowlist secret
            base_url="https://custom.example.com",
        )
    )

    class CustomImednetHook(hook_cls):
        """Custom hook subclass overriding get_sdk_client."""

        def get_sdk_client(self) -> ImednetSDK:
            return custom_client

    hook = CustomImednetHook(imednet_conn_id="any_conn")
    resolved = hook.get_sdk_client()
    assert resolved is custom_client
    assert resolved.config.api_key == "custom-sdk-key"  # pragma: allowlist secret


def test_real_airflow_hook_subclass_override_get_conn(real_airflow, managed_sdk):
    """Cover subclass overrides of get_conn() and verify get_sdk_client() inherits them."""
    hook_cls = real_airflow["ImednetHook"]
    sdk_cls = real_airflow["ImednetSDK"]

    custom_client = managed_sdk(
        sdk_cls(
            api_key="custom-conn-key",  # pragma: allowlist secret
            security_key="custom-conn-sec",  # pragma: allowlist secret
            base_url="https://custom-conn.example.com",
        )
    )

    class CustomConnHook(hook_cls):
        """Custom hook subclass overriding get_conn."""

        def get_conn(self) -> ImednetSDK:
            return custom_client

    hook = CustomConnHook(imednet_conn_id="any_conn")
    # Calling get_conn directly should return the overridden instance.
    assert hook.get_conn() is custom_client
    # Calling get_sdk_client should delegate to overridden get_conn.
    assert hook.get_sdk_client() is custom_client


def test_real_airflow_export_operator_subclass_resolution(real_airflow, managed_sdk):
    """Cover operator subclass hook resolution when overriding _get_sdk()."""
    operator_cls = real_airflow["ImednetExportOperator"]
    sdk_cls = real_airflow["ImednetSDK"]

    custom_client = managed_sdk(
        sdk_cls(
            api_key="operator-custom-key",  # pragma: allowlist secret
            security_key="operator-custom-sec",  # pragma: allowlist secret
            base_url="https://operator.example.com",
        )
    )

    class CustomExportOperator(operator_cls):
        """Operator subclass overriding _get_sdk."""

        def _get_sdk(self) -> ImednetSDK:
            return custom_client

    operator = CustomExportOperator(
        task_id="custom_operator_task",
        study_key="STUDY_SUBCLASS_01",
    )
    assert operator._get_sdk() is custom_client


def test_real_airflow_sdk_client_cleanup(real_airflow, synthetic_connection):
    """Verify that SDK clients created from hooks close their underlying transports cleanly."""
    hook_cls = real_airflow["ImednetHook"]
    hook = hook_cls(imednet_conn_id=synthetic_connection.conn_id)
    client = hook.get_conn()

    assert not client._client._client.is_closed
    client.close()
    assert client._client._client.is_closed


def test_real_airflow_hook_describe_connection_redaction(real_airflow, synthetic_connection):
    """Verify that describe_connection() redacts sensitive credentials."""
    hook_cls = real_airflow["ImednetHook"]
    hook = hook_cls(imednet_conn_id=synthetic_connection.conn_id)
    desc = hook.describe_connection()

    assert desc["imednet_conn_id"] == synthetic_connection.conn_id
    assert desc["api_key"] == "***"
    assert desc["security_key"] == "***"
    assert desc["api_key_configured"] is True
    assert desc["security_key_configured"] is True
