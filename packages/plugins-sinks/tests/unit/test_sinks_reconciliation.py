"""Unit tests for streaming checkpoints and voided-record reconciliation in plugins-sinks."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import imednet_sinks.document as doc_mod
import imednet_sinks.graph as graph_mod
import imednet_sinks.warehouse as wh_mod
from imednet.models.records import Record
from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig, export_to_mongodb
from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig, export_to_neo4j
from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig, export_to_snowflake
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

    executed_sqls = [c.args[0] for c in cursor.execute.call_args_list]
    assert any("MERGE INTO" in sql for sql in executed_sqls)
    deletes = [c for c in cursor.execute.call_args_list if "DELETE FROM" in c.args[0]]
    assert len(deletes) == 1
    assert deletes[0].args[1] == ["S-SF", 302]
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


def test_snowflake_reconcile_deleted_skips_missing_table_and_tracks_ensured_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SnowflakeExportSink checks INFORMATION_SCHEMA before DELETE FROM on voided-first streams and caches ensured tables."""
    import re

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

    table_cols: dict[tuple[str, str], list[str]] = {
        ("STUDY_SCHEMA", "RECORDS"): ["STUDY_KEY", "RECORD_ID"]
    }
    executed_sqls: list[tuple[str, Any]] = []

    def _exec(sql: str, params: Any = None) -> None:
        executed_sqls.append((sql, params))
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            assert "UPPER(" not in sql
            m_schema = re.search(r"TABLE_SCHEMA = '([^']+)'", sql)
            m_table = re.search(r"TABLE_NAME = '([^']+)'", sql)
            assert m_schema is not None and m_table is not None
            cols = table_cols.get((m_schema.group(1), m_table.group(1)), [])
            cursor.fetchall.return_value = [(c,) for c in cols]
        elif sql.startswith("CREATE TABLE IF NOT EXISTS"):
            table_cols[("study_schema", "Records")] = ["STUDY_KEY", "RECORD_ID"]
            cursor.fetchall.return_value = []
        elif sql.startswith("DELETE FROM"):
            if ("study_schema", "Records") not in table_cols:
                raise RuntimeError("SQL compilation error: Object does not exist")
            cursor.fetchall.return_value = []
        else:
            cursor.fetchall.return_value = []

    cursor.execute.side_effect = _exec

    voided_first = _record(401, deleted=True, date_modified="2026-04-12T10:00:00Z")
    active_mid = _record(402, deleted=False, date_modified="2026-04-12T10:05:00Z")
    voided_after = _record(403, deleted=True, date_modified="2026-04-12T10:10:00Z")

    # 1. Voided-only incremental stream on missing table skips DELETE FROM and commits checkpoint
    ledger = ExtractionStateLedger(ledger_path=tmp_path / "sf_missing_tbl.json")
    cfg = SnowflakeSinkConfig(
        study_key="S-MISS",
        account="acct",
        user="usr",
        password="pwd",  # pragma: allowlist secret
        database="DB",
        schema="study_schema",
        warehouse="WH",
        stage="STG",
        table="Records",
        batch_size=1,
        incremental=True,
        state_provider=ledger,
    )
    sdk_void_only = MagicMock()
    sdk_void_only.records.list.side_effect = lambda **_kw: iter([voided_first])
    assert export_to_snowflake(sdk_void_only, "S-MISS", config=cfg) == 0
    assert not any("DELETE FROM" in s for s, _ in executed_sqls)
    assert ledger.get_last_timestamp("S-MISS", "snowflake_Records") == datetime(
        2026, 4, 12, 10, 0, tzinfo=timezone.utc
    )

    # 2. Voided-first -> active -> voided-after: first reconcile skips, active creates & caches, second reconcile deletes
    executed_sqls.clear()
    sdk_mixed = MagicMock()
    sdk_mixed.records.list.side_effect = lambda **_kw: iter(
        [voided_first, active_mid, voided_after]
    )
    assert export_to_snowflake(sdk_mixed, "S-MISS", config=cfg) == 1
    deletes = [(s, p) for s, p in executed_sqls if s.startswith("DELETE FROM")]
    assert len(deletes) == 1
    assert deletes[0][1] == ["S-MISS", 403]
    # Only 2 INFORMATION_SCHEMA queries ran (initial missing-table check + _ensure_table_schema);
    # the post-create reconcile_deleted used self._ensured_tables without a third check.
    info_checks = [s for s, _ in executed_sqls if "INFORMATION_SCHEMA.COLUMNS" in s]
    assert len(info_checks) == 2

    # 3. Snapshot SDTM reconcile_deleted also skips missing table cleanly
    table_cols.pop(("study_schema", "Records"), None)
    executed_sqls.clear()
    sdtm_snap_cfg = SnowflakeSinkConfig(
        study_key="S-MISS",
        account="acct",
        user="usr",
        password="pwd",  # pragma: allowlist secret
        database="DB",
        schema="study_schema",
        warehouse="WH",
        stage="STG",
        table="Records",
        as_sdtm=True,
        sdtm_domain="DM",
    )
    with SnowflakeExportSink(sdtm_snap_cfg) as sdtm_sink:
        assert sdtm_sink.reconcile_deleted([voided_first], batch_id="sdtm_miss") == 0
    assert not any("DELETE FROM" in s for s, _ in executed_sqls)


def test_snowflake_direct_write_batch_tombstone_vs_reconcile_and_sdtm_hard_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SnowflakeExportSink.write_batch only retains tombstones when include_deleted=True and hard_delete=False."""
    import re

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

    known_cols: list[str] = []
    staged_rows: list[dict[str, Any]] = []
    warehouse_rows: dict[tuple[str, int], dict[str, Any]] = {}

    def _exec(sql: str, params: Any = None) -> None:
        if sql.startswith("SELECT COLUMN_NAME"):
            cursor.fetchall.return_value = [(c,) for c in known_cols]
        elif sql.startswith("CREATE TABLE IF NOT EXISTS"):
            for col in re.findall(r'"([A-Za-z0-9_$]+)"\s+[A-Z_]+', sql.split("(", 1)[1]):
                if col not in known_cols:
                    known_cols.append(col)
            cursor.fetchall.return_value = []
        elif sql.startswith("ALTER TABLE"):
            for col in re.findall(r'"([A-Za-z0-9_$]+)"\s+[A-Z_]+', sql.split("ADD COLUMN", 1)[1]):
                if col not in known_cols:
                    known_cols.append(col)
            cursor.fetchall.return_value = []
        elif sql.startswith("PUT file://"):
            local_file = sql.split("PUT file://", 1)[1].split(" ", 1)[0]
            staged_rows[:] = pq.read_table(local_file).to_pylist()
            cursor.fetchall.return_value = []
        elif sql.startswith("MERGE INTO"):
            hard_del = "THEN DELETE" in sql
            for row in staged_rows:
                study_val = str(row.get("study_key") or row.get("STUDYID"))
                rid_val = int(row.get("record_id") or row.get("RECORD_ID"))
                key = (study_val, rid_val)
                is_del = bool(row.get("deleted") or row.get("DELETED"))
                if key in warehouse_rows:
                    if hard_del and is_del:
                        del warehouse_rows[key]
                    else:
                        warehouse_rows[key].update(row)
                elif not (hard_del and is_del):
                    warehouse_rows[key] = dict(row)
            cursor.fetchall.return_value = []
        elif sql.startswith("DELETE FROM"):
            assert isinstance(params, list) and len(params) >= 2
            study_val = str(params[0])
            for rid in params[1:]:
                warehouse_rows.pop((study_val, int(rid)), None)
            cursor.fetchall.return_value = []
        else:
            cursor.fetchall.return_value = []

    cursor.execute.side_effect = _exec

    base_kw: dict[str, Any] = {
        "study_key": "S-TOMB",
        "account": "acct",
        "user": "usr",
        "password": "pwd",  # pragma: allowlist secret
        "database": "DB",
        "schema": "PUBLIC",
        "warehouse": "WH",
        "stage": "STG",
        "table": "RECORDS",
        "local_staging_dir": tmp_path / "stg",
    }
    r1 = _record(1, deleted=False)
    r2 = _record(2, deleted=False)
    r2_void = _record(2, deleted=True)
    r99_unmatched_void = _record(99, deleted=True)

    # 1. Default include_deleted=False, hard_delete=False: write_batch routes voided items to reconcile_deleted
    with SnowflakeExportSink(SnowflakeSinkConfig(**base_kw)) as sink:
        assert sink.write_batch([r1, r2], batch_id="b1") == 2
        assert set(warehouse_rows) == {("S-TOMB", 1), ("S-TOMB", 2)}
        assert sink.write_batch([r2_void, r99_unmatched_void], batch_id="b2") == 0
        assert set(warehouse_rows) == {("S-TOMB", 1)}

    # 2. Explicit include_deleted=True, hard_delete=False: retains deleted=True tombstones
    with SnowflakeExportSink(
        SnowflakeSinkConfig(**base_kw, include_deleted=True, hard_delete=False)
    ) as soft_sink:
        assert soft_sink.write_batch([r2_void], batch_id="b3") == 1
        assert warehouse_rows[("S-TOMB", 2)]["deleted"] is True

    # 3. Explicit include_deleted=True, hard_delete=True: deletes matched voided, skips unmatched voided
    with SnowflakeExportSink(
        SnowflakeSinkConfig(**base_kw, include_deleted=True, hard_delete=True)
    ) as hard_sink:
        assert hard_sink.write_batch([r2_void, r99_unmatched_void], batch_id="b4") == 2
        assert set(warehouse_rows) == {("S-TOMB", 1)}

    # 4. SDTM + include_deleted=True + hard_delete=True routes voided items via (STUDYID, RECORD_ID)
    known_cols.clear()
    warehouse_rows.clear()
    sdtm_cfg = SnowflakeSinkConfig(
        **base_kw,
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        include_deleted=True,
        hard_delete=True,
    )
    with SnowflakeExportSink(sdtm_cfg) as sdtm_sink:
        assert sdtm_sink.write_batch([r1, r2], batch_id="s1") == 2
        assert set(warehouse_rows) == {("S-TOMB", 1), ("S-TOMB", 2)}
        assert sdtm_sink.write_batch([r2_void, r99_unmatched_void], batch_id="s2") == 0
        assert set(warehouse_rows) == {("S-TOMB", 1)}

    # 5. Direct non-idempotent write_batch with include_deleted=False excludes voided items,
    #    and reconcile_deleted treats missing Snowflake tables (sqlstate='42S02') as a safe no-op.
    with SnowflakeExportSink(
        SnowflakeSinkConfig(**base_kw, idempotent=False, include_deleted=False)
    ) as append_sink:
        assert append_sink.write_batch([r2_void], batch_id="a_void_only") == 0

    class _MissingTableError(Exception):
        sqlstate = "42S02"

    cursor.execute.side_effect = _MissingTableError("Object does not exist")
    with SnowflakeExportSink(SnowflakeSinkConfig(**base_kw)) as missing_tbl_sink:
        assert missing_tbl_sink.reconcile_deleted([r99_unmatched_void], batch_id="m1") == 0


def test_sinks_incremental_sdtm_lineage_study_isolation_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snowflake, Neo4j, and MongoDB key incremental SDTM syncs on (STUDYID, RECORD_ID) and persist SEQ counters."""
    import re

    from imednet.errors import ExportBatchError

    # --- In-memory MongoDB mock ---
    mongo_docs: dict[Any, dict[str, Any]] = {}
    pymongo_mod = MagicMock()

    class _FakeUpdateOne:
        def __init__(
            self, flt: dict[str, Any], update_doc: dict[str, Any], upsert: bool = False
        ) -> None:
            self.flt = flt
            self.doc = update_doc.get("$set", update_doc)
            self.upsert = upsert

    pymongo_mod.UpdateOne = _FakeUpdateOne
    pymongo_mod.ReplaceOne = _FakeUpdateOne
    collection = MagicMock()
    collection.bulk_write.side_effect = lambda ops, **_kw: [
        mongo_docs.__setitem__(op.flt["_id"], dict(op.doc)) for op in ops
    ]
    collection.delete_many.side_effect = lambda flt: [
        mongo_docs.pop(k, None) for k in flt["_id"]["$in"]
    ]
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    mongo_client = MagicMock()
    mongo_client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = mongo_client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)

    # --- In-memory Neo4j mock ---
    neo_nodes: dict[tuple[str, int], dict[str, Any]] = {}
    neo4j_mod = MagicMock()
    neo_session = MagicMock()

    def _neo_run(query: str, **params: Any) -> Any:
        if "UNWIND $rows AS row" in query and "RECORD_ID: row.RECORD_ID" in query:
            for row in params["rows"]:
                neo_nodes[(str(row["STUDYID"]), int(row["RECORD_ID"]))] = dict(row)
        elif "DETACH DELETE r" in query and "RECORD_ID: rid" in query:
            sk = str(params["study_key"])
            for rid in params["record_ids"]:
                neo_nodes.pop((sk, int(rid)), None)
        return MagicMock()

    neo_session.run.side_effect = _neo_run
    neo_cm = MagicMock()
    neo_cm.__enter__.return_value = neo_session
    neo_cm.__exit__.return_value = False
    neo_driver = MagicMock()
    neo_driver.session.return_value = neo_cm
    neo4j_mod.GraphDatabase.driver.return_value = neo_driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    # --- In-memory Snowflake mock ---
    sf_rows: dict[tuple[str, int], dict[str, Any]] = {}
    sf_cols: list[str] = []
    sf_staged: list[dict[str, Any]] = []
    sf_mod = MagicMock()
    sf_cursor = MagicMock()
    sf_conn = MagicMock()
    sf_conn.cursor.return_value = sf_cursor
    sf_mod.connect.return_value = sf_conn

    def _sf_dep(pkg: str, _extra: str) -> Any:
        if pkg == "snowflake.connector":
            return sf_mod
        if "parquet" in pkg:
            return pq
        return pa

    monkeypatch.setattr(wh_mod, "_require_optional_dep", _sf_dep)

    def _sf_exec(sql: str, params: Any = None) -> None:
        if sql.startswith("SELECT COLUMN_NAME"):
            sf_cursor.fetchall.return_value = [(c,) for c in sf_cols]
        elif sql.startswith("CREATE TABLE IF NOT EXISTS"):
            for col in re.findall(r'"([A-Za-z0-9_$]+)"\s+[A-Z_]+', sql.split("(", 1)[1]):
                if col not in sf_cols:
                    sf_cols.append(col)
            sf_cursor.fetchall.return_value = []
        elif sql.startswith("ALTER TABLE"):
            for col in re.findall(r'"([A-Za-z0-9_$]+)"\s+[A-Z_]+', sql.split("ADD COLUMN", 1)[1]):
                if col not in sf_cols:
                    sf_cols.append(col)
            sf_cursor.fetchall.return_value = []
        elif sql.startswith("PUT file://"):
            local_file = sql.split("PUT file://", 1)[1].split(" ", 1)[0]
            sf_staged[:] = pq.read_table(local_file).to_pylist()
            sf_cursor.fetchall.return_value = []
        elif sql.startswith("MERGE INTO"):
            assert 'target."STUDYID" = source."STUDYID"' in sql
            assert 'target."RECORD_ID" = source."RECORD_ID"' in sql
            for row in sf_staged:
                sf_rows[(str(row["STUDYID"]), int(row["RECORD_ID"]))] = dict(row)
            sf_cursor.fetchall.return_value = []
        elif sql.startswith("DELETE FROM"):
            assert '"STUDYID" = %s' in sql and '"RECORD_ID" IN' in sql
            study_val = str(params[0])
            for rid in params[1:]:
                sf_rows.pop((study_val, int(rid)), None)
            sf_cursor.fetchall.return_value = []
        else:
            sf_cursor.fetchall.return_value = []

    sf_cursor.execute.side_effect = _sf_exec

    # Seed STUDY_A record_id=1 across all 3 sinks to verify multi-study isolation
    study_a_rec = Record(
        record_id=1,
        study_key="STUDY_A",
        subject_key="SUBJ-A1",
        form_id=10,
        form_key="AE",
        deleted=False,
        date_created="2026-05-01T08:00:00Z",
        date_modified="2026-05-01T09:00:00Z",
        record_data={"AETERM": "Study A Baseline Event"},
    )
    sdk_a = MagicMock()
    sdk_a.records.list.side_effect = lambda **_kw: iter([study_a_rec])
    ledger_a = ExtractionStateLedger(ledger_path=tmp_path / "ledger_a.json")

    export_to_mongodb(
        sdk_a,
        "STUDY_A",
        config=MongoDbSinkConfig(
            study_key="STUDY_A",
            uri="mongodb://localhost:27017",
            database="clin",
            collection="ae",
            as_sdtm=True,
            sdtm_domain="AE",
            incremental=True,
            state_provider=ledger_a,
        ),
    )
    export_to_neo4j(
        sdk_a,
        "STUDY_A",
        config=Neo4jSinkConfig(
            study_key="STUDY_A",
            uri="bolt://localhost:7687",
            auth=("neo4j", "secret"),
            as_sdtm=True,
            sdtm_domain="AE",
            incremental=True,
            state_provider=ledger_a,
        ),
    )
    export_to_snowflake(
        sdk_a,
        "STUDY_A",
        config=SnowflakeSinkConfig(
            study_key="STUDY_A",
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="AE",
            as_sdtm=True,
            sdtm_domain="AE",
            incremental=True,
            state_provider=ledger_a,
        ),
    )

    # Run 1 for STUDY_B: record 1 & record 2 for SUBJ-01
    b_r1 = Record(
        record_id=1,
        study_key="STUDY_B",
        subject_key="SUBJ-01",
        form_id=10,
        form_key="AE",
        deleted=False,
        date_created="2026-05-02T08:00:00Z",
        date_modified="2026-05-02T10:00:00Z",
        record_data={"AETERM": "Mild Rash"},
    )
    b_r2 = Record(
        record_id=2,
        study_key="STUDY_B",
        subject_key="SUBJ-01",
        form_id=10,
        form_key="AE",
        deleted=False,
        date_created="2026-05-02T08:30:00Z",
        date_modified="2026-05-02T11:00:00Z",
        record_data={"AETERM": "Fever"},
    )
    ledger_b = ExtractionStateLedger(ledger_path=tmp_path / "ledger_b.json")
    sdk_b1 = MagicMock()
    sdk_b1.records.list.side_effect = lambda **_kw: iter([b_r1, b_r2])

    mongo_cfg_b = MongoDbSinkConfig(
        study_key="STUDY_B",
        uri="mongodb://localhost:27017",
        database="clin",
        collection="ae",
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=ledger_b,
        batch_size=1,
    )
    neo_cfg_b = Neo4jSinkConfig(
        study_key="STUDY_B",
        uri="bolt://localhost:7687",
        auth=("neo4j", "secret"),
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=ledger_b,
        batch_size=1,
    )
    sf_cfg_b = SnowflakeSinkConfig(
        study_key="STUDY_B",
        account="acct",
        user="usr",
        password="pwd",  # pragma: allowlist secret
        database="DB",
        schema="PUBLIC",
        warehouse="WH",
        stage="STG",
        table="AE",
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=ledger_b,
        batch_size=1,
    )

    assert export_to_mongodb(sdk_b1, "STUDY_B", config=mongo_cfg_b) == 2
    assert export_to_neo4j(sdk_b1, "STUDY_B", config=neo_cfg_b) == 2
    assert export_to_snowflake(sdk_b1, "STUDY_B", config=sf_cfg_b) == 2

    # Run 2 for STUDY_B: boundary replay of b_r2? Wait: void b_r1 (record_id=1!), boundary-replay b_r2 (updated), add b_r3
    b_r1_void = b_r1.model_copy(
        update={"deleted": True, "date_modified": datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)}
    )
    b_r2_boundary = b_r2.model_copy(
        update={"record_data": {"AETERM": "Moderate Fever"}}
    )  # same 11:00Z boundary timestamp!
    b_r3_new = Record(
        record_id=3,
        study_key="STUDY_B",
        subject_key="SUBJ-01",
        form_id=10,
        form_key="AE",
        deleted=False,
        date_created="2026-05-02T12:30:00Z",
        date_modified="2026-05-02T12:30:00Z",
        record_data={"AETERM": "Chills"},
    )
    sdk_b2 = MagicMock()
    sdk_b2.records.list.side_effect = lambda **_kw: iter([b_r2_boundary, b_r1_void, b_r3_new])

    assert export_to_mongodb(sdk_b2, "STUDY_B", config=mongo_cfg_b) == 2
    assert export_to_neo4j(sdk_b2, "STUDY_B", config=neo_cfg_b) == 2
    assert export_to_snowflake(sdk_b2, "STUDY_B", config=sf_cfg_b) == 2

    # STUDY_A record 1 is intact across all 3 sinks while STUDY_B record 1 was deleted!
    assert "STUDY_A/1" in mongo_docs and "STUDY_B/1" not in mongo_docs
    assert ("STUDY_A", 1) in neo_nodes and ("STUDY_B", 1) not in neo_nodes
    assert ("STUDY_A", 1) in sf_rows and ("STUDY_B", 1) not in sf_rows

    # Subject-sorted batch_size=1 orders "Fever" (RECORD_ID=2 -> AESEQ=1) before "Mild Rash"
    # (RECORD_ID=1 -> AESEQ=2, voided in Run 2); boundary record 2 keeps AESEQ=1 and record 3 gets AESEQ=3!
    for store_row2, store_row3 in (
        (mongo_docs["STUDY_B/2"], mongo_docs["STUDY_B/3"]),
        (neo_nodes[("STUDY_B", 2)], neo_nodes[("STUDY_B", 3)]),
        (sf_rows[("STUDY_B", 2)], sf_rows[("STUDY_B", 3)]),
    ):
        assert store_row2["AETERM"] == "Moderate Fever"
        assert int(store_row2["AESEQ"]) == 1
        assert store_row3["AETERM"] == "Chills"
        assert int(store_row3["AESEQ"]) == 3

    # Failed reconcile_deleted raises ExportBatchError and rolls back checkpoint timestamp & metadata
    prior_sf_ts = ledger_b.get_last_timestamp("STUDY_B", "snowflake_AE")
    assert prior_sf_ts == datetime(2026, 5, 2, 12, 30, tzinfo=timezone.utc)

    def _fail_on_delete(sql: str, params: Any = None) -> None:
        if sql.startswith("DELETE FROM"):
            raise RuntimeError("Snowflake delete failed")
        _sf_exec(sql, params)

    sf_cursor.execute.side_effect = _fail_on_delete
    b_r4_new = Record(
        record_id=4,
        study_key="STUDY_B",
        subject_key="SUBJ-01",
        form_id=10,
        form_key="AE",
        deleted=False,
        date_created="2026-05-02T13:00:00Z",
        date_modified="2026-05-02T13:00:00Z",
        record_data={"AETERM": "Transient Pain"},
    )
    b_r3_void = b_r3_new.model_copy(
        update={"deleted": True, "date_modified": datetime(2026, 5, 2, 13, 30, tzinfo=timezone.utc)}
    )
    sdk_b_fail = MagicMock()
    sdk_b_fail.records.list.side_effect = lambda **_kw: iter([b_r4_new, b_r3_void])

    with pytest.raises(ExportBatchError):
        export_to_snowflake(sdk_b_fail, "STUDY_B", config=sf_cfg_b)

    assert ledger_b.get_last_timestamp("STUDY_B", "snowflake_AE") == prior_sf_ts
    sf_meta = ledger_b.read_state().studies["STUDY_B"].streams["snowflake_AE"].metadata
    assert sf_meta["sdtm_seq_counters"]["SUBJ-01"] == 3
    assert "4" not in sf_meta["sdtm_record_seqs"]
