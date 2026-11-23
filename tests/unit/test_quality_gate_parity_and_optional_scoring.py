"""Tests for unified opt-in quality gates, optional Variable readiness scoring, and incremental watermarks."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from imednet import cli
from imednet.datadict import DataDictionary
from imednet.datadict import Form as DDForm
from imednet.datadict import Variable as DDVariable
from imednet.integrations import export as export_mod
from imednet.integrations.parquet import export_to_hive_parquet
from imednet.integrations.sink_base import SinkConfig, apply_quality_gate, run_streaming_export
from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet.spi import sink_base as spi_sink_base
from imednet.spi import sinks as spi_sinks
from imednet.spi import validation as spi_validation
from imednet.utils.tabular import MASKED_SENTINEL
from imednet.validation.cache import (
    AsyncSchemaCache,
    SchemaCache,
    calculate_readiness_score,
    validate_record_data,
)
from imednet_workflows.state_ledger import FileStateProvider, StreamState


class _CliResult:
    def __init__(self, exit_code: int, stdout: str, stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class CliRunner:
    def invoke(self, app: Any, args: list[str]) -> _CliResult:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out = io.StringIO()
        err = io.StringIO()
        exit_code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                app(args)
        except SystemExit as exc:
            exit_code = int(exc.code) if isinstance(exc.code, int) else 0
        return _CliResult(exit_code, out.getvalue(), err.getvalue())


@pytest.fixture(autouse=True)
def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IMEDNET_API_KEY", "test-key")  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test-secret")  # pragma: allowlist secret
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg_cache"))


def _build_sparse_study_fixture(
    study_key: str = "STUDY_SPARSE",
) -> tuple[MagicMock, list[Form], list[Variable], dict[int, Record], list[dict[str, Any]]]:
    """Build an SDK fixture with 10 valid sparse Study Records (4 omitting optional Variables) + 1 deleted."""
    forms = [
        Form(
            form_id=10,
            form_key="F_DM",
            form_name="Demographics",
            form_type="Enrollment",
            study_key=study_key,
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
        ),
        Form(
            form_id=20,
            form_key="F_VS",
            form_name="Vital Signs",
            form_type="Standard",
            study_key=study_key,
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
        ),
    ]
    variables = [
        Variable(
            variable_id=1,
            variable_name="SUBJ_AGE",
            variable_type="int",
            form_id=10,
            form_key="F_DM",
            study_key=study_key,
            label="Subject Age",
            required=True,
        ),
        Variable(
            variable_id=2,
            variable_name="PATIENT_NAME",
            variable_type="text",
            form_id=10,
            form_key="F_DM",
            study_key=study_key,
            label="Patient Name",
            required=None,  # REST /variables omits required -> required_policy == 'no'
        ),
        Variable(
            variable_id=3,
            variable_name="ETHNIC_NOTE",
            variable_type="text",
            form_id=10,
            form_key="F_DM",
            study_key=study_key,
            label="Ethnic Note",
            required=False,
        ),
        Variable(
            variable_id=4,
            variable_name="SYSBP",
            variable_type="float",
            form_id=20,
            form_key="F_VS",
            study_key=study_key,
            label="Systolic BP",
            required="Yes",
        ),
        Variable(
            variable_id=5,
            variable_name="VS_COMMENT",
            variable_type="text",
            form_id=20,
            form_key="F_VS",
            study_key=study_key,
            label="Vital Signs Comment",
            required="No",
        ),
    ]
    intervals = [
        Interval(
            interval_id=1,
            interval_name="Baseline",
            interval_sequence=1,
            interval_description="Baseline Visit",
            interval_group_name="Core",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
            forms=[
                FormSummary(form_id=10, form_key="F_DM", form_name="Demographics"),
                FormSummary(form_id=20, form_key="F_VS", form_name="Vital Signs"),
            ],
        )
    ]

    # Records 1..4 omit optional Variables (ETHNIC_NOTE / VS_COMMENT) — earliest timestamps!
    records_by_id: dict[int, Record] = {}
    for rid in range(1, 6):
        dm_data: dict[str, Any] = {"SUBJ_AGE": 30 + rid, "PATIENT_NAME": f"Subject {rid}"}
        if rid >= 3:
            dm_data["ETHNIC_NOTE"] = "Provided"
        records_by_id[rid] = Record(
            record_id=rid,
            study_key=study_key,
            subject_id=100 + rid,
            subject_key=f"SUBJ-{rid:02d}",
            visit_id=1,
            interval_id=1,
            form_id=10,
            form_key="F_DM",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 1, 8, rid, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, 9, rid, tzinfo=UTC),
            record_data=dm_data,
        )
    for idx, rid in enumerate(range(6, 11), start=1):
        vs_data: dict[str, Any] = {"SYSBP": 115.0 + idx}
        if idx >= 3:
            vs_data["VS_COMMENT"] = "Measured seated"
        records_by_id[rid] = Record(
            record_id=rid,
            study_key=study_key,
            subject_id=100 + idx,
            subject_key=f"SUBJ-{idx:02d}",
            visit_id=1,
            interval_id=1,
            form_id=20,
            form_key="F_VS",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 1, 8, 10 + idx, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, 9, 10 + idx, tzinfo=UTC),
            record_data=vs_data,
        )
    # Record 11 is soft-deleted and excluded by default (include_deleted=False)
    records_by_id[11] = Record(
        record_id=11,
        study_key=study_key,
        subject_id=199,
        subject_key="SUBJ-99",
        visit_id=1,
        interval_id=1,
        form_id=20,
        form_key="F_VS",
        record_status="Deleted",
        deleted=True,
        date_created=datetime(2026, 6, 1, 8, 30, tzinfo=UTC),
        date_modified=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
        record_data={"SYSBP": 120.0},
    )

    list_calls: list[dict[str, Any]] = []

    def _query_records(*args: Any, **kw: Any) -> Any:
        from imednet.integrations.sink_base import (
            extract_record_timestamp,
            normalize_watermark_datetime,
        )

        call_info = {"args": args, **kw}
        list_calls.append(call_info)
        flt = str(kw.get("filter", "") or "")
        items = list(records_by_id.values())
        fk_arg = kw.get("form_key") or kw.get("formKey")
        if fk_arg:
            items = [r for r in items if r.form_key == fk_arg]
        elif "formKey==" in flt:
            fk_token = [p.split("==", 1)[1] for p in flt.split(";") if p.startswith("formKey==")][0]
            items = [r for r in items if r.form_key == fk_token]
        if kw.get("deleted") is False or "deleted==false" in flt:
            items = [r for r in items if not r.deleted]
        dm = kw.get("dateModified")
        if isinstance(dm, tuple) and len(dm) == 2 and dm[0] == ">=":
            wm_dt = normalize_watermark_datetime(str(dm[1]))
            if wm_dt is not None:
                items = [
                    r
                    for r in items
                    if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                ]
        elif "dateModified>=" in flt:
            wm_part = [p for p in flt.split(";") if p.startswith("dateModified>=")][0]
            wm_dt = normalize_watermark_datetime(wm_part.split(">=", 1)[1].strip("\"'"))
            if wm_dt is not None:
                items = [
                    r
                    for r in items
                    if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                ]
        return iter([r.model_copy(deep=True) for r in items])

    sdk = MagicMock(sinks=None)
    sdk.get_variables.return_value = variables
    sdk.variables.list.return_value = variables
    sdk.get_forms.return_value = forms
    sdk.forms.list.return_value = forms
    sdk.get_intervals.return_value = intervals
    sdk.intervals.list.return_value = intervals
    sdk.records.list.side_effect = _query_records
    sdk.get_records.side_effect = _query_records
    sdk.studies.get_configuration = MagicMock(side_effect=AttributeError)
    return sdk, forms, variables, records_by_id, list_calls


def test_readiness_score_optional_required_auto_query_and_empty_records() -> None:
    """Verify optional, required, Auto Query, and empty Record scoring across SchemaCache, AsyncSchemaCache, and DataDictionary."""
    variables = [
        Variable(
            variable_name="REQ_BOOL",
            variable_type="int",
            form_id=1,
            form_key="F_CLIN",
            required=True,
        ),
        Variable(
            variable_name="REQ_STR",
            variable_type="text",
            form_id=1,
            form_key="F_CLIN",
            required="Yes",
        ),
        Variable(
            variable_name="OPT_NONE",
            variable_type="text",
            form_id=1,
            form_key="F_CLIN",
            required=None,
        ),
        Variable(
            variable_name="OPT_FALSE",
            variable_type="int",
            form_id=1,
            form_key="F_CLIN",
            required=False,
        ),
        Variable(
            variable_name="OPT_NO",
            variable_type="text",
            form_id=1,
            form_key="F_CLIN",
            required="No",
        ),
    ]

    sync_cache = SchemaCache()
    sync_cache.populate(variables, study_key="S_OPT")
    async_cache = AsyncSchemaCache()
    async_cache.populate(variables, study_key="S_OPT")

    dd = DataDictionary(
        forms=[
            DDForm(
                formKey="F_CLIN",
                formName="Clinical",
                formId=1,
                formType="Standard",
                variables=[
                    DDVariable(
                        variableName="REQ_BOOL",
                        variableType="Integer",
                        required="Yes",
                    ),
                    DDVariable(
                        variableName="REQ_STR",
                        variableType="Text",
                        required="Yes",
                    ),
                    DDVariable(
                        variableName="OPT_NONE",
                        variableType="Text",
                        required=None,
                    ),
                    DDVariable(
                        variableName="OPT_FALSE",
                        variableType="Integer",
                        required="No",
                    ),
                    DDVariable(
                        variableName="OPT_NO",
                        variableType="Text",
                        required="No",
                    ),
                ],
            )
        ],
    )

    sparse_valid = {"REQ_BOOL": 0, "REQ_STR": "OK"}
    for schema in (sync_cache, async_cache, dd):
        kw = {"study_key": "S_OPT"} if isinstance(schema, (SchemaCache, AsyncSchemaCache)) else {}
        score, reasons = calculate_readiness_score(schema, "F_CLIN", sparse_valid, **kw)
        assert score == pytest.approx(100.0)
        assert reasons == []
        # Matches validate_record_data acceptance
        validate_record_data(schema, "F_CLIN", sparse_valid, **kw)

    # Missing required=True or required="Yes" penalizes score below 100.0 with failure reasons
    missing_req = {"REQ_BOOL": 42}
    for schema in (sync_cache, async_cache, dd):
        kw = {"study_key": "S_OPT"} if isinstance(schema, (SchemaCache, AsyncSchemaCache)) else {}
        score, reasons = calculate_readiness_score(schema, "F_CLIN", missing_req, **kw)
        assert score == pytest.approx(80.0)
        assert score < 100.0
        assert any("REQ_STR" in r and "Missing required variable" in r for r in reasons)

    # Missing "Auto Query" warns without failing readiness score when clinical data is present
    aq_vars = [
        Variable(
            variable_name="AGE",
            variable_type="int",
            form_id=2,
            form_key="F_AQ",
            required=True,
        ),
        Variable(
            variable_name="HR",
            variable_type="int",
            form_id=2,
            form_key="F_AQ",
            required="Auto Query",
        ),
    ]
    aq_cache = SchemaCache()
    aq_cache.populate(aq_vars, study_key="S_AQ")
    aq_score, aq_reasons = calculate_readiness_score(
        aq_cache, "F_AQ", {"AGE": 55}, study_key="S_AQ"
    )
    assert aq_score == pytest.approx(100.0)
    assert aq_reasons == ["Auto Query warning for variable HR: variable is missing"]

    # Empty {} Record on an all-optional Form scores 0.0 (and all-blank optional values score 0.0)
    opt_only_cache = SchemaCache()
    opt_only_cache.populate(
        [
            Variable(variable_name="NOTE_A", variable_type="text", form_id=3, form_key="F_OPT"),
            Variable(
                variable_name="NOTE_B",
                variable_type="text",
                form_id=3,
                form_key="F_OPT",
                required=False,
            ),
        ],
        study_key="S_OPT_ONLY",
    )
    empty_score, empty_reasons = calculate_readiness_score(
        opt_only_cache, "F_OPT", {}, study_key="S_OPT_ONLY"
    )
    assert empty_score == pytest.approx(0.0)
    assert empty_reasons == ["Empty record data"]

    blank_score, _ = calculate_readiness_score(
        opt_only_cache, "F_OPT", {"NOTE_A": "", "NOTE_B": None}, study_key="S_OPT_ONLY"
    )
    assert blank_score == pytest.approx(0.0)

    # SPI re-exports remain intact
    assert spi_validation.calculate_readiness_score is calculate_readiness_score
    assert spi_sink_base.apply_quality_gate is apply_quality_gate
    assert spi_sinks.apply_quality_gate is apply_quality_gate


def test_apply_quality_gate_drops_empty_draft_at_50_and_retains_sparse_optional_records() -> None:
    """Running apply_quality_gate with min_schema_readiness_score=50.0 drops empty {} drafts while keeping sparse Records."""
    variables = [
        Variable(variable_name="AGE", variable_type="int", form_id=10, form_key="F_DM"),
        Variable(
            variable_name="MIDDLE_INITIAL",
            variable_type="text",
            form_id=10,
            form_key="F_DM",
            required=False,
        ),
        Variable(
            variable_name="COMMENT",
            variable_type="text",
            form_id=10,
            form_key="F_DM",
            required="No",
        ),
    ]
    sdk = MagicMock()
    sdk.get_variables.return_value = variables
    sdk.variables.list.return_value = variables

    sparse_rec = Record(record_id=1, form_id=10, form_key="F_DM", record_data={"AGE": 47})
    empty_rec = Record(record_id=2, form_id=10, form_key="F_DM", record_data={})
    corrupt_rec = {"recordId": 3, "formKey": "F_DM", "recordData": "not-a-dict"}

    cfg = SinkConfig(
        study_key="S_GATE",
        quality_gate_enabled=True,
        min_schema_readiness_score=50.0,
    )
    retained = list(apply_quality_gate(sdk, "S_GATE", [sparse_rec, empty_rec, corrupt_rec], cfg))
    assert retained == [sparse_rec]


def test_all_14_export_paths_retain_identical_record_counts_and_mask_phi_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify all 14 SDK, CLI, Airflow, and sink export paths retain all 10 non-deleted Study Records under default settings."""
    pytest.importorskip("pyarrow")
    pytest.importorskip("openpyxl")
    duckdb = pytest.importorskip("duckdb")

    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    sdk, _forms, _variables, records_by_id, _calls = _build_sparse_study_fixture("STUDY_14")
    # Inject an empty draft Record #10 so default (quality_gate_enabled=False) MUST still retain all 10 non-deleted Records!
    records_by_id[10].record_data = {}

    expected_ids = list(range(1, 11))

    # 1. SDK export_to_csv
    p_csv = tmp_path / "sdk.csv"
    export_mod.export_to_csv(sdk, "STUDY_14", str(p_csv))
    df_csv = pd.read_csv(p_csv)
    assert sorted(df_csv["recordId"].tolist()) == expected_ids
    assert set(df_csv["PATIENT_NAME"].dropna()) == {MASKED_SENTINEL}

    # 2. SDK export_to_parquet
    p_pq = tmp_path / "sdk.parquet"
    export_mod.export_to_parquet(sdk, "STUDY_14", str(p_pq))
    df_pq = pd.read_parquet(p_pq)
    assert sorted(df_pq["recordId"].tolist()) == expected_ids
    assert set(df_pq["PATIENT_NAME"].dropna()) == {MASKED_SENTINEL}

    # 3. SDK export_to_hive_parquet
    p_hive = tmp_path / "sdk_hive"
    export_to_hive_parquet(sdk, "STUDY_14", str(p_hive))
    hive_ids = sorted(
        int(x)
        for pf in sorted(p_hive.glob("**/*.parquet"))
        for x in pd.read_parquet(pf)["recordId"].tolist()
    )
    assert hive_ids == expected_ids

    # 4. SDK export_to_excel
    p_xl = tmp_path / "sdk.xlsx"
    export_mod.export_to_excel(sdk, "STUDY_14", str(p_xl))
    assert sorted(pd.read_excel(p_xl)["recordId"].tolist()) == expected_ids

    # 5. SDK export_to_json (flat)
    p_json = tmp_path / "sdk.json"
    export_mod.export_to_json(sdk, "STUDY_14", str(p_json))
    json_rows = json.loads(p_json.read_text(encoding="utf-8"))
    assert sorted(r["recordId"] for r in json_rows) == expected_ids

    # 6. SDK export_to_json (hierarchical=True)
    p_hier = tmp_path / "sdk_hier.json"
    export_mod.export_to_json(sdk, "STUDY_14", str(p_hier), hierarchical=True)
    hier_data = json.loads(p_hier.read_text(encoding="utf-8"))
    hier_ids = sorted(
        rec["recordId"]
        for subj in hier_data
        for visit in subj["visits"]
        for form in visit["forms"]
        for rec in form["records"]
    )
    assert hier_ids == expected_ids

    # 7. SDK export_to_duckdb
    p_ddb = tmp_path / "sdk.duckdb"
    export_mod.export_to_duckdb(sdk, "STUDY_14", str(p_ddb), "records")
    with duckdb.connect(str(p_ddb), read_only=True, config={"threads": "1"}) as dcon:
        assert [
            r[0]
            for r in dcon.execute('SELECT "recordId" FROM "records" ORDER BY "recordId"').fetchall()
        ] == expected_ids

    # 8. SDK export_to_duckdb_by_form
    p_ddb_form = tmp_path / "sdk_by_form.duckdb"
    export_mod.export_to_duckdb_by_form(sdk, "STUDY_14", str(p_ddb_form))
    with duckdb.connect(str(p_ddb_form), read_only=True, config={"threads": "1"}) as dcon:
        ddb_form_ids = sorted(
            [r[0] for r in dcon.execute('SELECT "recordId" FROM "F_DM"').fetchall()]
            + [r[0] for r in dcon.execute('SELECT "recordId" FROM "F_VS"').fetchall()]
        )
    assert ddb_form_ids == expected_ids

    # 9. SDK export_to_sql
    p_sql = tmp_path / "sdk_single.sqlite"
    export_mod.export_to_sql(sdk, "STUDY_14", "records", f"sqlite:///{p_sql}")
    with sqlite3.connect(p_sql) as scon:
        assert [
            r[0]
            for r in scon.execute('SELECT "recordId" FROM "records" ORDER BY "recordId"').fetchall()
        ] == expected_ids

    # 10. SDK export_to_sql_by_form
    p_sql_form = tmp_path / "sdk_by_form.sqlite"
    export_mod.export_to_sql_by_form(sdk, "STUDY_14", f"sqlite:///{p_sql_form}")
    with sqlite3.connect(p_sql_form) as scon:
        sql_form_ids = sorted(
            [r[0] for r in scon.execute('SELECT "recordId" FROM "F_DM"').fetchall()]
            + [r[0] for r in scon.execute('SELECT "recordId" FROM "F_VS"').fetchall()]
        )
    assert sql_form_ids == expected_ids

    # 11. SDK export_to_long_sql (retains all 9 non-empty Records' EAV values; restore Record 10 value to verify 10-Record parity)
    records_by_id[10].record_data = {"SYSBP": 122.0}
    p_long = tmp_path / "sdk_long.sqlite"
    export_mod.export_to_long_sql(sdk, "STUDY_14", "long_records", f"sqlite:///{p_long}")
    with sqlite3.connect(p_long) as scon:
        long_ids = sorted(
            r[0] for r in scon.execute('SELECT DISTINCT "record_id" FROM "long_records"').fetchall()
        )
    assert long_ids == expected_ids

    # Re-set Record 10 to empty {} to verify CLI & Airflow also retain empty drafts by default (except EAV long-format which pivots non-empty cells)
    records_by_id[10].record_data = {}

    # 12. CLI `imednet export csv` and `imednet export sql` (SQLite by-form, --single-table, and --long-format)
    import imednet.cli.utils.context as cli_ctx

    monkeypatch.setattr(cli_ctx, "get_sdk", lambda *_a, **_k: sdk)
    runner = CliRunner()

    cli_csv = tmp_path / "cli.csv"
    res_csv = runner.invoke(cli.app, ["export", "csv", "STUDY_14", str(cli_csv)])
    assert res_csv.exit_code == 0, res_csv.stdout
    assert sorted(pd.read_csv(cli_csv)["recordId"].tolist()) == expected_ids

    cli_sql_by_form = tmp_path / "cli_by_form.sqlite"
    res_sql_bf = runner.invoke(
        cli.app, ["export", "sql", "STUDY_14", "records", f"sqlite:///{cli_sql_by_form}"]
    )
    assert res_sql_bf.exit_code == 0, res_sql_bf.stdout
    with sqlite3.connect(cli_sql_by_form) as scon:
        cli_bf_ids = sorted(
            [r[0] for r in scon.execute('SELECT "recordId" FROM "F_DM"').fetchall()]
            + [r[0] for r in scon.execute('SELECT "recordId" FROM "F_VS"').fetchall()]
        )
    assert cli_bf_ids == expected_ids

    cli_sql_single = tmp_path / "cli_single.sqlite"
    res_sql_st = runner.invoke(
        cli.app,
        [
            "export",
            "sql",
            "STUDY_14",
            "records",
            f"sqlite:///{cli_sql_single}",
            "--single-table",
        ],
    )
    assert res_sql_st.exit_code == 0, res_sql_st.stdout
    with sqlite3.connect(cli_sql_single) as scon:
        assert [
            r[0]
            for r in scon.execute('SELECT "recordId" FROM "records" ORDER BY "recordId"').fetchall()
        ] == expected_ids

    records_by_id[10].record_data = {"SYSBP": 122.0}
    cli_sql_long = tmp_path / "cli_long.sqlite"
    res_sql_lg = runner.invoke(
        cli.app,
        [
            "export",
            "sql",
            "STUDY_14",
            "long_records",
            f"sqlite:///{cli_sql_long}",
            "--long-format",
        ],
    )
    assert res_sql_lg.exit_code == 0, res_sql_lg.stdout
    with sqlite3.connect(cli_sql_long) as scon:
        assert (
            sorted(
                r[0]
                for r in scon.execute('SELECT DISTINCT "record_id" FROM "long_records"').fetchall()
            )
            == expected_ids
        )

    # 13. Airflow ImednetExportOperator (CSV, SQL, sql_by_form, long_sql) + deterministic _build_tabular_kwargs
    records_by_id[10].record_data = {}
    af_csv = tmp_path / "af.csv"
    op_csv = ImednetExportOperator(
        task_id="af_csv_default",
        study_key="STUDY_14",
        destination="csv",
        output_path=str(af_csv),
    )
    assert op_csv._build_tabular_kwargs({}, dest="csv") == {"quality_gate_enabled": False}
    monkeypatch.setattr(op_csv, "_get_sdk", lambda: sdk)
    op_csv.execute({})
    assert sorted(pd.read_csv(af_csv)["recordId"].tolist()) == expected_ids

    af_sql = tmp_path / "af_single.sqlite"
    monkeypatch.setattr(
        ImednetHook,
        "resolve_destination_connection",
        classmethod(lambda cls, cid, dest: {"conn_str": f"sqlite:///{af_sql}"}),
    )
    op_sql = ImednetExportOperator(
        task_id="af_sql_default",
        study_key="STUDY_14",
        destination="sql",
        output_path="records",
        destination_conn_id="sqlite_dest",
    )
    monkeypatch.setattr(op_sql, "_get_sdk", lambda: sdk)
    op_sql.execute({})
    with sqlite3.connect(af_sql) as scon:
        assert [
            r[0]
            for r in scon.execute('SELECT "recordId" FROM "records" ORDER BY "recordId"').fetchall()
        ] == expected_ids

    # 14. Sink streaming export (default SinkConfig quality_gate_enabled=False)
    captured_sink_ids: list[int] = []

    class _CollectingSink:
        def __enter__(self) -> _CollectingSink:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def write_batch(self, records: list[Any], *, batch_id: str) -> int:
            _ = batch_id
            captured_sink_ids.extend(int(r.record_id) for r in records)
            return len(records)

    run_streaming_export(
        sdk,
        "STUDY_14",
        _CollectingSink(),
        SinkConfig(study_key="STUDY_14"),
        stream_name="sink_default",
        quality_gate_fn=apply_quality_gate,
    )
    assert sorted(captured_sink_ids) == expected_ids


def test_opt_in_quality_gate_retains_sparse_records_and_drops_empty_drafts_across_cli_sdk_airflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When quality_gate_enabled=True / --quality-gate is enabled, sparse Records pass while empty {} drafts drop."""
    import imednet.cli.utils.context as cli_ctx
    from apache_airflow_providers_imednet import export as af_export
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    sdk, _forms, _variables, records_by_id, _calls = _build_sparse_study_fixture("STUDY_OPTION")
    # Make Record 10 an empty {} draft; Records 1..9 are valid (with 1..2 and 6..7 omitting optional Variables)
    records_by_id[10].record_data = {}
    expected_valid_ids = list(range(1, 10))

    # SDK CSV & SQL with quality_gate_enabled=True
    gated_csv = tmp_path / "gated.csv"
    export_mod.export_to_csv(
        sdk,
        "STUDY_OPTION",
        str(gated_csv),
        quality_gate_enabled=True,
        min_schema_readiness_score=50.0,
    )
    assert sorted(pd.read_csv(gated_csv)["recordId"].tolist()) == expected_valid_ids

    gated_sql = tmp_path / "gated.sqlite"
    export_mod.export_to_sql(
        sdk,
        "STUDY_OPTION",
        "records",
        f"sqlite:///{gated_sql}",
        quality_gate_enabled=True,
        min_schema_readiness_score=50.0,
    )
    with sqlite3.connect(gated_sql) as scon:
        assert [
            r[0]
            for r in scon.execute('SELECT "recordId" FROM "records" ORDER BY "recordId"').fetchall()
        ] == expected_valid_ids

    # CLI --quality-gate across SQLite per-Form and --single-table
    monkeypatch.setattr(cli_ctx, "get_sdk", lambda *_a, **_k: sdk)
    runner = CliRunner()
    cli_gated_bf = tmp_path / "cli_gated_bf.sqlite"
    res = runner.invoke(
        cli.app,
        [
            "export",
            "sql",
            "STUDY_OPTION",
            "records",
            f"sqlite:///{cli_gated_bf}",
            "--quality-gate",
            "--min-readiness-score",
            "50",
        ],
    )
    assert res.exit_code == 0, res.stdout
    with sqlite3.connect(cli_gated_bf) as scon:
        bf_ids = sorted(
            [r[0] for r in scon.execute('SELECT "recordId" FROM "F_DM"').fetchall()]
            + [r[0] for r in scon.execute('SELECT "recordId" FROM "F_VS"').fetchall()]
        )
    assert bf_ids == expected_valid_ids

    # Airflow operator with quality_gate_enabled=True + narrow custom export_func safety check
    af_gated_csv = tmp_path / "af_gated.csv"
    op = ImednetExportOperator(
        task_id="af_gated",
        study_key="STUDY_OPTION",
        destination="csv",
        output_path=str(af_gated_csv),
        quality_gate_enabled=True,
        min_schema_readiness_score=50.0,
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: sdk)
    op.execute({})
    assert sorted(pd.read_csv(af_gated_csv)["recordId"].tolist()) == expected_valid_ids

    narrow_calls: list[tuple[Any, str, str | None]] = []

    def _narrow_custom_writer(sdk_arg: Any, study_arg: str, path_arg: str | None) -> None:
        narrow_calls.append((sdk_arg, study_arg, path_arg))

    monkeypatch.setattr(af_export, "export_to_custom_narrow", _narrow_custom_writer, raising=False)
    op_narrow = ImednetExportOperator(
        task_id="af_narrow",
        study_key="STUDY_OPTION",
        export_func="export_to_custom_narrow",
        output_path="/tmp/narrow.out",
    )
    monkeypatch.setattr(op_narrow, "_get_sdk", lambda: sdk)
    assert op_narrow.execute({}) == "/tmp/narrow.out"
    assert narrow_calls == [(sdk, "STUDY_OPTION", "/tmp/narrow.out")]


def test_consecutive_incremental_sparse_runs_advance_watermarks_and_preserve_min_rejected_ts_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consecutive --incremental CSV, SQL, and Airflow runs advance last_timestamp and send dateModified filters, while retaining min_rejected_ts capping."""
    import imednet.cli.utils.context as cli_ctx
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    sdk, _forms, _variables, records_by_id, list_calls = _build_sparse_study_fixture("STUDY_INC")
    monkeypatch.setattr(cli_ctx, "get_sdk", lambda *_a, **_k: sdk)
    runner = CliRunner()

    ledger_path = tmp_path / "state_ledger.json"
    ledger = FileStateProvider(str(ledger_path))
    expected_wm = datetime(2026, 6, 1, 9, 30, tzinfo=UTC)

    # 1. CLI consecutive --incremental CSV runs
    inc_csv = tmp_path / "inc.csv"
    list_calls.clear()
    r1 = runner.invoke(
        cli.app,
        [
            "export",
            "csv",
            "STUDY_INC",
            str(inc_csv),
            "--incremental",
            "--stream-name",
            "cli_csv_stream",
            "--ledger-path",
            str(ledger_path),
        ],
    )
    assert r1.exit_code == 0, r1.stdout
    assert ledger.get_last_timestamp("STUDY_INC", "cli_csv_stream") == expected_wm

    list_calls.clear()
    r2 = runner.invoke(
        cli.app,
        [
            "export",
            "csv",
            "STUDY_INC",
            str(inc_csv),
            "--incremental",
            "--stream-name",
            "cli_csv_stream",
            "--ledger-path",
            str(ledger_path),
        ],
    )
    assert r2.exit_code == 0, r2.stdout
    assert any(
        "dateModified>=" in str(c.get("filter", "")) or c.get("dateModified") is not None
        for c in list_calls
    )
    assert sorted(pd.read_csv(inc_csv)["recordId"].tolist()) == list(range(1, 11))

    # 2. CLI consecutive --incremental SQL (--single-table) runs with --quality-gate enabled on sparse Records!
    inc_sql = tmp_path / "inc_sql.sqlite"
    r_sql_1 = runner.invoke(
        cli.app,
        [
            "export",
            "sql",
            "STUDY_INC",
            "records",
            f"sqlite:///{inc_sql}",
            "--single-table",
            "--quality-gate",
            "--incremental",
            "--stream-name",
            "cli_sql_stream",
            "--ledger-path",
            str(ledger_path),
        ],
    )
    assert r_sql_1.exit_code == 0, r_sql_1.stdout
    assert ledger.get_last_timestamp("STUDY_INC", "cli_sql_stream") == expected_wm

    list_calls.clear()
    r_sql_2 = runner.invoke(
        cli.app,
        [
            "export",
            "sql",
            "STUDY_INC",
            "records",
            f"sqlite:///{inc_sql}",
            "--single-table",
            "--quality-gate",
            "--incremental",
            "--stream-name",
            "cli_sql_stream",
            "--ledger-path",
            str(ledger_path),
        ],
    )
    assert r_sql_2.exit_code == 0, r_sql_2.stdout
    assert any(
        "dateModified>=" in str(c.get("filter", "")) or c.get("dateModified") is not None
        for c in list_calls
    )

    # 3. Airflow consecutive incremental runs on sparse Records
    af_sql = tmp_path / "af_inc.sqlite"
    monkeypatch.setattr(
        ImednetHook,
        "resolve_destination_connection",
        classmethod(lambda cls, cid, dest: {"conn_str": f"sqlite:///{af_sql}"}),
    )
    op_af = ImednetExportOperator(
        task_id="af_inc_sql",
        study_key="STUDY_INC",
        destination="sql",
        output_path="records",
        destination_conn_id="sqlite_inc",
        incremental=True,
        stream_name="af_sql_stream",
        ledger_path=str(ledger_path),
    )
    monkeypatch.setattr(op_af, "_get_sdk", lambda: sdk)
    op_af.execute({"run_id": "r1"})
    assert ledger.get_last_timestamp("STUDY_INC", "af_sql_stream") == expected_wm

    list_calls.clear()
    op_af.execute({"run_id": "r2"})
    assert any(
        "dateModified>=" in str(c.get("filter", "")) or c.get("dateModified") is not None
        for c in list_calls
    )

    # 4. Guardrail: min_rejected_ts watermark capping is retained when quality_gate_enabled=True drops an early invalid Record
    records_by_id[1].record_data = {"SUBJ_AGE": "INVALID_INT"}
    capped_csv = tmp_path / "capped.csv"
    export_mod.export_to_csv(
        sdk,
        "STUDY_INC",
        str(capped_csv),
        quality_gate_enabled=True,
        incremental=True,
        stream_name="capped_csv_stream",
        ledger_path=str(ledger_path),
    )
    # Record 1 has earliest timestamp (09:01Z), so min_rejected_ts prevents watermark advancement past 09:01Z
    assert ledger.get_last_timestamp("STUDY_INC", "capped_csv_stream") is None
    assert sorted(pd.read_csv(capped_csv)["recordId"].tolist()) == list(range(2, 11))

    # StreamState schema guardrail remains unchanged
    assert set(StreamState.model_fields.keys()) == {
        "last_timestamp",
        "records_processed",
        "last_run_status",
        "error_message",
        "metadata",
        "owner_pid",
        "owner_host",
        "lease_expires_at",
    }


def test_simulation_transport_sparse_optional_records_export_and_advance_watermarks(
    tmp_path: Path,
) -> None:
    """Verify SimulationTransport Study with optional Variables exports all Records and advances watermarks."""
    from imednet import ImednetSDK, SimulationTransport
    from imednet.simulation.state import SimulationState

    state = SimulationState()
    # Add an optional Variable (required=None, matching GET /studies/{studyKey}/variables) to F_DM and F_VS
    state.variables.extend(
        [
            {
                "variableId": 890,
                "variableName": "V_MIDDLE_NAME",
                "variableKey": "V_MIDDLE_NAME",
                "variableType": "Text",
                "formId": 301,
                "formKey": "F_DM",
                "studyKey": "STUDY",
                "label": "Middle Name",
            },
            {
                "variableId": 891,
                "variableName": "V_VS_NOTES",
                "variableKey": "V_VS_NOTES",
                "variableType": "Text",
                "formId": 302,
                "formKey": "F_VS",
                "studyKey": "STUDY",
                "label": "Vital Signs Notes",
            },
        ]
    )

    ledger_path = tmp_path / "sim_ledger.json"
    sim_csv = tmp_path / "sim.csv"
    sim_sql = tmp_path / "sim.sqlite"

    with ImednetSDK(
        api_key="sim-key",  # pragma: allowlist secret
        security_key="sim-sec",  # pragma: allowlist secret
        transport=SimulationTransport(state=state),
    ) as sdk:
        export_mod.export_to_csv(
            sdk,
            "STUDY",
            str(sim_csv),
            quality_gate_enabled=True,
            incremental=True,
            stream_name="sim_csv",
            ledger_path=str(ledger_path),
        )
        export_mod.export_to_sql(
            sdk,
            "STUDY",
            "records",
            f"sqlite:///{sim_sql}",
            quality_gate_enabled=True,
            incremental=True,
            stream_name="sim_sql",
            ledger_path=str(ledger_path),
        )

    assert sorted(pd.read_csv(sim_csv)["recordId"].tolist()) == [601, 602]
    with sqlite3.connect(sim_sql) as scon:
        assert [
            r[0]
            for r in scon.execute('SELECT "recordId" FROM "records" ORDER BY "recordId"').fetchall()
        ] == [601, 602]

    ledger = FileStateProvider(str(ledger_path))
    assert ledger.get_last_timestamp("STUDY", "sim_csv") == datetime(2026, 1, 10, 9, 35, tzinfo=UTC)
    assert ledger.get_last_timestamp("STUDY", "sim_sql") == datetime(2026, 1, 10, 9, 35, tzinfo=UTC)
