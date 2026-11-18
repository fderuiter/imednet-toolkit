"""Unit tests for airflow integration."""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

import imednet.sdk as sdk_mod


def _setup_airflow(monkeypatch):
    """Helper function to  setup airflow."""
    airflow_mod = ModuleType("airflow")
    airflow_mod.__path__ = []
    hooks_pkg = ModuleType("airflow.hooks")
    hooks_pkg.__path__ = []
    hooks_mod = ModuleType("airflow.sdk.bases.hook")
    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.__path__ = []
    sdk_bases = ModuleType("airflow.sdk.bases")
    sdk_bases.__path__ = []
    sdk_defs = ModuleType("airflow.sdk.definitions")
    sdk_defs.__path__ = []
    sdk_ctx = ModuleType("airflow.sdk.definitions.context")
    sdk_dag = ModuleType("airflow.sdk.definitions.dag")
    models_mod = ModuleType("airflow.models")

    class DummyBaseHook:
        """Test suite for DummyBaseHook."""

        @classmethod
        def get_connection(cls, conn_id):
            """Helper function to get connection."""
            raise NotImplementedError

    class DummyBaseOperator:
        """Test suite for DummyBaseOperator."""

        template_fields = ()

        def __init__(self, **kwargs):
            """Initialize the test object."""

    hooks_mod.BaseHook = DummyBaseHook
    models_mod.BaseOperator = DummyBaseOperator
    models_mod.DAG = DummyBaseOperator
    sdk_dag.DAG = DummyBaseOperator
    sdk_dag.dag = DummyBaseOperator

    sdk_bases.hook = hooks_mod
    sdk_mod.bases = sdk_bases
    sdk_defs.context = sdk_ctx
    sdk_defs.dag = sdk_dag
    sdk_mod.definitions = sdk_defs
    airflow_mod.sdk = sdk_mod
    airflow_mod.hooks = hooks_pkg
    airflow_mod.models = models_mod

    monkeypatch.setitem(sys.modules, "airflow", airflow_mod)
    monkeypatch.setitem(sys.modules, "airflow.hooks", hooks_pkg)
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk.bases", sdk_bases)
    monkeypatch.setitem(sys.modules, "airflow.sdk.bases.hook", hooks_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk.definitions", sdk_defs)
    monkeypatch.setitem(sys.modules, "airflow.sdk.definitions.context", sdk_ctx)
    monkeypatch.setitem(sys.modules, "airflow.sdk.definitions.dag", sdk_dag)
    monkeypatch.setitem(sys.modules, "airflow.models", models_mod)


def test_imednet_hook_returns_sdk(monkeypatch):
    """Test that imednet hook returns sdk."""
    _setup_airflow(monkeypatch)

    conn = MagicMock()
    conn.login = "KEY"
    conn.password = "SEC"
    conn.extra_dejson = {"base_url": "https://example.com"}

    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    import apache_airflow_providers_imednet as airflow_mod

    hook = airflow_mod.ImednetHook()
    sdk = hook.get_sdk_client()

    assert isinstance(sdk, sdk_mod.ImednetSDK)
    assert sdk._client._client.headers["x-api-key"] == "KEY"
    assert sdk._client._client.headers["x-imn-security-key"] == "SEC"
    assert sdk._client.base_url == "https://example.com"


def test_export_operator_calls_helper(monkeypatch):
    """Test that export operator calls helper."""
    if "apache_airflow_providers_imednet" in sys.modules:
        del sys.modules["apache_airflow_providers_imednet"]

    _setup_airflow(monkeypatch)

    conn = MagicMock()
    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    sdk = MagicMock()
    hook_inst = MagicMock(get_sdk_client=MagicMock(return_value=sdk))

    import apache_airflow_providers_imednet as airflow_mod
    import apache_airflow_providers_imednet.operators.export as export_ops

    monkeypatch.setattr(export_ops, "ImednetHook", MagicMock(return_value=hook_inst))
    export_mock = MagicMock()
    monkeypatch.setattr(airflow_mod.export, "export_to_csv", export_mock)

    op = airflow_mod.ImednetExportOperator(
        task_id="t",
        study_key="S",
        output_path="/tmp/out.csv",
        export_func="export_to_csv",
    )

    result = op.execute({})

    export_mock.assert_called_once_with(sdk, "S", "/tmp/out.csv")
    assert result == "/tmp/out.csv"


def test_export_operator_exposes_mapped_runtime_fields(monkeypatch):
    """Test that export operator exposes mapped runtime fields."""
    if "apache_airflow_providers_imednet" in sys.modules:
        del sys.modules["apache_airflow_providers_imednet"]

    _setup_airflow(monkeypatch)

    from apache_airflow_providers_imednet import ImednetExportOperator

    assert ImednetExportOperator.mapped_runtime_fields == (
        "study_key",
        "output_path",
        "export_kwargs",
    )
    assert ImednetExportOperator.template_fields == ImednetExportOperator.mapped_runtime_fields
    assert ImednetExportOperator.template_fields_renderers == {"export_kwargs": "json"}


def test_export_operator_copies_runtime_kwargs_and_resolves_sdk_at_execute(monkeypatch):
    """Test that export operator copies runtime kwargs and resolves sdk at execute."""
    if "apache_airflow_providers_imednet" in sys.modules:
        del sys.modules["apache_airflow_providers_imednet"]

    _setup_airflow(monkeypatch)

    conn = MagicMock()
    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    sdk = MagicMock()
    hook_inst = MagicMock(get_sdk_client=MagicMock(return_value=sdk))

    import apache_airflow_providers_imednet as airflow_mod
    import apache_airflow_providers_imednet.operators.export as export_ops

    hook_cls = MagicMock(return_value=hook_inst)
    monkeypatch.setattr(export_ops, "ImednetHook", hook_cls)
    export_mock = MagicMock()
    monkeypatch.setattr(airflow_mod.export, "export_to_json", export_mock)

    export_kwargs = {"orient": "records"}
    op = airflow_mod.ImednetExportOperator(
        task_id="t",
        study_key="S",
        output_path="/tmp/out.json",
        export_func="export_to_json",
        export_kwargs=export_kwargs,
    )

    export_kwargs["orient"] = "split"
    result = op.execute({})

    hook_cls.assert_called_once_with("imednet_default")
    hook_inst.get_sdk_client.assert_called_once_with()
    export_mock.assert_called_once_with(sdk, "S", "/tmp/out.json", orient="records")
    assert result == "/tmp/out.json"


def test_export_operator_rejects_unknown_export_callable(monkeypatch):
    """Test that export operator rejects unknown export callable."""
    if "apache_airflow_providers_imednet" in sys.modules:
        del sys.modules["apache_airflow_providers_imednet"]

    _setup_airflow(monkeypatch)

    conn = MagicMock()
    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    from apache_airflow_providers_imednet import ImednetExportOperator
    from apache_airflow_providers_imednet._airflow_compat import AirflowException

    op = ImednetExportOperator(
        task_id="t",
        study_key="S",
        output_path="/tmp/out.csv",
        export_func="does_not_exist",
    )

    with pytest.raises(AirflowException, match="Unsupported export_func"):
        op.execute({})


def test_imednet_hook_non_dict_extras(monkeypatch):
    """Test that imednet hook non dict extras."""
    _setup_airflow(monkeypatch)

    conn = MagicMock()
    conn.login = "KEY"
    conn.password = "SEC"
    conn.extra_dejson = "not-a-dict"

    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    from apache_airflow_providers_imednet.hooks import ImednetHook

    hook = ImednetHook()
    sdk = hook.get_sdk_client()

    assert sdk._client._client.headers["x-api-key"] == "KEY"
    assert sdk._client._client.headers["x-imn-security-key"] == "SEC"


def test_imednet_hook_get_extra_json_fallback(monkeypatch):
    """Test that imednet hook get extra json fallback."""
    _setup_airflow(monkeypatch)

    conn = MagicMock()
    conn.login = None
    conn.password = None
    conn.extra_dejson = None
    conn.get_extra = MagicMock(
        return_value=(
            '{"api_key":"GET_EXTRA_KEY","security_key":"GET_EXTRA_SEC","base_url":"https://get-extra"}'
        )
    )

    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    from apache_airflow_providers_imednet.hooks import ImednetHook

    hook = ImednetHook()
    sdk = hook.get_sdk_client()

    assert sdk._client._client.headers["x-api-key"] == "GET_EXTRA_KEY"
    assert sdk._client._client.headers["x-imn-security-key"] == "GET_EXTRA_SEC"
    assert sdk._client.base_url == "https://get-extra"
    conn.get_extra.assert_called_once_with()


def test_imednet_hook_extra_json_string_fallback(monkeypatch):
    """Test that imednet hook extra json string fallback."""
    _setup_airflow(monkeypatch)

    conn = MagicMock()
    conn.login = None
    conn.password = None
    conn.extra_dejson = None
    conn.get_extra = MagicMock(return_value="not-json")
    conn.extra = (
        '{"api_key":"EXTRA_FIELD_KEY","security_key":"EXTRA_FIELD_SEC","base_url":"https://extra"}'
    )

    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    from apache_airflow_providers_imednet.hooks import ImednetHook

    hook = ImednetHook()
    sdk = hook.get_sdk_client()

    assert sdk._client._client.headers["x-api-key"] == "EXTRA_FIELD_KEY"
    assert sdk._client._client.headers["x-imn-security-key"] == "EXTRA_FIELD_SEC"
    assert sdk._client.base_url == "https://extra"
    conn.get_extra.assert_called_once_with()


def test_imednet_hook_non_string_login(monkeypatch):
    """Test that imednet hook non string login."""
    _setup_airflow(monkeypatch)

    conn = MagicMock()
    conn.login = 123
    conn.password = "SEC"
    conn.extra_dejson = {"base_url": "https://example.com", "api_key": "FALLBACK_KEY"}

    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    from apache_airflow_providers_imednet.hooks import ImednetHook

    hook = ImednetHook()
    sdk = hook.get_sdk_client()
    assert sdk._client._client.headers["x-api-key"] == "FALLBACK_KEY"
    assert sdk._client._client.headers["x-imn-security-key"] == "SEC"


def test_imednet_hook_non_string_password(monkeypatch):
    """Test that imednet hook non string password."""
    _setup_airflow(monkeypatch)

    conn = MagicMock()
    conn.login = "KEY"
    conn.password = 123
    conn.extra_dejson = {
        "base_url": "https://example.com",
        "security_key": "FALLBACK_SEC",
    }

    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    from apache_airflow_providers_imednet.hooks import ImednetHook

    hook = ImednetHook()
    sdk = hook.get_sdk_client()
    assert sdk._client._client.headers["x-api-key"] == "KEY"
    assert sdk._client._client.headers["x-imn-security-key"] == "FALLBACK_SEC"


def test_imednet_hook_environment_fallback(monkeypatch):
    """Test that imednet hook environment fallback."""
    _setup_airflow(monkeypatch)
    monkeypatch.setenv("IMEDNET_API_KEY", "ENV_KEY")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "ENV_SEC")

    conn = MagicMock()
    conn.login = None
    conn.password = None
    # Intentionally invalid non-string extras to verify env fallback normalization.
    conn.extra_dejson = {"api_key": 123, "security_key": object(), "base_url": 456}

    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    from apache_airflow_providers_imednet.hooks import ImednetHook

    hook = ImednetHook()
    sdk = hook.get_sdk_client()
    assert sdk._client._client.headers["x-api-key"] == "ENV_KEY"
    assert sdk._client._client.headers["x-imn-security-key"] == "ENV_SEC"
    assert sdk._client.base_url != "456"


def test_imednet_hook_describe_connection_redacts_credentials(monkeypatch):
    """Test that imednet hook describe connection redacts credentials."""
    _setup_airflow(monkeypatch)

    conn = MagicMock()
    conn.login = "KEY"
    conn.password = "SEC"
    conn.extra_dejson = {"base_url": "https://example.com", "api_key": "EXTRA_KEY"}

    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    from apache_airflow_providers_imednet.hooks import ImednetHook

    hook = ImednetHook("imednet_custom")
    metadata = hook.describe_connection()

    assert metadata == {
        "imednet_conn_id": "imednet_custom",
        "base_url": "https://example.com",
        "api_key": "***",
        "security_key": "***",
        "api_key_configured": True,
        "security_key_configured": True,
    }


def test_imednet_hook_prefers_extras_over_environment(monkeypatch):
    """Test that imednet hook prefers extras over environment."""
    _setup_airflow(monkeypatch)
    monkeypatch.setenv("IMEDNET_API_KEY", "ENV_KEY")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "ENV_SEC")

    conn = MagicMock()
    conn.login = "LOGIN_KEY"
    conn.password = "LOGIN_SEC"
    conn.extra_dejson = {"api_key": "EXTRA_KEY", "security_key": "EXTRA_SEC"}

    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(
        hooks_base.BaseHook,
        "get_connection",
        classmethod(lambda cls, cid: conn),
    )

    from apache_airflow_providers_imednet.hooks import ImednetHook

    hook = ImednetHook()
    sdk = hook.get_sdk_client()
    assert sdk._client._client.headers["x-api-key"] == "EXTRA_KEY"
    assert sdk._client._client.headers["x-imn-security-key"] == "EXTRA_SEC"


def test_imednet_hook_study_discovery_serialization_safe(monkeypatch):
    """Test that imednet hook study discovery serialization safe."""
    _setup_airflow(monkeypatch)

    from apache_airflow_providers_imednet.hooks import ImednetHook

    class _StudyModel:
        """Test suite for  StudyModel."""

        def model_dump(self, mode="json", by_alias=True):
            """Helper function to model dump."""
            assert mode == "json"
            assert by_alias is True
            return {
                "studyKey": "S-001",
                "dateModified": datetime(2025, 1, 1, tzinfo=timezone.utc),
                "api_key": "SECRET",
                "flags": ("a", "b"),
            }

    sdk = MagicMock()
    sdk.studies.list.return_value = [_StudyModel()]

    hook = ImednetHook()
    monkeypatch.setattr(hook, "get_sdk_client", MagicMock(return_value=sdk))

    metadata = hook.list_studies_metadata()
    keys = hook.list_study_keys()

    assert metadata == [
        {
            "studyKey": "S-001",
            "dateModified": "2025-01-01T00:00:00+00:00",
            "api_key": "***",
            "flags": ["a", "b"],
        }
    ]
    assert keys == ["S-001"]


# Anchored at repo root so the path stays valid regardless of test-runner CWD.
_EXAMPLES_AIRFLOW_DIR = Path(__file__).parent.parent.parent / "examples" / "airflow"


def _setup_airflow_for_dag(monkeypatch):
    """Extended Airflow mock that supports DAG context managers and task decorators.

    Used to load example DAG modules (e.g. multi_study_pipeline.py) without a
    live Airflow installation.
    """
    _setup_airflow(monkeypatch)

    airflow_mod = sys.modules["airflow"]

    class _DummyDAG:
        """Test suite for  DummyDAG."""

        def __init__(self, *args, **kwargs):
            """Initialize the test object."""

        def __enter__(self):
            """Helper function to   enter  ."""
            return self

        def __exit__(self, *args):
            """Helper function to   exit  ."""

    class _DummyTaskDecorator:
        """Thin shim for ``@task`` and ``@task(...)``."""

        def __call__(self, *args, **kwargs):
            """Helper function to   call  ."""
            if len(args) == 1 and callable(args[0]):
                # bare @task usage
                return args[0]

            # @task(...) — return a no-op decorator
            def _decorator(fn):
                """Helper function to  decorator."""
                return fn

            return _decorator

    decorators_mod = ModuleType("airflow.decorators")
    decorators_mod.task = _DummyTaskDecorator()
    airflow_mod.DAG = _DummyDAG
    airflow_mod.decorators = decorators_mod
    airflow_mod.sdk.task = _DummyTaskDecorator()
    monkeypatch.setitem(sys.modules, "airflow.decorators", decorators_mod)

    class _DummyOperator:
        """Test suite for  DummyOperator."""

        template_fields = ()
        # Mirrors ImednetExportOperator.mapped_runtime_fields: these are the fields
        # that Airflow sets at runtime (via template rendering / expand_kwargs) and
        # that the operator must read from self at execute time, not from __init__.
        mapped_runtime_fields = ("study_key", "output_path", "export_kwargs")

        def __init__(self, **kwargs):
            """Initialize the test object."""

        @classmethod
        def partial(cls, **kwargs):
            """Helper function to partial."""

            class _PartialOp:
                """Test suite for  PartialOp."""

                def expand_kwargs(self, targets):
                    """Helper function to expand kwargs."""

            return _PartialOp()

    class _DummyHook:
        """Test suite for  DummyHook."""

        def __init__(self, conn_id: str = "imednet_default") -> None:
            """Initialize the test object."""

        def list_study_keys(self):
            """Helper function to list study keys."""
            return []

    provider_mod = ModuleType("apache_airflow_providers_imednet")
    provider_mod.ImednetExportOperator = _DummyOperator  # type: ignore[attr-defined]
    provider_mod.ImednetHook = _DummyHook  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "apache_airflow_providers_imednet", provider_mod)


def test_to_primitive_unknown_object_type_falls_back_to_str(monkeypatch):
    """_to_primitive produces a string for objects that don't match any known type."""
    _setup_airflow(monkeypatch)
    from apache_airflow_providers_imednet.hooks import ImednetHook

    class _Opaque:
        """Test suite for  Opaque."""

        def __str__(self):
            """Helper function to   str  ."""
            return "opaque-repr"

    result = ImednetHook._to_primitive(_Opaque())
    assert result == "opaque-repr"


def test_to_primitive_nested_sensitive_keys_are_redacted(monkeypatch):
    """Deeply nested sensitive keys inside _to_primitive output are masked."""
    _setup_airflow(monkeypatch)
    from apache_airflow_providers_imednet.hooks import ImednetHook

    data = {
        "studyKey": "S-001",
        "meta": {
            "api_key": "SECRET",
            "security_key": "ALSO_SECRET",
            "description": "public info",
        },
        "tags": ["clinical", "phase2"],
    }
    result = ImednetHook._to_primitive(data)
    assert isinstance(result, dict)
    assert result["studyKey"] == "S-001"
    assert result["meta"]["api_key"] == "***"  # type: ignore[index]
    assert result["meta"]["security_key"] == "***"  # type: ignore[index]
    assert result["meta"]["description"] == "public info"  # type: ignore[index]
    assert result["tags"] == ["clinical", "phase2"]


def test_export_operator_template_field_rendering_simulation(monkeypatch):
    """Template-field mutations (simulating Jinja rendering) are reflected at execute time.

    Airflow resolves ``template_fields`` by rendering Jinja and then *setting* the
    rendered value back onto the operator instance.  This test verifies that all
    mapped runtime fields (``study_key``, ``output_path``, ``export_kwargs``) are
    picked up from the instance at execute time rather than captured at construction.
    """
    for mod in list(sys.modules):
        if mod.startswith("apache_airflow_providers_imednet"):
            del sys.modules[mod]

    _setup_airflow(monkeypatch)

    import apache_airflow_providers_imednet as airflow_mod
    import apache_airflow_providers_imednet.operators.export as export_ops

    sdk = MagicMock()
    hook_inst = MagicMock(get_sdk_client=MagicMock(return_value=sdk))
    monkeypatch.setattr(export_ops, "ImednetHook", MagicMock(return_value=hook_inst))
    export_mock = MagicMock()
    monkeypatch.setattr(airflow_mod.export, "export_to_csv", export_mock)

    # Operator is built with template-placeholder strings (as from .partial().expand_kwargs())
    op = airflow_mod.ImednetExportOperator(
        task_id="export_records",
        study_key="{{ ti.xcom_pull('discover_studies')[0]['study_key'] }}",
        output_path="{{ ti.xcom_pull('discover_studies')[0]['output_path'] }}",
        export_func="export_to_csv",
        export_kwargs={"index": "{{ params.use_index }}"},
    )

    # Simulate Airflow template rendering: Airflow sets the rendered values back
    op.study_key = "RENDERED-STUDY-001"
    op.output_path = "/data/RENDERED-STUDY-001.csv"
    op.export_kwargs = {"index": False}

    result = op.execute({})

    export_mock.assert_called_once_with(
        sdk, "RENDERED-STUDY-001", "/data/RENDERED-STUDY-001.csv", index=False
    )
    assert result == "/data/RENDERED-STUDY-001.csv"


def test_list_study_keys_with_snake_case_study_key_field(monkeypatch):
    """list_study_keys returns keys from metadata using ``study_key`` (snake_case)."""
    _setup_airflow(monkeypatch)
    from apache_airflow_providers_imednet.hooks import ImednetHook

    hook = ImednetHook()
    monkeypatch.setattr(
        hook,
        "list_studies_metadata",
        MagicMock(return_value=[{"study_key": "S-SNAKE"}]),
    )
    assert hook.list_study_keys() == ["S-SNAKE"]


def test_list_studies_metadata_returns_empty_for_no_studies(monkeypatch):
    """list_studies_metadata returns an empty list when the SDK reports zero studies."""
    _setup_airflow(monkeypatch)
    from apache_airflow_providers_imednet.hooks import ImednetHook

    sdk = MagicMock()
    sdk.studies.list.return_value = []

    hook = ImednetHook()
    monkeypatch.setattr(hook, "get_sdk_client", MagicMock(return_value=sdk))

    assert hook.list_studies_metadata() == []


def test_list_study_keys_skips_entries_without_recognized_key(monkeypatch):
    """list_study_keys omits entries with no ``studyKey``/``study_key`` or an empty value."""
    _setup_airflow(monkeypatch)
    from apache_airflow_providers_imednet.hooks import ImednetHook

    hook = ImednetHook()
    monkeypatch.setattr(
        hook,
        "list_studies_metadata",
        MagicMock(
            return_value=[
                {"studyKey": "S-001"},
                {"no_key_here": "value"},  # neither field present
                {"studyKey": ""},  # empty string — skipped
                {"study_key": "S-003"},
            ]
        ),
    )
    assert hook.list_study_keys() == ["S-001", "S-003"]


def test_export_operator_resolves_snowflake_sink(monkeypatch):
    """Test that export operator resolves snowflake sink."""
    if "apache_airflow_providers_imednet" in sys.modules:
        del sys.modules["apache_airflow_providers_imednet"]

    _setup_airflow(monkeypatch)
    conn = MagicMock()
    import airflow.sdk.bases.hook as hooks_base

    monkeypatch.setattr(hooks_base.BaseHook, "get_connection", classmethod(lambda cls, cid: conn))

    # Mock SDK sinks namespace
    mock_snowflake_sink_cls = MagicMock()
    mock_snowflake_sink_cls.return_value = MagicMock()
    sinks_mock = MagicMock(SnowflakeExportSink=mock_snowflake_sink_cls)

    import apache_airflow_providers_imednet as airflow_mod
    import apache_airflow_providers_imednet.operators.export as export_ops

    sdk = MagicMock(sinks=sinks_mock)
    # Mock records.list to return a single dummy record
    sdk.records.list.return_value = [{"record_id": "test"}]
    hook_inst = MagicMock(get_sdk_client=MagicMock(return_value=sdk))
    monkeypatch.setattr(export_ops, "ImednetHook", MagicMock(return_value=hook_inst))

    op = airflow_mod.ImednetExportOperator(
        task_id="t",
        study_key="S",
        destination="snowflake",
    )

    op.execute({})

    mock_snowflake_sink_cls.assert_called()
    sink_instance = mock_snowflake_sink_cls.return_value
    sink_instance.__enter__.assert_called_once()
    sink_instance.write_batch.assert_called()


def test_reference_dag_safe_study_path_fragment(monkeypatch):
    """``_safe_study_path_fragment`` in airflow_multi_study_pipeline generates filesystem-safe tokens.

    The function is imported from the production reference DAG to provide true
    regression coverage (not an inline copy).  A minimal mock is applied so the
    DAG module loads without a live Airflow environment.
    """
    _setup_airflow_for_dag(monkeypatch)

    dag_path = _EXAMPLES_AIRFLOW_DIR / "airflow_multi_study_pipeline.py"
    spec = importlib.util.spec_from_file_location("_test_multi_study_pipeline", dag_path)
    assert spec is not None and spec.loader is not None, (
        "Could not locate airflow_multi_study_pipeline.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    fn = module._safe_study_path_fragment

    assert fn("STUDY-001") == "STUDY-001"
    assert fn("PROT-01") == "PROT-01"
    assert fn("study key!@#$") == "study_key"
    assert fn("---") == "study"
    assert fn("") == "study"
    assert fn("__leading__trailing__") == "leading__trailing"
