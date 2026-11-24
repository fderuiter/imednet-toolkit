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
