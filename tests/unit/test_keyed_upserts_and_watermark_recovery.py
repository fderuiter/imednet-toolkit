"""End-to-end tests for keyed file/DB upserts and transactional watermark recovery."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import duckdb
import httpx
import pandas as pd
import pyarrow.parquet as pq
import pytest
import sqlalchemy as sa

from imednet import ImednetSDK
from imednet.integrations import enrichment as enrichment_mod
from imednet.integrations.export import (
    TabularParquetSink,
    TabularSinkConfig,
    export_to_csv,
    export_to_duckdb_by_form,
    export_to_excel,
    export_to_json,
    export_to_long_sql,
    export_to_parquet,
)
from imednet.models.study_config import StudyConfiguration
from imednet.simulation.state import SimulationState
from imednet.simulation.transport import SimulationTransport
from imednet_workflows.state_ledger import (
    FileStateProvider,
    LedgerState,
    StreamExecutionStatus,
    StreamState,
    StudyState,
)


def _make_study_state() -> SimulationState:
    """Create an isolated SimulationState with custom PHI variable and initial Study Records."""
    state = SimulationState()
    state.variables.extend(
        [
            {
                "variableId": 804,
                "variableName": "V_CUSTOM_MRN",
                "variableKey": "V_CUSTOM_MRN",
                "variableType": "Text",
                "formId": 301,
                "formKey": "F_DM",
                "studyKey": "STUDY",
                "label": "Subject Custom PHI",
            },
            {
                "variableId": 805,
                "variableName": "V_NOTES",
                "variableKey": "V_NOTES",
                "variableType": "Text",
                "formId": 302,
                "formKey": "F_VS",
                "studyKey": "STUDY",
                "label": "Clinical Notes",
            },
            {
                "variableId": 807,
                "variableName": "PATIENT_INITIALS",
                "variableKey": "PATIENT_INITIALS",
                "variableType": "Text",
                "formId": 301,
                "formKey": "F_DM",
                "studyKey": "STUDY",
                "label": "Subject Initials",
            },
        ]
    )
    state.records = [
        {
            "recordId": 601,
            "studyKey": "STUDY",
            "subjectKey": "SUB-PHI-001",
            "subjectId": 201,
            "formId": 301,
            "formKey": "F_DM",
            "visitId": 401,
            "intervalId": 501,
            "siteId": 101,
            "recordType": "Standard",
            "recordStatus": "Complete",
            "recordData": {
                "V_AGE": 45,
                "V_GENDER": "Female",
                "V_CUSTOM_MRN": "MRN-12345",
                "PATIENT_INITIALS": "J.D.",
            },
            "deleted": False,
            "dateCreated": "2026-01-10T09:30:00Z",
            "dateModified": "2026-01-10T09:30:00Z",
            "lastUpdated": "2026-01-10T09:30:00Z",
        },
        {
            "recordId": 602,
            "studyKey": "STUDY",
            "subjectKey": "SUB-PHI-001",
            "subjectId": 201,
            "formId": 302,
            "formKey": "F_VS",
            "visitId": 401,
            "intervalId": 501,
            "siteId": 101,
            "recordType": "Standard",
            "recordStatus": "Complete",
            "recordData": {
                "V_WEIGHT": 68.5,
                "V_NOTES": "Initial note",
            },
            "deleted": False,
            "dateCreated": "2026-01-10T09:35:00Z",
            "dateModified": "2026-01-10T09:35:00Z",
            "lastUpdated": "2026-01-10T09:35:00Z",
        },
        {
            "recordId": 603,
            "studyKey": "STUDY",
            "subjectKey": "SUB-PHI-002",
            "subjectId": 202,
            "formId": 301,
            "formKey": "F_DM",
            "visitId": 401,
            "intervalId": 501,
            "siteId": 101,
            "recordType": "Standard",
            "recordStatus": "Complete",
            "recordData": {
                "V_AGE": 39,
                "V_GENDER": "Male",
                "V_CUSTOM_MRN": "MRN-55555",
                "PATIENT_INITIALS": "A.S.",
            },
            "deleted": False,
            "dateCreated": "2026-01-10T09:40:00Z",
            "dateModified": "2026-01-10T09:40:00Z",
            "lastUpdated": "2026-01-10T09:40:00Z",
        },
    ]
    return state


def _make_sdk(
    state: SimulationState,
    *,
    transport: httpx.BaseTransport | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
    phi_fields: list[str] | None = None,
) -> ImednetSDK:
    """Create an offline ImednetSDK wired to SimulationTransport and optional StudyConfiguration."""
    if monkeypatch is not None and phi_fields is not None:
        study_cfg = StudyConfiguration(studyKey="STUDY", phiFields=phi_fields)
        fake_store = SimpleNamespace(
            resolve_committed_config=lambda sk: study_cfg if sk == "STUDY" else None
        )
        monkeypatch.setattr(enrichment_mod, "_get_config_store_cls", lambda: fake_store)
    return ImednetSDK(
        api_key="sim-key",  # pragma: allowlist secret
        security_key="sim-sec",  # pragma: allowlist secret
        base_url="https://sim.example",
        transport=transport or SimulationTransport(state),
        retries=0,
    )


def _assert_no_temp_artifacts(directory: Path) -> None:
    """Verify no leftover temporary files or sidecar directories exist."""
    leftover = [
        p.name
        for p in directory.iterdir()
        if p.name.endswith(".tmp") or ".tmp." in p.name or p.name.startswith(".")
    ]
    assert leftover == [], f"Unexpected leftover temp artifacts: {leftover}"


def _run_all_six_exporters(
    sdk: ImednetSDK,
    paths: dict[str, Any],
    *,
    mode: str,
    since_ts: str | None = None,
    ledger_dir: Path | None = None,
    idempotent: bool = True,
    include_deleted: bool = False,
) -> None:
    """Invoke all six target exporters under either --incremental, --since, or snapshot mode."""
    common: dict[str, Any] = {
        "idempotent": idempotent,
        "include_deleted": include_deleted,
        "quality_gate_enabled": False,
        "batch_size": 2,
    }

    def _opts(name: str) -> dict[str, Any]:
        opts = dict(common)
        if mode == "incremental":
            assert ledger_dir is not None
            opts["incremental"] = True
            opts["ledger_path"] = ledger_dir / f"{name}_ledger.json"
        elif mode == "since":
            if since_ts is not None:
                opts["modified_since"] = since_ts
        return opts

    export_to_csv(sdk, "STUDY", paths["csv"], **_opts("csv"))
    export_to_parquet(sdk, "STUDY", paths["parquet"], **_opts("parquet"))
    export_to_excel(sdk, "STUDY", paths["excel"], **_opts("excel"))
    export_to_json(sdk, "STUDY", paths["json"], **_opts("json"))
    export_to_duckdb_by_form(sdk, "STUDY", paths["duckdb"], **_opts("duckdb"))
    export_to_long_sql(
        sdk,
        "STUDY",
        "long_records",
        paths["sqlite_uri"],
        **_opts("long_sql"),
    )


@pytest.mark.parametrize("mode", ["incremental", "since"])
def test_simulation_incremental_and_since_across_all_six_exporters(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify sequential --incremental and --since runs preserve, upsert, mask PHI, and purge deletes."""
    out_dir = tmp_path / f"out_{mode}"
    out_dir.mkdir()
    ledger_dir = tmp_path / f"ledgers_{mode}"
    ledger_dir.mkdir()

    paths = {
        "csv": out_dir / "records.csv",
        "parquet": out_dir / "records.parquet",
        "excel": out_dir / "records.xlsx",
        "json": out_dir / "records.json",
        "duckdb": out_dir / "forms.duckdb",
        "sqlite_uri": f"sqlite:///{out_dir / 'long.db'}",
    }

    state = _make_study_state()
    with _make_sdk(state, monkeypatch=monkeypatch, phi_fields=["V_CUSTOM_MRN"]) as sdk:
        # 1. Initial sync (3 records: 601, 602, 603)
        _run_all_six_exporters(sdk, paths, mode=mode, since_ts=None, ledger_dir=ledger_dir)
        _assert_no_temp_artifacts(out_dir)

        csv_df = pd.read_csv(paths["csv"])
        assert set(csv_df["recordId"]) == {601, 602, 603}
        assert set(csv_df["subjectKey"]) == {"SUB-PHI-001", "SUB-PHI-002"}
        assert set(csv_df.loc[csv_df["recordId"].isin([601, 603]), "PATIENT_INITIALS"]) == {
            "***MASKED***"
        }
        assert set(csv_df.loc[csv_df["recordId"].isin([601, 603]), "V_CUSTOM_MRN"]) == {
            "***MASKED***"
        }

        # 2. Mutate simulation state:
        # - 601 stays untouched at 09:30Z (omitted by >= 2026-01-12 filter!)
        # - 602 is updated at 2026-01-15T10:00:00Z with updated V_WEIGHT, cleared V_NOTES, and new variable V_BMI
        # - 604 is newly created at 2026-01-15T10:05:00Z on F_DM with PHI field
        state.variables.append(
            {
                "variableId": 806,
                "variableName": "V_BMI",
                "variableKey": "V_BMI",
                "variableType": "Float",
                "formId": 302,
                "formKey": "F_VS",
                "studyKey": "STUDY",
                "label": "BMI",
            }
        )
        for rec in state.records:
            if rec["recordId"] == 602:
                rec["recordData"] = {"V_WEIGHT": 71.2, "V_BMI": 23.4}
                rec["dateModified"] = "2026-01-15T10:00:00Z"
                rec["lastUpdated"] = "2026-01-15T10:00:00Z"
        state.records.append(
            {
                "recordId": 604,
                "studyKey": "STUDY",
                "subjectKey": "SUB-PHI-003",
                "subjectId": 201,
                "formId": 301,
                "formKey": "F_DM",
                "visitId": 402,
                "intervalId": 502,
                "siteId": 101,
                "recordType": "Standard",
                "recordStatus": "Complete",
                "recordData": {
                    "V_AGE": 52,
                    "V_GENDER": "Male",
                    "V_CUSTOM_MRN": "MRN-99999",
                    "PATIENT_INITIALS": "R.K.",
                },
                "deleted": False,
                "dateCreated": "2026-01-15T10:05:00Z",
                "dateModified": "2026-01-15T10:05:00Z",
                "lastUpdated": "2026-01-15T10:05:00Z",
            }
        )

        _run_all_six_exporters(
            sdk,
            paths,
            mode=mode,
            since_ts="2026-01-12T00:00:00Z",
            ledger_dir=ledger_dir,
        )
        _assert_no_temp_artifacts(out_dir)

        # Verify CSV
        csv_df = pd.read_csv(paths["csv"]).sort_values("recordId").reset_index(drop=True)
        assert list(csv_df["recordId"]) == [601, 602, 603, 604]
        assert float(csv_df.loc[csv_df["recordId"] == 602, "V_WEIGHT"].iloc[0]) == 71.2
        assert float(csv_df.loc[csv_df["recordId"] == 602, "V_BMI"].iloc[0]) == 23.4
        assert pd.isna(csv_df.loc[csv_df["recordId"] == 602, "V_NOTES"].iloc[0])
        assert set(csv_df["subjectKey"]) == {"SUB-PHI-001", "SUB-PHI-002", "SUB-PHI-003"}
        assert set(csv_df.loc[csv_df["recordId"].isin([601, 603, 604]), "PATIENT_INITIALS"]) == {
            "***MASKED***"
        }
        assert set(csv_df.loc[csv_df["recordId"].isin([601, 603, 604]), "V_CUSTOM_MRN"]) == {
            "***MASKED***"
        }

        # Verify Parquet
        pq_df = pq.read_table(paths["parquet"]).to_pandas().sort_values("recordId")
        assert list(pq_df["recordId"]) == [601, 602, 603, 604]
        assert float(pq_df.loc[pq_df["recordId"] == 602, "V_WEIGHT"].iloc[0]) == 71.2
        assert float(pq_df.loc[pq_df["recordId"] == 602, "V_BMI"].iloc[0]) == 23.4
        assert set(pq_df.loc[pq_df["recordId"].isin([601, 603, 604]), "PATIENT_INITIALS"]) == {
            "***MASKED***"
        }
        assert set(pq_df.loc[pq_df["recordId"].isin([601, 603, 604]), "V_CUSTOM_MRN"]) == {
            "***MASKED***"
        }

        # Verify Excel (flat Study Records sheet)
        excel_df = pd.read_excel(paths["excel"]).sort_values("recordId").reset_index(drop=True)
        assert list(excel_df["recordId"]) == [601, 602, 603, 604]
        assert float(excel_df.loc[excel_df["recordId"] == 602, "V_WEIGHT"].iloc[0]) == 71.2
        assert float(excel_df.loc[excel_df["recordId"] == 602, "V_BMI"].iloc[0]) == 23.4
        assert pd.isna(excel_df.loc[excel_df["recordId"] == 602, "V_NOTES"].iloc[0])
        assert set(
            excel_df.loc[excel_df["recordId"].isin([601, 603, 604]), "PATIENT_INITIALS"]
        ) == {"***MASKED***"}
        assert set(excel_df.loc[excel_df["recordId"].isin([601, 603, 604]), "V_CUSTOM_MRN"]) == {
            "***MASKED***"
        }

        # Verify JSON
        json_rows = json.loads(paths["json"].read_text(encoding="utf-8"))
        by_id = {int(r["recordId"]): r for r in json_rows}
        assert set(by_id) == {601, 602, 603, 604}
        assert by_id[602]["V_WEIGHT"] == 71.2
        assert by_id[602]["V_BMI"] == 23.4
        assert by_id[602].get("V_NOTES") is None
        for rid in (601, 603, 604):
            assert by_id[rid]["V_CUSTOM_MRN"] == "***MASKED***"
            assert by_id[rid]["PATIENT_INITIALS"] == "***MASKED***"

        # Verify DuckDB by Form
        with duckdb.connect(str(paths["duckdb"]), read_only=True) as con:
            dm_duck = con.execute('SELECT * FROM F_DM ORDER BY "recordId"').df()
            vs_duck = con.execute('SELECT * FROM F_VS ORDER BY "recordId"').df()
        assert list(dm_duck["recordId"]) == [601, 603, 604]
        assert list(vs_duck["recordId"]) == [602]
        assert float(vs_duck.iloc[0]["V_WEIGHT"]) == 71.2
        assert float(vs_duck.iloc[0]["V_BMI"]) == 23.4
        assert set(dm_duck["V_CUSTOM_MRN"]) == {"***MASKED***"}
        assert set(dm_duck["PATIENT_INITIALS"]) == {"***MASKED***"}

        # Verify Long SQL (cleared variable V_NOTES for 602 must be removed!)
        engine = sa.create_engine(paths["sqlite_uri"])
        try:
            with engine.connect() as conn:
                long_df = pd.read_sql_table("long_records", conn)
        finally:
            engine.dispose()
        assert set(long_df["record_id"]) == {601, 602, 603, 604}
        rec602_vars = dict(
            zip(
                long_df.loc[long_df["record_id"] == 602, "variable_name"],
                long_df.loc[long_df["record_id"] == 602, "value"],
                strict=True,
            )
        )
        assert float(rec602_vars["V_WEIGHT"]) == 71.2
        assert float(rec602_vars["V_BMI"]) == 23.4
        assert "V_NOTES" not in rec602_vars
        mrn_vals = set(long_df.loc[long_df["variable_name"] == "V_CUSTOM_MRN", "value"])
        assert mrn_vals == {"***MASKED***"}
        init_vals = set(long_df.loc[long_df["variable_name"] == "PATIENT_INITIALS", "value"])
        assert init_vals == {"***MASKED***"}

        # 3. Empty incremental sync (no records modified at or after 2026-01-16T00:00:00Z)
        # Note: for incremental mode, advance watermark past 10:05Z by setting 2026-01-16 on ledger
        # OR verify that even when API returns 0 records (--since="2026-01-16T00:00:00Z" / ledger at 2026-01-16),
        # existing outputs remain completely intact!
        if mode == "incremental":
            for ledger_file in ledger_dir.glob("*_ledger.json"):
                provider = FileStateProvider(ledger_file)
                for study_state in provider.read_state().studies.values():
                    for stream_key in study_state.streams:
                        provider.set_last_timestamp(
                            "STUDY",
                            stream_key,
                            datetime(2026, 1, 16, 0, 0, tzinfo=UTC),
                            records_processed=0,
                        )

        _run_all_six_exporters(
            sdk,
            paths,
            mode=mode,
            since_ts="2026-01-16T00:00:00Z",
            ledger_dir=ledger_dir,
        )
        _assert_no_temp_artifacts(out_dir)

        assert list(pd.read_csv(paths["csv"]).sort_values("recordId")["recordId"]) == [
            601,
            602,
            603,
            604,
        ]
        assert list(
            pq.read_table(paths["parquet"]).to_pandas().sort_values("recordId")["recordId"]
        ) == [601, 602, 603, 604]
        assert sorted(pd.read_excel(paths["excel"])["recordId"].tolist()) == [
            601,
            602,
            603,
            604,
        ]
        assert len(json.loads(paths["json"].read_text(encoding="utf-8"))) == 4
        with duckdb.connect(str(paths["duckdb"]), read_only=True) as con:
            assert con.execute("SELECT COUNT(*) FROM F_DM").fetchone()[0] == 3
            assert con.execute("SELECT COUNT(*) FROM F_VS").fetchone()[0] == 1
        engine = sa.create_engine(paths["sqlite_uri"])
        try:
            with engine.connect() as conn:
                assert set(pd.read_sql_table("long_records", conn)["record_id"]) == {
                    601,
                    602,
                    603,
                    604,
                }
        finally:
            engine.dispose()

        # 4. Voided Study Records reconciliation (603 on F_DM and 602 on F_VS become deleted=True)
        for rec in state.records:
            if rec["recordId"] in (602, 603):
                rec["deleted"] = True
                rec["dateModified"] = "2026-01-20T12:00:00Z"
                rec["lastUpdated"] = "2026-01-20T12:00:00Z"

        _run_all_six_exporters(
            sdk,
            paths,
            mode=mode,
            since_ts="2026-01-19T00:00:00Z",
            ledger_dir=ledger_dir,
            include_deleted=False,
            idempotent=True,
        )
        _assert_no_temp_artifacts(out_dir)

        assert sorted(pd.read_csv(paths["csv"])["recordId"].tolist()) == [601, 604]
        assert sorted(pq.read_table(paths["parquet"]).to_pandas()["recordId"].tolist()) == [
            601,
            604,
        ]
        assert sorted(pd.read_excel(paths["excel"])["recordId"].tolist()) == [601, 604]
        json_after_delete = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert sorted(int(r["recordId"]) for r in json_after_delete) == [
            601,
            604,
        ]
        with duckdb.connect(str(paths["duckdb"]), read_only=True) as con:
            assert sorted(con.execute('SELECT "recordId" FROM F_DM').df()["recordId"].tolist()) == [
                601,
                604,
            ]
            assert con.execute("SELECT COUNT(*) FROM F_VS").fetchone()[0] == 0
        engine = sa.create_engine(paths["sqlite_uri"])
        try:
            with engine.connect() as conn:
                assert sorted(
                    pd.read_sql_table("long_records", conn)["record_id"].unique().tolist()
                ) == [601, 604]
        finally:
            engine.dispose()


def test_full_snapshot_idempotent_overwrites_when_not_incremental(tmp_path: Path) -> None:
    """Verify full snapshot overwrite behavior is preserved when no incremental/--since/state_provider is active."""
    paths = {
        "csv": tmp_path / "snap.csv",
        "parquet": tmp_path / "snap.parquet",
        "excel": tmp_path / "snap.xlsx",
        "json": tmp_path / "snap.json",
        "duckdb": tmp_path / "snap.duckdb",
        "sqlite_uri": f"sqlite:///{tmp_path / 'snap.db'}",
    }
    state = _make_study_state()
    with _make_sdk(state) as sdk:
        _run_all_six_exporters(sdk, paths, mode="snapshot")
        assert len(pd.read_csv(paths["csv"])) == 3

        # Remove 601 and 603 completely from the Study snapshot (only 602 remains)
        state.records = [r for r in state.records if r["recordId"] == 602]
        _run_all_six_exporters(sdk, paths, mode="snapshot")

        assert list(pd.read_csv(paths["csv"])["recordId"]) == [602]
        assert list(pq.read_table(paths["parquet"]).to_pandas()["recordId"]) == [602]
        assert list(pd.read_excel(paths["excel"])["recordId"]) == [602]
        json_rows = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert [int(r["recordId"]) for r in json_rows] == [602]
        engine = sa.create_engine(paths["sqlite_uri"])
        try:
            with engine.connect() as conn:
                assert set(pd.read_sql_table("long_records", conn)["record_id"]) == {602}
        finally:
            engine.dispose()


def test_parquet_schema_widening_type_promotion_and_cleanup(tmp_path: Path) -> None:
    """Verify TabularParquetSink merges existing int64 column with float/string delta batches."""
    pq_path = tmp_path / "widening.parquet"
    sink_init = TabularParquetSink(
        str(pq_path), TabularSinkConfig(study_key="STUDY", idempotent=True, incremental=False)
    )
    sink_init.write_batch(
        [
            {"record_id": 1, "score": 10, "status": "ok"},
            {"record_id": 2, "score": 20, "status": "ok"},
        ],
        batch_id="b0",
    )
    sink_init.flush()
    sink_init.close()

    sink_inc = TabularParquetSink(
        str(pq_path), TabularSinkConfig(study_key="STUDY", idempotent=True, incremental=True)
    )
    sink_inc.write_batch([{"record_id": 2, "score": 25.75, "extra_col": "added"}], batch_id="b1")
    sink_inc.write_batch([{"record_id": 3, "score": 30.5, "status": "new"}], batch_id="b2")
    sink_inc.reconcile_deleted([{"record_id": 1}], batch_id="b_del")
    sink_inc.flush()
    sink_inc.close()

    _assert_no_temp_artifacts(tmp_path)
    merged = pq.read_table(pq_path).to_pandas().sort_values("record_id").reset_index(drop=True)
    assert list(merged["record_id"]) == [2, 3]
    assert list(merged["score"]) == [25.75, 30.5]
    assert merged.loc[merged["record_id"] == 2, "extra_col"].iloc[0] == "added"


def test_atomic_swap_preserves_existing_files_on_failure(tmp_path: Path) -> None:
    """Simulated write error during incremental run leaves prior files untouched and cleans up .tmp."""
    state = _make_study_state()
    csv_path = tmp_path / "atomic.csv"
    pq_path = tmp_path / "atomic.parquet"
    xlsx_path = tmp_path / "atomic.xlsx"
    json_path = tmp_path / "atomic.json"

    with _make_sdk(state) as sdk:
        export_to_csv(sdk, "STUDY", csv_path)
        export_to_parquet(sdk, "STUDY", pq_path)
        export_to_excel(sdk, "STUDY", xlsx_path)
        export_to_json(sdk, "STUDY", json_path)

        orig_csv = csv_path.read_bytes()
        orig_pq = pq_path.read_bytes()
        orig_xlsx = xlsx_path.read_bytes()
        orig_json = json_path.read_bytes()

        state.records[0]["recordData"]["V_AGE"] = 99
        state.records[0]["dateModified"] = "2026-02-01T00:00:00Z"

        with patch("os.replace", side_effect=OSError("Simulated disk full during atomic swap")):
            for fn, target in (
                (export_to_csv, csv_path),
                (export_to_parquet, pq_path),
                (export_to_excel, xlsx_path),
                (export_to_json, json_path),
            ):
                with pytest.raises(OSError, match="Simulated disk full"):
                    fn(sdk, "STUDY", target, modified_since="2026-01-20T00:00:00Z")

    assert csv_path.read_bytes() == orig_csv
    assert pq_path.read_bytes() == orig_pq
    assert xlsx_path.read_bytes() == orig_xlsx
    assert json_path.read_bytes() == orig_json
    _assert_no_temp_artifacts(tmp_path)


def test_quality_gate_watermark_capping_and_recovery(tmp_path: Path) -> None:
    """Verify apply_quality_gate never advances new_timestamp at or past rejected Study Records."""
    state = SimulationState()
    # F_DM requires V_AGE (Number) and V_GENDER (Coded) for 100% readiness score.
    # Step 1: Earliest record 701 (09:00Z) is an incomplete draft (missing V_GENDER -> 50% < 100%),
    # while later record 702 (10:00Z) is complete (100%).
    state.records = [
        {
            "recordId": 701,
            "studyKey": "STUDY",
            "subjectKey": "SUB1",
            "subjectId": 201,
            "formId": 301,
            "formKey": "F_DM",
            "visitId": 401,
            "intervalId": 501,
            "siteId": 101,
            "recordType": "Standard",
            "recordStatus": "Draft",
            "recordData": {"V_AGE": 41},  # Missing V_GENDER -> rejected!
            "deleted": False,
            "dateCreated": "2026-01-10T09:00:00Z",
            "dateModified": "2026-01-10T09:00:00Z",
            "lastUpdated": "2026-01-10T09:00:00Z",
        },
        {
            "recordId": 702,
            "studyKey": "STUDY",
            "subjectKey": "SUB1",
            "subjectId": 201,
            "formId": 301,
            "formKey": "F_DM",
            "visitId": 401,
            "intervalId": 501,
            "siteId": 101,
            "recordType": "Standard",
            "recordStatus": "Complete",
            "recordData": {"V_AGE": 50, "V_GENDER": "Female"},
            "deleted": False,
            "dateCreated": "2026-01-10T10:00:00Z",
            "dateModified": "2026-01-10T10:00:00Z",
            "lastUpdated": "2026-01-10T10:00:00Z",
        },
    ]

    csv_path = tmp_path / "gated.csv"
    ledger_path = tmp_path / "gated_ledger.json"
    provider = FileStateProvider(ledger_path)

    with _make_sdk(state) as sdk:
        export_to_csv(
            sdk,
            "STUDY",
            csv_path,
            incremental=True,
            state_provider=provider,
            quality_gate_enabled=True,
            min_schema_readiness_score=100.0,
        )
        # 702 exported, 701 dropped, and watermark MUST remain None (< 09:00Z)!
        assert list(pd.read_csv(csv_path)["recordId"]) == [702]
        assert provider.get_last_timestamp("STUDY", "csv") is None

        # Step 2: Fix 701 (09:00Z), add same-timestamp accepted 703 (10:30Z) + rejected 704 (10:30Z),
        # and add newer accepted 705 (11:00Z).
        state.records[0]["recordData"] = {"V_AGE": 41, "V_GENDER": "Male"}
        state.records.extend(
            [
                {
                    **copy.deepcopy(state.records[1]),
                    "recordId": 703,
                    "recordData": {"V_AGE": 60, "V_GENDER": "Female"},
                    "dateModified": "2026-01-10T10:30:00Z",
                    "lastUpdated": "2026-01-10T10:30:00Z",
                },
                {
                    **copy.deepcopy(state.records[1]),
                    "recordId": 704,
                    "recordData": {"V_AGE": 61},  # Rejected at 10:30Z!
                    "dateModified": "2026-01-10T10:30:00Z",
                    "lastUpdated": "2026-01-10T10:30:00Z",
                },
                {
                    **copy.deepcopy(state.records[1]),
                    "recordId": 705,
                    "recordData": {"V_AGE": 62, "V_GENDER": "Male"},
                    "dateModified": "2026-01-10T11:00:00Z",
                    "lastUpdated": "2026-01-10T11:00:00Z",
                },
            ]
        )

        export_to_csv(
            sdk,
            "STUDY",
            csv_path,
            incremental=True,
            state_provider=provider,
            quality_gate_enabled=True,
            min_schema_readiness_score=100.0,
        )
        # Accepted records 701, 702, 703, 705 are exported without duplicates;
        # Watermark MUST cap at 10:00Z (strictly < 10:30Z where 704 was rejected)!
        assert sorted(pd.read_csv(csv_path)["recordId"].tolist()) == [701, 702, 703, 705]
        assert provider.get_last_timestamp("STUDY", "csv") == datetime(
            2026, 1, 10, 10, 0, tzinfo=UTC
        )

        # Step 3: Fix 704 (still at 10:30Z). Next incremental run fetches >= 10:00Z,
        # upserts 702/703/704/705 cleanly while preserving 701, and advances watermark to 11:00Z!
        for rec in state.records:
            if rec["recordId"] == 704:
                rec["recordData"] = {"V_AGE": 61, "V_GENDER": "Female"}

        export_to_csv(
            sdk,
            "STUDY",
            csv_path,
            incremental=True,
            state_provider=provider,
            quality_gate_enabled=True,
            min_schema_readiness_score=100.0,
        )
        assert sorted(pd.read_csv(csv_path)["recordId"].tolist()) == [701, 702, 703, 704, 705]
        assert provider.get_last_timestamp("STUDY", "csv") == datetime(
            2026, 1, 10, 11, 0, tzinfo=UTC
        )


def test_initial_sync_failure_and_running_crash_recovery_end_to_end(tmp_path: Path) -> None:
    """Initial failure keeps last_timestamp None, and crashed RUNNING stream recovers on retry."""
    state = _make_study_state()
    sim_transport = SimulationTransport(state)
    should_fail = {"active": True}

    class _FlakyRecordsTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if should_fail["active"] and "/records" in request.url.path:
                raise httpx.ConnectError("Simulated API outage on initial sync", request=request)
            return sim_transport.handle_request(request)

    ledger_path = tmp_path / "recovery_ledger.json"
    provider = FileStateProvider(ledger_path)
    pq_path = tmp_path / "recovery.parquet"

    with _make_sdk(state, transport=_FlakyRecordsTransport()) as sdk:
        # 1. Initial sync fails due to API outage -> last_timestamp MUST remain None
        with pytest.raises(Exception):
            export_to_parquet(
                sdk,
                "STUDY",
                pq_path,
                incremental=True,
                state_provider=provider,
                max_retries=1,
            )

        assert provider.get_last_timestamp("STUDY", "parquet") is None
        assert provider.get_last_status("STUDY", "parquet") == StreamExecutionStatus.FAILED

        # 2. Simulate an abrupt process crash (e.g. OOM) leaving ledger stuck in RUNNING with expired lease and None watermark
        crashed = LedgerState(
            studies={
                "STUDY": StudyState(
                    streams={
                        "parquet": StreamState(
                            last_timestamp=None,
                            records_processed=0,
                            last_run_status=StreamExecutionStatus.RUNNING,
                            lease_expires_at=datetime(2020, 1, 1, tzinfo=UTC),
                        )
                    }
                )
            }
        )
        ledger_path.write_text(crashed.model_dump_json(indent=2), encoding="utf-8")
        assert provider.get_last_status("STUDY", "parquet") == StreamExecutionStatus.RUNNING

        # 3. Restore API and retry -> transitions RUNNING -> RUNNING -> SUCCESS and exports all historical records!
        should_fail["active"] = False
        export_to_parquet(
            sdk,
            "STUDY",
            pq_path,
            incremental=True,
            state_provider=provider,
        )

        assert provider.get_last_status("STUDY", "parquet") == StreamExecutionStatus.SUCCESS
        assert provider.get_last_timestamp("STUDY", "parquet") == datetime(
            2026, 1, 10, 9, 40, tzinfo=UTC
        )
        exported_ids = sorted(pq.read_table(pq_path).to_pandas()["recordId"].tolist())
        assert exported_ids == [601, 602, 603]
