"""Unit and regression tests for single-pass tabular export preparation."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, text

import imednet.integrations.enrichment as enrichment_mod
import imednet.integrations.export as export_mod
import imednet.integrations.parquet as parquet_mod
import imednet.integrations.sink_base as sink_base_mod
import imednet.spi.sink_base as spi_sink_base
import imednet.spi.sinks as spi_sinks
import imednet.spi.utils as spi_utils
import imednet.utils.security as security_mod
import imednet_workflows.record_mapper as rm_mod
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
    export_to_json,
    export_to_parquet,
    export_to_sql,
    export_to_sql_by_form,
)
from imednet.integrations.parquet import export_to_hive_parquet
from imednet.integrations.parquet_engine import PyArrowDatasetPartitionedStorageEngine
from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.records import Record
from imednet.models.study_config import StudyConfiguration
from imednet.models.variables import Variable
from imednet.utils.security import SensitivityRegistry, global_sensitivity_registry
from imednet_workflows.cached_loader import CachedRecordsLoader
from imednet_workflows.record_mapper import RecordMapper


def _install_study_phi_config(
    monkeypatch: pytest.MonkeyPatch,
    study_key: str,
    phi_fields: list[str],
) -> StudyConfiguration:
    """Install a StudyConfiguration with custom phiFields for resolve_study_configuration."""
    cfg = StudyConfiguration(studyKey=study_key, phiFields=phi_fields)

    class _StubConfigStore:
        @classmethod
        def resolve_committed_config(cls, sk: str) -> StudyConfiguration | None:
            return cfg if sk == study_key else None

    monkeypatch.setattr(enrichment_mod, "_get_config_store_cls", lambda: _StubConfigStore)
    return cfg


def _read_hive_parquet_df(base_dir: Path) -> pd.DataFrame:
    """Read all Parquet partition files written under a Hive base directory."""
    files = sorted(base_dir.glob("**/*.parquet"))
    assert files, f"No parquet files found under {base_dir}"
    return pd.concat([pd.read_parquet(f, engine="pyarrow") for f in files], ignore_index=True)


def _build_study_sdk(
    records: list[Record],
    variables: list[Variable],
    forms: list[Form] | None = None,
    intervals: list[Interval] | None = None,
    deleted_records: list[Record] | None = None,
) -> MagicMock:
    """Build a mock ImednetSDK returning realistic forms, variables, intervals, and records."""
    active_forms = forms or [
        Form(form_id=10, form_key="F_DM", form_name="Demographics"),
        Form(form_id=20, form_key="F_VS", form_name="Vital Signs"),
    ]
    active_intervals = intervals or [
        Interval(
            interval_id=100,
            interval_name="Baseline",
            interval_sequence=1,
            forms=[FormSummary(form_id=10, form_key="F_DM", form_name="Demographics")],
        )
    ]
    voided = list(deleted_records or [])

    def _list_records(*_args: Any, **kw: Any) -> Any:
        from imednet.integrations.sink_base import (
            extract_record_timestamp,
            normalize_watermark_datetime,
        )

        flt = str(kw.get("filter", "") or "")
        all_items = list(records) + voided
        if kw.get("deleted") is False or "deleted==false" in flt:
            items = [r for r in all_items if not r.deleted]
        elif kw.get("deleted") is True or "deleted==true" in flt:
            items = [r for r in all_items if r.deleted]
        else:
            items = list(all_items)
        if kw.get("dateModified") is not None:
            dm = kw["dateModified"]
            if isinstance(dm, tuple) and len(dm) == 2:
                op_str, raw_ts = dm
                wm_dt = normalize_watermark_datetime(raw_ts)
                if wm_dt is not None and op_str == ">=":
                    items = [
                        r
                        for r in items
                        if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                    ]
                elif wm_dt is not None and op_str == ">":
                    items = [
                        r
                        for r in items
                        if (ts := extract_record_timestamp(r)) is not None and ts > wm_dt
                    ]
        for clause in (c.strip() for c in flt.split(";") if c.strip()):
            if clause.startswith("dateModified>="):
                wm_dt = normalize_watermark_datetime(clause.split(">=", 1)[1])
                if wm_dt is not None:
                    items = [
                        r
                        for r in items
                        if (ts := extract_record_timestamp(r)) is not None and ts >= wm_dt
                    ]
        if kw.get("formId") is not None:
            raw_fid = kw["formId"]
            if isinstance(raw_fid, tuple) and len(raw_fid) == 2:
                fids = {int(raw_fid[1])}
            elif isinstance(raw_fid, (list, tuple, set)):
                fids = {int(x) for x in raw_fid}
            else:
                fids = {int(raw_fid)}
            items = [r for r in items if r.form_id in fids]
        return iter([r.model_copy(deep=True) for r in items])

    sdk = MagicMock()
    sdk.get_forms.return_value = active_forms
    sdk.forms.list.return_value = active_forms
    sdk.get_intervals.return_value = active_intervals
    sdk.intervals.list.return_value = active_intervals
    sdk.get_variables.return_value = variables
    sdk.variables.list.return_value = variables
    sdk.get_records.side_effect = _list_records
    sdk.records.list.side_effect = _list_records
    return sdk


class TestSpiTabularBatchStampContract:
    """Verify Requirement 1: SPI batch stamp helpers and invalidation rules."""

    def test_spi_exports_stamp_helpers_from_utils_and_sink_base(self) -> None:
        """Both imednet.spi.utils and imednet.spi.sink_base expose identical stamp helpers."""
        expected = (
            "TabularBatchStamp",
            "stamp_tabular_batch",
            "get_tabular_batch_stamp",
            "has_valid_tabular_batch_stamp",
            "normalize_phi_keys_snapshot",
        )
        for name in expected:
            assert hasattr(spi_sink_base, name)
            assert hasattr(spi_utils, name)
            assert getattr(spi_sink_base, name) is getattr(spi_utils, name)

    def test_stamp_binds_registry_phi_keys_headers_and_sanitization_without_pyarrow_warning(
        self,
    ) -> None:
        """Stamp validates snapshots, phi_keys, headers, sanitization, and emits no PyArrow warning."""
        reg = SensitivityRegistry()
        df = pd.DataFrame([{"record_id": 1, "subject_key": "S-1", "custom_mrn": "***MASKED***"}])

        spi_utils.stamp_tabular_batch(
            df,
            mask_phi=True,
            phi_keys=["customMrn", "subject_key"],
            sanitized=False,
            registry=reg,
        )
        stamp = spi_utils.get_tabular_batch_stamp(df)
        assert isinstance(stamp, spi_utils.TabularBatchStamp)
        assert stamp.mask_phi is True
        assert stamp.sanitized is False
        assert stamp.phi_keys == frozenset({"custommrn"})
        # Exempt identifiers are excluded from custom phi_keys snapshot
        assert "subjectkey" not in stamp.phi_keys

        assert spi_utils.has_valid_tabular_batch_stamp(
            df,
            mask_phi=True,
            phi_keys={"custom_mrn"},
            require_sanitized=False,
            registry=reg,
        )
        assert not spi_utils.has_valid_tabular_batch_stamp(
            df,
            mask_phi=True,
            phi_keys={"custom_mrn", "other_secret"},
            registry=reg,
        )
        assert not spi_utils.has_valid_tabular_batch_stamp(
            df,
            mask_phi=True,
            require_sanitized=True,
            registry=reg,
        )

        # PyArrow serialization must not warn or leak stamp attributes into df.attrs
        assert df.attrs == {}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            table = pa.Table.from_pandas(df)
        assert table.num_rows == 1
        assert not caught

        # Mutating SensitivityRegistry invalidates stamp
        reg.add_sensitive_key("runtime_secret_field")
        assert not spi_utils.has_valid_tabular_batch_stamp(df, mask_phi=True, registry=reg)

        # Re-stamping then mutating headers invalidates stamp
        spi_utils.stamp_tabular_batch(df, mask_phi=True, sanitized=True, registry=reg)
        assert spi_utils.has_valid_tabular_batch_stamp(
            df, mask_phi=True, require_sanitized=True, registry=reg
        )
        df["extra_col"] = ["unmasked"]
        assert spi_utils.get_tabular_batch_stamp(df) is None
        assert not spi_utils.has_valid_tabular_batch_stamp(df, mask_phi=True, registry=reg)


class TestRecordMapperSinglePassAndLabelStamping:
    """Verify Requirements 2 & 3: RecordMapper skips scalar scans, scans nested cells, masks labels."""

    def test_record_mapper_skips_scalar_phi_scans_masks_sensitive_labels_and_scans_nested_cells(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RecordMapper._build_dataframe skips scalar scans, masks sensitive labels, and stamps batches."""
        _install_study_phi_config(monkeypatch, "STUDY_RM", ["study_phi_code"])

        variables = [
            Variable(
                variable_id=1,
                variable_name="SAFE_TEXT",
                label="Clinical Note",
                variable_type="text",
                form_id=10,
                form_key="F_DM",
            ),
            Variable(
                variable_id=2,
                variable_name="OPAQUE_VAR",
                label="Patient Name",
                variable_type="text",
                form_id=10,
                form_key="F_DM",
            ),
            Variable(
                variable_id=3,
                variable_name="study_phi_code",
                label="Internal Code",
                variable_type="text",
                form_id=10,
                form_key="F_DM",
            ),
            Variable(
                variable_id=4,
                variable_name="NESTED_PAYLOAD",
                label="Nested Payload",
                variable_type="text",
                form_id=10,
                form_key="F_DM",
            ),
        ]
        records = [
            Record(
                record_id=1,
                subject_id=1,
                subject_key="SUBJ-001",
                visit_id=100,
                form_id=10,
                form_key="F_DM",
                record_data={
                    "SAFE_TEXT": "stable vitals",
                    "OPAQUE_VAR": "123-45-6789",
                    "study_phi_code": "CUSTOM-PHI-99",
                    "NESTED_PAYLOAD": [{"inner_safe": "ok", "DOB": "1980-01-01"}],
                },
            )
        ]
        sdk = _build_study_sdk(records, variables)

        scalar_calls = 0
        orig_mask = security_mod.mask_clinical_phi

        def spy_mask(value: Any, *, registry: Any = None) -> Any:
            nonlocal scalar_calls
            if not isinstance(value, (dict, list, tuple)):
                scalar_calls += 1
            return orig_mask(value, registry=registry)

        with (
            patch.object(security_mod, "mask_clinical_phi", new=spy_mask),
            patch.object(rm_mod, "mask_clinical_phi", new=spy_mask),
        ):
            mapper = RecordMapper(sdk, mask_phi=True)
            df = mapper.dataframe("STUDY_RM", use_labels_as_columns=True)

        assert scalar_calls == 0
        # Variable whose variable_name was non-sensitive ("OPAQUE_VAR") but whose label
        # ("Patient Name") is sensitive MUST be masked when use_labels_as_columns=True!
        assert df["Patient Name"].iloc[0] == "***MASKED***"
        assert df["Internal Code"].iloc[0] == "***MASKED***"
        assert df["Clinical Note"].iloc[0] == "stable vitals"
        assert df["Nested Payload"].iloc[0] == [{"inner_safe": "ok", "DOB": "***MASKED***"}]
        assert df["subjectKey"].iloc[0] == "SUBJ-001"

        assert spi_utils.has_valid_tabular_batch_stamp(
            df,
            mask_phi=True,
            phi_keys={"study_phi_code"},
            require_sanitized=False,
        )


class TestDirectTabularSinksStudyConfigPhiAndImmutability:
    """Verify Requirements 4, 5 & Acceptance Criterion 2 across all four Tabular*Sinks."""

    def test_skip_auto_redact_flags_preserve_custom_export_sink_auto_redaction(self) -> None:
        """Built-in Tabular*Sinks opt into _prepare_tabular_batch_df while custom sinks auto-redact."""
        assert TabularCSVSink._skip_auto_redact_batch is True
        assert TabularParquetSink._skip_auto_redact_batch is True
        assert TabularSQLSink._skip_auto_redact_batch is True
        assert TabularDuckDBSink._skip_auto_redact_batch is True

        class ExternalCustomSink(spi_sinks.ExportSink):
            def write_batch(self, records: Any, *, batch_id: str) -> int:
                self.last = records
                return len(records)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        assert ExternalCustomSink._skip_auto_redact_batch is False
        ext_sink = ExternalCustomSink(spi_sinks.SinkConfig(study_key="S1"))
        raw = {"record_id": 1, "SSN": "123-45-6789", "value": 5}
        ext_sink.write_batch([raw], batch_id="b0")
        assert raw["SSN"] == "123-45-6789"
        assert ext_sink.last[0]["SSN"] == "***MASKED***"

    def test_all_four_tabular_sinks_mask_default_and_study_phi_without_mutating_inputs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Direct write_batch on CSV, Parquet, SQL, and DuckDB masks default + StudyConfiguration PHI."""
        study_key = "STUDY_DIRECT"
        _install_study_phi_config(monkeypatch, study_key, ["custom_chart_id"])

        caller_df = pd.DataFrame(
            [
                {
                    "record_id": 1,
                    "subject_key": "SUBJ-01",
                    "study_key": study_key,
                    "SSN": "123-45-6789",
                    "custom_chart_id": "CHART-777",
                    "formula_note": "=HYPERLINK('http://bad')",
                    "vital": 98.6,
                }
            ]
        )
        caller_dict = {
            "record_id": 2,
            "subject_key": "SUBJ-02",
            "study_key": study_key,
            "SSN": "999-88-7777",
            "custom_chart_id": "CHART-888",
            "formula_note": "+SUM(1,2)",
            "vital": 99.1,
        }
        caller_record = Record(
            record_id=3,
            study_key=study_key,
            subject_key="SUBJ-03",
            form_id=10,
            record_data={
                "SSN": "555-44-3333",
                "custom_chart_id": "CHART-999",
                "formula_note": "@cmd",
                "vital": 97.9,
            },
        )

        # Also prepare a stamped-but-unsanitized DataFrame (simulating RecordMapper.dataframe())
        stamped_unsanitized_df = pd.DataFrame(
            [
                {
                    "record_id": 4,
                    "subject_key": "SUBJ-04",
                    "study_key": study_key,
                    "SSN": "***MASKED***",
                    "custom_chart_id": "***MASKED***",
                    "formula_note": "-2+3",
                    "vital": 98.2,
                }
            ]
        )
        spi_utils.stamp_tabular_batch(
            stamped_unsanitized_df,
            mask_phi=True,
            phi_keys={"custom_chart_id"},
            sanitized=False,
        )

        cfg = TabularSinkConfig(study_key=study_key, sanitize=True)

        # 1. TabularCSVSink
        csv_df_path = tmp_path / "direct_df.csv"
        csv_seq_path = tmp_path / "direct_seq.csv"
        with TabularCSVSink(str(csv_df_path), cfg) as sink:
            sink.write_batch(caller_df, batch_id="csv/0")
            sink.write_batch(stamped_unsanitized_df, batch_id="csv/2")
        with TabularCSVSink(str(csv_seq_path), cfg) as sink:
            sink.write_batch([caller_dict, caller_record], batch_id="csv/1")

        # 2. TabularParquetSink
        pq_df_path = tmp_path / "direct_df.parquet"
        pq_seq_path = tmp_path / "direct_seq.parquet"
        with TabularParquetSink(str(pq_df_path), cfg) as sink:
            sink.write_batch(caller_df, batch_id="pq/0")
            sink.write_batch(stamped_unsanitized_df, batch_id="pq/2")
        with TabularParquetSink(str(pq_seq_path), cfg) as sink:
            sink.write_batch([caller_dict, caller_record], batch_id="pq/1")

        # 3. TabularSQLSink
        sql_path = tmp_path / "direct.sqlite"
        engine = create_engine(f"sqlite:///{sql_path}")
        try:
            with TabularSQLSink("df_tbl", engine, cfg) as sink:
                sink.write_batch(caller_df, batch_id="sql/0")
                sink.write_batch(stamped_unsanitized_df, batch_id="sql/2")
            with TabularSQLSink("seq_tbl", engine, cfg) as sink:
                sink.write_batch([caller_dict, caller_record], batch_id="sql/1")
            with engine.connect() as conn:
                sql_df = (
                    pd.concat(
                        [
                            pd.read_sql(
                                text(
                                    "SELECT record_id, subject_key, SSN, custom_chart_id, formula_note FROM df_tbl"
                                ),
                                conn,
                            ),
                            pd.read_sql(
                                text(
                                    "SELECT record_id, subject_key, SSN, custom_chart_id, formula_note FROM seq_tbl"
                                ),
                                conn,
                            ),
                        ],
                        ignore_index=True,
                    )
                    .sort_values("record_id")
                    .reset_index(drop=True)
                )
        finally:
            engine.dispose()

        # 4. TabularDuckDBSink
        ddb_path = tmp_path / "direct.duckdb"
        with TabularDuckDBSink(str(ddb_path), "df_tbl", cfg) as sink:
            sink.write_batch(caller_df, batch_id="ddb/0")
            sink.write_batch(stamped_unsanitized_df, batch_id="ddb/2")
        with TabularDuckDBSink(str(ddb_path), "seq_tbl", cfg) as sink:
            sink.write_batch([caller_dict, caller_record], batch_id="ddb/1")
        with duckdb.connect(str(ddb_path), read_only=True) as con:
            ddb_df = con.execute(
                "SELECT record_id, subject_key, SSN, custom_chart_id, formula_note FROM df_tbl "
                "UNION ALL "
                "SELECT record_id, subject_key, SSN, custom_chart_id, formula_note FROM seq_tbl "
                "ORDER BY record_id"
            ).df()

        # Caller inputs MUST remain completely unmutated
        assert caller_df["SSN"].iloc[0] == "123-45-6789"
        assert caller_df["custom_chart_id"].iloc[0] == "CHART-777"
        assert caller_df["formula_note"].iloc[0] == "=HYPERLINK('http://bad')"
        assert caller_dict["SSN"] == "999-88-7777"
        assert caller_dict["custom_chart_id"] == "CHART-888"
        assert caller_dict["formula_note"] == "+SUM(1,2)"
        assert caller_record.record_data["SSN"] == "555-44-3333"
        assert caller_record.record_data["custom_chart_id"] == "CHART-999"
        assert caller_record.record_data["formula_note"] == "@cmd"
        assert stamped_unsanitized_df["formula_note"].iloc[0] == "-2+3"

        cols = ["record_id", "subject_key", "SSN", "custom_chart_id", "formula_note"]
        csv_df = (
            pd.concat(
                [pd.read_csv(csv_df_path)[cols], pd.read_csv(csv_seq_path)[cols]],
                ignore_index=True,
            )
            .sort_values("record_id")
            .reset_index(drop=True)
        )
        pq_df = (
            pd.concat(
                [
                    pq.read_table(pq_df_path).to_pandas()[cols],
                    pq.read_table(pq_seq_path).to_pandas()[cols],
                ],
                ignore_index=True,
            )
            .sort_values("record_id")
            .reset_index(drop=True)
        )

        for dest_name, out_df in (
            ("csv", csv_df),
            ("parquet", pq_df),
            ("sql", sql_df),
            ("duckdb", ddb_df),
        ):
            assert len(out_df) == 4, dest_name
            assert list(out_df["SSN"]) == ["***MASKED***"] * 4, dest_name
            assert list(out_df["custom_chart_id"]) == ["***MASKED***"] * 4, dest_name
            assert list(out_df["subject_key"]) == [
                "SUBJ-01",
                "SUBJ-02",
                "SUBJ-03",
                "SUBJ-04",
            ], dest_name
            assert out_df["formula_note"].iloc[0] == "'=HYPERLINK('http://bad')", dest_name
            assert out_df["formula_note"].iloc[1] == "'+SUM(1,2)", dest_name
            assert out_df["formula_note"].iloc[2] == "'@cmd", dest_name
            assert out_df["formula_note"].iloc[3] == "'-2+3", dest_name


class TestRuntimeInvalidationAndFailClosedPaths:
    """Verify Requirement 6 & Acceptance Criterion 3: runtime SensitivityRegistry & fail-closed guards."""

    def test_runtime_add_sensitive_key_invalidates_stamps_and_masks_subsequent_batches(
        self,
        tmp_path: Path,
    ) -> None:
        """Calling global_sensitivity_registry.add_sensitive_key invalidates stamps and masks headers."""
        runtime_key = "custom_runtime_mrn_token"
        variables = [
            Variable(
                variable_id=1,
                variable_name=runtime_key,
                label="Runtime Token",
                variable_type="text",
                form_id=10,
                form_key="F_DM",
            ),
            Variable(
                variable_id=2,
                variable_name="HR",
                label="Heart Rate",
                variable_type="int",
                form_id=10,
                form_key="F_DM",
            ),
        ]
        records = [
            Record(
                record_id=1,
                subject_id=1,
                subject_key="SUBJ-1",
                visit_id=100,
                form_id=10,
                form_key="F_DM",
                record_data={runtime_key: "RAW-TOKEN-42", "HR": 70},
            )
        ]
        sdk = _build_study_sdk(records, variables)
        mapper = RecordMapper(sdk, mask_phi=True)

        try:
            # Batch 1 before runtime registration: runtime_key is cleartext and stamped
            df_before = mapper.dataframe("STUDY_RT", use_labels_as_columns=False)
            assert df_before[runtime_key].iloc[0] == "RAW-TOKEN-42"
            assert spi_utils.has_valid_tabular_batch_stamp(df_before, mask_phi=True)

            # Register runtime sensitive key
            global_sensitivity_registry.add_sensitive_key(runtime_key)

            # Prior stamp is immediately invalidated!
            assert not spi_utils.has_valid_tabular_batch_stamp(df_before, mask_phi=True)

            # Passing the previously stamped df_before to TabularCSVSink or _mask_df fails closed
            stale_csv = tmp_path / "stale_batch.csv"
            with TabularCSVSink(str(stale_csv), TabularSinkConfig(study_key="STUDY_RT")) as sink:
                sink.write_batch(df_before, batch_id="rt/stale")
            assert df_before[runtime_key].iloc[0] == "RAW-TOKEN-42"
            assert pd.read_csv(stale_csv)[runtime_key].iloc[0] == "***MASKED***"

            # Subsequent RecordMapper batch masks the matching Variable header & payload
            df_after = mapper.dataframe("STUDY_RT", use_labels_as_columns=False)
            assert df_after[runtime_key].iloc[0] == "***MASKED***"
            assert spi_utils.has_valid_tabular_batch_stamp(df_after, mask_phi=True)
        finally:
            global_sensitivity_registry.remove_sensitive_key(runtime_key)

    def test_hive_parquet_fails_closed_for_unstamped_or_mutated_dataframe(
        self,
        tmp_path: Path,
    ) -> None:
        """_write_hive_form_rows masks unstamped or header-mutated DataFrames while skipping valid ones."""
        storage = PyArrowDatasetPartitionedStorageEngine()
        unstamped = pd.DataFrame(
            [{"recordId": 1, "subjectKey": "SUBJ-1", "SSN": "123-45-6789", "HR": 72}]
        )
        mock_mapper = MagicMock()
        mock_mapper._build_dataframe.return_value = unstamped
        hive_dir = tmp_path / "hive_fail_closed"
        parquet_mod._write_hive_form_rows(
            [{"recordId": 1}],
            mapper=mock_mapper,
            variable_keys=["SSN", "HR"],
            label_map={"SSN": "SSN", "HR": "HR"},
            use_labels_as_columns=False,
            mask_phi=True,
            phi_keys=set(),
            pyarrow_module=pa,
            storage_engine=storage,
            base_dir=str(hive_dir),
            study_key="STUDY_HIVE",
            form_key="F_DM",
            skip_empty=True,
        )
        out = _read_hive_parquet_df(hive_dir)
        assert out["SSN"].iloc[0] == "***MASKED***"
        assert out["subjectKey"].iloc[0] == "SUBJ-1"

        # Now stamp a DataFrame, mutate its columns to add an unmasked PHI column, and verify fail-closed
        mutated = pd.DataFrame([{"recordId": 2, "subjectKey": "SUBJ-2", "HR": 68}])
        spi_utils.stamp_tabular_batch(mutated, mask_phi=True)
        mutated["patientName"] = ["Cleartext Name"]
        mock_mapper._build_dataframe.return_value = mutated
        hive_dir_mut = tmp_path / "hive_mutated"
        parquet_mod._write_hive_form_rows(
            [{"recordId": 2}],
            mapper=mock_mapper,
            variable_keys=["HR", "patientName"],
            label_map={"HR": "HR", "patientName": "patientName"},
            use_labels_as_columns=False,
            mask_phi=True,
            phi_keys=set(),
            pyarrow_module=pa,
            storage_engine=storage,
            base_dir=str(hive_dir_mut),
            study_key="STUDY_HIVE",
            form_key="F_DM",
            skip_empty=True,
        )
        out_mut = _read_hive_parquet_df(hive_dir_mut)
        assert out_mut["patientName"].iloc[0] == "***MASKED***"


class TestFiveThousandRecordCsvSinglePassProfile:
    """Verify Requirement 7 & Acceptance Criterion 4 on a 5,000-Record, 500-Variable CSV export."""

    def test_5000_record_500_variable_csv_export_executes_single_pass_without_redundant_scans(
        self,
        tmp_path: Path,
    ) -> None:
        """5,000x500 export_to_csv runs 0 _redact_dataframe_batch, 1 _sanitize_df/batch, 0 scalar PHI scans."""
        n_records = 5000
        n_vars = 500
        forms = [Form(form_id=1, form_key="FRM1", form_name="Form 1")]
        variables = [
            Variable(
                variable_id=i,
                variable_name=f"FRM1_VAR_{i}",
                label=f"Var {i}",
                variable_type="text",
                form_id=1,
                form_key="FRM1",
            )
            for i in range(n_vars - 1)
        ] + [
            Variable(
                variable_id=n_vars,
                variable_name="SSN",
                label="Patient SSN",
                variable_type="text",
                form_id=1,
                form_key="FRM1",
            )
        ]

        base_data = {f"FRM1_VAR_{i}": f"val_{i}" for i in range(n_vars - 1)}
        base_data["FRM1_VAR_0"] = "=SUM(A1:A2)"
        base_data["SSN"] = "123-45-6789"

        records = [
            Record(
                record_id=r + 1,
                subject_id=r + 1,
                subject_key=f"SUBJ-{r + 1:05d}",
                visit_id=10,
                form_id=1,
                form_key="FRM1",
                date_modified="2026-01-01T00:00:00Z",
                record_data=dict(base_data),
            )
            for r in range(n_records)
        ]
        sdk = _build_study_sdk(records, variables, forms=forms)

        counts = {
            "mask_total": 0,
            "mask_scalar": 0,
            "sanitize_df": 0,
            "redact_df_batch": 0,
        }
        orig_mask = security_mod.mask_clinical_phi
        orig_sanitize_df = export_mod._sanitize_df
        orig_redact = sink_base_mod._redact_dataframe_batch

        def spy_mask(data: Any, *, registry: Any = None) -> Any:
            counts["mask_total"] += 1
            if not isinstance(data, (dict, list, tuple)):
                counts["mask_scalar"] += 1
            return orig_mask(data, registry=registry)

        def spy_sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
            counts["sanitize_df"] += 1
            return orig_sanitize_df(df)

        def spy_redact(df: Any) -> Any:
            counts["redact_df_batch"] += 1
            return orig_redact(df)

        csv_out = tmp_path / "profile_5k_500.csv"
        with (
            patch.object(security_mod, "mask_clinical_phi", new=spy_mask),
            patch.object(rm_mod, "mask_clinical_phi", new=spy_mask),
            patch.object(export_mod, "mask_clinical_phi", new=spy_mask),
            patch.object(sink_base_mod, "mask_clinical_phi", new=spy_mask),
            patch.object(export_mod, "_sanitize_df", new=spy_sanitize_df),
            patch.object(sink_base_mod, "_redact_dataframe_batch", new=spy_redact),
        ):
            export_to_csv(
                sdk,
                "STUDY_5K",
                str(csv_out),
                quality_gate_enabled=False,
                batch_size=500,
            )

        assert counts["redact_df_batch"] == 0
        assert counts["mask_scalar"] == 0
        assert counts["mask_total"] == n_records
        # 5,000 records at batch_size=500 -> 10 batches -> exactly 1 _sanitize_df pass per batch
        assert counts["sanitize_df"] == 10

        sample = pd.read_csv(csv_out, nrows=3)
        assert sample["SSN"].iloc[0] == "***MASKED***"
        assert sample["FRM1_VAR_0"].iloc[0] == "'=SUM(A1:A2)"

    def test_export_to_excel_runs_sanitize_df_once_per_batch(self, tmp_path: Path) -> None:
        """export_to_excel deduplicates _sanitize_df across _TabularTransformSink and collector."""
        variables = [
            Variable(
                variable_id=1,
                variable_name="NOTE",
                label="Note",
                variable_type="text",
                form_id=10,
                form_key="F_DM",
            )
        ]
        records = [
            Record(
                record_id=i + 1,
                subject_id=i + 1,
                subject_key=f"S-{i + 1}",
                visit_id=100,
                form_id=10,
                form_key="F_DM",
                record_data={"NOTE": "=1+1"},
            )
            for i in range(6)
        ]
        sdk = _build_study_sdk(records, variables)

        sanitize_calls = 0
        orig_sanitize_df = export_mod._sanitize_df

        def spy_sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
            nonlocal sanitize_calls
            sanitize_calls += 1
            return orig_sanitize_df(df)

        xlsx_path = tmp_path / "dedup_excel.xlsx"
        with patch.object(export_mod, "_sanitize_df", new=spy_sanitize_df):
            export_to_excel(sdk, "STUDY_XLSX", str(xlsx_path), batch_size=3)

        assert sanitize_calls == 2
        out_df = pd.read_excel(xlsx_path)
        assert len(out_df) == 6
        assert out_df["NOTE"].iloc[0] == "'=1+1"


class TestIncrementalReconciliationSkipsUnchangedHistory:
    """Verify Requirement 8: incremental Excel & JSON skip full-history re-masking unless PHI rules match."""

    def test_incremental_json_and_excel_skip_historical_rescan_and_remask_newly_sensitive_keys(
        self,
        tmp_path: Path,
    ) -> None:
        """Incremental Excel, flat JSON, and hierarchical JSON scale with deltas and honor new PHI rules."""
        retro_key = "retroactive_chart_identifier"
        variables = [
            Variable(
                variable_id=1,
                variable_name="NOTE",
                label="Note",
                variable_type="text",
                form_id=10,
                form_key="F_DM",
            ),
            Variable(
                variable_id=2,
                variable_name=retro_key,
                label="Retro Chart ID",
                variable_type="text",
                form_id=10,
                form_key="F_DM",
            ),
            Variable(
                variable_id=3,
                variable_name="SSN",
                label="SSN",
                variable_type="text",
                form_id=10,
                form_key="F_DM",
            ),
        ]
        initial_records = [
            Record(
                record_id=r + 1,
                subject_id=r + 1,
                subject_key=f"SUBJ-{r + 1:04d}",
                visit_id=100,
                form_id=10,
                form_key="F_DM",
                date_modified="2026-01-01T00:00:00Z",
                record_data={
                    "NOTE": "=A1+B1",
                    retro_key: f"CHART-{r + 1}",
                    "SSN": "123-45-6789",
                },
            )
            for r in range(200)
        ]

        flat_json_path = tmp_path / "inc_flat.json"
        hier_json_path = tmp_path / "inc_hier.json"
        excel_path = tmp_path / "inc.xlsx"
        ledger_dir = tmp_path / "ledgers"

        sdk_init = _build_study_sdk(initial_records, variables)
        export_to_json(
            sdk_init,
            "STUDY_INC",
            str(flat_json_path),
            incremental=True,
            ledger_path=str(ledger_dir),
        )
        export_to_json(
            sdk_init,
            "STUDY_INC",
            str(hier_json_path),
            hierarchical=True,
            incremental=True,
            ledger_path=str(ledger_dir),
        )
        export_to_excel(
            sdk_init,
            "STUDY_INC",
            str(excel_path),
            incremental=True,
            ledger_path=str(ledger_dir),
        )

        # Delta sync 1: 2 modified records, 1 voided record, 197 unchanged historical records
        delta_records = [
            Record(
                record_id=1,
                subject_id=1,
                subject_key="SUBJ-0001",
                visit_id=100,
                form_id=10,
                form_key="F_DM",
                date_modified="2026-01-02T00:00:00Z",
                record_data={
                    "NOTE": "updated-1",
                    retro_key: "CHART-1-NEW",
                    "SSN": "999-99-9999",
                },
            ),
            Record(
                record_id=2,
                subject_id=2,
                subject_key="SUBJ-0002",
                visit_id=100,
                form_id=10,
                form_key="F_DM",
                date_modified="2026-01-02T00:00:00Z",
                record_data={
                    "NOTE": "=UPDATED()",
                    retro_key: "CHART-2-NEW",
                    "SSN": "888-88-8888",
                },
            ),
        ]
        voided_records = [
            Record(
                record_id=3,
                subject_id=3,
                subject_key="SUBJ-0003",
                visit_id=100,
                form_id=10,
                form_key="F_DM",
                deleted=True,
                date_modified="2026-01-02T00:00:00Z",
                record_data={},
            )
        ]
        sdk_delta = _build_study_sdk(delta_records, variables, deleted_records=voided_records)

        orig_mask = security_mod.mask_clinical_phi
        orig_sanitize_df = export_mod._sanitize_df
        mask_calls = 0
        sanitize_df_rows = 0

        def spy_mask(data: Any, *, registry: Any = None) -> Any:
            nonlocal mask_calls
            mask_calls += 1
            return orig_mask(data, registry=registry)

        def spy_sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
            nonlocal sanitize_df_rows
            sanitize_df_rows += len(df)
            return orig_sanitize_df(df)

        with (
            patch.object(security_mod, "mask_clinical_phi", new=spy_mask),
            patch.object(rm_mod, "mask_clinical_phi", new=spy_mask),
            patch.object(export_mod, "mask_clinical_phi", new=spy_mask),
        ):
            export_to_json(
                sdk_delta,
                "STUDY_INC",
                str(flat_json_path),
                incremental=True,
                ledger_path=str(ledger_dir),
            )
        # Only the 2 delta records were scanned by mask_clinical_phi (not the 200 historical items)
        assert mask_calls == 2

        mask_calls = 0
        with (
            patch.object(security_mod, "mask_clinical_phi", new=spy_mask),
            patch.object(rm_mod, "mask_clinical_phi", new=spy_mask),
            patch.object(export_mod, "mask_clinical_phi", new=spy_mask),
        ):
            export_to_json(
                sdk_delta,
                "STUDY_INC",
                str(hier_json_path),
                hierarchical=True,
                incremental=True,
                ledger_path=str(ledger_dir),
            )
        assert mask_calls == 2

        with patch.object(export_mod, "_sanitize_df", new=spy_sanitize_df):
            export_to_excel(
                sdk_delta,
                "STUDY_INC",
                str(excel_path),
                incremental=True,
                ledger_path=str(ledger_dir),
            )
        # Only the 2 delta rows were sanitized (historical 197 rows were NOT re-sanitized!)
        assert sanitize_df_rows == 2

        flat_loaded = json.loads(flat_json_path.read_text(encoding="utf-8"))
        assert len(flat_loaded) == 199
        by_id = {int(item["recordId"]): item for item in flat_loaded}
        assert 3 not in by_id
        assert by_id[1]["NOTE"] == "updated-1"
        assert by_id[1]["SSN"] == "***MASKED***"
        assert by_id[100][retro_key] == "CHART-100"

        excel_loaded = pd.read_excel(excel_path)
        assert len(excel_loaded) == 199
        # Historical formula remained single-quoted (not double-quoted ''=A1+B1!)
        row_100 = excel_loaded.loc[excel_loaded["recordId"] == 100].iloc[0]
        assert row_100["NOTE"] == "'=A1+B1"
        assert row_100[retro_key] == "CHART-100"

        # Now add retro_key to SensitivityRegistry at runtime and run a 1-record incremental sync:
        # existing historical items with retro_key MUST be re-masked across Excel, flat JSON, & hier JSON!
        try:
            global_sensitivity_registry.add_sensitive_key(retro_key)
            tiny_delta = [
                delta_records[0].model_copy(update={"date_modified": "2026-01-03T00:00:00Z"})
            ]
            sdk_retro = _build_study_sdk(tiny_delta, variables)

            export_to_json(
                sdk_retro,
                "STUDY_INC",
                str(flat_json_path),
                incremental=True,
                ledger_path=str(ledger_dir),
            )
            export_to_json(
                sdk_retro,
                "STUDY_INC",
                str(hier_json_path),
                hierarchical=True,
                incremental=True,
                ledger_path=str(ledger_dir),
            )
            export_to_excel(
                sdk_retro,
                "STUDY_INC",
                str(excel_path),
                incremental=True,
                ledger_path=str(ledger_dir),
            )

            flat_after = json.loads(flat_json_path.read_text(encoding="utf-8"))
            assert all(item[retro_key] == "***MASKED***" for item in flat_after)

            hier_after = json.loads(hier_json_path.read_text(encoding="utf-8"))
            hier_recs = [
                rec
                for subj in hier_after
                for visit in subj.get("visits", [])
                for fnode in visit.get("forms", [])
                for rec in fnode.get("records", [])
            ]
            assert len(hier_recs) == 199
            assert all(rec[retro_key] == "***MASKED***" for rec in hier_recs)

            excel_after = pd.read_excel(excel_path)
            assert set(excel_after[retro_key]) == {"***MASKED***"}
        finally:
            global_sensitivity_registry.remove_sensitive_key(retro_key)


class TestAllFormatsPhiParity:
    """Verify Acceptance Criterion 1: mask_phi=True and mask_phi=False parity across all 7 formats."""

    @pytest.mark.parametrize("mask_phi", [True, False])
    def test_mask_phi_true_and_false_parity_across_all_tabular_and_json_formats(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        mask_phi: bool,
    ) -> None:
        """CSV, Parquet, Hive Parquet, DuckDB, SQL, JSON, and Excel match expected values and schemas."""
        study_key = "STUDY_PARITY"
        _install_study_phi_config(monkeypatch, study_key, ["custom_phi_var"])
        monkeypatch.setattr(
            parquet_mod,
            "_cached_records_loader",
            lambda: lambda s: CachedRecordsLoader(s, cache_dir=tmp_path / f"cache_{mask_phi}"),
        )

        variables = [
            Variable(
                variable_id=1,
                variable_name="SSN",
                label="Social Security",
                variable_type="text",
                form_id=10,
                form_key="F_DM",
            ),
            Variable(
                variable_id=2,
                variable_name="custom_phi_var",
                label="Custom PHI",
                variable_type="text",
                form_id=10,
                form_key="F_DM",
            ),
            Variable(
                variable_id=3,
                variable_name="SYSBP",
                label="Systolic BP",
                variable_type="int",
                form_id=10,
                form_key="F_DM",
            ),
        ]
        records = [
            Record(
                record_id=101,
                study_key=study_key,
                subject_id=1,
                subject_key="SUBJ-P1",
                visit_id=100,
                form_id=10,
                form_key="F_DM",
                date_modified="2026-01-01T00:00:00Z",
                record_data={
                    "SSN": "123-45-6789",
                    "custom_phi_var": "CONFIDENTIAL-1",
                    "SYSBP": 120,
                },
            )
        ]
        sdk = _build_study_sdk(records, variables)

        expected_ssn = "***MASKED***" if mask_phi else "123-45-6789"
        expected_custom = "***MASKED***" if mask_phi else "CONFIDENTIAL-1"

        csv_p = tmp_path / f"out_{mask_phi}.csv"
        pq_p = tmp_path / f"out_{mask_phi}.parquet"
        hive_p = tmp_path / f"hive_{mask_phi}"
        ddb_p = tmp_path / f"out_{mask_phi}.duckdb"
        ddb_forms_p = tmp_path / f"forms_{mask_phi}.duckdb"
        sql_p = tmp_path / f"out_{mask_phi}.sqlite"
        sql_forms_p = tmp_path / f"forms_{mask_phi}.sqlite"
        json_p = tmp_path / f"out_{mask_phi}.json"
        xlsx_p = tmp_path / f"out_{mask_phi}.xlsx"

        export_to_csv(sdk, study_key, str(csv_p), mask_phi=mask_phi, quality_gate_enabled=False)
        export_to_parquet(sdk, study_key, str(pq_p), mask_phi=mask_phi, quality_gate_enabled=False)
        export_to_hive_parquet(sdk, study_key, str(hive_p), mask_phi=mask_phi)
        export_to_duckdb(
            sdk, study_key, str(ddb_p), "study_tbl", mask_phi=mask_phi, quality_gate_enabled=False
        )
        export_to_duckdb_by_form(
            sdk, study_key, str(ddb_forms_p), mask_phi=mask_phi, quality_gate_enabled=False
        )
        export_to_sql(
            sdk,
            study_key,
            "study_tbl",
            f"sqlite:///{sql_p}",
            mask_phi=mask_phi,
            quality_gate_enabled=False,
        )
        export_to_sql_by_form(
            sdk,
            study_key,
            f"sqlite:///{sql_forms_p}",
            mask_phi=mask_phi,
            quality_gate_enabled=False,
        )
        export_to_json(sdk, study_key, str(json_p), mask_phi=mask_phi, quality_gate_enabled=False)
        export_to_excel(sdk, study_key, str(xlsx_p), mask_phi=mask_phi, quality_gate_enabled=False)

        csv_df = pd.read_csv(csv_p)
        pq_df = pq.read_table(pq_p).to_pandas()
        hive_df = _read_hive_parquet_df(hive_p)
        xlsx_df = pd.read_excel(xlsx_p)
        json_rows = json.loads(json_p.read_text(encoding="utf-8"))

        with duckdb.connect(str(ddb_p), read_only=True) as con:
            ddb_df = con.execute("SELECT * FROM study_tbl").df()
        with duckdb.connect(str(ddb_forms_p), read_only=True) as con:
            ddb_form_df = con.execute("SELECT * FROM F_DM").df()

        eng1 = create_engine(f"sqlite:///{sql_p}")
        eng2 = create_engine(f"sqlite:///{sql_forms_p}")
        try:
            with eng1.connect() as conn:
                sql_df = pd.read_sql(text("SELECT * FROM study_tbl"), conn)
            with eng2.connect() as conn:
                sql_form_df = pd.read_sql(text("SELECT * FROM F_DM"), conn)
        finally:
            eng1.dispose()
            eng2.dispose()

        for label, df in (
            ("csv", csv_df),
            ("parquet", pq_df),
            ("hive_parquet", hive_df),
            ("duckdb", ddb_df),
            ("duckdb_by_form", ddb_form_df),
            ("sql", sql_df),
            ("sql_by_form", sql_form_df),
            ("excel", xlsx_df),
        ):
            assert len(df) == 1, label
            assert df["SSN"].iloc[0] == expected_ssn, label
            assert df["custom_phi_var"].iloc[0] == expected_custom, label
            assert int(df["SYSBP"].iloc[0]) == 120, label
            assert df["subjectKey"].iloc[0] == "SUBJ-P1", label

        assert len(json_rows) == 1
        assert json_rows[0]["SSN"] == expected_ssn
        assert json_rows[0]["custom_phi_var"] == expected_custom
        assert json_rows[0]["SYSBP"] == 120
        assert json_rows[0]["subjectKey"] == "SUBJ-P1"
