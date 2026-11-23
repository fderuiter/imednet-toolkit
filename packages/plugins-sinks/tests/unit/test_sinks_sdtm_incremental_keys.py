"""Unit tests for incremental SDTM record keys, sequence seeding, and deletes in Snowflake & Neo4j."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pyarrow.parquet as pq
import pytest

import imednet_sinks.graph as graph_mod
import imednet_sinks.warehouse as wh_mod
from imednet.models.records import Record
from imednet_sinks import (
    Neo4jExportSink,
    Neo4jSinkConfig,
    SnowflakeExportSink,
    SnowflakeSinkConfig,
)
from imednet_sinks.graph import _build_sdtm_cypher, _extract_cypher_key_seq
from imednet_sinks.warehouse import (
    _extract_sql_key_seq,
    _records_to_arrow_table,
    _resolve_merge_keys,
)


def _mock_snowflake() -> tuple[ModuleType, MagicMock, MagicMock]:
    sf = ModuleType("snowflake.connector")
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    sf.connect = MagicMock(return_value=conn)  # type: ignore[attr-defined]
    return sf, conn, cursor


def _mock_neo4j() -> tuple[ModuleType, MagicMock, MagicMock]:
    neo4j = ModuleType("neo4j")
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    neo4j.GraphDatabase = MagicMock()  # type: ignore[attr-defined]
    neo4j.GraphDatabase.driver.return_value = driver
    return neo4j, driver, session


def test_snowflake_sdtm_incremental_merge_seq_seeding_and_reconcile_deleted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Snowflake SDTM merges/deletes by (STUDYID, RECORD_ID), preserves <DOM>SEQ, and seeds max SEQ."""
    sf, _conn, cursor = _mock_snowflake()
    orig_require = wh_mod._require_optional_dep
    monkeypatch.setattr(
        wh_mod,
        "_require_optional_dep",
        lambda pkg, extras: sf if pkg == "snowflake.connector" else orig_require(pkg, extras),
    )

    def _execute_side_effect(sql: str, params: Any = None) -> None:
        _ = params
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            cursor.fetchall.return_value = [
                ("STUDYID",),
                ("RECORD_ID",),
                ("DOMAIN",),
                ("USUBJID",),
                ("AESEQ",),
                ("AETERM",),
                ("AESER",),
            ]
        elif 'MAX("AESEQ")' in sql:
            cursor.fetchall.return_value = [("SUBJ-01", 3)]
        elif 'SELECT "RECORD_ID", "AESEQ"' in sql:
            cursor.fetchall.return_value = [(20, 2)]
        else:
            cursor.fetchall.return_value = []

    cursor.execute.side_effect = _execute_side_effect

    stage_dir = tmp_path / "sf_inc_sdtm"
    cfg = SnowflakeSinkConfig(
        study_key="STUDY_SF",
        account="acct",
        user="usr",
        password="pwd",  # pragma: allowlist secret
        database="DB",
        schema="PUBLIC",
        warehouse="WH",
        stage="STG",
        table="AE",
        local_staging_dir=str(stage_dir),
        as_sdtm=True,
        sdtm_domain="AE",
    )

    updated_and_new = [
        Record(
            study_key="STUDY_SF",
            record_id=20,
            form_id=10,
            form_key="AE",
            subject_key="SUBJ-01",
            record_data={"aeTerm": "Updated Nausea", "aeSerious": "Yes"},
        ),
        Record(
            study_key="STUDY_SF",
            record_id=40,
            form_id=10,
            form_key="AE",
            subject_key="SUBJ-01",
            record_data={"aeTerm": "New Dizziness", "aeSerious": "No"},
        ),
    ]
    voided = [
        Record(
            study_key="STUDY_SF",
            record_id=30,
            form_id=10,
            form_key="AE",
            subject_key="SUBJ-01",
            deleted=True,
            record_data={},
        )
    ]

    with SnowflakeExportSink(config=cfg) as sink:
        written = sink.write_batch(updated_and_new, batch_id="delta_1")
        written_2 = sink.write_batch([updated_and_new[1]], batch_id="delta_2")
        deleted = sink.reconcile_deleted(voided, batch_id="voided_1")

    assert written == 2
    assert written_2 == 1
    assert deleted == 1

    staged_rows = pq.read_table(stage_dir / "delta_1.parquet").to_pylist()
    assert [r["RECORD_ID"] for r in staged_rows] == [20, 40]
    # Updated record 20 preserves AESEQ=2; new record 40 continues after max(AESEQ)=3 -> AESEQ=4
    assert [r["AESEQ"] for r in staged_rows] == [2, 4]
    staged_rows_2 = pq.read_table(stage_dir / "delta_2.parquet").to_pylist()
    assert staged_rows_2[0]["RECORD_ID"] == 40
    assert staged_rows_2[0]["AESEQ"] == 4

    queries = [
        (c.args[0], c.args[1] if len(c.args) > 1 else None) for c in cursor.execute.call_args_list
    ]
    sql_texts = "\n".join(q for q, _ in queries)
    assert (
        'SELECT "USUBJID", MAX("AESEQ") FROM "DB"."PUBLIC"."AE" WHERE "STUDYID" = %s GROUP BY "USUBJID"'
        in sql_texts
    )
    assert (
        'ON target."STUDYID" = source."STUDYID" AND target."RECORD_ID" = source."RECORD_ID"'
        in sql_texts
    )
    assert 'target."AESEQ" = COALESCE(target."AESEQ", source."AESEQ")' in sql_texts

    delete_calls = [(q, p) for q, p in queries if q.startswith("DELETE FROM")]
    assert len(delete_calls) == 1
    assert (
        delete_calls[0][0]
        == 'DELETE FROM "DB"."PUBLIC"."AE" WHERE "STUDYID" = %s AND "RECORD_ID" IN (%s)'
    )
    assert delete_calls[0][1] == ["STUDY_SF", 30]


def test_snowflake_sdtm_helpers_fallback_and_default_mapper() -> None:
    """_resolve_merge_keys falls back cleanly without RECORD_ID and _records_to_arrow_table emits RECORD_ID."""
    assert _resolve_merge_keys(
        ["STUDYID", "RECORD_ID", "DOMAIN", "USUBJID", "AESEQ"],
        as_sdtm=True,
    ) == ("STUDYID", "RECORD_ID")
    assert _resolve_merge_keys(
        ["STUDYID", "DOMAIN", "USUBJID", "AESEQ"],
        as_sdtm=True,
    ) == ("STUDYID", "DOMAIN", "USUBJID", "AESEQ")

    rec = Record(
        study_key="ST",
        record_id=77,
        form_id=10,
        form_key="AE",
        subject_key="SUBJ-7",
        record_data={"aeTerm": "Cough"},
    )
    tbl = _records_to_arrow_table([rec], study_key="ST", as_sdtm=True, sdtm_domain="AE")
    rows = tbl.to_pylist()
    assert rows[0]["RECORD_ID"] == 77


def test_neo4j_sdtm_incremental_merge_seq_seeding_and_reconcile_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neo4j SDTM merges/deletes by (STUDYID, RECORD_ID), preserves r.<DOM>SEQ, and seeds max SEQ."""
    neo4j, _driver, session = _mock_neo4j()
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

    def _run_side_effect(cypher: str, **_kwargs: Any) -> Any:
        if "RETURN r.USUBJID AS usubjid" in cypher:
            return [{"usubjid": "SUBJ-01", "max_seq": 5}]
        if "RETURN r.RECORD_ID AS record_id" in cypher:
            return [{"record_id": 12, "seq": 2}]
        return []

    session.run.side_effect = _run_side_effect

    cfg = Neo4jSinkConfig(
        study_key="STUDY_NEO",
        uri="bolt://localhost:7687",
        auth=("neo4j", "secret"),
        as_sdtm=True,
        sdtm_domain="AE",
    )

    batch = [
        Record(
            study_key="STUDY_NEO",
            record_id=12,
            form_id=10,
            form_key="AE",
            subject_key="SUBJ-01",
            visit_id=1,
            record_data={"aeTerm": "Updated Rash"},
        ),
        Record(
            study_key="STUDY_NEO",
            record_id=99,
            form_id=10,
            form_key="AE",
            subject_key="SUBJ-01",
            visit_id=1,
            record_data={"aeTerm": "New Fever"},
        ),
    ]
    voided = [
        Record(
            study_key="STUDY_NEO",
            record_id=15,
            form_id=10,
            form_key="AE",
            subject_key="SUBJ-01",
            deleted=True,
            record_data={},
        )
    ]

    with Neo4jExportSink(config=cfg) as sink:
        written = sink.write_batch(batch, batch_id="neo_b1")
        written_2 = sink.write_batch([batch[1]], batch_id="neo_b2")
        deleted = sink.reconcile_deleted(voided, batch_id="neo_del1")

    assert written == 2
    assert written_2 == 1
    assert deleted == 1

    calls = session.run.call_args_list
    seed_calls = [c for c in calls if "RETURN r.USUBJID AS usubjid" in c.args[0]]
    merge_calls = [c for c in calls if "UNWIND $rows AS row" in c.args[0]]
    delete_calls = [c for c in calls if "DETACH DELETE r" in c.args[0]]

    assert len(seed_calls) == 1
    assert seed_calls[0].kwargs == {"study_key": "STUDY_NEO", "domain": "AE"}

    assert len(merge_calls) == 2
    merge_cypher = merge_calls[0].args[0]
    assert "MERGE (r:Record   {STUDYID: row.STUDYID, RECORD_ID: row.RECORD_ID})" in merge_cypher
    assert "SET r += row, r.AESEQ = coalesce(_prev_seq, row.AESEQ)" in merge_cypher
    rows = merge_calls[0].kwargs["rows"]
    assert [r["RECORD_ID"] for r in rows] == [12, 99]
    assert [r["AESEQ"] for r in rows] == [2, 6]
    rows_2 = merge_calls[1].kwargs["rows"]
    assert rows_2[0]["RECORD_ID"] == 99
    assert rows_2[0]["AESEQ"] == 6

    assert len(delete_calls) == 1
    assert "MATCH (r:Record {RECORD_ID: rid, STUDYID: $study_key})" in delete_calls[0].args[0]
    assert delete_calls[0].kwargs == {"record_ids": [15], "study_key": "STUDY_NEO"}

    fallback_merge, _ = _build_sdtm_cypher("AESEQ", has_visit=False, has_record_id=False)
    assert (
        "STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, USUBJID: row.USUBJID, AESEQ: row.AESEQ"
        in fallback_merge
    )
    dm_fallback_merge, _ = _build_sdtm_cypher(None, has_visit=False, has_record_id=False)
    assert "STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, USUBJID: row.USUBJID" in dm_fallback_merge


def test_sdtm_key_seq_parsers_and_dm_inferred_domains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Key/seq row parsers handle dicts/tuples/getters and DM/inferred domains skip SEQ queries."""
    assert _extract_sql_key_seq({"USUBJID": "S1", "MAX_SEQ": 4}, "USUBJID") == ("S1", 4)
    assert _extract_sql_key_seq({"record_id": 9, "aeseq": 2}, "RECORD_ID", "AESEQ") == ("9", 2)
    assert _extract_sql_key_seq(("S1", "bad"), "USUBJID") is None
    assert _extract_sql_key_seq((None, 1), "USUBJID") is None

    class _GetterObj:
        def get(self, k: str) -> Any:
            return {"usubjid": "S2", "max_seq": "7"}.get(k)

    assert _extract_cypher_key_seq(("S1", 3), "usubjid", "max_seq") == ("S1", 3)
    assert _extract_cypher_key_seq(_GetterObj(), "usubjid", "max_seq") == ("S2", 7)
    assert _extract_cypher_key_seq(("S1", "bad"), "usubjid", "max_seq") is None
    assert _extract_cypher_key_seq((None, 2), "usubjid", "max_seq") is None

    sf, _conn, cursor = _mock_snowflake()
    cursor.fetchall.return_value = []
    orig_require = wh_mod._require_optional_dep
    monkeypatch.setattr(
        wh_mod,
        "_require_optional_dep",
        lambda pkg, extras: sf if pkg == "snowflake.connector" else orig_require(pkg, extras),
    )
    dm_rec = Record(
        study_key="ST_DM",
        record_id=1,
        form_id=1,
        form_key="DM",
        subject_key="S1",
        record_data={"sex": "F"},
    )
    with SnowflakeExportSink(
        config=SnowflakeSinkConfig(
            study_key="ST_DM",
            account="a",
            user="u",
            password="p",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="DM",
            local_staging_dir=str(tmp_path / "sf_dm"),
            as_sdtm=True,
        )
    ) as sf_dm:
        assert sf_dm.write_batch([dm_rec], batch_id="dm_1") == 1

    neo4j, _driver, session = _mock_neo4j()
    session.run.return_value = []
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)
    with Neo4jExportSink(
        config=Neo4jSinkConfig(
            study_key="ST_DM",
            uri="bolt://localhost:7687",
            auth=("neo4j", "secret"),
            as_sdtm=True,
        )
    ) as neo_dm:
        assert neo_dm.write_batch([dm_rec], batch_id="dm_neo_1") == 1
