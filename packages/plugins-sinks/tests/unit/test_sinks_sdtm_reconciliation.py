"""Unit tests for incremental SDTM provenance reconciliation in MongoDB, Neo4j, and Snowflake sinks."""

from __future__ import annotations

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
from imednet.spi.sinks import SDTM_PROVENANCE_RECORD_ID_KEY
from imednet_sinks.document import MongoDbSinkConfig, export_to_mongodb
from imednet_sinks.graph import Neo4jSinkConfig, export_to_neo4j
from imednet_sinks.warehouse import SnowflakeSinkConfig, export_to_snowflake
from imednet_workflows.state_ledger import FileStateProvider

FORBIDDEN_SDTM_COLS = {"recordId", "record_id", "RECORD_ID", SDTM_PROVENANCE_RECORD_ID_KEY}


def _build_incremental_sdk(store: dict[int, Record]) -> MagicMock:
    from imednet.spi.sinks import extract_record_timestamp

    def _list_records(**kw: Any) -> Any:
        raw_filter = kw.get("filter") or ""
        deleted_filter: bool | None = (
            kw.get("deleted") if isinstance(kw.get("deleted"), bool) else None
        )
        min_iso: str | None = None
        for raw_clause in raw_filter.split(";"):
            clause = raw_clause.strip()
            if clause.startswith("deleted=="):
                deleted_filter = clause.split("==", 1)[1].strip().lower() == "true"
            elif clause.startswith("dateModified>="):
                min_iso = clause.split(">=", 1)[1].strip().strip('"').strip("'")

        out: list[Record] = []
        for rec in sorted(store.values(), key=lambda r: r.record_id or 0):
            if deleted_filter is not None and bool(rec.deleted) is not deleted_filter:
                continue
            if min_iso is not None:
                rts = extract_record_timestamp(rec)
                if rts is not None and rts.isoformat() < min_iso.replace("Z", "+00:00"):
                    continue
            out.append(rec)
        return iter(out)

    sdk = MagicMock()
    sdk.records.list.side_effect = _list_records
    return sdk


def test_mongodb_sdtm_incremental_provenance_updates_deletes_and_dm_year(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MongoDbExportSink reconciles SDTM updates/deletes via checkpoint provenance without leaking recordId."""
    docs_by_id: dict[str, dict[str, Any]] = {}

    class FakeUpdateOne:
        def __init__(
            self, flt: dict[str, Any], update: dict[str, Any], upsert: bool = False
        ) -> None:
            self.flt = flt
            self.update = update
            self.upsert = upsert

    pymongo_mod = MagicMock()
    pymongo_mod.UpdateOne = FakeUpdateOne
    collection = MagicMock()

    def _bulk_write(ops: list[Any], ordered: bool = False) -> None:
        _ = ordered
        for op in ops:
            docs_by_id[op.flt["_id"]] = dict(op.update["$set"])

    def _delete_many(flt: dict[str, Any]) -> None:
        ids = flt.get("_id", {}).get("$in", [])
        for did in ids:
            docs_by_id.pop(did, None)

    collection.bulk_write.side_effect = _bulk_write
    collection.delete_many.side_effect = _delete_many
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    client = MagicMock()
    client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)

    store: dict[int, Record] = {
        1: Record(
            record_id=1,
            study_key="S-MONGO-SDTM",
            form_id=10,
            form_key="F_AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-04-10T10:00:00+00:00",
            record_data={"AETERM": "Headache", "AESTDTC": "2026-04-01"},
        ),
        2: Record(
            record_id=2,
            study_key="S-MONGO-SDTM",
            form_id=10,
            form_key="F_AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-04-10T11:00:00+00:00",
            record_data={"AETERM": "Nausea", "AESTDTC": "2026-04-02"},
        ),
    }
    sdk = _build_incremental_sdk(store)
    ledger = FileStateProvider(ledger_path=tmp_path / "mongo_sdtm_ledger.json")
    cfg = MongoDbSinkConfig(
        study_key="S-MONGO-SDTM",
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="ae_sdtm",
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=ledger,
    )

    assert export_to_mongodb(sdk, "S-MONGO-SDTM", config=cfg) == 2
    assert len(docs_by_id) == 2

    # Run 2: Update record 1, void record 2, add record 3
    store[1] = Record(
        record_id=1,
        study_key="S-MONGO-SDTM",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=False,
        date_modified="2026-04-11T10:00:00+00:00",
        record_data={"AETERM": "Migraine", "AESTDTC": "2026-04-05"},
    )
    store[2] = Record(
        record_id=2,
        study_key="S-MONGO-SDTM",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=True,
        date_modified="2026-04-11T10:30:00+00:00",
        record_data={"AETERM": "Nausea", "AESTDTC": "2026-04-02"},
    )
    store[3] = Record(
        record_id=3,
        study_key="S-MONGO-SDTM",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=False,
        date_modified="2026-04-11T11:00:00+00:00",
        record_data={"AETERM": "Rash", "AESTDTC": "2026-04-03"},
    )
    assert export_to_mongodb(sdk, "S-MONGO-SDTM", config=cfg) == 2
    assert len(docs_by_id) == 2

    final_data = sorted(docs_by_id.values(), key=lambda r: r["AESEQ"])
    for row in final_data:
        assert set(row).isdisjoint(FORBIDDEN_SDTM_COLS)
    assert [r["AESEQ"] for r in final_data] == [1, 3]
    assert [r["AETERM"] for r in final_data] == ["Migraine", "Rash"]

    # Also check DM BRTHDTC 4-digit year preservation
    docs_by_id.clear()
    dm_sdk = _build_incremental_sdk(
        {
            9: Record(
                record_id=9,
                study_key="S-MONGO-DM",
                form_id=1,
                form_key="F_DM",
                subject_key="SUBJ-09",
                date_modified="2026-04-12T00:00:00+00:00",
                record_data={"BRTHDTC": "1979-11-23", "patientName": "Hidden"},
            )
        }
    )
    dm_cfg = MongoDbSinkConfig(
        study_key="S-MONGO-DM",
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="dm_sdtm",
        as_sdtm=True,
        sdtm_domain="DM",
        mask_phi=True,
    )
    assert export_to_mongodb(dm_sdk, "S-MONGO-DM", config=dm_cfg) == 1
    dm_doc = next(iter(docs_by_id.values()))
    assert dm_doc["BRTHDTC"] == "1979"
    assert dm_doc["patientName"] == "***MASKED***"


def test_neo4j_sdtm_incremental_provenance_updates_and_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neo4jExportSink merges SDTM nodes by deterministic natural key and detaches voided keys."""
    nodes_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}

    neo4j_mod = MagicMock()
    session = MagicMock()

    def _node_key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (item.get("STUDYID"), item.get("DOMAIN"), item.get("USUBJID"), item.get("AESEQ"))

    def _run_cypher(query: str, **params: Any) -> None:
        if "DETACH DELETE r" in query:
            for kdict in params.get("keys", []):
                nodes_by_key.pop(_node_key(kdict), None)
            return
        if "SET r += row" in query:
            for item in params.get("rows", []):
                nodes_by_key[_node_key(item)] = dict(item)

    session.run.side_effect = _run_cypher
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    driver = MagicMock()
    driver.session.return_value = session_cm
    neo4j_mod.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    store: dict[int, Record] = {
        10: Record(
            record_id=10,
            study_key="S-NEO-SDTM",
            form_id=10,
            form_key="F_AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-04-10T10:00:00+00:00",
            record_data={"AETERM": "Fever", "AESTDTC": "2026-04-01"},
        ),
        11: Record(
            record_id=11,
            study_key="S-NEO-SDTM",
            form_id=10,
            form_key="F_AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-04-10T11:00:00+00:00",
            record_data={"AETERM": "Chills", "AESTDTC": "2026-04-02"},
        ),
    }
    sdk = _build_incremental_sdk(store)
    ledger = FileStateProvider(ledger_path=tmp_path / "neo_sdtm_ledger.json")
    cfg = Neo4jSinkConfig(
        study_key="S-NEO-SDTM",
        uri="bolt://localhost:7687",
        auth=("neo4j", "secret"),  # pragma: allowlist secret
        as_sdtm=True,
        sdtm_domain="AE",
        incremental=True,
        state_provider=ledger,
    )

    assert export_to_neo4j(sdk, "S-NEO-SDTM", config=cfg) == 2
    assert len(nodes_by_key) == 2

    store[10] = Record(
        record_id=10,
        study_key="S-NEO-SDTM",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=False,
        date_modified="2026-04-11T10:00:00+00:00",
        record_data={"AETERM": "High Fever", "AESTDTC": "2026-04-06"},
    )
    store[11] = Record(
        record_id=11,
        study_key="S-NEO-SDTM",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=True,
        date_modified="2026-04-11T10:30:00+00:00",
        record_data={"AETERM": "Chills", "AESTDTC": "2026-04-02"},
    )
    store[12] = Record(
        record_id=12,
        study_key="S-NEO-SDTM",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=False,
        date_modified="2026-04-11T11:00:00+00:00",
        record_data={"AETERM": "Fatigue", "AESTDTC": "2026-04-04"},
    )
    assert export_to_neo4j(sdk, "S-NEO-SDTM", config=cfg) == 2
    assert len(nodes_by_key) == 2

    final_nodes = sorted(nodes_by_key.values(), key=lambda r: r["AESEQ"])
    for props in final_nodes:
        assert SDTM_PROVENANCE_RECORD_ID_KEY not in props
    assert [n["AESEQ"] for n in final_nodes] == [1, 3]
    assert [n["AETERM"] for n in final_nodes] == ["High Fever", "Fatigue"]


def test_snowflake_sdtm_incremental_provenance_staging_merge_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SnowflakeExportSink stages pure CDISC Parquet and merges/deletes by SDTM natural keys."""
    sf_mod = MagicMock()
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cursor
    sf_mod.connect.return_value = conn

    staged_tables: list[pa.Table] = []
    real_write_table = pq.write_table

    def _spy_write_table(table: pa.Table, where: Any, **kw: Any) -> None:
        staged_tables.append(table)
        real_write_table(table, where, **kw)

    pq_spy = MagicMock(wraps=pq)
    pq_spy.write_table.side_effect = _spy_write_table

    def _dep(pkg: str, _extra: str) -> Any:
        if pkg == "snowflake.connector":
            return sf_mod
        if "parquet" in pkg:
            return pq_spy
        return pa

    monkeypatch.setattr(wh_mod, "_require_optional_dep", _dep)

    store: dict[int, Record] = {
        201: Record(
            record_id=201,
            study_key="S-SF-SDTM",
            form_id=10,
            form_key="F_AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-04-10T10:00:00+00:00",
            record_data={"AETERM": "Cough", "AESTDTC": "2026-04-01"},
        ),
        202: Record(
            record_id=202,
            study_key="S-SF-SDTM",
            form_id=10,
            form_key="F_AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-04-10T11:00:00+00:00",
            record_data={"AETERM": "Dyspnea", "AESTDTC": "2026-04-02"},
        ),
    }
    sdk = _build_incremental_sdk(store)
    ledger = FileStateProvider(ledger_path=tmp_path / "sf_sdtm_ledger.json")
    cfg = SnowflakeSinkConfig(
        study_key="S-SF-SDTM",
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
        state_provider=ledger,
    )

    assert export_to_snowflake(sdk, "S-SF-SDTM", config=cfg) == 2
    assert len(staged_tables) == 1
    assert set(staged_tables[0].column_names).isdisjoint(FORBIDDEN_SDTM_COLS)

    cursor.execute.reset_mock()
    staged_tables.clear()

    # Run 2: Update 201, void 202, insert 203
    store[201] = Record(
        record_id=201,
        study_key="S-SF-SDTM",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=False,
        date_modified="2026-04-11T10:00:00+00:00",
        record_data={"AETERM": "Persistent Cough", "AESTDTC": "2026-04-07"},
    )
    store[202] = Record(
        record_id=202,
        study_key="S-SF-SDTM",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=True,
        date_modified="2026-04-11T10:30:00+00:00",
        record_data={"AETERM": "Dyspnea", "AESTDTC": "2026-04-02"},
    )
    store[203] = Record(
        record_id=203,
        study_key="S-SF-SDTM",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=False,
        date_modified="2026-04-11T11:00:00+00:00",
        record_data={"AETERM": "Wheezing", "AESTDTC": "2026-04-04"},
    )

    assert export_to_snowflake(sdk, "S-SF-SDTM", config=cfg) == 2
    assert len(staged_tables) == 1
    delta_tbl = staged_tables[0]
    assert set(delta_tbl.column_names).isdisjoint(FORBIDDEN_SDTM_COLS)
    rows_delta = sorted(delta_tbl.to_pylist(), key=lambda r: r["AESEQ"])
    assert [r["AESEQ"] for r in rows_delta] == [1, 3]
    assert [r["AETERM"] for r in rows_delta] == ["Persistent Cough", "Wheezing"]

    sql_calls = [c for c in cursor.execute.call_args_list]
    merge_sqls = [c.args[0] for c in sql_calls if "MERGE INTO" in c.args[0]]
    assert len(merge_sqls) == 1
    assert 'target."STUDYID" = source."STUDYID"' in merge_sqls[0]
    assert 'target."AESEQ" = source."AESEQ"' in merge_sqls[0]

    delete_calls = [c for c in sql_calls if "DELETE FROM" in c.args[0]]
    assert any(c.args[1] == ["S-SF-SDTM", "AE", "SUBJ-01", 2] for c in delete_calls)


def test_sinks_sdtm_dm_domain_non_seq_deletes_and_scalar_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DM domain natural keys (without *SEQ), VISITNUM branches, and changed natural keys reconcile cleanly."""
    from datetime import date
    from decimal import Decimal
    from enum import Enum

    from imednet.spi.sinks import CentralizedMapper
    from imednet_sinks.document import _record_to_document
    from imednet_sinks.graph import _build_sdtm_cypher, _post_process_graph_sdtm

    class _SampleEnum(Enum):
        ACTIVE = "ACTIVE"

    # Exercise Neo4j SDTM VISITNUM Cypher builder and non-primitive property serializer
    merge_q, create_q = _build_sdtm_cypher(None, has_visit=True)
    assert "VISITNUM" in merge_q and "CREATE (r:Record)" in create_q
    serialized_props = _post_process_graph_sdtm(
        {
            "STUDYID": "S1",
            "STATUS": _SampleEnum.ACTIVE,
            "DMDTC": date(2026, 4, 1),
            "SCORE": Decimal("98.6"),
            "META": {"nested": [1, 2]},
        }
    )
    assert serialized_props["STATUS"] == "ACTIVE"
    assert serialized_props["DMDTC"] == "2026-04-01"
    assert serialized_props["SCORE"] == "98.6"
    assert '"nested"' in serialized_props["META"]

    # Exercise _record_to_document with SDTM CentralizedMapper (both with and without record_id)
    sdtm_mapper = CentralizedMapper(mode="document", as_sdtm=True, sdtm_domain="DM")
    doc_with_id = _record_to_document(
        Record(record_id=77, study_key="S1", subject_key="SUBJ-7", record_data={"SEX": "M"}),
        "S1",
        mapper=sdtm_mapper,
    )
    assert doc_with_id["_id"] == "S1/77"
    doc_no_id = _record_to_document(
        Record(record_id=None, study_key="S1", subject_key="SUBJ-8", record_data={"SEX": "F"}),
        "S1",
        mapper=sdtm_mapper,
    )
    assert doc_no_id["_id"].startswith("S1/DM/SUBJ-8/")

    # Exercise MongoDB non-idempotent SDTM insert + fallback _id when record_id is None
    pymongo_mod = MagicMock()
    inserted_batches: list[list[dict[str, Any]]] = []
    collection = MagicMock()

    def _insert_many(docs: list[dict[str, Any]], ordered: bool = False) -> Any:
        _ = ordered
        inserted_batches.append(list(docs))
        res = MagicMock()
        res.inserted_ids = [d["_id"] for d in docs]
        return res

    collection.insert_many.side_effect = _insert_many
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    client = MagicMock()
    client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)

    mongo_sdk = _build_incremental_sdk(
        {
            1: Record(
                record_id=None,
                study_key="S-MONGO-NOID",
                form_id=1,
                form_key="F_DM",
                subject_key="SUBJ-99",
                record_data={"SEX": "M"},
            )
        }
    )
    mongo_cfg = MongoDbSinkConfig(
        study_key="S-MONGO-NOID",
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="dm",
        as_sdtm=True,
        sdtm_domain="DM",
        idempotent=False,
    )
    assert export_to_mongodb(mongo_sdk, "S-MONGO-NOID", config=mongo_cfg) == 1
    assert inserted_batches[0][0]["_id"].startswith("S-MONGO-NOID/DM/SUBJ-99/")

    # Exercise Neo4j DM incremental delete (no *SEQ column) + VISITNUM + non-idempotent CREATE
    neo4j_mod = MagicMock()
    cypher_queries: list[str] = []
    session = MagicMock()
    session.run.side_effect = lambda q, **_kw: cypher_queries.append(q)
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    driver = MagicMock()
    driver.session.return_value = session_cm
    neo4j_mod.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    dm_store: dict[int, Record] = {
        50: Record(
            record_id=50,
            study_key="S-NEO-DM",
            form_id=1,
            form_key="F_DM",
            subject_key="SUBJ-50",
            visit_id=1,
            deleted=False,
            date_modified="2026-04-10T10:00:00+00:00",
            record_data={"SEX": "F", "VISITNUM": 1},
        )
    }
    neo_sdk = _build_incremental_sdk(dm_store)
    neo_ledger = FileStateProvider(ledger_path=tmp_path / "neo_dm.json")
    neo_cfg = Neo4jSinkConfig(
        study_key="S-NEO-DM",
        uri="bolt://localhost:7687",
        auth=("neo4j", "secret"),  # pragma: allowlist secret
        as_sdtm=True,
        sdtm_domain="DM",
        incremental=True,
        state_provider=neo_ledger,
    )
    assert export_to_neo4j(neo_sdk, "S-NEO-DM", config=neo_cfg) == 1
    dm_store[50] = Record(
        record_id=50,
        study_key="S-NEO-DM",
        form_id=1,
        form_key="F_DM",
        subject_key="SUBJ-50",
        deleted=True,
        date_modified="2026-04-11T10:00:00+00:00",
        record_data={"SEX": "F"},
    )
    assert export_to_neo4j(neo_sdk, "S-NEO-DM", config=neo_cfg) == 0
    assert any(
        "MATCH (r:Record {STUDYID: k.STUDYID, DOMAIN: k.DOMAIN, USUBJID: k.USUBJID})" in q
        for q in cypher_queries
    )

    # Exercise Snowflake SDTM subject reassignment (changed_old_keys deleted before MERGE)
    sf_mod = MagicMock()
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cursor
    sf_mod.connect.return_value = conn
    monkeypatch.setattr(
        wh_mod,
        "_require_optional_dep",
        lambda pkg, _extra: (
            sf_mod if pkg == "snowflake.connector" else (pq if "parquet" in pkg else pa)
        ),
    )
    sf_store: dict[int, Record] = {
        301: Record(
            record_id=301,
            study_key="S-SF-DM",
            form_id=1,
            form_key="F_DM",
            subject_key="SUBJ-OLD",
            deleted=False,
            date_modified="2026-04-10T10:00:00+00:00",
            record_data={"SEX": "M"},
        )
    }
    sf_sdk = _build_incremental_sdk(sf_store)
    sf_ledger = FileStateProvider(ledger_path=tmp_path / "sf_dm.json")
    sf_cfg = SnowflakeSinkConfig(
        study_key="S-SF-DM",
        account="acct",
        user="usr",
        password="pwd",  # pragma: allowlist secret
        database="DB",
        schema="PUBLIC",
        warehouse="WH",
        stage="STG",
        table="DM",
        as_sdtm=True,
        sdtm_domain="DM",
        incremental=True,
        state_provider=sf_ledger,
    )
    assert export_to_snowflake(sf_sdk, "S-SF-DM", config=sf_cfg) == 1
    sf_store[301] = Record(
        record_id=301,
        study_key="S-SF-DM",
        form_id=1,
        form_key="F_DM",
        subject_key="SUBJ-NEW",
        deleted=False,
        date_modified="2026-04-11T10:00:00+00:00",
        record_data={"SEX": "M"},
    )
    assert export_to_snowflake(sf_sdk, "S-SF-DM", config=sf_cfg) == 1
    sf_deletes = [c for c in cursor.execute.call_args_list if "DELETE FROM" in c.args[0]]
    assert any(c.args[1] == ["S-SF-DM", "DM", "SUBJ-OLD"] for c in sf_deletes)
