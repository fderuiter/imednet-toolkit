"""Regression tests for SDTM PHI birth-year preservation, domain isolation, and incremental Subject refresh."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pyarrow.parquet as pq
import pytest

import imednet.integrations.export as export_mod
import imednet.integrations.parquet as parquet_mod
import imednet_sinks.document as doc_mod
import imednet_sinks.graph as graph_mod
import imednet_sinks.warehouse as wh_mod
from imednet.errors import ExportBatchError
from imednet.integrations.enrichment import CentralizedMapper, EnrichmentPipeline
from imednet.integrations.sink_base import apply_quality_gate, evaluate_standards_readiness
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.sdtm import _SdtmNormalizer
from imednet.models.standards import transform_records_to_sdtm_rows
from imednet.models.study_config import StudyConfiguration
from imednet.models.variables import Variable
from imednet_workflows.state_ledger import FileStateProvider, StreamExecutionStatus


class _InMemoryMongoCollection:
    """Minimal in-memory PyMongo collection double supporting bulk_write, insert_many, and delete_many."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def bulk_write(self, ops: list[Any], ordered: bool = False) -> Any:
        _ = ordered
        for op in ops:
            doc_id = str(op._filter["_id"])
            payload = dict(op._doc["$set"])
            if doc_id in self.docs:
                self.docs[doc_id].update(payload)
            else:
                self.docs[doc_id] = payload
        return MagicMock()

    def insert_many(self, docs: list[dict[str, Any]], ordered: bool = False) -> Any:
        _ = ordered
        ids: list[str] = []
        for idx, doc in enumerate(docs):
            doc_id = str(doc.get("_id", f"auto_{len(self.docs)}_{idx}"))
            self.docs[doc_id] = dict(doc)
            ids.append(doc_id)
        return MagicMock(inserted_ids=ids)

    def delete_many(self, flt: dict[str, Any]) -> Any:
        removed = 0
        if "_id" in flt and isinstance(flt["_id"], dict) and "$in" in flt["_id"]:
            for target_id in flt["_id"]["$in"]:
                if str(target_id) in self.docs:
                    del self.docs[str(target_id)]
                    removed += 1
            return MagicMock(deleted_count=removed)
        target_study = flt.get("STUDYID")
        target_domain = flt.get("DOMAIN")
        target_usubjids = set(flt.get("USUBJID", {}).get("$in", []))
        for doc_id in list(self.docs):
            doc = self.docs[doc_id]
            if (
                doc.get("STUDYID") == target_study
                and doc.get("DOMAIN") == target_domain
                and doc.get("USUBJID") in target_usubjids
            ):
                del self.docs[doc_id]
                removed += 1
        return MagicMock(deleted_count=removed)


class _InMemoryNeo4jStore:
    """Minimal in-memory Neo4j session double tracking SDTM Record nodes by composite key."""

    def __init__(self) -> None:
        self.nodes: list[dict[str, Any]] = []

    def run(self, cypher: str, **params: Any) -> Any:
        if "DETACH DELETE r" in cypher and "slices" in params:
            slice_set = {(s["STUDYID"], s["DOMAIN"], s["USUBJID"]) for s in params["slices"]}
            self.nodes = [
                n
                for n in self.nodes
                if (n.get("STUDYID"), n.get("DOMAIN"), n.get("USUBJID")) not in slice_set
            ]
            return MagicMock()
        rows = params.get("rows", [])
        for row in rows:
            seq_key = next(
                (k for k in row if k.endswith("SEQ") and k not in ("STUDYID", "DOMAIN", "USUBJID")),
                None,
            )
            matched_idx = None
            for idx, existing in enumerate(self.nodes):
                same_slice = (
                    existing.get("STUDYID") == row.get("STUDYID")
                    and existing.get("DOMAIN") == row.get("DOMAIN")
                    and existing.get("USUBJID") == row.get("USUBJID")
                )
                if same_slice and (seq_key is None or existing.get(seq_key) == row.get(seq_key)):
                    matched_idx = idx
                    break
            if "MERGE (r:Record" in cypher and matched_idx is not None:
                self.nodes[matched_idx].update(row)
            else:
                self.nodes.append(dict(row))
        return MagicMock()


class _InMemorySnowflakeStore:
    """Minimal in-memory Snowflake cursor/connection double supporting PUT, COPY, MERGE, and DELETE/INSERT."""

    def __init__(self) -> None:
        self.known_cols: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self.staged_rows: list[dict[str, Any]] = []
        self.executed_sql: list[str] = []

    def make_driver(self) -> Any:
        sf_mod = MagicMock()
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur
        sf_mod.connect.return_value = conn

        def _execute(sql: str, params: list[Any] | None = None) -> None:
            self.executed_sql.append(sql)
            if "INFORMATION_SCHEMA.COLUMNS" in sql:
                cur.fetchall.return_value = [(c,) for c in self.known_cols]
            elif sql.startswith("CREATE TABLE IF NOT EXISTS"):
                for col in re.findall(r'"([A-Z0-9_$]+)"\s+[A-Z_]+', sql.split("(", 1)[1]):
                    if col not in self.known_cols:
                        self.known_cols.append(col)
                cur.fetchall.return_value = []
            elif sql.startswith("ALTER TABLE"):
                for col in re.findall(r'"([A-Z0-9_$]+)"\s+[A-Z_]+', sql.split("ADD COLUMN", 1)[1]):
                    if col not in self.known_cols:
                        self.known_cols.append(col)
                cur.fetchall.return_value = []
            elif sql.startswith("PUT file://"):
                local_file = sql.split("PUT file://", 1)[1].split(" ", 1)[0]
                raw_rows = pq.read_table(local_file).to_pylist()
                self.staged_rows = [{k.upper(): v for k, v in r.items()} for r in raw_rows]
                cur.fetchall.return_value = []
            elif sql.startswith("DELETE FROM") and params:
                st, dom, *usubjids = params
                usub_set = set(usubjids)
                self.rows = [
                    r
                    for r in self.rows
                    if not (
                        r.get("STUDYID") == st
                        and r.get("DOMAIN") == dom
                        and r.get("USUBJID") in usub_set
                    )
                ]
                cur.fetchall.return_value = []
            elif sql.startswith("INSERT INTO"):
                self.rows.extend(dict(r) for r in self.staged_rows)
                cur.fetchall.return_value = []
            elif sql.startswith("MERGE INTO"):
                for r in self.staged_rows:
                    seq_col = next((k for k in r if k.endswith("SEQ")), None)
                    matched = None
                    for idx, ex in enumerate(self.rows):
                        if (
                            ex.get("STUDYID") == r.get("STUDYID")
                            and ex.get("DOMAIN") == r.get("DOMAIN")
                            and ex.get("USUBJID") == r.get("USUBJID")
                            and (seq_col is None or ex.get(seq_col) == r.get(seq_col))
                        ):
                            matched = idx
                            break
                    if matched is not None:
                        self.rows[matched].update(r)
                    else:
                        self.rows.append(dict(r))
                cur.fetchall.return_value = []
            else:
                cur.fetchall.return_value = []

        cur.execute.side_effect = _execute
        return sf_mod


def _make_queryable_sdk(
    forms: list[Form],
    variables: list[Variable],
    records_store: dict[int, Record],
    observed_calls: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Create a realistic queryable SDK double supporting watermark, formId, subjectKey, and deleted filters."""
    from imednet.integrations.sink_base import (
        extract_record_timestamp,
        normalize_watermark_datetime,
    )

    sdk = MagicMock()
    sdk.forms.list.side_effect = lambda **_kw: iter(forms)
    sdk.get_forms.return_value = forms
    sdk.variables.list.side_effect = lambda **_kw: iter(variables)
    sdk.get_variables.return_value = variables

    def _list_records(_study_key: str | None = None, **kw: Any) -> Any:
        if observed_calls is not None:
            observed_calls.append(dict(kw))
        flt = str(kw.get("filter", "") or "")
        items = [r.model_copy(deep=True) for r in records_store.values()]

        if kw.get("deleted") is False or "deleted==false" in flt:
            items = [r for r in items if not r.deleted]

        fid_arg = kw.get("formId")
        if fid_arg is not None:
            allowed_fids = {fid_arg} if isinstance(fid_arg, int) else set(fid_arg)
            items = [r for r in items if r.form_id in allowed_fids]
        elif "formId==" in flt:
            allowed_fids = set()
            for clause in flt.split(";"):
                for tok in clause.split(","):
                    if tok.startswith("formId=="):
                        allowed_fids.add(int(tok.split("==", 1)[1]))
            if allowed_fids:
                items = [r for r in items if r.form_id in allowed_fids]

        subj_arg = kw.get("subjectKey")
        if subj_arg is not None:
            allowed_subjs = {subj_arg} if isinstance(subj_arg, str) else set(subj_arg)
            items = [r for r in items if r.subject_key in allowed_subjs]
        elif "subjectKey==" in flt:
            allowed_subjs = set()
            for clause in flt.split(";"):
                for tok in clause.split(","):
                    if tok.startswith("subjectKey=="):
                        allowed_subjs.add(tok.split("==", 1)[1])
            if allowed_subjs:
                items = [r for r in items if r.subject_key in allowed_subjs]

        dm = kw.get("dateModified")
        wm_dt = None
        wm_strict = False
        if isinstance(dm, tuple) and len(dm) == 2 and dm[0] in (">", ">="):
            wm_dt = normalize_watermark_datetime(str(dm[1]))
            wm_strict = dm[0] == ">"
        elif "dateModified>=" in flt:
            wm_part = [p for p in flt.split(";") if p.startswith("dateModified>=")][0]
            wm_dt = normalize_watermark_datetime(wm_part.split(">=", 1)[1])
        elif "dateModified>" in flt:
            wm_part = [p for p in flt.split(";") if p.startswith("dateModified>")][0]
            wm_dt = normalize_watermark_datetime(wm_part.split(">", 1)[1])
            wm_strict = True
        if wm_dt is not None:
            items = [
                r
                for r in items
                if (ts := extract_record_timestamp(r)) is not None
                and (ts > wm_dt if wm_strict else ts >= wm_dt)
            ]
        return iter(items)

    sdk.records.list.side_effect = _list_records
    return sdk


def test_all_nine_targets_preserve_four_digit_brthdtc_and_omit_record_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All 9 export targets emit '1985' for birthDate='1985-07-20' under as_sdtm=True, mask_phi=True and omit recordId."""
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("openpyxl")
    pytest.importorskip("pyarrow")

    forms = [Form(form_id=1, form_key="DM", form_name="Demographics")]
    variables = [
        Variable(variable_name="sex", label="Sex", variable_type="text", form_id=1, form_key="DM"),
        Variable(
            variable_name="birthDate",
            label="Birth Date",
            variable_type="text",
            form_id=1,
            form_key="DM",
        ),
        Variable(
            variable_name="patientName",
            label="Patient Name",
            variable_type="text",
            form_id=1,
            form_key="DM",
        ),
    ]
    dm_rec = Record(
        record_id=77,
        study_key="STUDY9",
        form_id=1,
        form_key="DM",
        subject_key="SUBJ-001",
        date_modified="2026-06-01T00:00:00Z",
        record_data={
            "sex": "F",
            "birthDate": "1985-07-20",
            "patientName": "Jane Secret",
        },
    )
    sdk = _make_queryable_sdk(forms, variables, {77: dm_rec})

    # 1. CSV
    csv_path = tmp_path / "dm.csv"
    export_mod.export_to_csv(
        sdk, "STUDY9", str(csv_path), as_sdtm=True, sdtm_domain="DM", mask_phi=True
    )
    csv_df = pd.read_csv(csv_path, dtype=str)
    assert list(csv_df["BRTHDTC"]) == ["1985"]
    assert list(csv_df["patientName"]) == ["***MASKED***"]
    assert "recordId" not in csv_df.columns

    # 2. Parquet + Hive Parquet
    pq_path = tmp_path / "dm.parquet"
    export_mod.export_to_parquet(
        sdk, "STUDY9", str(pq_path), as_sdtm=True, sdtm_domain="DM", mask_phi=True
    )
    pq_df = pd.read_parquet(pq_path)
    assert list(pq_df["BRTHDTC"]) == ["1985"]
    assert "recordId" not in pq_df.columns

    hive_dir = tmp_path / "hive_dm"
    parquet_mod.export_to_hive_parquet(
        sdk, "STUDY9", str(hive_dir), as_sdtm=True, sdtm_domain="DM", mask_phi=True
    )
    hive_files = list((hive_dir / "study_key=STUDY9" / "form_key=DM").rglob("*.parquet"))
    assert len(hive_files) == 1
    hive_df = pd.read_parquet(hive_files[0])
    assert list(hive_df["BRTHDTC"]) == ["1985"]
    assert "recordId" not in hive_df.columns

    # 3. Excel
    xl_path = tmp_path / "dm.xlsx"
    export_mod.export_to_excel(
        sdk, "STUDY9", str(xl_path), as_sdtm=True, sdtm_domain="DM", mask_phi=True
    )
    xl_df = pd.read_excel(xl_path, dtype=str)
    assert list(xl_df["BRTHDTC"]) == ["1985"]
    assert "recordId" not in xl_df.columns

    # 4. JSON
    json_path = tmp_path / "dm.json"
    export_mod.export_to_json(
        sdk, "STUDY9", str(json_path), as_sdtm=True, sdtm_domain="DM", mask_phi=True
    )
    json_rows = json.loads(json_path.read_text(encoding="utf-8"))
    assert [r["BRTHDTC"] for r in json_rows] == ["1985"]
    assert all("recordId" not in r for r in json_rows)

    # 5. DuckDB + DuckDB by Form
    ddb_path = tmp_path / "dm.duckdb"
    export_mod.export_to_duckdb(
        sdk, "STUDY9", str(ddb_path), "dm_tbl", as_sdtm=True, sdtm_domain="DM", mask_phi=True
    )
    ddb_forms_path = tmp_path / "dm_forms.duckdb"
    export_mod.export_to_duckdb_by_form(
        sdk, "STUDY9", str(ddb_forms_path), as_sdtm=True, sdtm_domain="DM", mask_phi=True
    )
    with duckdb.connect(str(ddb_path), read_only=True, config={"threads": "1"}) as con:
        assert con.execute('SELECT "BRTHDTC" FROM dm_tbl').fetchall() == [("1985",)]
        cols = {r[0] for r in con.execute("DESCRIBE dm_tbl").fetchall()}
        assert "recordId" not in cols
    with duckdb.connect(str(ddb_forms_path), read_only=True, config={"threads": "1"}) as con:
        assert con.execute('SELECT "BRTHDTC" FROM "DM"').fetchall() == [("1985",)]

    # 6. SQL + SQL by Form
    sql_db = tmp_path / "dm.sqlite"
    sql_uri = f"sqlite:///{sql_db}"
    export_mod.export_to_sql(
        sdk, "STUDY9", "dm_sql", sql_uri, as_sdtm=True, sdtm_domain="DM", mask_phi=True
    )
    sql_forms_db = tmp_path / "dm_forms.sqlite"
    export_mod.export_to_sql_by_form(
        sdk, "STUDY9", f"sqlite:///{sql_forms_db}", as_sdtm=True, sdtm_domain="DM", mask_phi=True
    )
    with sqlite3.connect(sql_db) as sconn:
        assert sconn.execute('SELECT "BRTHDTC" FROM dm_sql').fetchall() == [("1985",)]
        sql_cols = {r[1] for r in sconn.execute("PRAGMA table_info(dm_sql)").fetchall()}
        assert "recordId" not in sql_cols
    with sqlite3.connect(sql_forms_db) as sconn:
        assert sconn.execute('SELECT "BRTHDTC" FROM "DM"').fetchall() == [("1985",)]

    # 7. MongoDB
    mongo_coll = _InMemoryMongoCollection()
    fake_pymongo = MagicMock()
    fake_pymongo.UpdateOne.side_effect = lambda flt, doc, upsert=True: MagicMock(
        _filter=flt, _doc=doc, _upsert=upsert
    )
    fake_client = MagicMock()
    fake_client.__getitem__.return_value = {"dm": mongo_coll}
    fake_pymongo.MongoClient.return_value = fake_client
    orig_doc_dep = doc_mod._require_optional_dep
    monkeypatch.setattr(
        doc_mod,
        "_require_optional_dep",
        lambda pkg, ex: fake_pymongo if pkg == "pymongo" else orig_doc_dep(pkg, ex),
    )
    doc_mod.export_to_mongodb(
        sdk,
        "STUDY9",
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="dm",
        as_sdtm=True,
        sdtm_domain="DM",
        mask_phi=True,
    )
    mongo_docs = list(mongo_coll.docs.values())
    assert len(mongo_docs) == 1
    assert mongo_docs[0]["BRTHDTC"] == "1985"
    assert "recordId" not in mongo_docs[0]
    assert "record_id" not in mongo_docs[0]

    # 8. Neo4j
    neo_store = _InMemoryNeo4jStore()
    fake_neo4j = MagicMock()
    fake_session = MagicMock()
    fake_session.__enter__.return_value = neo_store
    fake_driver = MagicMock()
    fake_driver.session.return_value = fake_session
    fake_neo4j.GraphDatabase.driver.return_value = fake_driver
    orig_graph_dep = graph_mod._require_optional_dep
    monkeypatch.setattr(
        graph_mod,
        "_require_optional_dep",
        lambda pkg, ex: fake_neo4j if pkg == "neo4j" else orig_graph_dep(pkg, ex),
    )
    graph_mod.export_to_neo4j(
        sdk,
        "STUDY9",
        uri="bolt://localhost:7687",
        auth=("neo4j", "test"),  # pragma: allowlist secret
        as_sdtm=True,
        sdtm_domain="DM",
        mask_phi=True,
    )
    assert len(neo_store.nodes) == 1
    assert neo_store.nodes[0]["BRTHDTC"] == "1985"
    assert "recordId" not in neo_store.nodes[0]
    assert "record_id" not in neo_store.nodes[0]

    # 9. Snowflake
    sf_store = _InMemorySnowflakeStore()
    sf_driver = sf_store.make_driver()
    orig_wh_dep = wh_mod._require_optional_dep
    monkeypatch.setattr(
        wh_mod,
        "_require_optional_dep",
        lambda pkg, ex: sf_driver if pkg == "snowflake.connector" else orig_wh_dep(pkg, ex),
    )
    wh_mod.export_to_snowflake(
        sdk,
        "STUDY9",
        config=wh_mod.SnowflakeSinkConfig(
            study_key="STUDY9",
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="DM_TBL",
            local_staging_dir=str(tmp_path / "sf_stage"),
            as_sdtm=True,
            sdtm_domain="DM",
            mask_phi=True,
        ),
    )
    assert len(sf_store.rows) == 1
    assert sf_store.rows[0]["BRTHDTC"] == "1985"
    assert "RECORD_ID" not in sf_store.rows[0]
    assert "RECORDID" not in sf_store.rows[0]


def test_single_terminology_lookup_multiform_domain_filtering_and_hive_naming(
    tmp_path: Path,
) -> None:
    """_SdtmNormalizer runs terminology_lookups once, multi-Form exports filter by sdtm_domain, and Hive honors naming_convention."""
    pytest.importorskip("pyarrow")

    # 1. Chain-sensitive terminology lookup: "1" -> "2", "2" -> "3" must produce "2", NOT "3"
    study_cfg = StudyConfiguration(
        study_key="CHAIN_STUDY",
        terminology_lookups={"aeSeverity": {"1": "2", "2": "3"}},
    )
    pipeline = EnrichmentPipeline(study_cfg)
    normalizer = _SdtmNormalizer(
        domain="AE",
        study_key="CHAIN_STUDY",
        include_deleted=False,
        mask_phi=True,
        pipeline=pipeline,
    )
    rows = transform_records_to_sdtm_rows(
        [
            Record(
                record_id=1,
                study_key="CHAIN_STUDY",
                form_id=1,
                form_key="AE",
                subject_key="S01",
                record_data={"aeTerm": "Headache", "aeSeverity": "1"},
            )
        ],
        domain="AE",
        study_key="CHAIN_STUDY",
        pipeline=pipeline,
    )
    assert normalizer is not None
    assert rows[0]["AESEV"] == "2"

    # 2. Multi-Form Study exported with sdtm_domain="AE" excludes DM and VS records
    forms = [
        Form(form_id=1, form_key="AE", form_name="Adverse Events"),
        Form(form_id=2, form_key="DM", form_name="Demographics"),
        Form(form_id=3, form_key="VS", form_name="Vital Signs"),
    ]
    variables = [
        Variable(
            variable_name="aeTerm", label="AE Term", variable_type="text", form_id=1, form_key="AE"
        ),
        Variable(variable_name="sex", label="Sex", variable_type="text", form_id=2, form_key="DM"),
        Variable(
            variable_name="vsResult", label="Result", variable_type="int", form_id=3, form_key="VS"
        ),
    ]
    store = {
        1: Record(
            record_id=1,
            study_key="MULTI",
            form_id=1,
            form_key="AE",
            subject_key="S01",
            record_data={"aeTerm": "Nausea", "aeStartDate": "2026-03-02"},
        ),
        2: Record(
            record_id=2,
            study_key="MULTI",
            form_id=2,
            form_key="DM",
            subject_key="S01",
            record_data={"sex": "M", "birthDate": "1985-07-20"},
        ),
        3: Record(
            record_id=3,
            study_key="MULTI",
            form_id=3,
            form_key="VS",
            subject_key="S01",
            record_data={"vsTest": "HR", "vsResult": 72, "vsDate": "2026-03-02"},
        ),
    }
    sdk = _make_queryable_sdk(forms, variables, store)

    ae_csv = tmp_path / "ae_only.csv"
    export_mod.export_to_csv(sdk, "MULTI", str(ae_csv), as_sdtm=True, sdtm_domain="AE")
    ae_df = pd.read_csv(ae_csv)
    assert len(ae_df) == 1
    assert list(ae_df["DOMAIN"]) == ["AE"]
    assert list(ae_df["AETERM"]) == ["Nausea"]

    # 3. Hive Parquet standard export with naming_convention="sdtm" renames columns to uppercase CDISC conventions
    hive_nc = tmp_path / "hive_naming"
    parquet_mod.export_to_hive_parquet(sdk, "MULTI", str(hive_nc), naming_convention="sdtm")
    ae_part = list((hive_nc / "study_key=MULTI" / "form_key=AE").rglob("*.parquet"))
    assert len(ae_part) == 1
    nc_df = pd.read_parquet(ae_part[0])
    assert "USUBJID" in nc_df.columns
    assert "AETERM" in nc_df.columns


def test_centralized_mapper_and_sinks_assign_chronological_seq_per_domain() -> None:
    """CentralizedMapper groups mixed-domain records per domain and assigns chronological <DOM>SEQ per Subject."""
    mapper = CentralizedMapper(mode="warehouse", as_sdtm=True, mask_phi=True)
    mixed_batch = [
        Record(
            record_id=10,
            study_key="SEQ_ST",
            form_key="AE",
            subject_key="S01",
            record_data={"aeTerm": "Later AE", "aeStartDate": "2026-05-10"},
        ),
        Record(
            record_id=11,
            study_key="SEQ_ST",
            form_key="VS",
            subject_key="S01",
            record_data={"vsTest": "Pulse", "vsResult": 80, "vsDate": "2026-05-05"},
        ),
        Record(
            record_id=12,
            study_key="SEQ_ST",
            form_key="AE",
            subject_key="S01",
            record_data={"aeTerm": "Earlier AE", "aeStartDate": "2026-05-01"},
        ),
        Record(
            record_id=13,
            study_key="SEQ_ST",
            form_key="VS",
            subject_key="S01",
            record_data={"vsTest": "BP", "vsResult": 120, "vsDate": "2026-05-02"},
        ),
    ]
    mapped = mapper.map_records(mixed_batch, study_key="SEQ_ST")
    ae_rows = [r for r in mapped if r["DOMAIN"] == "AE"]
    vs_rows = [r for r in mapped if r["DOMAIN"] == "VS"]
    assert [(r["AETERM"], r["AESEQ"]) for r in ae_rows] == [
        ("Earlier AE", 1),
        ("Later AE", 2),
    ]
    assert [(r["VSTEST"], r["VSSEQ"]) for r in vs_rows] == [
        ("BP", 1),
        ("Pulse", 2),
    ]


def test_incremental_sdtm_subject_refresh_across_tabular_hive_and_plugin_sinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Incremental SDTM syncs refresh active Subject records, purge voided records, and renumber AESEQ chronologically."""
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("openpyxl")
    pytest.importorskip("pyarrow")

    forms = [Form(form_id=1, form_key="AE", form_name="Adverse Events")]
    variables = [
        Variable(
            variable_name="aeTerm", label="AE Term", variable_type="text", form_id=1, form_key="AE"
        ),
        Variable(
            variable_name="aeStartDate",
            label="AE Start Date",
            variable_type="text",
            form_id=1,
            form_key="AE",
        ),
    ]

    # Initial state:
    # S01 has two AEs (2026-05-05 -> AESEQ=1, 2026-05-10 -> AESEQ=2)
    # S02 has one AE  (2026-05-03 -> AESEQ=1)
    records_store: dict[int, Record] = {
        1: Record(
            record_id=1,
            study_key="INC_SDTM",
            form_id=1,
            form_key="AE",
            subject_key="S01",
            deleted=False,
            date_modified="2026-06-01T10:05:00Z",
            record_data={"aeTerm": "Mid Event", "aeStartDate": "2026-05-05"},
        ),
        2: Record(
            record_id=2,
            study_key="INC_SDTM",
            form_id=1,
            form_key="AE",
            subject_key="S01",
            deleted=False,
            date_modified="2026-06-01T10:10:00Z",
            record_data={"aeTerm": "Late Event", "aeStartDate": "2026-05-10"},
        ),
        3: Record(
            record_id=3,
            study_key="INC_SDTM",
            form_id=1,
            form_key="AE",
            subject_key="S02",
            deleted=False,
            date_modified="2026-06-01T10:00:00Z",
            record_data={"aeTerm": "S02 Event", "aeStartDate": "2026-05-03"},
        ),
    }
    seen_calls: list[dict[str, Any]] = []
    sdk = _make_queryable_sdk(forms, variables, records_store, observed_calls=seen_calls)

    ledger = tmp_path / "sdtm_ledger.json"
    csv_out = tmp_path / "ae.csv"
    pq_out = tmp_path / "ae.parquet"
    hive_out = tmp_path / "hive_ae"
    xl_out = tmp_path / "ae.xlsx"
    json_out = tmp_path / "ae.json"
    ddb_out = tmp_path / "ae.duckdb"
    sql_db = tmp_path / "ae.sqlite"
    sql_uri = f"sqlite:///{sql_db}"

    # Configure MongoDB, Neo4j, and Snowflake in-memory stores
    mongo_coll = _InMemoryMongoCollection()
    fake_pymongo = MagicMock()
    fake_pymongo.UpdateOne.side_effect = lambda flt, doc, upsert=True: MagicMock(
        _filter=flt, _doc=doc, _upsert=upsert
    )
    fake_client = MagicMock()
    fake_client.__getitem__.return_value = {"ae": mongo_coll}
    fake_pymongo.MongoClient.return_value = fake_client
    orig_doc_dep = doc_mod._require_optional_dep
    monkeypatch.setattr(
        doc_mod,
        "_require_optional_dep",
        lambda pkg, ex: fake_pymongo if pkg == "pymongo" else orig_doc_dep(pkg, ex),
    )

    neo_store = _InMemoryNeo4jStore()
    fake_neo4j = MagicMock()
    fake_session = MagicMock()
    fake_session.__enter__.return_value = neo_store
    fake_driver = MagicMock()
    fake_driver.session.return_value = fake_session
    fake_neo4j.GraphDatabase.driver.return_value = fake_driver
    orig_graph_dep = graph_mod._require_optional_dep
    monkeypatch.setattr(
        graph_mod,
        "_require_optional_dep",
        lambda pkg, ex: fake_neo4j if pkg == "neo4j" else orig_graph_dep(pkg, ex),
    )

    sf_store = _InMemorySnowflakeStore()
    sf_driver = sf_store.make_driver()
    orig_wh_dep = wh_mod._require_optional_dep
    monkeypatch.setattr(
        wh_mod,
        "_require_optional_dep",
        lambda pkg, ex: sf_driver if pkg == "snowflake.connector" else orig_wh_dep(pkg, ex),
    )

    def _run_all_syncs() -> None:
        common = {
            "as_sdtm": True,
            "sdtm_domain": "AE",
            "incremental": True,
            "ledger_path": str(ledger),
        }
        export_mod.export_to_csv(sdk, "INC_SDTM", str(csv_out), stream_name="s_csv", **common)
        export_mod.export_to_parquet(sdk, "INC_SDTM", str(pq_out), stream_name="s_pq", **common)
        parquet_mod.export_to_hive_parquet(
            sdk, "INC_SDTM", str(hive_out), stream_name="s_hive", **common
        )
        export_mod.export_to_excel(sdk, "INC_SDTM", str(xl_out), stream_name="s_xl", **common)
        export_mod.export_to_json(sdk, "INC_SDTM", str(json_out), stream_name="s_json", **common)
        export_mod.export_to_duckdb(
            sdk, "INC_SDTM", str(ddb_out), "ae_tbl", stream_name="s_ddb", **common
        )
        export_mod.export_to_sql(sdk, "INC_SDTM", "ae_sql", sql_uri, stream_name="s_sql", **common)
        doc_mod.export_to_mongodb(
            sdk,
            "INC_SDTM",
            uri="mongodb://localhost:27017",
            database="clinical",
            collection="ae",
            stream_name="s_mongo",
            **common,
        )
        graph_mod.export_to_neo4j(
            sdk,
            "INC_SDTM",
            uri="bolt://localhost:7687",
            auth=("neo4j", "test"),  # pragma: allowlist secret
            stream_name="s_neo",
            **common,
        )
        wh_mod.export_to_snowflake(
            sdk,
            "INC_SDTM",
            config=wh_mod.SnowflakeSinkConfig(
                study_key="INC_SDTM",
                account="acct",
                user="usr",
                password="pwd",  # pragma: allowlist secret
                database="DB",
                schema="PUBLIC",
                warehouse="WH",
                stage="STG",
                table="AE_TBL",
                local_staging_dir=str(tmp_path / "sf_inc_stage"),
                stream_name="s_sf",
                **common,
            ),
        )

    # Run 1: initial full load
    _run_all_syncs()

    # Run 2 delta:
    # - Record 1 (S01 "Mid Event") is VOIDED at 2026-06-02T12:00:00Z
    # - Record 4 (S01 "Earliest Event", aeStartDate=2026-05-01) is ADDED at 2026-06-02T12:05:00Z
    # Record 2 (S01 "Late Event", 2026-05-10) was NOT modified in delta, so re-fetching active
    # records for S01 must yield:
    #   S01: ("Earliest Event", AESEQ=1), ("Late Event", AESEQ=2)
    #   S02: ("S02 Event", AESEQ=1)  -- untouched!
    records_store[1].deleted = True
    records_store[1].date_modified = "2026-06-02T12:00:00Z"
    records_store[4] = Record(
        record_id=4,
        study_key="INC_SDTM",
        form_id=1,
        form_key="AE",
        subject_key="S01",
        deleted=False,
        date_modified="2026-06-02T12:05:00Z",
        record_data={"aeTerm": "Earliest Event", "aeStartDate": "2026-05-01"},
    )
    seen_calls.clear()

    _run_all_syncs()

    # Verify targeted subject refresh batched subjectKey and formId filters
    assert any(
        ("subjectKey" in c or "subjectKey==" in str(c.get("filter", "")))
        and ("formId" in c or "formId==" in str(c.get("filter", "")))
        for c in seen_calls
    ), f"Observed calls: {seen_calls}"
    # Verify Snowflake never queried absent STUDY_KEY or RECORD_ID columns
    assert all("STUDY_KEY" not in sql and "RECORD_ID" not in sql for sql in sf_store.executed_sql)

    expected_tuples = [
        ("S01", 1, "Earliest Event"),
        ("S01", 2, "Late Event"),
        ("S02", 1, "S02 Event"),
    ]

    def _extract_tuples(df: pd.DataFrame) -> list[tuple[str, int, str]]:
        return sorted(
            (str(r["USUBJID"]), int(r["AESEQ"]), str(r["AETERM"])) for _, r in df.iterrows()
        )

    assert _extract_tuples(pd.read_csv(csv_out)) == expected_tuples
    assert _extract_tuples(pd.read_parquet(pq_out)) == expected_tuples
    hive_files = sorted((hive_out / "study_key=INC_SDTM" / "form_key=AE").rglob("*.parquet"))
    hive_combined = pd.concat([pd.read_parquet(f) for f in hive_files], ignore_index=True)
    assert _extract_tuples(hive_combined) == expected_tuples
    assert _extract_tuples(pd.read_excel(xl_out)) == expected_tuples

    json_loaded = json.loads(json_out.read_text(encoding="utf-8"))
    assert (
        sorted((str(r["USUBJID"]), int(r["AESEQ"]), str(r["AETERM"])) for r in json_loaded)
        == expected_tuples
    )

    with duckdb.connect(str(ddb_out), read_only=True, config={"threads": "1"}) as con:
        ddb_rows = con.execute(
            'SELECT "USUBJID", "AESEQ", "AETERM" FROM ae_tbl ORDER BY "USUBJID", "AESEQ"'
        ).fetchall()
    assert [(str(u), int(s), str(t)) for u, s, t in ddb_rows] == expected_tuples

    with sqlite3.connect(sql_db) as sconn:
        sql_rows = sconn.execute(
            'SELECT "USUBJID", "AESEQ", "AETERM" FROM ae_sql ORDER BY "USUBJID", "AESEQ"'
        ).fetchall()
    assert [(str(u), int(s), str(t)) for u, s, t in sql_rows] == expected_tuples

    assert (
        sorted(
            (str(d["USUBJID"]), int(d["AESEQ"]), str(d["AETERM"])) for d in mongo_coll.docs.values()
        )
        == expected_tuples
    )
    assert (
        sorted((str(n["USUBJID"]), int(n["AESEQ"]), str(n["AETERM"])) for n in neo_store.nodes)
        == expected_tuples
    )
    assert (
        sorted((str(r["USUBJID"]), int(r["AESEQ"]), str(r["AETERM"])) for r in sf_store.rows)
        == expected_tuples
    )


def test_watermark_never_advances_when_transformation_errors_prevent_durable_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """State providers roll back and preserve prior watermarks when transformation errors prevent writes."""
    pytest.importorskip("pyarrow")

    forms = [Form(form_id=1, form_key="VITALS", form_name="Vitals")]
    variables = [
        Variable(
            variable_name="HR",
            label="Heart Rate",
            variable_type="integer",
            form_id=1,
            form_key="VITALS",
        )
    ]
    prior_ts = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    ledger_path = tmp_path / "err_ledger.json"
    provider = FileStateProvider(str(ledger_path))
    provider.set_last_timestamp("ERR_STUDY", "csv_err", prior_ts, records_processed=1)
    provider.set_last_timestamp("ERR_STUDY", "hive_err_VITALS", prior_ts, records_processed=1)
    provider.set_last_timestamp("ERR_STUDY", "sdtm_err", prior_ts, records_processed=1)

    bad_rec = Record(
        record_id=99,
        study_key="ERR_STUDY",
        form_id=1,
        form_key="VITALS",
        subject_key="S01",
        date_modified="2026-05-10T00:00:00Z",
        record_data={"HR": "NOT_AN_INTEGER"},
    )
    sdk = _make_queryable_sdk(forms, variables, {99: bad_rec})

    from imednet.models.study_config import MappingRule

    class _FailingPipeline:
        phi_keys: set[str] = set()
        mappings: dict[str, Any] = {
            "HR": MappingRule(
                domain="VS",
                target_field="HR",
                source_form_key="VITALS",
                source_variable_name="HR",
            )
        }
        terminology: dict[str, dict[str, str]] = {}

        def process(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise ValueError("Simulated transformation failure")

    failing_pipeline = _FailingPipeline()
    monkeypatch.setattr(export_mod, "resolve_enrichment_pipeline", lambda _sk: failing_pipeline)
    monkeypatch.setattr(parquet_mod, "resolve_enrichment_pipeline", lambda _sk: failing_pipeline)
    import imednet_workflows.record_mapper as rm_mod

    monkeypatch.setattr(rm_mod, "resolve_enrichment_pipeline", lambda _sk: failing_pipeline)

    with pytest.raises(ExportBatchError):
        export_mod.export_to_csv(
            sdk,
            "ERR_STUDY",
            str(tmp_path / "err.csv"),
            incremental=True,
            stream_name="csv_err",
            state_provider=provider,
            quality_gate_enabled=False,
        )
    assert provider.get_last_timestamp("ERR_STUDY", "csv_err") == prior_ts
    assert provider.get_last_status("ERR_STUDY", "csv_err") == StreamExecutionStatus.FAILED

    with pytest.raises(ExportBatchError):
        parquet_mod.export_to_hive_parquet(
            sdk,
            "ERR_STUDY",
            str(tmp_path / "err_hive"),
            incremental=True,
            stream_name="hive_err",
            state_provider=provider,
            quality_gate_enabled=False,
        )
    assert provider.get_last_timestamp("ERR_STUDY", "hive_err_VITALS") == prior_ts
    assert provider.get_last_status("ERR_STUDY", "hive_err_VITALS") == StreamExecutionStatus.FAILED

    with pytest.raises(ValueError, match="Simulated transformation failure"):
        export_mod.export_to_csv(
            sdk,
            "ERR_STUDY",
            str(tmp_path / "err_sdtm.csv"),
            as_sdtm=True,
            sdtm_domain="VS",
            incremental=True,
            stream_name="sdtm_err",
            state_provider=provider,
            quality_gate_enabled=False,
        )
    assert provider.get_last_timestamp("ERR_STUDY", "sdtm_err") == prior_ts
    assert provider.get_last_status("ERR_STUDY", "sdtm_err") == StreamExecutionStatus.FAILED


def test_sql_and_quality_gate_retain_records_when_optional_or_recommended_variables_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQL readiness gating retains Study Records when optional schema Variables or recommended SDTM Variables are absent."""
    forms = [Form(form_id=1, form_key="AE", form_name="Adverse Events")]
    variables = [
        Variable(
            variable_name="aeTerm",
            label="AE Term",
            variable_type="text",
            form_id=1,
            form_key="AE",
            required=True,
        ),
        Variable(
            variable_name="aeSeverity",
            label="Severity",
            variable_type="text",
            form_id=1,
            form_key="AE",
            required=True,
        ),
        # 4 optional Variables omitted from recordData -> must NOT reduce readiness score below 100%
        Variable(
            variable_name="optComment1",
            label="Comment 1",
            variable_type="text",
            form_id=1,
            form_key="AE",
            required=False,
        ),
        Variable(
            variable_name="optComment2",
            label="Comment 2",
            variable_type="text",
            form_id=1,
            form_key="AE",
            required=False,
        ),
        Variable(
            variable_name="optComment3",
            label="Comment 3",
            variable_type="text",
            form_id=1,
            form_key="AE",
            required=False,
        ),
        Variable(
            variable_name="optComment4",
            label="Comment 4",
            variable_type="text",
            form_id=1,
            form_key="AE",
            required=False,
        ),
    ]
    rec_vaccine = Record(
        record_id=1,
        study_key="SQL_GATE",
        form_id=1,
        form_key="AE",
        subject_key="S01",
        record_data={
            "aeTerm": "Mild Rash",
            "aeSeverity": "MILD",
            "aeRelationship": "RELATED",
            # Recommended fields aeDecod, aeActionTaken, aeOutcome are absent in vaccine profile (WARNING only)
        },
    )
    rec_biologic = Record(
        record_id=2,
        study_key="SQL_GATE",
        form_id=1,
        form_key="AE",
        subject_key="S02",
        record_data={
            "aeTerm": "Infusion Reaction",
            "aeDecod": "INFUSION RELATED REACTION",
            "aeSeverity": "MILD",
            "aeRelationship": "RELATED",
            # Recommended fields aeActionTaken, aeOutcome are absent in biologic profile (WARNING only)
        },
    )
    sdk_vac = _make_queryable_sdk(forms, variables, {1: rec_vaccine})
    sdk_bio = _make_queryable_sdk(forms, variables, {2: rec_biologic})

    sql_db = tmp_path / "gated.sqlite"
    export_mod.export_to_sql(
        sdk_vac,
        "SQL_GATE",
        "ae_gated_vac",
        f"sqlite:///{sql_db}",
        quality_gate_enabled=True,
        min_schema_readiness_score=100.0,
        standards_profile="vaccine",
    )
    export_mod.export_to_sql(
        sdk_bio,
        "SQL_GATE",
        "ae_gated_bio",
        f"sqlite:///{sql_db}",
        quality_gate_enabled=True,
        min_schema_readiness_score=100.0,
        standards_profile="biologic",
    )
    with sqlite3.connect(sql_db) as sconn:
        vac_rows = sconn.execute('SELECT "aeTerm", "aeSeverity" FROM ae_gated_vac').fetchall()
        bio_rows = sconn.execute('SELECT "aeTerm", "aeSeverity" FROM ae_gated_bio').fetchall()
    assert vac_rows == [("Mild Rash", "MILD")]
    assert bio_rows == [("Infusion Reaction", "MILD")]

    # Also verify evaluate_standards_readiness honors StudyConfiguration.reporting_profile & terminology_lookups
    import imednet.integrations.enrichment as enrich_mod

    study_cfg = StudyConfiguration(
        study_key="CFG_STUDY",
        reporting_profile="vaccine",
        terminology_lookups={"aeSeverity": {"G1": "MILD"}},
    )
    monkeypatch.setattr(
        enrich_mod,
        "resolve_study_configuration",
        lambda sk: study_cfg if sk == "CFG_STUDY" else None,
    )
    report = evaluate_standards_readiness(
        [
            Record(
                record_id=2,
                study_key="CFG_STUDY",
                form_id=1,
                form_key="AE",
                subject_key="S01",
                record_data={
                    "aeTerm": "Soreness",
                    "aeSeverity": "G1",
                    "aeRelationship": "RELATED",
                    "aeDecod": "INJECTION SITE PAIN",
                    "aeActionTaken": "NONE",
                    "aeOutcome": "RECOVERED",
                },
            )
        ],
        study_key="CFG_STUDY",
    )
    assert report.profile == "vaccine"
    assert report.score == 100.0
    assert report.is_ready is True
    assert list(
        apply_quality_gate(
            sdk_vac,
            "SQL_GATE",
            [rec_vaccine],
            export_mod.TabularSinkConfig(
                study_key="SQL_GATE",
                quality_gate_enabled=True,
                min_schema_readiness_score=100.0,
                standards_profile="vaccine",
            ),
            variables=variables,
            forms=forms,
        )
    ) == [rec_vaccine]
