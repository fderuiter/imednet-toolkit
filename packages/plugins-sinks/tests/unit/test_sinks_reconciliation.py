"""Unit tests for streaming checkpoints and voided-record reconciliation in plugins-sinks."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import imednet_sinks.document as doc_mod
import imednet_sinks.graph as graph_mod
import imednet_sinks.warehouse as wh_mod
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig, export_to_mongodb
from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig, export_to_neo4j
from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig, export_to_snowflake

from imednet.models.records import Record
from imednet_workflows.state_ledger import ExtractionStateLedger


def _record(
    record_id: int,
    *,
    deleted: bool = False,
    date_modified: str = "2026-04-10T12:00:00Z",
) -> Record:
    return Record(
        record_id=record_id,
        subject_id=100 + record_id,
        subject_key="SUBJ-01",
        visit_id=1,
        form_id=10,
        form_key="AE",
        record_status="Complete",
        deleted=deleted,
        date_created="2026-04-01T00:00:00Z",
        date_modified=date_modified,
        record_data={"TERM": f"Event {record_id}"},
    )


def test_mongodb_incremental_streaming_and_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MongoDbExportSink streams batches, reconciles deleted records, and commits checkpoints."""
    pymongo_mod = MagicMock()
    collection = MagicMock()
    collection.insert_many.return_value = MagicMock(inserted_ids=["1"])
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    client = MagicMock()
    client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)

    active = _record(101, deleted=False, date_modified="2026-04-10T10:00:00Z")
    voided = _record(102, deleted=True, date_modified="2026-04-10T11:00:00Z")

    sdk = MagicMock()
    sdk.records.list.side_effect = lambda **_kw: iter([active, voided])

    ledger = ExtractionStateLedger(ledger_path=tmp_path / "mongo_ledger.json")
    cfg = MongoDbSinkConfig(
        study_key="S-MONGO",
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="records",
        batch_size=1,
        incremental=True,
        state_provider=ledger,
    )
    written = export_to_mongodb(sdk, "S-MONGO", config=cfg)
    assert written == 1
    collection.bulk_write.assert_called_once()
    collection.delete_many.assert_called_once_with({"_id": {"$in": ["S-MONGO/102"]}})
    assert ledger.get_last_timestamp("S-MONGO", "mongodb_records") == datetime(
        2026, 4, 10, 11, 0, tzinfo=timezone.utc
    )

    # Non-idempotent insert_many path + reconcile_deleted edge cases
    non_idemp_cfg = MongoDbSinkConfig(
        study_key="S-MONGO",
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="records",
        idempotent=False,
    )
    with MongoDbExportSink(non_idemp_cfg) as sink:
        assert sink.write_batch([active], batch_id="b0") == 1
        assert sink.reconcile_deleted([voided], batch_id="v0") == 0

    with MongoDbExportSink(cfg) as idemp_sink:
        assert idemp_sink.reconcile_deleted([], batch_id="empty") == 0
        assert idemp_sink.reconcile_deleted([{"no_id": True}], batch_id="noid") == 0


def test_neo4j_incremental_streaming_and_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neo4jExportSink streams batches, reconciles deleted nodes, and commits checkpoints."""
    neo4j_mod = MagicMock()
    session = MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    driver = MagicMock()
    driver.session.return_value = session_cm
    neo4j_mod.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    active = _record(201, deleted=False, date_modified="2026-04-11T09:00:00Z")
    voided = _record(202, deleted=True, date_modified="2026-04-11T10:00:00Z")

    sdk = MagicMock()
    sdk.records.list.side_effect = lambda **_kw: iter([active, voided])

    ledger = ExtractionStateLedger(ledger_path=tmp_path / "neo4j_ledger.json")
    cfg = Neo4jSinkConfig(
        study_key="S-NEO",
        uri="bolt://localhost:7687",
        auth=("neo4j", "secret"),
        batch_size=1,
        incremental=True,
        state_provider=ledger,
    )
    written = export_to_neo4j(sdk, "S-NEO", config=cfg)
    assert written == 1
    queries = [c.args[0] for c in session.run.call_args_list]
    assert any("DETACH DELETE r" in q for q in queries)
    assert ledger.get_last_timestamp("S-NEO", "neo4j_neo4j") == datetime(
        2026, 4, 11, 10, 0, tzinfo=timezone.utc
    )

    non_idemp = Neo4jExportSink(
        Neo4jSinkConfig(
            study_key="S-NEO",
            uri="bolt://localhost:7687",
            auth=("neo4j", "secret"),
            idempotent=False,
        )
    )
    assert non_idemp.reconcile_deleted([voided], batch_id="v0") == 0

    with Neo4jExportSink(cfg) as idemp_sink:
        assert idemp_sink.reconcile_deleted([], batch_id="empty") == 0
        assert idemp_sink.reconcile_deleted([{"no_id": True}], batch_id="noid") == 0


def test_snowflake_incremental_streaming_and_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SnowflakeExportSink deduplicates incremental batches, reconciles deletes, and cleans staging."""
    sf_mod = MagicMock()
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cursor
    sf_mod.connect.return_value = conn

    def _dep(pkg: str, _extra: str) -> Any:
        if pkg == "snowflake.connector":
            return sf_mod
        if "parquet" in pkg:
            return pq
        return pa

    monkeypatch.setattr(wh_mod, "_require_optional_dep", _dep)

    active = _record(301, deleted=False, date_modified="2026-04-12T08:00:00Z")
    voided = _record(302, deleted=True, date_modified="2026-04-12T09:30:00Z")

    sdk = MagicMock()
    sdk.records.list.side_effect = lambda **_kw: iter([active, voided])

    ledger = ExtractionStateLedger(ledger_path=tmp_path / "sf_ledger.json")
    # Leave local_staging_dir=None to exercise temporary directory lifecycle too
    cfg = SnowflakeSinkConfig(
        study_key="S-SF",
        account="acct",
        user="usr",
        password="pwd",  # pragma: allowlist secret
        database="DB",
        schema="PUBLIC",
        warehouse="WH",
        stage="STG",
        table="RECORDS",
        batch_size=1,
        incremental=True,
        state_provider=ledger,
    )
    written = export_to_snowflake(sdk, "S-SF", config=cfg)
    assert written == 1

    deletes = [c for c in cursor.execute.call_args_list if "DELETE FROM" in c.args[0]]
    assert len(deletes) == 2
    assert deletes[0].args[1] == ["S-SF", 301]
    assert deletes[1].args[1] == ["S-SF", 302]
    assert ledger.get_last_timestamp("S-SF", "snowflake_RECORDS") == datetime(
        2026, 4, 12, 9, 30, tzinfo=timezone.utc
    )

    non_idemp = SnowflakeExportSink(
        SnowflakeSinkConfig(
            study_key="S-SF",
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="RECORDS",
            idempotent=False,
        )
    )
    assert non_idemp.reconcile_deleted([voided], batch_id="v0") == 0

    with SnowflakeExportSink(cfg) as idemp_sink:
        assert idemp_sink.reconcile_deleted([], batch_id="empty") == 0
        assert idemp_sink.reconcile_deleted([{"no_id": True}], batch_id="noid") == 0
