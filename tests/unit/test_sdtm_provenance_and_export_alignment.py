"""Unit and integration tests for SDTM checkpoint provenance, domain filtering, PHI safety, and CLI parity."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect

from imednet import cli
from imednet.errors import (
    ExportBatchError,
    ExportConfigurationError,
    ImednetError,
)
from imednet.integrations.dispatcher import export as dispatch_export
from imednet.integrations.export import (
    InvalidDuckDBIdentifierError,
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
from imednet.integrations.parquet import export_to_hive_parquet
from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.records import Record
from imednet.models.sdtm import SDTM_PROVENANCE_RECORD_ID_KEY, transform_records_to_sdtm
from imednet.models.variables import Variable
from imednet.spi.sinks import (
    extract_sdtm_natural_key,
    hydrate_sdtm_checkpoint_state,
    persist_sdtm_checkpoint_state,
    pop_deleted_sdtm_provenance_keys,
    pop_sdtm_record_id_provenance,
    reconcile_sdtm_provenance_rows,
    sdtm_natural_key_tuple,
    strip_sdtm_provenance,
)
from imednet.utils.job_poller import JobFailedError
from imednet_sinks.document import MongoDbSinkConfig
from imednet_sinks.graph import Neo4jSinkConfig
from imednet_sinks.warehouse import SnowflakeSinkConfig
from imednet_workflows.cached_loader import CachedRecordsLoader
from imednet_workflows.record_mapper import RecordMapper
from imednet_workflows.state_ledger import (
    AirflowStateProvider,
    FileStateProvider,
    StreamExecutionStatus,
)

FORBIDDEN_PROVENANCE_COLS = {"recordId", "record_id", "RECORD_ID", SDTM_PROVENANCE_RECORD_ID_KEY}


class _CliResult:
    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _invoke_cli(args: list[str]) -> _CliResult:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    out = io.StringIO()
    err = io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cli.app(args)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else (1 if exc.code else 0)
    return _CliResult(code, out.getvalue(), err.getvalue())


def _install_fake_airflow_vars(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}

    class FakeVariable:
        @staticmethod
        def get(key: str, default: Any = None, deserialize_json: bool = False) -> Any:
            if key not in store:
                return default
            raw = store[key]
            return json.loads(raw) if deserialize_json else raw

        @staticmethod
        def set(
            key: str,
            value: Any,
            description: str | None = None,
            serialize_json: bool = False,
        ) -> None:
            _ = description
            store[key] = json.dumps(value) if serialize_json else str(value)

        @staticmethod
        def delete(key: str) -> None:
            store.pop(key, None)

        @staticmethod
        def keys(prefix: str | None = None) -> list[str]:
            if prefix is None:
                return sorted(store)
            return sorted(k for k in store if k.startswith(prefix))

    import sys

    airflow_mod = ModuleType("airflow")
    sdk_mod = ModuleType("airflow.sdk")
    sdk_mod.Variable = FakeVariable  # type: ignore[attr-defined]
    airflow_mod.sdk = sdk_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "airflow", airflow_mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)
    monkeypatch.delitem(sys.modules, "airflow.models", raising=False)
    monkeypatch.delitem(sys.modules, "airflow.models.variable", raising=False)
    monkeypatch.delitem(sys.modules, "airflow.sdk.execution_time", raising=False)
    monkeypatch.delitem(sys.modules, "airflow.sdk.execution_time.task_runner", raising=False)
    return store


def _build_stateful_sdk(
    records_store: dict[int, Record],
    *,
    forms: list[Form],
    variables: list[Variable],
) -> MagicMock:
    from imednet.integrations.sink_base import (
        extract_record_timestamp,
        normalize_watermark_datetime,
    )

    intervals = [
        Interval(
            interval_id=1,
            interval_name="Screening",
            interval_sequence=1,
            interval_description="Screening Visit",
            interval_group_name="Core",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
            forms=[
                FormSummary(form_id=f.form_id, form_key=f.form_key, form_name=f.form_name or "")
                for f in forms
                if f.form_id is not None and f.form_key is not None
            ],
        )
    ]

    def _list_records(**kw: Any) -> Any:
        raw_filter = kw.get("filter")
        form_id_kw = kw.get("formId")
        deleted_kw = kw.get("deleted")
        clauses: list[str] = []
        if isinstance(raw_filter, str) and raw_filter:
            clauses.extend(c.strip() for c in raw_filter.split(";") if c.strip())

        allowed_form_ids: set[int] | None = None
        if isinstance(form_id_kw, int):
            allowed_form_ids = {form_id_kw}
        elif isinstance(form_id_kw, (list, tuple, set)):
            allowed_form_ids = {int(x) for x in form_id_kw}

        min_ts: datetime | None = None
        filter_deleted: bool | None = deleted_kw if isinstance(deleted_kw, bool) else None

        for clause in clauses:
            if clause.startswith("dateModified>="):
                bound_str = clause.split(">=", 1)[1].strip().strip('"').strip("'")
                min_ts = normalize_watermark_datetime(bound_str)
            elif clause.startswith("formId=="):
                fid = int(clause.split("==", 1)[1].strip())
                allowed_form_ids = {fid} if allowed_form_ids is None else (allowed_form_ids & {fid})
            elif clause.startswith("deleted=="):
                filter_deleted = clause.split("==", 1)[1].strip().lower() == "true"

        matched: list[Record] = []
        for rec in sorted(records_store.values(), key=lambda r: r.record_id or 0):
            if allowed_form_ids is not None and rec.form_id not in allowed_form_ids:
                continue
            if filter_deleted is not None and bool(rec.deleted) is not filter_deleted:
                continue
            if filter_deleted is None and "deleted==" not in (raw_filter or "") and rec.deleted:
                # Default API behavior omits deleted unless requested or unfiltered mock
                pass
            if min_ts is not None:
                rts = extract_record_timestamp(rec)
                if rts is not None and rts < min_ts:
                    continue
            matched.append(rec)
        return iter(matched)

    sdk = MagicMock()
    sdk.forms.list.side_effect = lambda *_a, **_kw: list(forms)
    sdk.get_forms.side_effect = lambda *_a, **_kw: list(forms)
    sdk.intervals.list.side_effect = lambda *_a, **_kw: list(intervals)
    sdk.get_intervals.side_effect = lambda *_a, **_kw: list(intervals)
    sdk.variables.list.side_effect = lambda *_a, **kw: [
        v
        for v in variables
        if kw.get("formId") is None
        or (isinstance(kw["formId"], int) and v.form_id == kw["formId"])
        or (isinstance(kw["formId"], list) and v.form_id in kw["formId"])
    ]
    sdk.get_variables.side_effect = sdk.variables.list.side_effect
    sdk.records.list.side_effect = lambda *_a, **kw: _list_records(**kw)
    sdk.get_records.side_effect = lambda *_a, **kw: list(_list_records(**kw))
    sdk.visits.list.return_value = []
    return sdk


# ---------------------------------------------------------------------------
# 1. Checkpoint metadata persistence & rollback (File + Airflow)
# ---------------------------------------------------------------------------


def test_checkpoint_metadata_persists_across_transactions_and_rollbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FileStateProvider and AirflowStateProvider hydrate, preserve, and roll back metadata."""
    _install_fake_airflow_vars(monkeypatch)
    providers: list[Any] = [
        FileStateProvider(ledger_path=tmp_path / "state.json"),
        AirflowStateProvider(),
    ]

    for provider in providers:
        t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)

        with provider.transaction("STUDY_META", "ae_stream") as tx1:
            assert tx1["metadata"] == {}
            persist_sdtm_checkpoint_state(
                tx1["metadata"],
                {"AE": {"SUBJ-01": 2}},
                {
                    "101": {
                        "STUDYID": "STUDY_META",
                        "DOMAIN": "AE",
                        "USUBJID": "SUBJ-01",
                        "AESEQ": 1,
                    }
                },
            )
            tx1["records_processed"] = 2
            tx1["new_timestamp"] = t1

        # Zero-delta run must hydrate and preserve existing metadata untouched
        with provider.transaction("STUDY_META", "ae_stream") as tx2:
            seq_map, prov_map = hydrate_sdtm_checkpoint_state(tx2["metadata"])
            assert seq_map == {"AE": {"SUBJ-01": 2}}
            assert prov_map["101"]["AESEQ"] == 1
            tx2["records_processed"] = 0

        # Failed run mutating tx["metadata"] in-memory must NOT overwrite committed metadata
        with pytest.raises(RuntimeError, match="sink crash"):
            with provider.transaction("STUDY_META", "ae_stream") as tx_fail:
                tx_fail["metadata"]["sdtm_seq_counters"] = {"AE": {"SUBJ-01": 999}}
                tx_fail["metadata"]["sdtm_provenance"] = {}
                tx_fail["new_timestamp"] = t2
                raise RuntimeError("sink crash")

        state_after_fail = provider.read_state()
        entry_fail = state_after_fail.studies["STUDY_META"].streams["ae_stream"]
        assert entry_fail.last_run_status == StreamExecutionStatus.FAILED
        assert entry_fail.metadata["sdtm_seq_counters"] == {"AE": {"SUBJ-01": 2}}
        assert entry_fail.metadata["sdtm_provenance"]["101"]["AESEQ"] == 1

        # Retry run sees intact committed metadata and advances cleanly
        with provider.transaction("STUDY_META", "ae_stream") as tx3:
            seq_retry, prov_retry = hydrate_sdtm_checkpoint_state(tx3["metadata"])
            assert seq_retry == {"AE": {"SUBJ-01": 2}}
            assert prov_retry["101"]["AESEQ"] == 1
            seq_retry["AE"]["SUBJ-01"] = 3
            prov_retry["103"] = {
                "STUDYID": "STUDY_META",
                "DOMAIN": "AE",
                "USUBJID": "SUBJ-01",
                "AESEQ": 3,
            }
            persist_sdtm_checkpoint_state(tx3["metadata"], seq_retry, prov_retry)
            tx3["records_processed"] = 1
            tx3["new_timestamp"] = t2

        final_entry = provider.read_state().studies["STUDY_META"].streams["ae_stream"]
        assert final_entry.last_run_status == StreamExecutionStatus.SUCCESS
        assert final_entry.metadata["sdtm_seq_counters"] == {"AE": {"SUBJ-01": 3}}
        assert set(final_entry.metadata["sdtm_provenance"]) == {"101", "103"}


# ---------------------------------------------------------------------------
# 2. SPI provenance helpers & pure CDISC public SDTM outputs
# ---------------------------------------------------------------------------


def test_spi_sdtm_provenance_helpers_and_public_sdtm_column_purity() -> None:
    """SPI helpers strip transient recordId provenance while public SDTM APIs emit pure CDISC columns."""
    records = [
        Record(
            record_id=10,
            study_key="STUDY_PURE",
            form_id=1,
            form_key="F_AE",
            subject_key="SUBJ-1",
            record_data={"AETERM": "Headache", "AESTDTC": "2026-01-01"},
        )
    ]
    df_public = transform_records_to_sdtm(records, domain="AE", study_key="STUDY_PURE")
    assert set(df_public.columns).isdisjoint(FORBIDDEN_PROVENANCE_COLS)
    assert list(df_public["AESEQ"]) == [1]

    sdk = _build_stateful_sdk(
        {10: records[0]},
        forms=[Form(form_id=1, form_key="F_AE", form_name="Adverse Events")],
        variables=[Variable(variable_id=1, form_id=1, form_key="F_AE", variable_name="AETERM")],
    )
    mapper = RecordMapper(sdk)
    df_mapper = mapper.sdtm_dataframe("STUDY_PURE", domain="AE")
    assert set(df_mapper.columns).isdisjoint(FORBIDDEN_PROVENANCE_COLS)
    assert list(df_mapper["AESEQ"]) == [1]

    raw_rows = [
        {
            "STUDYID": "STUDY_PURE",
            "DOMAIN": "AE",
            "USUBJID": "SUBJ-1",
            "AESEQ": 1,
            "AETERM": "Updated Headache",
            SDTM_PROVENANCE_RECORD_ID_KEY: "10",
        }
    ]
    stripped = strip_sdtm_provenance(raw_rows)
    assert SDTM_PROVENANCE_RECORD_ID_KEY not in stripped[0]
    prov: dict[str, dict[str, Any]] = {
        "10": {"STUDYID": "STUDY_PURE", "DOMAIN": "AE", "USUBJID": "SUBJ-1", "AESEQ": 1},
        "11": {"STUDYID": "STUDY_PURE", "DOMAIN": "AE", "USUBJID": "SUBJ-1", "AESEQ": 2},
    }
    cleaned, replaced, changed_old = reconcile_sdtm_provenance_rows(
        raw_rows, prov, default_domain="AE"
    )
    assert len(cleaned) == 1
    assert SDTM_PROVENANCE_RECORD_ID_KEY not in cleaned[0]
    assert len(replaced) == 1
    assert changed_old == []

    deleted_keys = pop_deleted_sdtm_provenance_keys([Record(record_id=11, deleted=True)], prov)
    assert len(deleted_keys) == 1
    assert sdtm_natural_key_tuple(extract_sdtm_natural_key(deleted_keys[0])) == (
        ("STUDYID", "STUDY_PURE"),
        ("DOMAIN", "AE"),
        ("USUBJID", "SUBJ-1"),
        ("AESEQ", 2),
    )
    assert pop_sdtm_record_id_provenance({"AETERM": "x"}) is None


# ---------------------------------------------------------------------------
# 3. Incremental SDTM update/delete reconciliation across ALL tabular & Hive formats
# ---------------------------------------------------------------------------


def test_incremental_sdtm_reconciles_updates_deletes_and_sequences_across_all_exports(
    tmp_path: Path,
) -> None:
    """Repeated incremental as_sdtm=True runs deduplicate updates, delete voided rows, and preserve <DOM>SEQ."""
    forms = [Form(form_id=10, form_key="F_AE", form_name="Adverse Events")]
    variables = [
        Variable(variable_id=1, form_id=10, form_key="F_AE", variable_name="AETERM", label="Term"),
        Variable(
            variable_id=2, form_id=10, form_key="F_AE", variable_name="AESTDTC", label="Start"
        ),
    ]

    store: dict[int, Record] = {
        101: Record(
            record_id=101,
            study_key="S_INC",
            form_id=10,
            form_key="F_AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-04-01T10:00:00Z",
            record_data={"AETERM": "Headache", "AESTDTC": "2026-04-01"},
        ),
        102: Record(
            record_id=102,
            study_key="S_INC",
            form_id=10,
            form_key="F_AE",
            subject_key="SUBJ-01",
            deleted=False,
            date_modified="2026-04-01T11:00:00Z",
            record_data={"AETERM": "Nausea", "AESTDTC": "2026-04-02"},
        ),
    }
    sdk = _build_stateful_sdk(store, forms=forms, variables=variables)

    csv_path = tmp_path / "ae.csv"
    pq_path = tmp_path / "ae.parquet"
    xlsx_path = tmp_path / "ae.xlsx"
    json_flat_path = tmp_path / "ae_flat.json"
    json_hier_path = tmp_path / "ae_hier.json"
    sql_conn = f"sqlite:///{tmp_path / 'ae_single.db'}"
    sql_by_form_conn = f"sqlite:///{tmp_path / 'ae_by_form.db'}"
    long_sql_conn = f"sqlite:///{tmp_path / 'ae_long.db'}"
    ddb_path = tmp_path / "ae.duckdb"
    ddb_by_form_path = tmp_path / "ae_forms.duckdb"
    hive_dir = tmp_path / "hive_sdtm"
    ledger_dir = tmp_path / "ledgers"
    ledger_dir.mkdir()

    def _run_all_targets() -> None:
        common = {"as_sdtm": True, "sdtm_domain": "AE", "incremental": True}
        export_to_csv(sdk, "S_INC", csv_path, ledger_path=ledger_dir / "csv.json", **common)
        export_to_parquet(sdk, "S_INC", pq_path, ledger_path=ledger_dir / "pq.json", **common)
        export_to_excel(sdk, "S_INC", xlsx_path, ledger_path=ledger_dir / "xl.json", **common)
        export_to_json(
            sdk,
            "S_INC",
            json_flat_path,
            hierarchical=False,
            ledger_path=ledger_dir / "jf.json",
            **common,
        )
        export_to_json(
            sdk,
            "S_INC",
            json_hier_path,
            hierarchical=True,
            ledger_path=ledger_dir / "jh.json",
            **common,
        )
        export_to_sql(
            sdk, "S_INC", "ae_tbl", sql_conn, ledger_path=ledger_dir / "sql.json", **common
        )
        export_to_sql_by_form(
            sdk, "S_INC", sql_by_form_conn, ledger_path=ledger_dir / "sqlf.json", **common
        )
        export_to_long_sql(
            sdk, "S_INC", "ae_long", long_sql_conn, ledger_path=ledger_dir / "sqll.json", **common
        )
        export_to_duckdb(
            sdk, "S_INC", ddb_path, "ae_tbl", ledger_path=ledger_dir / "ddb.json", **common
        )
        export_to_duckdb_by_form(
            sdk, "S_INC", ddb_by_form_path, ledger_path=ledger_dir / "ddbf.json", **common
        )
        export_to_hive_parquet(
            sdk, "S_INC", str(hive_dir), ledger_path=ledger_dir / "hive.json", **common
        )

    # Run 1: Initial load (101 -> AESEQ=1 Headache, 102 -> AESEQ=2 Nausea)
    _run_all_targets()

    # Run 2: Zero-delta run preserves state & rows without duplication
    _run_all_targets()

    # Run 3: Mutate 101 topic + date, void 102, and insert 103
    store[101] = Record(
        record_id=101,
        study_key="S_INC",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=False,
        date_modified="2026-04-03T09:00:00Z",
        record_data={"AETERM": "Severe Migraine", "AESTDTC": "2026-04-10"},
    )
    store[102] = Record(
        record_id=102,
        study_key="S_INC",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=True,
        date_modified="2026-04-03T09:30:00Z",
        record_data={"AETERM": "Nausea", "AESTDTC": "2026-04-02"},
    )
    store[103] = Record(
        record_id=103,
        study_key="S_INC",
        form_id=10,
        form_key="F_AE",
        subject_key="SUBJ-01",
        deleted=False,
        date_modified="2026-04-03T10:00:00Z",
        record_data={"AETERM": "Rash", "AESTDTC": "2026-04-03"},
    )
    _run_all_targets()

    def _assert_tabular_df(df: pd.DataFrame, label: str) -> None:
        assert set(df.columns).isdisjoint(FORBIDDEN_PROVENANCE_COLS), label
        ordered = df.sort_values("AESEQ").reset_index(drop=True)
        assert list(ordered["AESEQ"].astype(int)) == [1, 3], label
        assert list(ordered["AETERM"]) == ["Severe Migraine", "Rash"], label
        assert list(ordered["AESTDTC"]) == ["2026-04-10", "2026-04-03"], label

    _assert_tabular_df(pd.read_csv(csv_path), "csv")
    _assert_tabular_df(pd.read_parquet(pq_path), "parquet")
    _assert_tabular_df(pd.read_excel(xlsx_path), "excel")
    _assert_tabular_df(pd.DataFrame(json.loads(json_flat_path.read_text("utf-8"))), "json_flat")

    hier_payload = json.loads(json_hier_path.read_text("utf-8"))
    hier_records = [
        rec
        for subj in hier_payload
        for dom_grp in subj.get("domains", [])
        for rec in dom_grp.get("records", [])
    ]
    _assert_tabular_df(pd.DataFrame(hier_records), "json_hier")

    _assert_tabular_df(pd.read_sql_table("ae_tbl", create_engine(sql_conn)), "sql")
    _assert_tabular_df(pd.read_sql_table("F_AE", create_engine(sql_by_form_conn)), "sql_by_form")

    long_df = pd.read_sql_table("ae_long", create_engine(long_sql_conn))
    assert set(long_df.columns).isdisjoint(FORBIDDEN_PROVENANCE_COLS)
    aeterm_long = long_df[long_df["variable_name"] == "AETERM"].sort_values("AESEQ")
    assert list(aeterm_long["AESEQ"].astype(int)) == [1, 3]
    assert list(aeterm_long["value"]) == ["Severe Migraine", "Rash"]

    import duckdb

    with duckdb.connect(str(ddb_path)) as conn:
        _assert_tabular_df(conn.execute("SELECT * FROM ae_tbl").df(), "duckdb")
    with duckdb.connect(str(ddb_by_form_path)) as conn:
        _assert_tabular_df(conn.execute("SELECT * FROM F_AE").df(), "duckdb_by_form")

    hive_files = sorted(hive_dir.rglob("*.parquet"))
    assert hive_files
    hive_df = pd.concat([pd.read_parquet(p) for p in hive_files], ignore_index=True)
    _assert_tabular_df(hive_df, "hive_parquet")


# ---------------------------------------------------------------------------
# 4 & 5. Multi-Form SDTM domain filtering + BRTHDTC 4-digit year vs ***MASKED***
# ---------------------------------------------------------------------------


def test_multiform_sdtm_domain_filtering_and_brthdtc_year_preservation(tmp_path: Path) -> None:
    """sdtm_domain='VS' excludes non-VS forms; BRTHDTC keeps YYYY on SDTM and ***MASKED*** on raw."""
    forms = [
        Form(form_id=1, form_key="F_DM", form_name="Demographics"),
        Form(form_id=2, form_key="CRF_20", form_name="Vital Signs Assessment"),
        Form(form_id=3, form_key="F_AE", form_name="Adverse Events"),
    ]
    variables = [
        Variable(variable_id=1, form_id=1, form_key="F_DM", variable_name="BRTHDTC"),
        Variable(variable_id=2, form_id=1, form_key="F_DM", variable_name="patientName"),
        Variable(variable_id=3, form_id=2, form_key="CRF_20", variable_name="SYSBP"),
        Variable(variable_id=4, form_id=3, form_key="F_AE", variable_name="AETERM"),
    ]
    records = {
        1: Record(
            record_id=1,
            study_key="MULTI_ST",
            form_id=1,
            form_key="F_DM",
            subject_key="SUBJ-01",
            record_data={"BRTHDTC": "1980-05-15", "SEX": "F", "patientName": "Alice Secret"},
        ),
        2: Record(
            record_id=2,
            study_key="MULTI_ST",
            form_id=2,
            form_key="CRF_20",
            subject_key="SUBJ-01",
            record_data={"VSTESTCD": "SYSBP", "VSORRES": "120", "VSDTC": "2026-02-10"},
        ),
        3: Record(
            record_id=3,
            study_key="MULTI_ST",
            form_id=3,
            form_key="F_AE",
            subject_key="SUBJ-01",
            record_data={"AETERM": "Cough", "AESTDTC": "2026-02-11"},
        ),
    }
    sdk = _build_stateful_sdk(records, forms=forms, variables=variables)

    # 1. Multi-Form sdtm_domain="VS" excludes F_DM and F_AE
    vs_csv = tmp_path / "vs.csv"
    export_to_csv(sdk, "MULTI_ST", vs_csv, as_sdtm=True, sdtm_domain="VS")
    vs_df = pd.read_csv(vs_csv)
    assert len(vs_df) == 1
    assert list(vs_df["DOMAIN"]) == ["VS"]
    assert list(vs_df["VSTESTCD"]) == ["SYSBP"]

    sql_forms_conn = f"sqlite:///{tmp_path / 'vs_forms.db'}"
    export_to_sql_by_form(sdk, "MULTI_ST", sql_forms_conn, as_sdtm=True, sdtm_domain="VS")
    assert inspect(create_engine(sql_forms_conn)).get_table_names() == ["CRF_20"]

    # 2. BRTHDTC keeps 4-digit year ("1980") when as_sdtm=True, mask_phi=True across formats
    dm_csv = tmp_path / "dm_sdtm.csv"
    dm_pq = tmp_path / "dm_sdtm.parquet"
    dm_json = tmp_path / "dm_sdtm.json"
    dm_hive = tmp_path / "dm_hive"
    export_to_csv(sdk, "MULTI_ST", dm_csv, as_sdtm=True, sdtm_domain="DM", mask_phi=True)
    export_to_parquet(sdk, "MULTI_ST", dm_pq, as_sdtm=True, sdtm_domain="DM", mask_phi=True)
    export_to_json(sdk, "MULTI_ST", dm_json, as_sdtm=True, sdtm_domain="DM", mask_phi=True)
    export_to_hive_parquet(
        sdk, "MULTI_ST", str(dm_hive), as_sdtm=True, sdtm_domain="DM", mask_phi=True
    )

    dm_csv_df = pd.read_csv(dm_csv, dtype=str)
    assert list(dm_csv_df["BRTHDTC"]) == ["1980"]
    assert list(dm_csv_df["patientName"]) == ["***MASKED***"]
    assert list(pd.read_parquet(dm_pq)["BRTHDTC"].astype(str)) == ["1980"]
    assert json.loads(dm_json.read_text("utf-8"))[0]["BRTHDTC"] == "1980"
    hive_dm_df = pd.read_parquet(next(dm_hive.rglob("*.parquet")))
    assert list(hive_dm_df["BRTHDTC"].astype(str)) == ["1980"]

    # 3. Non-SDTM export (as_sdtm=False, mask_phi=True) MUST still mask BRTHDTC with ***MASKED***
    raw_csv = tmp_path / "dm_raw.csv"
    export_to_csv(sdk, "MULTI_ST", raw_csv, form_whitelist=[1], as_sdtm=False, mask_phi=True)
    raw_df = pd.read_csv(raw_csv, dtype=str)
    assert list(raw_df["BRTHDTC"]) == ["***MASKED***"]
    assert list(raw_df["patientName"]) == ["***MASKED***"]

    # 4. Hive Parquet non-streaming naming_convention also succeeds without None models
    hive_camel = tmp_path / "hive_camel"
    export_to_hive_parquet(sdk, "MULTI_ST", str(hive_camel), naming_convention="camelCase")
    camel_files = list(hive_camel.rglob("*.parquet"))
    assert len(camel_files) == 3


# ---------------------------------------------------------------------------
# 7. CachedRecordsLoader pre-write SQLite PHI redaction & ::unmasked isolation
# ---------------------------------------------------------------------------


def test_cached_records_loader_redacts_phi_before_sqlite_write_and_isolates_unmasked(
    tmp_path: Path,
) -> None:
    """mask_phi=True persists zero raw PHI to SQLite; mask_phi=False uses ::unmasked namespace."""
    phi_record = Record(
        study_key="STUDY_CACHE",
        form_id=1,
        form_key="F_DM",
        record_id=55,
        subject_key="SUBJ-PHI",
        date_modified=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        record_data={
            "patientName": "Secret Patient Name",
            "BRTHDTC": "1980-05-15",
            "email": "patient@example.com",
            "ssn": "123-45-6789",
            "AGE": 45,
        },
    )
    sdk = MagicMock()
    sdk.get_records.side_effect = lambda **_kw: [phi_record]
    sdk.records.list.side_effect = lambda **_kw: iter([phi_record])

    cache_dir = tmp_path / "cache"
    masked_loader = CachedRecordsLoader(sdk, cache_dir=cache_dir, mask_phi=True)
    loaded_masked = masked_loader.load_records("STUDY_CACHE")
    assert len(loaded_masked) == 1
    assert loaded_masked[0].record_data["patientName"] == "***MASKED***"
    assert loaded_masked[0].record_data["BRTHDTC"] == "***MASKED***"

    # Inspect raw SQLite rows directly to verify zero plaintext PHI hit disk under mask_phi=True
    with sqlite3.connect(masked_loader.db_path) as conn:
        rows = conn.execute("SELECT study_key, payload FROM record_cache").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "STUDY_CACHE"
    raw_payload = rows[0][1]
    assert "Secret Patient Name" not in raw_payload
    assert "1980-05-15" not in raw_payload
    assert "patient@example.com" not in raw_payload
    assert "123-45-6789" not in raw_payload

    # Loading with mask_phi=False uses isolated 'STUDY_CACHE::unmasked' namespace
    unmasked_loader = CachedRecordsLoader(sdk, cache_dir=cache_dir, mask_phi=False)
    loaded_unmasked = unmasked_loader.load_records("STUDY_CACHE")
    assert len(loaded_unmasked) == 1
    assert loaded_unmasked[0].record_data["patientName"] == "Secret Patient Name"
    assert loaded_unmasked[0].record_data["BRTHDTC"] == "1980-05-15"

    # Re-reading masked_loader still returns masked records from 'STUDY_CACHE'
    reloaded_masked = masked_loader.load_records("STUDY_CACHE")
    assert reloaded_masked[0].record_data["patientName"] == "***MASKED***"

    with sqlite3.connect(masked_loader.db_path) as conn:
        keys = {r[0] for r in conn.execute("SELECT DISTINCT study_key FROM record_cache")}
    assert keys == {"STUDY_CACHE", "STUDY_CACHE::unmasked"}


# ---------------------------------------------------------------------------
# 8 & 9. Defaults, ImednetError wrapping, DuckDB identifier MRO, and dispatcher
# ---------------------------------------------------------------------------


def test_export_defaults_driver_error_wrapping_and_dispatcher_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """export_to_csv/sql default quality_gate=False & manifest_path=None, and wrap driver errors."""
    forms = [Form(form_id=1, form_key="F_VIT", form_name="Vitals")]
    variables = [
        Variable(variable_id=1, form_id=1, form_key="F_VIT", variable_name="SYSBP"),
        Variable(variable_id=2, form_id=1, form_key="F_VIT", variable_name="DIABP"),
        Variable(variable_id=3, form_id=1, form_key="F_VIT", variable_name="PULSE"),
    ]
    # Incomplete draft record (only 1 of 3 variables populated, status Draft)
    draft_rec = Record(
        record_id=1,
        study_key="STUDY_DEF",
        form_id=1,
        form_key="F_VIT",
        subject_key="S1",
        record_status="Draft",
        record_data={"SYSBP": 118},
    )
    sdk = _build_stateful_sdk({1: draft_rec}, forms=forms, variables=variables)

    csv_out = tmp_path / "draft.csv"
    sql_out = f"sqlite:///{tmp_path / 'draft.db'}"
    export_to_csv(sdk, "STUDY_DEF", csv_out)
    export_to_sql(sdk, "STUDY_DEF", "vitals", sql_out)

    # Draft record retained by default (quality_gate_enabled=False) and no manifest sidecar created
    assert len(pd.read_csv(csv_out)) == 1
    assert len(pd.read_sql_table("vitals", create_engine(sql_out))) == 1
    assert list(tmp_path.glob("*manifest*")) == []

    # InvalidDuckDBIdentifierError inherits from both ExportConfigurationError (ImednetError) & ValueError
    assert issubclass(InvalidDuckDBIdentifierError, ExportConfigurationError)
    assert issubclass(InvalidDuckDBIdentifierError, ImednetError)
    assert issubclass(InvalidDuckDBIdentifierError, ValueError)
    with pytest.raises(InvalidDuckDBIdentifierError):
        export_to_duckdb(sdk, "STUDY_DEF", tmp_path / "bad.duckdb", "DROP TABLE x;--")

    # Writing CSV to a directory or SQL with bad connection string raises ImednetError subclass
    dir_target = tmp_path / "existing_dir"
    dir_target.mkdir()
    with pytest.raises(ExportBatchError):
        export_to_csv(sdk, "STUDY_DEF", dir_target, max_retries=0)
    with pytest.raises(ExportConfigurationError):
        export_to_sql(sdk, "STUDY_DEF", "vitals", "not_a_valid_driver://bad")

    # Dispatcher routes snowflake via export_to_snowflake (promoting include_deleted=True on incremental)
    captured_sf: dict[str, Any] = {}

    def _fake_export_snowflake(sdk_arg: Any, study_arg: str, config: SnowflakeSinkConfig) -> int:
        captured_sf["sdk"] = sdk_arg
        captured_sf["study"] = study_arg
        captured_sf["config"] = config
        return 7

    monkeypatch.setattr("imednet_sinks.warehouse.export_to_snowflake", _fake_export_snowflake)
    sf_cfg = SnowflakeSinkConfig(
        study_key="STUDY_DEF",
        account="acct",
        user="usr",
        password="pwd",  # pragma: allowlist secret
        database="DB",
        schema="PUBLIC",
        warehouse="WH",
        stage="STG",
        table="TBL",
    )
    res_sf = dispatch_export("snowflake", sdk, "STUDY_DEF", config=sf_cfg, incremental=True)
    assert res_sf == 7
    assert captured_sf["config"].incremental is True
    assert sf_cfg.incremental is False  # original config not mutated

    captured_mongo: dict[str, Any] = {}

    def _fake_export_mongo(_sdk: Any, _study: str, config: MongoDbSinkConfig) -> int:
        captured_mongo["config"] = config
        return 3

    monkeypatch.setattr("imednet_sinks.document.export_to_mongodb", _fake_export_mongo)
    mongo_cfg = MongoDbSinkConfig(
        study_key="STUDY_DEF",
        uri="mongodb://localhost:27017",
        database="db",
        collection="col",
        mask_phi=True,
    )
    assert dispatch_export("mongodb", sdk, "STUDY_DEF", config=mongo_cfg, mask_phi=False) == 3
    assert captured_mongo["config"].mask_phi is False
    assert mongo_cfg.mask_phi is True


# ---------------------------------------------------------------------------
# 10, 11, 12. CLI export options, optional SQLite table, env passwords & @with_sdk
# ---------------------------------------------------------------------------


def test_cli_export_forms_by_key_optional_sqlite_table_passwords_and_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI resolves --forms by formKey, makes SQLite table optional, supports env passwords, and formats ImednetError."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_api_key_123")  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec_key_456")  # pragma: allowlist secret

    forms = [
        Form(form_id=10, form_key="F_DM", form_name="Demographics"),
        Form(form_id=20, form_key="F_VS", form_name="Vital Signs"),
    ]
    variables = [
        Variable(variable_id=1, form_id=10, form_key="F_DM", variable_name="AGE"),
        Variable(variable_id=2, form_id=20, form_key="F_VS", variable_name="SYSBP"),
        Variable(variable_id=3, form_id=20, form_key="F_VS", variable_name="DIABP"),
    ]
    records = {
        1: Record(
            record_id=1,
            study_key="CLI_ST",
            form_id=10,
            form_key="F_DM",
            subject_key="S01",
            record_data={"AGE": 40},
        ),
        2: Record(
            record_id=2,
            study_key="CLI_ST",
            form_id=20,
            form_key="F_VS",
            subject_key="S01",
            record_data={"SYSBP": 122, "DIABP": 78},
        ),
    }
    sdk = _build_stateful_sdk(records, forms=forms, variables=variables)
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda **_kw: sdk)

    # 1. --forms F_VS (resolved by formKey) + --vars SYSBP on CSV
    csv_file = tmp_path / "vs_only.csv"
    res_csv = _invoke_cli(
        ["export", "csv", "CLI_ST", str(csv_file), "--forms", "F_VS", "--vars", "SYSBP"]
    )
    assert res_csv.exit_code == 0, res_csv.stderr
    df_csv = pd.read_csv(csv_file)
    assert len(df_csv) == 1
    assert "SYSBP" in df_csv.columns
    assert "DIABP" not in df_csv.columns

    # 2. Optional table on per-Form SQLite CLI export + warning when positional table is passed
    sqlite_uri = f"sqlite:///{tmp_path / 'per_form.db'}"
    res_sql_opt = _invoke_cli(["export", "sql", "CLI_ST", sqlite_uri, "--forms", "F_VS"])
    assert res_sql_opt.exit_code == 0, res_sql_opt.stderr
    assert inspect(create_engine(sqlite_uri)).get_table_names() == ["F_VS"]

    sqlite_warn_uri = f"sqlite:///{tmp_path / 'warn_form.db'}"
    res_sql_warn = _invoke_cli(["export", "sql", "CLI_ST", "ignored_tbl", sqlite_warn_uri])
    assert res_sql_warn.exit_code == 0
    assert "Warning: positional table argument 'ignored_tbl' is ignored" in res_sql_warn.stderr

    # 3. Missing table on --single-table, --long-format, or non-SQLite exits 2; conflict exits 2
    assert _invoke_cli(["export", "sql", "CLI_ST", sqlite_uri, "--single-table"]).exit_code == 2
    assert _invoke_cli(["export", "sql", "CLI_ST", sqlite_uri, "--long-format"]).exit_code == 2
    assert _invoke_cli(["export", "sql", "CLI_ST", "postgresql://localhost/db"]).exit_code == 2
    assert (
        _invoke_cli(
            ["export", "csv", "CLI_ST", str(csv_file), "--as-sdtm", "--use-labels"]
        ).exit_code
        == 2
    )

    # 4. --long-format SQL & --hierarchical JSON
    long_uri = f"sqlite:///{tmp_path / 'long.db'}"
    res_long = _invoke_cli(
        ["export", "sql", "CLI_ST", "long_tbl", long_uri, "--long-format", "--forms", "F_VS"]
    )
    assert res_long.exit_code == 0, res_long.stderr
    long_df = pd.read_sql_table("long_tbl", create_engine(long_uri))
    assert set(long_df["variable_name"]) == {"SYSBP", "DIABP"}

    hier_json = tmp_path / "hier.json"
    res_hier = _invoke_cli(
        ["export", "json", "CLI_ST", str(hier_json), "--hierarchical", "--forms", "F_VS"]
    )
    assert res_hier.exit_code == 0, res_hier.stderr
    hier_data = json.loads(hier_json.read_text("utf-8"))
    assert "S01" in json.dumps(hier_data) and "F_DM" not in json.dumps(hier_data)

    # 5. Neo4j & Snowflake --password and NEO4J_PASSWORD / SNOWFLAKE_PASSWORD env vars + redaction
    sdk.sinks = MagicMock()
    sdk.sinks.Neo4jSinkConfig = Neo4jSinkConfig
    sdk.sinks.SnowflakeSinkConfig = SnowflakeSinkConfig
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: object())

    monkeypatch.setenv("NEO4J_PASSWORD", "neo_env_secret_999")  # pragma: allowlist secret
    res_neo = _invoke_cli(["export", "neo4j", "CLI_ST", "bolt://localhost:7687", "neo4j"])
    assert res_neo.exit_code == 0, res_neo.stderr
    assert sdk.sinks.export_to_neo4j.call_args.kwargs["config"].auth == (
        "neo4j",
        "neo_env_secret_999",
    )

    # Conflict: positional password + --password exits 2
    res_neo_conflict = _invoke_cli(
        [
            "export",
            "neo4j",
            "CLI_ST",
            "bolt://localhost:7687",
            "neo4j",
            "pos_pwd",
            "--password",
            "opt_pwd",
        ]
    )
    assert res_neo_conflict.exit_code == 2

    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "sf_env_secret_888")  # pragma: allowlist secret
    res_sf = _invoke_cli(
        ["export", "snowflake", "CLI_ST", "acct", "usr", "DB", "PUBLIC", "WH", "STG", "TBL"]
    )
    assert res_sf.exit_code == 0, res_sf.stderr
    assert (
        sdk.sinks.export_to_snowflake.call_args.kwargs["config"].password
        == "sf_env_secret_888"  # pragma: allowlist secret
    )

    # 6. @with_sdk formats ImednetError as 'Error: ...' (with secret redaction), while RuntimeError
    #    and JobFailedError keep 'Unexpected error: ...'
    sdk.sinks.export_to_neo4j.side_effect = ExportBatchError(
        "Neo4j auth failed with neo_env_secret_999", batch_id="b1"
    )
    res_imednet_err = _invoke_cli(["export", "neo4j", "CLI_ST", "bolt://localhost:7687", "neo4j"])
    assert res_imednet_err.exit_code == 1
    assert res_imednet_err.stderr.strip().startswith("Error:")
    assert "Unexpected error:" not in res_imednet_err.stderr
    assert "neo_env_secret_999" not in res_imednet_err.stderr
    assert "********" in res_imednet_err.stderr

    sdk.sinks.export_to_neo4j.side_effect = RuntimeError("boom_runtime")
    res_rt = _invoke_cli(["export", "neo4j", "CLI_ST", "bolt://localhost:7687", "neo4j"])
    assert res_rt.exit_code == 1
    assert "Unexpected error: boom_runtime" in res_rt.stderr

    sdk.sinks.export_to_neo4j.side_effect = JobFailedError("job_boom", "FAILED")
    res_jf = _invoke_cli(["export", "neo4j", "CLI_ST", "bolt://localhost:7687", "neo4j"])
    assert res_jf.exit_code == 1
    assert "Unexpected error:" in res_jf.stderr and "job_boom" in res_jf.stderr
