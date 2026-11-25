"""Unit tests for idempotent incremental contracts and incremental SDTM Record lineage."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

import imednet_sinks.document as doc_mod
import imednet_sinks.graph as graph_mod
import imednet_sinks.warehouse as wh_mod
from imednet.errors import ExportBatchError, ExportConfigurationError
from imednet.integrations.export import (
    TabularCSVSink,
    TabularSinkConfig,
    export_to_csv,
    export_to_duckdb,
    export_to_duckdb_by_form,
    export_to_excel,
    export_to_json,
    export_to_parquet,
    export_to_sql,
    export_to_sql_by_form,
)
from imednet.integrations.parquet import export_to_hive_parquet
from imednet.integrations.sink_base import (
    SinkConfig,
    _extract_safe_config_options,
    run_streaming_export,
)
from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.records import Record
from imednet.models.sdtm import transform_records_to_sdtm, transform_records_to_sdtm_rows
from imednet.models.variables import Variable
from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig
from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig
from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig
from imednet_workflows.state_ledger import FileStateProvider


def _build_sdtm_sdk(
    upstream_by_study: dict[str, dict[int, Record]],
    default_study: str = "STUDY_B",
) -> MagicMock:
    """Create a stateful mock SDK supporting multi-study watermarks, forms, and variables."""
    forms = [
        Form(
            form_id=301,
            form_key="AE",
            form_name="Adverse Events",
            form_type="Standard",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
        ),
        Form(
            form_id=302,
            form_key="VS",
            form_name="Vital Signs",
            form_type="Standard",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
        ),
    ]
    intervals = [
        Interval(
            interval_id=401,
            interval_name="Visit 1",
            interval_sequence=1,
            interval_description="Visit 1",
            interval_group_name="Core",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
            forms=[
                FormSummary(form_id=301, form_key="AE", form_name="Adverse Events"),
                FormSummary(form_id=302, form_key="VS", form_name="Vital Signs"),
            ],
        )
    ]
    variables = [
        Variable(
            variable_id=1,
            variable_name="AETERM",
            label="Reported Term for the Adverse Event",
            variable_type="text",
            form_id=301,
            form_key="AE",
            required=True,
        ),
        Variable(
            variable_id=2,
            variable_name="patientName",
            label="Participant Name",
            variable_type="text",
            form_id=301,
            form_key="AE",
            required=True,
        ),
        Variable(
            variable_id=3,
            variable_name="VSTESTCD",
            label="Vital Signs Test Short Name",
            variable_type="text",
            form_id=302,
            form_key="VS",
        ),
        Variable(
            variable_id=4,
            variable_name="VSORRES",
            label="Result or Finding in Original Units",
            variable_type="float",
            form_id=302,
            form_key="VS",
        ),
    ]

    def _list_records(study_key: str | None = None, **kw: Any) -> Any:
        from imednet.integrations.sink_base import (
            extract_record_timestamp,
            normalize_watermark_datetime,
        )

        sk = study_key or kw.get("studyKey") or default_study
        items = list(upstream_by_study.get(str(sk), {}).values())
        flt = str(kw.get("filter", "") or "")
        if kw.get("deleted") is False:
            items = [r for r in items if not r.deleted]
        elif kw.get("deleted") is True:
            items = [r for r in items if r.deleted]
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
                wm_dt = normalize_watermark_datetime(clause.split(">=", 1)[1])
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


def test_sink_config_rejects_non_idempotent_incremental_or_state_provider_before_network(
    tmp_path: Path,
) -> None:
    """SinkConfig and _extract_safe_config_options reject idempotent=False with incremental or state_provider."""
    ledger = FileStateProvider(ledger_path=tmp_path / "ledger.json")
    sdk = MagicMock()

    with pytest.raises(ExportConfigurationError, match="dateModified>="):
        SinkConfig(study_key="S1", incremental=True, idempotent=False)

    with pytest.raises(ExportConfigurationError, match="idempotent=True"):
        SinkConfig(study_key="S1", state_provider=ledger, idempotent=False)

    with pytest.raises(ValueError, match="idempotent=True"):
        TabularSinkConfig(study_key="S1", incremental=True, idempotent=False)

    with pytest.raises(ExportConfigurationError, match="idempotent=True"):
        MongoDbSinkConfig(
            study_key="S1",
            uri="mongodb://localhost:27017",
            database="db",
            collection="col",
            incremental=True,
            idempotent=False,
        )

    with pytest.raises(ExportConfigurationError, match="idempotent=True"):
        Neo4jSinkConfig(
            study_key="S1",
            uri="bolt://localhost:7687",
            auth=("neo4j", "pwd"),
            state_provider=ledger,
            idempotent=False,
        )

    with pytest.raises(ExportConfigurationError, match="idempotent=True"):
        SnowflakeSinkConfig(
            study_key="S1",
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="TBL",
            incremental=True,
            idempotent=False,
        )

    # _extract_safe_config_options rejects spoofed duck-typed config objects before RecordsEndpoint
    spoofed_cfg = MagicMock(
        study_key="S1",
        incremental=True,
        idempotent=False,
        state_provider=None,
        batch_size=100,
    )
    with pytest.raises(ExportConfigurationError, match="dateModified>="):
        _extract_safe_config_options(spoofed_cfg)

    dummy_sink = MagicMock()
    with pytest.raises(ExportConfigurationError):
        run_streaming_export(sdk, "S1", dummy_sink, config=spoofed_cfg)

    with pytest.raises(ExportConfigurationError):
        export_to_csv(
            sdk,
            "S1",
            str(tmp_path / "out.csv"),
            incremental=True,
            idempotent=False,
        )

    assert sdk.records.list.call_count == 0


def test_non_incremental_non_idempotent_append_exports_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-incremental idempotent=False exports execute append writes without regression across all sinks."""
    duckdb = pytest.importorskip("duckdb")
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    rec = Record(
        record_id=10,
        study_key="S_APPEND",
        subject_key="SUBJ-01",
        form_id=301,
        form_key="AE",
        deleted=False,
        date_created="2026-06-01T08:00:00Z",
        date_modified="2026-06-01T09:00:00Z",
        record_data={"AETERM": "Headache"},
    )
    sdk = _build_sdtm_sdk({"S_APPEND": {10: rec}}, default_study="S_APPEND")

    # CSV append
    csv_path = tmp_path / "append.csv"
    cfg_tab = TabularSinkConfig(study_key="S_APPEND", idempotent=False)
    with TabularCSVSink(csv_path, cfg_tab) as csv_sink:
        csv_sink.write_batch([rec], batch_id="b1")
        csv_sink.write_batch([rec], batch_id="b2")
    assert len(pd.read_csv(csv_path)) == 2

    # DuckDB non-incremental idempotent=False
    ddb_path = tmp_path / "append.duckdb"
    export_to_duckdb(sdk, "S_APPEND", str(ddb_path), "records", idempotent=False)
    export_to_duckdb(sdk, "S_APPEND", str(ddb_path), "records", idempotent=False)
    with duckdb.connect(str(ddb_path)) as con:
        row_cnt = con.execute("SELECT COUNT(*) FROM records").fetchone()
        assert row_cnt is not None and row_cnt[0] == 2

    # SQL non-incremental idempotent=False
    sql_path = tmp_path / "append.sqlite"
    export_to_sql(
        sdk,
        "S_APPEND",
        "records",
        f"sqlite:///{sql_path}",
        idempotent=False,
        quality_gate_enabled=False,
    )
    export_to_sql(
        sdk,
        "S_APPEND",
        "records",
        f"sqlite:///{sql_path}",
        idempotent=False,
        quality_gate_enabled=False,
    )
    with sqlite3.connect(sql_path) as con:
        assert con.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 2

    # MongoDB insert_many
    pymongo_mod = MagicMock()
    collection = MagicMock()
    collection.insert_many.return_value = MagicMock(inserted_ids=["1"])
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    client = MagicMock()
    client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)
    with MongoDbExportSink(
        MongoDbSinkConfig(
            study_key="S_APPEND",
            uri="mongodb://localhost:27017",
            database="db",
            collection="col",
            idempotent=False,
        )
    ) as m_sink:
        assert m_sink.write_batch([rec], batch_id="b1") == 1
    collection.insert_many.assert_called_once()

    # Neo4j CREATE cypher
    neo4j_mod = MagicMock()
    session = MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    driver = MagicMock()
    driver.session.return_value = session_cm
    neo4j_mod.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)
    with Neo4jExportSink(
        Neo4jSinkConfig(
            study_key="S_APPEND",
            uri="bolt://localhost:7687",
            auth=("neo4j", "pwd"),
            idempotent=False,
        )
    ) as n_sink:
        assert n_sink.write_batch([rec], batch_id="b1") == 1
    assert any("CREATE (r:Record" in c.args[0] for c in session.run.call_args_list)

    # Snowflake COPY INTO ... FORCE = TRUE
    sf_mod = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cursor
    sf_mod.connect.return_value = conn
    monkeypatch.setattr(
        wh_mod,
        "_require_optional_dep",
        lambda pkg, _ex: (
            sf_mod if pkg == "snowflake.connector" else (pq if "parquet" in pkg else pa)
        ),
    )
    with SnowflakeExportSink(
        SnowflakeSinkConfig(
            study_key="S_APPEND",
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="RECORDS",
            idempotent=False,
            local_staging_dir=tmp_path / "sf_stg",
        )
    ) as sf_sink:
        assert sf_sink.write_batch([rec], batch_id="b1") == 1
    assert any("FORCE = TRUE" in c.args[0] for c in cursor.execute.call_args_list)


def test_sdtm_snapshot_omits_record_id_across_all_domains_while_preserve_retains_it() -> None:
    """Snapshot SDTM transforms omit RECORD_ID across AE, CM, DD, DM, DV, VS; preserve_record_id retains it."""
    payloads = {
        "AE": {"AETERM": "Headache"},
        "CM": {"CMTRT": "Ibuprofen"},
        "DD": {"DDTESTCD": "PRCDTH", "DDORRES": "Disease"},
        "DM": {"AGE": 55, "SEX": "M"},
        "DV": {"DVTERM": "Late lab draw"},
        "VS": {"VSTESTCD": "DIABP", "VSORRES": "80"},
    }
    for domain, data in payloads.items():
        rec = Record(
            record_id=77,
            study_key="S_SNAP",
            subject_key="SUBJ-77",
            form_id=1,
            form_key=domain,
            deleted=False,
            record_data=data,
        )
        snap_rows = transform_records_to_sdtm_rows([rec], domain=domain, study_key="S_SNAP")
        assert len(snap_rows) == 1
        assert "RECORD_ID" not in snap_rows[0]

        snap_df = transform_records_to_sdtm([rec], domain=domain, study_key="S_SNAP")
        assert "RECORD_ID" not in snap_df.columns

        inc_rows = transform_records_to_sdtm_rows(
            [rec], domain=domain, study_key="S_SNAP", preserve_record_id=True
        )
        assert inc_rows[0]["RECORD_ID"] == 77

        inc_df = transform_records_to_sdtm(
            [rec], domain=domain, study_key="S_SNAP", preserve_record_id=True
        )
        assert list(inc_df["RECORD_ID"]) == [77]


def test_tabular_and_hive_parquet_incremental_sdtm_syncs_lineage_isolation_and_quality_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Incremental as_sdtm=True across CSV, Parquet, Hive Parquet, Excel, JSON, SQL, and DuckDB preserves lineage."""
    pytest.importorskip("pyarrow")
    pytest.importorskip("openpyxl")
    duckdb = pytest.importorskip("duckdb")
    import pyarrow.parquet as pq

    upstream_a = {
        1: Record(
            record_id=1,
            study_key="STUDY_A",
            subject_key="SUBJ-A1",
            visit_id=401,
            form_id=301,
            form_key="AE",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            record_data={"AETERM": "Study A Intact Event", "patientName": "Alice Secret"},
        )
    }
    upstream_b = {
        1: Record(
            record_id=1,
            study_key="STUDY_B",
            subject_key="SUBJ-01",
            visit_id=401,
            form_id=301,
            form_key="AE",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 1, 8, 0, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            record_data={"AETERM": "Initial Rash", "patientName": "Bob Secret"},
        ),
        2: Record(
            record_id=2,
            study_key="STUDY_B",
            subject_key="SUBJ-01",
            visit_id=401,
            form_id=301,
            form_key="AE",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 1, 8, 30, tzinfo=UTC),
            date_modified=datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
            record_data={"AETERM": "Initial Nausea", "patientName": "Bob Secret"},
        ),
    }
    sdk = _build_sdtm_sdk({"STUDY_A": upstream_a, "STUDY_B": upstream_b})

    ledger_dir = tmp_path / "ledgers"
    csv_path = tmp_path / "shared_ae.csv"
    pq_path = tmp_path / "shared_ae.parquet"
    xl_path = tmp_path / "shared_ae.xlsx"
    json_path = tmp_path / "shared_ae.json"
    hive_dir = tmp_path / "hive_sdtm"
    sql_uri = f"sqlite:///{tmp_path / 'shared_ae.sqlite'}"
    sql_by_form_uri = f"sqlite:///{tmp_path / 'by_form_ae.sqlite'}"
    ddb_path = tmp_path / "shared_ae.duckdb"
    ddb_by_form_path = tmp_path / "by_form_ae.duckdb"

    def _sync_study(study_key: str) -> None:
        common: dict[str, Any] = {
            "incremental": True,
            "ledger_path": str(ledger_dir),
            "as_sdtm": True,
            "sdtm_domain": "AE",
            "batch_size": 1,
        }
        export_to_csv(sdk, study_key, str(csv_path), quality_gate_enabled=False, **common)
        export_to_parquet(sdk, study_key, str(pq_path), **common)
        export_to_excel(sdk, study_key, str(xl_path), **common)
        export_to_json(sdk, study_key, str(json_path), **common)
        export_to_sql(sdk, study_key, "ae", sql_uri, quality_gate_enabled=False, **common)
        export_to_sql_by_form(sdk, study_key, sql_by_form_uri, **common)
        export_to_duckdb(sdk, study_key, str(ddb_path), "ae", **common)
        export_to_duckdb_by_form(sdk, study_key, str(ddb_by_form_path), **common)
        export_to_hive_parquet(
            sdk,
            study_key,
            str(hive_dir),
            config=SinkConfig(
                study_key=study_key,
                incremental=True,
                state_provider=FileStateProvider(ledger_path=tmp_path / "hive_ledger.json"),
                as_sdtm=True,
                sdtm_domain="AE",
                batch_size=1,
            ),
        )

    # Seed STUDY_A (RECORD_ID=1) and Run 1 of STUDY_B (RECORD_ID=1, 2) into shared destinations!
    _sync_study("STUDY_A")
    _sync_study("STUDY_B")
    # Zero-change boundary replay on STUDY_B must not duplicate records or bump AESEQ
    _sync_study("STUDY_B")

    # Run 2 of STUDY_B: void RECORD_ID=1, update RECORD_ID=2 at boundary/newer ts, add RECORD_ID=3
    upstream_b[1] = upstream_b[1].model_copy(
        update={"deleted": True, "date_modified": datetime(2026, 6, 1, 12, 0, tzinfo=UTC)}
    )
    upstream_b[2] = upstream_b[2].model_copy(
        update={
            "record_data": {"AETERM": "Updated Nausea", "patientName": "Bob Secret"},
            "date_modified": datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
        }
    )
    upstream_b[3] = Record(
        record_id=3,
        study_key="STUDY_B",
        subject_key="SUBJ-01",
        visit_id=401,
        form_id=301,
        form_key="AE",
        record_status="Complete",
        deleted=False,
        date_created=datetime(2026, 6, 1, 12, 30, tzinfo=UTC),
        date_modified=datetime(2026, 6, 1, 12, 30, tzinfo=UTC),
        record_data={"AETERM": "New Migraine", "patientName": "Bob Secret"},
    )
    _sync_study("STUDY_B")

    def _assert_df_state(df: pd.DataFrame, *, check_study_a: bool = True) -> None:
        if check_study_a:
            a_rows = df[df["STUDYID"] == "STUDY_A"]
            assert len(a_rows) == 1
            assert int(a_rows.iloc[0]["RECORD_ID"]) == 1
            assert a_rows.iloc[0]["AETERM"] == "Study A Intact Event"

        b_rows = df[df["STUDYID"] == "STUDY_B"].sort_values("RECORD_ID")
        assert list(b_rows["RECORD_ID"].astype(int)) == [2, 3]
        assert list(b_rows["AETERM"]) == ["Updated Nausea", "New Migraine"]
        # Subject-sorted batch_size=1 orders "Initial Nausea" (RECORD_ID=2 -> AESEQ=1)
        # before "Initial Rash" (RECORD_ID=1 -> AESEQ=2, voided in Run 2), so RECORD_ID=3 gets AESEQ=3.
        assert list(b_rows["AESEQ"].astype(int)) == [1, 3]
        if "PATIENTNAME" in b_rows.columns:
            assert (b_rows["PATIENTNAME"] == "***MASKED***").all()

    _assert_df_state(pd.read_csv(csv_path))
    _assert_df_state(pd.read_parquet(pq_path))
    _assert_df_state(pd.read_excel(xl_path))
    _assert_df_state(pd.DataFrame(json.loads(json_path.read_text(encoding="utf-8"))))

    with sqlite3.connect(tmp_path / "shared_ae.sqlite") as con:
        _assert_df_state(pd.read_sql_query("SELECT * FROM ae", con))
    with sqlite3.connect(tmp_path / "by_form_ae.sqlite") as con:
        _assert_df_state(pd.read_sql_query("SELECT * FROM ae", con))

    with duckdb.connect(str(ddb_path)) as con:
        _assert_df_state(con.execute("SELECT * FROM ae").df())
    with duckdb.connect(str(ddb_by_form_path)) as con:
        _assert_df_state(con.execute("SELECT * FROM ae").df())

    hive_b_files = sorted((hive_dir / "study_key=STUDY_B" / "form_key=AE").rglob("*.parquet"))
    hive_b_df = pd.concat([pq.read_table(f).to_pandas() for f in hive_b_files], ignore_index=True)
    _assert_df_state(hive_b_df, check_study_a=False)
    hive_a_files = sorted((hive_dir / "study_key=STUDY_A" / "form_key=AE").rglob("*.parquet"))
    hive_a_df = pd.concat([pq.read_table(f).to_pandas() for f in hive_a_files], ignore_index=True)
    assert len(hive_a_df) == 1 and int(hive_a_df.iloc[0]["RECORD_ID"]) == 1

    # Verify quality-gate watermark holdback remains intact on incremental SDTM runs
    qg_ledger = FileStateProvider(ledger_path=tmp_path / "qg_ledger.json")
    qg_csv = tmp_path / "qg_ae.csv"
    upstream_qg = {
        10: Record(
            record_id=10,
            study_key="STUDY_QG",
            subject_key="SUBJ-01",
            visit_id=401,
            form_id=301,
            form_key="AE",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 2, 8, 0, tzinfo=UTC),
            date_modified=datetime(2026, 6, 2, 10, 0, tzinfo=UTC),
            record_data={"AETERM": "Valid Early", "patientName": "Alice"},
        ),
        11: Record(
            record_id=11,
            study_key="STUDY_QG",
            subject_key="SUBJ-01",
            visit_id=401,
            form_id=301,
            form_key="AE",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 2, 8, 0, tzinfo=UTC),
            date_modified=datetime(2026, 6, 2, 10, 30, tzinfo=UTC),
            record_data={"AETERM": "Rejected Middle"},  # missing patientName -> Score 50 < 100
        ),
        12: Record(
            record_id=12,
            study_key="STUDY_QG",
            subject_key="SUBJ-01",
            visit_id=401,
            form_id=301,
            form_key="AE",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 2, 8, 0, tzinfo=UTC),
            date_modified=datetime(2026, 6, 2, 11, 0, tzinfo=UTC),
            record_data={"AETERM": "Valid Late", "patientName": "Alice"},
        ),
    }
    sdk_qg = _build_sdtm_sdk({"STUDY_QG": upstream_qg}, default_study="STUDY_QG")
    export_to_csv(
        sdk_qg,
        "STUDY_QG",
        str(qg_csv),
        config=TabularSinkConfig(
            study_key="STUDY_QG",
            incremental=True,
            state_provider=qg_ledger,
            as_sdtm=True,
            sdtm_domain="AE",
            quality_gate_enabled=True,
        ),
    )
    # Watermark is held back at or below min_rejected_ts (10:30Z), never advancing to 11:00Z!
    qg_streams = qg_ledger.read_state().studies["STUDY_QG"].streams
    committed_qg_ts = next(iter(qg_streams.values())).last_timestamp
    assert committed_qg_ts is not None
    assert committed_qg_ts <= datetime(2026, 6, 2, 10, 30, tzinfo=UTC)

    # Verify failed reconcile_deleted on TabularSQLSink raises ExportBatchError & rolls back checkpoint
    rb_ledger = FileStateProvider(ledger_path=tmp_path / "rb_ledger.json")
    rb_sql_uri = f"sqlite:///{tmp_path / 'rb.sqlite'}"
    upstream_rb = {
        1: Record(
            record_id=1,
            study_key="STUDY_RB",
            subject_key="SUBJ-01",
            visit_id=401,
            form_id=301,
            form_key="AE",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2026, 6, 3, 8, 0, tzinfo=UTC),
            date_modified=datetime(2026, 6, 3, 9, 0, tzinfo=UTC),
            record_data={"AETERM": "Baseline"},
        )
    }
    sdk_rb = _build_sdtm_sdk({"STUDY_RB": upstream_rb}, default_study="STUDY_RB")
    export_to_sql(
        sdk_rb,
        "STUDY_RB",
        "ae",
        rb_sql_uri,
        config=TabularSinkConfig(
            study_key="STUDY_RB",
            incremental=True,
            state_provider=rb_ledger,
            as_sdtm=True,
            sdtm_domain="AE",
        ),
    )
    rb_stream_name = next(iter(rb_ledger.read_state().studies["STUDY_RB"].streams))
    prior_rb_ts = rb_ledger.get_last_timestamp("STUDY_RB", rb_stream_name)
    assert prior_rb_ts == datetime(2026, 6, 3, 9, 0, tzinfo=UTC)

    upstream_rb[1] = upstream_rb[1].model_copy(
        update={"deleted": True, "date_modified": datetime(2026, 6, 3, 10, 0, tzinfo=UTC)}
    )
    monkeypatch.setattr(
        "imednet.integrations.export.TabularSQLSink._delete_existing_record_ids",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("SQL delete failed")),
    )
    with pytest.raises(ExportBatchError):
        export_to_sql(
            sdk_rb,
            "STUDY_RB",
            "ae",
            rb_sql_uri,
            config=TabularSinkConfig(
                study_key="STUDY_RB",
                incremental=True,
                state_provider=rb_ledger,
                as_sdtm=True,
                sdtm_domain="AE",
            ),
        )
    assert rb_ledger.get_last_timestamp("STUDY_RB", rb_stream_name) == prior_rb_ts
