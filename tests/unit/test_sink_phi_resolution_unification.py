"""Unit and integration tests for unified sink PHI resolution across store origins."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pandas as pd
import pyarrow.parquet as pq
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from imednet import ImednetSDK
from imednet.core.context import (
    get_base_url_context,
    reset_base_url_context,
    reset_security_key_context,
    set_base_url_context,
    set_security_key_context,
)
from imednet.integrations.enrichment import (
    EnrichmentPipeline,
    resolve_sink_enrichment_pipeline,
    resolve_sink_phi_keys,
    resolve_study_configuration,
)
from imednet.integrations.export import (
    TabularCSVSink,
    TabularDuckDBSink,
    TabularParquetSink,
    TabularSinkConfig,
    TabularSQLSink,
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
from imednet.integrations.sink_base import (
    ExportSink,
    SinkConfig,
    redact_batch_records,
    run_streaming_export,
)
from imednet.models.records import Record
from imednet.models.study_config import MappingRule, StudyConfiguration
from imednet.simulation import SimulationState, create_simulation_transport
from imednet.spi import sinks as spi_sinks
from imednet.utils.tabular import MASKED_SENTINEL
from imednet_workflows.config_version_control import ConfigVersionStore

MARKER_MRN = "MARKER_MRN_ALPHA"
MARKER_CHART = "MARKER_CHART_BETA"
MARKER_SSN = "000-00-0000"


class CustomSpiSink(ExportSink):
    """Custom ExportSink subclass relying on __init_subclass__ auto-redaction."""

    def __init__(self, config: SinkConfig) -> None:
        super().__init__(config)
        self.batches: list[list[Any]] = []

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        _ = batch_id
        batch_items = list(records)
        self.batches.append(batch_items)
        return len(batch_items)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def _build_simulation_sdk(
    study_keys: Sequence[str],
    *,
    base_url: str = "https://edc.trn.imednetapi.com",
    security_key: str = "sim-sec-key",  # pragma: allowlist secret
) -> tuple[ImednetSDK, SimulationState]:
    """Create a simulation ImednetSDK populated with custom PHI Variables and Records."""
    state = SimulationState()
    state.studies = []
    state.forms = []
    state.intervals = []
    state.variables = []
    state.subjects = []
    state.visits = []
    state.records = []

    for idx, sk in enumerate(study_keys, start=1):
        form_id = 300 + idx
        interval_id = 400 + idx
        visit_id = 500 + idx
        state.studies.append({"studyKey": sk, "studyName": f"Study {sk}"})
        state.forms.append(
            {
                "studyKey": sk,
                "formId": form_id,
                "formKey": "F_DM",
                "formName": "Demographics",
                "formType": "CRF",
                "subjectRecordReport": True,
                "unscheduledVisit": False,
                "disabled": False,
            }
        )
        state.intervals.append(
            {
                "studyKey": sk,
                "intervalId": interval_id,
                "intervalName": "Screening",
                "intervalSequence": 1,
                "disabled": False,
                "forms": [{"formId": form_id, "formKey": "F_DM", "formName": "Demographics"}],
            }
        )
        for v_idx, (v_name, v_label) in enumerate(
            [
                ("medicalRecordNumber", "Medical Record #"),
                ("siteChartId", "Site Chart Identifier"),
                ("ssn", "Social Security Number"),
                ("dateOfBirth", "Date of Birth"),
                ("clinicalScore", "Clinical Score"),
            ],
            start=1,
        ):
            state.variables.append(
                {
                    "studyKey": sk,
                    "variableId": idx * 100 + v_idx,
                    "variableName": v_name,
                    "variableType": "TEXT",
                    "formId": form_id,
                    "formKey": "F_DM",
                    "formName": "Demographics",
                    "label": v_label,
                    "deleted": False,
                }
            )
        subj_key = f"SUBJ-{idx:03d}"
        state.subjects.append({"studyKey": sk, "subjectKey": subj_key, "siteId": 1})
        state.visits.append(
            {
                "studyKey": sk,
                "visitId": visit_id,
                "subjectKey": subj_key,
                "intervalId": interval_id,
                "intervalName": "Screening",
            }
        )
        state.records.append(
            {
                "studyKey": sk,
                "recordId": 1000 + idx,
                "subjectKey": subj_key,
                "formId": form_id,
                "formKey": "F_DM",
                "visitId": visit_id,
                "intervalId": interval_id,
                "recordStatus": "Record Complete",
                "deleted": False,
                "dateCreated": "2026-01-15T10:00:00Z",
                "dateModified": "2026-01-15T10:05:00Z",
                "recordData": {
                    "medicalRecordNumber": MARKER_MRN,
                    "siteChartId": MARKER_CHART,
                    "ssn": MARKER_SSN,
                    "dateOfBirth": "1980-05-20",
                    "clinicalScore": "98",
                },
            }
        )

    transport = create_simulation_transport(state)
    sdk = ImednetSDK(
        api_key="sim-api-key",  # pragma: allowlist secret
        security_key=security_key,
        base_url=base_url,
        transport=transport,
        simulation_mode=True,
    )
    return sdk, state


def _assert_no_plaintext_markers(serialized: str) -> None:
    assert MARKER_MRN not in serialized
    assert MARKER_CHART not in serialized
    assert MARKER_SSN not in serialized
    assert MASKED_SENTINEL in serialized


def test_spi_reexports_and_sink_enrichment_resolvers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify resolve_sink_enrichment_pipeline and resolve_sink_phi_keys precedence and SPI exports."""
    assert spi_sinks.resolve_sink_enrichment_pipeline is resolve_sink_enrichment_pipeline
    assert spi_sinks.resolve_sink_phi_keys is resolve_sink_phi_keys

    db_path = tmp_path / "cfg_store" / "config_versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "IMEDNET_CONFIG_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)

    sdk, _ = _build_simulation_sdk(["STUDY_STORE"], base_url="https://origin-a.imednet.example")
    store_cfg = StudyConfiguration(
        studyKey="STUDY_STORE",
        phiFields=["storeCustomVar"],
    )
    ConfigVersionStore().commit_config("STUDY_STORE", store_cfg, "commit store phi", sdk=sdk)

    b_tok = set_base_url_context(None)
    s_tok = set_security_key_context(None)
    try:
        # 1. Store resolution via study_key or record study_key when config.study_key is unset
        pipe_store = resolve_sink_enrichment_pipeline("STUDY_STORE")
        assert pipe_store is not None
        assert "storecustomvar" in pipe_store.phi_keys

        unscoped_cfg: Any = SimpleNamespace(study_key="", mask_phi=True, extra={})
        rec = Record(
            record_id=1,
            study_key="STUDY_STORE",
            subject_key="S-1",
            form_id=301,
            record_data={"storeCustomVar": "MARKER-1"},
        )
        assert "storecustomvar" in resolve_sink_phi_keys(records=[rec])
        assert "storecustomvar" in resolve_sink_phi_keys(config=unscoped_cfg, records=[rec])
        pipe_from_rec = resolve_sink_enrichment_pipeline(config=unscoped_cfg, records=[rec])
        assert pipe_from_rec is not None
        assert "storecustomvar" in pipe_from_rec.phi_keys

        # 2. SinkConfig.extra["study_config"] as both model and dict
        dict_cfg_sink = SinkConfig(
            study_key="STUDY_DICT",
            extra={"study_config": {"studyKey": "STUDY_DICT", "phiFields": ["dictPhiField"]}},
        )
        assert "dictphifield" in resolve_sink_phi_keys(config=dict_cfg_sink)
        dict_pipe = resolve_sink_enrichment_pipeline(config=dict_cfg_sink)
        assert dict_pipe is not None
        assert dict_pipe.process({"dictPhiField": "RAW"})["dictPhiField"] == MASKED_SENTINEL

        # 3. SinkConfig.extra["enrichment_pipeline"] duck-typed custom single-arg .process(data)
        class _DuckPipeline:
            phi_keys = {"duckCustomPhi"}

            def process(self, data: dict[str, Any]) -> dict[str, Any]:
                out = dict(data)
                if "clinicalScore" in out:
                    out["clinicalScore"] = f"ENRICHED:{out['clinicalScore']}"
                return out

        duck_sink_cfg = SinkConfig(
            study_key="STUDY_DUCK",
            extra={"enrichment_pipeline": _DuckPipeline()},
        )
        duck_phi = resolve_sink_phi_keys(config=duck_sink_cfg)
        assert duck_phi == {"duck_custom_phi", "duckcustomphi"}
        adapted = resolve_sink_enrichment_pipeline(config=duck_sink_cfg)
        assert adapted is not None
        processed = adapted.process(
            {"duckCustomPhi": "RAW_DUCK", "clinicalScore": "42"},
            mask_phi=True,
        )
        assert processed["duckCustomPhi"] == MASKED_SENTINEL
        assert processed["clinicalScore"] == "ENRICHED:42"
    finally:
        reset_base_url_context(b_tok)
        reset_security_key_context(s_tok)


def test_all_thirteen_export_paths_and_standalone_multi_origin_mask_custom_phi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify zero custom PHI leaks across all 13+ streaming, export_to_*, SPI, and standalone paths."""
    db_path = tmp_path / "ledger" / "config_versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "IMEDNET_CONFIG_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)

    sdk, _ = _build_simulation_sdk(
        ["STUDY_COMMITTED", "STUDY_EXTRA"],
        base_url="https://edc.trn.imednetapi.com",
    )

    committed_cfg = StudyConfiguration(
        studyKey="STUDY_COMMITTED",
        phiFields=["medicalRecordNumber", "siteChartId"],
    )
    ConfigVersionStore().commit_config(
        "STUDY_COMMITTED",
        committed_cfg,
        "Register custom study PHI variables",
        sdk=sdk,
    )

    extra_cfg = StudyConfiguration(
        studyKey="STUDY_EXTRA",
        phiFields=["medicalRecordNumber", "siteChartId"],
    )

    # Clear ambient SDK contexts to simulate clean caller environment
    b_tok = set_base_url_context(None)
    s_tok = set_security_key_context(None)
    try:
        # Path 1: SPI ExportSink.redact_batch auto-redaction (both extra and committed store)
        spi_extra_cfg = SinkConfig(
            study_key="STUDY_EXTRA",
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        spi_sink_extra = CustomSpiSink(spi_extra_cfg)
        run_streaming_export(sdk, "STUDY_EXTRA", spi_sink_extra, spi_extra_cfg)
        assert len(spi_sink_extra.batches) == 1
        spi_rec_extra = spi_sink_extra.batches[0][0]
        assert spi_rec_extra.record_data["medicalRecordNumber"] == MASKED_SENTINEL
        assert spi_rec_extra.record_data["siteChartId"] == MASKED_SENTINEL
        assert spi_rec_extra.record_data["ssn"] == MASKED_SENTINEL
        assert spi_rec_extra.subject_key == "SUBJ-002"

        spi_committed_cfg = SinkConfig(study_key="STUDY_COMMITTED", mask_phi=True)
        spi_sink_committed = CustomSpiSink(spi_committed_cfg)
        run_streaming_export(sdk, "STUDY_COMMITTED", spi_sink_committed, spi_committed_cfg)
        spi_rec_comm = spi_sink_committed.batches[0][0]
        assert spi_rec_comm.record_data["medicalRecordNumber"] == MASKED_SENTINEL
        assert spi_rec_comm.record_data["siteChartId"] == MASKED_SENTINEL
        assert spi_rec_comm.record_data["ssn"] == MASKED_SENTINEL

        # Path 2: TabularCSVSink with SinkConfig.extra["study_config"] via run_streaming_export
        stream_csv = tmp_path / "stream_extra.csv"
        csv_sink_cfg = TabularSinkConfig(
            study_key="STUDY_EXTRA",
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        run_streaming_export(
            sdk, "STUDY_EXTRA", TabularCSVSink(stream_csv, csv_sink_cfg), csv_sink_cfg
        )
        _assert_no_plaintext_markers(stream_csv.read_text(encoding="utf-8"))
        df_stream_csv = pd.read_csv(stream_csv)
        assert df_stream_csv.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
        assert df_stream_csv.loc[0, "siteChartId"] == MASKED_SENTINEL
        assert df_stream_csv.loc[0, "ssn"] == MASKED_SENTINEL

        # Path 3: TabularParquetSink with SinkConfig.extra["study_config"] via run_streaming_export
        stream_pq = tmp_path / "stream_extra.parquet"
        pq_sink_cfg = TabularSinkConfig(
            study_key="STUDY_EXTRA",
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        run_streaming_export(
            sdk,
            "STUDY_EXTRA",
            TabularParquetSink(stream_pq, pq_sink_cfg),
            pq_sink_cfg,
        )
        df_stream_pq = pq.read_table(stream_pq).to_pandas()
        assert df_stream_pq.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
        assert df_stream_pq.loc[0, "siteChartId"] == MASKED_SENTINEL
        assert df_stream_pq.loc[0, "ssn"] == MASKED_SENTINEL

        # Path 4: TabularSQLSink with SinkConfig.extra["study_config"] via run_streaming_export
        stream_sql_db = tmp_path / "stream_extra.sqlite"
        sql_engine = create_engine(f"sqlite:///{stream_sql_db}")
        sql_sink_cfg = TabularSinkConfig(
            study_key="STUDY_EXTRA",
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        run_streaming_export(
            sdk,
            "STUDY_EXTRA",
            TabularSQLSink("records_extra", sql_engine, sql_sink_cfg),
            sql_sink_cfg,
        )
        df_stream_sql = pd.read_sql(text("SELECT * FROM records_extra"), sql_engine)
        sql_engine.dispose()
        assert df_stream_sql.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
        assert df_stream_sql.loc[0, "siteChartId"] == MASKED_SENTINEL
        assert df_stream_sql.loc[0, "ssn"] == MASKED_SENTINEL

        # Path 5: TabularDuckDBSink with SinkConfig.extra["study_config"] via run_streaming_export
        stream_ddb = tmp_path / "stream_extra.duckdb"
        ddb_sink_cfg = TabularSinkConfig(
            study_key="STUDY_EXTRA",
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        run_streaming_export(
            sdk,
            "STUDY_EXTRA",
            TabularDuckDBSink(stream_ddb, "records_extra", ddb_sink_cfg),
            ddb_sink_cfg,
        )
        with duckdb.connect(str(stream_ddb)) as dconn:
            df_stream_ddb = dconn.execute("SELECT * FROM records_extra").df()
        assert df_stream_ddb.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
        assert df_stream_ddb.loc[0, "siteChartId"] == MASKED_SENTINEL
        assert df_stream_ddb.loc[0, "ssn"] == MASKED_SENTINEL

        # Path 6: export_to_csv (variable names, human-readable labels, and enrichment_pipeline)
        helper_csv = tmp_path / "helper_extra.csv"
        export_to_csv(
            sdk,
            "STUDY_EXTRA",
            str(helper_csv),
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        _assert_no_plaintext_markers(helper_csv.read_text(encoding="utf-8"))
        df_helper_csv = pd.read_csv(helper_csv)
        assert df_helper_csv.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
        assert df_helper_csv.loc[0, "siteChartId"] == MASKED_SENTINEL
        assert df_helper_csv.loc[0, "ssn"] == MASKED_SENTINEL

        helper_labels_csv = tmp_path / "helper_labels_extra.csv"
        export_to_csv(
            sdk,
            "STUDY_EXTRA",
            str(helper_labels_csv),
            use_labels_as_columns=True,
            mask_phi=True,
            extra={"enrichment_pipeline": EnrichmentPipeline(extra_cfg)},
        )
        _assert_no_plaintext_markers(helper_labels_csv.read_text(encoding="utf-8"))
        df_labels_csv = pd.read_csv(helper_labels_csv)
        assert df_labels_csv.loc[0, "Medical Record #"] == MASKED_SENTINEL
        assert df_labels_csv.loc[0, "Site Chart Identifier"] == MASKED_SENTINEL

        # Path 7: export_to_parquet + export_to_hive_parquet
        helper_pq = tmp_path / "helper_extra.parquet"
        export_to_parquet(
            sdk,
            "STUDY_EXTRA",
            str(helper_pq),
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        df_helper_pq = pq.read_table(helper_pq).to_pandas()
        assert df_helper_pq.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
        assert df_helper_pq.loc[0, "siteChartId"] == MASKED_SENTINEL
        assert df_helper_pq.loc[0, "ssn"] == MASKED_SENTINEL

        hive_dir = tmp_path / "hive_extra"
        export_to_hive_parquet(
            sdk,
            "STUDY_EXTRA",
            str(hive_dir),
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        hive_files = list(hive_dir.glob("**/*.parquet"))
        assert hive_files
        df_hive = pq.read_table(hive_files[0]).to_pandas()
        assert df_hive.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
        assert df_hive.loc[0, "siteChartId"] == MASKED_SENTINEL

        # Path 8: export_to_sql + export_to_sql_by_form
        helper_sql_uri = f"sqlite:///{tmp_path / 'helper_extra.sqlite'}"
        export_to_sql(
            sdk,
            "STUDY_EXTRA",
            "tbl_extra",
            helper_sql_uri,
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        export_to_sql_by_form(
            sdk,
            "STUDY_EXTRA",
            helper_sql_uri,
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        eng_helper = create_engine(helper_sql_uri)
        for tbl_name in ("tbl_extra", "F_DM"):
            df_tbl = pd.read_sql(text(f"SELECT * FROM {tbl_name}"), eng_helper)
            assert df_tbl.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
            assert df_tbl.loc[0, "siteChartId"] == MASKED_SENTINEL
            assert df_tbl.loc[0, "ssn"] == MASKED_SENTINEL
        eng_helper.dispose()

        # Path 9: export_to_duckdb + export_to_duckdb_by_form
        helper_ddb = tmp_path / "helper_extra.duckdb"
        export_to_duckdb(
            sdk,
            "STUDY_EXTRA",
            str(helper_ddb),
            table_name="tbl_extra",
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        export_to_duckdb_by_form(
            sdk,
            "STUDY_EXTRA",
            str(helper_ddb),
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        with duckdb.connect(str(helper_ddb)) as dconn:
            for tbl_name in ("tbl_extra", "F_DM"):
                df_ddb = dconn.execute(f"SELECT * FROM {tbl_name}").df()
                assert df_ddb.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
                assert df_ddb.loc[0, "siteChartId"] == MASKED_SENTINEL
                assert df_ddb.loc[0, "ssn"] == MASKED_SENTINEL

        # Path 10: export_to_excel
        helper_xlsx = tmp_path / "helper_extra.xlsx"
        export_to_excel(
            sdk,
            "STUDY_EXTRA",
            str(helper_xlsx),
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        df_xlsx = pd.read_excel(helper_xlsx)
        assert df_xlsx.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
        assert df_xlsx.loc[0, "siteChartId"] == MASKED_SENTINEL
        assert df_xlsx.loc[0, "ssn"] == MASKED_SENTINEL

        # Path 11: export_to_json (flat and hierarchical)
        helper_json = tmp_path / "helper_extra.json"
        export_to_json(
            sdk,
            "STUDY_EXTRA",
            str(helper_json),
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        raw_flat_json = helper_json.read_text(encoding="utf-8")
        _assert_no_plaintext_markers(raw_flat_json)
        flat_payload = json.loads(raw_flat_json)
        assert flat_payload[0]["medicalRecordNumber"] == MASKED_SENTINEL
        assert flat_payload[0]["siteChartId"] == MASKED_SENTINEL
        assert flat_payload[0]["ssn"] == MASKED_SENTINEL

        helper_hier_json = tmp_path / "helper_hier_extra.json"
        export_to_json(
            sdk,
            "STUDY_EXTRA",
            str(helper_hier_json),
            hierarchical=True,
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        raw_hier_json = helper_hier_json.read_text(encoding="utf-8")
        _assert_no_plaintext_markers(raw_hier_json)
        hier_payload = json.loads(raw_hier_json)
        hier_rec = hier_payload[0]["visits"][0]["forms"][0]["records"][0]
        assert hier_rec["medicalRecordNumber"] == MASKED_SENTINEL
        assert hier_rec["siteChartId"] == MASKED_SENTINEL
        assert hier_rec["ssn"] == MASKED_SENTINEL

        # Path 12: export_to_long_sql
        long_sql_uri = f"sqlite:///{tmp_path / 'long_extra.sqlite'}"
        export_to_long_sql(
            sdk,
            "STUDY_EXTRA",
            "long_records",
            long_sql_uri,
            mask_phi=True,
            extra={"study_config": extra_cfg},
        )
        long_eng = create_engine(long_sql_uri)
        df_long = pd.read_sql(text("SELECT variable_name, value FROM long_records"), long_eng)
        long_eng.dispose()
        long_map = dict(zip(df_long["variable_name"], df_long["value"], strict=False))
        assert long_map["medicalRecordNumber"] == MASKED_SENTINEL
        assert long_map["siteChartId"] == MASKED_SENTINEL
        assert long_map["ssn"] == MASKED_SENTINEL
        assert long_map["clinicalScore"] == "98"

        # Path 13: Standalone write_batch across CSV, Parquet, SQL, DuckDB, and CustomSpiSink
        # with IMEDNET_BASE_URL unset and NO active ImednetSDK session (committed to non-default origin!)
        assert get_base_url_context() is None
        standalone_rec = Record(
            record_id=777,
            study_key="STUDY_COMMITTED",
            subject_key="SUBJ-777",
            form_id=301,
            form_key="F_DM",
            record_data={
                "medicalRecordNumber": MARKER_MRN,
                "siteChartId": MARKER_CHART,
                "ssn": MARKER_SSN,
                "clinicalScore": "100",
            },
        )

        # Test both explicit cfg.study_key="STUDY_COMMITTED" and unset cfg.study_key="" (resolved from rec.study_key)
        for idx, sink_study_key in enumerate(("STUDY_COMMITTED", "")):
            cfg = TabularSinkConfig(study_key=sink_study_key or "PLACEHOLDER", mask_phi=True)
            spi_cfg = SinkConfig(study_key=sink_study_key or "PLACEHOLDER", mask_phi=True)
            if not sink_study_key:
                cfg.study_key = ""
                spi_cfg.study_key = ""

            standalone_csv = tmp_path / f"standalone_{idx}.csv"
            with TabularCSVSink(standalone_csv, cfg) as csv_sink:
                csv_sink.write_batch([standalone_rec], batch_id="b0")
            _assert_no_plaintext_markers(standalone_csv.read_text(encoding="utf-8"))
            df_sa_csv = pd.read_csv(standalone_csv)
            assert df_sa_csv.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
            assert df_sa_csv.loc[0, "siteChartId"] == MASKED_SENTINEL
            assert df_sa_csv.loc[0, "ssn"] == MASKED_SENTINEL

            standalone_pq = tmp_path / f"standalone_{idx}.parquet"
            with TabularParquetSink(standalone_pq, cfg) as pq_sink:
                pq_sink.write_batch([standalone_rec], batch_id="b0")
            df_sa_pq = pq.read_table(standalone_pq).to_pandas()
            assert df_sa_pq.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
            assert df_sa_pq.loc[0, "siteChartId"] == MASKED_SENTINEL
            assert df_sa_pq.loc[0, "ssn"] == MASKED_SENTINEL

            sa_sql_engine = create_engine(f"sqlite:///{tmp_path / f'standalone_{idx}.sqlite'}")
            with TabularSQLSink("sa_tbl", sa_sql_engine, cfg) as sql_sink:
                sql_sink.write_batch([standalone_rec], batch_id="b0")
            df_sa_sql = pd.read_sql(text("SELECT * FROM sa_tbl"), sa_sql_engine)
            sa_sql_engine.dispose()
            assert df_sa_sql.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
            assert df_sa_sql.loc[0, "siteChartId"] == MASKED_SENTINEL
            assert df_sa_sql.loc[0, "ssn"] == MASKED_SENTINEL

            standalone_ddb = tmp_path / f"standalone_{idx}.duckdb"
            with TabularDuckDBSink(standalone_ddb, "sa_tbl", cfg) as ddb_sink:
                ddb_sink.write_batch([standalone_rec], batch_id="b0")
            with duckdb.connect(str(standalone_ddb)) as dconn:
                df_sa_ddb = dconn.execute("SELECT * FROM sa_tbl").df()
            assert df_sa_ddb.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
            assert df_sa_ddb.loc[0, "siteChartId"] == MASKED_SENTINEL
            assert df_sa_ddb.loc[0, "ssn"] == MASKED_SENTINEL

            spi_sa_sink = CustomSpiSink(spi_cfg)
            spi_sa_sink.write_batch([standalone_rec], batch_id="b0")
            redacted_sa = spi_sa_sink.batches[0][0]
            assert redacted_sa.record_data["medicalRecordNumber"] == MASKED_SENTINEL
            assert redacted_sa.record_data["siteChartId"] == MASKED_SENTINEL
            assert redacted_sa.record_data["ssn"] == MASKED_SENTINEL
    finally:
        reset_base_url_context(b_tok)
        reset_security_key_context(s_tok)


def test_guardrails_origin_isolation_fail_closed_exemptions_and_sdtm_birth_year(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify single-origin isolation, fail-closed decryption, structural key exemptions, and SDTM birth year truncation."""
    db_path = tmp_path / "guardrails_store" / "config_versions.sqlite3"
    valid_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_path))
    monkeypatch.setenv("IMEDNET_CONFIG_ENCRYPTION_KEY", valid_key)
    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)

    # 1. Absent base_dir lookup creates zero directories on disk
    absent_dir = db_path.parent
    assert not absent_dir.exists()
    assert ConfigVersionStore.resolve_committed_config("NONEXISTENT") is None
    assert not absent_dir.exists()

    # Commit under origin A
    sdk_a, _ = _build_simulation_sdk(["STUDY_ISO"], base_url="https://origin-a.imednet.example")
    sdk_b, _ = _build_simulation_sdk(["STUDY_ISO"], base_url="https://origin-b.imednet.example")
    iso_cfg = StudyConfiguration(
        studyKey="STUDY_ISO",
        phiFields=[
            "medicalRecordNumber",
            "study_key",
            "subject_key",
            "record_id",
            "usubjid",
            "subjid",
            "studyid",
        ],
    )
    ConfigVersionStore().commit_config("STUDY_ISO", iso_cfg, "Origin A commit", sdk=sdk_a)

    b_tok = set_base_url_context(None)
    s_tok = set_security_key_context(None)
    try:
        # Unscoped lookup finds Origin A commit
        resolved_unscoped = resolve_study_configuration("STUDY_ISO")
        assert resolved_unscoped is not None
        assert "medicalRecordNumber" in resolved_unscoped.phi_fields

        # 2. Strict single-origin isolation when IMEDNET_BASE_URL is set to Origin B
        monkeypatch.setenv("IMEDNET_BASE_URL", "https://origin-b.imednet.example")
        assert resolve_study_configuration("STUDY_ISO") is None
        monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)

        # Strict single-origin isolation when get_base_url_context() (with sdk_b:) is active
        with sdk_b:
            assert resolve_study_configuration("STUDY_ISO") is None

        # Multi-origin conflict deterministically selects latest commit timestamp
        sdk_b_commit, _ = _build_simulation_sdk(
            ["STUDY_ISO"],
            base_url="https://origin-b.imednet.example",
        )
        newer_cfg = StudyConfiguration(
            studyKey="STUDY_ISO",
            phiFields=["medicalRecordNumber", "siteChartId"],
        )
        ConfigVersionStore().commit_config(
            "STUDY_ISO",
            newer_cfg,
            "Origin B newer commit",
            sdk=sdk_b_commit,
        )
        b_tok_clear = set_base_url_context(None)
        try:
            resolved_latest = resolve_study_configuration("STUDY_ISO")
            assert resolved_latest is not None
            assert "siteChartId" in resolved_latest.phi_fields
        finally:
            reset_base_url_context(b_tok_clear)

        # 3. Fail-closed to None on invalid or missing encryption key while keeping default PHI masking
        monkeypatch.setenv(
            "IMEDNET_CONFIG_ENCRYPTION_KEY",
            Fernet.generate_key().decode("ascii"),
        )
        reset_security_key_context(s_tok)
        s_tok = set_security_key_context(None)
        assert resolve_study_configuration("STUDY_ISO") is None

        monkeypatch.delenv("IMEDNET_CONFIG_ENCRYPTION_KEY", raising=False)
        assert resolve_study_configuration("STUDY_ISO") is None

        fallback_rec = Record(
            record_id=55,
            study_key="STUDY_ISO",
            subject_key="SUBJ-055",
            form_id=301,
            record_data={"ssn": MARKER_SSN, "clinicalScore": "77"},
        )
        fallback_redacted = redact_batch_records(
            [fallback_rec],
            mask_phi=True,
            study_key="STUDY_ISO",
        )
        assert fallback_redacted[0].record_data["ssn"] == MASKED_SENTINEL
        assert fallback_redacted[0].record_data["clinicalScore"] == "77"

        # Restore valid key and verify structural exemptions + 21 CFR Part 11 SDTM 4-digit birth-year truncation
        monkeypatch.setenv("IMEDNET_CONFIG_ENCRYPTION_KEY", valid_key)

        sdtm_mapping = {
            "STUDYID": "STUDY_ISO",
            "USUBJID": "STUDY_ISO-SUBJ-001",
            "SUBJID": "SUBJ-001",
            "study_key": "STUDY_ISO",
            "subject_key": "SUBJ-001",
            "record_id": 1001,
            "BRTHDTC": "1980-05-20",
            "medicalRecordNumber": MARKER_MRN,
            "siteChartId": MARKER_CHART,
            "ssn": MARKER_SSN,
        }
        redacted_map = redact_batch_records(
            sdtm_mapping,
            mask_phi=True,
            as_sdtm=True,
            config=SinkConfig(study_key="STUDY_ISO", mask_phi=True, as_sdtm=True),
        )
        assert redacted_map["STUDYID"] == "STUDY_ISO"
        assert redacted_map["USUBJID"] == "STUDY_ISO-SUBJ-001"
        assert redacted_map["SUBJID"] == "SUBJ-001"
        assert redacted_map["study_key"] == "STUDY_ISO"
        assert redacted_map["subject_key"] == "SUBJ-001"
        assert redacted_map["record_id"] == 1001
        assert redacted_map["BRTHDTC"] == "1980"
        assert redacted_map["medicalRecordNumber"] == MASKED_SENTINEL
        assert redacted_map["siteChartId"] == MASKED_SENTINEL
        assert redacted_map["ssn"] == MASKED_SENTINEL

        # Verify DataFrame and single-pass generator inputs to redact_batch_records
        sdtm_df = pd.DataFrame([sdtm_mapping])
        redacted_df = redact_batch_records(
            sdtm_df,
            mask_phi=True,
            as_sdtm=True,
            study_key="STUDY_ISO",
        )
        assert redacted_df.loc[0, "STUDYID"] == "STUDY_ISO"
        assert redacted_df.loc[0, "USUBJID"] == "STUDY_ISO-SUBJ-001"
        assert redacted_df.loc[0, "BRTHDTC"] == "1980"
        assert redacted_df.loc[0, "medicalRecordNumber"] == MASKED_SENTINEL
        assert redacted_df.loc[0, "siteChartId"] == MASKED_SENTINEL

        def _gen_records() -> Iterator[dict[str, Any]]:
            yield {
                "study_key": "STUDY_ISO",
                "subject_key": "SUBJ-GEN",
                "medicalRecordNumber": MARKER_MRN,
            }

        gen_out = redact_batch_records(_gen_records(), mask_phi=True)
        assert len(gen_out) == 1
        assert gen_out[0]["subject_key"] == "SUBJ-GEN"
        assert gen_out[0]["medicalRecordNumber"] == MASKED_SENTINEL

        # Also verify export_to_csv with extra MappingRule + phiFields in SDTM and non-SDTM modes
        mapped_extra = StudyConfiguration(
            studyKey="STUDY_EXTRA",
            phiFields=["medicalRecordNumber", "mappedPhiTarget"],
            mappings=[
                MappingRule(
                    domain="DM",
                    sourceFormKey="F_DM",
                    sourceVariableName="siteChartId",
                    targetField="mappedPhiTarget",
                )
            ],
        )
        mapped_csv = tmp_path / "mapped_extra.csv"
        sdk_extra, _ = _build_simulation_sdk(["STUDY_EXTRA"])
        export_to_csv(
            sdk_extra,
            "STUDY_EXTRA",
            str(mapped_csv),
            mask_phi=True,
            extra={"study_config": mapped_extra},
        )
        raw_mapped = mapped_csv.read_text(encoding="utf-8")
        _assert_no_plaintext_markers(raw_mapped)
        df_mapped = pd.read_csv(mapped_csv)
        assert df_mapped.loc[0, "mappedPhiTarget"] == MASKED_SENTINEL

        # Verify SQLite ciphertext has zero plaintext PHI field names across committed origins
        origin_dbs = list((db_path.parent).glob("**/config_versions.sqlite3"))
        all_payloads: list[Any] = []
        for odb in origin_dbs:
            with sqlite3.connect(odb) as conn:
                all_payloads.extend(
                    r[0] for r in conn.execute("SELECT config_data FROM config_commits")
                )
        assert len(all_payloads) == 2
        for p in all_payloads:
            assert "medicalRecordNumber" not in str(p)
            assert "siteChartId" not in str(p)
    finally:
        reset_base_url_context(b_tok)
        reset_security_key_context(s_tok)
