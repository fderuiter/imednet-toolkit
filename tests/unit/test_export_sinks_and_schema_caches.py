"""Regression tests for in-place export sink evolution and retired-variable schema caches."""

from __future__ import annotations

import csv
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect

from imednet.constants import MAX_SQLITE_COLUMNS
from imednet.datadict import Choice, DataDictionary
from imednet.datadict import Form as DDForm
from imednet.datadict import Variable as DDVariable
from imednet.datadict.logic import extract_variable_constraints
from imednet.errors import ValidationError
from imednet.integrations.export import (
    TabularCSVSink,
    TabularSinkConfig,
    TabularSQLSink,
    _execute_chunked_sql_id_delete,
    _to_sql_with_chunking,
    export_to_csv,
    export_to_excel,
    export_to_sql,
    export_to_sql_by_form,
)
from imednet.integrations.sink_base import get_state_provider, normalize_watermark_datetime
from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet.utils.tabular import MASKED_SENTINEL
from imednet.validation.cache import SchemaCache, calculate_readiness_score, validate_record_data


def _make_streaming_sdk(
    *,
    forms: list[Form],
    variables_ref: list[Variable],
    records_ref: list[Record],
    on_yield_record: Any = None,
) -> MagicMock:
    """Create a streaming SDK mock supporting dynamic variable/record updates and watermarks."""
    sdk = MagicMock()
    intervals = [
        Interval(
            interval_id=1,
            interval_name="Screening",
            interval_sequence=1,
            forms=[
                FormSummary(form_id=f.form_id, form_key=f.form_key, form_name=f.form_name)
                for f in forms
            ],
        )
    ]
    sdk.forms.list.side_effect = lambda **_kw: list(forms)
    sdk.get_forms.side_effect = lambda **_kw: list(forms)
    sdk.intervals.list.side_effect = lambda **_kw: list(intervals)
    sdk.get_intervals.side_effect = lambda **_kw: list(intervals)
    sdk.sites.list.side_effect = lambda **_kw: []
    sdk.get_sites.side_effect = lambda **_kw: []

    def _list_vars(**kw: Any) -> list[Variable]:
        form_id_filter = kw.get("formId")
        if form_id_filter is None:
            return list(variables_ref)
        allowed = (
            set(form_id_filter)
            if isinstance(form_id_filter, (list, tuple, set))
            else {form_id_filter}
        )
        return [v for v in variables_ref if v.form_id in allowed]

    sdk.variables.list.side_effect = _list_vars
    sdk.get_variables.side_effect = _list_vars

    def _iter_records(**kw: Any) -> Any:
        from imednet.integrations.sink_base import extract_record_timestamp

        flt = str(kw.get("filter", "") or "")
        items = list(records_ref)
        if kw.get("deleted") is False or "deleted==false" in flt:
            items = [r for r in items if not r.deleted]
        elif kw.get("deleted") is True or "deleted==true" in flt:
            items = [r for r in items if r.deleted]

        form_id = kw.get("formId")
        if form_id is not None:
            allowed_fids = set(form_id) if isinstance(form_id, (list, tuple, set)) else {form_id}
            items = [r for r in items if r.form_id in allowed_fids]

        dm = kw.get("dateModified")
        if isinstance(dm, tuple) and len(dm) == 2:
            op_str, raw_ts = dm
            wm_dt = normalize_watermark_datetime(raw_ts)
            if wm_dt is not None and op_str == ">=":
                items = [
                    r for r in items if (ts := extract_record_timestamp(r)) is None or ts >= wm_dt
                ]

        for idx, rec in enumerate(items):
            if on_yield_record is not None:
                on_yield_record(idx, rec)
            yield rec

    sdk.records.list.side_effect = _iter_records
    sdk.get_records.side_effect = lambda **kw: list(_iter_records(**kw))
    return sdk


# =============================================================================
# 1. Relational schema evolution via ALTER TABLE
# =============================================================================


def test_tabular_sql_sink_and_export_impl_evolve_added_variables_via_alter_table(
    tmp_path: Path,
) -> None:
    """TabularSQLSink.write_batch and _tabular_export_impl add new Variables via ALTER TABLE."""
    db_path = tmp_path / "evolve.sqlite"
    conn_str = f"sqlite:///{db_path}"
    engine = create_engine(conn_str)
    try:
        # 1a. Direct TabularSQLSink non-incremental multi-batch schema evolution
        cfg = TabularSinkConfig(study_key="STUDY_EV", mask_phi=False)
        with TabularSQLSink("direct_tbl", engine, cfg) as sink:
            sink.set_contract_types(
                {"recordId": "int64", "SYSBP": "int64", "WEIGHT": "float64", "ACTIVE": "bool"}
            )
            assert sink.write_batch([{"recordId": 1, "SYSBP": 120}], batch_id="b0") == 1
            assert (
                sink.write_batch(
                    [{"recordId": 2, "SYSBP": 125, "WEIGHT": 71.5, "ACTIVE": True, "NOTE": "ok"}],
                    batch_id="b1",
                )
                == 1
            )

        direct_df = pd.read_sql_query('SELECT * FROM "direct_tbl" ORDER BY "recordId"', engine)
        assert list(direct_df["recordId"]) == [1, 2]
        assert pd.isna(direct_df.loc[0, "WEIGHT"])
        assert direct_df.loc[1, "WEIGHT"] == pytest.approx(71.5)
        assert direct_df.loc[1, "NOTE"] == "ok"

        # 1b. _tabular_export_impl across mid-stream batch evolution AND subsequent incremental run
        forms = [Form(form_id=10, form_key="VITALS", form_name="Vital Signs")]
        variables = [
            Variable(
                variable_id=1,
                variable_name="HR",
                variable_type="integer",
                label="Heart Rate",
                form_id=10,
                form_key="VITALS",
            )
        ]
        run1_records = [
            Record(
                record_id=101,
                study_key="STUDY_EV",
                subject_key="001",
                form_id=10,
                form_key="VITALS",
                date_modified="2026-06-01T10:00:00Z",
                record_data={"HR": 70},
            ),
            Record(
                record_id=102,
                study_key="STUDY_EV",
                subject_key="002",
                form_id=10,
                form_key="VITALS",
                date_modified="2026-06-01T11:00:00Z",
                record_data={"HR": 74, "TEMP_C": 36.8},
            ),
        ]

        def _inject_midstream_var(idx: int, _rec: Record) -> None:
            if idx == 1 and all(v.variable_name != "TEMP_C" for v in variables):
                variables.append(
                    Variable(
                        variable_id=2,
                        variable_name="TEMP_C",
                        variable_type="float",
                        label="Temperature C",
                        form_id=10,
                        form_key="VITALS",
                    )
                )

        provider = get_state_provider(ledger_path=tmp_path / "ledger.json")
        sdk = _make_streaming_sdk(
            forms=forms,
            variables_ref=variables,
            records_ref=run1_records,
            on_yield_record=_inject_midstream_var,
        )
        export_to_sql(
            sdk,
            "STUDY_EV",
            "study_vitals",
            conn_str,
            if_exists="replace",
            state_provider=provider,
            batch_size=1,
            mask_phi=False,
        )

        # Run 2: add another Variable between checkpointed runs and update Record 101
        variables.append(
            Variable(
                variable_id=3,
                variable_name="OXSAT",
                variable_type="integer",
                label="Oxygen Saturation",
                form_id=10,
                form_key="VITALS",
            )
        )
        run1_records.clear()
        run1_records.extend(
            [
                Record(
                    record_id=101,
                    study_key="STUDY_EV",
                    subject_key="001",
                    form_id=10,
                    form_key="VITALS",
                    date_modified="2026-06-02T09:00:00Z",
                    record_data={"HR": 68, "TEMP_C": 36.6, "OXSAT": 99},
                ),
                Record(
                    record_id=103,
                    study_key="STUDY_EV",
                    subject_key="003",
                    form_id=10,
                    form_key="VITALS",
                    date_modified="2026-06-02T10:00:00Z",
                    record_data={"HR": 80, "TEMP_C": 37.1, "OXSAT": 98},
                ),
            ]
        )
        export_to_sql(
            sdk,
            "STUDY_EV",
            "study_vitals",
            conn_str,
            if_exists="replace",
            state_provider=provider,
            batch_size=1,
            mask_phi=False,
        )

        out_df = pd.read_sql_query('SELECT * FROM "study_vitals" ORDER BY "recordId"', engine)
        assert list(out_df["recordId"]) == [101, 102, 103]
        row_101 = out_df[out_df["recordId"] == 101].iloc[0]
        row_102 = out_df[out_df["recordId"] == 102].iloc[0]
        row_103 = out_df[out_df["recordId"] == 103].iloc[0]
        assert row_101["HR"] == 68
        assert row_101["OXSAT"] == 99
        assert row_102["TEMP_C"] == pytest.approx(36.8)
        assert pd.isna(row_102["OXSAT"])
        assert row_103["OXSAT"] == 98
    finally:
        engine.dispose()


# =============================================================================
# 2. Checkpointed multi-Form SQL exports under state_provider + if_exists="replace"
# =============================================================================


def test_export_to_sql_by_form_retains_history_and_evolves_schema_under_state_provider(
    tmp_path: Path,
) -> None:
    """export_to_sql_by_form checks _is_incremental_config(config) and retains historical rows."""
    db_path = tmp_path / "by_form.sqlite"
    conn_str = f"sqlite:///{db_path}"
    ledger = get_state_provider(ledger_path=tmp_path / "by_form_ledger.json")

    forms = [
        Form(form_id=1, form_key="DEMO", form_name="Demographics"),
        Form(form_id=2, form_key="LABS", form_name="Laboratory"),
    ]
    variables = [
        Variable(
            variable_id=11,
            variable_name="COUNTRY",
            variable_type="text",
            label="Country",
            form_id=1,
            form_key="DEMO",
        ),
        Variable(
            variable_id=21,
            variable_name="GLUC",
            variable_type="float",
            label="Glucose",
            form_id=2,
            form_key="LABS",
        ),
    ]
    records = [
        Record(
            record_id=1,
            study_key="STUDY_BF",
            subject_key="001",
            form_id=1,
            form_key="DEMO",
            date_modified="2026-07-01T08:00:00Z",
            record_data={"COUNTRY": "NL"},
        ),
        Record(
            record_id=2,
            study_key="STUDY_BF",
            subject_key="002",
            form_id=1,
            form_key="DEMO",
            date_modified="2026-07-01T08:30:00Z",
            record_data={"COUNTRY": "DE"},
        ),
        Record(
            record_id=10,
            study_key="STUDY_BF",
            subject_key="001",
            form_id=2,
            form_key="LABS",
            date_modified="2026-07-01T09:00:00Z",
            record_data={"GLUC": 5.2},
        ),
    ]
    sdk = _make_streaming_sdk(forms=forms, variables_ref=variables, records_ref=records)

    # Run 1: initial checkpointed sync with explicit if_exists="replace"
    export_to_sql_by_form(
        sdk,
        "STUDY_BF",
        conn_str,
        if_exists="replace",
        state_provider=ledger,
        mask_phi=False,
    )

    # Run 2: add HBA1C variable to LABS, void DEMO record 2, add LABS record 11, keep DEMO record 1
    variables.append(
        Variable(
            variable_id=22,
            variable_name="HBA1C",
            variable_type="float",
            label="HbA1c",
            form_id=2,
            form_key="LABS",
        )
    )
    records.extend(
        [
            Record(
                record_id=2,
                study_key="STUDY_BF",
                subject_key="002",
                form_id=1,
                form_key="DEMO",
                deleted=True,
                date_modified="2026-07-02T10:00:00Z",
                record_data={"COUNTRY": "DE"},
            ),
            Record(
                record_id=11,
                study_key="STUDY_BF",
                subject_key="003",
                form_id=2,
                form_key="LABS",
                date_modified="2026-07-02T11:00:00Z",
                record_data={"GLUC": 6.1, "HBA1C": 5.7},
            ),
        ]
    )

    export_to_sql_by_form(
        sdk,
        "STUDY_BF",
        conn_str,
        if_exists="replace",
        state_provider=ledger,
        mask_phi=False,
    )

    with sqlite3.connect(db_path) as sconn:
        demo_df = pd.read_sql_query('SELECT * FROM "DEMO" ORDER BY "recordId"', sconn)
        labs_df = pd.read_sql_query('SELECT * FROM "LABS" ORDER BY "recordId"', sconn)

    assert list(demo_df["recordId"]) == [1]
    assert demo_df.iloc[0]["COUNTRY"] == "NL"
    assert list(labs_df["recordId"]) == [10, 11]
    assert labs_df.loc[labs_df["recordId"] == 10, "GLUC"].iloc[0] == pytest.approx(5.2)
    assert pd.isna(labs_df.loc[labs_df["recordId"] == 10, "HBA1C"].iloc[0])
    assert labs_df.loc[labs_df["recordId"] == 11, "HBA1C"].iloc[0] == pytest.approx(5.7)


# =============================================================================
# 3. Wide SQLite (>2000 columns) splits, key propagation, updates & deletes
# =============================================================================


def test_wide_sqlite_splits_propagate_record_and_study_keys_and_reconcile_deletes(
    tmp_path: Path,
) -> None:
    """_to_sql_with_chunking and _execute_chunked_sql_id_delete link and prune all _part tables."""
    db_path = tmp_path / "wide_parts.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        # 3a. Legacy unkeyed DataFrame preserves positional slicing without synthetic recordId
        unkeyed_cols = {f"u_{i}": [i] for i in range(MAX_SQLITE_COLUMNS + 5)}
        unkeyed_df = pd.DataFrame(unkeyed_cols)
        _to_sql_with_chunking(unkeyed_df, "legacy_wide", engine, if_exists="replace")

        insp = inspect(engine)
        p1_unkeyed = [c["name"] for c in insp.get_columns("legacy_wide_part1")]
        p2_unkeyed = [c["name"] for c in insp.get_columns("legacy_wide_part2")]
        assert len(p1_unkeyed) == MAX_SQLITE_COLUMNS
        assert len(p2_unkeyed) == 5
        assert "recordId" not in p2_unkeyed

        # 3b. Keyed wide export via TabularSQLSink propagates recordId + studyKey across _part1/_part2
        var_names = [f"V_{i:04d}" for i in range(MAX_SQLITE_COLUMNS + 8)]
        row_1 = {"recordId": 1, **{v: i for i, v in enumerate(var_names)}}
        row_2 = {"recordId": 2, **{v: i + 100 for i, v in enumerate(var_names)}}

        base_cfg = TabularSinkConfig(study_key="STUDY_WIDE", mask_phi=False)
        with TabularSQLSink("wide_records", engine, base_cfg) as sink:
            assert sink.write_batch([row_1, row_2], batch_id="w0") == 2

        # Multi-study record sharing recordId=1 in STUDY_OTHER must remain isolated
        other_cfg = TabularSinkConfig(
            study_key="STUDY_OTHER",
            incremental=True,
            mask_phi=False,
        )
        row_other = {"recordId": 1, **{v: -1 for v in var_names}}
        with TabularSQLSink("wide_records", engine, other_cfg) as sink:
            assert sink.write_batch([row_other], batch_id="w_other") == 1

        insp = inspect(engine)
        part1_cols = [c["name"] for c in insp.get_columns("wide_records_part1")]
        part2_cols = [c["name"] for c in insp.get_columns("wide_records_part2")]
        assert len(part1_cols) <= MAX_SQLITE_COLUMNS
        assert len(part2_cols) <= MAX_SQLITE_COLUMNS
        assert {"recordId", "studyKey"} <= set(part1_cols)
        assert {"recordId", "studyKey"} <= set(part2_cols)

        # Incremental update of Record 1 (updating columns in both _part1 and _part2 + adding V_EXTRA)
        # and voided delete of Record 2 across all _part* tables
        inc_cfg = TabularSinkConfig(
            study_key="STUDY_WIDE",
            incremental=True,
            mask_phi=False,
        )
        updated_row_1 = {
            "recordId": 1,
            **{v: i for i, v in enumerate(var_names)},
            "V_0000": 9999,
            var_names[-1]: 8888,
            "V_ADDED_LATE": "evolved_in_part2",
        }
        with TabularSQLSink("wide_records", engine, inc_cfg) as sink:
            assert sink.write_batch([updated_row_1], batch_id="w_upd") == 1
            assert (
                sink.reconcile_deleted(
                    [Record(record_id=2, study_key="STUDY_WIDE", deleted=True)],
                    batch_id="w_del",
                )
                == 1
            )

        p1_df = pd.read_sql_query(
            'SELECT "studyKey", "recordId", "V_0000" FROM "wide_records_part1" '
            'ORDER BY "studyKey", "recordId"',
            engine,
        )
        p2_df = pd.read_sql_query(
            f'SELECT "studyKey", "recordId", "{var_names[-1]}", "V_ADDED_LATE" '
            f'FROM "wide_records_part2" ORDER BY "studyKey", "recordId"',
            engine,
        )
        assert list(zip(p1_df["studyKey"], p1_df["recordId"], strict=True)) == [
            ("STUDY_OTHER", 1),
            ("STUDY_WIDE", 1),
        ]
        assert list(zip(p2_df["studyKey"], p2_df["recordId"], strict=True)) == [
            ("STUDY_OTHER", 1),
            ("STUDY_WIDE", 1),
        ]
        assert p1_df.loc[p1_df["studyKey"] == "STUDY_WIDE", "V_0000"].iloc[0] == 9999
        assert p2_df.loc[p2_df["studyKey"] == "STUDY_WIDE", var_names[-1]].iloc[0] == 8888
        assert (
            p2_df.loc[p2_df["studyKey"] == "STUDY_WIDE", "V_ADDED_LATE"].iloc[0]
            == "evolved_in_part2"
        )
        assert p1_df.loc[p1_df["studyKey"] == "STUDY_OTHER", "V_0000"].iloc[0] == -1

        # Direct _execute_chunked_sql_id_delete prunes STUDY_OTHER recordId=1 from both parts
        deleted_count = _execute_chunked_sql_id_delete(
            engine,
            "wide_records",
            ("recordId", "record_id", "RECORD_ID"),
            [1],
            study_key="STUDY_OTHER",
        )
        assert deleted_count == 1
        rem_p2 = pd.read_sql_query(
            'SELECT "studyKey", "recordId" FROM "wide_records_part2"', engine
        )
        assert rem_p2.to_dict(orient="records") == [{"studyKey": "STUDY_WIDE", "recordId": 1}]
    finally:
        engine.dispose()


def test_sqlite_append_crossing_column_limit_migrates_to_split_parts(tmp_path: Path) -> None:
    """Adding Variables that push an existing SQLite table past 2000 columns splits cleanly."""
    db_path = tmp_path / "cross_limit.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        initial_vars = [f"C_{i:04d}" for i in range(MAX_SQLITE_COLUMNS - 3)]
        r1 = {"recordId": 10, **{k: 1 for k in initial_vars}}
        r2 = {
            "recordId": 11,
            **{k: 2 for k in initial_vars},
            "NEW_1": "a",
            "NEW_2": "b",
            "NEW_3": "c",
            "NEW_4": "d",
        }
        cfg = TabularSinkConfig(study_key="STUDY_CROSS", incremental=True, mask_phi=False)
        with TabularSQLSink("grow_tbl", engine, cfg) as sink:
            sink.write_batch([r1], batch_id="g0")
        with TabularSQLSink("grow_tbl", engine, cfg) as sink:
            sink.write_batch([r2], batch_id="g1")

        insp = inspect(engine)
        assert not insp.has_table("grow_tbl")
        assert insp.has_table("grow_tbl_part1")
        assert insp.has_table("grow_tbl_part2")
        p2 = pd.read_sql_query(
            'SELECT "recordId", "studyKey", "NEW_4" FROM "grow_tbl_part2" ORDER BY "recordId"',
            engine,
        )
        assert list(p2["recordId"]) == [10, 11]
        assert pd.isna(p2.loc[0, "NEW_4"])
        assert p2.loc[1, "NEW_4"] == "d"
    finally:
        engine.dispose()


# =============================================================================
# 4. Non-incremental and SDTM CSV batch header alignment and atomic expansion
# =============================================================================


def test_tabular_csv_sink_aligns_reordered_and_added_variables_across_batches(
    tmp_path: Path,
) -> None:
    """TabularCSVSink.write_batch aligns out-of-order columns and expands new headers atomically."""
    csv_path = tmp_path / "batched.csv"
    cfg = TabularSinkConfig(study_key="STUDY_CSV", mask_phi=False, idempotent=True)
    with TabularCSVSink(csv_path, cfg) as sink:
        sink.write_batch(
            [{"recordId": 1, "subjectKey": "001", "ALPHA": "a1", "BETA": "b1"}], batch_id="c0"
        )
        # Out-of-order and omitted column
        sink.write_batch([{"BETA": "b2", "recordId": 2, "subjectKey": "002"}], batch_id="c1")
        # Newly added Variables GAMMA and DELTA with shuffled column order
        sink.write_batch(
            [{"GAMMA": "g3", "ALPHA": "a3", "recordId": 3, "subjectKey": "003", "DELTA": "d3"}],
            batch_id="c2",
        )

    # Also append with idempotent=False introducing EPSILON
    append_cfg = TabularSinkConfig(study_key="STUDY_CSV", mask_phi=False, idempotent=False)
    with TabularCSVSink(csv_path, append_cfg) as sink:
        sink.write_batch(
            [{"EPSILON": "e4", "BETA": "b4", "recordId": 4, "subjectKey": "004"}],
            batch_id="c3",
        )

    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    assert list(rows[0].keys()) == [
        "recordId",
        "subjectKey",
        "ALPHA",
        "BETA",
        "GAMMA",
        "DELTA",
        "EPSILON",
    ]
    assert rows == [
        {
            "recordId": "1",
            "subjectKey": "001",
            "ALPHA": "a1",
            "BETA": "b1",
            "GAMMA": "",
            "DELTA": "",
            "EPSILON": "",
        },
        {
            "recordId": "2",
            "subjectKey": "002",
            "ALPHA": "",
            "BETA": "b2",
            "GAMMA": "",
            "DELTA": "",
            "EPSILON": "",
        },
        {
            "recordId": "3",
            "subjectKey": "003",
            "ALPHA": "a3",
            "BETA": "",
            "GAMMA": "g3",
            "DELTA": "d3",
            "EPSILON": "",
        },
        {
            "recordId": "4",
            "subjectKey": "004",
            "ALPHA": "",
            "BETA": "b4",
            "GAMMA": "",
            "DELTA": "",
            "EPSILON": "e4",
        },
    ]


def test_sdtm_csv_batches_align_headers_across_multiple_domains(tmp_path: Path) -> None:
    """Multi-batch SDTM CSV exports unify domain-specific columns without shifting values."""
    sdtm_csv = tmp_path / "sdtm_multi.csv"
    forms = [
        Form(form_id=1, form_key="AE", form_name="Adverse Events"),
        Form(form_id=2, form_key="VS", form_name="Vital Signs"),
    ]
    variables = [
        Variable(
            variable_id=1, variable_name="AETERM", variable_type="text", form_id=1, form_key="AE"
        ),
        Variable(
            variable_id=2, variable_name="VSTESTCD", variable_type="text", form_id=2, form_key="VS"
        ),
        Variable(
            variable_id=3, variable_name="VSORRES", variable_type="text", form_id=2, form_key="VS"
        ),
    ]
    records = [
        Record(
            record_id=1,
            study_key="STUDY_SDTM",
            subject_key="001",
            form_id=1,
            form_key="AE",
            record_data={"AETERM": "Headache"},
        ),
        Record(
            record_id=2,
            study_key="STUDY_SDTM",
            subject_key="002",
            form_id=2,
            form_key="VS",
            record_data={"VSTESTCD": "HR", "VSORRES": "72"},
        ),
    ]
    sdk = _make_streaming_sdk(forms=forms, variables_ref=variables, records_ref=records)
    export_to_csv(
        sdk,
        "STUDY_SDTM",
        sdtm_csv,
        as_sdtm=True,
        batch_size=1,
        mask_phi=True,
    )

    with open(sdtm_csv, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2
    by_dom = {r["DOMAIN"]: r for r in rows}
    assert by_dom["AE"]["AETERM"] == "Headache"
    assert by_dom["AE"]["VSTESTCD"] == ""
    assert by_dom["VS"]["VSTESTCD"] == "HR"
    assert by_dom["VS"]["VSORRES"] == "72"
    assert by_dom["VS"]["AETERM"] == ""


# =============================================================================
# 5. Excel sibling worksheet preservation and leading-zero / literal "NA" text
# =============================================================================


def test_export_to_excel_preserves_sibling_sheets_leading_zero_subject_and_literal_na(
    tmp_path: Path,
) -> None:
    """export_to_excel updates only target sheet while keeping '001', 'NA', PHI mask, and formulas."""
    xlsx_path = tmp_path / "shared_study.xlsx"
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        pd.DataFrame({"metric": ["enrolled"], "count": [42]}).to_excel(
            writer, sheet_name="Overview", index=False
        )
        pd.DataFrame({"siteCode": ["007"], "status": ["NA"]}).to_excel(
            writer, sheet_name="Sites", index=False
        )

    forms = [Form(form_id=10, form_key="DEMO", form_name="Demographics")]
    variables = [
        Variable(
            variable_id=1,
            variable_name="COHORT_NOTE",
            variable_type="text",
            label="Cohort Note",
            form_id=10,
            form_key="DEMO",
        ),
        Variable(
            variable_id=2,
            variable_name="FORMULA_TXT",
            variable_type="text",
            label="Formula Text",
            form_id=10,
            form_key="DEMO",
        ),
        Variable(
            variable_id=3,
            variable_name="patientName",
            variable_type="text",
            label="Patient Name",
            form_id=10,
            form_key="DEMO",
        ),
    ]
    records = [
        Record(
            record_id=1,
            study_key="STUDY_XL",
            subject_key="001",
            form_id=10,
            form_key="DEMO",
            date_modified="2026-08-01T10:00:00Z",
            record_data={
                "COHORT_NOTE": "NA",
                "FORMULA_TXT": "=SUM(1,2)",
                "patientName": "Jane Doe",
            },
        ),
        Record(
            record_id=2,
            study_key="STUDY_XL",
            subject_key="002",
            form_id=10,
            form_key="DEMO",
            date_modified="2026-08-01T11:00:00Z",
            record_data={
                "COHORT_NOTE": None,
                "FORMULA_TXT": "plain",
                "patientName": "John Roe",
            },
        ),
    ]
    ledger = get_state_provider(ledger_path=tmp_path / "xl_ledger.json")
    sdk = _make_streaming_sdk(forms=forms, variables_ref=variables, records_ref=records)

    # Run 1: target sheet "DEMO" does not yet exist in shared_study.xlsx
    export_to_excel(
        sdk,
        "STUDY_XL",
        xlsx_path,
        sheet_name="DEMO",
        state_provider=ledger,
        mask_phi=True,
    )

    # Run 2: add Record 3 ("003") and void Record 2; Record 1 ("001" + "NA") comes from existing sheet
    records.clear()
    records.extend(
        [
            Record(
                record_id=2,
                study_key="STUDY_XL",
                subject_key="002",
                form_id=10,
                form_key="DEMO",
                deleted=True,
                date_modified="2026-08-02T09:00:00Z",
                record_data={},
            ),
            Record(
                record_id=3,
                study_key="STUDY_XL",
                subject_key="003",
                form_id=10,
                form_key="DEMO",
                date_modified="2026-08-02T10:00:00Z",
                record_data={
                    "COHORT_NOTE": "Arm B",
                    "FORMULA_TXT": "@RISK",
                    "patientName": "Sam Smith",
                },
            ),
        ]
    )
    export_to_excel(
        sdk,
        "STUDY_XL",
        xlsx_path,
        sheet_name="DEMO",
        state_provider=ledger,
        mask_phi=True,
    )

    with pd.ExcelFile(xlsx_path) as xls:
        assert xls.sheet_names == ["Overview", "Sites", "DEMO"]
        overview_df = pd.read_excel(xls, sheet_name="Overview")
        sites_df = pd.read_excel(
            xls, sheet_name="Sites", dtype=object, keep_default_na=False, na_values=[""]
        )
        demo_df = pd.read_excel(
            xls, sheet_name="DEMO", dtype=object, keep_default_na=False, na_values=[""]
        )

    assert overview_df.to_dict(orient="records") == [{"metric": "enrolled", "count": 42}]
    assert sites_df.to_dict(orient="records") == [{"siteCode": "007", "status": "NA"}]
    assert list(demo_df["recordId"]) == [1, 3]
    assert list(demo_df["subjectKey"]) == ["001", "003"]
    assert all(isinstance(sk, str) for sk in demo_df["subjectKey"])
    assert demo_df.loc[demo_df["recordId"] == 1, "COHORT_NOTE"].iloc[0] == "NA"
    assert demo_df.loc[demo_df["recordId"] == 1, "FORMULA_TXT"].iloc[0] == "'=SUM(1,2)"
    assert demo_df.loc[demo_df["recordId"] == 3, "FORMULA_TXT"].iloc[0] == "'@RISK"
    assert list(demo_df["patientName"]) == [MASKED_SENTINEL, MASKED_SENTINEL]


# =============================================================================
# 6. Retired Variable caching, readiness scoring, pre-retirement validation & watermarks
# =============================================================================


def test_retired_variables_skip_required_rule_but_validate_historical_values() -> None:
    """deleted=True and disabled=True Variables skip required rules while validating present values."""
    active_var = Variable.model_validate(
        {
            "variableId": 1,
            "variableName": "SYSBP",
            "variableType": "integer",
            "formId": 50,
            "formKey": "VITALS",
            "deleted": False,
            "disabled": False,
            "required": True,
        }
    )
    deleted_req_var = Variable.model_validate(
        {
            "variableId": 2,
            "variableName": "OLD_ARM",
            "variableType": "dropdown",
            "formId": 50,
            "formKey": "VITALS",
            "deleted": True,
            "disabled": False,
            "required": True,
        }
    )
    disabled_req_var = Variable.model_validate(
        {
            "variableId": 3,
            "variableName": "LEGACY_SCORE",
            "variableType": "integer",
            "formId": 50,
            "formKey": "VITALS",
            "deleted": False,
            "disabled": True,
            "required": True,
        }
    )
    retired_only_form_var = Variable.model_validate(
        {
            "variableId": 4,
            "variableName": "RETIRED_FIELD",
            "variableType": "text",
            "formId": 60,
            "formKey": "OLD_FORM",
            "deleted": True,
            "required": True,
        }
    )

    dd_deleted_var = DDVariable(
        variableName="OLD_ARM",
        variableType="Dropdown",
        required="Yes",
        choices=[
            Choice(choiceText="Arm A", choiceValue="A"),
            Choice(choiceText="Arm B", choiceValue="B"),
        ],
        extraAttributes={"deleted": True},
    )
    dd_disabled_var = DDVariable(
        variableName="LEGACY_SCORE",
        variableType="Integer",
        required="Yes",
        extraAttributes={"disabled": True, "minValue": 0, "maxValue": 10},
    )

    # extract_variable_constraints marks retired Variables non-required while keeping value bounds
    assert extract_variable_constraints(active_var).required_policy == "yes"
    assert extract_variable_constraints(deleted_req_var).required_policy == "no"
    assert extract_variable_constraints(disabled_req_var).required_policy == "no"

    c_dd_deleted = extract_variable_constraints(dd_deleted_var)
    c_dd_disabled = extract_variable_constraints(dd_disabled_var)
    assert c_dd_deleted.required_policy == "no"
    assert c_dd_deleted.choices == ["A", "B"]
    assert c_dd_disabled.required_policy == "no"
    assert (c_dd_disabled.min_value, c_dd_disabled.max_value) == (0.0, 10.0)

    cache = SchemaCache()
    cache.populate(
        [
            active_var,
            deleted_req_var,
            disabled_req_var,
            retired_only_form_var,
        ],
        study_key="STUDY_RET",
    )

    # form_key_from_id resolution preserved even for retired-only forms
    assert cache.form_key_from_id(50) == "VITALS"
    assert cache.form_key_from_id(60) == "OLD_FORM"
    assert set(cache.variables_for_form("VITALS")) == {"SYSBP"}

    # Valid Record omitting retired required=True Variables scores 100.0 in both SchemaCache & DataDictionary
    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="VITALS",
                formName="Vitals",
                formId=50,
                formType="Subject",
                variables=[
                    DDVariable(
                        variableName="SYSBP",
                        variableType="Integer",
                        required="Yes",
                        extraAttributes={"minValue": 60, "maxValue": 240},
                    ),
                    dd_deleted_var,
                    dd_disabled_var,
                ],
            )
        ]
    )
    assert calculate_readiness_score(cache, "VITALS", {"SYSBP": 120}) == (100.0, [])
    assert calculate_readiness_score(dd, "VITALS", {"SYSBP": 120}) == (100.0, [])
    assert calculate_readiness_score(cache, "OLD_FORM", {}) == (100.0, [])
    validate_record_data(cache, "VITALS", {"SYSBP": 120})
    validate_record_data(dd, "VITALS", {"SYSBP": 120})

    # Valid historical Record with pre-retirement values passes and scores 100.0
    historical_valid = {"SYSBP": 118, "OLD_ARM": "A", "LEGACY_SCORE": 7}
    validate_record_data(cache, "VITALS", historical_valid)
    validate_record_data(dd, "VITALS", historical_valid)
    assert calculate_readiness_score(cache, "VITALS", historical_valid) == (100.0, [])
    assert calculate_readiness_score(dd, "VITALS", historical_valid) == (100.0, [])

    # Invalid pre-retirement type on REST SchemaCache STILL fails validation and reduces score
    historical_bad_type = {"SYSBP": 118, "LEGACY_SCORE": "not_an_int"}
    with pytest.raises(ValidationError, match="Value must be an integer"):
        validate_record_data(cache, "VITALS", historical_bad_type)
    bad_type_score, bad_type_issues = calculate_readiness_score(
        cache, "VITALS", historical_bad_type
    )
    assert bad_type_score < 100.0
    assert any("LEGACY_SCORE" in issue for issue in bad_type_issues)

    # Invalid pre-retirement choice/range on DataDictionary STILL fails validation and reduces score
    historical_bad_choice = {"SYSBP": 118, "OLD_ARM": "INVALID_CHOICE", "LEGACY_SCORE": 7}
    with pytest.raises(ValidationError, match="OLD_ARM"):
        validate_record_data(dd, "VITALS", historical_bad_choice)
    bad_choice_score, bad_choice_issues = calculate_readiness_score(
        dd, "VITALS", historical_bad_choice
    )
    assert bad_choice_score < 100.0
    assert any("OLD_ARM" in issue for issue in bad_choice_issues)

    historical_bad_range = {"SYSBP": 118, "LEGACY_SCORE": 99}
    with pytest.raises(ValidationError, match="LEGACY_SCORE"):
        validate_record_data(dd, "VITALS", historical_bad_range)
    bad_range_score, bad_range_issues = calculate_readiness_score(
        dd, "VITALS", historical_bad_range
    )
    assert bad_range_score < 100.0
    assert any("LEGACY_SCORE" in issue for issue in bad_range_issues)


def test_quality_gate_advances_watermark_for_retired_required_vars_and_caps_active_missing(
    tmp_path: Path,
) -> None:
    """Opt-in quality gate advances watermarks past retired required Variables but caps active misses."""
    db_path = tmp_path / "qg_retired.sqlite"
    conn_str = f"sqlite:///{db_path}"
    ledger = get_state_provider(ledger_path=tmp_path / "qg_ledger.json")

    forms = [Form(form_id=10, form_key="VITALS", form_name="Vitals")]
    variables = [
        Variable.model_validate(
            {
                "variableId": 1,
                "variableName": "HR",
                "variableType": "integer",
                "formId": 10,
                "formKey": "VITALS",
                "deleted": False,
                "disabled": False,
                "required": True,
            }
        ),
        Variable.model_validate(
            {
                "variableId": 2,
                "variableName": "RETIRED_DEL",
                "variableType": "text",
                "formId": 10,
                "formKey": "VITALS",
                "deleted": True,
                "disabled": False,
                "required": True,
            }
        ),
        Variable.model_validate(
            {
                "variableId": 3,
                "variableName": "RETIRED_DIS",
                "variableType": "text",
                "formId": 10,
                "formKey": "VITALS",
                "deleted": False,
                "disabled": True,
                "required": True,
            }
        ),
    ]
    records = [
        Record(
            record_id=1,
            study_key="STUDY_QG",
            subject_key="001",
            form_id=10,
            form_key="VITALS",
            date_modified="2026-09-01T10:00:00Z",
            record_data={"HR": 72},
        ),
        Record(
            record_id=2,
            study_key="STUDY_QG",
            subject_key="002",
            form_id=10,
            form_key="VITALS",
            date_modified="2026-09-01T12:00:00Z",
            record_data={"HR": 76},
        ),
    ]
    sdk = _make_streaming_sdk(forms=forms, variables_ref=variables, records_ref=records)

    # Run 1: both records omit retired required=True Variables -> accepted and watermark advances to 12:00Z
    export_to_sql(
        sdk,
        "STUDY_QG",
        "vitals_tbl",
        conn_str,
        quality_gate_enabled=True,
        min_schema_readiness_score=100.0,
        state_provider=ledger,
        mask_phi=False,
    )
    wm_after_run1 = ledger.get_last_timestamp("STUDY_QG", "sql_vitals_tbl")
    assert wm_after_run1 == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    # Run 2: Record 3 at 13:00Z omits ACTIVE required HR; Record 4 at 14:00Z is valid ->
    # Record 3 is dropped and watermark is held back at or before 13:00Z (remains 12:00Z)
    records.extend(
        [
            Record(
                record_id=3,
                study_key="STUDY_QG",
                subject_key="003",
                form_id=10,
                form_key="VITALS",
                date_modified="2026-09-01T13:00:00Z",
                record_data={},
            ),
            Record(
                record_id=4,
                study_key="STUDY_QG",
                subject_key="004",
                form_id=10,
                form_key="VITALS",
                date_modified="2026-09-01T14:00:00Z",
                record_data={"HR": 80},
            ),
        ]
    )
    export_to_sql(
        sdk,
        "STUDY_QG",
        "vitals_tbl",
        conn_str,
        quality_gate_enabled=True,
        min_schema_readiness_score=100.0,
        state_provider=ledger,
        mask_phi=False,
    )
    wm_after_run2 = ledger.get_last_timestamp("STUDY_QG", "sql_vitals_tbl")
    assert wm_after_run2 == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    with sqlite3.connect(db_path) as sconn:
        exported_df = pd.read_sql_query(
            'SELECT "recordId", "HR" FROM "vitals_tbl" ORDER BY "recordId"', sconn
        )
    assert list(exported_df["recordId"]) == [1, 2, 4]
