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
    s01_dm = collection.docs[f"{study_key}/1"]
    s02_dm = collection.docs[f"{study_key}/2"]
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
        f"{study_key}/1",
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

    # --- 1. Snapshot mode (incremental=False): omits RECORD_ID and partitions by (DOMAIN, seq_col) ---
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
    inc_neo_rows = [
        row
        for q, p in neo4j_calls
        if "MERGE (r:Record" in q and "RECORD_ID: row.RECORD_ID" in q
        for row in p.get("rows", ())
    ]
    assert len(inc_neo_rows) == 3
    assert any(
        r["DOMAIN"] == "DM" and "DMSEQ" not in r and r["BRTHDTC"] == "1981" for r in inc_neo_rows
    )
    assert any(r["DOMAIN"] == "VS" and r["VSSEQ"] == 1 for r in inc_neo_rows)
    assert any(r["DOMAIN"] == "AE" and r["AESEQ"] == 1 for r in inc_neo_rows)

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
    assert len(inc_sf_merges) == 1
    assert (
        'target."STUDYID" = source."STUDYID" AND target."RECORD_ID" = source."RECORD_ID"'
        in inc_sf_merges[0]
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
