"""Unit tests for SDTM PHI redaction, mixed-domain merges, and sidecar lineage in plugins-sinks."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import imednet.integrations.enrichment as enrichment_mod
import imednet_sinks.document as doc_mod
import imednet_sinks.graph as graph_mod
import imednet_sinks.warehouse as wh_mod
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.study_config import StudyConfiguration
from imednet_sinks.document import MongoDbSinkConfig, export_to_mongodb
from imednet_sinks.graph import Neo4jSinkConfig, export_to_neo4j
from imednet_sinks.warehouse import SnowflakeSinkConfig, export_to_snowflake
from imednet_workflows.state_ledger import ExtractionStateLedger


def _make_sdk(upstream_records: dict[int, Record]) -> MagicMock:
    forms = [
        Form(form_id=301, form_key="F_DM", form_name="Demographics", form_type="Enrollment"),
        Form(form_id=302, form_key="F_VS", form_name="Vital Signs", form_type="Standard"),
        Form(form_id=303, form_key="F_AE", form_name="Adverse Events", form_type="Standard"),
    ]

    def _list_records(**kw: Any) -> Any:
        from imednet.integrations.sink_base import (
            extract_record_timestamp,
            normalize_watermark_datetime,
        )

        flt = str(kw.get("filter", "") or "")
        items = list(upstream_records.values())
        if kw.get("formId") is not None:
            raw_fids = kw["formId"]
            if isinstance(raw_fids, tuple) and len(raw_fids) == 2 and raw_fids[0] in ("==", "="):
                fids = {int(raw_fids[1])}
            elif isinstance(raw_fids, (list, tuple, set)):
                fids = {int(x) for x in raw_fids}
            else:
                fids = {int(raw_fids)}
            items = [r for r in items if r.form_id in fids]
        for clause in (c.strip() for c in flt.split(";") if c.strip()):
            if clause == "deleted==false":
                items = [r for r in items if not r.deleted]
            elif clause.startswith("dateModified>="):
                wm_dt = normalize_watermark_datetime(clause.split(">=", 1)[1])
                if wm_dt is not None:
                    items = [
                        r
                        for r in items
                        if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                    ]
        if kw.get("sort") == "subjectKey,asc;recordId,asc":
            items.sort(key=lambda r: (str(r.subject_key or ""), int(r.record_id or 0)))
        return iter([r.model_copy(deep=True) for r in items])

    sdk = MagicMock()
    sdk.forms.list.return_value = forms
    sdk.get_forms.return_value = forms
    sdk.variables.list.return_value = []
    sdk.get_variables.return_value = []
    sdk.records.list.side_effect = _list_records
    return sdk


class _InMemoryMongoCollection:
    """Minimal stateful MongoDB collection fake supporting bulk_write UpdateOne and delete_many."""

    def __init__(self) -> None:
        self.docs: dict[Any, dict[str, Any]] = {}

    def _matches(self, doc: dict[str, Any], flt: dict[str, Any]) -> bool:
        if "$or" in flt:
            return any(self._matches(doc, sub) for sub in flt["$or"])
        for k, expected in flt.items():
            actual = doc.get(k)
            if isinstance(expected, dict):
                if "$in" in expected and actual not in expected["$in"]:
                    return False
                if "$ne" in expected and actual == expected["$ne"]:
                    return False
            elif actual != expected:
                return False
        return True

    def delete_many(self, flt: dict[str, Any]) -> MagicMock:
        to_remove = [doc_id for doc_id, doc in self.docs.items() if self._matches(doc, flt)]
        for doc_id in to_remove:
            del self.docs[doc_id]
        return MagicMock(deleted_count=len(to_remove))

    def bulk_write(self, ops: list[Any], ordered: bool = False) -> MagicMock:
        for flt, payload, _upsert in ops:
            doc_id = flt["_id"]
            self.docs[doc_id] = dict(payload)
        return MagicMock()


def test_mongodb_sdtm_incremental_replay_dedup_and_void_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MongoDbExportSink preserves *SEQ on replay, masks BRTHDTC='1981'/None, and deletes voided DM & AE."""
    study_key = "S_MONGO_SDTM"
    study_cfg = StudyConfiguration(
        studyKey=study_key,
        version="1.0.0",
        reportingProfile="general",
        phiFields=["customPhiTag"],
    )
    monkeypatch.setattr(
        enrichment_mod,
        "resolve_study_configuration",
        lambda sk: study_cfg if sk == study_key else None,
    )

    collection = _InMemoryMongoCollection()
    pymongo_mod = MagicMock()
    pymongo_mod.UpdateOne.side_effect = lambda flt, upd, upsert=False: (flt, upd["$set"], upsert)
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    client = MagicMock()
    client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)

    upstream: dict[int, Record] = {
        1: Record(
            record_id=1,
            study_key=study_key,
            subject_key="S01",
            form_id=301,
            form_key="F_DM",
            deleted=False,
            date_modified=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            record_data={"BRTHDTC": "1981-05-14", "customPhiTag": "PHI-VAL-01"},
        ),
        2: Record(
            record_id=2,
            study_key=study_key,
            subject_key="S02",
            form_id=301,
            form_key="F_DM",
            deleted=False,
            date_modified=datetime(2026, 7, 1, 10, 1, tzinfo=UTC),
            record_data={"BRTHDTC": None, "customPhiTag": "PHI-VAL-01"},
        ),
        3: Record(
            record_id=3,
            study_key=study_key,
            subject_key="S01",
            form_id=303,
            form_key="F_AE",
            deleted=False,
            date_modified=datetime(2026, 7, 1, 10, 2, tzinfo=UTC),
            record_data={"AETERM": "Headache v1"},
        ),
        4: Record(
            record_id=4,
            study_key=study_key,
            subject_key="S01",
            form_id=303,
            form_key="F_AE",
            deleted=False,
            date_modified=datetime(2026, 7, 1, 10, 3, tzinfo=UTC),
            record_data={"AETERM": "Rash to void"},
        ),
    }
    sdk = _make_sdk(upstream)
    ledger = ExtractionStateLedger(ledger_path=tmp_path / "mongo_sdtm.json")
    cfg = MongoDbSinkConfig(
        study_key=study_key,
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="sdtm",
        as_sdtm=True,
        mask_phi=True,
        incremental=True,
        state_provider=ledger,
    )

    export_to_mongodb(sdk, study_key, config=cfg)
    assert len(collection.docs) == 4
    s01_dm = collection.docs[f"{study_key}/DM/S01"]
    s02_dm = collection.docs[f"{study_key}/DM/S02"]
    assert s01_dm["BRTHDTC"] == "1981"
    assert s02_dm["BRTHDTC"] is None
    assert s01_dm["customPhiTag"] == "***MASKED***"
    assert "recordId" not in s01_dm and "record_id" not in s01_dm

    # Run 2: replay record 3, void DM record 2 & AE record 4, add AE record 5 -> AESEQ=3
    upstream[3].record_data = {"AETERM": "Headache v2"}
    upstream[3].date_modified = datetime(2026, 7, 2, 11, 0, tzinfo=UTC)
    upstream[2].deleted = True
    upstream[2].date_modified = datetime(2026, 7, 2, 11, 1, tzinfo=UTC)
    upstream[4].deleted = True
    upstream[4].date_modified = datetime(2026, 7, 2, 11, 2, tzinfo=UTC)
    upstream[5] = Record(
        record_id=5,
        study_key=study_key,
        subject_key="S01",
        form_id=303,
        form_key="F_AE",
        deleted=False,
        date_modified=datetime(2026, 7, 2, 11, 3, tzinfo=UTC),
        record_data={"AETERM": "Fatigue new"},
    )
    export_to_mongodb(sdk, study_key, config=cfg)

    assert set(collection.docs.keys()) == {
        f"{study_key}/DM/S01",
        f"{study_key}/3",
        f"{study_key}/5",
    }
    assert collection.docs[f"{study_key}/3"]["AESEQ"] == 1
    assert collection.docs[f"{study_key}/3"]["AETERM"] == "Headache v2"
    assert collection.docs[f"{study_key}/5"]["AESEQ"] == 3
    assert collection.docs[f"{study_key}/5"]["AETERM"] == "Fatigue new"


def test_neo4j_and_snowflake_mixed_domain_partitioning_and_void_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neo4j and Snowflake partition mixed-domain merges by (DOMAIN, seq_col) and reconcile deletes."""
    study_key = "S_MIXED_SDTM"
    upstream: dict[int, Record] = {
        1: Record(
            record_id=1,
            study_key=study_key,
            subject_key="S01",
            form_id=301,
            form_key="F_DM",
            deleted=False,
            date_modified=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            record_data={"BRTHDTC": "1981-05-14", "SEX": "F"},
        ),
        2: Record(
            record_id=2,
            study_key=study_key,
            subject_key="S01",
            form_id=302,
            form_key="F_VS",
            deleted=False,
            date_modified=datetime(2026, 7, 1, 10, 1, tzinfo=UTC),
            record_data={"VSTESTCD": "SYSBP", "VSORRES": 120},
        ),
        3: Record(
            record_id=3,
            study_key=study_key,
            subject_key="S01",
            form_id=303,
            form_key="F_AE",
            deleted=False,
            date_modified=datetime(2026, 7, 1, 10, 2, tzinfo=UTC),
            record_data={"AETERM": "Headache"},
        ),
    }
    sdk = _make_sdk(upstream)

    # --- 1. Strict snapshot opt-out (preserve_record_id=False): omits RECORD_ID and partitions by (DOMAIN, seq_col) ---
    neo4j_calls: list[tuple[str, dict[str, Any]]] = []
    neo4j_mod = MagicMock()
    session = MagicMock()
    session.run.side_effect = lambda q, **p: neo4j_calls.append((q, p)) or MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    driver = MagicMock()
    driver.session.return_value = session_cm
    neo4j_mod.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    neo_snap_cfg = Neo4jSinkConfig(
        study_key=study_key,
        uri="bolt://localhost:7687",
        auth=("neo4j", "secret"),
        as_sdtm=True,
        incremental=False,
        preserve_record_id=False,
    )
    export_to_neo4j(sdk, study_key, config=neo_snap_cfg)

    merge_queries = [q for q, _ in neo4j_calls if "MERGE (r:Record" in q]
    assert len(merge_queries) == 3
    assert any("SEQ" not in q and "RECORD_ID" not in q for q in merge_queries)
    assert any("VSSEQ: row.VSSEQ" in q for q in merge_queries)
    assert any("AESEQ: row.AESEQ" in q for q in merge_queries)
    for _, params in neo4j_calls:
        for row in params.get("rows", ()):
            assert "RECORD_ID" not in row

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

    sf_snap_cfg = SnowflakeSinkConfig(
        study_key=study_key,
        account="acct",
        user="usr",
        password="pwd",  # pragma: allowlist secret
        database="DB",
        schema="PUBLIC",
        warehouse="WH",
        stage="STG",
        table="SDTM_MIXED",
        as_sdtm=True,
        incremental=False,
        preserve_record_id=False,
    )
    export_to_snowflake(sdk, study_key, config=sf_snap_cfg)

    sf_merges = [c.args[0] for c in cursor.execute.call_args_list if "MERGE INTO" in c.args[0]]
    assert len(sf_merges) == 3
    on_clauses = [
        sql.split(" AS source ON ", 1)[1].split(" WHEN MATCHED", 1)[0] for sql in sf_merges
    ]
    assert any("SEQ" not in on_clause and "RECORD_ID" not in on_clause for on_clause in on_clauses)
    assert any('target."VSSEQ" = source."VSSEQ"' in on_clause for on_clause in on_clauses)
    assert any('target."AESEQ" = source."AESEQ"' in on_clause for on_clause in on_clauses)

    # --- 2. Incremental mode (incremental=True + ledger): keys merges & deletes on (STUDYID, RECORD_ID) ---
    neo4j_calls.clear()
    cursor.execute.reset_mock()

    neo_ledger = ExtractionStateLedger(ledger_path=tmp_path / "neo_sdtm.json")
    neo_cfg = Neo4jSinkConfig(
        study_key=study_key,
        uri="bolt://localhost:7687",
        auth=("neo4j", "secret"),
        as_sdtm=True,
        incremental=True,
        state_provider=neo_ledger,
    )
    export_to_neo4j(sdk, study_key, config=neo_cfg)
    inc_neo_rid_rows = [
        row
        for q, p in neo4j_calls
        if "MERGE (r:Record" in q and "RECORD_ID: row.RECORD_ID" in q
        for row in p.get("rows", ())
    ]
    inc_neo_dm_rows = [
        row
        for q, p in neo4j_calls
        if "MERGE (r:Record" in q and "DOMAIN: row.DOMAIN, USUBJID: row.USUBJID" in q
        for row in p.get("rows", ())
    ]
    assert len(inc_neo_rid_rows) == 2
    assert len(inc_neo_dm_rows) == 1
    assert any(
        r["DOMAIN"] == "DM" and "DMSEQ" not in r and r["BRTHDTC"] == "1981" for r in inc_neo_dm_rows
    )
    assert any(r["DOMAIN"] == "VS" and r["VSSEQ"] == 1 for r in inc_neo_rid_rows)
    assert any(r["DOMAIN"] == "AE" and r["AESEQ"] == 1 for r in inc_neo_rid_rows)

    sf_ledger = ExtractionStateLedger(ledger_path=tmp_path / "sf_sdtm.json")
    sf_cfg = SnowflakeSinkConfig(
        study_key=study_key,
        account="acct",
        user="usr",
        password="pwd",  # pragma: allowlist secret
        database="DB",
        schema="PUBLIC",
        warehouse="WH",
        stage="STG",
        table="SDTM_MIXED",
        as_sdtm=True,
        incremental=True,
        state_provider=sf_ledger,
    )
    export_to_snowflake(sdk, study_key, config=sf_cfg)
    inc_sf_merges = [c.args[0] for c in cursor.execute.call_args_list if "MERGE INTO" in c.args[0]]
    assert len(inc_sf_merges) == 3
    assert any(
        'target."STUDYID" = source."STUDYID" AND target."DOMAIN" = source."DOMAIN" AND target."USUBJID" = source."USUBJID"'
        in sql
        for sql in inc_sf_merges
    )
    assert any(
        'target."STUDYID" = source."STUDYID" AND target."RECORD_ID" = source."RECORD_ID"' in sql
        for sql in inc_sf_merges
    )

    # Void both DM (record 1) and AE (record 3) -> incremental deletes target (STUDYID, RECORD_ID),
    # while snapshot full-sync deletes resolve composite SDTM keys
    neo4j_calls.clear()
    cursor.execute.reset_mock()
    upstream[1].deleted = True
    upstream[1].date_modified = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
    upstream[3].deleted = True
    upstream[3].date_modified = datetime(2026, 7, 2, 12, 1, tzinfo=UTC)

    export_to_neo4j(sdk, study_key, config=neo_cfg)
    neo_deletes = [(q, p) for q, p in neo4j_calls if "DETACH DELETE r" in q]
    assert len(neo_deletes) == 1
    assert "RECORD_ID: rid, STUDYID: $study_key" in neo_deletes[0][0]
    assert sorted(neo_deletes[0][1]["record_ids"]) == [1, 3]

    export_to_snowflake(sdk, study_key, config=sf_cfg)
    sf_deletes = [
        (c.args[0], c.args[1]) for c in cursor.execute.call_args_list if "DELETE FROM" in c.args[0]
    ]
    assert len(sf_deletes) == 1
    assert '"STUDYID" = %s AND "RECORD_ID" IN' in sf_deletes[0][0]
    assert sf_deletes[0][1][0] == study_key
    assert sorted(sf_deletes[0][1][1:]) == [1, 3]

    # Snapshot full-sync reconciliation (no RECORD_ID) deletes DM by (STUDYID, DOMAIN, USUBJID)
    neo4j_calls.clear()
    cursor.execute.reset_mock()
    export_to_neo4j(sdk, study_key, config=neo_snap_cfg)
    snap_neo_deletes = [(q, p) for q, p in neo4j_calls if "DETACH DELETE r" in q]
    assert any("DOMAIN: k.domain, USUBJID: k.usubjid" in q for q, _ in snap_neo_deletes)

    export_to_snowflake(sdk, study_key, config=sf_snap_cfg)
    snap_sf_deletes = [
        (c.args[0], c.args[1]) for c in cursor.execute.call_args_list if "DELETE FROM" in c.args[0]
    ]
    assert any(
        "SEQ" not in sql and params == [study_key, "DM", "S01"] for sql, params in snap_sf_deletes
    )


def test_mongodb_sdtm_helpers_config_validation_and_composite_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise MongoDB config validation, SDTM _id/filter/dedup helpers, and RECORD_ID-free reconciliation."""
    from imednet.integrations.enrichment import CentralizedMapper

    with pytest.raises(ValueError, match="uri must be a non-empty string"):
        MongoDbSinkConfig(study_key="S1", uri="  ", database="db", collection="col")
    with pytest.raises(ValueError, match="database must be a non-empty string"):
        MongoDbSinkConfig(study_key="S1", uri="mongodb://h", database="", collection="col")
    with pytest.raises(ValueError, match="collection must be a non-empty string"):
        MongoDbSinkConfig(study_key="S1", uri="mongodb://h", database="db", collection=" ")

    # _attach_sdtm_document_id branches: RECORD_ID fallback, DM composite, non-DM SEQ composite
    rec_with_doc_rid = doc_mod._attach_sdtm_document_id(
        {"STUDYID": "S1", "DOMAIN": "AE", "USUBJID": "U1", "RECORD_ID": 77},
        {},
        "S1",
    )
    assert rec_with_doc_rid["_id"] == "S1/77"

    dm_no_rid = doc_mod._attach_sdtm_document_id(
        {"STUDYID": "S1", "DOMAIN": "DM", "USUBJID": "U1"},
        {},
        "S1",
    )
    assert dm_no_rid["_id"] == "S1/DM/U1"

    ae_no_rid = doc_mod._attach_sdtm_document_id(
        {"STUDYID": "S1", "DOMAIN": "AE", "USUBJID": "U1", "AESEQ": 4},
        {},
        "S1",
    )
    assert ae_no_rid["_id"] == "S1/AE/U1/4"

    # _sdtm_composite_mongo_filter branches
    assert doc_mod._sdtm_composite_mongo_filter({"STUDYID": "S1", "DOMAIN": "DM"}) is None
    assert doc_mod._sdtm_composite_mongo_filter(
        {"study_id": "S1", "domain": "DM", "usubjid": "U1"}
    ) == {"STUDYID": "S1", "DOMAIN": "DM", "USUBJID": "U1"}
    assert (
        doc_mod._sdtm_composite_mongo_filter(
            {"study_id": "S1", "domain": "AE", "usubjid": "U1", "seq_val": None}
        )
        is None
    )
    assert doc_mod._sdtm_composite_mongo_filter(
        {"study_id": "S1", "domain": "AE", "usubjid": "U1", "seq_col": "AESEQ", "seq_val": 2}
    ) == {"STUDYID": "S1", "DOMAIN": "AE", "USUBJID": "U1", "AESEQ": 2}

    # _dedupe_sdtm_mongo_docs branches: unkeyed doc retained + duplicate composite key replaced
    deduped = doc_mod._dedupe_sdtm_mongo_docs(
        [
            {"note": "unkeyed"},
            {"STUDYID": "S1", "DOMAIN": "AE", "USUBJID": "U1", "AESEQ": 1, "AETERM": "old"},
            {"STUDYID": "S1", "DOMAIN": "AE", "USUBJID": "U1", "AESEQ": 1, "AETERM": "new"},
        ]
    )
    assert len(deduped) == 2
    assert deduped[0] == {"note": "unkeyed"}
    assert deduped[1]["AETERM"] == "new"

    # _record_to_document fallback when mapper does not populate _id
    plain_sdtm_mapper = CentralizedMapper(mode="document", as_sdtm=True, sdtm_domain="DM")
    wrapped = doc_mod._record_to_document(
        {"subjectKey": "U9", "record_data": {"SEX": "F"}},
        "S1",
        mapper=plain_sdtm_mapper,
    )
    assert wrapped["_id"] == "S1/DM/U9"

    # Incremental write_batch with RECORD_ID-free mapped docs prunes superseded composite keys,
    # and reconcile_deleted resolves composite SDTM filters for RECORD_ID-free voided records.
    collection = _InMemoryMongoCollection()
    collection.docs["S1/legacy_dm"] = {
        "_id": "S1/legacy_dm",
        "STUDYID": "S1",
        "DOMAIN": "DM",
        "USUBJID": "U1",
        "SEX": "U",
    }
    collection.docs["S1/legacy_ae"] = {
        "_id": "S1/legacy_ae",
        "STUDYID": "S1",
        "DOMAIN": "AE",
        "USUBJID": "U1",
        "AESEQ": 1,
        "AETERM": "Old",
    }
    pymongo_mod = MagicMock()
    pymongo_mod.UpdateOne.side_effect = lambda flt, upd, upsert=False: (flt, upd["$set"], upsert)
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    client = MagicMock()
    client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)

    cfg = MongoDbSinkConfig(
        study_key="S1",
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="sdtm",
        as_sdtm=True,
        idempotent=True,
        modified_since=datetime(2026, 7, 1, tzinfo=UTC),
    )
    with doc_mod.MongoDbExportSink(cfg) as sink:
        assert sink.write_batch([], batch_id="b0") == 0
        written = sink.write_batch(
            [
                {"formKey": "F_DM", "subjectKey": "U1", "record_data": {"SEX": "F"}},
                {"formKey": "F_AE", "subjectKey": "U1", "record_data": {"AETERM": "New"}},
            ],
            batch_id="b1",
        )
        assert written == 2
        assert "S1/legacy_dm" not in collection.docs
        assert "S1/legacy_ae" not in collection.docs
        assert collection.docs["S1/DM/U1"]["SEX"] == "F"
        assert collection.docs["S1/AE/U1/1"]["AETERM"] == "New"

        assert sink.reconcile_deleted([{"unrelated": "x"}], batch_id="r0") == 0
        deleted = sink.reconcile_deleted(
            [
                {"STUDYID": "S1", "DOMAIN": "DM", "USUBJID": "U1"},
                {"STUDYID": "S1", "DOMAIN": "AE", "USUBJID": "U1", "AESEQ": 1},
            ],
            batch_id="r1",
        )
        assert deleted == 2
        assert collection.docs == {}


def test_baseline_as_sdtm_then_modified_since_sync_preserves_record_id_and_reconciles_voids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 7: Baseline as_sdtm=True export followed by modified_since sync updates by RECORD_ID without duplicates and deletes voided Neo4j & Snowflake Records."""
    import duckdb
    import pandas as pd

    from imednet.integrations.export import (
        export_to_csv,
        export_to_duckdb,
        export_to_hive_parquet,
    )

    study_key = "S_AC7_SYNC"
    upstream: dict[int, Record] = {
        1: Record(
            record_id=1,
            study_key=study_key,
            subject_key="S01",
            form_id=301,
            form_key="F_DM",
            deleted=False,
            date_modified=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            record_data={"BRTHDTC": "1981-05-14", "SEX": "F"},
        ),
        2: Record(
            record_id=2,
            study_key=study_key,
            subject_key="S01",
            form_id=303,
            form_key="F_AE",
            deleted=False,
            date_modified=datetime(2026, 7, 1, 10, 1, tzinfo=UTC),
            record_data={"AETERM": "Headache v1"},
        ),
        3: Record(
            record_id=3,
            study_key=study_key,
            subject_key="S01",
            form_id=303,
            form_key="F_AE",
            deleted=False,
            date_modified=datetime(2026, 7, 1, 10, 2, tzinfo=UTC),
            record_data={"AETERM": "Nausea to void"},
        ),
    }
    sdk = _make_sdk(upstream)

    neo4j_calls: list[tuple[str, dict[str, Any]]] = []
    neo4j_mod = MagicMock()
    session = MagicMock()
    session.run.side_effect = lambda q, **p: neo4j_calls.append((q, p)) or MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    driver = MagicMock()
    driver.session.return_value = session_cm
    neo4j_mod.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

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

    csv_path = tmp_path / "sync.csv"
    hive_dir = tmp_path / "sync_hive"
    ddb_path = tmp_path / "sync.duckdb"

    # 1a. Default full non-incremental as_sdtm=True export omits RECORD_ID
    full_csv_path = tmp_path / "full_snapshot.csv"
    export_to_csv(sdk, study_key, full_csv_path, as_sdtm=True, quality_gate_enabled=False)
    assert "RECORD_ID" not in pd.read_csv(full_csv_path).columns

    # 1b. Seeded baseline with explicit preserve_record_id=True (for subsequent modified_since syncs)
    export_to_csv(
        sdk,
        study_key,
        csv_path,
        as_sdtm=True,
        preserve_record_id=True,
        quality_gate_enabled=False,
    )
    export_to_hive_parquet(
        sdk,
        study_key,
        hive_dir,
        as_sdtm=True,
        preserve_record_id=True,
        quality_gate_enabled=False,
    )
    export_to_duckdb(
        sdk,
        study_key,
        ddb_path,
        "sdtm_sync",
        as_sdtm=True,
        preserve_record_id=True,
        quality_gate_enabled=False,
    )
    export_to_neo4j(
        sdk,
        study_key,
        config=Neo4jSinkConfig(
            study_key=study_key,
            uri="bolt://localhost:7687",
            auth=("neo4j", "secret"),
            as_sdtm=True,
            preserve_record_id=True,
            quality_gate_enabled=False,
        ),
    )
    export_to_snowflake(
        sdk,
        study_key,
        config=SnowflakeSinkConfig(
            study_key=study_key,
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="SDTM_SYNC",
            as_sdtm=True,
            preserve_record_id=True,
            quality_gate_enabled=False,
        ),
    )

    baseline_csv = pd.read_csv(csv_path)
    assert "RECORD_ID" in baseline_csv.columns
    assert set(baseline_csv["RECORD_ID"].astype(int)) == {1, 2, 3}
    assert any("MERGE (r:Record" in q and "RECORD_ID: row.RECORD_ID" in q for q, _ in neo4j_calls)
    assert any(
        "MERGE INTO" in c.args[0] and 'target."RECORD_ID" = source."RECORD_ID"' in c.args[0]
        for c in cursor.execute.call_args_list
    )

    # 2. Follow-up modified_since sync: update record 2, void record 3
    watermark = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)
    upstream[2].record_data = {"AETERM": "Headache v2"}
    upstream[2].date_modified = datetime(2026, 7, 2, 9, 0, tzinfo=UTC)
    upstream[3].deleted = True
    upstream[3].date_modified = datetime(2026, 7, 2, 9, 5, tzinfo=UTC)

    neo4j_calls.clear()
    cursor.execute.reset_mock()

    export_to_csv(
        sdk,
        study_key,
        csv_path,
        as_sdtm=True,
        modified_since=watermark,
        quality_gate_enabled=False,
    )
    export_to_hive_parquet(
        sdk,
        study_key,
        hive_dir,
        as_sdtm=True,
        modified_since=watermark,
        quality_gate_enabled=False,
    )
    export_to_duckdb(
        sdk,
        study_key,
        ddb_path,
        "sdtm_sync",
        as_sdtm=True,
        modified_since=watermark,
        quality_gate_enabled=False,
    )
    export_to_neo4j(
        sdk,
        study_key,
        config=Neo4jSinkConfig(
            study_key=study_key,
            uri="bolt://localhost:7687",
            auth=("neo4j", "secret"),
            as_sdtm=True,
            modified_since=watermark,
            quality_gate_enabled=False,
        ),
    )
    export_to_snowflake(
        sdk,
        study_key,
        config=SnowflakeSinkConfig(
            study_key=study_key,
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="SDTM_SYNC",
            as_sdtm=True,
            modified_since=watermark,
            quality_gate_enabled=False,
        ),
    )

    synced_csv = pd.read_csv(csv_path)
    assert set(synced_csv["RECORD_ID"].astype(int)) == {1, 2}
    assert synced_csv.loc[synced_csv["RECORD_ID"] == 2, "AETERM"].iloc[0] == "Headache v2"

    synced_hive = pd.concat(
        [pd.read_parquet(f) for f in sorted(hive_dir.rglob("*.parquet"))], ignore_index=True
    )
    assert set(synced_hive["RECORD_ID"].astype(int)) == {1, 2}
    assert synced_hive.loc[synced_hive["RECORD_ID"] == 2, "AETERM"].iloc[0] == "Headache v2"

    with duckdb.connect(str(ddb_path), read_only=True) as dconn:
        synced_ddb = dconn.execute('SELECT * FROM "sdtm_sync"').df()
    assert set(synced_ddb["RECORD_ID"].astype(int)) == {1, 2}
    assert synced_ddb.loc[synced_ddb["RECORD_ID"] == 2, "AETERM"].iloc[0] == "Headache v2"

    # Verify Neo4j updated record 2 by RECORD_ID and deleted voided record 3 by RECORD_ID
    neo_delete_calls = [
        (q, p) for q, p in neo4j_calls if "DETACH DELETE r" in q and "RECORD_ID: rid" in q
    ]
    assert len(neo_delete_calls) == 1
    assert neo_delete_calls[0][1]["record_ids"] == [3]

    # Verify Snowflake updated record 2 by RECORD_ID and deleted voided record 3 by RECORD_ID
    sf_delete_calls = [
        c.args
        for c in cursor.execute.call_args_list
        if len(c.args) >= 2
        and isinstance(c.args[0], str)
        and 'DELETE FROM "DB"."PUBLIC"."SDTM_SYNC"' in c.args[0]
        and '"RECORD_ID" IN' in c.args[0]
    ]
    assert len(sf_delete_calls) == 1
    assert sf_delete_calls[0][1] == [study_key, 3]


def test_neo4j_sdtm_naming_convention_and_as_sdtm_visitnum_normalization_and_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 8: Neo4jExportSink normalizes positive VISITNUM and links (:Subject)-[:HAS_VISIT]->(:Visit)-[:HAS_RECORD]->(:Record) under naming_convention='sdtm' and as_sdtm=True."""
    from decimal import Decimal

    assert graph_mod._normalize_visit_id(2) == 2
    assert graph_mod._normalize_visit_id("3") == 3
    assert graph_mod._normalize_visit_id(4.0) == 4
    assert graph_mod._normalize_visit_id(Decimal(5)) == 5
    for invalid in (0, -1, "0", "-3", "", "   ", False, True, float("nan"), float("inf")):
        assert graph_mod._normalize_visit_id(invalid) is None

    neo4j_calls: list[tuple[str, dict[str, Any]]] = []
    neo4j_mod = MagicMock()
    session = MagicMock()
    session.run.side_effect = lambda q, **p: neo4j_calls.append((q, p)) or MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    driver = MagicMock()
    driver.session.return_value = session_cm
    neo4j_mod.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    # 1. naming_convention='sdtm' (with as_sdtm=False)
    sdtm_conv_cfg = Neo4jSinkConfig(
        study_key="S_GRAPH_SDTM",
        uri="bolt://localhost:7687",
        auth=("neo4j", "secret"),
        naming_convention="sdtm",
        as_sdtm=False,
    )
    with graph_mod.Neo4jExportSink(sdtm_conv_cfg) as sink:
        sink.write_batch(
            [
                {
                    "recordId": 101,
                    "studyKey": "S_GRAPH_SDTM",
                    "subjectKey": "SUBJ-01",
                    "formKey": "F_VS",
                    "visitId": "2",
                    "recordData": {"VSTESTCD": "HR"},
                },
                {
                    "recordId": 102,
                    "studyKey": "S_GRAPH_SDTM",
                    "subjectKey": "SUBJ-01",
                    "formKey": "F_AE",
                    "visitId": 0,
                    "recordData": {"AETERM": "Unscheduled AE"},
                },
            ],
            batch_id="b1",
        )

    assert len(neo4j_calls) == 1
    conv_cypher, conv_params = neo4j_calls[0]
    assert (
        "MERGE (v:Visit {VISITNUM: row.VISITNUM, USUBJID: row.USUBJID, STUDYID: row.STUDYID})"
        in conv_cypher
    )
    assert "MERGE (su)-[:HAS_VISIT]->(v)" in conv_cypher
    assert "MERGE (v)-[:HAS_RECORD]->(r)" in conv_cypher
    assert "MERGE (su)-[:HAS_RECORD]->(r)" in conv_cypher
    conv_by_rid = {r["RECORD_ID"]: r for r in conv_params["rows"]}
    assert conv_by_rid[101]["VISITNUM"] == 2
    assert conv_by_rid[102]["VISITNUM"] is None

    # 2. as_sdtm=True also links positive VISITNUM via (:Subject)-[:HAS_VISIT]->(:Visit)-[:HAS_RECORD]->(:Record)
    neo4j_calls.clear()
    as_sdtm_cfg = Neo4jSinkConfig(
        study_key="S_GRAPH_SDTM",
        uri="bolt://localhost:7687",
        auth=("neo4j", "secret"),
        as_sdtm=True,
    )
    with graph_mod.Neo4jExportSink(as_sdtm_cfg) as sink:
        sink.write_batch(
            [
                {
                    "recordId": 201,
                    "studyKey": "S_GRAPH_SDTM",
                    "subjectKey": "SUBJ-01",
                    "formKey": "F_VS",
                    "visitId": 3,
                    "recordData": {"VSTESTCD": "SYSBP", "VSORRES": 118},
                },
                {
                    "recordId": 202,
                    "studyKey": "S_GRAPH_SDTM",
                    "subjectKey": "SUBJ-01",
                    "formKey": "F_AE",
                    "visitId": -1,
                    "recordData": {"AETERM": "Unscheduled"},
                },
            ],
            batch_id="b2",
        )

    as_sdtm_visit_queries = [
        (q, p)
        for q, p in neo4j_calls
        if "MERGE (su)-[:HAS_VISIT]->(v)" in q and "MERGE (v)-[:HAS_RECORD]->(r)" in q
    ]
    as_sdtm_direct_queries = [
        (q, p)
        for q, p in neo4j_calls
        if "MERGE (su)-[:HAS_RECORD]->(r)" in q and "HAS_VISIT" not in q
    ]
    assert len(as_sdtm_visit_queries) == 1
    assert [r["VISITNUM"] for r in as_sdtm_visit_queries[0][1]["rows"]] == [3]
    assert len(as_sdtm_direct_queries) == 1
    assert "VISITNUM" not in as_sdtm_direct_queries[0][1]["rows"][0]


def test_sinks_unassigned_subject_guard_dm_composite_key_and_uppercase_ddl_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neo4j skips Subject/Visit edges for unassigned rows, DM keys on (STUDYID, DOMAIN, USUBJID) even with RECORD_ID, and Snowflake deduplicates uppercase DDL columns."""
    from imednet.integrations.enrichment import CentralizedMapper

    # 1. MongoDB DM _id uses STUDYID/DM/USUBJID even when RECORD_ID is present
    dm_with_rid = doc_mod._attach_sdtm_document_id(
        {"STUDYID": "S1", "DOMAIN": "DM", "USUBJID": "U1", "RECORD_ID": 10},
        {},
        "S1",
    )
    assert dm_with_rid["_id"] == "S1/DM/U1"

    # 2. Neo4j skips Subject/Visit nodes when USUBJID/subject_key is null/empty
    neo4j_calls: list[tuple[str, dict[str, Any]]] = []
    neo4j_mod = MagicMock()
    session = MagicMock()
    session.run.side_effect = lambda q, **p: neo4j_calls.append((q, p)) or MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    driver = MagicMock()
    driver.session.return_value = session_cm
    neo4j_mod.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    with graph_mod.Neo4jExportSink(
        Neo4jSinkConfig(
            study_key="S1",
            uri="bolt://localhost:7687",
            auth=("neo4j", "secret"),
            as_sdtm=True,
            preserve_record_id=True,
        )
    ) as neo_sink:
        neo_sink.write_batch(
            [
                {
                    "recordId": 91,
                    "studyKey": "S1",
                    "subjectKey": "",
                    "formKey": "F_AE",
                    "visitId": 2,
                    "recordData": {"AETERM": "Unassigned AE"},
                },
                {
                    "recordId": 92,
                    "studyKey": "S1",
                    "subjectKey": "U1",
                    "formKey": "F_DM",
                    "recordData": {"SEX": "F"},
                },
            ],
            batch_id="b_unassigned",
        )
    assert any(
        "CASE WHEN row.USUBJID IS NOT NULL AND row.USUBJID <> ''" in q
        and "CASE WHEN row.USUBJID IS NULL OR row.USUBJID = ''" in q
        for q, _ in neo4j_calls
    )
    assert any("DOMAIN: row.DOMAIN, USUBJID: row.USUBJID" in q for q, _ in neo4j_calls)

    neo4j_calls.clear()
    with graph_mod.Neo4jExportSink(
        Neo4jSinkConfig(
            study_key="S1",
            uri="bolt://localhost:7687",
            auth=("neo4j", "secret"),
            as_sdtm=True,
            preserve_record_id=False,
        )
    ) as neo_snap_sink:
        neo_snap_sink.write_batch(
            [
                {
                    "recordId": 93,
                    "studyKey": "S1",
                    "subjectKey": None,
                    "formKey": "F_AE",
                    "recordData": {"AETERM": "Unassigned Snapshot AE"},
                }
            ],
            batch_id="b_unassigned_snap",
        )
    assert len(neo4j_calls) == 1
    assert (
        "MERGE (r:Record   {STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, AESEQ: row.AESEQ})"
        in neo4j_calls[0][0]
    )
    assert neo4j_calls[0][1]["rows"][0].get("USUBJID") is None

    # 3. Snowflake uppercase DDL deduplication and _post_process_warehouse nested parity
    for conv in ("snake_case", "camelCase", "sdtm"):
        cm = CentralizedMapper(
            mode="tabular",
            naming_convention=conv,
            post_processor=wh_mod._post_process_warehouse,
        )
        sample_rec = {
            "recordId": 5,
            "studyKey": "S1",
            "subjectKey": "U1",
            "formKey": "AE",
            "dateModified": "2026-07-01T00:00:00Z",
            "recordData": {"formKey": "collision_val", "heart_rate": 72, "nested": {"bp_sys": 120}},
        }
        mapped = cm.map_record(sample_rec)
        direct = wh_mod._post_process_warehouse(
            CentralizedMapper(mode="tabular", naming_convention=conv).map_record(sample_rec),
            naming_convention=conv,
            _keys_already_formatted=True,
        )
        assert mapped == direct
        arrow_tbl = wh_mod._records_to_arrow_table(
            [sample_rec],
            study_key="S1",
            naming_convention=conv,
        )
        assert arrow_tbl.num_rows == 1

    sf_mod = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
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
    with wh_mod.SnowflakeExportSink(
        SnowflakeSinkConfig(
            study_key="S1",
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="T_CASE_DEDUP",
            naming_convention="camelCase",
        )
    ) as sf_sink:
        sf_sink.write_batch(
            [
                {
                    "recordId": 1,
                    "studyKey": "S1",
                    "subjectKey": "U1",
                    "formKey": "AE",
                    "recordData": {"studyKey": "nested_study", "StudyKey": "dup_case"},
                }
            ],
            batch_id="b_sf_dedup",
        )
    executed_sqls = [c.args[0] for c in cursor.execute.call_args_list if c.args]
    create_sql = next(s for s in executed_sqls if "CREATE TABLE IF NOT EXISTS" in s)
    assert create_sql.count('"VAR_STUDYKEY"') == 1
    merge_sql = next(s for s in executed_sqls if "MERGE INTO" in s)
    assert merge_sql.count('target."VAR_STUDYKEY" = source."VAR_STUDYKEY"') == 1
