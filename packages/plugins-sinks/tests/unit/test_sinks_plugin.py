"""Unit tests for the SinksNamespace plugin entry point and call resolution."""

from __future__ import annotations

import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig
from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig
from imednet_sinks.plugin import SinksNamespace, create_sinks
from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig


class _SyncFacadeStub:
    """Minimal synchronous SDK facade stub."""

    def get_records(self, study_key: str) -> list[object]:
        return []


class _AsyncFacadeStub:
    """Minimal asynchronous SDK facade stub without synchronous get_records."""

    async def async_get_records(self, study_key: str) -> list[object]:
        return []


def test_create_sinks_exposes_classes_and_supports_async() -> None:
    """Verify create_sinks returns a SinksNamespace with concrete sink/config classes."""
    sdk = _SyncFacadeStub()
    ns = create_sinks(sdk)  # type: ignore[arg-type]

    assert isinstance(ns, SinksNamespace)
    assert ns.supports_async is True
    assert ns.MongoDbSinkConfig is MongoDbSinkConfig
    assert ns.MongoDbExportSink is MongoDbExportSink
    assert ns.Neo4jSinkConfig is Neo4jSinkConfig
    assert ns.Neo4jExportSink is Neo4jExportSink
    assert ns.SnowflakeSinkConfig is SnowflakeSinkConfig
    assert ns.SnowflakeExportSink is SnowflakeExportSink

    assert (
        ns.MongoDbSinkConfig(
            "STUDY_A",
            uri="mongodb://localhost:27017",
            database="db",
            collection="col",
            max_buffered_records=100,
        ).max_buffered_records
        == 100
    )
    assert (
        ns.Neo4jSinkConfig(
            "STUDY_A",
            uri="bolt://localhost:7687",
            max_buffered_records=200,
        ).max_buffered_records
        == 200
    )
    assert ns.SnowflakeSinkConfig("STUDY_A", max_buffered_records=300).max_buffered_records == 300


def test_bound_export_helpers_delegate_without_deprecation_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify bound (study_key, ...) calls forward self._sdk with zero deprecation warnings."""
    import imednet_sinks.document as doc_mod
    import imednet_sinks.graph as graph_mod
    import imednet_sinks.warehouse as wh_mod

    mongo_fn = MagicMock(return_value=11)
    neo_fn = MagicMock(return_value=22)
    sf_fn = MagicMock(return_value=33)
    monkeypatch.setattr(doc_mod, "export_to_mongodb", mongo_fn)
    monkeypatch.setattr(graph_mod, "export_to_neo4j", neo_fn)
    monkeypatch.setattr(wh_mod, "export_to_snowflake", sf_fn)

    sdk = _SyncFacadeStub()
    ns = SinksNamespace(sdk)  # type: ignore[arg-type]
    sf_cfg = SnowflakeSinkConfig(study_key="STUDY_1")

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        assert ns.export_to_mongodb("STUDY_1", "mongodb://localhost:27017", "db", "col") == 11
        assert ns.export_to_mongodb(study_key="STUDY_KW") == 11
        assert ns.export_to_neo4j("STUDY_1", "bolt://localhost:7687") == 22
        assert ns.export_to_snowflake("STUDY_1", config=sf_cfg) == 33

    assert not [w for w in recorded if issubclass(w.category, DeprecationWarning)]
    assert mongo_fn.call_args_list[0].args == (
        sdk,
        "STUDY_1",
        "mongodb://localhost:27017",
        "db",
        "col",
    )
    assert mongo_fn.call_args_list[1].args == (sdk,)
    assert mongo_fn.call_args_list[1].kwargs == {"study_key": "STUDY_KW"}
    assert neo_fn.call_args.args == (sdk, "STUDY_1", "bolt://localhost:7687")
    assert sf_fn.call_args.args == (sdk, "STUDY_1")
    assert sf_fn.call_args.kwargs == {"config": sf_cfg}


def test_deprecated_explicit_sdk_calls_warn_and_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify positional and keyword sdk arguments emit DeprecationWarning at caller frame."""
    import imednet_sinks.document as doc_mod
    import imednet_sinks.graph as graph_mod
    import imednet_sinks.warehouse as wh_mod

    mongo_fn = MagicMock(return_value=7)
    neo_fn = MagicMock(return_value=8)
    sf_fn = MagicMock(return_value=9)
    monkeypatch.setattr(doc_mod, "export_to_mongodb", mongo_fn)
    monkeypatch.setattr(graph_mod, "export_to_neo4j", neo_fn)
    monkeypatch.setattr(wh_mod, "export_to_snowflake", sf_fn)

    bound_sdk = _SyncFacadeStub()
    explicit_sdk = _SyncFacadeStub()
    ns = SinksNamespace(bound_sdk)  # type: ignore[arg-type]
    sf_cfg = SnowflakeSinkConfig(study_key="STUDY_2")

    with pytest.deprecated_call(match="Passing 'sdk' to sdk.sinks.export_to_mongodb") as rec:
        assert ns.export_to_mongodb(explicit_sdk, "STUDY_2") == 7  # type: ignore[call-overload]
    assert Path(rec[0].filename).resolve() == Path(__file__).resolve()
    assert mongo_fn.call_args.args == (explicit_sdk, "STUDY_2")

    with pytest.deprecated_call(match="Passing 'sdk' to sdk.sinks.export_to_neo4j"):
        assert ns.export_to_neo4j(sdk=explicit_sdk, study_key="STUDY_2") == 8  # type: ignore[call-overload]
    assert neo_fn.call_args.args == (explicit_sdk,)
    assert neo_fn.call_args.kwargs == {"study_key": "STUDY_2"}

    with pytest.deprecated_call(match="Passing 'sdk' to sdk.sinks.export_to_snowflake"):
        assert ns.export_to_snowflake(explicit_sdk, "STUDY_2", config=sf_cfg) == 9  # type: ignore[call-overload]
    assert sf_fn.call_args.args == (explicit_sdk, "STUDY_2")


def test_async_sdk_rejected_on_bound_and_explicit_export_calls() -> None:
    """Verify synchronous sink export helpers reject AsyncImednetSDK facades with TypeError."""
    async_sdk = _AsyncFacadeStub()
    sync_sdk = _SyncFacadeStub()
    async_ns = SinksNamespace(async_sdk)  # type: ignore[arg-type]
    sync_ns = SinksNamespace(sync_sdk)  # type: ignore[arg-type]
    sf_cfg = SnowflakeSinkConfig(study_key="STUDY_ASYNC")

    with pytest.raises(TypeError, match="requires a synchronous ImednetSDK"):
        async_ns.export_to_mongodb("STUDY_ASYNC")
    with pytest.raises(TypeError, match="requires a synchronous ImednetSDK"):
        async_ns.export_to_neo4j("STUDY_ASYNC")
    with pytest.raises(TypeError, match="requires a synchronous ImednetSDK"):
        async_ns.export_to_snowflake("STUDY_ASYNC", config=sf_cfg)
    with pytest.raises(TypeError, match="requires a synchronous ImednetSDK"):
        async_ns.export_to_mongodb(999)  # type: ignore[call-overload]

    with pytest.deprecated_call():
        with pytest.raises(TypeError, match="requires a synchronous ImednetSDK"):
            sync_ns.export_to_mongodb(async_sdk, "STUDY_ASYNC")  # type: ignore[call-overload]

    with pytest.deprecated_call():
        with pytest.raises(TypeError, match="requires a synchronous ImednetSDK"):
            sync_ns.export_to_neo4j(sdk=async_sdk, study_key="STUDY_ASYNC")  # type: ignore[call-overload]


def test_resolve_export_call_argument_validation_errors() -> None:
    """Verify missing, duplicate, and non-string study_key arguments raise TypeError."""
    sdk = _SyncFacadeStub()
    other_sdk = _SyncFacadeStub()
    ns = SinksNamespace(sdk)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="missing 1 required positional argument: 'study_key'"):
        ns.export_to_mongodb()  # type: ignore[call-overload]

    with pytest.raises(TypeError, match="got multiple values for argument 'study_key'"):
        ns.export_to_mongodb("STUDY_1", study_key="STUDY_2")  # type: ignore[call-overload]

    with pytest.raises(TypeError, match="'study_key' must be a str, got int"):
        ns.export_to_mongodb(42)  # type: ignore[call-overload]

    with pytest.deprecated_call():
        with pytest.raises(TypeError, match="'study_key' must be a str, got int"):
            ns.export_to_mongodb(other_sdk, 42)  # type: ignore[call-overload]

    with pytest.raises(TypeError, match="'study_key' must be a str, got list"):
        ns.export_to_mongodb(study_key=["bad"])  # type: ignore[call-overload]
