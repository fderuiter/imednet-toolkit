"""Unit tests executing Snowflake, MongoDB, and Neo4j sink showcase scripts against mocked drivers."""

from __future__ import annotations

import runpy
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import imednet_sinks.document as doc_mod
import imednet_sinks.graph as graph_mod
import imednet_sinks.warehouse as wh_mod
import pytest
from imednet_sinks import MongoDbSinkConfig, Neo4jSinkConfig, SnowflakeSinkConfig

import imednet
from imednet.models.records import Record

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"
TEST_API_VAL = "env-api-placeholder"
TEST_SEC_VAL = "env-sec-placeholder"


def _sample_records() -> list[Record]:
    """Build typed Study Record fixtures for showcase batch execution."""
    return [
        Record.model_validate(
            {
                "study_key": "STUDY_ALPHA",
                "record_id": 101,
                "subject_id": 1,
                "subject_key": "SUBJ-001",
                "form_id": 10,
                "form_key": "DM",
                "visit_id": 5,
                "record_status": "Complete",
                "deleted": False,
                "record_data": {"AGE": 42},
            }
        ),
        Record.model_validate(
            {
                "study_key": "STUDY_ALPHA",
                "record_id": 102,
                "subject_id": 2,
                "subject_key": "SUBJ-002",
                "form_id": 10,
                "form_key": "DM",
                "visit_id": 5,
                "record_status": "Complete",
                "deleted": False,
                "record_data": {"AGE": 51},
            }
        ),
    ]


def _install_mock_sdk(monkeypatch: pytest.MonkeyPatch, records: list[Record]) -> MagicMock:
    """Patch ImednetSDK so showcase scripts stream the provided Study Records offline."""
    sdk_instance = MagicMock()
    sdk_instance.records.list.return_value = records
    sdk_cls = MagicMock(return_value=sdk_instance)
    monkeypatch.setattr(imednet, "ImednetSDK", sdk_cls)
    return sdk_cls


def test_snowflake_showcase_executes_batch_writes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify examples/snowflake_export.py configures SnowflakeSinkConfig and writes Record batches."""
    records = _sample_records()
    sdk_cls = _install_mock_sdk(monkeypatch, records)

    sf = ModuleType("snowflake.connector")
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    sf.connect = MagicMock(return_value=conn)  # type: ignore[attr-defined]

    pa = ModuleType("pyarrow")
    pq = ModuleType("pyarrow.parquet")
    arrow_table = MagicMock()
    arrow_table.column_names = ["study_key", "record_id", "date_modified", "record_data"]
    pa.Table = MagicMock()  # type: ignore[attr-defined]
    pa.Table.from_pylist = MagicMock(return_value=arrow_table)
    pq.write_table = MagicMock()  # type: ignore[attr-defined]

    def fake_require(pkg: str, _extras: str) -> Any:
        if pkg == "snowflake.connector":
            return sf
        if pkg == "pyarrow":
            return pa
        if pkg == "pyarrow.parquet":
            return pq
        return MagicMock()

    monkeypatch.setattr(wh_mod, "_require_optional_dep", fake_require)

    monkeypatch.setenv("IMEDNET_API_KEY", TEST_API_VAL)
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", TEST_SEC_VAL)
    monkeypatch.setenv("IMEDNET_STUDY_KEY", "STUDY_ALPHA")
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acme-warehouse")
    monkeypatch.setenv("SNOWFLAKE_USER", "etl_loader")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "env-sf-pass")
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "CLINICAL_DB")
    monkeypatch.setenv("SNOWFLAKE_SCHEMA", "RAW")
    monkeypatch.setenv("SNOWFLAKE_WAREHOUSE", "LOAD_WH")
    monkeypatch.setenv("SNOWFLAKE_STAGE", "IMEDNET_STAGE")
    monkeypatch.setenv("SNOWFLAKE_TABLE", "STUDY_RECORDS")

    ns = runpy.run_path(str(EXAMPLES_DIR / "snowflake_export.py"))

    sdk_cls.assert_called_once_with(api_key=TEST_API_VAL, security_key=TEST_SEC_VAL)
    cfg = ns["config"]
    assert isinstance(cfg, SnowflakeSinkConfig)
    assert cfg.study_key == "STUDY_ALPHA"
    assert cfg.account == "acme-warehouse"
    assert cfg.database == "CLINICAL_DB"
    assert cfg.stage == "IMEDNET_STAGE"
    assert cfg.table == "STUDY_RECORDS"
    assert ns["records_loaded"] == 2

    sf.connect.assert_called_once()
    pq.write_table.assert_called_once()
    executed_sql = " ".join(str(call.args[0]) for call in cursor.execute.call_args_list)
    assert "PUT file://" in executed_sql
    assert "MERGE INTO" in executed_sql

    out = capsys.readouterr().out
    assert "Loaded 2 Records from Study 'STUDY_ALPHA' into Snowflake." in out
    assert "env-sf-pass" not in out
    assert TEST_API_VAL not in out


def test_mongodb_showcase_executes_batch_writes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify examples/mongodb_export.py configures MongoDbSinkConfig and writes Record batches."""
    records = _sample_records()
    sdk_cls = _install_mock_sdk(monkeypatch, records)

    pymongo = ModuleType("pymongo")
    client = MagicMock()
    collection = MagicMock()
    db = MagicMock(__getitem__=MagicMock(return_value=collection))
    client.__getitem__ = MagicMock(return_value=db)
    pymongo.MongoClient = MagicMock(return_value=client)  # type: ignore[attr-defined]
    pymongo.UpdateOne = MagicMock(  # type: ignore[attr-defined]
        side_effect=lambda filt, upd, upsert=False: (filt, upd, upsert)
    )
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

    monkeypatch.setenv("IMEDNET_API_KEY", TEST_API_VAL)
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", TEST_SEC_VAL)
    monkeypatch.setenv("IMEDNET_STUDY_KEY", "STUDY_ALPHA")
    monkeypatch.setenv("MONGODB_URI", "mongodb://mongo.internal:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "clinical_store")
    monkeypatch.setenv("MONGODB_COLLECTION", "study_records")

    ns = runpy.run_path(str(EXAMPLES_DIR / "mongodb_export.py"))

    sdk_cls.assert_called_once_with(api_key=TEST_API_VAL, security_key=TEST_SEC_VAL)
    cfg = ns["config"]
    assert isinstance(cfg, MongoDbSinkConfig)
    assert cfg.study_key == "STUDY_ALPHA"
    assert cfg.uri == "mongodb://mongo.internal:27017"
    assert cfg.database == "clinical_store"
    assert cfg.collection == "study_records"
    assert cfg.batch_size == 500
    assert cfg.idempotent is True
    assert ns["records_loaded"] == 2

    pymongo.MongoClient.assert_called_once_with("mongodb://mongo.internal:27017")
    collection.bulk_write.assert_called_once()
    ops = collection.bulk_write.call_args.args[0]
    assert len(ops) == 2

    out = capsys.readouterr().out
    assert "Loaded 2 Records from Study 'STUDY_ALPHA' into MongoDB." in out
    assert TEST_API_VAL not in out


def test_neo4j_showcase_executes_batch_writes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify examples/neo4j_export.py configures Neo4jSinkConfig and writes Record batches."""
    records = _sample_records()
    sdk_cls = _install_mock_sdk(monkeypatch, records)

    neo4j = ModuleType("neo4j")
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    neo4j.GraphDatabase = MagicMock()  # type: ignore[attr-defined]
    neo4j.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

    monkeypatch.setenv("IMEDNET_API_KEY", TEST_API_VAL)
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", TEST_SEC_VAL)
    monkeypatch.setenv("IMEDNET_STUDY_KEY", "STUDY_ALPHA")
    monkeypatch.setenv("NEO4J_URI", "bolt://graph.internal:7687")
    monkeypatch.setenv("NEO4J_USER", "graph_loader")
    monkeypatch.setenv("NEO4J_PASSWORD", "env-neo4j-pass")
    monkeypatch.setenv("NEO4J_DATABASE", "clinical_graph")

    ns = runpy.run_path(str(EXAMPLES_DIR / "neo4j_export.py"))

    sdk_cls.assert_called_once_with(api_key=TEST_API_VAL, security_key=TEST_SEC_VAL)
    cfg = ns["config"]
    assert isinstance(cfg, Neo4jSinkConfig)
    assert cfg.study_key == "STUDY_ALPHA"
    assert cfg.uri == "bolt://graph.internal:7687"
    assert cfg.auth == ("graph_loader", "env-neo4j-pass")
    assert cfg.database == "clinical_graph"
    assert cfg.batch_size == 500
    assert cfg.idempotent is True
    assert ns["records_loaded"] == 2

    neo4j.GraphDatabase.driver.assert_called_once_with(
        "bolt://graph.internal:7687", auth=("graph_loader", "env-neo4j-pass")
    )
    driver.verify_connectivity.assert_called_once()
    session.run.assert_called_once()

    out = capsys.readouterr().out
    assert "Loaded 2 Records from Study 'STUDY_ALPHA' into Neo4j." in out
    assert "env-neo4j-pass" not in out
    assert TEST_API_VAL not in out
