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
    import logging

    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    saved_level = root_logger.level

    airflow_mod = sys.modules.get("airflow")
    if (
        airflow_mod is None
        or getattr(airflow_mod, "__path__", None) is None
        or getattr(airflow_mod, "__file__", None) is None
    ):
        for mod in list(sys.modules):
            if mod == "airflow" or mod.startswith(("airflow.", "apache_airflow_providers_imednet")):
                sys.modules.pop(mod, None)

    yield
    for mod in list(sys.modules):
        if mod.startswith(("apache_airflow_providers_imednet", "airflow")):
            sys.modules.pop(mod, None)
    root_logger.handlers = saved_handlers
    root_logger.setLevel(saved_level)


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


def test_real_airflow_providers_manager_and_task_runtime_discover_imednet(real_airflow):
    """Verify ProvidersManager and ProvidersManagerTaskRuntime discover apache-airflow-providers-imednet and ImednetHook."""
    from airflow.providers_manager import ProvidersManager
    from airflow.sdk.providers_manager_runtime import ProvidersManagerTaskRuntime

    hook_cls = real_airflow["ImednetHook"]

    pm = ProvidersManager()
    pm._cleanup()
    providers = pm.providers
    assert "apache-airflow-providers-imednet" in providers
    assert providers["apache-airflow-providers-imednet"].data["name"] == "iMednet"

    hooks = pm.hooks
    assert "imednet" in hooks
    assert hooks["imednet"] is not None
    assert hooks["imednet"].connection_type == "imednet"
    assert hooks["imednet"].hook_name == "iMednet"
    assert hooks["imednet"].hook_class_name == "airflow.providers.imednet.hooks.ImednetHook"

    from airflow.providers.imednet.hooks import ImednetHook as AliasedImednetHook

    assert AliasedImednetHook is hook_cls

    runtime_pm = ProvidersManagerTaskRuntime()
    runtime_pm._cleanup()
    runtime_hooks = runtime_pm.hooks
    assert "imednet" in runtime_hooks
    assert runtime_hooks["imednet"] is not None
    assert runtime_hooks["imednet"].connection_type == "imednet"


def test_real_airflow_provider_alias_stays_synced_across_reimports(real_airflow):
    """Re-importing apache_airflow_providers_imednet refreshes stale airflow.providers.imednet aliases."""
    import importlib
    from types import ModuleType

    # Simulate stale alias left behind after canonical package reload/eviction
    stale_hooks = ModuleType("airflow.providers.imednet.hooks")
    stale_hooks.ImednetHook = object  # type: ignore[attr-defined]
    sys.modules["airflow.providers.imednet.hooks"] = stale_hooks
    for mod in list(sys.modules):
        if mod.startswith("apache_airflow_providers_imednet"):
            sys.modules.pop(mod, None)

    importlib.import_module("apache_airflow_providers_imednet")
    from airflow.providers.imednet.hooks import ImednetHook as AliasedHook

    from apache_airflow_providers_imednet.hooks import ImednetHook as CanonicalHook

    assert AliasedHook is CanonicalHook


def test_real_airflow_hook_resolves_connection_host_scheme_port_and_precedence(
    monkeypatch,
    real_airflow,
    managed_sdk,
):
    """ImednetHook honors explicit base_url extras first, then Connection.host/scheme/port."""
    connection_cls = real_airflow["Connection"]
    base_hook_cls = real_airflow["BaseHook"]
    hook_cls = real_airflow["ImednetHook"]

    # 1. Connection(host="edc.trn.imednetapi.com") routes to training endpoint
    trn_conn = connection_cls(
        conn_id="imednet_trn",
        conn_type="imednet",
        host="edc.trn.imednetapi.com",
        login="trn-key",  # pragma: allowlist secret
        password="trn-secret",  # pragma: allowlist secret
    )
    monkeypatch.setattr(base_hook_cls, "get_connection", classmethod(lambda cls, cid: trn_conn))
    trn_sdk = managed_sdk(hook_cls("imednet_trn").get_conn())
    assert str(trn_sdk.config.base_url).rstrip("/") == "https://edc.trn.imednetapi.com"
    assert trn_sdk._client.base_url == "https://edc.trn.imednetapi.com"

    # 2. Connection host with /api/v1/edc suffix and custom scheme/port
    custom_conn = connection_cls(
        conn_id="imednet_custom_host",
        conn_type="http",
        host="edc.trn.imednetapi.com/api/v1/edc/",
        port=8443,
        login="trn-key",  # pragma: allowlist secret
        password="trn-secret",  # pragma: allowlist secret
    )
    monkeypatch.setattr(base_hook_cls, "get_connection", classmethod(lambda cls, cid: custom_conn))
    custom_sdk = managed_sdk(hook_cls("imednet_custom_host").get_conn())
    assert str(custom_sdk.config.base_url).rstrip("/") == "http://edc.trn.imednetapi.com:8443"
    assert custom_sdk._client.base_url == "http://edc.trn.imednetapi.com:8443"

    # 3. Explicit extra["base_url"] takes precedence over Connection.host and strips /api/v1/edc
    override_conn = connection_cls(
        conn_id="imednet_override",
        conn_type="imednet",
        host="edc.prod.imednetapi.com",
        login="trn-key",  # pragma: allowlist secret
        password="trn-secret",  # pragma: allowlist secret
        extra=json.dumps({"base_url": "https://edc.trn.imednetapi.com/api/v1/edc"}),
    )
    monkeypatch.setattr(
        base_hook_cls, "get_connection", classmethod(lambda cls, cid: override_conn)
    )
    override_sdk = managed_sdk(hook_cls("imednet_override").get_conn())
    assert str(override_sdk.config.base_url).rstrip("/") == "https://edc.trn.imednetapi.com"
    assert override_sdk._client.base_url == "https://edc.trn.imednetapi.com"


def test_real_airflow_sqlite_destination_uris_work_with_sqlalchemy(
    monkeypatch,
    real_airflow,
    tmp_path,
):
    """resolve_destination_connection emits valid SQLAlchemy SQLite URLs for absolute & relative paths."""
    import sqlalchemy as sa

    connection_cls = real_airflow["Connection"]
    base_hook_cls = real_airflow["BaseHook"]
    hook_cls = real_airflow["ImednetHook"]

    abs_db = tmp_path / "warehouse_abs.db"
    uri_abs_db = tmp_path / "warehouse_uri_abs.db"
    rel_name = "warehouse_rel.db"

    cases = [
        (
            "sqlite_host_abs",
            connection_cls(conn_id="sqlite_host_abs", conn_type="sqlite", host=str(abs_db)),
            f"sqlite:///{abs_db}",
            abs_db,
        ),
        (
            "sqlite_uri_abs",
            connection_cls(conn_id="sqlite_uri_abs", uri=f"sqlite:///{uri_abs_db}"),
            f"sqlite:///{uri_abs_db}",
            uri_abs_db,
        ),
        (
            "sqlite_host_rel",
            connection_cls(conn_id="sqlite_host_rel", conn_type="sqlite", host=rel_name),
            f"sqlite:///{rel_name}",
            tmp_path / rel_name,
        ),
    ]

    monkeypatch.chdir(tmp_path)
    for conn_id, conn_obj, expected_uri, expected_file in cases:
        monkeypatch.setattr(base_hook_cls, "get_connection", classmethod(lambda cls, cid: conn_obj))
        resolved = hook_cls.resolve_destination_connection(conn_id, "sql")
        assert resolved["conn_str"] == expected_uri

        engine = sa.create_engine(str(resolved["conn_str"]))
        try:
            with engine.begin() as db_conn:
                db_conn.execute(sa.text("CREATE TABLE records (id INTEGER PRIMARY KEY)"))
                db_conn.execute(sa.text("INSERT INTO records (id) VALUES (1)"))
        finally:
            engine.dispose()
        assert expected_file.exists()


def test_real_airflow_hook_test_connection_and_provider_discovery(monkeypatch, real_airflow):
    """ProvidersManager reports connection_testable=True and test_connection() redacts sensitive errors."""
    import httpx
    from airflow.providers_manager import ProvidersManager
    from airflow.sdk.providers_manager_runtime import ProvidersManagerTaskRuntime

    connection_cls = real_airflow["Connection"]
    base_hook_cls = real_airflow["BaseHook"]
    hook_cls = real_airflow["ImednetHook"]
    sdk_cls = real_airflow["ImednetSDK"]

    pm = ProvidersManager()
    pm._cleanup()
    pm_runtime = ProvidersManagerTaskRuntime()
    pm_runtime._cleanup()
    assert "apache-airflow-providers-imednet" in pm.providers
    hook_meta = pm_runtime.hooks.get("imednet")
    assert hook_meta is not None
    assert hook_meta.connection_type == "imednet"
    assert hook_meta.connection_testable is True

    conn = connection_cls(
        conn_id="imednet_test_conn",
        conn_type="imednet",
        host="edc.trn.imednetapi.com",
        login="SUPER_SECRET_API_KEY",  # pragma: allowlist secret
        password="SUPER_SECRET_SEC_KEY",  # pragma: allowlist secret
    )
    discovered_hook = conn.get_hook()
    assert isinstance(discovered_hook, hook_cls)

    monkeypatch.setattr(base_hook_cls, "get_connection", classmethod(lambda cls, cid: conn))

    # 1. Successful connection test
    ok_sdk = sdk_cls(
        api_key="k",
        security_key="s",
        base_url="https://edc.trn.imednetapi.com",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"data": [], "pagination": {"totalPages": 1}})
        ),
    )
    hook_ok = hook_cls("imednet_test_conn")
    monkeypatch.setattr(hook_ok, "get_sdk_client", lambda: ok_sdk)
    ok, msg = hook_ok.test_connection()
    assert ok is True
    assert "successfully" in msg.lower()

    # 2. Failed connection test redacts credentials
    def _failing_sdk():
        raise RuntimeError(
            "Auth failed for https://user:SUPER_SECRET_SEC_KEY@edc.trn.imednetapi.com"  # pragma: allowlist secret
            "?api_key=SUPER_SECRET_API_KEY"  # pragma: allowlist secret
        )

    hook_fail = hook_cls("imednet_test_conn")
    monkeypatch.setattr(hook_fail, "get_sdk_client", _failing_sdk)
    ok_fail, err_msg = hook_fail.test_connection()
    assert ok_fail is False
    assert "SUPER_SECRET_API_KEY" not in err_msg
    assert "SUPER_SECRET_SEC_KEY" not in err_msg
