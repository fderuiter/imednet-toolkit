"""Unit tests for unified SPI streaming batches, ledger checkpoints, and deleted reconciliation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pandas as pd
import pytest
import respx
from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig, export_to_mongodb
from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig, export_to_neo4j
from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig, export_to_snowflake

from imednet import cli
from imednet.errors import ExportBatchError
from imednet.integrations import dispatcher
from imednet.integrations import export as export_mod
from imednet.integrations.export import (
    TabularSinkConfig,
    _tabular_export,
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
from imednet.models.intervals import Interval
from imednet.models.records import Record
from imednet.models.study_config import MappingRule, StudyConfiguration
from imednet.models.variables import Variable
from imednet.sdk import ImednetSDK
from imednet.spi import sink_base as spi_sink_base
from imednet.spi.sink_base import (
    ExportSink,
    SinkConfig,
    StateProviderProtocol,
    build_record_query_branches,
    get_state_provider,
    iter_batches,
    run_streaming_export,
)
from imednet_workflows.data_extraction import DataExtractionWorkflow
from imednet_workflows.extraction_engine import extract_canonical_records
from imednet_workflows.record_mapper import RecordMapper, transform_records_to_sdtm
from imednet_workflows.state_ledger import (
    AirflowStateProvider,
    ExtractionStateLedger,
    FileStateProvider,
)
from imednet_workflows.subject_data import SubjectDataWorkflow


def _make_record(
    record_id: int,
    *,
    subject_key: str = "SUBJ-01",
    visit_id: int = 1,
    form_id: int = 10,
    form_key: str = "AE",
    date_modified: str = "2026-01-10T10:00:00Z",
    deleted: bool = False,
    record_data: dict[str, Any] | None = None,
) -> Record:
    return Record(
        record_id=record_id,
        subject_id=100 + record_id,
        subject_key=subject_key,
        visit_id=visit_id,
        form_id=form_id,
        form_key=form_key,
        record_status="Complete",
        deleted=deleted,
        date_created="2026-01-01T00:00:00Z",
        date_modified=date_modified,
        record_data=record_data if record_data is not None else {"AGE": 30 + record_id},
    )


class _CountingSink(ExportSink):
    """Test sink recording exact upstream generator read counts per batch."""

    def __init__(
        self,
        config: SinkConfig,
        read_counter: list[int],
        *,
        fail_on_batch: int | None = None,
        fail_on_flush: bool = False,
        on_flush_check: Any = None,
    ) -> None:
        super().__init__(config)
        self._counter = read_counter
        self._fail_on_batch = fail_on_batch
        self._fail_on_flush = fail_on_flush
        self._on_flush_check = on_flush_check
        self.reads_at_batch: list[int] = []
        self.batches: list[list[Any]] = []
        self.reconciled: list[Any] = []
        self.flushed = False
        self.closed = False

    def write_batch(self, records: Any, *, batch_id: str) -> int:
        batch_idx = len(self.batches)
        self.reads_at_batch.append(self._counter[0])
        items = records.to_dict("records") if isinstance(records, pd.DataFrame) else list(records)
        if self._fail_on_batch is not None and batch_idx == self._fail_on_batch:
            raise ExportBatchError("simulated batch failure", batch_id=batch_id)
        self.batches.append(items)
        return len(items)

    def reconcile_deleted(self, records: Any, *, batch_id: str) -> int:
        _ = batch_id
        self.reconciled.extend(records)
        return len(records)

    def flush(self) -> None:
        if self._on_flush_check is not None:
            self._on_flush_check()
        if self._fail_on_flush:
            raise ExportBatchError("simulated flush failure", batch_id="flush")
        self.flushed = True

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# 1. Lazy fixed-size batching across iter_batches, dispatcher, tabular,
#    destination sinks, RecordMapper, and ImednetExportOperator
# ---------------------------------------------------------------------------


def test_iter_batches_streams_lazily_without_len() -> None:
    """iter_batches reads 0 items upfront and 2 items before batch 0 when batch_size=2."""
    reads = [0]

    def _gen() -> Iterator[int]:
        for idx in range(5):
            reads[0] += 1
            yield idx

    batch_iter = iter_batches(_gen(), 2)
    assert reads[0] == 0

    first = next(batch_iter)
    assert first == [0, 1]
    assert reads[0] == 2

    second = next(batch_iter)
    assert second == [2, 3]
    assert reads[0] == 4

    third = next(batch_iter)
    assert third == [4]
    assert reads[0] == 5

    with pytest.raises(StopIteration):
        next(batch_iter)

    with pytest.raises(ValueError, match="batch_size"):
        iter_batches(_gen(), -1)


def test_dispatcher_export_reads_two_records_before_batch_zero() -> None:
    """dispatcher.export reads 2 records before batch 0 when batch_size=2."""
    reads = [0]

    def _lazy_records(**_kwargs: Any) -> Iterator[Record]:
        for idx in range(1, 6):
            reads[0] += 1
            yield _make_record(idx)

    sdk = MagicMock()
    sdk.records.list.side_effect = _lazy_records

    created_sinks: list[_CountingSink] = []

    class _LazyTargetSink(_CountingSink):
        def __init__(self, config: SinkConfig) -> None:
            super().__init__(config, reads)
            created_sinks.append(self)

    dispatcher.register_sink_target("lazy_counting_sink", _LazyTargetSink)
    total = dispatcher.export("lazy_counting_sink", sdk, "STUDY-LAZY", batch_size=2)

    assert total == 5
    sink = created_sinks[0]
    assert sink.reads_at_batch == [2, 4, 5]
    assert sink.flushed is True
    assert sink.closed is True


def test_tabular_export_reads_two_records_before_batch_zero() -> None:
    """_tabular_export with real RecordMapper reads 2 records before batch 0 when batch_size=2."""
    reads = [0]

    def _lazy_records(**_kwargs: Any) -> Iterator[Record]:
        for idx in range(1, 6):
            reads[0] += 1
            yield _make_record(
                idx,
                record_data={"AGE": 20 + idx, "patientName": f"Person {idx}", "NOTE": "=SUM(1,2)"},
            )

    sdk = MagicMock()
    sdk.get_variables.return_value = [
        Variable(variable_name="AGE", label="Age", variable_type="int", form_id=10, form_key="AE"),
        Variable(
            variable_name="patientName",
            label="Patient Name",
            variable_type="text",
            form_id=10,
            form_key="AE",
        ),
        Variable(
            variable_name="NOTE", label="Note", variable_type="text", form_id=10, form_key="AE"
        ),
    ]
    sdk.records.list.side_effect = _lazy_records

    cfg = TabularSinkConfig(study_key="STUDY-TAB", batch_size=2, quality_gate_enabled=False)
    sink = _CountingSink(cfg, reads)

    _tabular_export(sdk, "STUDY-TAB", sink, cfg, sanitize=True)

    assert sink.reads_at_batch == [2, 4, 5]
    assert len(sink.batches) == 3
    # Verify PHI masking and CSV formula sanitization happened per streamed chunk
    first_row = sink.batches[0][0]
    assert first_row["patientName"] == "***MASKED***"
    assert first_row["NOTE"] == "'=SUM(1,2)"


def test_airflow_operator_reads_two_records_before_batch_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ImednetExportOperator.execute reads 2 records before batch 0 when batch_size=2."""
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    reads = [0]

    def _lazy_records(**_kwargs: Any) -> Iterator[Record]:
        for idx in range(1, 6):
            reads[0] += 1
            yield _make_record(idx)

    created_sinks: list[_CountingSink] = []

    def _sink_factory(config: Any) -> _CountingSink:
        inst = _CountingSink(config, reads)
        created_sinks.append(inst)
        return inst

    mock_sinks = SimpleNamespace(
        MongoDbExportSink=_sink_factory,
        MongoDbSinkConfig=MongoDbSinkConfig,
    )
    sdk = MagicMock(sinks=mock_sinks)
    sdk.records.list.side_effect = _lazy_records

    op = ImednetExportOperator(
        task_id="lazy_mongo_export",
        study_key="STUDY-AF",
        destination="mongodb",
        batch_size=2,
        export_kwargs={
            "uri": "mongodb://localhost:27017",
            "database": "clinical",
            "collection": "records",
        },
    )
    monkeypatch.setattr(op, "_get_sdk", lambda: sdk)

    op.execute({})

    assert len(created_sinks) == 1
    assert created_sinks[0].reads_at_batch == [2, 4, 5]
    assert created_sinks[0].flushed is True


def test_record_mapper_iter_dataframes_streams_lazily() -> None:
    """RecordMapper.iter_dataframes and iter_sdtm_dataframes stream chunks lazily."""
    reads = [0]

    def _lazy_records(**_kwargs: Any) -> Iterator[Record]:
        for idx in range(1, 6):
            reads[0] += 1
            yield _make_record(
                idx,
                form_key="AE",
                record_data={"aeTerm": f"Headache {idx}", "aeSerious": "Yes"},
            )

    sdk = MagicMock()
    sdk.get_variables.return_value = [
        Variable(
            variable_name="aeTerm", label="AE Term", variable_type="text", form_id=10, form_key="AE"
        ),
        Variable(
            variable_name="aeSerious",
            label="Serious",
            variable_type="text",
            form_id=10,
            form_key="AE",
        ),
    ]
    sdk.records.list.side_effect = _lazy_records

    mapper = RecordMapper(sdk, chunk_size=2)
    frames = mapper.iter_dataframes("STUDY-MAP")
    assert reads[0] == 0

    first_df = next(frames)
    assert len(first_df) == 2
    assert reads[0] == 2
    remaining = list(frames)
    assert [len(df) for df in remaining] == [2, 1]
    assert reads[0] == 5

    # SDTM chunked streaming
    reads[0] = 0
    sdtm_frames = mapper.iter_sdtm_dataframes("STUDY-MAP", domain="AE")
    first_sdtm = next(sdtm_frames)
    assert len(first_sdtm) == 2
    assert reads[0] == 2
    rest_sdtm = list(sdtm_frames)
    assert sum(len(df) for df in rest_sdtm) == 3


# ---------------------------------------------------------------------------
# 2. SPI state provider discovery & transactional checkpoints + rollback
# ---------------------------------------------------------------------------


def test_spi_discovers_pluggable_state_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spi.sink_base.get_state_provider resolves default, file, and airflow providers via entry points."""
    ledger_file = tmp_path / "ledger.json"
    default_provider = spi_sink_base.get_state_provider(ledger_path=ledger_file)
    assert isinstance(default_provider, StateProviderProtocol)
    assert isinstance(default_provider, ExtractionStateLedger)

    file_provider = get_state_provider(ledger_path=ledger_file, provider_name="file")
    assert isinstance(file_provider, FileStateProvider)

    airflow_provider = get_state_provider(provider_name="airflow")
    assert isinstance(airflow_provider, AirflowStateProvider)

    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kwargs: [])
    with pytest.raises(ImportError, match="workflows"):
        get_state_provider(provider_name="default")


def test_incremental_checkpoint_lifecycle_and_flush_ordering(tmp_path: Path) -> None:
    """Checkpoints pass dateModified>=watermark and commit ONLY after sink.flush() succeeds."""
    ledger_path = tmp_path / "state.json"
    ledger = ExtractionStateLedger(ledger_path=ledger_path)

    seen_calls: list[dict[str, Any]] = []

    def _run1_records(**kwargs: Any) -> Iterator[Record]:
        seen_calls.append(kwargs)
        yield _make_record(1, date_modified="2026-02-01T10:00:00Z")
        yield _make_record(2, date_modified="2026-02-01T12:30:00Z")

    sdk = MagicMock()
    sdk.records.list.side_effect = _run1_records

    reads = [0]
    cfg1 = SinkConfig(
        study_key="STUDY-INC",
        batch_size=2,
        incremental=True,
        stream_name="mongo_records",
        state_provider=ledger,
    )

    def _assert_not_committed_before_flush() -> None:
        # Inside flush(), transaction has NOT exited yet: on-disk status is RUNNING, not SUCCESS!
        on_disk = ledger.read_state().studies["STUDY-INC"].streams["mongo_records"]
        assert on_disk.last_run_status.value == "running"
        assert on_disk.last_timestamp != datetime(2026, 2, 1, 12, 30, tzinfo=timezone.utc)

    sink1 = _CountingSink(cfg1, reads, on_flush_check=_assert_not_committed_before_flush)
    written1 = run_streaming_export(sdk, "STUDY-INC", sink1, cfg1)
    assert written1 == 2
    assert "dateModified" not in seen_calls[0]
    committed_wm = ledger.get_last_timestamp("STUDY-INC", "mongo_records")
    assert committed_wm == datetime(2026, 2, 1, 12, 30, tzinfo=timezone.utc)

    # Run 2: passes dateModified >= 2026-02-01T12:30:00Z and advances watermark
    def _run2_records(**kwargs: Any) -> Iterator[Record]:
        seen_calls.append(kwargs)
        yield _make_record(2, date_modified="2026-02-01T12:30:00Z")
        yield _make_record(3, date_modified="2026-02-02T08:15:00Z")

    sdk.records.list.side_effect = _run2_records
    cfg2 = SinkConfig(
        study_key="STUDY-INC",
        batch_size=2,
        incremental=True,
        stream_name="mongo_records",
        ledger_path=ledger_path,
    )
    sink2 = _CountingSink(cfg2, reads)
    written2 = run_streaming_export(sdk, "STUDY-INC", sink2, cfg2)
    assert written2 == 2
    assert seen_calls[1]["dateModified"] == (">=", "2026-02-01T12:30:00Z")
    assert ledger.get_last_timestamp("STUDY-INC", "mongo_records") == datetime(
        2026, 2, 2, 8, 15, tzinfo=timezone.utc
    )


def test_batch_or_flush_failure_preserves_watermark_and_marks_failed(tmp_path: Path) -> None:
    """Batch write or flush failures leave the prior high-water mark intact and mark status failed."""
    ledger_path = tmp_path / "rollback_state.json"
    ledger = ExtractionStateLedger(ledger_path=ledger_path)
    prior_ts = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
    ledger.set_last_timestamp("STUDY-FAIL", "stream_a", prior_ts, records_processed=10)

    sdk = MagicMock()
    sdk.records.list.return_value = [
        _make_record(11, date_modified="2026-01-16T10:00:00Z"),
        _make_record(12, date_modified="2026-01-16T11:00:00Z"),
        _make_record(13, date_modified="2026-01-16T12:00:00Z"),
    ]
    reads = [0]
    cfg = SinkConfig(
        study_key="STUDY-FAIL",
        batch_size=2,
        incremental=True,
        stream_name="stream_a",
        state_provider=ledger,
    )

    # Fail on batch 1 (after batch 0 wrote 2 records)
    failing_batch_sink = _CountingSink(cfg, reads, fail_on_batch=1)
    with pytest.raises(ExportBatchError, match="simulated batch failure"):
        run_streaming_export(sdk, "STUDY-FAIL", failing_batch_sink, cfg)

    assert ledger.get_last_timestamp("STUDY-FAIL", "stream_a") == prior_ts
    entry_after_batch_fail = ledger.read_state().studies["STUDY-FAIL"].streams["stream_a"]
    assert entry_after_batch_fail.last_run_status.value == "failed"

    # Fail during flush() after all batches succeed
    failing_flush_sink = _CountingSink(cfg, reads, fail_on_flush=True)
    with pytest.raises(ExportBatchError, match="simulated flush failure"):
        run_streaming_export(sdk, "STUDY-FAIL", failing_flush_sink, cfg)

    assert ledger.get_last_timestamp("STUDY-FAIL", "stream_a") == prior_ts
    entry_after_flush_fail = ledger.read_state().studies["STUDY-FAIL"].streams["stream_a"]
    assert entry_after_flush_fail.last_run_status.value == "failed"


def test_airflow_state_provider_incremental_checkpoint_and_failure_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AirflowStateProvider passes dateModified filter, updates checkpoint, and rolls back on error."""
    from apache_airflow_providers_imednet.operators.export import ImednetExportOperator

    xcom_store: dict[str, dict[str, Any]] = {}

    class _FakeTI:
        @staticmethod
        def xcom_pull(*, key: str, include_prior_dates: bool = False) -> Any:
            _ = include_prior_dates
            return xcom_store.get(key)

        @staticmethod
        def xcom_push(*, key: str, value: Any) -> None:
            xcom_store[key] = value

    python_op_mod = ModuleType("airflow.operators.python")
    python_op_mod.get_current_context = lambda: {"ti": _FakeTI()}  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "airflow.operators.python", python_op_mod)
    monkeypatch.setitem(__import__("sys").modules, "airflow.models.xcom", None)

    provider = AirflowStateProvider()
    seen_kwargs: list[dict[str, Any]] = []

    def _run1(**kwargs: Any) -> list[Record]:
        seen_kwargs.append(kwargs)
        return [_make_record(1, date_modified="2026-03-01T14:00:00Z")]

    reads = [0]
    mock_sinks = SimpleNamespace(
        MongoDbExportSink=lambda config: _CountingSink(config, reads),
        MongoDbSinkConfig=MongoDbSinkConfig,
    )
    sdk = MagicMock(sinks=mock_sinks)
    sdk.records.list.side_effect = _run1

    op1 = ImednetExportOperator(
        task_id="af_inc_1",
        study_key="STUDY-AF-INC",
        destination="mongodb",
        incremental=True,
        stream_name="mongo_af",
        state_provider=provider,
        export_kwargs={"uri": "mongodb://localhost:27017", "database": "db", "collection": "col"},
    )
    monkeypatch.setattr(op1, "_get_sdk", lambda: sdk)
    op1.execute({})

    assert provider.get_last_timestamp("STUDY-AF-INC", "mongo_af") == datetime(
        2026, 3, 1, 14, 0, tzinfo=timezone.utc
    )

    # Second run fails mid-stream -> prior watermark preserved & status marked failed
    def _run2(**kwargs: Any) -> list[Record]:
        seen_kwargs.append(kwargs)
        return [_make_record(2, date_modified="2026-03-02T10:00:00Z")]

    sdk.records.list.side_effect = _run2
    sdk.sinks = SimpleNamespace(
        MongoDbExportSink=lambda config: _CountingSink(config, reads, fail_on_batch=0),
        MongoDbSinkConfig=MongoDbSinkConfig,
    )
    op2 = ImednetExportOperator(
        task_id="af_inc_2",
        study_key="STUDY-AF-INC",
        destination="mongodb",
        incremental=True,
        stream_name="mongo_af",
        state_provider=provider,
        export_kwargs={"uri": "mongodb://localhost:27017", "database": "db", "collection": "col"},
    )
    monkeypatch.setattr(op2, "_get_sdk", lambda: sdk)

    with pytest.raises(ExportBatchError):
        op2.execute({})

    assert seen_kwargs[1]["dateModified"] == (">=", "2026-03-01T14:00:00Z")
    assert provider.get_last_timestamp("STUDY-AF-INC", "mongo_af") == datetime(
        2026, 3, 1, 14, 0, tzinfo=timezone.utc
    )
    state_payload = xcom_store["state_STUDY-AF-INC_mongo_af"]
    assert state_payload["status"] == "failed"


# ---------------------------------------------------------------------------
# 3. Voided (deleted=True) exclusion & include_deleted=True across workflows
# ---------------------------------------------------------------------------


def test_workflows_and_mapper_exclude_deleted_unless_opted_in() -> None:
    """RecordMapper, DataExtractionWorkflow, SubjectDataWorkflow, and extract_canonical_records omit deleted=True."""
    active_rec = _make_record(
        1,
        subject_key="SUBJ-01",
        form_key="AE",
        deleted=False,
        record_data={"aeTerm": "Nausea", "aeSerious": "No", "AE_TERM": "Nausea"},
    )
    voided_rec = _make_record(
        2,
        subject_key="SUBJ-01",
        form_key="AE",
        deleted=True,
        record_data={"aeTerm": "Syncope", "aeSerious": "Yes", "AE_TERM": "Syncope"},
    )

    sdk = MagicMock()
    sdk.get_variables.return_value = [
        Variable(
            variable_name="aeTerm", label="AE Term", variable_type="text", form_id=10, form_key="AE"
        ),
        Variable(
            variable_name="aeSerious",
            label="Serious",
            variable_type="text",
            form_id=10,
            form_key="AE",
        ),
    ]
    sdk.records.list.side_effect = lambda **_kw: iter([active_rec, voided_rec])
    sdk.get_records.side_effect = lambda *_a, **_kw: [active_rec, voided_rec]
    sdk.get_studies.return_value = [SimpleNamespace(study_key="S1")]
    sdk.get_forms.return_value = [Form(form_id=10, form_key="AE", form_name="AE")]
    sdk.get_intervals.return_value = [
        Interval(
            interval_id=1,
            interval_name="Visit 1",
            forms=[{"formId": 10, "formKey": "AE", "formName": "AE"}],
        )
    ]
    sdk.get_subjects.return_value = [SimpleNamespace(subject_key="SUBJ-01", status="Active")]
    sdk.get_visits.return_value = [SimpleNamespace(visit_id=1, interval_id=1)]
    sdk.get_queries.return_value = []

    # 1. RecordMapper (dataframe, sdtm_dataframe, build_hierarchy, transform_records_to_sdtm)
    mapper = RecordMapper(sdk)
    df_default = mapper.dataframe("S1")
    assert list(df_default["recordId"]) == [1]

    df_audit = mapper.dataframe("S1", include_deleted=True)
    assert list(df_audit["recordId"]) == [1, 2]

    sdtm_default = mapper.sdtm_dataframe("S1", domain="AE")
    assert len(sdtm_default) == 1
    sdtm_audit = mapper.sdtm_dataframe("S1", domain="AE", include_deleted=True)
    assert len(sdtm_audit) == 2

    tree_default = mapper.build_hierarchy("S1")
    assert len(tree_default[0]["visits"][0]["forms"][0]["records"]) == 1
    tree_audit = mapper.build_hierarchy("S1", include_deleted=True)
    assert len(tree_audit[0]["visits"][0]["forms"][0]["records"]) == 2

    fn_sdtm_default = transform_records_to_sdtm(
        [active_rec, voided_rec], domain="AE", study_key="S1"
    )
    assert len(fn_sdtm_default) == 1
    fn_sdtm_audit = transform_records_to_sdtm(
        [active_rec, voided_rec], domain="AE", study_key="S1", include_deleted=True
    )
    assert len(fn_sdtm_audit) == 2

    # 2. DataExtractionWorkflow
    dew = DataExtractionWorkflow(sdk)
    extracted_default = dew.extract_records_by_criteria("S1")
    assert [r.record_id for r in extracted_default] == [1]
    extracted_audit = dew.extract_records_by_criteria("S1", include_deleted=True)
    assert [r.record_id for r in extracted_audit] == [1, 2]

    # 3. SubjectDataWorkflow
    sdw = SubjectDataWorkflow(sdk)
    subj_default = sdw.get_all_subject_data("S1", "SUBJ-01")
    assert [r.record_id for r in subj_default.records] == [1]
    subj_audit = sdw.get_all_subject_data("S1", "SUBJ-01", include_deleted=True)
    assert [r.record_id for r in subj_audit.records] == [1, 2]

    # 4. extract_canonical_records
    study_cfg = StudyConfiguration(
        study_key="S1",
        mappings=[
            MappingRule(
                domain="AE",
                target_field="subjectKey",
                source_form_key="AE",
                source_variable_name="subjectKey",
            ),
            MappingRule(
                domain="AE",
                target_field="aeTerm",
                source_form_key="AE",
                source_variable_name="AE_TERM",
            ),
            MappingRule(
                domain="AE",
                target_field="aeSeverity",
                source_form_key="AE",
                source_variable_name="AE_SEV",
                fallback_value="MILD",
            ),
        ],
    )
    canon_default = extract_canonical_records([active_rec, voided_rec], study_cfg)
    assert [ev.ae_term for ev in canon_default.adverse_events] == ["Nausea"]

    canon_audit = extract_canonical_records(
        [active_rec, voided_rec], study_cfg, include_deleted=True
    )
    assert [ev.ae_term for ev in canon_audit.adverse_events] == ["Nausea", "Syncope"]


# ---------------------------------------------------------------------------
# 4. Incremental voided-record reconciliation across MongoDB, Neo4j, Snowflake,
#    SQL, and DuckDB
# ---------------------------------------------------------------------------


def test_destination_sinks_reconcile_newly_voided_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MongoDB, Neo4j, and Snowflake sinks delete newly voided records on incremental syncs."""
    import imednet_sinks.document as doc_mod
    import imednet_sinks.graph as graph_mod
    import imednet_sinks.warehouse as wh_mod

    active = _make_record(101, date_modified="2026-04-02T10:00:00Z", deleted=False)
    voided = _make_record(102, date_modified="2026-04-02T11:00:00Z", deleted=True)

    sdk = MagicMock()
    sdk.records.list.side_effect = lambda **_kw: iter([active, voided])

    # MongoDB reconciliation
    pymongo_mod = MagicMock()
    collection = MagicMock()
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    client = MagicMock()
    client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)

    ledger_mongo = ExtractionStateLedger(ledger_path=tmp_path / "mongo_ledger.json")
    mongo_cfg = MongoDbSinkConfig(
        study_key="S-RECON",
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="records",
        incremental=True,
        state_provider=ledger_mongo,
    )
    written_mongo = export_to_mongodb(sdk, "S-RECON", config=mongo_cfg)
    assert written_mongo == 1
    collection.bulk_write.assert_called_once()
    collection.delete_many.assert_called_once_with({"_id": {"$in": ["S-RECON/102"]}})
    assert ledger_mongo.get_last_timestamp("S-RECON", "mongodb_records") == datetime(
        2026, 4, 2, 11, 0, tzinfo=timezone.utc
    )

    # Neo4j reconciliation
    neo4j_mod = MagicMock()
    session = MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    driver = MagicMock()
    driver.session.return_value = session_cm
    neo4j_mod.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    ledger_neo = ExtractionStateLedger(ledger_path=tmp_path / "neo_ledger.json")
    neo_cfg = Neo4jSinkConfig(
        study_key="S-RECON",
        uri="bolt://localhost:7687",
        auth=("neo4j", "pass"),
        incremental=True,
        state_provider=ledger_neo,
    )
    written_neo = export_to_neo4j(sdk, "S-RECON", config=neo_cfg)
    assert written_neo == 1
    cypher_calls = [c.args[0] for c in session.run.call_args_list]
    assert any("DETACH DELETE r" in q for q in cypher_calls)

    # Snowflake reconciliation + incremental upsert DELETE
    sf_mod = MagicMock()
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cursor
    sf_mod.connect.return_value = conn
    import pyarrow as pa
    import pyarrow.parquet as pq

    monkeypatch.setattr(
        wh_mod,
        "_require_optional_dep",
        lambda pkg, _ex: (
            sf_mod if pkg == "snowflake.connector" else (pq if "parquet" in pkg else pa)
        ),
    )

    ledger_sf = ExtractionStateLedger(ledger_path=tmp_path / "sf_ledger.json")
    sf_cfg = SnowflakeSinkConfig(
        study_key="S-RECON",
        account="acct",
        user="usr",
        password="pwd",  # pragma: allowlist secret
        database="DB",
        schema="PUBLIC",
        warehouse="WH",
        stage="STG",
        table="RECORDS",
        local_staging_dir=tmp_path / "sf_stage",
        incremental=True,
        state_provider=ledger_sf,
    )
    written_sf = export_to_snowflake(sdk, "S-RECON", config=sf_cfg)
    assert written_sf == 1
    executed_sqls = [c.args[0] for c in cursor.execute.call_args_list]
    delete_calls = [c for c in cursor.execute.call_args_list if "DELETE FROM" in c.args[0]]
    # Active record 101 upserts via staged MERGE INTO; voided record 102 reconciles via DELETE FROM
    assert len(delete_calls) == 1
    assert delete_calls[0].args[1] == ["S-RECON", 102]
    assert any("MERGE INTO" in sql for sql in executed_sqls)
    assert any("COPY INTO" in sql for sql in executed_sqls)

    # Direct unit check when idempotent=False -> reconcile_deleted is a no-op
    non_idemp_mongo = MongoDbExportSink(
        MongoDbSinkConfig(
            study_key="S-RECON",
            uri="mongodb://localhost:27017",
            database="clinical",
            collection="records",
            idempotent=False,
        )
    )
    assert non_idemp_mongo.reconcile_deleted([voided], batch_id="b0") == 0
    non_idemp_neo = Neo4jExportSink(
        Neo4jSinkConfig(
            study_key="S-RECON",
            uri="bolt://localhost:7687",
            auth=("neo4j", "pass"),
            idempotent=False,
        )
    )
    assert non_idemp_neo.reconcile_deleted([voided], batch_id="b0") == 0
    non_idemp_sf = SnowflakeExportSink(
        SnowflakeSinkConfig(
            study_key="S-RECON",
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="RECORDS",
            local_staging_dir=tmp_path / "sf_stage2",
            idempotent=False,
        )
    )
    assert non_idemp_sf.reconcile_deleted([voided], batch_id="b0") == 0


def test_tabular_sql_duckdb_parquet_excel_json_streaming_and_reconciliation(
    tmp_path: Path,
) -> None:
    """SQL and DuckDB incremental runs upsert modified rows and delete newly voided rows."""
    duckdb = pytest.importorskip("duckdb")

    sdk = MagicMock()
    sdk.get_variables.return_value = [
        Variable(variable_name="AGE", label="Age", variable_type="int", form_id=10, form_key="AE")
    ]
    sdk.forms.list.return_value = [SimpleNamespace(form_id=10, form_key="AE")]
    sdk.variables.list.return_value = [
        Variable(variable_name="AGE", label="Age", variable_type="int", form_id=10, form_key="AE")
    ]

    # Run 1: records 1 and 2 active
    r1_v1 = _make_record(1, date_modified="2026-05-01T10:00:00Z", record_data={"AGE": 40})
    r2_v1 = _make_record(2, date_modified="2026-05-01T11:00:00Z", record_data={"AGE": 50})
    sdk.records.list.side_effect = lambda **_kw: iter([r1_v1, r2_v1])

    sql_db = tmp_path / "inc.sqlite"
    conn_str = f"sqlite:///{sql_db}"
    sql_ledger = tmp_path / "sql_ledger.json"

    export_to_sql(
        sdk,
        "S-TAB",
        "records",
        conn_str,
        batch_size=1,
        incremental=True,
        ledger_path=sql_ledger,
    )

    ddb_path = tmp_path / "inc.duckdb"
    ddb_ledger = tmp_path / "ddb_ledger.json"
    export_to_duckdb(
        sdk,
        "S-TAB",
        str(ddb_path),
        "records",
        batch_size=1,
        incremental=True,
        ledger_path=ddb_ledger,
    )

    # Run 2: record 1 updated to AGE=41, record 2 voided (deleted=True), record 3 added
    r1_v2 = _make_record(1, date_modified="2026-05-02T09:00:00Z", record_data={"AGE": 41})
    r2_voided = _make_record(
        2, date_modified="2026-05-02T09:30:00Z", deleted=True, record_data={"AGE": 50}
    )
    r3_v1 = _make_record(3, date_modified="2026-05-02T10:00:00Z", record_data={"AGE": 60})
    sdk.records.list.side_effect = lambda **_kw: iter([r1_v2, r2_voided, r3_v1])

    export_to_sql(
        sdk,
        "S-TAB",
        "records",
        conn_str,
        batch_size=2,
        incremental=True,
        ledger_path=sql_ledger,
    )
    with sqlite3.connect(sql_db) as sconn:
        sql_rows = sconn.execute(
            'SELECT "recordId", "AGE" FROM records ORDER BY "recordId"'
        ).fetchall()
    assert sql_rows == [(1, 41), (3, 60)]

    export_to_duckdb(
        sdk,
        "S-TAB",
        str(ddb_path),
        "records",
        batch_size=2,
        incremental=True,
        ledger_path=ddb_ledger,
    )
    with duckdb.connect(str(ddb_path)) as dconn:
        ddb_rows = dconn.execute(
            'SELECT "recordId", "AGE" FROM records ORDER BY "recordId"'
        ).fetchall()
    assert ddb_rows == [(1, 41), (3, 60)]

    # Verify streaming Parquet, Excel, JSON, CSV, by-form, and long-SQL with streaming kwargs
    sdk.records.list.side_effect = lambda **_kw: iter([r1_v2, r2_voided, r3_v1])
    pq_file = tmp_path / "stream.parquet"
    export_to_parquet(sdk, "S-TAB", str(pq_file), batch_size=2, modified_since="2026-05-02")
    pq_df = pd.read_parquet(pq_file)
    assert list(pq_df["recordId"]) == [1, 3]

    xl_file = tmp_path / "stream.xlsx"
    export_to_excel(sdk, "S-TAB", str(xl_file), batch_size=2, modified_since="2026-05-02")
    assert xl_file.exists()

    json_file = tmp_path / "stream.json"
    export_to_json(sdk, "S-TAB", str(json_file), batch_size=2, modified_since="2026-05-02")
    json_rows = json.loads(json_file.read_text(encoding="utf-8"))
    assert [r["recordId"] for r in json_rows] == [1, 3]

    csv_file = tmp_path / "stream.csv"
    export_to_csv(sdk, "S-TAB", str(csv_file), batch_size=2, modified_since="2026-05-02")
    csv_df = pd.read_csv(csv_file)
    assert list(csv_df["recordId"]) == [1, 3]

    by_form_ddb = tmp_path / "by_form.duckdb"
    export_to_duckdb_by_form(sdk, "S-TAB", str(by_form_ddb), batch_size=1)
    with duckdb.connect(str(by_form_ddb)) as dconn:
        assert dconn.execute('SELECT COUNT(*) FROM "AE"').fetchone()[0] == 2

    by_form_sql = tmp_path / "by_form.sqlite"
    export_to_sql_by_form(sdk, "S-TAB", f"sqlite:///{by_form_sql}", batch_size=1)
    with sqlite3.connect(by_form_sql) as sconn:
        assert sconn.execute('SELECT COUNT(*) FROM "AE"').fetchone()[0] == 2

    long_sql = tmp_path / "long.sqlite"
    export_to_long_sql(
        sdk, "S-TAB", "long_tbl", f"sqlite:///{long_sql}", modified_since="2026-05-02"
    )
    with sqlite3.connect(long_sql) as sconn:
        assert sconn.execute("SELECT COUNT(*) FROM long_tbl").fetchone()[0] >= 2


# ---------------------------------------------------------------------------
# 5. Connector exclusivity & multi-branch OR + watermark HTTP query safety
# ---------------------------------------------------------------------------


def test_or_filter_with_watermark_never_mixes_and_or_connectors(tmp_path: Path) -> None:
    """OR filters combined with watermarks issue separate AND-only HTTP queries and deduplicate."""
    branches = build_record_query_branches(
        record_filter="subjectKey==SUBJ-01,subjectKey==SUBJ-02",
        watermark="2026-06-01T00:00:00Z",
        exclude_deleted_upstream=True,
    )
    assert len(branches) == 2

    with pytest.raises(ValueError, match="cannot combine both"):
        SinkConfig(study_key="S1", record_filter="subjectKey==SUBJ-01;visitId==1,visitId==2")

    seen_http_filters: list[str] = []
    with respx.mock(assert_all_called=True) as router:

        def _handler(request: httpx.Request) -> httpx.Response:
            flt = request.url.params.get("filter", "")
            seen_http_filters.append(flt)
            # Upstream API forbids mixing ';' and ',' in a single filter param
            assert not (";" in flt and "," in flt)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "recordId": 501,
                            "subjectKey": "SUBJ-01",
                            "visitId": 1,
                            "formId": 10,
                            "formKey": "AE",
                            "deleted": False,
                            "dateModified": "2026-06-02T00:00:00Z",
                            "recordData": {"AGE": 33},
                        }
                    ]
                },
            )

        router.get("https://api.test/api/v1/edc/studies/S-HTTP/records").mock(side_effect=_handler)
        router.get("https://api.test/api/v1/edc/studies/S-HTTP/variables").respond(
            200,
            json={
                "data": [
                    {
                        "variableName": "AGE",
                        "variableType": "int",
                        "label": "Age",
                        "formId": 10,
                        "formKey": "AE",
                    }
                ]
            },
        )

        with ImednetSDK(
            api_key="k",
            security_key="s",
            base_url="https://api.test",
        ) as real_sdk:
            out_csv = tmp_path / "or_branch.csv"
            export_to_csv(
                real_sdk,
                "S-HTTP",
                str(out_csv),
                record_filter="subjectKey==SUBJ-01,subjectKey==SUBJ-02",
                modified_since="2026-06-01T00:00:00Z",
            )
            df = pd.read_csv(out_csv)
            # Duplicate recordId 501 returned across both OR branches is deduplicated!
            assert list(df["recordId"]) == [501]
            assert len(seen_http_filters) == 2
            assert all("dateModified>=2026-06-01T00:00:00Z" in f for f in seen_http_filters)


# ---------------------------------------------------------------------------
# 6. CLI `imednet export` flags across all subcommands
# ---------------------------------------------------------------------------


def _invoke_cli(args: list[str]) -> tuple[int, str, str]:
    import io
    from contextlib import redirect_stderr, redirect_stdout

    out = io.StringIO()
    err = io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cli.app(args)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize(
    "subcommand",
    ["parquet", "csv", "excel", "json", "duckdb", "sql", "mongodb", "neo4j", "snowflake"],
)
def test_cli_export_all_subcommands_expose_streaming_flags(subcommand: str) -> None:
    """Every `imednet export` subcommand accepts --since, --filter, --incremental, --include-deleted."""
    code, stdout, _stderr = _invoke_cli(["export", subcommand, "--help"])
    assert code == 0
    for flag in ("--since", "--filter", "--incremental", "--include-deleted"):
        assert flag in stdout


def test_cli_export_passes_streaming_flags_and_validates_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI export forwards --since, --filter, --incremental, and --include-deleted to targets."""
    import importlib.util

    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    mock_sdk = MagicMock()
    mock_sdk.sinks.MongoDbSinkConfig = MongoDbSinkConfig
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))

    mock_csv = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_csv", mock_csv)
    monkeypatch.setattr(cli, "export_to_csv", mock_csv)

    ledger_file = tmp_path / "cli_ledger.json"
    code, _out, err = _invoke_cli(
        [
            "export",
            "csv",
            "STUDY1",
            str(tmp_path / "out.csv"),
            "--since",
            "2026-01-01T00:00:00Z",
            "--filter",
            "subjectKey==SUBJ-01",
            "--incremental",
            "--include-deleted",
            "--stream-name",
            "cli_csv",
            "--ledger-path",
            str(ledger_file),
        ]
    )
    assert code == 0, err
    mock_csv.assert_called_once_with(
        mock_sdk,
        "STUDY1",
        str(tmp_path / "out.csv"),
        use_labels_as_columns=False,
        modified_since="2026-01-01T00:00:00Z",
        record_filter={"subjectKey": ("==", "SUBJ-01")},
        incremental=True,
        include_deleted=True,
        stream_name="cli_csv",
        ledger_path=ledger_file,
    )

    # Sink CLI forwarding (mongodb)
    code_m, _out_m, err_m = _invoke_cli(
        [
            "export",
            "mongodb",
            "STUDY1",
            "mongodb://localhost:27017",
            "db",
            "records",
            "--since",
            "2026-02-01",
            "--filter",
            "subjectKey==SUBJ-01,subjectKey==SUBJ-02",
            "--incremental",
            "--include-deleted",
        ]
    )
    assert code_m == 0, err_m
    called_cfg = mock_sdk.sinks.export_to_mongodb.call_args.kwargs["config"]
    assert isinstance(called_cfg, MongoDbSinkConfig)
    assert called_cfg.modified_since == "2026-02-01"
    assert called_cfg.record_filter == "subjectKey==SUBJ-01,subjectKey==SUBJ-02"
    assert called_cfg.incremental is True
    assert called_cfg.include_deleted is True

    # Invalid mixed connector filter exits 1
    bad_code, _bad_out, bad_err = _invoke_cli(
        [
            "export",
            "csv",
            "STUDY1",
            str(tmp_path / "bad.csv"),
            "--filter",
            "subjectKey==SUBJ-01;visitId==1,visitId==2",
        ]
    )
    assert bad_code == 1
    assert "Error" in bad_err

    # Invalid --since exits 1
    bad_since_code, _bs_out, bs_err = _invoke_cli(
        [
            "export",
            "csv",
            "STUDY1",
            str(tmp_path / "bad2.csv"),
            "--since",
            "not-a-timestamp",
        ]
    )
    assert bad_since_code == 1
    assert "Error" in bs_err

    # Invalid --record-data-filter mixed connectors exits 1
    bad_rdf_code, _br_out, br_err = _invoke_cli(
        [
            "export",
            "csv",
            "STUDY1",
            str(tmp_path / "bad3.csv"),
            "--record-data-filter",
            "AGE>=18;SEX==F,SEX==M",
        ]
    )
    assert bad_rdf_code == 1
    assert "Error" in br_err


def test_spi_record_and_watermark_helpers() -> None:
    """SPI helper functions handle naive/aware datetimes, dict records, and invalid watermarks."""
    naive = datetime(2026, 7, 1, 12, 0)
    assert spi_sink_base.normalize_watermark_datetime(naive) == datetime(
        2026, 7, 1, 12, 0, tzinfo=timezone.utc
    )
    assert spi_sink_base.normalize_watermark_datetime("   ") is None
    with pytest.raises(ValueError, match="Watermark"):
        spi_sink_base.normalize_watermark_datetime(12345)  # type: ignore[arg-type]

    rec_dict = {"recordId": 77, "deleted": True, "dateModified": "2026-07-02T01:02:03Z"}
    assert spi_sink_base.extract_record_id(rec_dict) == 77
    assert spi_sink_base.is_record_deleted(rec_dict) is True
    assert spi_sink_base.is_record_deleted({"recordId": 78, "deleted": False}) is False
    assert spi_sink_base.extract_record_timestamp(rec_dict) == datetime(
        2026, 7, 2, 1, 2, 3, tzinfo=timezone.utc
    )


def test_parquet_consistent_schema_all_null_first_chunk_followed_by_populated_chunk(
    tmp_path: Path,
) -> None:
    """Parquet streaming keeps a stable schema when chunk 0 is all-null and chunk 1 is populated."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from imednet.integrations.export import TabularParquetSink

    sdk = MagicMock()
    sdk.get_variables.return_value = [
        Variable(variable_name="AGE", label="Age", variable_type="int", form_id=10, form_key="AE"),
        Variable(
            variable_name="WEIGHT",
            label="Weight",
            variable_type="float",
            form_id=10,
            form_key="AE",
        ),
        Variable(
            variable_name="SERIOUS",
            label="Serious",
            variable_type="bool",
            form_id=10,
            form_key="AE",
        ),
        Variable(
            variable_name="NOTE",
            label="Note",
            variable_type="text",
            form_id=10,
            form_key="AE",
        ),
        Variable(
            variable_name="UNTYPED_SCORE",
            label="Untyped Score",
            variable_type=None,
            form_id=10,
            form_key="AE",
        ),
    ]

    # Chunk 0 (records 1 & 2): every clinical variable is None across the entire first batch.
    # Chunk 1 (records 3 & 4): populated values + partial-null int row (pandas float64-with-NaN).
    records_batch = [
        _make_record(
            1,
            record_data={
                "AGE": None,
                "WEIGHT": None,
                "SERIOUS": None,
                "NOTE": None,
                "UNTYPED_SCORE": None,
            },
        ),
        _make_record(
            2,
            record_data={
                "AGE": None,
                "WEIGHT": None,
                "SERIOUS": None,
                "NOTE": None,
                "UNTYPED_SCORE": None,
            },
        ),
        _make_record(
            3,
            record_data={
                "AGE": 42,
                "WEIGHT": 71.5,
                "SERIOUS": True,
                "NOTE": "Recovered",
                "UNTYPED_SCORE": 88,
            },
        ),
        _make_record(
            4,
            record_data={
                "AGE": None,
                "WEIGHT": 68.0,
                "SERIOUS": False,
                "NOTE": "Stable",
                "UNTYPED_SCORE": 95,
            },
        ),
    ]
    sdk.records.list.side_effect = lambda **_kw: iter(records_batch)

    pq_file = tmp_path / "all_null_first_chunk.parquet"
    export_to_parquet(sdk, "S-PQ", str(pq_file), batch_size=2)

    arrow_tbl = pq.read_table(pq_file)
    assert arrow_tbl.schema.field("AGE").type == pa.int64()
    assert arrow_tbl.schema.field("WEIGHT").type == pa.float64()
    assert arrow_tbl.schema.field("SERIOUS").type == pa.bool_()
    assert arrow_tbl.schema.field("NOTE").type == pa.string()
    assert arrow_tbl.schema.field("UNTYPED_SCORE").type == pa.int64()

    pydict = arrow_tbl.to_pydict()
    assert pydict["recordId"] == [1, 2, 3, 4]
    assert pydict["AGE"] == [None, None, 42, None]
    assert pydict["WEIGHT"] == [None, None, 71.5, 68.0]
    assert pydict["SERIOUS"] == [None, None, True, False]
    assert pydict["NOTE"] == [None, None, "Recovered", "Stable"]
    assert pydict["UNTYPED_SCORE"] == [None, None, 88, 95]

    # Standalone TabularParquetSink without contract metadata: null -> int64 -> float64 + new col
    standalone_pq = tmp_path / "standalone_evolve.parquet"
    standalone_cfg = TabularSinkConfig(study_key="S-PQ", batch_size=1)
    with TabularParquetSink(str(standalone_pq), standalone_cfg) as sink:
        sink.write_batch([{"recordId": 1, "SCORE": None, "TAG": None}], batch_id="b0")
        sink.write_batch([{"recordId": 2, "SCORE": 10, "TAG": "ok"}], batch_id="b1")
        sink.write_batch(
            [{"recordId": 3, "SCORE": 12.5, "TAG": 99, "EXTRA": "late"}], batch_id="b2"
        )

    evolved_tbl = pq.read_table(standalone_pq)
    assert evolved_tbl.schema.field("SCORE").type == pa.float64()
    assert evolved_tbl.schema.field("TAG").type == pa.string()
    assert evolved_tbl.schema.field("EXTRA").type == pa.string()
    assert evolved_tbl.to_pydict()["SCORE"] == [None, 10.0, 12.5]
    assert evolved_tbl.to_pydict()["TAG"] == [None, "ok", "99"]
    assert evolved_tbl.to_pydict()["EXTRA"] == [None, None, "late"]

    # Zero-row incremental no-op preserves existing Parquet file; full zero-row replaces it
    sdk.records.list.side_effect = lambda **_kw: iter([])
    export_to_parquet(
        sdk,
        "S-PQ",
        str(pq_file),
        incremental=True,
        ledger_path=tmp_path / "pq_ledger.json",
    )
    assert pq.read_table(pq_file).num_rows == 4

    export_to_parquet(sdk, "S-PQ", str(pq_file))
    empty_pq_tbl = pq.read_table(pq_file)
    assert empty_pq_tbl.num_rows == 0
    assert "AGE" in empty_pq_tbl.column_names


def test_duckdb_zero_row_full_export_replaces_snapshot_and_incremental_noop_preserves(
    tmp_path: Path,
) -> None:
    """Pre-populated DuckDB table clears on full zero-row export and stays intact on incremental no-op."""
    duckdb = pytest.importorskip("duckdb")
    from imednet.integrations.export import TabularDuckDBSink

    sdk = MagicMock()
    sdk.get_variables.return_value = [
        Variable(variable_name="AGE", label="Age", variable_type="int", form_id=10, form_key="AE"),
        Variable(
            variable_name="NOTE", label="Note", variable_type="text", form_id=10, form_key="AE"
        ),
    ]
    sdk.forms.list.return_value = [SimpleNamespace(form_id=10, form_key="AE")]
    sdk.variables.list.return_value = list(sdk.get_variables.return_value)

    # 1. Pre-populate DuckDB target (and verify all-null batch 0 + string batch 1 works in DuckDB too)
    r1 = _make_record(
        1, date_modified="2026-05-01T10:00:00Z", record_data={"AGE": None, "NOTE": None}
    )
    r2 = _make_record(
        2, date_modified="2026-05-01T11:00:00Z", record_data={"AGE": 50, "NOTE": "active"}
    )
    sdk.records.list.side_effect = lambda **_kw: iter([r1, r2])

    ddb_path = tmp_path / "snapshot.duckdb"
    ledger_path = tmp_path / "ddb_snapshot_ledger.json"
    export_to_duckdb(sdk, "S-DDB", str(ddb_path), "records", batch_size=1)

    with duckdb.connect(str(ddb_path)) as conn:
        initial_rows = conn.execute(
            'SELECT "recordId", "AGE", "NOTE" FROM "records" ORDER BY "recordId"'
        ).fetchall()
    assert initial_rows == [(1, None, None), (2, 50, "active")]

    # 2. Incremental no-op (zero rows returned): pre-populated target MUST stay intact
    sdk.records.list.side_effect = lambda **_kw: iter([])
    export_to_duckdb(
        sdk,
        "S-DDB",
        str(ddb_path),
        "records",
        incremental=True,
        ledger_path=ledger_path,
    )
    export_to_duckdb(
        sdk,
        "S-DDB",
        str(ddb_path),
        "records",
        modified_since="2026-06-01T00:00:00Z",
    )
    with duckdb.connect(str(ddb_path)) as conn:
        after_inc_rows = conn.execute(
            'SELECT "recordId", "AGE", "NOTE" FROM "records" ORDER BY "recordId"'
        ).fetchall()
    assert after_inc_rows == [(1, None, None), (2, 50, "active")]

    # 3. Full zero-row export: pre-populated target MUST be replaced with an empty snapshot
    export_to_duckdb(sdk, "S-DDB", str(ddb_path), "records")
    with duckdb.connect(str(ddb_path)) as conn:
        after_full_empty = conn.execute(
            'SELECT "recordId", "AGE", "NOTE" FROM "records"'
        ).fetchall()
    assert after_full_empty == []

    # 4. Full zero-row export when study has zero variables also clears pre-populated snapshot
    sdk.records.list.side_effect = lambda **_kw: iter([r2])
    export_to_duckdb(sdk, "S-DDB", str(ddb_path), "records")
    with duckdb.connect(str(ddb_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM "records"').fetchone()[0] == 1

    sdk.get_variables.return_value = []
    sdk.records.list.side_effect = lambda **_kw: iter([])
    export_to_duckdb(sdk, "S-DDB", str(ddb_path), "records")
    with duckdb.connect(str(ddb_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM "records"').fetchone()[0] == 0

    # 5. Standalone TabularDuckDBSink without contract metadata + export_to_duckdb_by_form
    with TabularDuckDBSink(
        str(ddb_path), "standalone_tbl", TabularSinkConfig(study_key="S-DDB")
    ) as sink:
        sink.write_batch([{"recordId": 10, "VAL": "x"}], batch_id="b0")
    with TabularDuckDBSink(
        str(ddb_path),
        "standalone_tbl",
        TabularSinkConfig(study_key="S-DDB", incremental=True),
    ):
        pass
    with duckdb.connect(str(ddb_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM "standalone_tbl"').fetchone()[0] == 1
    with TabularDuckDBSink(str(ddb_path), "standalone_tbl", TabularSinkConfig(study_key="S-DDB")):
        pass
    with duckdb.connect(str(ddb_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM "standalone_tbl"').fetchone()[0] == 0

    by_form_path = tmp_path / "by_form_empty.duckdb"
    sdk.records.list.side_effect = lambda **_kw: iter([r2])
    export_to_duckdb_by_form(sdk, "S-DDB", str(by_form_path))
    with duckdb.connect(str(by_form_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM "AE"').fetchone()[0] == 1

    sdk.records.list.side_effect = lambda **_kw: iter([])
    export_to_duckdb_by_form(sdk, "S-DDB", str(by_form_path), modified_since="2026-06-01T00:00:00Z")
    with duckdb.connect(str(by_form_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM "AE"').fetchone()[0] == 1

    export_to_duckdb_by_form(sdk, "S-DDB", str(by_form_path))
    with duckdb.connect(str(by_form_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM "AE"').fetchone()[0] == 0


def test_parquet_and_duckdb_streaming_edge_case_schema_evolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify mixed-object, bool/int, nested Arrow batches and untyped-null/cross-schema DuckDB cases."""
    duckdb = pytest.importorskip("duckdb")
    import pyarrow as pa
    import pyarrow.parquet as pq

    import imednet.integrations.export as export_mod
    from imednet.integrations.export import TabularDuckDBSink, TabularParquetSink

    # 1. Parquet: mixed scalar object batch ([42, "N/A"]), bool <-> int widening, and list -> string
    pq_edge = tmp_path / "edge_evolve.parquet"
    pq_cfg = TabularSinkConfig(study_key="S-EDGE", batch_size=2)
    with TabularParquetSink(str(pq_edge), pq_cfg) as sink:
        sink.write_batch(
            [
                {"recordId": 1, "MIXED": None, "FLAG": True, "NESTED": [1, 2]},
                {"recordId": 2, "MIXED": None, "FLAG": False, "NESTED": [3]},
            ],
            batch_id="b0",
        )
        sink.write_batch(
            [
                {"recordId": 3, "MIXED": 42, "FLAG": 2, "NESTED": "flat"},
                {"recordId": 4, "MIXED": "N/A", "FLAG": None, "NESTED": None},
            ],
            batch_id="b1",
        )

    pq_tbl = pq.read_table(pq_edge)
    assert pq_tbl.schema.field("MIXED").type == pa.string()
    assert pq_tbl.schema.field("FLAG").type == pa.string()
    assert pq_tbl.schema.field("NESTED").type == pa.string()
    pq_dict = pq_tbl.to_pydict()
    assert pq_dict["MIXED"] == [None, None, "42", "N/A"]
    assert pq_dict["FLAG"][2:] == ["2", None]
    assert pq_dict["NESTED"] == ["[1, 2]", "[3]", "flat", None]

    # 2. DuckDB: untyped Variable (variable_type=None) with label spaces, all-null chunk 0 -> string/float chunk 1
    sdk = MagicMock()
    vars_list = [
        Variable(
            variable_name="UNTYPED_NOTE",
            label="Untyped Note",
            variable_type=None,
            form_id=10,
            form_key="AE",
        ),
        Variable(
            variable_name="UNTYPED_MEASURE",
            label="Untyped Measure",
            variable_type=None,
            form_id=10,
            form_key="AE",
        ),
        Variable(
            variable_name="AGE",
            label="Patient Age",
            variable_type="int",
            form_id=10,
            form_key="AE",
        ),
    ]
    sdk.get_variables.return_value = vars_list
    sdk.variables.list.return_value = vars_list
    sdk.forms.list.return_value = [SimpleNamespace(form_id=10, form_key="AE")]

    chunk_records = [
        _make_record(
            1,
            record_data={"UNTYPED_NOTE": None, "UNTYPED_MEASURE": None, "AGE": None},
        ),
        _make_record(
            2,
            record_data={"UNTYPED_NOTE": "resolved", "UNTYPED_MEASURE": 12.5, "AGE": 44},
        ),
        _make_record(
            3,
            record_data={"UNTYPED_NOTE": "ongoing", "UNTYPED_MEASURE": 15.0, "AGE": None},
        ),
    ]
    sdk.records.list.side_effect = lambda **_kw: iter(chunk_records)

    ddb_untyped = tmp_path / "untyped_null.duckdb"
    export_to_duckdb(
        sdk,
        "S-EDGE",
        str(ddb_untyped),
        "records",
        use_labels_as_columns=True,
        batch_size=1,
    )
    export_to_duckdb_by_form(
        sdk,
        "S-EDGE",
        str(ddb_untyped),
        use_labels_as_columns=True,
        batch_size=1,
    )

    with duckdb.connect(str(ddb_untyped)) as conn:
        for tbl_name in ("records", "AE"):
            rows = conn.execute(
                f'SELECT "recordId", "Untyped Note", "Untyped Measure", "Patient Age" '
                f'FROM "{tbl_name}" ORDER BY "recordId"'
            ).fetchall()
            assert rows == [
                (1, None, None, None),
                (2, "resolved", 12.5, 44),
                (3, "ongoing", 15.0, None),
            ]
            col_types = {
                r[1]: r[2].upper()
                for r in conn.execute(f"PRAGMA table_info('{tbl_name}')").fetchall()
            }
            assert col_types["Patient Age"] == "BIGINT"

    # 3. Standalone TabularDuckDBSink: null -> int64 -> float64 -> string + late column + cross-schema table
    ddb_standalone = tmp_path / "standalone_edge.duckdb"
    with duckdb.connect(str(ddb_standalone)) as conn:
        conn.execute("CREATE SCHEMA other_schema")
        conn.execute("CREATE TABLE other_schema.records AS SELECT 999 AS id")

    # Zero-row flush when another schema has same table name must not raise CatalogException
    with TabularDuckDBSink(str(ddb_standalone), "records", TabularSinkConfig(study_key="S-EDGE")):
        pass
    with duckdb.connect(str(ddb_standalone)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM "records"').fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM other_schema.records").fetchone()[0] == 1

    # Multi-batch widening in standalone TabularDuckDBSink
    with TabularDuckDBSink(
        str(ddb_standalone), "evolve_tbl", TabularSinkConfig(study_key="S-EDGE")
    ) as sink:
        sink.write_batch([{"recordId": 1, "SCORE": None}], batch_id="b0")
        sink.write_batch([{"recordId": 2, "SCORE": 10}], batch_id="b1")
        sink.write_batch([{"recordId": 3, "SCORE": 12.5, "EXTRA": "late"}], batch_id="b2")
        sink.write_batch([{"recordId": 4, "SCORE": "high", "EXTRA": None}], batch_id="b3")

    with duckdb.connect(str(ddb_standalone)) as conn:
        evolved_rows = conn.execute(
            'SELECT "recordId", "SCORE", "EXTRA" FROM "evolve_tbl" ORDER BY "recordId"'
        ).fetchall()
    assert evolved_rows == [
        (1, None, None),
        (2, "10.0", None),
        (3, "12.5", "late"),
        (4, "high", None),
    ]

    # 4. export_to_duckdb_by_form mock-fetch zero-column DataFrame replaces pre-populated table
    mapper_inst = MagicMock()
    mapper_inst._build_record_model.return_value = object()
    mapper_inst._fetch_records.return_value = []
    mapper_inst._parse_records.return_value = ([], 0)
    mapper_inst._build_dataframe.return_value = pd.DataFrame()
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: MagicMock(return_value=mapper_inst))

    export_to_duckdb_by_form(sdk, "S-EDGE", str(ddb_untyped))
    with duckdb.connect(str(ddb_untyped)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM "AE"').fetchone()[0] == 0
