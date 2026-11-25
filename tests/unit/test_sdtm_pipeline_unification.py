"""Comprehensive tests for unified SPI SDTM pipeline across all 13 destinations and CLI."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import imednet.integrations.enrichment as enrichment_mod
from imednet import ImednetSDK
from imednet import cli as cli_mod
from imednet.integrations.export import (
    export_to_csv,
    export_to_duckdb,
    export_to_duckdb_by_form,
    export_to_excel,
    export_to_hive_parquet,
    export_to_json,
    export_to_long_sql,
    export_to_parquet,
    export_to_sql,
    export_to_sql_by_form,
)
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.study_config import StudyConfiguration
from imednet.models.variables import Variable


def _is_missing(val: Any) -> bool:
    if val is None or val is pd.NA:
        return True
    if isinstance(val, float) and pd.isna(val):
        return True
    return False


def _build_stateful_sdtm_sdk(
    upstream_records: dict[int, Record],
    *,
    forms: list[Form] | None = None,
    variables: list[Variable] | None = None,
) -> MagicMock:
    """Create a stateful mock SDK supporting forms, variables, filters, and watermarks."""
    study_forms = (
        forms
        if forms is not None
        else [
            Form(
                form_id=301,
                form_key="F_DM",
                form_name="Demographics",
                form_type="Enrollment",
                date_created="2026-01-01T00:00:00Z",
                date_modified="2026-01-01T00:00:00Z",
            ),
            Form(
                form_id=302,
                form_key="F_VS",
                form_name="Vital Signs",
                form_type="Standard",
                date_created="2026-01-01T00:00:00Z",
                date_modified="2026-01-01T00:00:00Z",
            ),
            Form(
                form_id=303,
                form_key="F_AE",
                form_name="Adverse Events",
                form_type="Standard",
                date_created="2026-01-01T00:00:00Z",
                date_modified="2026-01-01T00:00:00Z",
            ),
        ]
    )
    study_vars = (
        variables
        if variables is not None
        else [
            Variable(
                variable_id=1,
                variable_name="BRTHDTC",
                label="Birth Date",
                variable_type="text",
                form_id=301,
                form_key="F_DM",
            ),
            Variable(
                variable_id=2,
                variable_name="SEX",
                label="Sex",
                variable_type="text",
                form_id=301,
                form_key="F_DM",
            ),
            Variable(
                variable_id=3,
                variable_name="patientName",
                label="Patient Name",
                variable_type="text",
                form_id=301,
                form_key="F_DM",
            ),
            Variable(
                variable_id=4,
                variable_name="customMrn",
                label="Custom MRN",
                variable_type="text",
                form_id=301,
                form_key="F_DM",
            ),
            Variable(
                variable_id=5,
                variable_name="VSTESTCD",
                label="VS Test Code",
                variable_type="text",
                form_id=302,
                form_key="F_VS",
            ),
            Variable(
                variable_id=6,
                variable_name="VSORRES",
                label="VS Result",
                variable_type="float",
                form_id=302,
                form_key="F_VS",
            ),
            Variable(
                variable_id=7,
                variable_name="AETERM",
                label="AE Term",
                variable_type="text",
                form_id=303,
                form_key="F_AE",
            ),
        ]
    )

    def _list_records(**kw: Any) -> Any:
        from imednet.integrations.sink_base import (
            extract_record_timestamp,
            normalize_watermark_datetime,
        )

        flt = str(kw.get("filter", "") or "")
        items = list(upstream_records.values())
        if kw.get("deleted") is False:
            items = [r for r in items if not r.deleted]
        elif kw.get("deleted") is True:
            items = [r for r in items if r.deleted]
        if kw.get("dateModified") is not None:
            dm = kw["dateModified"]
            if isinstance(dm, tuple) and len(dm) == 2:
                op_str, raw_ts = dm
                wm_dt = normalize_watermark_datetime(raw_ts)
                if wm_dt is not None:
                    if op_str == ">=":
                        items = [
                            r
                            for r in items
                            if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                        ]
                    elif op_str == ">":
                        items = [
                            r
                            for r in items
                            if (ts := extract_record_timestamp(r)) is not None and ts > wm_dt
                        ]
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
                wm_str = clause.split(">=", 1)[1]
                wm_dt = normalize_watermark_datetime(wm_str)
                if wm_dt is not None:
                    items = [
                        r
                        for r in items
                        if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                    ]
            elif clause.startswith("formId=="):
                fid = int(clause.split("==", 1)[1])
                items = [r for r in items if r.form_id == fid]
        if kw.get("sort") == "subjectKey,asc;recordId,asc":
            items.sort(key=lambda r: (str(r.subject_key or ""), int(r.record_id or 0)))
        return iter([r.model_copy(deep=True) for r in items])

    sdk = MagicMock()
    sdk.get_forms.return_value = study_forms
    sdk.forms.list.return_value = study_forms
    sdk.get_intervals.return_value = []
    sdk.get_variables.return_value = study_vars
    sdk.variables.list.side_effect = lambda **kw: (
        [
            v
            for v in study_vars
            if v.form_id in (kw["formId"] if isinstance(kw["formId"], list) else [kw["formId"]])
        ]
        if "formId" in kw
        else list(study_vars)
    )
    sdk.records.list.side_effect = _list_records
    return sdk


def _assert_dm_brthdtc_and_phi_rows(
    rows: list[dict[str, Any]],
    *,
    dest_label: str,
) -> None:
    """Assert BRTHDTC='1981' & None, PHI masked, and zero synthetic EDC columns."""
    assert len(rows) == 2, f"{dest_label}: expected 2 DM rows, got {rows!r}"
    serialized = json.dumps(rows, default=str)
    assert "1981-05-14" not in serialized, f"{dest_label} leaked full birth date"
    assert "Jane Secret" not in serialized, f"{dest_label} leaked patientName"
    assert "MRN-SECRET-01" not in serialized, f"{dest_label} leaked customMrn"

    by_subj = {str(r["USUBJID"]): r for r in rows}
    assert set(by_subj) == {"S01", "S02"}, f"{dest_label}: {by_subj}"

    for subj, row in by_subj.items():
        assert "recordId" not in row, f"{dest_label} leaked recordId: {row}"
        assert "record_id" not in row, f"{dest_label} leaked record_id: {row}"
        assert "DMSEQ" not in row or _is_missing(row.get("DMSEQ")), f"{dest_label} emitted DMSEQ"
        assert row["DOMAIN"] == "DM"
        assert row["STUDYID"] == "STUDY_PHI"
        for key, val in row.items():
            if str(key).upper() in {"PATIENTNAME", "CUSTOMMRN"} and not _is_missing(val):
                assert val == "***MASKED***", f"{dest_label} {subj} {key}={val!r}"

    assert by_subj["S01"]["BRTHDTC"] == "1981", (
        f"{dest_label}: expected '1981', got {by_subj['S01']['BRTHDTC']!r}"
    )
    assert _is_missing(by_subj["S02"].get("BRTHDTC")), (
        f"{dest_label}: expected None/missing for null BRTHDTC, "
        f"got {by_subj['S02'].get('BRTHDTC')!r}"
    )


def test_all_13_destinations_and_airflow_operator_single_stage_sdtm_phi_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1: All 13 destinations and ImednetExportOperator emit '1981' and None for BRTHDTC."""
    from tests.utils.airflow_mocks import setup_dummy_airflow

    import imednet_sinks.document as doc_mod
    import imednet_sinks.graph as graph_mod
    import imednet_sinks.warehouse as wh_mod
    from imednet_sinks.document import MongoDbSinkConfig, export_to_mongodb
    from imednet_sinks.graph import Neo4jSinkConfig, export_to_neo4j
    from imednet_sinks.warehouse import SnowflakeSinkConfig, export_to_snowflake

    study_key = "STUDY_PHI"
    study_cfg = StudyConfiguration(
        studyKey=study_key,
        version="1.0.0",
        reportingProfile="general",
        phiFields=["customMrn"],
    )
    monkeypatch.setattr(
        enrichment_mod,
        "resolve_study_configuration",
        lambda sk: study_cfg if sk == study_key else None,
    )

    upstream = {
        1: Record(
            record_id=1,
            study_key=study_key,
            subject_key="S01",
            form_id=301,
            form_key="F_DM",
            record_status="Complete",
            date_modified=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
            record_data={
                "BRTHDTC": "1981-05-14",
                "SEX": "F",
                "patientName": "Jane Secret",
                "customMrn": "MRN-SECRET-01",
            },
        ),
        2: Record(
            record_id=2,
            study_key=study_key,
            subject_key="S02",
            form_id=301,
            form_key="F_DM",
            record_status="Complete",
            date_modified=datetime(2026, 5, 1, 10, 5, tzinfo=UTC),
            record_data={
                "BRTHDTC": None,
                "SEX": "M",
                "patientName": "Jane Secret",
                "customMrn": "MRN-SECRET-01",
            },
        ),
    }
    sdk = _build_stateful_sdtm_sdk(
        upstream,
        forms=[
            Form(
                form_id=301,
                form_key="F_DM",
                form_name="Demographics",
                form_type="Enrollment",
                date_created="2026-01-01T00:00:00Z",
                date_modified="2026-01-01T00:00:00Z",
            )
        ],
    )

    # 1. CSV
    csv_path = tmp_path / "dm.csv"
    export_to_csv(sdk, study_key, csv_path, as_sdtm=True, mask_phi=True)
    _assert_dm_brthdtc_and_phi_rows(
        pd.read_csv(csv_path, dtype=str).to_dict(orient="records"), dest_label="csv"
    )

    # 2. Parquet
    pq_path = tmp_path / "dm.parquet"
    export_to_parquet(sdk, study_key, pq_path, as_sdtm=True, mask_phi=True)
    _assert_dm_brthdtc_and_phi_rows(
        pd.read_parquet(pq_path).to_dict(orient="records"), dest_label="parquet"
    )

    # 3. Excel (verify dtype=str readback preserves '1981' string and null BRTHDTC)
    xl_path = tmp_path / "dm.xlsx"
    export_to_excel(sdk, study_key, xl_path, as_sdtm=True, mask_phi=True)
    _assert_dm_brthdtc_and_phi_rows(
        pd.read_excel(xl_path, dtype=str).to_dict(orient="records"), dest_label="excel"
    )

    # 4. JSON
    json_path = tmp_path / "dm.json"
    export_to_json(sdk, study_key, json_path, as_sdtm=True, mask_phi=True)
    json_rows = json.loads(json_path.read_text(encoding="utf-8"))
    assert json_rows[0]["BRTHDTC"] == "1981" or json_rows[1]["BRTHDTC"] == "1981"
    _assert_dm_brthdtc_and_phi_rows(json_rows, dest_label="json")

    # 5. DuckDB
    ddb_path = tmp_path / "dm.duckdb"
    export_to_duckdb(sdk, study_key, ddb_path, "dm_tbl", as_sdtm=True, mask_phi=True)
    with duckdb.connect(str(ddb_path), read_only=True) as dconn:
        _assert_dm_brthdtc_and_phi_rows(
            dconn.execute('SELECT * FROM "dm_tbl"').df().to_dict(orient="records"),
            dest_label="duckdb",
        )

    # 6. DuckDB by Form
    ddb_bf_path = tmp_path / "dm_bf.duckdb"
    export_to_duckdb_by_form(sdk, study_key, ddb_bf_path, as_sdtm=True, mask_phi=True)
    with duckdb.connect(str(ddb_bf_path), read_only=True) as dconn:
        _assert_dm_brthdtc_and_phi_rows(
            dconn.execute('SELECT * FROM "F_DM"').df().to_dict(orient="records"),
            dest_label="duckdb_by_form",
        )

    # 7. SQL
    sql_path = tmp_path / "dm.sqlite"
    export_to_sql(sdk, study_key, "dm_tbl", f"sqlite:///{sql_path}", as_sdtm=True, mask_phi=True)
    with sqlite3.connect(sql_path) as sconn:
        _assert_dm_brthdtc_and_phi_rows(
            pd.read_sql_query('SELECT * FROM "dm_tbl"', sconn).to_dict(orient="records"),
            dest_label="sql",
        )

    # 8. SQL by Form
    sql_bf_path = tmp_path / "dm_bf.sqlite"
    export_to_sql_by_form(sdk, study_key, f"sqlite:///{sql_bf_path}", as_sdtm=True, mask_phi=True)
    with sqlite3.connect(sql_bf_path) as sconn:
        _assert_dm_brthdtc_and_phi_rows(
            pd.read_sql_query('SELECT * FROM "F_DM"', sconn).to_dict(orient="records"),
            dest_label="sql_by_form",
        )

    # 9. Long SQL
    long_sql_path = tmp_path / "dm_long.sqlite"
    export_to_long_sql(
        sdk, study_key, "dm_long_tbl", f"sqlite:///{long_sql_path}", as_sdtm=True, mask_phi=True
    )
    with sqlite3.connect(long_sql_path) as sconn:
        _assert_dm_brthdtc_and_phi_rows(
            pd.read_sql_query('SELECT * FROM "dm_long_tbl"', sconn).to_dict(orient="records"),
            dest_label="long_sql",
        )

    # 10. Hive Parquet
    hive_dir = tmp_path / "hive_dm"
    export_to_hive_parquet(sdk, study_key, hive_dir, as_sdtm=True, mask_phi=True)
    hive_files = sorted(hive_dir.rglob("*.parquet"))
    assert len(hive_files) >= 1
    hive_rows = pd.concat([pd.read_parquet(f) for f in hive_files], ignore_index=True).to_dict(
        orient="records"
    )
    _assert_dm_brthdtc_and_phi_rows(hive_rows, dest_label="hive_parquet")

    # 11. MongoDB
    mongo_docs: list[dict[str, Any]] = []
    pymongo_mod = MagicMock()
    pymongo_mod.UpdateOne.side_effect = lambda flt, upd, upsert=False: (flt, upd["$set"], upsert)
    collection = MagicMock()
    collection.bulk_write.side_effect = lambda ops, ordered=False: mongo_docs.extend(
        [dict(op[1]) for op in ops]
    )
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    client = MagicMock()
    client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)

    export_to_mongodb(
        sdk,
        study_key,
        config=MongoDbSinkConfig(
            study_key=study_key,
            uri="mongodb://localhost:27017",
            database="clinical",
            collection="dm",
            as_sdtm=True,
            mask_phi=True,
        ),
    )
    cleaned_mongo = [{k: v for k, v in d.items() if k != "_id"} for d in mongo_docs]
    _assert_dm_brthdtc_and_phi_rows(cleaned_mongo, dest_label="mongodb")

    # 12. Neo4j
    neo4j_rows: list[dict[str, Any]] = []
    neo4j_mod = MagicMock()
    session = MagicMock()

    def _capture_neo4j(query: str, **params: Any) -> Any:
        if "UNWIND $rows AS row" in query and "rows" in params:
            for r in params["rows"]:
                neo4j_rows.append(dict(r))
        return MagicMock()

    session.run.side_effect = _capture_neo4j
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    driver = MagicMock()
    driver.session.return_value = session_cm
    neo4j_mod.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    export_to_neo4j(
        sdk,
        study_key,
        config=Neo4jSinkConfig(
            study_key=study_key,
            uri="bolt://localhost:7687",
            auth=("neo4j", "secret"),
            as_sdtm=True,
            mask_phi=True,
        ),
    )
    _assert_dm_brthdtc_and_phi_rows(neo4j_rows, dest_label="neo4j")

    # 13. Snowflake
    sf_staged_rows: list[dict[str, Any]] = []
    sf_mod = MagicMock()
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cursor
    sf_mod.connect.return_value = conn

    orig_write_table = pq.write_table

    def _capture_sf_parquet(table: Any, where: Any, *a: Any, **kw: Any) -> Any:
        orig_write_table(table, where, *a, **kw)
        sf_staged_rows.extend(pq.read_table(where).to_pylist())

    monkeypatch.setattr(pq, "write_table", _capture_sf_parquet)
    monkeypatch.setattr(
        wh_mod,
        "_require_optional_dep",
        lambda pkg, _extra: (
            sf_mod if pkg == "snowflake.connector" else (pq if "parquet" in pkg else pa)
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
            table="DM",
            as_sdtm=True,
            mask_phi=True,
        ),
    )
    _assert_dm_brthdtc_and_phi_rows(sf_staged_rows, dest_label="snowflake")

    # 14. Airflow ImednetExportOperator (Excel + MongoDB)
    setup_dummy_airflow(monkeypatch)
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    af_xl_path = tmp_path / "airflow_dm.xlsx"
    op_xl = ImednetExportOperator(
        task_id="af_sdtm_excel",
        study_key=study_key,
        destination="excel",
        output_path=str(af_xl_path),
        as_sdtm=True,
        mask_phi=True,
    )
    monkeypatch.setattr(op_xl, "_get_sdk", lambda: sdk)
    op_xl.execute({})
    _assert_dm_brthdtc_and_phi_rows(
        pd.read_excel(af_xl_path, dtype=str).to_dict(orient="records"),
        dest_label="ImednetExportOperator[excel]",
    )

    mongo_docs.clear()
    sdk.sinks = SimpleNamespace(
        MongoDbExportSink=doc_mod.MongoDbExportSink,
        MongoDbSinkConfig=doc_mod.MongoDbSinkConfig,
    )
    fake_mongo_conn = SimpleNamespace(
        conn_type="mongo",
        host="localhost",
        port=27017,
        login=None,
        password=None,
        schema="clinical",
        extra={"database": "clinical", "collection": "af_dm"},
        get_uri=lambda: "mongodb://localhost:27017/clinical",
    )
    monkeypatch.setattr(
        ImednetHook,
        "_get_airflow_connection",
        classmethod(lambda cls, conn_id: fake_mongo_conn if conn_id == "mongo_af" else None),
    )
    op_mongo = ImednetExportOperator(
        task_id="af_sdtm_mongo",
        study_key=study_key,
        destination="mongodb",
        destination_conn_id="mongo_af",
        as_sdtm=True,
        mask_phi=True,
    )
    monkeypatch.setattr(op_mongo, "_get_sdk", lambda: sdk)
    op_mongo.execute({})
    cleaned_af_mongo = [{k: v for k, v in d.items() if k != "_id"} for d in mongo_docs]
    _assert_dm_brthdtc_and_phi_rows(cleaned_af_mongo, dest_label="ImednetExportOperator[mongodb]")


def test_hive_parquet_as_sdtm_on_simulation_study_zero_nonetype_errors(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 2: export_to_hive_parquet(..., as_sdtm=True) writes partitioned Parquet files cleanly."""
    sdk = ImednetSDK(simulation_mode=True)
    out_dir = tmp_path / "sim_hive_sdtm"

    with caplog.at_level(logging.WARNING):
        export_to_hive_parquet(sdk, "STUDY", out_dir, as_sdtm=True, mask_phi=True)

    assert "NoneType" not in caplog.text

    study_part = out_dir / "study_key=STUDY"
    dm_files = list((study_part / "form_key=F_DM").rglob("*.parquet"))
    vs_files = list((study_part / "form_key=F_VS").rglob("*.parquet"))
    ae_files = list((study_part / "form_key=F_AE").rglob("*.parquet"))
    assert dm_files and vs_files and ae_files

    dm_df = pd.concat([pd.read_parquet(f) for f in dm_files], ignore_index=True)
    vs_df = pd.concat([pd.read_parquet(f) for f in vs_files], ignore_index=True)
    ae_df = pd.concat([pd.read_parquet(f) for f in ae_files], ignore_index=True)

    assert set(dm_df["DOMAIN"]) == {"DM"}
    assert "DMSEQ" not in dm_df.columns
    assert "recordId" not in dm_df.columns and "record_id" not in dm_df.columns
    assert list(dm_df["BRTHDTC"]) == ["1981"]

    assert set(vs_df["DOMAIN"]) == {"VS"}
    assert list(vs_df["VSSEQ"]) == [1]

    assert set(ae_df["DOMAIN"]) == {"AE"}
    assert list(ae_df["AESEQ"]) == [1]


def test_multi_form_sdtm_routing_domain_filtering_and_empty_study_schemas(
    tmp_path: Path,
) -> None:
    """AC 3: Multi-Form routing stamps per-Record domains, filters sdtm_domain, and infers empty schemas."""
    sdk = ImednetSDK(simulation_mode=True)

    # 1. Multi-form study export without explicit sdtm_domain routes F_DM->DM (no DMSEQ) & F_VS->VS (VSSEQ)
    pq_all = tmp_path / "multi_sdtm.parquet"
    export_to_parquet(sdk, "STUDY", pq_all, as_sdtm=True)
    all_df = pd.read_parquet(pq_all)
    assert set(all_df["DOMAIN"]) == {"DM", "VS", "AE"}
    assert "DMSEQ" not in all_df.columns
    dm_rows = all_df[all_df["DOMAIN"] == "DM"]
    vs_rows = all_df[all_df["DOMAIN"] == "VS"]
    ae_rows = all_df[all_df["DOMAIN"] == "AE"]
    assert len(dm_rows) == 1 and dm_rows["VSSEQ"].isna().all() and dm_rows["AESEQ"].isna().all()
    assert len(vs_rows) == 1 and int(vs_rows["VSSEQ"].iloc[0]) == 1
    assert len(ae_rows) == 1 and int(ae_rows["AESEQ"].iloc[0]) == 1

    # 2. Domain filtering when sdtm_domain='VS' restricts export to VS forms only
    pq_vs = tmp_path / "vs_only.parquet"
    export_to_parquet(sdk, "STUDY", pq_vs, as_sdtm=True, sdtm_domain="VS")
    vs_only_df = pd.read_parquet(pq_vs)
    assert set(vs_only_df["DOMAIN"]) == {"VS"}
    assert len(vs_only_df) == 1
    assert "AESEQ" not in vs_only_df.columns
    assert "DMSEQ" not in vs_only_df.columns

    # 3. Empty study with F_DM and F_VS forms infers exact SDTM contract columns without AESEQ fallback
    empty_sdk = _build_stateful_sdtm_sdk(
        {},
        forms=[
            Form(
                form_id=301,
                form_key="F_DM",
                form_name="Demographics",
                form_type="Enrollment",
                date_created="2026-01-01T00:00:00Z",
                date_modified="2026-01-01T00:00:00Z",
            ),
            Form(
                form_id=302,
                form_key="F_VS",
                form_name="Vital Signs",
                form_type="Standard",
                date_created="2026-01-01T00:00:00Z",
                date_modified="2026-01-01T00:00:00Z",
            ),
        ],
    )
    empty_dm_pq = tmp_path / "empty_dm.parquet"
    export_to_parquet(empty_sdk, "EMPTY_STUDY", empty_dm_pq, as_sdtm=True, sdtm_domain="DM")
    empty_dm_df = pd.read_parquet(empty_dm_pq)
    assert empty_dm_df.empty
    assert {"STUDYID", "DOMAIN", "USUBJID"}.issubset(set(empty_dm_df.columns))
    assert "DMSEQ" not in empty_dm_df.columns
    assert "AESEQ" not in empty_dm_df.columns

    empty_multi_csv = tmp_path / "empty_multi.csv"
    export_to_csv(empty_sdk, "EMPTY_STUDY", empty_multi_csv, as_sdtm=True)
    empty_multi_df = pd.read_csv(empty_multi_csv)
    assert empty_multi_df.empty
    assert "VSSEQ" in empty_multi_df.columns
    assert "DMSEQ" not in empty_multi_df.columns
    assert "AESEQ" not in empty_multi_df.columns


def test_incremental_sdtm_sidecar_lineage_replay_and_void_reconciliation(
    tmp_path: Path,
) -> None:
    """AC 4: Incremental SDTM preserves *SEQ, never reuses voided *SEQ, and reconciles deletes."""
    from imednet_workflows.state_ledger import FileStateProvider

    study_key = "STUDY_SDTM_INC"
    upstream: dict[int, Record] = {
        1: Record(
            record_id=1,
            study_key=study_key,
            subject_key="S01",
            form_id=301,
            form_key="F_DM",
            record_status="Complete",
            deleted=False,
            date_modified=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            record_data={"BRTHDTC": "1981-05-14", "SEX": "F"},
        ),
        2: Record(
            record_id=2,
            study_key=study_key,
            subject_key="S02",
            form_id=301,
            form_key="F_DM",
            record_status="Complete",
            deleted=False,
            date_modified=datetime(2026, 6, 1, 10, 1, tzinfo=UTC),
            record_data={"BRTHDTC": "1975-09-20", "SEX": "M"},
        ),
        3: Record(
            record_id=3,
            study_key=study_key,
            subject_key="S01",
            form_id=303,
            form_key="F_AE",
            record_status="Complete",
            deleted=False,
            date_modified=datetime(2026, 6, 1, 10, 2, tzinfo=UTC),
            record_data={"AETERM": "Headache Initial"},
        ),
        4: Record(
            record_id=4,
            study_key=study_key,
            subject_key="S01",
            form_id=303,
            form_key="F_AE",
            record_status="Complete",
            deleted=False,
            date_modified=datetime(2026, 6, 1, 10, 3, tzinfo=UTC),
            record_data={"AETERM": "Nausea To Void"},
        ),
    }
    sdk = _build_stateful_sdtm_sdk(
        upstream,
        variables=[
            Variable(
                variable_id=1,
                variable_name="BRTHDTC",
                label="Birth Date",
                variable_type="text",
                form_id=301,
                form_key="F_DM",
            ),
            Variable(
                variable_id=2,
                variable_name="SEX",
                label="Sex",
                variable_type="text",
                form_id=301,
                form_key="F_DM",
            ),
            Variable(
                variable_id=7,
                variable_name="AETERM",
                label="AE Term",
                variable_type="text",
                form_id=303,
                form_key="F_AE",
            ),
        ],
    )
    ledger_path = tmp_path / "sdtm_ledger.json"

    csv_path = tmp_path / "inc.csv"
    pq_path = tmp_path / "inc.parquet"
    xl_path = tmp_path / "inc.xlsx"
    json_path = tmp_path / "inc.json"
    sql_path = tmp_path / "inc.sqlite"
    ddb_path = tmp_path / "inc.duckdb"
    hive_dir = tmp_path / "inc_hive"

    def _run_all() -> None:
        common = {
            "as_sdtm": True,
            "incremental": True,
            "ledger_path": str(ledger_path),
            "quality_gate_enabled": False,
        }
        export_to_csv(sdk, study_key, csv_path, stream_name="s_csv", **common)
        export_to_parquet(sdk, study_key, pq_path, stream_name="s_pq", **common)
        export_to_excel(sdk, study_key, xl_path, stream_name="s_xl", **common)
        export_to_json(sdk, study_key, json_path, stream_name="s_json", **common)
        export_to_sql(
            sdk, study_key, "sdtm_tbl", f"sqlite:///{sql_path}", stream_name="s_sql", **common
        )
        export_to_duckdb(sdk, study_key, ddb_path, "sdtm_tbl", stream_name="s_ddb", **common)
        export_to_hive_parquet(sdk, study_key, hive_dir, stream_name="s_hive", **common)

    # Run 1: initial full checkpointed export
    _run_all()
    provider = FileStateProvider(ledger_path)
    persisted_csv = provider.get_sdtm_lineage(study_key, "s_csv")
    lineage_csv = persisted_csv["sdtm_lineage"]
    assert lineage_csv["1"]["seq_col"] is None
    assert lineage_csv["3"]["seq_col"] == "AESEQ" and lineage_csv["3"]["seq_val"] == 1
    assert lineage_csv["4"]["seq_col"] == "AESEQ" and lineage_csv["4"]["seq_val"] == 2
    assert persisted_csv["sdtm_seq_watermarks"]["AE:S01"] == 2

    # Run 2: replay record 3 (updated), void record 2 (DM) & record 4 (AE#2), add record 5 (AE->AESEQ=3)
    upstream[3].record_data = {"AETERM": "Headache Updated"}
    upstream[3].date_modified = datetime(2026, 6, 2, 11, 0, tzinfo=UTC)
    upstream[2].deleted = True
    upstream[2].date_modified = datetime(2026, 6, 2, 11, 1, tzinfo=UTC)
    upstream[4].deleted = True
    upstream[4].date_modified = datetime(2026, 6, 2, 11, 2, tzinfo=UTC)
    upstream[5] = Record(
        record_id=5,
        study_key=study_key,
        subject_key="S01",
        form_id=303,
        form_key="F_AE",
        record_status="Complete",
        deleted=False,
        date_modified=datetime(2026, 6, 2, 11, 3, tzinfo=UTC),
        record_data={"AETERM": "Dizziness New"},
    )
    _run_all()

    def _verify_reconciled(rows: list[dict[str, Any]], label: str) -> None:
        assert len(rows) == 3, f"{label}: expected 3 active rows, got {rows!r}"
        for r in rows:
            assert "recordId" not in r and "record_id" not in r, f"{label} leaked recordId: {r}"
        dm_rows = [r for r in rows if r["DOMAIN"] == "DM"]
        ae_rows = sorted(
            [r for r in rows if r["DOMAIN"] == "AE"], key=lambda r: int(float(r["AESEQ"]))
        )
        assert len(dm_rows) == 1, f"{label}: voided DM S02 not deleted: {dm_rows}"
        assert dm_rows[0]["USUBJID"] == "S01"
        assert dm_rows[0]["BRTHDTC"] == "1981"

        assert [int(float(r["AESEQ"])) for r in ae_rows] == [1, 3], (
            f"{label}: expected monotonic AESEQ [1, 3] (never reusing voided 2), got {ae_rows}"
        )
        assert [r["AETERM"] for r in ae_rows] == ["Headache Updated", "Dizziness New"]

    _verify_reconciled(pd.read_csv(csv_path, dtype=str).to_dict(orient="records"), "csv")
    _verify_reconciled(pd.read_parquet(pq_path).to_dict(orient="records"), "parquet")
    _verify_reconciled(pd.read_excel(xl_path, dtype=str).to_dict(orient="records"), "excel")
    _verify_reconciled(json.loads(json_path.read_text(encoding="utf-8")), "json")
    with sqlite3.connect(sql_path) as sconn:
        _verify_reconciled(
            pd.read_sql_query('SELECT * FROM "sdtm_tbl"', sconn).to_dict(orient="records"), "sql"
        )
    with duckdb.connect(str(ddb_path), read_only=True) as dconn:
        _verify_reconciled(
            dconn.execute('SELECT * FROM "sdtm_tbl"').df().to_dict(orient="records"), "duckdb"
        )
    hive_rows = pd.concat(
        [pd.read_parquet(f) for f in sorted(hive_dir.rglob("*.parquet"))], ignore_index=True
    ).to_dict(orient="records")
    _verify_reconciled(hive_rows, "hive_parquet")


@pytest.mark.parametrize("profile_name", ["general", "drug", "biologic", "vaccine", "device"])
def test_cli_validate_standards_simulation_succeeds_for_all_five_profiles(
    profile_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 5: validate-standards STUDY --profile <name> --simulation exits 0 for all 5 profiles."""
    cli_mod.app(
        [
            "workflows",
            "validate-standards",
            "STUDY",
            "--profile",
            profile_name,
            "--simulation",
            "--format",
            "json",
        ]
    )
    out, err = capsys.readouterr()
    assert err == ""
    envelope = json.loads(out)
    assert envelope["schemaVersion"] == "1.0"
    assert envelope["resource"] == "standards_readiness"
    item = envelope["items"][0]
    assert item["profile"] == profile_name
    assert item["passed"] is True
    assert item["is_ready"] is True
    assert item["score"] >= 85.0


def test_documented_cli_sdtm_export_command_succeeds_in_simulation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 6: Running the documented docs/how-to/cli.rst SDTM export command with --simulation exits 0."""
    monkeypatch.chdir(tmp_path)
    cli_mod.app(
        [
            "export",
            "csv",
            "STUDY",
            "./ae_sdtm.csv",
            "--sdtm-domain",
            "AE",
            "--standards-profile",
            "drug",
            "--min-standards-score",
            "85",
            "--simulation",
        ]
    )
    capsys.readouterr()
    out_file = tmp_path / "ae_sdtm.csv"
    assert out_file.exists()
    df = pd.read_csv(out_file)
    assert len(df) == 1
    assert list(df["DOMAIN"]) == ["AE"]
    assert list(df["AESEQ"]) == [1]
    assert "recordId" not in df.columns and "record_id" not in df.columns


def test_core_standards_profiles_domain_dict_and_normalizer_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover StandardsProfile registry, _DomainDict resolution, and CategoricalNormalizer branches in core."""
    from imednet.models.standards import (
        PROFILE_REGISTRY,
        BiologicSafetyProfile,
        CategoricalNormalizer,
        DrugSafetyProfile,
        StandardsReadinessValidator,
        VaccineSafetyProfile,
        _coerce_float_or_default,
        _DomainDict,
    )

    assert PROFILE_REGISTRY.list_profiles() == [
        "biologic",
        "device",
        "drug",
        "general",
        "vaccine",
    ]
    assert isinstance(PROFILE_REGISTRY.get("biologic"), BiologicSafetyProfile)
    assert isinstance(PROFILE_REGISTRY.get("vaccine"), VaccineSafetyProfile)

    dk = _DomainDict({"AE": ["aeTerm"], "AE.aeTerm": {"required": True}})
    assert "Adverse Events" in dk
    assert "Adverse Events.aeTerm" in dk
    assert "UNKNOWN" not in dk
    assert "UNKNOWN.field" not in dk
    assert dk["Adverse Events"] == ["aeTerm"]
    assert dk["Adverse Events.aeTerm"] == {"required": True}
    assert dk.get("Adverse Events") == ["aeTerm"]
    assert dk.get("MISSING", "fallback") == "fallback"

    validator = StandardsReadinessValidator(profile=DrugSafetyProfile())
    empty_report = validator.score_records(records_by_domain={})
    assert empty_report.score == 0.0
    assert empty_report.is_ready is False

    normalizer = CategoricalNormalizer(
        cdisc_ct={"AEOUT": {"RECOVERED/RESOLVED"}},
        date_fields={"AESTDTC"},
    )
    norm_res = normalizer.normalize_record(
        {
            "subject_key": "  SUBJ-9  ",
            "USUBJID": "",
            "SEX": "unknown_code",
            "AEOUT": "recovered/resolved",
            "AESTDTC": "2026-05-01",
            "AESER": "true",
        },
        study_key="STUDY9",
        terminology_lookups={"SEX": {"F": "F", "M": "M"}},
    )
    assert norm_res.normalized_record["subject_key"] == "SUBJ-9"
    assert norm_res.normalized_record["USUBJID"] == "STUDY9-SUBJ-9"
    assert norm_res.normalized_record["AEOUT"] == "RECOVERED/RESOLVED"
    assert norm_res.normalized_record["AESER"] is True
    assert any("unmapped" in w for w in norm_res.warnings)

    outside_res = normalizer.normalize_record(
        {"subjectKey": "S1", "AEOUT": "UNEXPECTED"},
        study_key="STUDY9",
    )
    assert any("outside the configured terminology" in w for w in outside_res.warnings)
    assert normalizer._normalize_lookups({"sex": {"f": "F"}}) == {"sex": {"F": "F"}}

    assert _coerce_float_or_default(None) == 0.0
    assert _coerce_float_or_default("not-a-float") == 0.0
    monkeypatch.setattr("imednet.models.standards.is_strict_mode", lambda: True)
    with pytest.raises(ValueError):
        _coerce_float_or_default("not-a-float")


def test_sink_base_sdtm_lineage_quality_gate_and_filter_branch_helpers(tmp_path: Path) -> None:
    """Cover sink_base SDTM composite keys, lineage rehydration, quality gate, and filter branch helpers."""
    from types import SimpleNamespace

    from imednet.integrations.sink_base import (
        SinkConfig,
        _combine_branch_with_watermark,
        _expand_scalar_list_branches,
        _extract_record_for_standards,
        _resolve_record_query_branches,
        _split_top_level,
        _validate_record_data_filter_input,
        apply_quality_gate,
        commit_sdtm_lineage,
        deduplicate_sdtm_rows,
        evaluate_standards_readiness,
        extract_sdtm_composite_key,
        rehydrate_sdtm_lineage,
        resolve_voided_sdtm_keys,
        run_streaming_export,
    )

    # _extract_record_for_standards across dict (recordData/data/flat), Record, and __dict__ object
    dom1, raw1, can1 = _extract_record_for_standards(
        {"formId": 301, "subjectKey": "S1", "recordData": {"brthdtc": "1980-01-01", "ethnic": "H"}},
        form_domain_map={301: "DM"},
    )
    assert dom1 == "DM" and can1["birthDate"] == "1980-01-01" and can1["ethnicity"] == "H"
    assert raw1["brthdtc"] == "1980-01-01"

    obj_rec = SimpleNamespace(
        form_key="F_AE",
        form_id=303,
        subject_key="S2",
        record_id=91,
        record_data={"AETERM": "Fever", "bad-var!": "x"},
    )
    dom2, _, can2 = _extract_record_for_standards(obj_rec, form_domain_map={"F_AE": "AE"})
    assert dom2 == "AE" and can2["subjectKey"] == "S2"

    cfg_extra = SinkConfig(
        study_key="ST",
        standards_profile="drug",
        extra={"terminology_lookups": {"aeSeverity": {"MILD": "MILD"}}},
    )
    readiness = evaluate_standards_readiness([obj_rec], study_key="ST", config=cfg_extra)
    assert readiness.warnings  # includes CDASH warning for bad-var!

    # apply_quality_gate: disabled schema gate with standards_profile logs CDASH and yields;
    # enabled schema gate retains schema-valid records when min_standards_score is unset
    # (decoupled from min_schema_readiness_score) and drops schema-invalid records.
    cfg_cdash_only = SinkConfig(
        study_key="ST", quality_gate_enabled=False, standards_profile="drug"
    )
    assert len(list(apply_quality_gate(MagicMock(), "ST", [obj_rec], cfg_cdash_only))) == 1

    cfg_schema_only = SinkConfig(
        study_key="ST",
        quality_gate_enabled=True,
        min_schema_readiness_score=95.0,
        standards_profile="drug",
    )
    vars_ae = [
        Variable(
            variable_id=1,
            variable_name="AETERM",
            variable_type="text",
            form_id=303,
            form_key="F_AE",
        )
    ]
    retained = list(
        apply_quality_gate(MagicMock(), "ST", [obj_rec], cfg_schema_only, variables=vars_ae)
    )
    assert len(retained) == 1

    vars_schema_fail = [
        *vars_ae,
        Variable(
            variable_id=2,
            variable_name="AESEV_NUM",
            variable_type="integer",
            form_id=303,
            form_key="F_AE",
        ),
    ]
    bad_schema_rec = SimpleNamespace(
        form_key="F_AE",
        form_id=303,
        subject_key="S2",
        record_id=92,
        record_data={"AETERM": "Fever", "AESEV_NUM": "not_an_int"},
    )
    dropped = list(
        apply_quality_gate(
            MagicMock(), "ST", [bad_schema_rec], cfg_schema_only, variables=vars_schema_fail
        )
    )
    assert dropped == []

    # extract_sdtm_composite_key & deduplicate_sdtm_rows edge cases
    assert extract_sdtm_composite_key({"STUDYID": "S"}) is None
    assert extract_sdtm_composite_key({"STUDYID": "S", "DOMAIN": "  ", "USUBJID": "U"}) is None
    assert extract_sdtm_composite_key(
        {"STUDYID": "S", "DOMAIN": "FA", "USUBJID": "U", "VSSEQ": 2.0}
    ) == ("S", "FA", "U", "VSSEQ", 2)
    assert extract_sdtm_composite_key(
        {"STUDYID": "S", "DOMAIN": "AE", "USUBJID": "U", "AESEQ": True}
    ) == ("S", "AE", "U", "AESEQ", None)
    assert extract_sdtm_composite_key(
        {"STUDYID": "S", "DOMAIN": "AE", "USUBJID": "U", "AESEQ": float("nan")}
    ) == ("S", "AE", "U", "AESEQ", None)
    assert extract_sdtm_composite_key(
        {"STUDYID": "S", "DOMAIN": "AE", "USUBJID": "U", "AESEQ": "bad"}
    ) == ("S", "AE", "U", "AESEQ", None)
    assert len(deduplicate_sdtm_rows([{"STUDYID": "S"}])) == 1
    dedup_rows = deduplicate_sdtm_rows(
        [
            {"unkeyed": 1},
            {"STUDYID": "S", "DOMAIN": "AE", "USUBJID": "U", "AESEQ": 1, "AETERM": "v1"},
            {"STUDYID": "S", "DOMAIN": "AE", "USUBJID": "U", "AESEQ": 1, "AETERM": "v2"},
        ]
    )
    assert len(dedup_rows) == 2 and dedup_rows[1]["AETERM"] == "v2"

    # Lineage rehydration from read_state() fallback, active_tx, and sink memory + voided key fallback
    legacy_provider = SimpleNamespace(
        read_state=lambda: SimpleNamespace(
            studies={
                "ST": SimpleNamespace(
                    streams={
                        "records": SimpleNamespace(
                            metadata={
                                "sdtm_lineage": {
                                    "10": {
                                        "study_id": "ST",
                                        "domain": "AE",
                                        "usubjid": "U1",
                                        "seq_col": "AESEQ",
                                        "seq_val": 2,
                                    }
                                },
                                "sdtm_seq_watermarks": {"AE:U1": 4},
                            }
                        )
                    }
                )
            }
        )
    )
    fake_sink = SimpleNamespace(
        config=SinkConfig(study_key="ST", state_provider=legacy_provider),
        _active_tx={"metadata": {"sdtm_seq_watermarks": {"AE:U1": 5}}},
        write_batch=lambda *_a, **_k: 0,
    )
    commit_sdtm_lineage(
        fake_sink,
        {
            "11": {
                "study_id": "ST",
                "domain": "AE",
                "usubjid": "U1",
                "seq_col": "AESEQ",
                "seq_val": 6,
            }
        },
    )
    counters, overrides, full_lin = rehydrate_sdtm_lineage(fake_sink, study_key="ST")
    assert counters["AE"]["U1"] == 6
    assert overrides["10"] == 2 and overrides["11"] == 6
    assert "11" in full_lin

    void_keys = resolve_voided_sdtm_keys(
        [
            {"STUDYID": "ST", "DOMAIN": "AE", "USUBJID": "U1", "AESEQ": 6},
            {"formKey": "F_DM", "subjectKey": "U2"},
            SimpleNamespace(form_key="F_DM", subject_key="U3", study_key="ST"),
        ],
        fake_sink,
        study_key="ST",
        form_domain_map={"F_DM": "DM"},
    )
    assert len(void_keys) == 3

    # Filter / watermark query branch combinators
    with pytest.raises(ValueError, match="cannot be empty"):
        _validate_record_data_filter_input("   ")
    assert _split_top_level('a=="x,y",b=="z\\"w"', ",") == ['a=="x,y"', 'b=="z\\"w"']
    with pytest.raises(ValueError, match="empty clause"):
        _split_top_level("a==1,", ",")

    t1 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 6, 2, 0, 0, tzinfo=UTC)
    t3 = datetime(2026, 6, 3, 0, 0, tzinfo=UTC)
    assert _combine_branch_with_watermark({"dateModified": [("==", t1), ("==", t2)]}, None) is None
    assert _combine_branch_with_watermark({"dateModified": ("<", t1)}, t2) is None
    combined_branch = _combine_branch_with_watermark(
        {"dateModified": [("!=", t2), ("<=", t3)]},
        t1,
    )
    assert combined_branch is not None
    _, pred = combined_branch
    assert pred is not None
    assert pred({"dateModified": "2026-06-01T12:00:00Z"}) is True
    assert pred({"dateModified": "2026-06-02T00:00:00Z"}) is False
    assert pred({"dateModified": "2026-06-04T00:00:00Z"}) is False
    assert pred({}) is False

    assert _expand_scalar_list_branches({"formKey": set()}) == []
    assert len(_expand_scalar_list_branches({"formKey": {"F_DM", "F_AE"}})) == 2
    branches = _resolve_record_query_branches(
        record_filter={"formKey": ["F_DM", "F_AE"], "filter": "subjectKey==S01"},
        watermark=t1,
    )
    assert len(branches) == 2
    seq_branches = _resolve_record_query_branches(
        record_filter=["formKey==F_AE;subjectKey==S01"],
        watermark=None,
    )
    assert len(seq_branches) == 1

    # run_streaming_export domain form filtering when records_source is None
    sdk_dom = _build_stateful_sdtm_sdk(
        {
            1: Record(
                record_id=1,
                study_key="ST",
                subject_key="S01",
                form_id=301,
                form_key="F_DM",
                record_data={"SEX": "F"},
            ),
            2: Record(
                record_id=2,
                study_key="ST",
                subject_key="S01",
                form_id=303,
                form_key="F_AE",
                record_data={"AETERM": "Headache"},
            ),
        },
        variables=[],
    )
    collected_batches: list[Any] = []

    class _MemSink:
        def __enter__(self) -> _MemSink:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def write_batch(self, records: Any, *, batch_id: str) -> int:
            _ = batch_id
            collected_batches.extend(records)
            return len(records)

        def flush(self) -> None:
            return None

    assert (
        run_streaming_export(
            sdk_dom,
            "ST",
            _MemSink(),
            SinkConfig(study_key="ST", as_sdtm=True, sdtm_domain="LB", quality_gate_enabled=False),
        )
        == 0
    )
    assert (
        run_streaming_export(
            sdk_dom,
            "ST",
            _MemSink(),
            SinkConfig(study_key="ST", as_sdtm=True, sdtm_domain="AE", quality_gate_enabled=False),
        )
        == 1
    )
    assert len(collected_batches) == 1 and collected_batches[0].form_key == "F_AE"


def test_parquet_sql_and_json_sdtm_legacy_pruning_and_schema_evolution(tmp_path: Path) -> None:
    """Cover Hive Parquet legacy SDTM pruning, SQL SDTM schema evolution/delete, and JSON SDTM merges."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from sqlalchemy import create_engine

    from imednet.integrations.export import (
        TabularSinkConfig,
        TabularSQLSink,
        _merge_sdtm_json_records,
        _remask_historical_item_if_needed,
    )
    from imednet.integrations.parquet import (
        PyArrowDatasetPartitionedStorageEngine,
        _prune_hive_partition_sdtm_keys,
    )

    # 1. _prune_hive_partition_sdtm_keys: partial rewrite & full partition cleanup
    hive_base = tmp_path / "hive_sdtm_prune"
    part_dir = hive_base / "study_key=ST" / "form_key=F_AE"
    sub_dir = part_dir / "batch_sub"
    sub_dir.mkdir(parents=True)
    pf_path = sub_dir / "part0.parquet"
    pq.write_table(
        pa.Table.from_pandas(
            pd.DataFrame(
                [
                    {
                        "STUDYID": "ST",
                        "DOMAIN": "AE",
                        "USUBJID": "S01",
                        "AESEQ": 1,
                        "AETERM": "Keep",
                        "study_key": "ST",
                        "form_key": "F_AE",
                    },
                    {
                        "STUDYID": "ST",
                        "DOMAIN": "AE",
                        "USUBJID": "S01",
                        "AESEQ": 2,
                        "AETERM": "Drop",
                        "study_key": "ST",
                        "form_key": "F_AE",
                    },
                ]
            ),
            preserve_index=False,
        ),
        pf_path,
    )
    engine = PyArrowDatasetPartitionedStorageEngine()
    _prune_hive_partition_sdtm_keys(
        [pf_path],
        {("ST", "AE", "S01", "AESEQ", 2)},
        pyarrow_module=pa,
        storage_engine=engine,
        base_dir=str(hive_base),
        study_key="ST",
        form_key="F_AE",
    )
    remaining_files = list(part_dir.rglob("*.parquet"))
    assert len(remaining_files) == 1
    rem_df = pd.read_parquet(remaining_files[0])
    assert len(rem_df) == 1 and rem_df.iloc[0]["AETERM"] == "Keep"

    _prune_hive_partition_sdtm_keys(
        remaining_files,
        {("ST", "AE", "S01", "AESEQ", 1)},
        pyarrow_module=pa,
        storage_engine=engine,
        base_dir=str(hive_base),
        study_key="ST",
        form_key="F_AE",
    )
    assert not part_dir.exists()

    # 2. TabularSQLSink: incremental SDTM ALTER TABLE schema evolution + _delete_existing_sdtm_keys
    sql_engine = create_engine(f"sqlite:///{tmp_path / 'evolve.sqlite'}")
    sql_cfg = TabularSinkConfig(
        study_key="ST",
        as_sdtm=True,
        idempotent=True,
        modified_since=datetime(2026, 6, 1, tzinfo=UTC),
        pandas_kwargs={"if_exists": "append"},
    )
    with TabularSQLSink("sdtm_evolve", sql_engine, sql_cfg) as sink:
        sink.write_batch(
            [{"STUDYID": "ST", "DOMAIN": "DM", "USUBJID": "S01", "SEX": "F"}],
            batch_id="b1",
        )
        sink.write_batch(
            [{"STUDYID": "ST", "DOMAIN": "AE", "USUBJID": "S01", "AESEQ": 1, "AETERM": "Headache"}],
            batch_id="b2",
        )
        deleted = sink._delete_existing_sdtm_keys(
            [
                ("ST", "DM", "S01", None, None),
                ("ST", "AE", "S01", "AESEQ", 1),
            ]
        )
        assert deleted == 2
    with sql_engine.connect() as conn:
        final_sql_df = pd.read_sql_table("sdtm_evolve", conn)
    assert final_sql_df.empty
    assert {"AESEQ", "AETERM"}.issubset(set(final_sql_df.columns))

    # 3. _remask_historical_item_if_needed (list/tuple/dict) & _merge_sdtm_json_records
    nested_tuple = (
        [{"BRTHDTC": "1981", "patientName": "Alice"}],
        {"BRTHDTC": "1981-05-14", "brthdtc": "not-a-date"},
    )
    remasked = _remask_historical_item_if_needed(nested_tuple, {"patientname"}, {}, as_sdtm=True)
    assert remasked[0][0]["BRTHDTC"] == "1981"
    assert remasked[0][0]["patientName"] == "***MASKED***"
    assert remasked[1]["BRTHDTC"] == "1981"
    assert remasked[1]["brthdtc"] is None

    merged_json = _merge_sdtm_json_records(
        existing_records=[
            {"STUDYID": "ST", "DOMAIN": "DM", "USUBJID": "S01", "BRTHDTC": "1981", "SEX": "U"},
            {"STUDYID": "ST", "DOMAIN": "AE", "USUBJID": "S01", "AESEQ": 1, "AETERM": "VoidMe"},
            {
                "STUDYID": "ST",
                "DOMAIN": "VS",
                "USUBJID": "S01",
                "VSSEQ": 1,
                "patientName": "Secret",
            },
        ],
        delta_records=[
            {"STUDYID": "ST", "DOMAIN": "DM", "USUBJID": "S01", "BRTHDTC": "1981", "SEX": "F"},
            {"STUDYID": "ST", "DOMAIN": "AE", "USUBJID": "S01", "AESEQ": 2, "AETERM": "NewAE"},
            {"note": "unkeyed"},
        ],
        voided_keys={("ST", "AE", "S01", "AESEQ", 1)},
        mask_historical_phi=True,
        phi_keys={"patientname"},
    )
    assert len(merged_json) == 4
    dm_merged = next(r for r in merged_json if r.get("DOMAIN") == "DM")
    vs_merged = next(r for r in merged_json if r.get("DOMAIN") == "VS")
    assert dm_merged["SEX"] == "F" and dm_merged["BRTHDTC"] == "1981"
    assert vs_merged["patientName"] == "***MASKED***"


def test_mixed_form_sdtm_domain_ae_across_csv_hive_duckdb_neo4j_and_record_mapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1: Mixed-Form Study with sdtm_domain='AE' emits only AE Records across CSV, Hive Parquet, DuckDB, Neo4j, and RecordMapper."""
    import imednet_sinks.graph as graph_mod
    from imednet.models.study_config import MappingRule
    from imednet_sinks.graph import Neo4jSinkConfig, export_to_neo4j
    from imednet_workflows.record_mapper import RecordMapper

    study_key = "S_AC1_MIXED_AE"
    study_cfg = StudyConfiguration(
        studyKey=study_key,
        version="1.0.0",
        reportingProfile="drug",
        mappings=[
            MappingRule(
                sourceFormKey="F_SAFETY_CUSTOM",
                sourceVariableName="CUSTOM_TERM",
                domain="AE",
                targetField="AETERM",
            )
        ],
    )
    monkeypatch.setattr(
        enrichment_mod,
        "resolve_study_configuration",
        lambda sk: study_cfg if sk == study_key else None,
    )

    forms = [
        Form(form_id=301, form_key="F_DM", form_name="Demographics", form_type="Enrollment"),
        Form(form_id=302, form_key="F_VS", form_name="Vital Signs", form_type="Standard"),
        Form(form_id=303, form_key="F_AE", form_name="Adverse Events", form_type="Standard"),
        Form(
            form_id=304,
            form_key="F_SAFETY_CUSTOM",
            form_name="Custom Safety Log",
            form_type="Standard",
        ),
    ]
    variables = [
        Variable(
            variable_id=1, variable_name="SEX", variable_type="text", form_id=301, form_key="F_DM"
        ),
        Variable(
            variable_id=2,
            variable_name="VSTESTCD",
            variable_type="text",
            form_id=302,
            form_key="F_VS",
        ),
        Variable(
            variable_id=3,
            variable_name="AETERM",
            variable_type="text",
            form_id=303,
            form_key="F_AE",
        ),
        Variable(
            variable_id=4,
            variable_name="CUSTOM_TERM",
            variable_type="text",
            form_id=304,
            form_key="F_SAFETY_CUSTOM",
        ),
    ]
    upstream = {
        1: Record(
            record_id=1,
            study_key=study_key,
            subject_key="S01",
            form_id=301,
            form_key="F_DM",
            record_data={"SEX": "F"},
        ),
        2: Record(
            record_id=2,
            study_key=study_key,
            subject_key="S01",
            form_id=302,
            form_key="F_VS",
            record_data={"VSTESTCD": "HR"},
        ),
        3: Record(
            record_id=3,
            study_key=study_key,
            subject_key="S01",
            form_id=303,
            form_key="F_AE",
            record_data={"AETERM": "Headache"},
        ),
        4: Record(
            record_id=4,
            study_key=study_key,
            subject_key="S01",
            form_id=304,
            form_key="F_SAFETY_CUSTOM",
            record_data={"CUSTOM_TERM": "Rash"},
        ),
    }
    sdk = _build_stateful_sdtm_sdk(upstream, forms=forms, variables=variables)

    # Inject a leaked non-AE record even when upstream receives formId, proving local domain filtering
    orig_side_effect = sdk.records.list.side_effect
    observed_form_ids: list[Any] = []

    def _leaky_list_records(**kw: Any) -> Any:
        if "formId" in kw and kw["formId"] is not None:
            observed_form_ids.append(kw["formId"])
        matched = list(orig_side_effect(**kw))
        return iter([upstream[1].model_copy(deep=True), *matched])

    sdk.records.list.side_effect = _leaky_list_records

    csv_path = tmp_path / "ae_only.csv"
    hive_dir = tmp_path / "ae_hive"
    ddb_path = tmp_path / "ae_only.duckdb"

    export_to_csv(sdk, study_key, csv_path, sdtm_domain="AE", quality_gate_enabled=False)
    export_to_hive_parquet(sdk, study_key, hive_dir, sdtm_domain="AE", quality_gate_enabled=False)
    export_to_duckdb(
        sdk, study_key, ddb_path, "ae_tbl", sdtm_domain="AE", quality_gate_enabled=False
    )

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
    export_to_neo4j(
        sdk,
        study_key,
        config=Neo4jSinkConfig(
            study_key=study_key,
            uri="bolt://localhost:7687",
            auth=("neo4j", "secret"),
            as_sdtm=True,
            sdtm_domain="AE",
            quality_gate_enabled=False,
        ),
    )

    mapper_df = RecordMapper(sdk).sdtm_dataframe(study_key, domain="AE")

    assert observed_form_ids, "Expected upstream formId filtering on Records API calls"
    csv_df = pd.read_csv(csv_path)
    assert set(csv_df["DOMAIN"]) == {"AE"}
    assert len(csv_df) == 2

    hive_df = pd.concat(
        [pd.read_parquet(f) for f in sorted(hive_dir.rglob("*.parquet"))],
        ignore_index=True,
    )
    assert set(hive_df["DOMAIN"]) == {"AE"}
    assert len(hive_df) == 2

    with duckdb.connect(str(ddb_path), read_only=True) as dconn:
        ddb_df = dconn.execute('SELECT * FROM "ae_tbl"').df()
    assert set(ddb_df["DOMAIN"]) == {"AE"}
    assert len(ddb_df) == 2

    neo_rows = [row for q, p in neo4j_calls if "MERGE (r:Record" in q for row in p.get("rows", ())]
    assert len(neo_rows) == 2
    assert {r["DOMAIN"] for r in neo_rows} == {"AE"}

    assert set(mapper_df["DOMAIN"]) == {"AE"}
    assert len(mapper_df) == 2


def test_cli_export_ae_with_drug_profile_and_validate_standards_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 2 & AC 3: CLI export csv --sdtm-domain AE --standards-profile drug --min-standards-score 85 exits 0; validate-standards exits 1 when unready."""
    study_key = "STUDY"
    valid_sdk = _build_stateful_sdtm_sdk(
        {
            1: Record(
                record_id=1,
                study_key=study_key,
                subject_key="S01",
                form_id=301,
                form_key="F_DM",
                record_data={"SEX": "F", "BRTHDTC": "1980-04-12"},
            ),
            2: Record(
                record_id=2,
                study_key=study_key,
                subject_key="S01",
                form_id=303,
                form_key="F_AE",
                record_data={
                    "AETERM": "Headache",
                    "AEDECOD": "Headache",
                    "AESEV": "MODERATE",
                    "AEREL": "RELATED",
                    "AEACN": "DOSE NOT CHANGED",
                    "AEOUT": "RECOVERED/RESOLVED",
                    "AESER": "N",
                    "AESTDTC": "2026-03-10",
                },
            ),
        },
        variables=[
            Variable(
                variable_id=1,
                variable_name="SEX",
                variable_type="text",
                form_id=301,
                form_key="F_DM",
            ),
            Variable(
                variable_id=2,
                variable_name="BRTHDTC",
                variable_type="text",
                form_id=301,
                form_key="F_DM",
            ),
            Variable(
                variable_id=3,
                variable_name="AETERM",
                variable_type="text",
                form_id=303,
                form_key="F_AE",
            ),
            Variable(
                variable_id=4,
                variable_name="AEDECOD",
                variable_type="text",
                form_id=303,
                form_key="F_AE",
            ),
            Variable(
                variable_id=5,
                variable_name="AESEV",
                variable_type="text",
                form_id=303,
                form_key="F_AE",
            ),
            Variable(
                variable_id=6,
                variable_name="AEREL",
                variable_type="text",
                form_id=303,
                form_key="F_AE",
            ),
            Variable(
                variable_id=7,
                variable_name="AEACN",
                variable_type="text",
                form_id=303,
                form_key="F_AE",
            ),
            Variable(
                variable_id=8,
                variable_name="AEOUT",
                variable_type="text",
                form_id=303,
                form_key="F_AE",
            ),
            Variable(
                variable_id=9,
                variable_name="AESER",
                variable_type="text",
                form_id=303,
                form_key="F_AE",
            ),
            Variable(
                variable_id=10,
                variable_name="AESTDTC",
                variable_type="text",
                form_id=303,
                form_key="F_AE",
            ),
        ],
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: valid_sdk)
    monkeypatch.chdir(tmp_path)

    cli_mod.app(
        [
            "export",
            "csv",
            study_key,
            "./ae_sdtm.csv",
            "--sdtm-domain",
            "AE",
            "--standards-profile",
            "drug",
            "--min-standards-score",
            "85",
        ]
    )
    capsys.readouterr()
    exported_df = pd.read_csv(tmp_path / "ae_sdtm.csv")
    assert list(exported_df["DOMAIN"]) == ["AE"]
    assert list(exported_df["AESEV"]) == ["MODERATE"]

    # Ready biologic & vaccine validation succeeds without KeyError
    for profile_name in ("biologic", "vaccine"):
        cli_mod.app(
            [
                "workflows",
                "validate-standards",
                study_key,
                "--profile",
                profile_name,
                "--domain",
                "AE",
                "--format",
                "json",
            ]
        )
        out, _ = capsys.readouterr()
        payload = json.loads(out)["items"][0]
        assert payload["profile"] == profile_name
        assert payload["passed"] is True

    # Unready Study exits 1 without --min-score
    unready_sdk = _build_stateful_sdtm_sdk(
        {
            10: Record(
                record_id=10,
                study_key=study_key,
                subject_key="S01",
                form_id=303,
                form_key="F_AE",
                record_data={},
            )
        }
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: unready_sdk)
    with pytest.raises(SystemExit) as exc_info:
        cli_mod.app(["workflows", "validate-standards", study_key, "--profile", "drug"])
    assert exc_info.value.code == 1
    capsys.readouterr()


def test_drug_safety_profile_aesev_and_empty_cm_lb_ex_mh_and_unconfigured_domain() -> None:
    """AC 4: DrugSafetyProfile accepts MILD/MODERATE/SEVERE; empty CM/LB/EX/MH do not score 100.0; unconfigured domain returns 0.0 with warning."""
    from imednet.models.standards import PROFILE_REGISTRY, StandardsReadinessValidator

    drug_validator = StandardsReadinessValidator(profile=PROFILE_REGISTRY.get("drug"))
    for severity in ("MILD", "MODERATE", "SEVERE"):
        res = drug_validator.score_records(
            records_by_domain={
                "AE": [
                    {
                        "subjectKey": "S01",
                        "aeTerm": "Nausea",
                        "aeDecod": "Nausea",
                        "aeSeverity": severity,
                        "aeRelationship": "RELATED",
                        "aeActionTaken": "DOSE NOT CHANGED",
                        "aeOutcome": "RECOVERED/RESOLVED",
                    }
                ]
            }
        )
        assert res.score == 100.0
        assert res.is_ready is True

    for profile_name in ("drug", "biologic", "vaccine"):
        validator = StandardsReadinessValidator(profile=PROFILE_REGISTRY.get(profile_name))
        for domain in ("CM", "LB", "EX", "MH"):
            empty_record_res = validator.score_records(records_by_domain={domain: [{}]})
            assert empty_record_res.score < 100.0
            assert empty_record_res.is_ready is False

    unconfigured_res = drug_validator.score_records(
        records_by_domain={"ZZ": [{"subjectKey": "S01", "FOO": "BAR"}]}
    )
    assert unconfigured_res.score == 0.0
    assert unconfigured_res.is_ready is False
    assert unconfigured_res.warnings


def test_evaluate_standards_readiness_study_config_and_decoupled_quality_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 5: evaluate_standards_readiness applies committed StudyConfiguration; apply_quality_gate retains schema-valid Records when min_standards_score is unset."""
    from imednet.errors import ExportError
    from imednet.integrations.sink_base import (
        SinkConfig,
        apply_quality_gate,
        evaluate_standards_readiness,
    )
    from imednet.models.study_config import MappingRule

    study_key = "S_AC5_CFG"
    study_cfg = StudyConfiguration(
        studyKey=study_key,
        version="1.0.0",
        reportingProfile="drug",
        terminologyLookups={"aeSeverity": {"2": "MODERATE"}},
        mappings=[
            MappingRule(
                sourceFormKey="F_CUSTOM_SAFETY",
                sourceVariableName="RAW_EVENT",
                domain="AE",
                targetField="aeTerm",
            ),
            MappingRule(
                sourceFormKey="F_CUSTOM_SAFETY",
                sourceVariableName="RAW_DECOD",
                domain="AE",
                targetField="aeDecod",
            ),
            MappingRule(
                sourceFormKey="F_CUSTOM_SAFETY",
                sourceVariableName="RAW_SEV",
                domain="AE",
                targetField="aeSeverity",
            ),
            MappingRule(
                sourceFormKey="F_CUSTOM_SAFETY",
                sourceVariableName="RAW_REL",
                domain="AE",
                targetField="aeRelationship",
            ),
            MappingRule(
                sourceFormKey="F_CUSTOM_SAFETY",
                sourceVariableName="RAW_ACTION",
                domain="AE",
                targetField="aeActionTaken",
            ),
            MappingRule(
                sourceFormKey="F_CUSTOM_SAFETY",
                sourceVariableName="RAW_OUT",
                domain="AE",
                targetField="aeOutcome",
            ),
        ],
    )
    monkeypatch.setattr(
        enrichment_mod,
        "resolve_study_configuration",
        lambda sk: study_cfg if sk == study_key else None,
    )

    mapped_rec = Record(
        record_id=1,
        study_key=study_key,
        subject_key="S01",
        form_id=410,
        form_key="F_CUSTOM_SAFETY",
        record_data={
            "RAW_EVENT": "Headache",
            "RAW_DECOD": "Headache",
            "RAW_SEV": "2",
            "RAW_REL": "RELATED",
            "RAW_ACTION": "NONE",
            "RAW_OUT": "RECOVERED/RESOLVED",
        },
    )
    report = evaluate_standards_readiness(
        [mapped_rec],
        study_key=study_key,
        config=SinkConfig(study_key=study_key, standards_profile="drug"),
    )
    assert report.score == 100.0
    assert report.is_ready is True

    # Schema-valid AE Record missing required drug-profile AE fields is retained when min_standards_score is unset
    schema_valid_rec = Record(
        record_id=2,
        study_key="S_GATE",
        subject_key="S01",
        form_id=303,
        form_key="F_AE",
        record_data={"AETERM": "Mild Rash"},
    )
    sdk = _build_stateful_sdtm_sdk({2: schema_valid_rec})
    retained = list(
        apply_quality_gate(
            sdk,
            "S_GATE",
            [schema_valid_rec],
            SinkConfig(
                study_key="S_GATE",
                quality_gate_enabled=True,
                min_schema_readiness_score=80.0,
                standards_profile="drug",
                min_standards_score=None,
            ),
        )
    )
    assert len(retained) == 1

    with pytest.raises(ExportError):
        export_to_csv(
            sdk,
            "S_GATE",
            tmp_path / "gate_fail.csv",
            sdtm_domain="AE",
            standards_profile="drug",
            min_standards_score=95.0,
        )


def test_out_of_order_records_chronological_seq_across_batch_and_chunk_sizes_and_bounded_streaming(
    tmp_path: Path,
) -> None:
    """AC 6 & Req 3/4: Out-of-order mixed-Form Records assign identical chronological <DOM>SEQ across batch_size=1, chunk_size=1, and batch_size=100, and stream 1 Subject in memory."""
    from imednet.integrations.enrichment import CentralizedMapper
    from imednet.integrations.sink_base import ExportSink, SinkConfig, run_streaming_export
    from imednet.models.sdtm import iter_subject_ordered_sdtm_records

    study_key = "S_AC6_SEQ"
    # Mixed-Form, out-of-order timestamps & recordIds across S02 and S01 (with F_DM first so records[0] is DM)
    upstream = {
        10: Record(
            record_id=10,
            study_key=study_key,
            subject_key="S02",
            form_id=301,
            form_key="F_DM",
            date_created=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            record_data={"SEX": "M", "BRTHDTC": "1975-02-03"},
        ),
        30: Record(
            record_id=30,
            study_key=study_key,
            subject_key="S01",
            form_id=303,
            form_key="F_AE",
            date_created=datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
            record_data={"AETERM": "Third chronologically", "AESTDTC": "2026-05-03"},
        ),
        20: Record(
            record_id=20,
            study_key=study_key,
            subject_key="S01",
            form_id=303,
            form_key="F_AE",
            date_created=datetime(2026, 5, 1, 8, 0, tzinfo=UTC),
            record_data={"AETERM": "First chronologically", "AESTDTC": "2026-05-01"},
        ),
        25: Record(
            record_id=25,
            study_key=study_key,
            subject_key="S01",
            form_id=302,
            form_key="F_VS",
            date_created=datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
            record_data={"VSTESTCD": "HR", "VSORRES": 72},
        ),
        22: Record(
            record_id=22,
            study_key=study_key,
            subject_key="S01",
            form_id=303,
            form_key="F_AE",
            date_created=datetime(2026, 5, 2, 8, 0, tzinfo=UTC),
            record_data={"AETERM": "Second chronologically", "AESTDTC": "2026-05-02"},
        ),
    }
    sdk = _build_stateful_sdtm_sdk(upstream)

    csv_b1 = tmp_path / "b1.csv"
    csv_b100 = tmp_path / "b100.csv"
    export_to_csv(sdk, study_key, csv_b1, as_sdtm=True, batch_size=1, quality_gate_enabled=False)
    export_to_csv(
        sdk, study_key, csv_b100, as_sdtm=True, batch_size=100, quality_gate_enabled=False
    )

    df_csv_b1 = pd.read_csv(csv_b1)
    df_csv_b100 = pd.read_csv(csv_b100)
    ae_b1 = df_csv_b1[df_csv_b1["DOMAIN"] == "AE"].sort_values("AESEQ")
    ae_b100 = df_csv_b100[df_csv_b100["DOMAIN"] == "AE"].sort_values("AESEQ")
    assert list(ae_b1["AESEQ"].astype(int)) == [1, 2, 3]
    assert list(ae_b1["AETERM"]) == [
        "First chronologically",
        "Second chronologically",
        "Third chronologically",
    ]
    assert list(ae_b1["AETERM"]) == list(ae_b100["AETERM"])
    assert set(df_csv_b1["DOMAIN"]) == {"DM", "VS", "AE"}

    hive_c1 = tmp_path / "hive_c1"
    hive_c100 = tmp_path / "hive_c100"
    export_to_hive_parquet(
        sdk, study_key, hive_c1, as_sdtm=True, chunk_size=1, quality_gate_enabled=False
    )
    export_to_hive_parquet(
        sdk, study_key, hive_c100, as_sdtm=True, chunk_size=100, quality_gate_enabled=False
    )
    df_hive_c1 = pd.concat(
        [pd.read_parquet(f) for f in sorted(hive_c1.rglob("*.parquet"))], ignore_index=True
    )
    df_hive_c100 = pd.concat(
        [pd.read_parquet(f) for f in sorted(hive_c100.rglob("*.parquet"))], ignore_index=True
    )
    ae_h1 = df_hive_c1[df_hive_c1["DOMAIN"] == "AE"].sort_values("AESEQ")
    ae_h100 = df_hive_c100[df_hive_c100["DOMAIN"] == "AE"].sort_values("AESEQ")
    assert list(ae_h1["AESEQ"].astype(int)) == [1, 2, 3]
    assert list(ae_h1["AETERM"]) == list(ae_h100["AETERM"])

    # CentralizedMapper + run_streaming_export equivalence between batch_size=1 and batch_size=100
    class _CaptureSink(ExportSink):
        def __init__(self, cfg: SinkConfig) -> None:
            super().__init__(cfg)
            self.cfg = cfg
            self.mapper = CentralizedMapper(
                naming_convention="sdtm",
                as_sdtm=True,
                preserve_record_id=True,
            )
            self.rows: list[dict[str, Any]] = []

        def write_batch(self, records: Any, *, batch_id: str) -> int:
            mapped = self.mapper.map_batch(list(records), study_key=study_key)
            self.rows.extend(mapped)
            return len(mapped)

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    sink_b1 = _CaptureSink(
        SinkConfig(study_key=study_key, as_sdtm=True, batch_size=1, quality_gate_enabled=False)
    )
    sink_b100 = _CaptureSink(
        SinkConfig(study_key=study_key, as_sdtm=True, batch_size=100, quality_gate_enabled=False)
    )
    run_streaming_export(sdk, study_key, sink_b1, sink_b1.cfg)
    run_streaming_export(sdk, study_key, sink_b100, sink_b100.cfg)
    ae_stream_b1 = sorted(
        [r for r in sink_b1.rows if r["DOMAIN"] == "AE"], key=lambda r: int(r["AESEQ"])
    )
    ae_stream_b100 = sorted(
        [r for r in sink_b100.rows if r["DOMAIN"] == "AE"], key=lambda r: int(r["AESEQ"])
    )
    assert [int(r["AESEQ"]) for r in ae_stream_b1] == [1, 2, 3]
    assert [r["AETERM"] for r in ae_stream_b1] == [r["AETERM"] for r in ae_stream_b100]

    # Bounded 1-Subject streaming memory check when source_is_ordered=True
    pulled_subjects: list[str] = []

    def _subject_sorted_upstream() -> Any:
        ordered = [
            Record(
                record_id=2,
                study_key=study_key,
                subject_key="S01",
                form_id=303,
                form_key="F_AE",
                date_created=datetime(2026, 5, 2, tzinfo=UTC),
                record_data={"AETERM": "Later"},
            ),
            Record(
                record_id=1,
                study_key=study_key,
                subject_key="S01",
                form_id=303,
                form_key="F_AE",
                date_created=datetime(2026, 5, 1, tzinfo=UTC),
                record_data={"AETERM": "Earlier"},
            ),
            Record(
                record_id=3,
                study_key=study_key,
                subject_key="S02",
                form_id=303,
                form_key="F_AE",
                date_created=datetime(2026, 5, 1, tzinfo=UTC),
                record_data={"AETERM": "Subject2"},
            ),
            Record(
                record_id=4,
                study_key=study_key,
                subject_key="S03",
                form_id=303,
                form_key="F_AE",
                date_created=datetime(2026, 5, 1, tzinfo=UTC),
                record_data={"AETERM": "Subject3"},
            ),
        ]
        for rec in ordered:
            assert rec.subject_key is not None
            pulled_subjects.append(rec.subject_key)
            yield rec

    stream_iter = iter_subject_ordered_sdtm_records(
        _subject_sorted_upstream(),
        study_key=study_key,
        source_is_ordered=True,
    )
    first_emitted = next(stream_iter)
    second_emitted = next(stream_iter)
    assert first_emitted.record_id == 1
    assert second_emitted.record_id == 2
    assert "S03" not in pulled_subjects, "Bounded streaming must flush S01 before pulling S03"


def test_enrichment_pipeline_cross_domain_composite_keys_and_fallback_values() -> None:
    """AC 9: EnrichmentPipeline indexes rules by (domain, source_form_key, source_variable_name) and applies fallback_value on missing/empty inputs."""
    from imednet.integrations.enrichment import EnrichmentPipeline
    from imednet.models.study_config import MappingRule

    cfg = StudyConfiguration(
        studyKey="S_ENRICH",
        version="1.0.0",
        reportingProfile="general",
        mappings=[
            MappingRule(
                sourceFormKey="F_AE",
                sourceVariableName="RAW_TEXT",
                domain="AE",
                targetField="AETERM",
                fallbackValue="UNSPECIFIED_AE",
            ),
            MappingRule(
                sourceFormKey="F_CM",
                sourceVariableName="RAW_TEXT",
                domain="CM",
                targetField="CMTRT",
                fallbackValue="UNSPECIFIED_CM",
            ),
        ],
    )
    pipeline = EnrichmentPipeline(config=cfg)

    # Cross-domain rules sharing RAW_TEXT do not collide
    ae_present = pipeline.process(
        {"formKey": "F_AE", "subjectKey": "S01", "RAW_TEXT": "Migraine"},
        domain="AE",
    )
    cm_present = pipeline.process(
        {"formKey": "F_CM", "subjectKey": "S01", "RAW_TEXT": "Ibuprofen"},
        domain="CM",
    )
    assert ae_present["AETERM"] == "Migraine" and "CMTRT" not in ae_present
    assert cm_present["CMTRT"] == "Ibuprofen" and "AETERM" not in cm_present

    # Missing, None, empty string, and whitespace-only inputs receive fallback_value
    for empty_payload in (
        {"formKey": "F_AE", "subjectKey": "S01"},
        {"formKey": "F_AE", "subjectKey": "S01", "RAW_TEXT": None},
        {"formKey": "F_AE", "subjectKey": "S01", "RAW_TEXT": ""},
        {"formKey": "F_AE", "subjectKey": "S01", "RAW_TEXT": "   "},
    ):
        enriched = pipeline.process(dict(empty_payload), domain="AE")
        assert enriched["AETERM"] == "UNSPECIFIED_AE"
