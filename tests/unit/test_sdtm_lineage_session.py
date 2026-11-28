"""Unit tests for in-memory SdtmLineageSession caching, rollback, and O(batch) updates."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import imednet.integrations.sink_base as sink_base_mod
from imednet import cli as cli_mod
from imednet.errors import ExportBatchError, ExportError
from imednet.integrations.enrichment import CentralizedMapper
from imednet.integrations.export import (
    TabularSinkConfig,
    _TabularTransformSink,
    export_to_csv,
    export_to_hive_parquet,
    export_to_sql_by_form,
)
from imednet.integrations.sink_base import (
    AsyncExportSink,
    ExportSink,
    SdtmLineageSession,
    SinkConfig,
    _get_or_create_sdtm_lineage_session,
    async_run_streaming_export,
    commit_sdtm_lineage,
    rehydrate_sdtm_lineage,
    resolve_voided_sdtm_keys,
    run_streaming_export,
)
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.sdtm import format_scoped_seq_override_key
from imednet.models.variables import Variable
from imednet.spi import sink_base as spi_sink_base
from imednet.spi import sinks as spi_sinks
from imednet_workflows.state_ledger import ExtractionStateLedger, FileStateProvider


def _make_ae_forms_and_vars() -> tuple[list[Form], list[Variable]]:
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
            form_id=303,
            form_key="F_AE",
            form_name="Adverse Events",
            form_type="Standard",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
        ),
    ]
    variables = [
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
            variable_name="patientName",
            label="Patient Name",
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
    ]
    return forms, variables


def _ae_record(
    record_id: int,
    *,
    subject_key: str = "SUBJ-01",
    ae_term: str | None = "Headache",
    deleted: bool = False,
    minute: int = 0,
    hour: int = 10,
    day: int = 2,
) -> Record:
    ts = f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00Z"
    data = {"AETERM": ae_term} if ae_term is not None else {}
    return Record(
        record_id=record_id,
        subject_id=1,
        subject_key=subject_key,
        visit_id=1,
        form_id=303,
        form_key="F_AE",
        record_status="Record Complete",
        deleted=deleted,
        date_created="2026-09-01T00:00:00Z",
        date_modified=ts,
        record_data=data,
    )


def _build_sdk(
    records: list[Record],
    forms: list[Form] | None = None,
    variables: list[Variable] | None = None,
) -> MagicMock:
    default_forms, default_vars = _make_ae_forms_and_vars()
    study_forms = forms if forms is not None else default_forms
    study_vars = variables if variables is not None else default_vars

    sdk = MagicMock()
    sdk.forms.list.side_effect = lambda **_kw: list(study_forms)
    sdk.get_forms.side_effect = lambda *_a, **_kw: list(study_forms)
    sdk.variables.list.side_effect = lambda **_kw: list(study_vars)
    sdk.get_variables.side_effect = lambda *_a, **_kw: list(study_vars)
    sdk.studies.list.return_value = []
    sdk.get_studies.return_value = []

    def _list_records(**kw: Any) -> Iterator[Record]:
        items = list(records)
        fk = kw.get("form_key")
        if fk:
            items = [r for r in items if r.form_key == fk]
        flt = str(kw.get("filter") or "")
        if "deleted==false" in flt.lower():
            items = [r for r in items if not r.deleted]
        return iter(items)

    sdk.records.list.side_effect = _list_records

    async def _async_list_records(**kw: Any) -> list[Record]:
        return list(_list_records(**kw))

    async def _async_forms(*_a: Any, **_kw: Any) -> list[Form]:
        return list(study_forms)

    async def _async_vars(*_a: Any, **_kw: Any) -> list[Variable]:
        return list(study_vars)

    async def _async_studies(*_a: Any, **_kw: Any) -> list[Any]:
        return []

    sdk.records.async_list = _async_list_records
    sdk.forms.async_list = _async_forms
    sdk.async_get_forms = _async_forms
    sdk.variables.async_list = _async_vars
    sdk.async_get_variables = _async_vars
    sdk.studies.async_list = _async_studies
    sdk.async_get_studies = _async_studies
    return sdk


def test_spi_reexports_and_session_apply_updates_mechanics() -> None:
    """SdtmLineageSession is re-exported on SPI modules and updates state in O(len(updates))."""
    assert spi_sinks.SdtmLineageSession is SdtmLineageSession
    assert spi_sink_base.SdtmLineageSession is SdtmLineageSession
    assert "SdtmLineageSession" in spi_sinks.__all__
    assert "SdtmLineageSession" in spi_sink_base.__all__
    assert "SdtmLineageSession" in sink_base_mod.__all__

    session = SdtmLineageSession.from_payload(
        "STUDY1",
        "csv",
        lineage={
            "10": {
                "study_id": "STUDY1",
                "domain": "AE",
                "usubjid": "STUDY1-S01",
                "seq_col": "AESEQ",
                "seq_val": 3,
            },
            "11": {
                "study_id": "STUDY1",
                "domain": "DM",
                "usubjid": "STUDY1-S01",
                "seq_col": None,
                "seq_val": None,
            },
        },
        watermarks={"AE:STUDY1-S01": 5},
    )
    assert session.domain_seq_counters == {"AE": {"STUDY1-S01": 5}}
    old_scoped_10 = format_scoped_seq_override_key("AE", "STUDY1-S01", "10")
    assert session.seq_overrides[old_scoped_10] == 3
    assert session.seq_overrides["10"] == 3

    # Reassign record 10 to a new subject and add record 12; verify old scoped key is evicted
    deltas, wm_deltas = session.apply_updates(
        {
            "10": {
                "study_id": "STUDY1",
                "domain": "AE",
                "usubjid": "STUDY1-S02",
                "seq_col": "AESEQ",
                "seq_val": 1,
            },
            "12": {
                "study_id": "STUDY1",
                "domain": "AE",
                "usubjid": "STUDY1-S01",
                "seq_col": "AESEQ",
                "seq_val": 6,
            },
        },
        watermark_updates={"AE:STUDY1-S01": 2},  # lower watermark must not regress
    )
    new_scoped_10 = format_scoped_seq_override_key("AE", "STUDY1-S02", "10")
    scoped_12 = format_scoped_seq_override_key("AE", "STUDY1-S01", "12")
    assert old_scoped_10 not in session.seq_overrides
    assert session.seq_overrides[new_scoped_10] == 1
    assert session.seq_overrides["10"] == 1
    assert session.seq_overrides[scoped_12] == 6
    assert session.watermarks["AE:STUDY1-S01"] == 6
    assert session.domain_seq_counters["AE"]["STUDY1-S01"] == 6
    assert session.domain_seq_counters["AE"]["STUDY1-S02"] == 1
    assert set(deltas) == {"10", "12"}
    assert wm_deltas["AE:STUDY1-S01"] == 6
    assert wm_deltas["AE:STUDY1-S02"] == 1

    # Verify fallback when seq_overrides is a plain dict rather than _ScopedSeqOverrides
    old_plain_key = format_scoped_seq_override_key("AE", "STUDY1-OLD", "99")
    plain_session = SdtmLineageSession(
        study_key="STUDY1",
        stream_name="csv",
        full_lineage={
            "99": {
                "study_id": "STUDY1",
                "domain": "AE",
                "usubjid": "STUDY1-OLD",
                "seq_col": "AESEQ",
                "seq_val": 2,
            }
        },
        watermarks={"AE:STUDY1-OLD": 2},
        domain_seq_counters={"AE": {"STUDY1-OLD": 2}},
        seq_overrides={old_plain_key: 2},
    )
    plain_session.apply_updates(
        {
            "99": {
                "study_id": "STUDY1",
                "domain": "AE",
                "usubjid": "STUDY1-NEW",
                "seq_col": "AESEQ",
                "seq_val": 1,
            }
        }
    )
    assert old_plain_key not in plain_session.seq_overrides
    assert (
        plain_session.seq_overrides[format_scoped_seq_override_key("AE", "STUDY1-NEW", "99")] == 1
    )

    # Verify build_sink_mapper(..., lineage_session=...) and CentralizedMapper.bind_sdtm_lineage
    cfg = SinkConfig(study_key="STUDY1", as_sdtm=True, mask_phi=True)
    mapper = spi_sinks.build_sink_mapper(cfg, lineage_session=session)
    assert isinstance(mapper, CentralizedMapper)
    assert mapper._domain_seq_counters is session.domain_seq_counters
    assert mapper._seq_overrides is session.seq_overrides

    other_session = SdtmLineageSession.from_payload("STUDY1", "other")
    mapper.bind_sdtm_lineage(
        domain_seq_counters=other_session.domain_seq_counters,
        seq_overrides=other_session.seq_overrides,
    )
    assert mapper._domain_seq_counters is other_session.domain_seq_counters
    assert mapper._seq_overrides is other_session.seq_overrides


def test_file_state_provider_20_batch_csv_executes_2_reads_and_0_get_sdtm_lineage(
    tmp_path: Path,
) -> None:
    """FileStateProvider executes 0 get_sdtm_lineage and 2 read_state calls on a 20-batch SDTM CSV export."""
    ledger_path = tmp_path / "ledger.json"
    provider = FileStateProvider(ledger_path)

    # First run: seed 5 records to populate genuine masked USUBJID lineage & watermarks
    seed_records = [_ae_record(i, ae_term=f"Seed {i}", minute=i, day=1) for i in range(1, 6)]
    seed_sdk = _build_sdk(seed_records)
    seed_csv = tmp_path / "seed_ae.csv"
    export_to_csv(
        seed_sdk,
        "STUDY_CSV",
        str(seed_csv),
        as_sdtm=True,
        incremental=True,
        batch_size=5,
        quality_gate_enabled=False,
        state_provider=provider,
    )

    # Second run: 20 delta batches -> 0 get_sdtm_lineage calls, 2 read_state calls
    records = [_ae_record(100 + i, ae_term=f"Event {i}", minute=i, day=2) for i in range(1, 21)]
    sdk = _build_sdk(records)
    csv_out = tmp_path / "ae_sdtm.csv"

    with (
        patch.object(provider, "read_state", wraps=provider.read_state) as spy_read,
        patch.object(
            provider, "get_sdtm_lineage", wraps=provider.get_sdtm_lineage
        ) as spy_get_lineage,
    ):
        export_to_csv(
            sdk,
            "STUDY_CSV",
            str(csv_out),
            as_sdtm=True,
            incremental=True,
            batch_size=1,
            quality_gate_enabled=False,
            state_provider=provider,
        )

    assert spy_get_lineage.call_count == 0
    assert spy_read.call_count == 2

    df = pd.read_csv(csv_out)
    assert len(df) == 20
    assert list(df["AESEQ"]) == list(range(6, 26))

    # Persisted JSON schema must not leak private transient markers
    raw_disk = json.loads(ledger_path.read_text(encoding="utf-8"))
    stream_json = raw_disk["studies"]["STUDY_CSV"]["streams"]["csv"]
    assert "_lineage_hydrated" not in stream_json
    assert "_sdtm_lineage_session" not in stream_json
    assert "_lineage_hydrated" not in stream_json["metadata"]
    assert "_sdtm_lineage_session" not in stream_json["metadata"]
    assert len(stream_json["metadata"]["sdtm_lineage"]) == 25
    assert list(stream_json["metadata"]["sdtm_seq_watermarks"].values()) == [25]


class _CustomProviderWithoutHydratedFlag:
    """Minimal StateProviderProtocol implementation omitting _lineage_hydrated."""

    def __init__(self) -> None:
        self.timestamps: dict[tuple[str, str], datetime] = {}
        self.lineage_store: dict[tuple[str, str], dict[str, Any]] = {}
        self.get_sdtm_lineage_calls = 0

    def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
        return self.timestamps.get((study_key, stream_name))

    def set_last_timestamp(
        self,
        study_key: str,
        stream_name: str,
        timestamp: datetime,
        *,
        status: Any = "success",
        records_processed: int = 0,
        error_summary: str | None = None,
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> None:
        _ = (status, records_processed, error_summary, force)
        self.timestamps[(study_key, stream_name)] = timestamp
        if metadata:
            self.lineage_store[(study_key, stream_name)] = dict(metadata)

    def get_sdtm_lineage(self, study_key: str, stream_name: str) -> dict[str, Any]:
        self.get_sdtm_lineage_calls += 1
        return dict(self.lineage_store.get((study_key, stream_name), {}))

    @contextmanager
    def transaction(
        self,
        study_key: str,
        stream_name: str,
        fallback_timestamp: datetime | None = None,
    ) -> Iterator[dict[str, Any]]:
        tx: dict[str, Any] = {
            "last_timestamp": self.timestamps.get((study_key, stream_name)) or fallback_timestamp,
            "new_timestamp": None,
            "records_processed": 0,
            "metadata": {},
        }
        yield tx
        if tx["new_timestamp"] is not None:
            self.set_last_timestamp(
                study_key,
                stream_name,
                tx["new_timestamp"],
                records_processed=tx["records_processed"],
                metadata=tx["metadata"],
            )


class _RecordingSdtmSink(ExportSink):
    """Test ExportSink exercising _get_or_create_sdtm_lineage_session, commit_sdtm_lineage, and resolve_voided_sdtm_keys."""

    def __init__(self, config: SinkConfig, *, fail_on_batch: int | None = None) -> None:
        super().__init__(config)
        self._fail_on_batch = fail_on_batch
        self.written_rows: list[dict[str, Any]] = []
        self.deleted_keys: list[dict[str, Any]] = []
        self._batch_idx = 0
        self._mapper: CentralizedMapper | None = None
        self._lineage_out: dict[str, dict[str, Any]] = {}
        self._domain_seq_counters: dict[str, dict[str, int]] = {}

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        _ = batch_id
        self._batch_idx += 1
        if self._fail_on_batch is not None and self._batch_idx == self._fail_on_batch:
            raise RuntimeError("simulated destination outage")
        session, *_ = _get_or_create_sdtm_lineage_session(self, study_key=self.config.study_key)
        self._lineage_out.clear()
        if self._mapper is None:
            self._mapper = spi_sinks.build_sink_mapper(
                self.config,
                lineage_session=session,
                lineage_out=self._lineage_out,
                form_domain_map=getattr(self, "_active_form_domain_map", None),
            )
        else:
            self._mapper.bind_sdtm_lineage(
                domain_seq_counters=session.domain_seq_counters,
                seq_overrides=session.seq_overrides,
            )
        pairs = self._mapper.map_batch_with_sources(
            records,
            study_key=self.config.study_key or "",
        )
        rows = [doc for _, doc in pairs]
        commit_sdtm_lineage(self, self._lineage_out, study_key=self.config.study_key)
        self.written_rows.extend(rows)
        return len(rows)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        _ = batch_id
        keys = resolve_voided_sdtm_keys(records, self, study_key=self.config.study_key)
        self.deleted_keys.extend(keys)
        return len(keys)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _AsyncRecordingSdtmSink(AsyncExportSink):
    """AsyncExportSink counterpart verifying async_run_streaming_export transaction lifecycle."""

    def __init__(self, config: SinkConfig, *, fail_on_batch: int | None = None) -> None:
        super().__init__(config)
        self._fail_on_batch = fail_on_batch
        self.written_rows: list[dict[str, Any]] = []
        self._batch_idx = 0

    async def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        _ = batch_id
        self._batch_idx += 1
        if self._fail_on_batch is not None and self._batch_idx == self._fail_on_batch:
            raise RuntimeError("simulated async outage")
        counters, overrides, _ = rehydrate_sdtm_lineage(self, study_key=self.config.study_key)
        batch_lineage: dict[str, dict[str, Any]] = {}
        mapper = spi_sinks.build_sink_mapper(
            self.config,
            domain_seq_counters=counters,
            seq_overrides=overrides,
            lineage_out=batch_lineage,
            form_domain_map=getattr(self, "_active_form_domain_map", None),
        )
        pairs = mapper.map_batch_with_sources(records, study_key=self.config.study_key or "")
        rows = [doc for _, doc in pairs]
        commit_sdtm_lineage(self, batch_lineage, study_key=self.config.study_key)
        self.written_rows.extend(rows)
        return len(rows)

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        pass


def test_custom_provider_without_lineage_hydrated_reads_once_per_stream() -> None:
    """Custom providers without _lineage_hydrated execute at most 1 _read_provider_lineage_payload call."""
    provider = _CustomProviderWithoutHydratedFlag()
    seed_cfg = SinkConfig(
        study_key="S_CUST",
        batch_size=2,
        max_retries=0,
        incremental=True,
        as_sdtm=True,
        mask_phi=False,
        state_provider=provider,
        stream_name="custom_sink",
    )
    seed_sdk = _build_sdk([_ae_record(1, minute=1, day=1), _ae_record(2, minute=2, day=1)])
    run_streaming_export(
        seed_sdk, "S_CUST", _RecordingSdtmSink(seed_cfg), seed_cfg, stream_name="custom_sink"
    )
    provider.get_sdtm_lineage_calls = 0

    records = [_ae_record(10 + i, minute=i, day=2) for i in range(1, 11)]
    sdk = _build_sdk(records)
    cfg = SinkConfig(
        study_key="S_CUST",
        batch_size=1,
        max_retries=0,
        incremental=True,
        as_sdtm=True,
        mask_phi=False,
        state_provider=provider,
        stream_name="custom_sink",
    )
    sink = _RecordingSdtmSink(cfg)

    with patch.object(
        sink_base_mod,
        "_read_provider_lineage_payload",
        wraps=sink_base_mod._read_provider_lineage_payload,
    ) as spy_fallback:
        written = run_streaming_export(sdk, "S_CUST", sink, cfg, stream_name="custom_sink")

    assert written == 10
    assert spy_fallback.call_count == 1
    assert provider.get_sdtm_lineage_calls == 1
    assert [r["AESEQ"] for r in sink.written_rows] == list(range(3, 13))
    assert sink._active_tx is None
    assert sink._sdtm_lineage == {}
    assert sink._domain_seq_counters == {}

    # Also verify async_run_streaming_export with custom provider executes 1 fallback read
    async_provider = _CustomProviderWithoutHydratedFlag()
    async_cfg = SinkConfig(
        study_key="S_ASYNC",
        batch_size=1,
        max_retries=0,
        incremental=True,
        as_sdtm=True,
        mask_phi=False,
        state_provider=async_provider,
        stream_name="async_sink",
    )
    async_sink = _AsyncRecordingSdtmSink(async_cfg)
    with patch.object(
        sink_base_mod,
        "_read_provider_lineage_payload",
        wraps=sink_base_mod._read_provider_lineage_payload,
    ) as spy_async_fallback:
        async_written = asyncio.run(
            async_run_streaming_export(
                sdk, "S_ASYNC", async_sink, async_cfg, stream_name="async_sink"
            )
        )
    assert async_written == 10
    assert spy_async_fallback.call_count == 1
    assert async_sink._active_tx is None


def test_resolve_voided_sdtm_keys_o_batch_and_monotonic_void_preservation(
    tmp_path: Path,
) -> None:
    """resolve_voided_sdtm_keys resolves keys in O(len(records)) and voided --SEQ is never reused."""
    provider = FileStateProvider(tmp_path / "void_ledger.json")
    initial_records = [
        _ae_record(501, ae_term="Alpha", minute=1, day=1),
        _ae_record(502, ae_term="Beta", minute=2, day=1),
    ]
    sdk1 = _build_sdk(initial_records)
    cfg = SinkConfig(
        study_key="S_VOID",
        batch_size=1,
        max_retries=0,
        incremental=True,
        as_sdtm=True,
        mask_phi=False,
        state_provider=provider,
        stream_name="void_sink",
    )
    sink1 = _RecordingSdtmSink(cfg)
    assert run_streaming_export(sdk1, "S_VOID", sink1, cfg, stream_name="void_sink") == 2
    assert [r["AESEQ"] for r in sink1.written_rows] == [1, 2]

    # Run 2: Record 502 is voided, Record 503 is added -> 502 resolves AESEQ=2, 503 receives AESEQ=3 (never 2!)
    delta_records = [
        _ae_record(502, ae_term="Beta", deleted=True, minute=5, day=2),
        _ae_record(503, ae_term="Gamma", deleted=False, minute=6, day=2),
    ]
    sdk2 = _build_sdk(delta_records)
    sink2 = _RecordingSdtmSink(cfg)
    with patch.object(provider, "get_sdtm_lineage", wraps=provider.get_sdtm_lineage) as spy_lineage:
        assert run_streaming_export(sdk2, "S_VOID", sink2, cfg, stream_name="void_sink") == 1
    assert spy_lineage.call_count == 0
    assert sink2.deleted_keys == [
        {
            "study_id": "S_VOID",
            "domain": "AE",
            "usubjid": "SUBJ-01",
            "seq_col": "AESEQ",
            "seq_val": 2,
        }
    ]
    assert [r["AESEQ"] for r in sink2.written_rows] == [3]

    # Empty records fast path
    assert resolve_voided_sdtm_keys([], sink2, study_key="S_VOID") == []


def test_mid_stream_failures_rollback_cleanly_and_assign_identical_seq_on_retry(
    tmp_path: Path,
) -> None:
    """ExportBatchError and min_standards_score failures roll back state and assign identical --SEQ on retry."""
    ledger_path = tmp_path / "rollback_ledger.json"
    provider = FileStateProvider(ledger_path)

    # Seed baseline state
    seed_records = [_ae_record(1, ae_term="Baseline AE", minute=0, day=1)]
    seed_sdk = _build_sdk(seed_records)
    base_cfg = SinkConfig(
        study_key="S_RB",
        batch_size=1,
        max_retries=0,
        incremental=True,
        as_sdtm=True,
        mask_phi=False,
        state_provider=provider,
        stream_name="rb_stream",
    )
    seed_sink = _RecordingSdtmSink(base_cfg)
    run_streaming_export(seed_sdk, "S_RB", seed_sink, base_cfg, stream_name="rb_stream")
    prior_ts = provider.get_last_timestamp("S_RB", "rb_stream")
    prior_lineage = provider.get_sdtm_lineage("S_RB", "rb_stream")
    assert prior_lineage["sdtm_seq_watermarks"] == {"AE:SUBJ-01": 1}

    # 1. Mid-stream ExportBatchError on batch 2 of 3 (via execute_batch retry wrapper)
    delta_records = [
        _ae_record(2, ae_term="Delta 1", minute=1, day=2),
        _ae_record(
            3, ae_term=None, minute=2, day=2
        ),  # missing AETERM triggers standards error when strict
        _ae_record(4, ae_term="Delta 3", minute=3, day=2),
    ]
    delta_sdk = _build_sdk(delta_records)

    class _RetryWrappedFailingSink(_RecordingSdtmSink):
        def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
            return self._execute_with_retry(
                "test_export",
                batch_id,
                lambda: super(_RetryWrappedFailingSink, self).write_batch(
                    records, batch_id=batch_id
                ),
            )

    failing_sink = _RetryWrappedFailingSink(base_cfg, fail_on_batch=2)
    with pytest.raises(ExportBatchError):
        run_streaming_export(delta_sdk, "S_RB", failing_sink, base_cfg, stream_name="rb_stream")

    assert failing_sink._active_tx is None
    assert getattr(failing_sink, "_active_state_provider", None) is None
    assert failing_sink._sdtm_lineage == {}
    assert failing_sink._domain_seq_counters == {}
    assert provider.get_last_timestamp("S_RB", "rb_stream") == prior_ts
    assert provider.get_sdtm_lineage("S_RB", "rb_stream") == prior_lineage

    # 2. Async mid-stream failure also clears _active_tx and leaves state untouched
    failing_async_sink = _AsyncRecordingSdtmSink(base_cfg, fail_on_batch=2)
    with pytest.raises(RuntimeError, match="simulated async outage"):
        asyncio.run(
            async_run_streaming_export(
                delta_sdk, "S_RB", failing_async_sink, base_cfg, stream_name="rb_stream"
            )
        )
    assert failing_async_sink._active_tx is None
    assert provider.get_last_timestamp("S_RB", "rb_stream") == prior_ts
    assert provider.get_sdtm_lineage("S_RB", "rb_stream") == prior_lineage

    # 3. min_standards_score failure (on batch 2 missing AETERM) rolls back cleanly as well
    strict_cfg = SinkConfig(
        study_key="S_RB",
        batch_size=1,
        max_retries=0,
        incremental=True,
        as_sdtm=True,
        mask_phi=False,
        standards_profile="drug",
        min_standards_score=100.0,
        state_provider=provider,
        stream_name="rb_stream",
    )
    strict_sink = _RecordingSdtmSink(strict_cfg)
    with pytest.raises(ExportError, match="Standards readiness check failed"):
        run_streaming_export(delta_sdk, "S_RB", strict_sink, strict_cfg, stream_name="rb_stream")

    assert strict_sink._active_tx is None
    assert provider.get_last_timestamp("S_RB", "rb_stream") == prior_ts
    assert provider.get_sdtm_lineage("S_RB", "rb_stream") == prior_lineage

    # 4. Retry with healthy sink assigns identical --SEQ values (2, 3, 4)
    retry_sink = _RecordingSdtmSink(base_cfg)
    assert (
        run_streaming_export(delta_sdk, "S_RB", retry_sink, base_cfg, stream_name="rb_stream") == 3
    )
    assert [r["AESEQ"] for r in retry_sink.written_rows] == [2, 3, 4]


def test_tabular_context_propagation_and_multi_form_hive_and_sql_rollback(
    tmp_path: Path,
) -> None:
    """_TabularTransformSink propagates _sdtm_lineage_session and multi-form exports roll back cleanly."""
    inner = MagicMock()
    tab_cfg = TabularSinkConfig(study_key="S_TAB", as_sdtm=True)
    tab_sink = _TabularTransformSink(
        inner,
        MagicMock(),
        None,
        [],
        {},
        tab_cfg,
        sanitize=False,
    )
    sentinel_session = SdtmLineageSession.from_payload("S_TAB", "csv")
    tab_sink._sdtm_lineage_session = {("S_TAB", "csv", 0): sentinel_session}
    tab_sink._active_tx = {"metadata": {}, "_lineage_hydrated": True}
    tab_sink._sync_active_context_to_inner()
    assert inner._sdtm_lineage_session == {("S_TAB", "csv", 0): sentinel_session}
    assert inner._active_tx is tab_sink._active_tx

    # Multi-form hive parquet incremental export: 0 get_sdtm_lineage calls, 2 read_state calls
    hive_ledger = FileStateProvider(tmp_path / "hive_ledger.json")
    records = [
        _ae_record(200 + i, ae_term=(None if i == 4 else f"Hive AE {i}"), minute=i, day=3)
        for i in range(1, 7)
    ]
    sdk = _build_sdk(records)
    hive_dir = tmp_path / "hive_out"

    with (
        patch.object(hive_ledger, "read_state", wraps=hive_ledger.read_state) as spy_hive_read,
        patch.object(
            hive_ledger, "get_sdtm_lineage", wraps=hive_ledger.get_sdtm_lineage
        ) as spy_hive_lineage,
    ):
        export_to_hive_parquet(
            sdk,
            "S_HIVE",
            str(hive_dir),
            as_sdtm=True,
            incremental=True,
            batch_size=2,
            quality_gate_enabled=False,
            state_provider=hive_ledger,
        )

    assert spy_hive_lineage.call_count == 0
    assert spy_hive_read.call_count == 2
    ae_parquet_files = sorted(hive_dir.rglob("*.parquet"))
    assert ae_parquet_files
    hive_df = pd.concat([pd.read_parquet(p) for p in ae_parquet_files], ignore_index=True)
    assert sorted(hive_df["AESEQ"].dropna().astype(int).tolist()) == [1, 2, 3, 4, 5, 6]

    # Multi-form sql_by_form min_standards_score failure leaves state unchanged & retry assigns same SEQ
    sql_ledger = FileStateProvider(tmp_path / "sql_ledger.json")
    sql_db = tmp_path / "by_form.sqlite"
    with pytest.raises(ExportError, match="Standards readiness check failed"):
        export_to_sql_by_form(
            sdk,
            "S_SQL_RB",
            f"sqlite:///{sql_db}",
            as_sdtm=True,
            incremental=True,
            batch_size=2,
            quality_gate_enabled=False,
            standards_profile="drug",
            min_standards_score=100.0,
            state_provider=sql_ledger,
        )
    assert sql_ledger.get_last_timestamp("S_SQL_RB", "sql_F_AE") is None
    assert sql_ledger.get_sdtm_lineage("S_SQL_RB", "sql_F_AE") == {
        "sdtm_lineage": {},
        "sdtm_seq_watermarks": {},
    }

    export_to_sql_by_form(
        sdk,
        "S_SQL_RB",
        f"sqlite:///{sql_db}",
        as_sdtm=True,
        incremental=True,
        batch_size=2,
        quality_gate_enabled=False,
        state_provider=sql_ledger,
    )
    with sqlite3.connect(sql_db) as conn:
        sql_df = pd.read_sql_query('SELECT AESEQ FROM "F_AE" ORDER BY AESEQ', conn)
    assert list(sql_df["AESEQ"]) == [1, 2, 3, 4, 5, 6]


def test_cli_export_sdtm_incremental_with_ledger_path(tmp_path: Path) -> None:
    """CLI `imednet export csv --as-sdtm --incremental` uses session caching without get_sdtm_lineage."""
    ledger_file = tmp_path / "cli_ledger.json"
    csv_file = tmp_path / "cli_ae.csv"
    records = [_ae_record(900 + i, ae_term=f"CLI AE {i}", minute=i, day=4) for i in range(1, 5)]
    sdk = _build_sdk(records)

    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=sdk),
        patch.object(
            ExtractionStateLedger,
            "get_sdtm_lineage",
            wraps=ExtractionStateLedger.get_sdtm_lineage,
        ) as spy_cli_lineage,
    ):
        cli_mod.app(
            [
                "export",
                "csv",
                "STUDY_CLI",
                str(csv_file),
                "--as-sdtm",
                "--incremental",
                "--ledger-path",
                str(ledger_file),
                "--no-quality-gate",
            ]
        )

    assert spy_cli_lineage.call_count == 0
    df = pd.read_csv(csv_file)
    assert list(df["AESEQ"]) == [1, 2, 3, 4]
