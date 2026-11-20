"""End-to-end simulation and regression tests for keyed file/SQL upserts and watermark recovery.

Covers:
- Keyed incremental (`incremental=True`) and `modified_since=...` upserts + hard-delete
  reconciliation across all 6 streaming exporters (`csv`, `parquet`, `excel`, `json`,
  `duckdb_by_form`, `long_sql`) while preserving non-incremental `idempotent=True`
  snapshot overwrite semantics.
- Parquet schema widening across incremental batches without PyArrow schema errors.
- Atomic single-file swap failure (`os.replace` raising `OSError`) leaving prior files intact.
- Real `apply_quality_gate` watermark holdback and recovery across runs, plus CPython GC
  `id(rec)` address-reuse immunity, custom `gate_fn` shallow copies (`model_copy()`), and
  rejected records with `date_modified=None`.
- Chunked SQL ID deletes (>500 IDs) and `camelCase`/`snake_case` unpacking in `_unpack_long_sql_rows`.
- Native `os.PathLike` (`pathlib.Path`) destination paths across file and DuckDB exporters.
"""

from __future__ import annotations

import gc
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, text

from imednet.integrations.export import (
    TabularSinkConfig,
    _unpack_long_sql_rows,
    export_to_csv,
    export_to_duckdb_by_form,
    export_to_excel,
    export_to_json,
    export_to_long_sql,
    export_to_parquet,
    export_to_sql,
)
from imednet.integrations.sink_base import run_streaming_export
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet_workflows.state_ledger import ExtractionStateLedger


def _make_record(
    record_id: int,
    *,
    date_modified: str | datetime | None,
    data: dict[str, Any],
    deleted: bool = False,
    form_id: int = 10,
    form_key: str = "DEMOG",
) -> Record:
    payload: dict[str, Any] = {
        "recordId": record_id,
        "subjectId": record_id * 10,
        "subjectKey": f"SUBJ-{record_id:04d}",
        "formId": form_id,
        "formKey": form_key,
        "deleted": deleted,
        "recordData": data,
    }
    if date_modified is not None:
        payload["dateModified"] = (
            date_modified.isoformat() if isinstance(date_modified, datetime) else date_modified
        )
    return Record.model_validate(payload)


def _make_variable(
    name: str,
    form_id: int = 10,
    form_key: str = "DEMOG",
    variable_type: str = "text",
) -> Variable:
    return Variable.model_validate(
        {
            "variableId": abs(hash((form_id, name))) % 100_000 + 1,
            "variableName": name,
            "label": name,
            "variableType": variable_type,
            "formId": form_id,
            "formKey": form_key,
        }
    )


class _SimulatedSDK:
    """Deterministic in-memory SDK stub supporting filter-aware `records.list` and `get_variables`."""

    def __init__(
        self,
        records: list[Record],
        variables: list[Variable],
        *,
        fail_records: Exception | None = None,
    ) -> None:
        self._records = list(records)
        self._variables = list(variables)
        self._fail_records = fail_records
        self.seen_filters: list[Any] = []
        default_forms = [SimpleNamespace(form_id=10, form_key="DEMOG")]
        self.records = SimpleNamespace(list=self._list_records)
        self.variables = SimpleNamespace(list=self._list_variables)
        self.intervals = SimpleNamespace(list=lambda *_a, **_kw: [])
        self.forms = SimpleNamespace(list=lambda *_a, **_kw: list(default_forms))
        self.subjects = SimpleNamespace(list=lambda *_a, **_kw: [])
        self.visits = SimpleNamespace(list=lambda *_a, **_kw: [])
        self.get_records = self._list_records
        self.get_variables = self._list_variables
        self.get_intervals = lambda *_a, **_kw: []
        self.get_forms = lambda *_a, **_kw: list(default_forms)
        self.get_subjects = lambda *_a, **_kw: []
        self.get_visits = lambda *_a, **_kw: []

    def set_records(self, records: list[Record]) -> None:
        self._records = list(records)

    def set_variables(self, variables: list[Variable]) -> None:
        self._variables = list(variables)

    def _list_variables(self, study_key: str | None = None, **_kwargs: Any) -> list[Variable]:
        return list(self._variables)

    def _list_records(
        self,
        study_key: str | None = None,
        *,
        record_data_filter: str | None = None,
        **kwargs: Any,
    ) -> list[Record]:
        dm_cond = kwargs.get("dateModified")
        self.seen_filters.append(dm_cond if dm_cond is not None else record_data_filter)
        if self._fail_records is not None:
            raise self._fail_records
        cutoff: datetime | None = None
        if isinstance(dm_cond, tuple) and len(dm_cond) == 2 and dm_cond[0] == ">":
            raw_val = dm_cond[1]
            parsed = (
                raw_val if isinstance(raw_val, datetime) else datetime.fromisoformat(str(raw_val))
            )
            cutoff = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        elif record_data_filter and "dateModified>" in record_data_filter:
            ts_raw = record_data_filter.split("dateModified>", 1)[1].strip().strip("'\"")
            parsed = datetime.fromisoformat(ts_raw)
            cutoff = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        deleted_filter = kwargs.get("deleted")
        form_id_filter = kwargs.get("formId")
        out: list[Record] = []
        for rec in self._records:
            if deleted_filter is False and rec.deleted:
                continue
            if form_id_filter is not None and rec.form_id != form_id_filter:
                continue
            if cutoff is not None and rec.date_modified is not None:
                rec_dt = (
                    rec.date_modified
                    if rec.date_modified.tzinfo
                    else rec.date_modified.replace(tzinfo=timezone.utc)
                )
                if rec_dt <= cutoff:
                    continue
            out.append(rec)
        return out


def _df_id_col(df: pd.DataFrame) -> str:
    return "recordId" if "recordId" in df.columns else "record_id"


def _read_exporter_records(exporter: str, dest: Path) -> dict[int, dict[str, Any]]:
    """Read exported destination back as `{record_id: row_dict}` and assert zero duplicate IDs."""
    if exporter == "csv":
        df = pd.read_csv(dest)
        id_col = _df_id_col(df)
        ids = [int(v) for v in df[id_col].tolist()]
        assert len(ids) == len(set(ids)), f"Duplicate record_ids in CSV: {ids}"
        return {int(row[id_col]): dict(row) for _, row in df.iterrows()}

    if exporter == "parquet":
        df = pd.read_parquet(dest)
        id_col = _df_id_col(df)
        ids = [int(v) for v in df[id_col].tolist()]
        assert len(ids) == len(set(ids)), f"Duplicate record_ids in Parquet: {ids}"
        return {int(row[id_col]): dict(row) for _, row in df.iterrows()}

    if exporter == "excel":
        df = pd.read_excel(dest)
        id_col = _df_id_col(df)
        ids = [int(v) for v in df[id_col].tolist()]
        assert len(ids) == len(set(ids)), f"Duplicate record_ids in Excel: {ids}"
        return {int(row[id_col]): dict(row) for _, row in df.iterrows()}

    if exporter == "json":
        rows = json.loads(dest.read_text(encoding="utf-8"))
        ids = [int(r.get("record_id") or r.get("recordId")) for r in rows]
        assert len(ids) == len(set(ids)), f"Duplicate record_ids in JSON: {ids}"
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            rid = int(r.get("record_id") or r.get("recordId"))
            merged = dict(r)
            if isinstance(r.get("record_data"), dict):
                merged.update(r["record_data"])
            if isinstance(r.get("recordData"), dict):
                merged.update(r["recordData"])
            out[rid] = merged
        return out

    if exporter == "duckdb_by_form":
        with duckdb.connect(str(dest)) as conn:
            df = conn.execute("SELECT * FROM DEMOG").df()
        id_col = _df_id_col(df)
        ids = [int(v) for v in df[id_col].tolist()]
        assert len(ids) == len(set(ids)), f"Duplicate record_ids in DuckDB: {ids}"
        return {int(row[id_col]): dict(row) for _, row in df.iterrows()}

    if exporter == "long_sql":
        engine = create_engine(f"sqlite:///{dest}")
        try:
            df = pd.read_sql_table("clinical_long", engine)
        finally:
            engine.dispose()
        grouped: dict[int, dict[str, Any]] = {}
        seen_pairs: set[tuple[int, str]] = set()
        for _, row in df.iterrows():
            rid = int(row["record_id"])
            var_name = str(row["variable_name"])
            assert (rid, var_name) not in seen_pairs, (
                f"Duplicate (record_id, variable_name) in long_sql: {(rid, var_name)}"
            )
            seen_pairs.add((rid, var_name))
            grouped.setdefault(rid, {})[var_name] = row["value"]
        return grouped

    raise ValueError(f"Unknown exporter: {exporter}")


def _run_exporter(
    exporter: str,
    sdk: Any,
    study_key: str,
    dest: Path,
    **kwargs: Any,
) -> None:
    if exporter == "csv":
        export_to_csv(sdk, study_key, dest, batch_size=1, **kwargs)
    elif exporter == "parquet":
        export_to_parquet(sdk, study_key, dest, batch_size=1, **kwargs)
    elif exporter == "excel":
        export_to_excel(sdk, study_key, dest, batch_size=1, **kwargs)
    elif exporter == "json":
        export_to_json(sdk, study_key, dest, batch_size=1, **kwargs)
    elif exporter == "duckdb_by_form":
        export_to_duckdb_by_form(sdk, study_key, dest, batch_size=1, **kwargs)
    elif exporter == "long_sql":
        export_to_long_sql(
            sdk,
            study_key,
            "clinical_long",
            f"sqlite:///{dest}",
            chunk_size=1,
            batch_size=1,
            **kwargs,
        )
    else:
        raise ValueError(f"Unknown exporter: {exporter}")


@pytest.mark.parametrize(
    ("exporter", "suffix"),
    [
        ("csv", ".csv"),
        ("parquet", ".parquet"),
        ("excel", ".xlsx"),
        ("json", ".json"),
        ("duckdb_by_form", ".duckdb"),
        ("long_sql", ".sqlite"),
    ],
)
@pytest.mark.parametrize("sync_mode", ["incremental", "modified_since"])
def test_all_exporters_keyed_upserts_and_deletes_preserve_prior_records(
    tmp_path: Path,
    exporter: str,
    suffix: str,
    sync_mode: str,
) -> None:
    """All 6 exporters upsert modified records, remove deleted IDs, and keep untouched history."""
    ledger = ExtractionStateLedger(str(tmp_path / "ledger.json"))
    dest = tmp_path / f"out_{exporter}_{sync_mode}{suffix}"
    variables = [_make_variable("AGE"), _make_variable("STATUS")]

    # Run 1: initial baseline with records 1, 2, 3 across multiple chunks (batch_size=1)
    run1_records = [
        _make_record(1, date_modified="2026-01-01T10:00:00Z", data={"AGE": "41", "STATUS": "OK"}),
        _make_record(2, date_modified="2026-01-01T10:05:00Z", data={"AGE": "52", "STATUS": "OK"}),
        _make_record(3, date_modified="2026-01-01T10:10:00Z", data={"AGE": "63", "STATUS": "OK"}),
    ]
    sdk = _SimulatedSDK(run1_records, variables)

    if sync_mode == "incremental":
        _run_exporter(
            exporter,
            sdk,
            "STUDY_UPSERT",
            dest,
            incremental=True,
            state_provider=ledger,
            mask_phi=False,
        )
    else:
        _run_exporter(exporter, sdk, "STUDY_UPSERT", dest, mask_phi=False)

    after_run1 = _read_exporter_records(exporter, dest)
    assert set(after_run1) == {1, 2, 3}
    assert str(after_run1[2]["AGE"]) == "52"

    # Run 2: record 1 untouched (filtered out by watermark / modified_since), record 2 modified,
    # record 3 hard-deleted, record 4 newly created.
    delta_records = [
        _make_record(1, date_modified="2026-01-01T10:00:00Z", data={"AGE": "41", "STATUS": "OK"}),
        _make_record(
            2, date_modified="2026-01-02T08:00:00Z", data={"AGE": "53", "STATUS": "UPDATED"}
        ),
        _make_record(
            3,
            date_modified="2026-01-02T08:05:00Z",
            data={"AGE": "63", "STATUS": "DEL"},
            deleted=True,
        ),
        _make_record(4, date_modified="2026-01-02T08:10:00Z", data={"AGE": "29", "STATUS": "NEW"}),
    ]
    sdk.set_records(delta_records)

    if sync_mode == "incremental":
        _run_exporter(
            exporter,
            sdk,
            "STUDY_UPSERT",
            dest,
            incremental=True,
            state_provider=ledger,
            mask_phi=False,
        )
    else:
        _run_exporter(
            exporter,
            sdk,
            "STUDY_UPSERT",
            dest,
            modified_since="2026-01-01T10:10:00Z",
            mask_phi=False,
        )

    after_run2 = _read_exporter_records(exporter, dest)
    assert set(after_run2) == {1, 2, 4}
    assert str(after_run2[1]["AGE"]) == "41"
    assert str(after_run2[2]["AGE"]) == "53"
    assert str(after_run2[2]["STATUS"]) == "UPDATED"
    assert str(after_run2[4]["STATUS"]) == "NEW"


@pytest.mark.parametrize(
    ("exporter", "suffix"),
    [
        ("csv", ".csv"),
        ("parquet", ".parquet"),
        ("excel", ".xlsx"),
        ("json", ".json"),
        ("duckdb_by_form", ".duckdb"),
        ("long_sql", ".sqlite"),
    ],
)
def test_non_incremental_snapshot_export_still_replaces_stale_destination_rows(
    tmp_path: Path,
    exporter: str,
    suffix: str,
) -> None:
    """Default `idempotent=True` non-incremental exports still overwrite prior snapshot data."""
    dest = tmp_path / f"snapshot_{exporter}{suffix}"
    variables = [_make_variable("AGE")]

    sdk = _SimulatedSDK(
        [
            _make_record(1, date_modified="2026-01-01T10:00:00Z", data={"AGE": "40"}),
            _make_record(2, date_modified="2026-01-01T10:05:00Z", data={"AGE": "50"}),
        ],
        variables,
    )
    _run_exporter(exporter, sdk, "STUDY_SNAP", dest, mask_phi=False)
    assert set(_read_exporter_records(exporter, dest)) == {1, 2}

    # Full snapshot now only returns record 99 -> records 1 & 2 must be replaced
    sdk.set_records([_make_record(99, date_modified="2026-01-02T10:00:00Z", data={"AGE": "77"})])
    _run_exporter(exporter, sdk, "STUDY_SNAP", dest, mask_phi=False)
    assert set(_read_exporter_records(exporter, dest)) == {99}


def test_parquet_incremental_schema_widening_backfills_nulls(tmp_path: Path) -> None:
    """Parquet incremental sync widens schema cleanly when new form variables appear later."""
    dest = tmp_path / "widening.parquet"
    sdk = _SimulatedSDK(
        [_make_record(1, date_modified="2026-01-01T10:00:00Z", data={"AGE": "44"})],
        [_make_variable("AGE")],
    )
    export_to_parquet(sdk, "STUDY_WIDE", dest, mask_phi=False)

    sdk.set_variables([_make_variable("AGE"), _make_variable("SITE_CODE")])
    sdk.set_records(
        [
            _make_record(1, date_modified="2026-01-01T10:00:00Z", data={"AGE": "44"}),
            _make_record(
                2,
                date_modified="2026-01-02T10:00:00Z",
                data={"AGE": "51", "SITE_CODE": "EU-01"},
            ),
        ]
    )
    export_to_parquet(
        sdk,
        "STUDY_WIDE",
        dest,
        modified_since="2026-01-01T10:00:00Z",
        mask_phi=False,
    )

    df = pd.read_parquet(dest)
    id_col = _df_id_col(df)
    df = df.sort_values(id_col).reset_index(drop=True)
    assert [int(v) for v in df[id_col].tolist()] == [1, 2]
    assert pd.isna(df.loc[0, "SITE_CODE"])
    assert df.loc[1, "SITE_CODE"] == "EU-01"


@pytest.mark.parametrize(
    ("exporter", "suffix"),
    [
        ("csv", ".csv"),
        ("parquet", ".parquet"),
        ("excel", ".xlsx"),
        ("json", ".json"),
    ],
)
def test_atomic_file_swap_failure_leaves_existing_destination_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exporter: str,
    suffix: str,
) -> None:
    """If `os.replace` raises OSError during flush, existing destination file is uncorrupted."""
    dest = tmp_path / f"atomic_{exporter}{suffix}"
    variables = [_make_variable("AGE")]
    sdk = _SimulatedSDK(
        [_make_record(1, date_modified="2026-01-01T10:00:00Z", data={"AGE": "40"})],
        variables,
    )
    _run_exporter(exporter, sdk, "STUDY_ATOMIC", dest, mask_phi=False)
    baseline_bytes = dest.read_bytes()

    sdk.set_records(
        [
            _make_record(1, date_modified="2026-01-01T10:00:00Z", data={"AGE": "40"}),
            _make_record(2, date_modified="2026-01-02T10:00:00Z", data={"AGE": "99"}),
        ]
    )

    real_replace = os.replace

    def _failing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if os.fspath(dst) == str(dest):
            raise OSError("simulated disk full during atomic swap")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _failing_replace)
    with pytest.raises(OSError, match="simulated disk full during atomic swap"):
        _run_exporter(
            exporter,
            sdk,
            "STUDY_ATOMIC",
            dest,
            modified_since="2026-01-01T10:00:00Z",
            mask_phi=False,
        )

    assert dest.read_bytes() == baseline_bytes
    leftover_tmp = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftover_tmp == []


def test_quality_gate_caps_watermark_below_rejected_draft_and_recovers_once_fixed(
    tmp_path: Path,
) -> None:
    """Watermark never advances past an earlier rejected draft even when later records pass."""
    ledger = ExtractionStateLedger(str(tmp_path / "qg_ledger.json"))
    dest = tmp_path / "qg.csv"
    # Integer variable AGE so real SchemaValidator + calculate_readiness_score drops AGE='DRAFT'
    variables = [_make_variable("AGE", variable_type="integer")]

    # Run 1: baseline record at 08:00Z passes quality gate -> watermark = 08:00Z
    r1 = _make_record(1, date_modified="2026-01-01T08:00:00Z", data={"AGE": 30})
    sdk = _SimulatedSDK([r1], variables)

    export_to_csv(
        sdk,
        "STUDY_QG",
        dest,
        incremental=True,
        state_provider=ledger,
        quality_gate_enabled=True,
        min_schema_readiness_score=100.0,
        mask_phi=False,
    )
    assert ledger.get_last_timestamp("STUDY_QG", "csv") == datetime(
        2026, 1, 1, 8, 0, tzinfo=timezone.utc
    )

    # Run 2: record 2 (08:30Z) passes, record 3 (09:00Z) is rejected DRAFT, record 4 (10:00Z) passes.
    # Watermark MUST cap at 08:30Z (< 09:00Z), NOT advance to 10:00Z!
    r2 = _make_record(2, date_modified="2026-01-01T08:30:00Z", data={"AGE": 35})
    r3_draft = _make_record(3, date_modified="2026-01-01T09:00:00Z", data={"AGE": "DRAFT"})
    r4 = _make_record(4, date_modified="2026-01-01T10:00:00Z", data={"AGE": 45})
    sdk.set_records([r1, r2, r3_draft, r4])

    export_to_csv(
        sdk,
        "STUDY_QG",
        dest,
        incremental=True,
        state_provider=ledger,
        quality_gate_enabled=True,
        min_schema_readiness_score=100.0,
        batch_size=1,
        mask_phi=False,
    )
    assert ledger.get_last_timestamp("STUDY_QG", "csv") == datetime(
        2026, 1, 1, 8, 30, tzinfo=timezone.utc
    )
    rows_after_run2 = _read_exporter_records("csv", dest)
    assert set(rows_after_run2) == {1, 2, 4}

    # Run 3: record 3 is corrected in source EDC while keeping its 09:00Z timestamp.
    # Because watermark was held at 08:30Z, record 3 is re-fetched and exported, and watermark
    # advances to 10:00Z without duplicating record 4!
    r3_fixed = _make_record(3, date_modified="2026-01-01T09:00:00Z", data={"AGE": 39})
    sdk.set_records([r1, r2, r3_fixed, r4])

    export_to_csv(
        sdk,
        "STUDY_QG",
        dest,
        incremental=True,
        state_provider=ledger,
        quality_gate_enabled=True,
        min_schema_readiness_score=100.0,
        batch_size=1,
        mask_phi=False,
    )
    assert ledger.get_last_timestamp("STUDY_QG", "csv") == datetime(
        2026, 1, 1, 10, 0, tzinfo=timezone.utc
    )
    rows_after_run3 = _read_exporter_records("csv", dest)
    assert set(rows_after_run3) == {1, 2, 3, 4}
    assert int(rows_after_run3[3]["AGE"]) == 39


def test_quality_gate_watermark_resists_gc_id_reuse_shallow_copies_and_none_timestamps(
    tmp_path: Path,
) -> None:
    """Watermark cap cannot be bypassed by CPython `id()` reuse, `model_copy()`, or None timestamps."""
    ledger = ExtractionStateLedger(str(tmp_path / "gc_ledger.json"))

    class _CountingSink:
        def __init__(self) -> None:
            self.written_ids: list[int] = []

        def write_batch(self, records: list[Record], *, batch_id: str) -> int:
            self.written_ids.extend(r.record_id for r in records if r.record_id is not None)
            return len(records)

        def reconcile_deleted(self, records: list[Record], *, batch_id: str) -> int:
            return 0

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

        def __enter__(self) -> _CountingSink:
            return self

        def __exit__(self, *_exc: object) -> None:
            self.close()

    # Case 1: Stream 250 transient Record instances from a generator where record 1 (09:00Z)
    # is dropped and records 2..250 (10:00Z) pass; force GC on each step so freed addresses
    # would be reused if rejected objects were not retained.
    def _transient_record_stream(_since: datetime | None, _inc_del: bool):
        yield _make_record(1, date_modified="2026-02-01T09:00:00Z", data={"STATUS": "DROP"})
        gc.collect()
        for rid in range(2, 252):
            yield _make_record(rid, date_modified="2026-02-01T10:00:00Z", data={"STATUS": "OK"})

    def _drop_if_marked(_sdk: Any, _study_key: str, records: Any, _cfg: Any):
        for rec in records:
            if (rec.record_data or {}).get("STATUS") == "DROP":
                gc.collect()
                continue
            # Also yield a shallow copy to verify fallback record_id candidate matching
            yield rec.model_copy()

    cfg = TabularSinkConfig(
        "STUDY_GC",
        incremental=True,
        quality_gate_enabled=True,
        state_provider=ledger,
    )
    sink1 = _CountingSink()
    run_streaming_export(
        SimpleNamespace(),
        "STUDY_GC",
        sink1,
        cfg,
        stream_name="gc_stream",
        batch_id_prefix="STUDY_GC/gc",
        records_source=_transient_record_stream,
        quality_gate_fn=_drop_if_marked,
        iter_batches_fn=lambda it, _sz: ([r] for r in it),
    )
    assert len(sink1.written_ids) == 250
    # Rejected record 1 at 09:00Z has no accepted record strictly < 09:00Z -> watermark stays None!
    assert ledger.get_last_timestamp("STUDY_GC", "gc_stream") is None

    # Case 2: Rejected record with `date_modified=None` alongside accepted timestamped record
    # must hold watermark at None because its temporal ordering is unknown.
    def _none_ts_rejected_stream(_since: datetime | None, _inc_del: bool):
        yield _make_record(10, date_modified=None, data={"STATUS": "DROP"})
        yield _make_record(11, date_modified="2026-02-01T11:00:00Z", data={"STATUS": "OK"})

    sink2 = _CountingSink()
    run_streaming_export(
        SimpleNamespace(),
        "STUDY_GC",
        sink2,
        cfg,
        stream_name="none_ts_stream",
        batch_id_prefix="STUDY_GC/none_ts",
        records_source=_none_ts_rejected_stream,
        quality_gate_fn=_drop_if_marked,
        iter_batches_fn=lambda it, _sz: ([r] for r in it),
    )
    assert sink2.written_ids == [11]
    assert ledger.get_last_timestamp("STUDY_GC", "none_ts_stream") is None


def test_chunked_sql_delete_over_500_ids_and_camelcase_long_sql_unpacking(
    tmp_path: Path,
) -> None:
    """SQLite keyed upserts/deletes handle >500 IDs in chunks and `_unpack_long_sql_rows` reads camelCase."""
    wide_db = tmp_path / "wide_550.sqlite"
    long_db = tmp_path / "long_550.sqlite"
    conn_wide = f"sqlite:///{wide_db}"
    conn_long = f"sqlite:///{long_db}"

    variables = [_make_variable("SCORE")]
    initial_records = [
        _make_record(i, date_modified="2026-03-01T00:00:00Z", data={"SCORE": str(i)})
        for i in range(1, 561)
    ]
    sdk = _SimulatedSDK(initial_records, variables)

    export_to_sql(sdk, "STUDY_550", "wide_tbl", conn_wide, batch_size=600, mask_phi=False)
    export_to_long_sql(
        sdk,
        "STUDY_550",
        "long_tbl",
        conn_long,
        chunk_size=600,
        batch_size=600,
        mask_phi=False,
    )

    # Incremental update: modify 520 records and delete 30 records (>500 IDs in single batch)
    delta_records = [
        _make_record(i, date_modified="2026-03-02T00:00:00Z", data={"SCORE": str(i + 1000)})
        for i in range(1, 521)
    ] + [
        _make_record(
            i,
            date_modified="2026-03-02T00:00:00Z",
            data={"SCORE": "0"},
            deleted=True,
        )
        for i in range(521, 551)
    ]
    sdk.set_records(delta_records + initial_records[550:])

    export_to_sql(
        sdk,
        "STUDY_550",
        "wide_tbl",
        conn_wide,
        modified_since="2026-03-01T00:00:00Z",
        batch_size=600,
        mask_phi=False,
    )
    export_to_long_sql(
        sdk,
        "STUDY_550",
        "long_tbl",
        conn_long,
        modified_since="2026-03-01T00:00:00Z",
        chunk_size=600,
        batch_size=600,
        mask_phi=False,
    )

    eng_w = create_engine(conn_wide)
    eng_l = create_engine(conn_long)
    try:
        wide_cols = {col["name"] for col in inspect(eng_w).get_columns("wide_tbl")}
        wide_id_col = "recordId" if "recordId" in wide_cols else "record_id"
        with eng_w.connect() as c:
            wide_count = c.execute(text("SELECT COUNT(*) FROM wide_tbl")).scalar_one()
            wide_distinct = c.execute(
                text(f"SELECT COUNT(DISTINCT {wide_id_col}) FROM wide_tbl")
            ).scalar_one()
            score_1 = c.execute(
                text(f"SELECT SCORE FROM wide_tbl WHERE {wide_id_col} = 1")
            ).scalar_one()
        with eng_l.connect() as c:
            long_count = c.execute(text("SELECT COUNT(*) FROM long_tbl")).scalar_one()
            long_distinct = c.execute(
                text("SELECT COUNT(DISTINCT record_id) FROM long_tbl")
            ).scalar_one()
            long_score_1 = c.execute(
                text("SELECT value FROM long_tbl WHERE record_id = 1 AND variable_name = 'SCORE'")
            ).scalar_one()
    finally:
        eng_w.dispose()
        eng_l.dispose()

    # 560 initial - 30 deleted = 530 unique records remaining (520 updated + 10 untouched 551..560)
    assert wide_count == 530
    assert wide_distinct == 530
    assert str(score_1) == "1001"
    assert long_count == 530
    assert long_distinct == 530
    assert str(long_score_1) == "1001"

    # Direct camelCase dict & object verification for _unpack_long_sql_rows
    camel_rows = _unpack_long_sql_rows(
        [
            {
                "recordId": 77,
                "formId": 12,
                "dateModified": "2026-03-05T12:00:00Z",
                "recordData": {"BP": 120},
            },
            SimpleNamespace(
                recordId=78,
                formId=13,
                dateModified="2026-03-05T12:05:00Z",
                recordData={"BP": 118},
            ),
        ],
        mask_phi=False,
        pipeline=None,
        phi_keys=None,
    )
    assert camel_rows == [
        {
            "record_id": 77,
            "form_id": 12,
            "variable_name": "BP",
            "value": 120,
            "timestamp": "2026-03-05T12:00:00Z",
        },
        {
            "record_id": 78,
            "form_id": 13,
            "variable_name": "BP",
            "value": 118,
            "timestamp": "2026-03-05T12:05:00Z",
        },
    ]


def test_initial_sync_failure_and_stale_running_lease_recovery_end_to_end(
    tmp_path: Path,
) -> None:
    """Initial export failure leaves watermark None; expired RUNNING lease recovers cleanly."""
    ledger_file = tmp_path / "recovery_ledger.json"
    ledger = ExtractionStateLedger(str(ledger_file))

    dest = tmp_path / "recovered.csv"
    variables = [_make_variable("AGE")]
    failing_sdk = _SimulatedSDK([], variables, fail_records=RuntimeError("503 upstream down"))

    with pytest.raises(RuntimeError, match="503 upstream down"):
        export_to_csv(
            failing_sdk,
            "STUDY_REC",
            dest,
            incremental=True,
            state_provider=ledger,
            mask_phi=False,
        )

    stream_key = "csv"
    assert ledger.get_last_timestamp("STUDY_REC", stream_key) is None

    # Simulate crashed container worker (PID 1 on remote host) with expired lease
    expired_payload = {
        "studies": {
            "STUDY_REC": {
                "streams": {
                    stream_key: {
                        "last_timestamp": None,
                        "records_processed": 0,
                        "last_run_status": "running",
                        "owner_pid": 1,
                        "owner_host": "crashed-pod-xyz",
                        "lease_expires_at": (
                            datetime.now(timezone.utc) - timedelta(seconds=30)
                        ).isoformat(),
                    }
                }
            }
        }
    }
    ledger_file.write_text(json.dumps(expired_payload), encoding="utf-8")

    # Retry succeeds with historical 2024 record -> must fetch all history (no dateModified filter!)
    historical_rec = _make_record(1, date_modified="2024-05-10T09:00:00Z", data={"AGE": "61"})
    healthy_sdk = _SimulatedSDK([historical_rec], variables)
    export_to_csv(
        healthy_sdk,
        "STUDY_REC",
        dest,
        incremental=True,
        state_provider=ledger,
        mask_phi=False,
    )

    assert healthy_sdk.seen_filters == [None]
    assert ledger.get_last_timestamp("STUDY_REC", stream_key) == datetime(
        2024, 5, 10, 9, 0, tzinfo=timezone.utc
    )
    rows = _read_exporter_records("csv", dest)
    assert set(rows) == {1}
    assert str(rows[1]["AGE"]) == "61"


def test_by_form_streaming_quality_gate_caps_watermark_below_rejected_record(
    tmp_path: Path,
) -> None:
    """Single-pass per-form streaming caps per-form watermarks strictly below rejected records."""
    ledger = ExtractionStateLedger(str(tmp_path / "by_form_ledger.json"))
    ddb_path = tmp_path / "by_form_gate.duckdb"
    variables = [_make_variable("AGE"), _make_variable("WT")]

    # Run 1: rec 1 (10:00) valid, rec 2 (11:00) rejected (only unmapped drift keys), rec 3 (12:00) valid
    run1_records = [
        _make_record(1, date_modified="2026-06-01T10:00:00Z", data={"AGE": "40", "WT": "70"}),
        _make_record(
            2,
            date_modified="2026-06-01T11:00:00Z",
            data={"D1": "x", "D2": "x", "D3": "x", "D4": "x"},
        ),
        _make_record(3, date_modified="2026-06-01T12:00:00Z", data={"AGE": "55", "WT": "82"}),
    ]
    sdk1 = _SimulatedSDK(run1_records, variables)
    export_to_duckdb_by_form(
        sdk1,
        "STUDY_BY_FORM_GATE",
        ddb_path,
        incremental=True,
        quality_gate_enabled=True,
        min_schema_readiness_score=75.0,
        state_provider=ledger,
        mask_phi=False,
    )

    # Watermark for duckdb_DEMOG must be held at 10:00 (strictly < 11:00 rejected rec 2), not 12:00
    assert ledger.get_last_timestamp("STUDY_BY_FORM_GATE", "duckdb_DEMOG") == datetime(
        2026, 6, 1, 10, 0, tzinfo=timezone.utc
    )
    rows1 = _read_exporter_records("duckdb_by_form", ddb_path)
    assert set(rows1) == {1, 3}

    # Run 2: rec 2 is fixed at 11:05 -> re-fetched because watermark was capped at 10:00!
    run2_records = [
        _make_record(1, date_modified="2026-06-01T10:00:00Z", data={"AGE": "40", "WT": "70"}),
        _make_record(2, date_modified="2026-06-01T11:05:00Z", data={"AGE": "48", "WT": "76"}),
        _make_record(3, date_modified="2026-06-01T12:00:00Z", data={"AGE": "55", "WT": "82"}),
    ]
    sdk2 = _SimulatedSDK(run2_records, variables)
    export_to_duckdb_by_form(
        sdk2,
        "STUDY_BY_FORM_GATE",
        ddb_path,
        incremental=True,
        quality_gate_enabled=True,
        min_schema_readiness_score=75.0,
        state_provider=ledger,
        mask_phi=False,
    )

    assert ledger.get_last_timestamp("STUDY_BY_FORM_GATE", "duckdb_DEMOG") == datetime(
        2026, 6, 1, 12, 0, tzinfo=timezone.utc
    )
    rows2 = _read_exporter_records("duckdb_by_form", ddb_path)
    assert set(rows2) == {1, 2, 3}
    assert str(rows2[2]["AGE"]) == "48"
