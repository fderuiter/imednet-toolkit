"""Comprehensive tests for incremental file/DB export reconciliation and RFC 8259 JSON."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from imednet.integrations.export import (
    TabularCSVSink,
    TabularParquetSink,
    TabularSinkConfig,
    export_to_csv,
    export_to_duckdb,
    export_to_duckdb_by_form,
    export_to_excel,
    export_to_json,
    export_to_long_sql,
    export_to_parquet,
    export_to_sql,
    export_to_sql_by_form,
)
from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet_workflows.state_ledger import FileStateProvider


def _reject_nonfinite_json_constant(token: str) -> Any:
    raise ValueError(f"Non-RFC 8259 JSON token encountered: {token!r}")


def _build_study_sdk(
    upstream_records: dict[int, Record], study_key: str = "STUDY_INC"
) -> MagicMock:
    """Create a realistic stateful mock SDK honoring filter, record_data_filter, and watermarks."""
    forms = [
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
    intervals = [
        Interval(
            interval_id=401,
            interval_name="Baseline",
            interval_sequence=1,
            interval_description="Baseline Visit",
            interval_group_name="Core",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
            forms=[FormSummary(form_id=302, form_key="F_VS", form_name="Vital Signs")],
        )
    ]
    variables = [
        Variable(
            variable_id=1,
            variable_name="AGE",
            label="Participant Age",
            variable_type="int",
            form_id=301,
            form_key="F_DM",
        ),
        Variable(
            variable_id=2,
            variable_name="patientName",
            label="Participant Name",
            variable_type="text",
            form_id=301,
            form_key="F_DM",
        ),
        Variable(
            variable_id=3,
            variable_name="SYSBP",
            label="Systolic BP",
            variable_type="float",
            form_id=302,
            form_key="F_VS",
        ),
        Variable(
            variable_id=4,
            variable_name="AETERM",
            label="AE Term",
            variable_type="text",
            form_id=303,
            form_key="F_AE",
        ),
    ]

    def _list_records(**kw: Any) -> Any:
        from imednet.integrations.sink_base import normalize_watermark_datetime

        flt = str(kw.get("filter", "") or "")
        assert not (";" in flt and "," in flt)
        items = list(upstream_records.values())
        if kw.get("deleted") is False:
            items = [r for r in items if not r.deleted]
        elif kw.get("deleted") is True:
            items = [r for r in items if r.deleted]
        for attr_kw, model_attr in (
            ("dateModified", "date_modified"),
            ("lastUpdated", "last_updated"),
        ):
            if kw.get(attr_kw) is not None:
                clause_val = kw[attr_kw]
                if isinstance(clause_val, tuple) and len(clause_val) == 2:
                    op_str, raw_ts = clause_val
                    wm_dt = normalize_watermark_datetime(raw_ts)
                    if wm_dt is not None:
                        if op_str == ">=":
                            items = [
                                r
                                for r in items
                                if (
                                    ts := normalize_watermark_datetime(getattr(r, model_attr, None))
                                )
                                is not None
                                and ts >= wm_dt
                            ]
                        elif op_str == ">":
                            items = [
                                r
                                for r in items
                                if (
                                    ts := normalize_watermark_datetime(getattr(r, model_attr, None))
                                )
                                is not None
                                and ts > wm_dt
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
        if kw.get("subjectKey") is not None or kw.get("subject_key") is not None:
            raw_sks = kw["subjectKey"] if kw.get("subjectKey") is not None else kw["subject_key"]
            if isinstance(raw_sks, tuple) and len(raw_sks) == 2 and raw_sks[0] in ("==", "="):
                sks = {str(raw_sks[1])}
            elif isinstance(raw_sks, (list, tuple, set)):
                sks = {str(x) for x in raw_sks}
            else:
                sks = {str(raw_sks)}
            items = [r for r in items if r.subject_key in sks]
        for clause in (c.strip() for c in flt.split(";") if c.strip()):
            if clause == "deleted==false":
                items = [r for r in items if not r.deleted]
            elif clause.startswith("dateModified>="):
                wm_str = clause.split(">=", 1)[1].strip('"')
                wm_dt = normalize_watermark_datetime(wm_str)
                if wm_dt is not None:
                    items = [
                        r
                        for r in items
                        if (ts := normalize_watermark_datetime(r.date_modified)) is not None
                        and ts >= wm_dt
                    ]
            elif clause.startswith("lastUpdated>="):
                wm_str = clause.split(">=", 1)[1].strip('"')
                wm_dt = normalize_watermark_datetime(wm_str)
                if wm_dt is not None:
                    items = [
                        r
                        for r in items
                        if (ts := normalize_watermark_datetime(r.last_updated)) is not None
                        and ts >= wm_dt
                    ]
            elif clause.startswith("formId=="):
                fid = int(clause.split("==", 1)[1])
                items = [r for r in items if r.form_id == fid]
            elif clause.startswith("subjectKey=="):
                sk = clause.split("==", 1)[1]
                items = [r for r in items if r.subject_key == sk]
        return iter([r.model_copy(deep=True) for r in items])

    sdk = MagicMock()
    sdk.get_forms.return_value = forms
    sdk.forms.list.return_value = forms
    sdk.get_intervals.return_value = intervals
    sdk.get_variables.return_value = variables
    sdk.variables.list.side_effect = lambda **kw: (
        [
            v
            for v in variables
            if v.form_id in (kw["formId"] if isinstance(kw["formId"], list) else [kw["formId"]])
        ]
        if "formId" in kw
        else list(variables)
    )
    sdk.records.list.side_effect = _list_records
    return sdk


def _make_10_records(study_key: str = "STUDY_INC") -> dict[int, Record]:
    """Create 10 Study Records spanning Enrollment F_DM, Scheduled F_VS, and Unscheduled F_AE."""
    records: dict[int, Record] = {}
    for rid in range(1, 11):
        if rid <= 3:
            form_id, form_key, visit_id = 301, "F_DM", 401
            payload: dict[str, Any] = {"AGE": 30 + rid, "patientName": f"Alice {rid}"}
        elif rid <= 8:
            form_id, form_key, visit_id = 302, "F_VS", 401
            payload = {"SYSBP": 110.0 + rid}
        else:
            form_id, form_key, visit_id = 303, "F_AE", None
            payload = {"AETERM": f"Event {rid}"}

        records[rid] = Record(
            record_id=rid,
            study_key=study_key,
            subject_key=f"SUBJ-{(rid - 1) % 3 + 1:02d}",
            visit_id=visit_id,
            form_id=form_id,
            form_key=form_key,
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, 10, rid, tzinfo=UTC),
            record_data=payload,
        )
    return records


def _extract_hier_records(tree: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for subj in tree:
        for visit in subj.get("visits", []):
            for fnode in visit.get("forms", []):
                for rec in fnode.get("records", []):
                    by_id[int(rec["recordId"])] = rec
        for fnode in subj.get("forms", []):
            for rec in fnode.get("records", []):
                by_id[int(rec["recordId"])] = rec
    return by_id


def test_all_destinations_retain_10_records_update_deltas_delete_voided_and_preserve_zero_delta(
    tmp_path: Path,
) -> None:
    """Verify 10-record retention, delta upserts, voided deletions, PHI masking, and zero-delta safety across all formats."""
    pytest.importorskip("pyarrow")
    pytest.importorskip("openpyxl")
    duckdb = pytest.importorskip("duckdb")

    upstream = _make_10_records()
    sdk = _build_study_sdk(upstream)
    ledger_dir = tmp_path / "ledgers"
    out_dir = tmp_path / "exports"
    out_dir.mkdir()

    csv_path = out_dir / "records.csv"
    pq_path = out_dir / "records.parquet"
    xl_path = out_dir / "records.xlsx"
    json_flat_path = out_dir / "records_flat.json"
    json_hier_path = out_dir / "records_hier.json"
    sql_db = tmp_path / "sql_dest" / "records.sqlite"
    sql_by_form_db = tmp_path / "sql_dest" / "by_form.sqlite"
    long_sql_db = tmp_path / "sql_dest" / "long.sqlite"
    sql_db.parent.mkdir()
    ddb_path = tmp_path / "ddb_dest" / "records.duckdb"
    ddb_by_form_path = tmp_path / "ddb_dest" / "by_form.duckdb"
    ddb_path.parent.mkdir()

    def _run_all_incremental() -> None:
        common = {"incremental": True, "ledger_path": str(ledger_dir), "batch_size": 3}
        export_to_csv(sdk, "STUDY_INC", str(csv_path), quality_gate_enabled=False, **common)
        export_to_parquet(sdk, "STUDY_INC", str(pq_path), **common)
        export_to_excel(sdk, "STUDY_INC", str(xl_path), **common)
        export_to_json(sdk, "STUDY_INC", str(json_flat_path), **common)
        export_to_json(sdk, "STUDY_INC", str(json_hier_path), hierarchical=True, **common)
        export_to_sql(
            sdk,
            "STUDY_INC",
            "records",
            f"sqlite:///{sql_db}",
            quality_gate_enabled=False,
            **common,
        )
        export_to_sql_by_form(sdk, "STUDY_INC", f"sqlite:///{sql_by_form_db}", **common)
        export_to_long_sql(
            sdk,
            "STUDY_INC",
            "long_records",
            f"sqlite:///{long_sql_db}",
            chunk_size=4,
            **common,
        )
        export_to_duckdb(sdk, "STUDY_INC", str(ddb_path), "records", **common)
        export_to_duckdb_by_form(sdk, "STUDY_INC", str(ddb_by_form_path), **common)

    # 1. Run 1 (initial 10 records) + Run 2 (consecutive incremental run with zero upstream changes)
    _run_all_incremental()
    _run_all_incremental()

    # Verify zero sidecar SQLite/DuckDB files created in file export directory
    sidecars = [
        p.name for p in out_dir.iterdir() if p.suffix in {".db", ".sqlite", ".sqlite3", ".duckdb"}
    ]
    assert sidecars == []

    expected_10 = list(range(1, 11))
    csv_df = pd.read_csv(csv_path)
    assert sorted(csv_df["recordId"].tolist()) == expected_10
    assert (csv_df.loc[csv_df["recordId"] == 1, "patientName"] == "***MASKED***").all()

    pq_df = pd.read_parquet(pq_path)
    assert sorted(pq_df["recordId"].tolist()) == expected_10
    assert pq_df.loc[pq_df["recordId"] == 1, "patientName"].iloc[0] == "***MASKED***"

    xl_df = pd.read_excel(xl_path)
    assert sorted(xl_df["recordId"].tolist()) == expected_10
    assert xl_df.loc[xl_df["recordId"] == 1, "patientName"].iloc[0] == "***MASKED***"

    flat_json = json.loads(
        json_flat_path.read_text(encoding="utf-8"),
        parse_constant=_reject_nonfinite_json_constant,
    )
    assert sorted(r["recordId"] for r in flat_json) == expected_10
    assert next(r for r in flat_json if r["recordId"] == 1)["patientName"] == "***MASKED***"

    hier_json = json.loads(
        json_hier_path.read_text(encoding="utf-8"),
        parse_constant=_reject_nonfinite_json_constant,
    )
    hier_by_id = _extract_hier_records(hier_json)
    assert sorted(hier_by_id) == expected_10
    assert hier_by_id[1]["patientName"] == "***MASKED***"
    assert hier_by_id[9]["AETERM"] == "Event 9"

    with sqlite3.connect(sql_db) as sconn:
        assert [
            r[0]
            for r in sconn.execute(
                'SELECT "recordId" FROM "records" ORDER BY "recordId"'
            ).fetchall()
        ] == expected_10
    with sqlite3.connect(sql_by_form_db) as sconn:
        by_form_ids = sorted(
            r[0]
            for tbl in ("F_DM", "F_VS", "F_AE")
            for r in sconn.execute(f'SELECT "recordId" FROM "{tbl}"').fetchall()
        )
        assert by_form_ids == expected_10
    with sqlite3.connect(long_sql_db) as sconn:
        long_ids = sorted(
            r[0] for r in sconn.execute("SELECT DISTINCT record_id FROM long_records").fetchall()
        )
        assert long_ids == expected_10
        masked_names = [
            r[0]
            for r in sconn.execute(
                "SELECT value FROM long_records WHERE variable_name = 'patientName'"
            ).fetchall()
        ]
        assert masked_names == ["***MASKED***", "***MASKED***", "***MASKED***"]

    with duckdb.connect(str(ddb_path)) as dconn:
        assert [
            r[0]
            for r in dconn.execute(
                'SELECT "recordId" FROM "records" ORDER BY "recordId"'
            ).fetchall()
        ] == expected_10
    with duckdb.connect(str(ddb_by_form_path)) as dconn:
        ddb_form_ids = sorted(
            r[0]
            for tbl in ("F_DM", "F_VS", "F_AE")
            for r in dconn.execute(f'SELECT "recordId" FROM "{tbl}"').fetchall()
        )
        assert ddb_form_ids == expected_10

    # Verify FileStateProvider checkpoints persisted for duckdb_by_form and long_sql
    provider = FileStateProvider(str(ledger_dir))
    assert provider.get_last_timestamp("STUDY_INC", "duckdb_F_DM") is not None
    assert provider.get_last_timestamp("STUDY_INC", "duckdb_F_VS") is not None
    assert provider.get_last_timestamp("STUDY_INC", "duckdb_F_AE") is not None
    assert provider.get_last_timestamp("STUDY_INC", "long_sql_long_records") is not None

    # 2. Run 3: Modify recordId=4 (SYSBP=145.5), void recordId=2 (F_DM) and recordId=9 (F_AE), add recordId=11
    upstream[4] = upstream[4].model_copy(
        update={
            "date_modified": datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
            "record_data": {"SYSBP": 145.5},
        }
    )
    upstream[2] = upstream[2].model_copy(
        update={
            "deleted": True,
            "date_modified": datetime(2026, 6, 2, 9, 5, tzinfo=UTC),
        }
    )
    upstream[9] = upstream[9].model_copy(
        update={
            "deleted": True,
            "date_modified": datetime(2026, 6, 2, 9, 10, tzinfo=UTC),
        }
    )
    upstream[11] = Record(
        record_id=11,
        study_key="STUDY_INC",
        subject_key="SUBJ-01",
        visit_id=None,
        form_id=303,
        form_key="F_AE",
        record_status="Complete",
        deleted=False,
        date_created=datetime(2026, 6, 2, 9, 15, tzinfo=UTC),
        date_modified=datetime(2026, 6, 2, 9, 15, tzinfo=UTC),
        record_data={"AETERM": "Nausea"},
    )

    _run_all_incremental()

    expected_after_delta = [1, 3, 4, 5, 6, 7, 8, 10, 11]

    csv_df2 = pd.read_csv(csv_path)
    assert sorted(csv_df2["recordId"].tolist()) == expected_after_delta
    assert float(csv_df2.loc[csv_df2["recordId"] == 4, "SYSBP"].iloc[0]) == 145.5

    pq_df2 = pd.read_parquet(pq_path)
    assert sorted(pq_df2["recordId"].tolist()) == expected_after_delta
    assert float(pq_df2.loc[pq_df2["recordId"] == 4, "SYSBP"].iloc[0]) == 145.5

    xl_df2 = pd.read_excel(xl_path)
    assert sorted(xl_df2["recordId"].tolist()) == expected_after_delta
    assert float(xl_df2.loc[xl_df2["recordId"] == 4, "SYSBP"].iloc[0]) == 145.5

    flat_json2 = json.loads(
        json_flat_path.read_text(encoding="utf-8"),
        parse_constant=_reject_nonfinite_json_constant,
    )
    assert sorted(r["recordId"] for r in flat_json2) == expected_after_delta
    assert next(r for r in flat_json2 if r["recordId"] == 4)["SYSBP"] == 145.5

    hier_json2 = json.loads(
        json_hier_path.read_text(encoding="utf-8"),
        parse_constant=_reject_nonfinite_json_constant,
    )
    hier_by_id2 = _extract_hier_records(hier_json2)
    assert sorted(hier_by_id2) == expected_after_delta
    assert hier_by_id2[4]["SYSBP"] == 145.5
    assert hier_by_id2[11]["AETERM"] == "Nausea"

    with sqlite3.connect(sql_db) as sconn:
        assert [
            r[0]
            for r in sconn.execute(
                'SELECT "recordId" FROM "records" ORDER BY "recordId"'
            ).fetchall()
        ] == expected_after_delta
        assert (
            float(sconn.execute('SELECT "SYSBP" FROM "records" WHERE "recordId" = 4').fetchone()[0])
            == 145.5
        )
    with sqlite3.connect(sql_by_form_db) as sconn:
        by_form_ids2 = sorted(
            r[0]
            for tbl in ("F_DM", "F_VS", "F_AE")
            for r in sconn.execute(f'SELECT "recordId" FROM "{tbl}"').fetchall()
        )
        assert by_form_ids2 == expected_after_delta
    with sqlite3.connect(long_sql_db) as sconn:
        long_ids2 = sorted(
            r[0] for r in sconn.execute("SELECT DISTINCT record_id FROM long_records").fetchall()
        )
        assert long_ids2 == expected_after_delta
        val_4 = float(
            sconn.execute(
                "SELECT value FROM long_records WHERE record_id = 4 AND variable_name = 'SYSBP'"
            ).fetchone()[0]
        )
        assert val_4 == 145.5

    with duckdb.connect(str(ddb_path)) as dconn:
        assert [
            r[0]
            for r in dconn.execute(
                'SELECT "recordId" FROM "records" ORDER BY "recordId"'
            ).fetchall()
        ] == expected_after_delta
        assert (
            float(dconn.execute('SELECT "SYSBP" FROM "records" WHERE "recordId" = 4').fetchone()[0])
            == 145.5
        )
    with duckdb.connect(str(ddb_by_form_path)) as dconn:
        ddb_form_ids2 = sorted(
            r[0]
            for tbl in ("F_DM", "F_VS", "F_AE")
            for r in dconn.execute(f'SELECT "recordId" FROM "{tbl}"').fetchall()
        )
        assert ddb_form_ids2 == expected_after_delta
        assert (
            float(dconn.execute('SELECT "SYSBP" FROM "F_VS" WHERE "recordId" = 4').fetchone()[0])
            == 145.5
        )

    # 3. Run 4: Explicit future modified_since returning 0 records leaves all destinations intact
    future_wm = "2027-01-01T00:00:00Z"
    export_to_csv(
        sdk, "STUDY_INC", str(csv_path), quality_gate_enabled=False, modified_since=future_wm
    )
    export_to_parquet(sdk, "STUDY_INC", str(pq_path), modified_since=future_wm)
    export_to_excel(sdk, "STUDY_INC", str(xl_path), modified_since=future_wm)
    export_to_json(sdk, "STUDY_INC", str(json_flat_path), modified_since=future_wm)
    export_to_json(
        sdk, "STUDY_INC", str(json_hier_path), hierarchical=True, modified_since=future_wm
    )
    export_to_sql(
        sdk,
        "STUDY_INC",
        "records",
        f"sqlite:///{sql_db}",
        quality_gate_enabled=False,
        modified_since=future_wm,
    )
    export_to_sql_by_form(sdk, "STUDY_INC", f"sqlite:///{sql_by_form_db}", modified_since=future_wm)
    export_to_long_sql(
        sdk,
        "STUDY_INC",
        "long_records",
        f"sqlite:///{long_sql_db}",
        modified_since=future_wm,
    )
    export_to_duckdb(sdk, "STUDY_INC", str(ddb_path), "records", modified_since=future_wm)
    export_to_duckdb_by_form(sdk, "STUDY_INC", str(ddb_by_form_path), modified_since=future_wm)

    assert sorted(pd.read_csv(csv_path)["recordId"].tolist()) == expected_after_delta
    assert sorted(pd.read_parquet(pq_path)["recordId"].tolist()) == expected_after_delta
    assert sorted(pd.read_excel(xl_path)["recordId"].tolist()) == expected_after_delta
    assert (
        sorted(r["recordId"] for r in json.loads(json_flat_path.read_text(encoding="utf-8")))
        == expected_after_delta
    )
    assert (
        sorted(_extract_hier_records(json.loads(json_hier_path.read_text(encoding="utf-8"))))
        == expected_after_delta
    )
    with sqlite3.connect(long_sql_db) as sconn:
        assert (
            sorted(
                r[0]
                for r in sconn.execute("SELECT DISTINCT record_id FROM long_records").fetchall()
            )
            == expected_after_delta
        )
    with duckdb.connect(str(ddb_by_form_path)) as dconn:
        assert (
            sorted(
                r[0]
                for tbl in ("F_DM", "F_VS", "F_AE")
                for r in dconn.execute(f'SELECT "recordId" FROM "{tbl}"').fetchall()
            )
            == expected_after_delta
        )


def test_strict_rfc8259_json_serializes_missing_and_nonfinite_floats_as_null(
    tmp_path: Path,
) -> None:
    """Verify flat and hierarchical export_to_json emit strict RFC 8259 JSON with zero NaN/Infinity tokens."""
    records = {
        1: Record(
            record_id=1,
            study_key="S_NAN",
            subject_key="SUBJ-01",
            visit_id=401,
            form_id=302,
            form_key="F_VS",
            record_status="Complete",
            date_created=datetime(2026, 6, 1, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, tzinfo=UTC),
            record_data={"SYSBP": 120.5},
        ),
        2: Record(
            record_id=2,
            study_key="S_NAN",
            subject_key="SUBJ-01",
            visit_id=401,
            form_id=301,
            form_key="F_DM",
            record_status="Complete",
            date_created=datetime(2026, 6, 1, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, tzinfo=UTC),
            # AGE present, SYSBP missing -> float64 NaN in mixed-form DataFrame!
            record_data={"AGE": 52, "patientName": "Bob"},
        ),
        3: Record(
            record_id=3,
            study_key="S_NAN",
            subject_key="SUBJ-02",
            visit_id=401,
            form_id=302,
            form_key="F_VS",
            record_status="Complete",
            date_created=datetime(2026, 6, 1, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, tzinfo=UTC),
            record_data={"SYSBP": math.nan},
        ),
    }
    sdk = _build_study_sdk(records, study_key="S_NAN")

    flat_out = tmp_path / "strict_flat.json"
    hier_out = tmp_path / "strict_hier.json"

    export_to_json(sdk, "S_NAN", str(flat_out))
    export_to_json(sdk, "S_NAN", str(hier_out), hierarchical=True)

    raw_flat = flat_out.read_text(encoding="utf-8")
    raw_hier = hier_out.read_text(encoding="utf-8")
    assert "NaN" not in raw_flat
    assert "Infinity" not in raw_flat
    assert "NaN" not in raw_hier
    assert "Infinity" not in raw_hier

    parsed_flat = json.loads(raw_flat, parse_constant=_reject_nonfinite_json_constant)
    parsed_hier = json.loads(raw_hier, parse_constant=_reject_nonfinite_json_constant)

    rec2_flat = next(r for r in parsed_flat if r["recordId"] == 2)
    assert rec2_flat["SYSBP"] is None
    assert rec2_flat["AGE"] == 52

    rec3_flat = next(r for r in parsed_flat if r["recordId"] == 3)
    assert rec3_flat["SYSBP"] is None

    hier_by_id = _extract_hier_records(parsed_hier)
    assert hier_by_id[3]["SYSBP"] is None


def test_parquet_and_csv_incremental_schema_widening_and_voided_only_runs(
    tmp_path: Path,
) -> None:
    """Verify TabularCSVSink and TabularParquetSink handle schema widening, duplicate delta IDs, and void-only runs."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    csv_file = tmp_path / "widen.csv"
    pq_file = tmp_path / "widen.parquet"

    # Initial full run: SCORE is int64, 3 rows
    base_cfg = TabularSinkConfig(study_key="S_WIDEN", batch_size=2)
    initial_rows = [
        {"recordId": 1, "SCORE": 10},
        {"recordId": 2, "SCORE": 20},
        {"recordId": 3, "SCORE": 30},
    ]
    with TabularCSVSink(str(csv_file), base_cfg) as csink:
        csink.write_batch(initial_rows, batch_id="b0")
    with TabularParquetSink(str(pq_file), base_cfg) as psink:
        psink.write_batch(initial_rows, batch_id="b0")

    # Incremental run 2: update recordId=1 twice across batches, widen SCORE to string + add NOTE column
    inc_cfg = TabularSinkConfig(study_key="S_WIDEN", incremental=True, batch_size=1)
    with TabularCSVSink(str(csv_file), inc_cfg) as csink:
        csink.write_batch([{"recordId": 1, "SCORE": 15, "NOTE": "first"}], batch_id="b1")
        csink.write_batch([{"recordId": 1, "SCORE": "pending", "NOTE": "final"}], batch_id="b2")
        assert csink.reconcile_deleted([{"recordId": 2}], batch_id="b2") == 1

    with TabularParquetSink(str(pq_file), inc_cfg) as psink:
        psink.write_batch([{"recordId": 1, "SCORE": 15, "NOTE": "first"}], batch_id="b1")
        psink.write_batch([{"recordId": 1, "SCORE": "pending", "NOTE": "final"}], batch_id="b2")
        assert psink.reconcile_deleted([{"recordId": 2}], batch_id="b2") == 1

    csv_df = pd.read_csv(csv_file)
    assert sorted(csv_df["recordId"].tolist()) == [1, 3]
    assert csv_df.loc[csv_df["recordId"] == 1, "SCORE"].iloc[0] == "pending"
    assert csv_df.loc[csv_df["recordId"] == 1, "NOTE"].iloc[0] == "final"

    pq_tbl = pq.read_table(pq_file)
    assert pq_tbl.schema.field("SCORE").type == pa.string()
    pq_dict = pq_tbl.to_pydict()
    assert pq_dict["recordId"] == [3, 1]
    assert pq_dict["SCORE"] == ["30", "pending"]
    assert pq_dict["NOTE"] == [None, "final"]

    # Incremental run 3: void-only run (0 active batches, recordId=3 voided)
    with TabularCSVSink(str(csv_file), inc_cfg) as csink:
        csink.reconcile_deleted([{"recordId": 3}], batch_id="b3")
    with TabularParquetSink(str(pq_file), inc_cfg) as psink:
        psink.reconcile_deleted([{"recordId": 3}], batch_id="b3")

    assert pd.read_csv(csv_file)["recordId"].tolist() == [1]
    assert pq.read_table(pq_file).to_pydict()["recordId"] == [1]


def test_hierarchical_json_quality_gate_drops_low_readiness_records(tmp_path: Path) -> None:
    """Verify export_to_json(hierarchical=True, quality_gate_enabled=True) filters records via SchemaValidator."""
    records = {
        1: Record(
            record_id=1,
            study_key="S_QG",
            subject_key="SUBJ-1",
            visit_id=401,
            form_id=302,
            form_key="F_VS",
            record_status="Complete",
            date_created=datetime(2026, 6, 1, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, tzinfo=UTC),
            record_data={"SYSBP": 120.0},
        ),
        2: Record(
            record_id=2,
            study_key="S_QG",
            subject_key="SUBJ-2",
            visit_id=401,
            form_id=999,
            form_key="",
            record_status="Complete",
            date_created=datetime(2026, 6, 1, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, tzinfo=UTC),
            record_data={"UNKNOWN": 1},
        ),
    }
    sdk = _build_study_sdk(records, study_key="S_QG")
    out = tmp_path / "gated_hier.json"
    export_to_json(
        sdk,
        "S_QG",
        str(out),
        hierarchical=True,
        quality_gate_enabled=True,
        min_schema_readiness_score=95.0,
    )
    loaded = json.loads(out.read_text(encoding="utf-8"))
    by_id = _extract_hier_records(loaded)
    assert list(by_id) == [1]
    assert by_id[1]["SYSBP"] == 120.0


def test_incremental_reconciliation_edge_cases(tmp_path: Path) -> None:
    """Cover initial-run dedup, int-to-float widening, void-only hierarchical pruning, and scalar helpers."""
    import numpy as np

    pytest.importorskip("pyarrow")
    import pyarrow as pa
    import pyarrow.parquet as pq

    from imednet.integrations.export import _canonical_record_id, _sanitize_json_value

    # Scalar normalization & RFC 8259 sanitization edge cases
    assert _canonical_record_id(True) is None
    assert _canonical_record_id(42.0) == "42"
    assert _canonical_record_id(42.5) == "42.5"
    assert _canonical_record_id(float("nan")) is None
    assert _canonical_record_id("  99  ") == "99"
    assert _canonical_record_id(np.int64(7)) == "7"
    assert _sanitize_json_value((True, np.int64(5), np.float64("nan"), pd.NA)) == [
        True,
        5,
        None,
        None,
    ]

    # Parquet initial incremental run (dest does not exist yet) with duplicate recordId across batches
    pq_init = tmp_path / "init_dedup.parquet"
    inc_cfg = TabularSinkConfig(study_key="S_EDGE", incremental=True, batch_size=1)
    with TabularParquetSink(str(pq_init), inc_cfg) as psink:
        psink.write_batch([{"recordId": 1, "VAL": 10}], batch_id="b1")
        psink.write_batch([{"recordId": 1, "VAL": 20}, {"recordId": 2, "VAL": 30}], batch_id="b2")
    assert pq.read_table(pq_init).to_pydict() == {"recordId": [1, 2], "VAL": [20, 30]}

    # Int-to-float Arrow schema widening on subsequent incremental run
    with TabularParquetSink(str(pq_init), inc_cfg) as psink2:
        psink2.write_batch([{"recordId": 2, "VAL": 30.5}], batch_id="b3")
    widened = pq.read_table(pq_init)
    assert pa.types.is_floating(widened.schema.field("VAL").type)
    assert widened.to_pydict() == {"recordId": [1, 2], "VAL": [20.0, 30.5]}

    # Hierarchical JSON void-only incremental run prunes empty Subject/Visit/Form containers
    records = {
        1: Record(
            record_id=1,
            study_key="S_PRUNE",
            subject_key="SUBJ-ONLY",
            visit_id=401,
            form_id=302,
            form_key="F_VS",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 1, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            record_data={"SYSBP": 118.0},
        )
    }
    sdk = _build_study_sdk(records, study_key="S_PRUNE")
    hier_path = tmp_path / "prune_hier.json"
    ledger = tmp_path / "prune_ledger.json"

    export_to_json(
        sdk,
        "S_PRUNE",
        str(hier_path),
        hierarchical=True,
        incremental=True,
        ledger_path=str(ledger),
    )
    assert len(json.loads(hier_path.read_text(encoding="utf-8"))) == 1

    records[1] = records[1].model_copy(
        update={"deleted": True, "date_modified": datetime(2026, 6, 2, 10, 0, tzinfo=UTC)}
    )
    export_to_json(
        sdk,
        "S_PRUNE",
        str(hier_path),
        hierarchical=True,
        incremental=True,
        ledger_path=str(ledger),
    )
    assert json.loads(hier_path.read_text(encoding="utf-8")) == []


def test_mapper_and_sql_by_form_export_signed_records_with_older_date_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_make_mapper_records_source and export_to_sql_by_form export 21 CFR 11.10(e) signed records whose dateModified < watermark."""
    import imednet.integrations.export as export_mod

    records = {
        1: Record(
            record_id=1,
            study_key="S_CFR11",
            subject_key="SUBJ-01",
            visit_id=401,
            form_id=302,
            form_key="F_VS",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            last_updated=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            record_data={"SYSBP": 122.0},
        ),
        2: Record(
            record_id=2,
            study_key="S_CFR11",
            subject_key="SUBJ-02",
            visit_id=401,
            form_id=302,
            form_key="F_VS",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
            last_updated=datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
            record_data={"SYSBP": 118.0},
        ),
    }
    sdk = _build_study_sdk(records, study_key="S_CFR11")
    monkeypatch.setenv("IMEDNET_CACHE_DIR", str(tmp_path / "cfr11_cache"))

    csv_path = tmp_path / "cfr11.csv"
    sql_by_form_db = tmp_path / "cfr11_by_form.db"
    ledger_path = tmp_path / "cfr11_ledger.json"

    # Run 1: initial incremental export
    export_to_csv(
        sdk,
        "S_CFR11",
        str(csv_path),
        incremental=True,
        stream_name="csv_cfr11",
        ledger_path=str(ledger_path),
    )
    export_to_sql_by_form(
        sdk,
        "S_CFR11",
        f"sqlite:///{sql_by_form_db}",
        incremental=True,
        stream_name="sql_cfr11",
        ledger_path=str(ledger_path),
    )

    provider = FileStateProvider(ledger_path)
    assert provider.get_last_timestamp("S_CFR11", "csv_cfr11") == datetime(
        2026, 6, 1, 12, 0, tzinfo=UTC
    )
    assert provider.get_last_timestamp("S_CFR11", "sql_cfr11_F_VS") == datetime(
        2026, 6, 1, 12, 0, tzinfo=UTC
    )

    # Upstream: Investigator e-signs Record 1 (dateModified stays < 12:00Z, lastUpdated advances to Jun 4)
    # and edits Record 2 (both dateModified and lastUpdated advance to Jun 2 -> overlaps both branches)
    records[1] = records[1].model_copy(
        update={
            "record_status": "Signed",
            "date_modified": datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            "last_updated": datetime(2026, 6, 4, 16, 20, tzinfo=UTC),
        }
    )
    records[2] = records[2].model_copy(
        update={
            "date_modified": datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
            "last_updated": datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
            "record_data": {"SYSBP": 128.5},
        }
    )

    # Run 2: incremental export updates signed status on Record 1 and advances checkpoints to Jun 4
    export_to_csv(
        sdk,
        "S_CFR11",
        str(csv_path),
        incremental=True,
        stream_name="csv_cfr11",
        ledger_path=str(ledger_path),
    )
    export_to_sql_by_form(
        sdk,
        "S_CFR11",
        f"sqlite:///{sql_by_form_db}",
        incremental=True,
        stream_name="sql_cfr11",
        ledger_path=str(ledger_path),
    )

    csv_df = pd.read_csv(csv_path)
    assert sorted(csv_df["recordId"].tolist()) == [1, 2]
    assert csv_df.loc[csv_df["recordId"] == 1, "recordStatus"].iloc[0] == "Signed"
    assert float(csv_df.loc[csv_df["recordId"] == 2, "SYSBP"].iloc[0]) == 128.5

    with sqlite3.connect(sql_by_form_db) as sconn:
        rows = sconn.execute(
            'SELECT "recordId", "recordStatus", "SYSBP" FROM "F_VS" ORDER BY "recordId"'
        ).fetchall()
    assert rows == [(1, "Signed", 122.0), (2, "Complete", 128.5)]

    assert provider.get_last_timestamp("S_CFR11", "csv_cfr11") == datetime(
        2026, 6, 4, 16, 20, tzinfo=UTC
    )
    assert provider.get_last_timestamp("S_CFR11", "sql_cfr11_F_VS") == datetime(
        2026, 6, 4, 16, 20, tzinfo=UTC
    )

    # Also verify non-loader fallback path in _make_mapper_records_source issues separate branches and deduplicates
    real_mapper = export_mod._record_mapper()(sdk)
    real_mapper._loader = None
    source = export_mod._make_mapper_records_source(
        sdk,
        "S_CFR11",
        real_mapper,
        TabularSinkConfig(study_key="S_CFR11"),
    )
    yielded = list(source(datetime(2026, 6, 1, 12, 0, tzinfo=UTC), True))
    assert sorted(r.record_id for r in yielded) == [1, 2]
    assert next(r for r in yielded if r.record_id == 1).record_status == "Signed"
