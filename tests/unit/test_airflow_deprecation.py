"""Unit tests for airflow deprecation."""

import sys
from types import ModuleType


def _setup_airflow(monkeypatch):
    """Helper function to  setup airflow."""
    airflow_mod = ModuleType("airflow")
    hooks_pkg = ModuleType("airflow.hooks")
    hooks_mod = ModuleType("airflow.sdk.bases.hook")
    sdk_mod = ModuleType("airflow.sdk")
    sdk_bases = ModuleType("airflow.sdk.bases")
    sdk_defs = ModuleType("airflow.sdk.definitions")
    sdk_ctx = ModuleType("airflow.sdk.definitions.context")
    models_mod = ModuleType("airflow.models")

    class DummyBaseHook:
        """Test suite for DummyBaseHook."""

        @classmethod
        def get_connection(cls, conn_id):  # pragma: no cover
            """Helper function to get connection."""
            raise NotImplementedError

    class DummyBaseOperator:
        """Test suite for DummyBaseOperator."""

        template_fields = ()

        def __init__(self, **kwargs):  # pragma: no cover
            """Initialize the test object."""

    hooks_mod.BaseHook = DummyBaseHook
    models_mod.BaseOperator = DummyBaseOperator

    sdk_bases.hook = hooks_mod
    sdk_mod.bases = sdk_bases
    sdk_defs.context = sdk_ctx
    sdk_mod.definitions = sdk_defs
    airflow_mod.sdk = sdk_mod
    airflow_mod.hooks = hooks_pkg
    airflow_mod.models = models_mod

    monkeypatch.setitem(sys.modules, "airflow", airflow_mod)
    monkeypatch.setitem(sys.modules, "airflow.hooks", hooks_pkg)
    monkeypatch.setitem(sys.modules, "airflow.sdk.bases.hook", hooks_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk.bases", sdk_bases)
    monkeypatch.setitem(sys.modules, "airflow.sdk.definitions", sdk_defs)
    monkeypatch.setitem(sys.modules, "airflow.sdk.definitions.context", sdk_ctx)
    monkeypatch.setitem(sys.modules, "airflow.models", models_mod)


def test_airflow_provider_exports_public_api(monkeypatch):
    """Test that airflow provider exports public api."""
    _setup_airflow(monkeypatch)
    for mod in [
        "apache_airflow_providers_imednet",
        "apache_airflow_providers_imednet.hooks",
        "apache_airflow_providers_imednet.operators",
        "apache_airflow_providers_imednet.sensors",
    ]:
        monkeypatch.delitem(sys.modules, mod, raising=False)

    import apache_airflow_providers_imednet as provider

    assert provider.ImednetHook is not None
    assert provider.ImednetExportOperator is not None
