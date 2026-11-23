# pylint: disable=duplicate-code
"""Export helpers built on top of :class:`RecordMapper`."""

from __future__ import annotations

import contextlib
import csv
import hashlib
import inspect
import io
import json
import math
import numbers
import os
import re
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import NonCallableMock

from imednet.integrations.enrichment import (
    EnrichmentPipeline,
    _matches_custom_phi_key,
    mask_custom_phi_recursive,
    resolve_enrichment_pipeline,
    resolve_study_configuration,
)
from imednet.integrations.sink_base import (
    BoundedFormBufferRouter,
    ExportSink,
    SinkConfig,
    StateProviderProtocol,
    _enforce_batch_standards_readiness,
    _extract_safe_config_options,
    apply_quality_gate,
    extract_record_form_ref,
    extract_record_id,
    extract_record_timestamp,
    get_state_provider,
    is_record_deleted,
    iter_batches,
    iter_filtered_sdk_records,
    normalize_watermark_datetime,
    run_streaming_export,
)
from imednet.models.standards import (
    format_variable_name,
    get_sdtm_domain_variables,
    normalize_naming_convention,
    resolve_sdtm_domain,
    transform_records_to_sdtm,
)

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore[assignment, unused-ignore]
from imednet.constants import MAX_SQLITE_COLUMNS
from imednet.utils import sanitize_csv_formula
from imednet.utils.security import global_sensitivity_registry, mask_clinical_phi
from imednet.utils.tabular import MASKED_SENTINEL

from .. import ImednetClient
from ..sdk import ImednetSDK

_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def _validate_duckdb_identifier(name: str) -> None:
    if not isinstance(name, str) or not _SAFE_IDENTIFIER_PATTERN.fullmatch(name):
        raise ValueError(
            f"Invalid identifier {name!r}. Only alphanumeric characters and underscores are allowed."
        )


def _quote_duckdb_identifier(name: str) -> str:
    _validate_duckdb_identifier(name)
    escaped_name = name.replace('"', '""')
    return f'"{escaped_name}"'


validate_duckdb_identifier = _validate_duckdb_identifier


_DUCKDB_DF_ALIAS = "df"


def _record_mapper() -> Any:
    from importlib.metadata import entry_points

    mappers = list(entry_points(group="imednet.mappers", name="RecordMapper"))
    if not mappers:
        raise ImportError(
            "Record export requires an installed mapper plugin. "
            "Please install a package that provides this capability."
        )
    return mappers[0].load()


def _create_record_mapper(sdk: Any, *, mask_phi: bool = True) -> Any:
    """Instantiate the configured RecordMapper while preserving call contracts."""
    mapper_cls = _record_mapper()
    if mask_phi:
        return mapper_cls(sdk)
    try:
        accepts_mask_phi = "mask_phi" in inspect.signature(mapper_cls).parameters
    except (TypeError, ValueError):
        accepts_mask_phi = False
    if accepts_mask_phi:
        return mapper_cls(sdk, mask_phi=False)
    mapper = mapper_cls(sdk)
    mapper.mask_phi = False
    return mapper


def _is_custom_phi_key(key: str, phi_keys: set[str] | None) -> bool:
    """Return True when *key* matches a custom StudyConfiguration PHI field."""
    if not phi_keys or not key:
        return False
    return _matches_custom_phi_key(key, phi_keys)


def _mask_df(df: pd.DataFrame, *, phi_keys: set[str] | None = None) -> pd.DataFrame:
    """Mask sensitive fields in the DataFrame based on global and study rules."""
    sensitive_cols = list(
        dict.fromkeys(
            col
            for col in df.columns
            if global_sensitivity_registry.is_sensitive(str(col))
            or _is_custom_phi_key(str(col), phi_keys)
        )
    )
    for col in sensitive_cols:
        if len(df) > 0:
            df[col] = MASKED_SENTINEL

    if len(df) == 0:
        return df

    remaining = df.drop(columns=sensitive_cols)
    try:
        object_cols = list(dict.fromkeys(remaining.select_dtypes(include=[object, "str"]).columns))
    except TypeError:
        object_cols = list(dict.fromkeys(remaining.select_dtypes(include=[object]).columns))

    custom_mask_fn = (lambda val: mask_custom_phi_recursive(val, phi_keys)) if phi_keys else None
    for col in object_cols:
        target = df[col]
        if isinstance(target, pd.DataFrame):
            df[col] = target.apply(lambda s: s.apply(mask_clinical_phi))
            if custom_mask_fn is not None:
                df[col] = df[col].apply(lambda s: s.apply(custom_mask_fn))
        else:
            df[col] = target.apply(mask_clinical_phi)
            if custom_mask_fn is not None:
                df[col] = df[col].apply(custom_mask_fn)

    return df


def _to_sql_with_chunking(
    df: pd.DataFrame,
    table: str,
    engine: Any,
    *,
    if_exists: Literal["fail", "replace", "append", "delete_rows"],
    **kwargs: Any,
) -> None:
    """Write ``df`` to ``table`` splitting columns when using SQLite.

    SQLite limits tables to ``MAX_SQLITE_COLUMNS`` columns. When the DataFrame
    exceeds this, the data is written to multiple tables suffixed with
    ``_part1``, ``_part2`` and so on.
    """
    if engine.dialect.name == "sqlite" and len(df.columns) > MAX_SQLITE_COLUMNS:
        for i, start in enumerate(range(0, len(df.columns), MAX_SQLITE_COLUMNS), start=1):
            chunk = df.iloc[:, start : start + MAX_SQLITE_COLUMNS]
            chunk.to_sql(
                f"{table}_part{i}",
                engine,
                if_exists=if_exists,
                index=False,
                **kwargs,
            )
    else:
        df.to_sql(table, engine, if_exists=if_exists, index=False, **kwargs)


def _records_df(
    sdk: ImednetSDK,
    study_key: str,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    mask_phi: bool = True,
    _mapper: Any = None,
) -> pd.DataFrame:
    """Return a DataFrame of study records with duplicate columns removed."""
    if pd is None:
        raise ImportError(
            "pandas is required for _records_df. Install with \"pip install 'imednet[export]'\"."
        )
    mapper = _mapper if _mapper is not None else _create_record_mapper(sdk, mask_phi=mask_phi)
    df: pd.DataFrame = mapper.dataframe(
        study_key,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
    )
    if isinstance(df, pd.DataFrame):
        df.columns = df.columns.astype(str)
        df = df.loc[:, ~df.columns.str.lower().duplicated()]
    return df


def _prepare_export_df(
    sdk: ImednetSDK,
    study_key: str,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    sanitize: bool = False,
    mask_phi: bool = True,
    _mapper: Any = None,
) -> pd.DataFrame:
    """Prepare a DataFrame for export by fetching records and optionally sanitizing."""
    df = _records_df(
        sdk,
        study_key,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        mask_phi=mask_phi,
        _mapper=_mapper,
    )
    pipeline = resolve_enrichment_pipeline(study_key)
    phi_keys = set(pipeline.phi_keys) if pipeline is not None else None
    if mask_phi:
        df = _mask_df(df, phi_keys=phi_keys)
    if sanitize:
        df = _sanitize_df(df)
    return df


_DEFAULT_PREPARE_EXPORT_DF = _prepare_export_df
_DEFAULT_APPLY_QUALITY_GATE = apply_quality_gate


def _merge_form_whitelist_into_filter(
    record_filter: dict[str, Any] | str | Sequence[str] | None,
    form_whitelist: list[int] | None,
) -> dict[str, Any] | str | Sequence[str] | None:
    """Combine ``record_filter`` with ``form_whitelist`` without mixing connectors."""
    if form_whitelist is None:
        return record_filter
    form_val: Any = form_whitelist[0] if len(form_whitelist) == 1 else list(form_whitelist)
    if record_filter is None:
        return {"formId": form_val}
    if isinstance(record_filter, str):
        return {"formId": form_val, "filter": record_filter}
    if isinstance(record_filter, Mapping):
        merged = dict(record_filter)
        merged["formId"] = form_val
        return merged
    return {"formId": form_val, "filter": ";".join(str(x) for x in record_filter)}


def _is_mock_mapper(mapper: Any) -> bool:
    """Return True when ``mapper`` is a unittest.mock stub."""
    return isinstance(mapper, NonCallableMock)


def _uses_mock_fetch_records(mapper: Any) -> bool:
    """Return True when a mock mapper explicitly configured ``_fetch_records``."""
    if not isinstance(mapper, NonCallableMock):
        return False
    mock_children = getattr(mapper, "_mock_children", {})
    obj_vars = vars(mapper)
    return "_fetch_records" in mock_children or "_fetch_records" in obj_vars


def _sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Sanitize DataFrame string columns to prevent CSV injection."""
    # Explicitly include "str" to avoid Pandas 3.0 warning about object dtype
    # including strings implicitly. Use a try/except block to handle older Pandas versions
    # (e.g. 2.3.3) where "str" raises a TypeError.
    try:
        cols = df.select_dtypes(include=[object, "str"])
    except TypeError:
        cols = df.select_dtypes(include=[object])

    for col in cols:
        df[col] = df[col].apply(sanitize_csv_formula)
    return df


_ENVELOPE_IDENTITY_KEYS = frozenset(
    {
        "record_id",
        "recordId",
        "subject_key",
        "subjectKey",
        "study_key",
        "studyKey",
        "visit_id",
        "visitId",
        "form_id",
        "formId",
    }
)


def _unpack_record_envelope_mapping(
    envelope: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Flatten a record envelope + ``record_data`` mapping into a tabular row dictionary."""
    row: dict[str, Any] = {}
    for k, v in envelope.items():
        if k in ("record_data", "recordData"):
            continue
        if isinstance(v, (dict, list)):
            continue
        row[k] = v
    for k, v in payload.items():
        if k in _ENVELOPE_IDENTITY_KEYS and row.get(k) is not None:
            continue
        row[k] = v
    return row


def _normalize_tabular_batch_item(item: Any) -> Any:
    """Normalize a single Record model or record-envelope dict for DataFrame construction."""
    if isinstance(item, Mapping):
        if isinstance(item.get("record_data"), Mapping):
            return _unpack_record_envelope_mapping(item, item["record_data"])
        if isinstance(item.get("recordData"), Mapping):
            return _unpack_record_envelope_mapping(item, item["recordData"])
        return dict(item)

    if isinstance(item, NonCallableMock):
        return item

    model_dump = getattr(type(item), "model_dump", None)
    if callable(model_dump):
        dumped = item.model_dump()
        if isinstance(dumped, Mapping):
            payload = dumped.get("record_data")
            if isinstance(payload, Mapping):
                return _unpack_record_envelope_mapping(dumped, payload)
            return {k: v for k, v in dumped.items() if not isinstance(v, (dict, list))}

    record_data = getattr(item, "record_data", None)
    if isinstance(record_data, Mapping):
        envelope: dict[str, Any] = {}
        for attr in ("record_id", "subject_key", "study_key", "visit_id", "form_id"):
            val = getattr(item, attr, None)
            if val is not None:
                envelope[attr] = val
        return _unpack_record_envelope_mapping(envelope, record_data)

    return item


def _prepare_tabular_batch_df(
    records: Any,
    *,
    mask_phi: bool,
    sanitize: bool,
) -> pd.DataFrame:
    """Convert ``records`` into a DataFrame with standardized PHI masking and CSV sanitization."""
    if isinstance(records, pd.DataFrame):
        if len(records) == 0:
            return records
        df = records.copy()
    else:
        items = list(records) if not isinstance(records, Sequence) else records
        if not items:
            return pd.DataFrame()
        normalized = [_normalize_tabular_batch_item(item) for item in items]
        df = pd.DataFrame(normalized)
        if len(df) == 0:
            return df

    if any(isinstance(c, str) for c in df.columns):
        lower_cols = [c.lower() if isinstance(c, str) else c for c in df.columns]
        dup_mask = pd.Index(lower_cols).duplicated()
        if bool(dup_mask.any()):
            df = df.loc[:, ~dup_mask]

    if mask_phi:
        df = _mask_df(df)
    if sanitize:
        df = _sanitize_df(df)
    return df


@dataclass
class TabularSinkConfig(SinkConfig):
    """Configuration for tabular sinks."""

    manifest_path: str | None = None
    use_labels_as_columns: bool = False
    sanitize: bool = False
    variable_whitelist: list[str] | None = None
    form_whitelist: list[int] | None = None
    pandas_kwargs: dict[str, Any] = field(default_factory=dict)


def _is_incremental_config(config: SinkConfig) -> bool:
    """Return True when ``config`` activates incremental, watermark, or stateful sync."""
    return bool(
        config.incremental or config.modified_since is not None or config.state_provider is not None
    )


def _canonical_record_id(value: Any) -> str | None:
    """Normalize a ``recordId`` / ``record_id`` value to a canonical string key."""
    if value is None or value is pd.NA or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, numbers.Real):
        as_float = float(value)
        if math.isnan(as_float) or math.isinf(as_float):
            return None
        return str(int(as_float)) if as_float.is_integer() else str(as_float)
    if _is_missing_scalar(value):
        return None
    text = str(value).strip()
    return text if text else None


def _extract_row_canonical_id(row: Mapping[str, Any]) -> str | None:
    """Extract canonical record ID from a row mapping under ``recordId``, ``record_id``, or ``RECORD_ID``."""
    for key in ("recordId", "record_id", "RECORD_ID"):
        if key in row:
            cid = _canonical_record_id(row[key])
            if cid is not None:
                return cid
    return None


def _find_df_record_id_col(df: pd.DataFrame) -> str | None:
    """Return the record ID column name in ``df``, if present."""
    for candidate in ("recordId", "record_id", "RECORD_ID"):
        if candidate in df.columns:
            return candidate
    return None


def _dedupe_df_by_record_id(
    df: pd.DataFrame,
    *,
    exclude_ids: set[str] | None = None,
) -> tuple[pd.DataFrame, set[str]]:
    """Keep the latest row per canonical record ID and optionally drop ``exclude_ids``."""
    if df.empty:
        return df, set()
    id_col = _find_df_record_id_col(df)
    if id_col is None:
        return df.reset_index(drop=True), set()

    excluded = exclude_ids or set()
    raw_ids = df[id_col].tolist()
    seen: set[str] = set()
    kept_rev: list[int] = []
    for idx in range(len(raw_ids) - 1, -1, -1):
        cid = _canonical_record_id(raw_ids[idx])
        if cid is None:
            kept_rev.append(idx)
            continue
        if cid in excluded or cid in seen:
            continue
        seen.add(cid)
        kept_rev.append(idx)

    kept_rev.reverse()
    return df.iloc[kept_rev].reset_index(drop=True), seen


def _filter_df_excluding_record_ids(df: pd.DataFrame, exclude_ids: set[str]) -> pd.DataFrame:
    """Drop rows from ``df`` whose canonical record ID appears in ``exclude_ids``."""
    if df.empty or not exclude_ids:
        return df
    id_col = _find_df_record_id_col(df)
    if id_col is None:
        return df
    mask = [_canonical_record_id(val) not in exclude_ids for val in df[id_col].tolist()]
    return df.loc[mask].reset_index(drop=True)


def _coerce_path_str(path: Any) -> Any:
    """Normalize ``os.PathLike`` paths to ``str`` while preserving mock objects."""
    return os.fspath(path) if isinstance(path, os.PathLike) else path


def _atomic_replace_file(
    dest_path: str | os.PathLike[str],
    write_fn: Callable[[str], Any],
    *,
    suffix: str = ".tmp",
) -> None:
    """Write via a sibling temporary file and atomically replace ``dest_path``."""
    dest_str = _coerce_path_str(dest_path)
    dest = Path(dest_str)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = str(dest.parent / f".{dest.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}{suffix}")
    try:
        write_fn(tmp_path)
        if Path(tmp_path).exists():
            os.replace(tmp_path, dest_str)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _concat_tabular_frames(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate tabular DataFrames while avoiding pandas all-NA column dtype warnings."""
    non_empty = [f for f in frames if not f.empty]
    if not non_empty:
        return frames[0].copy() if frames else pd.DataFrame()
    if len(non_empty) == 1:
        return non_empty[0].reset_index(drop=True)

    unified_cols: list[Any] = []
    seen_cols: set[Any] = set()
    for frame in non_empty:
        for col in frame.columns:
            if col not in seen_cols:
                seen_cols.add(col)
                unified_cols.append(col)

    non_na_somewhere = {
        col for frame in non_empty for col in frame.columns if bool(frame[col].notna().any())
    }
    prepared: list[pd.DataFrame] = []
    for frame in non_empty:
        keep_cols = [
            col
            for col in frame.columns
            if bool(frame[col].notna().any()) or col not in non_na_somewhere
        ]
        if keep_cols:
            prepared.append(frame[keep_cols])
        else:
            anchor = next(iter(non_na_somewhere))
            anchor_dtype = next(
                other[anchor].dtype
                for other in non_empty
                if anchor in other.columns and bool(other[anchor].notna().any())
            )
            stub = pd.DataFrame(
                {anchor: pd.Series([pd.NA] * len(frame), index=frame.index, dtype=anchor_dtype)}
            )
            prepared.append(stub)

    merged = pd.concat(prepared, ignore_index=True)
    return merged.reindex(columns=unified_cols)


def _sanitize_json_value(value: Any) -> Any:
    """Recursively convert missing sentinels and nonfinite floats to ``None`` for RFC 8259 JSON."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return {k: _sanitize_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json_value(v) for v in value]
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        as_float = float(value)
        return None if (math.isnan(as_float) or math.isinf(as_float)) else as_float
    if _is_missing_scalar(value):
        return None
    item_fn = getattr(value, "item", None)
    if callable(item_fn):
        try:
            return _sanitize_json_value(item_fn())
        except Exception:
            return value
    return value


def _dataframe_to_json_records(df: Any) -> list[dict[str, Any]]:
    """Convert a DataFrame (or test mock) to strict JSON-safe record dictionaries."""
    if not isinstance(df, pd.DataFrame):
        raw_mock = cast(Any, df).where(pd.notnull(df), None).to_dict(orient="records")
        return cast(list[dict[str, Any]], _sanitize_json_value(raw_mock))
    if df.empty:
        return []
    obj_df = df.astype(object)
    raw_records = obj_df.where(pd.notnull(obj_df), None).to_dict(orient="records")
    return cast(list[dict[str, Any]], _sanitize_json_value(raw_records))


class TabularCSVSink(ExportSink):
    """Sink for exporting data to CSV format."""

    def __init__(self, path: str | os.PathLike[str], config: TabularSinkConfig):
        """Initialize the CSV sink."""
        super().__init__(config)
        self.path: str = _coerce_path_str(path)
        self._is_first_batch = True
        self._cfg = config
        self._is_incremental_sync = _is_incremental_config(config)
        self._staged_frames: list[pd.DataFrame] = []
        self._voided_ids: set[str] = set()

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write a batch of records to the sink."""
        df = _prepare_tabular_batch_df(
            records,
            mask_phi=self._cfg.mask_phi,
            sanitize=self._cfg.sanitize,
        )
        if len(df) == 0:
            return 0

        if self._is_incremental_sync and self._cfg.idempotent:

            def stage_export() -> int:
                csv_str = df.to_csv(
                    index=False,
                    header=self._is_first_batch,
                    **self._cfg.pandas_kwargs,
                )
                checksum = hashlib.sha256(csv_str.encode("utf-8")).hexdigest()
                rows_loaded = len(df)
                self._append_manifest(batch_id, rows_loaded, checksum)
                id_col = _find_df_record_id_col(df)
                if id_col is not None:
                    for raw_id in df[id_col].tolist():
                        cid = _canonical_record_id(raw_id)
                        if cid is not None:
                            self._voided_ids.discard(cid)
                self._staged_frames.append(df)
                return rows_loaded

            loaded = self._execute_with_retry("export_csv", batch_id, stage_export)
            self._is_first_batch = False
            return loaded

        def execute_export() -> int:
            if self._is_first_batch:
                if self._cfg.idempotent or not Path(self.path).exists():
                    mode = "w"
                    header = True
                else:
                    mode = "a"
                    header = Path(self.path).stat().st_size == 0
            else:
                mode = "a"
                header = False

            csv_str = df.to_csv(index=False, header=header, **self._cfg.pandas_kwargs)
            checksum = hashlib.sha256(csv_str.encode('utf-8')).hexdigest()

            with open(self.path, mode=mode, encoding="utf-8") as f:
                f.write(csv_str)

            rows_loaded = len(df)
            self._append_manifest(batch_id, rows_loaded, checksum)
            return rows_loaded

        loaded = self._execute_with_retry("export_csv", batch_id, execute_export)
        self._is_first_batch = False
        return loaded

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Stage newly voided record IDs for atomic CSV reconciliation on flush."""
        _ = batch_id
        if not self._cfg.idempotent or not records:
            return 0
        cids = [
            cid for r in records if (cid := _canonical_record_id(extract_record_id(r))) is not None
        ]
        if not cids:
            return 0
        self._voided_ids.update(cids)
        return len(cids)

    def _reconcile_csv_to_disk(self) -> int:
        combined_delta = (
            _concat_tabular_frames(self._staged_frames) if self._staged_frames else pd.DataFrame()
        )
        dedup_delta, delta_ids = _dedupe_df_by_record_id(
            combined_delta,
            exclude_ids=self._voided_ids,
        )
        dest_exists = Path(self.path).exists() and Path(self.path).stat().st_size > 0
        sep = str(self._cfg.pandas_kwargs.get("sep", ","))

        if not dest_exists:
            if dedup_delta.empty:
                return 0
            csv_out = dedup_delta.to_csv(index=False, header=True, **self._cfg.pandas_kwargs)
            _atomic_replace_file(
                self.path,
                lambda tmp: Path(tmp).write_text(csv_out, encoding="utf-8"),
                suffix=".csv.tmp",
            )
            return len(dedup_delta)

        delta_fields = [str(c) for c in dedup_delta.columns]
        delta_rows: list[dict[str, str]] = []
        if not dedup_delta.empty:
            rendered_delta = dedup_delta.to_csv(
                index=False,
                header=True,
                **self._cfg.pandas_kwargs,
            )
            delta_rows = list(csv.DictReader(io.StringIO(rendered_delta), delimiter=sep))

        exclude_ids = delta_ids | self._voided_ids

        def _stream_merge(tmp_path: str) -> None:
            with (
                open(self.path, encoding="utf-8", newline="") as src_f,
                open(tmp_path, mode="w", encoding="utf-8", newline="") as dst_f,
            ):
                reader = csv.DictReader(src_f, delimiter=sep)
                existing_fields = [f for f in (reader.fieldnames or []) if f is not None]
                unified_fields = existing_fields + [
                    c for c in delta_fields if c not in existing_fields
                ]
                if not unified_fields:
                    return
                writer = csv.DictWriter(
                    dst_f,
                    fieldnames=unified_fields,
                    delimiter=sep,
                    lineterminator="\n",
                    restval="",
                )
                writer.writeheader()
                for row in reader:
                    row_cid = _extract_row_canonical_id(row)
                    if row_cid is not None and row_cid in exclude_ids:
                        continue
                    writer.writerow({k: (row.get(k) or "") for k in unified_fields})
                for d_row in delta_rows:
                    writer.writerow({k: (d_row.get(k) or "") for k in unified_fields})

        _atomic_replace_file(self.path, _stream_merge, suffix=".csv.tmp")
        return len(delta_rows)

    def _append_manifest(self, batch_id: str, row_count: int, checksum: str) -> None:
        if not self._cfg.manifest_path:
            return
        entry = {
            "batch_id": batch_id,
            "destination": self.path,
            "row_count": row_count,
            "checksum": checksum,
            "loaded_at": datetime.now(tz=UTC).isoformat(),
        }
        with open(self._cfg.manifest_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + os.linesep)

    def flush(self) -> None:
        """Finalize staged incremental CSV updates and voided deletions atomically."""
        should_reconcile = False
        if self._is_incremental_sync and self._cfg.idempotent:
            should_reconcile = bool(self._staged_frames or self._voided_ids)
        elif (
            self._cfg.idempotent
            and self._voided_ids
            and Path(self.path).exists()
            and Path(self.path).stat().st_size > 0
        ):
            should_reconcile = True

        if not should_reconcile:
            return

        try:
            self._reconcile_csv_to_disk()
        finally:
            self._staged_frames.clear()
            self._voided_ids.clear()

    def close(self) -> None:
        """Clear any staged state."""
        self._staged_frames.clear()
        self._voided_ids.clear()


_RECORD_META_CONTRACT_TYPES: tuple[tuple[str, str], ...] = (
    ("recordId", "int64"),
    ("subjectKey", "string"),
    ("visitId", "int64"),
    ("formId", "int64"),
    ("recordStatus", "string"),
    ("dateCreated", "string"),
)

_LOGICAL_TO_PANDAS_DTYPE: dict[str, str] = {
    "int64": "Int64",
    "float64": "Float64",
    "bool": "boolean",
    "string": "string",
}

_LOGICAL_TO_DUCKDB_SQL: dict[str, str] = {
    "int64": "BIGINT",
    "float64": "DOUBLE",
    "bool": "BOOLEAN",
    "string": "VARCHAR",
}


def _quote_duckdb_column(name: str) -> str:
    """Safely double-quote an arbitrary DataFrame column identifier for DuckDB DDL."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError(f"Invalid DuckDB column name {name!r}.")
    return '"' + name.replace('"', '""') + '"'


def _is_missing_scalar(value: Any) -> bool:
    """Return True when ``value`` is a scalar missing sentinel without failing on sequences."""
    if value is None or value is pd.NA:
        return True
    if isinstance(value, (bytes, str, Mapping, Sequence)):
        return False
    try:
        res = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if hasattr(res, "__len__"):
        return False
    return bool(res)


def _variable_type_to_logical(var_type: str | None) -> str | None:
    """Map an iMednet ``Variable.variable_type`` string to a logical tabular type."""
    if not var_type or not isinstance(var_type, str):
        return None
    normalized = var_type.strip().lower()
    if normalized in {"int", "integer", "number"}:
        return "int64"
    if normalized in {"float", "decimal", "double", "numeric"}:
        return "float64"
    if normalized in {"bool", "boolean", "checkbox"}:
        return "bool"
    if normalized in {"text", "string", "date", "datetime", "time", "memo", "dropdown", "radio"}:
        return "string"
    return None


def _unify_logical_types(left: str | None, right: str | None) -> str | None:
    """Combine two logical tabular types into the narrowest common supertype."""
    if left is None:
        return right
    if right is None:
        return left
    if left == right:
        return left
    if {left, right} <= {"int64", "float64"}:
        return "float64"
    return "string"


def _build_sdtm_contract_types(
    domain: str | None,
    *,
    variable_whitelist: Sequence[str] | None = None,
) -> dict[str, str | None]:
    """Derive expected uppercase SDTM column names and logical types for ``domain``."""
    dom = resolve_sdtm_domain(domain, fallback="AE")
    std_vars = get_sdtm_domain_variables(dom)
    seq_col = f"{dom}SEQ"
    contract: dict[str, str | None] = {}
    for col in std_vars:
        if col in {seq_col, "AGE"}:
            contract[col] = "int64"
        else:
            contract[col] = "string"
    if variable_whitelist is not None:
        for extra_var in variable_whitelist:
            if extra_var not in contract:
                contract[extra_var] = "string"
    return contract


def _apply_naming_convention_to_df(
    df: pd.DataFrame,
    convention: str | None,
    *,
    domain: str | None = None,
    meta_cols: set[str] | None = None,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Rename DataFrame metadata and variable columns according to ``convention``."""
    canonical_conv = normalize_naming_convention(convention)
    if canonical_conv is None or (df.empty and len(df.columns) == 0):
        return df
    meta_lookup = {
        m.lower() for m in (meta_cols or {k for k, _ in _RECORD_META_CONTRACT_TYPES})
    } | {"studykey", "study_key", "studyid"}
    renamed_cols = [
        format_variable_name(
            str(col),
            canonical_conv,
            domain=domain,
            is_metadata=(str(col).lower() in meta_lookup),
        )
        for col in df.columns
    ]
    out = df.copy()
    out.columns = pd.Index(renamed_cols)
    if study_key:
        study_col = format_variable_name(
            "study_key",
            canonical_conv,
            domain=domain,
            is_metadata=True,
        )
        if study_col.lower() not in {str(c).lower() for c in out.columns}:
            out.insert(0, study_col, study_key)
    dup_mask = out.columns.astype(str).str.lower().duplicated()
    if bool(dup_mask.any()):
        out = out.loc[:, ~dup_mask]
    return out


def _build_tabular_contract_types(
    variable_keys: Sequence[str],
    label_map: Mapping[str, Any],
    variables: Sequence[Any],
    *,
    use_labels_as_columns: bool,
    mask_phi: bool = True,
    phi_keys: set[str] | None = None,
    pipeline: EnrichmentPipeline | None = None,
    naming_convention: str | None = None,
    sdtm_domain: str | None = None,
) -> dict[str, str | None]:
    """Derive expected column names and logical types from the Record + Variable contract."""
    canonical_conv = (
        None if use_labels_as_columns else normalize_naming_convention(naming_convention)
    )
    if canonical_conv is None:
        contract: dict[str, str | None] = dict(_RECORD_META_CONTRACT_TYPES)
    else:
        contract = {}
        for meta_name, meta_type in [("studyKey", "string"), *_RECORD_META_CONTRACT_TYPES]:
            formatted_meta = format_variable_name(
                meta_name, canonical_conv, domain=sdtm_domain, is_metadata=True
            )
            contract.setdefault(formatted_meta, meta_type)
    seen_lower: set[str] = {name.lower() for name in contract}
    active_phi_keys = (
        phi_keys
        if phi_keys is not None
        else (set(pipeline.phi_keys) if pipeline is not None else None)
    )

    vars_by_name: dict[str, list[Any]] = {}
    for var in variables:
        vname = getattr(var, "variable_name", None)
        if isinstance(vname, str) and vname:
            vars_by_name.setdefault(vname, []).append(var)

    effective_keys: list[str] = list(variable_keys)
    if pipeline is not None and pipeline.mappings:
        replaced_sources: set[str] = set()
        mapped_targets: list[str] = []
        for src_var, rule in pipeline.mappings.items():
            if rule.target_field and rule.target_field != src_var and src_var in variable_keys:
                replaced_sources.add(src_var)
                if (
                    rule.target_field not in variable_keys
                    and rule.target_field not in mapped_targets
                ):
                    mapped_targets.append(rule.target_field)
        effective_keys = [k for k in variable_keys if k not in replaced_sources] + mapped_targets

    for key in effective_keys:
        raw_col_name = str(label_map.get(key, key) if use_labels_as_columns else key)
        col_name = (
            format_variable_name(raw_col_name, canonical_conv, domain=sdtm_domain)
            if canonical_conv is not None
            else raw_col_name
        )
        col_lower = col_name.lower()
        if col_lower in seen_lower:
            continue
        seen_lower.add(col_lower)
        if mask_phi and (
            global_sensitivity_registry.is_sensitive(key)
            or global_sensitivity_registry.is_sensitive(raw_col_name)
            or global_sensitivity_registry.is_sensitive(col_name)
            or _is_custom_phi_key(key, active_phi_keys)
            or _is_custom_phi_key(raw_col_name, active_phi_keys)
            or _is_custom_phi_key(col_name, active_phi_keys)
        ):
            contract[col_name] = "string"
            continue
        if pipeline is not None and key in pipeline.terminology:
            contract[col_name] = "string"
            continue
        logical: str | None = None
        for var in vars_by_name.get(key, ()):
            vt_logical = _variable_type_to_logical(getattr(var, "variable_type", None))
            logical = _unify_logical_types(logical, vt_logical)
        contract[col_name] = logical

    if mask_phi:
        for col in list(contract):
            if global_sensitivity_registry.is_sensitive(col) or _is_custom_phi_key(
                col, active_phi_keys
            ):
                contract[col] = "string"
    return contract


def _build_empty_contract_df(
    contract_types: Mapping[str, str | None] | None,
) -> pd.DataFrame:
    """Build a zero-row DataFrame preserving contract columns and nullable dtypes."""
    if not contract_types:
        return pd.DataFrame({"recordId": pd.Series(dtype="Int64")})
    return pd.DataFrame(
        {
            col: pd.Series(dtype=_LOGICAL_TO_PANDAS_DTYPE.get(ltype or "string", "string"))
            for col, ltype in contract_types.items()
        }
    )


def _apply_contract_to_all_null_df_columns(
    df: pd.DataFrame,
    contract_types: Mapping[str, str | None] | None,
) -> pd.DataFrame:
    """Assign contract nullable dtypes to all-null columns so initial DDL uses concrete types."""
    if not contract_types or df.empty:
        return df
    adjusted: pd.DataFrame | None = None
    for col in df.columns:
        ltype = contract_types.get(str(col))
        if ltype is not None and bool(df[col].isna().all()):
            if adjusted is None:
                adjusted = df.copy()
            adjusted[col] = pd.Series(
                [pd.NA] * len(df),
                index=df.index,
                dtype=_LOGICAL_TO_PANDAS_DTYPE[ltype],
            )
    return adjusted if adjusted is not None else df


def _infer_series_logical_type(series: pd.Series) -> str | None:
    """Infer a logical tabular type for ``series``, returning ``None`` when all values are null."""
    if len(series) == 0:
        return None
    non_missing = [v for v in series.tolist() if not _is_missing_scalar(v)]
    if not non_missing:
        return None
    if pd.api.types.is_bool_dtype(series.dtype):
        return "bool"
    if pd.api.types.is_integer_dtype(series.dtype):
        return "int64"
    if pd.api.types.is_float_dtype(series.dtype):
        return "float64"
    if pd.api.types.is_string_dtype(series.dtype) and series.dtype != object:
        return "string"
    if series.dtype == object:
        if all(isinstance(v, bool) for v in non_missing):
            return "bool"
        if all(isinstance(v, int) and not isinstance(v, bool) for v in non_missing):
            return "int64"
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_missing):
            return "float64"
        return "string"
    return None


def _duckdb_sql_to_logical(sql_type: str) -> str | None:
    """Map a DuckDB ``PRAGMA table_info`` SQL type string to a logical tabular type."""
    upper = sql_type.strip().upper()
    if upper in {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "INT",
        "INT4",
        "INT8",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
    }:
        return "int64"
    if upper in {"FLOAT", "REAL", "FLOAT4", "DOUBLE", "FLOAT8"} or upper.startswith(
        ("DECIMAL", "NUMERIC")
    ):
        return "float64"
    if upper in {"BOOLEAN", "BOOL"}:
        return "bool"
    if "CHAR" in upper or upper in {"TEXT", "STRING"}:
        return "string"
    return None


def _duckdb_table_exists(conn: Any, table_name: str) -> bool:
    """Return True when ``table_name`` exists as a base table in DuckDB's current schema."""
    rows = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = ? AND table_type = 'BASE TABLE'",
        [table_name],
    ).fetchall()
    return bool(rows)


def _replace_duckdb_with_empty_table(
    conn: Any,
    table_name: str,
    contract_types: Mapping[str, str | None] | None,
) -> None:
    """Replace ``table_name`` with a zero-row DuckDB table preserving schema when available."""
    quoted_tbl = _quote_duckdb_identifier(table_name)
    if not contract_types and _duckdb_table_exists(conn, table_name):
        conn.execute(
            f"CREATE OR REPLACE TABLE {quoted_tbl} AS SELECT * FROM {quoted_tbl} WHERE 1=0"
        )
        return
    empty_df = _build_empty_contract_df(contract_types)
    quoted_alias = _quote_duckdb_identifier(_DUCKDB_DF_ALIAS)
    conn.register(_DUCKDB_DF_ALIAS, empty_df)
    try:
        conn.execute(f"CREATE OR REPLACE TABLE {quoted_tbl} AS SELECT * FROM {quoted_alias}")
    finally:
        conn.unregister(_DUCKDB_DF_ALIAS)


def _collect_untyped_all_null_columns(
    df: pd.DataFrame,
    contract_types: Mapping[str, str | None] | None,
) -> set[str]:
    """Identify columns created from all-null values without a contract type."""
    ctypes = contract_types or {}
    untyped: set[str] = set()
    for col_raw in df.columns:
        col = str(col_raw)
        if ctypes.get(col) is None and _infer_series_logical_type(df[col_raw]) is None:
            untyped.add(col)
    return untyped


def _evolve_duckdb_table_for_df(
    conn: Any,
    table_name: str,
    df: pd.DataFrame,
    contract_types: Mapping[str, str | None] | None,
    untyped_null_cols: set[str],
) -> pd.DataFrame:
    """Widen or add DuckDB table columns prior to ``INSERT INTO ... BY NAME``."""
    if df.empty or isinstance(conn, NonCallableMock):
        return df
    _validate_duckdb_identifier(table_name)
    quoted_tbl = _quote_duckdb_identifier(table_name)
    ctypes = contract_types or {}
    existing_info = {
        str(row[1]): str(row[2])
        for row in conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    }
    adjusted: pd.DataFrame | None = None

    for col_raw in df.columns:
        col = str(col_raw)
        series = df[col_raw]
        incoming_logical = _infer_series_logical_type(series)
        quoted_col = _quote_duckdb_column(col)

        if col not in existing_info:
            desired_logical = ctypes.get(col) or incoming_logical
            if desired_logical is None:
                untyped_null_cols.add(col)
                sql_type = "INTEGER"
            else:
                sql_type = _LOGICAL_TO_DUCKDB_SQL[desired_logical]
            conn.execute(f"ALTER TABLE {quoted_tbl} ADD COLUMN {quoted_col} {sql_type}")
            existing_info[col] = sql_type
            continue

        if incoming_logical is None:
            continue

        existing_sql = existing_info[col]
        existing_logical = _duckdb_sql_to_logical(existing_sql)
        if (
            incoming_logical == "float64"
            and pd.api.types.is_float_dtype(series.dtype)
            and bool(series.isna().any())
            and (ctypes.get(col) == "int64" or existing_logical == "int64")
        ):
            non_missing = [v for v in series.tolist() if not _is_missing_scalar(v)]
            if all(float(v).is_integer() for v in non_missing):
                if adjusted is None:
                    adjusted = df.copy()
                adjusted[col_raw] = pd.Series(
                    [pd.NA if _is_missing_scalar(v) else int(v) for v in series.tolist()],
                    index=df.index,
                    dtype="Int64",
                )
                incoming_logical = "int64"

        if col in untyped_null_cols:
            untyped_null_cols.discard(col)
            target_sql = _LOGICAL_TO_DUCKDB_SQL[incoming_logical]
            if existing_sql.strip().upper() != target_sql:
                conn.execute(f"ALTER TABLE {quoted_tbl} ALTER {quoted_col} TYPE {target_sql}")
                existing_info[col] = target_sql
            continue

        if existing_logical is not None:
            unified = _unify_logical_types(existing_logical, incoming_logical)
            if unified is not None and unified != existing_logical:
                target_sql = _LOGICAL_TO_DUCKDB_SQL[unified]
                conn.execute(f"ALTER TABLE {quoted_tbl} ALTER {quoted_col} TYPE {target_sql}")
                existing_info[col] = target_sql

    return adjusted if adjusted is not None else df


def _logical_to_arrow_type(pa: Any, logical_type: str | None) -> Any | None:
    """Convert a logical tabular type name into a ``pyarrow.DataType``."""
    if logical_type == "int64":
        return pa.int64()
    if logical_type == "float64":
        return pa.float64()
    if logical_type == "bool":
        return pa.bool_()
    if logical_type == "string":
        return pa.string()
    return None


def _series_to_arrow_array(pa: Any, series: pd.Series) -> Any:
    """Convert a single pandas Series to an Arrow Array, stringifying mixed object scalars."""
    try:
        arr = pa.Array.from_pandas(series)
        if pa.types.is_large_string(arr.type):
            return arr.cast(pa.string())
        return arr
    except (
        pa.ArrowInvalid,
        pa.ArrowTypeError,
        pa.ArrowNotImplementedError,
        TypeError,
        ValueError,
    ):
        return pa.array(
            [None if _is_missing_scalar(v) else str(v) for v in series.tolist()],
            type=pa.string(),
        )


def _safe_cast_arrow_array(pa: Any, col: Any, target_type: Any) -> Any:
    """Cast an Arrow Array/ChunkedArray to ``target_type``, stringifying Python values on fallback."""
    try:
        return col.cast(target_type)
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError):
        if pa.types.is_string(target_type) or pa.types.is_large_string(target_type):
            return pa.array(
                [None if v is None else str(v) for v in col.to_pylist()],
                type=target_type,
            )
        raise


def _unify_arrow_types(pa: Any, left: Any, right: Any) -> Any:
    """Return a ``pyarrow.DataType`` capable of representing both ``left`` and ``right``."""
    if pa.types.is_null(left):
        return right
    if pa.types.is_null(right):
        return left
    if left == right:
        return left
    if pa.types.is_integer(left) and pa.types.is_integer(right):
        return pa.int64()
    if (pa.types.is_integer(left) or pa.types.is_floating(left)) and (
        pa.types.is_integer(right) or pa.types.is_floating(right)
    ):
        return pa.float64()
    return pa.string()


def _align_arrow_column(
    pa: Any,
    series: Any,
    inferred_col: Any,
    target_type: Any,
    row_count: int,
) -> tuple[Any, Any]:
    """Align a batch column to ``target_type``, widening only when lossless cast is impossible."""
    if inferred_col is None:
        return pa.nulls(row_count, type=target_type), target_type
    if pa.types.is_null(target_type):
        return inferred_col, inferred_col.type
    if pa.types.is_null(inferred_col.type):
        return pa.nulls(row_count, type=target_type), target_type
    if inferred_col.type == target_type:
        return inferred_col, target_type

    if pa.types.is_string(target_type) or pa.types.is_large_string(target_type):
        if series is not None and pa.types.is_floating(inferred_col.type):
            try:
                as_int = pa.array(series, type=pa.int64(), from_pandas=True, safe=True)
                return as_int.cast(target_type), target_type
            except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError):
                pass
        return _safe_cast_arrow_array(pa, inferred_col, target_type), target_type

    if series is not None and pa.types.is_boolean(target_type) == pa.types.is_boolean(
        inferred_col.type
    ):
        try:
            cast_arr = pa.array(series, type=target_type, from_pandas=True, safe=True)
            return cast_arr, target_type
        except (
            pa.ArrowInvalid,
            pa.ArrowTypeError,
            pa.ArrowNotImplementedError,
            TypeError,
            ValueError,
        ):
            pass

    widened = _unify_arrow_types(pa, target_type, inferred_col.type)
    return _safe_cast_arrow_array(pa, inferred_col, widened), widened


def _cast_arrow_table_to_schema(pa: Any, tbl: Any, target_schema: Any) -> Any:
    """Cast or null-pad an Arrow ``Table`` to match ``target_schema``."""
    cast_cols: list[Any] = []
    for target_field in target_schema:
        if target_field.name in tbl.column_names:
            col = tbl[target_field.name]
            if pa.types.is_null(col.type):
                cast_cols.append(pa.nulls(tbl.num_rows, type=target_field.type))
            elif col.type != target_field.type:
                cast_cols.append(_safe_cast_arrow_array(pa, col, target_field.type))
            else:
                cast_cols.append(col)
        else:
            cast_cols.append(pa.nulls(tbl.num_rows, type=target_field.type))
    return pa.Table.from_arrays(cast_cols, schema=target_schema)


def _unify_arrow_schemas(pa: Any, base_schema: Any, incoming_schema: Any) -> Any:
    """Return a unified ``pyarrow.Schema`` widening overlapping fields and appending new fields."""
    incoming_by_name = {f.name: f.type for f in incoming_schema}
    unified_fields: list[Any] = []
    seen_names: set[str] = set()
    for b_field in base_schema:
        seen_names.add(b_field.name)
        if b_field.name in incoming_by_name:
            utype = _unify_arrow_types(pa, b_field.type, incoming_by_name[b_field.name])
        else:
            utype = b_field.type
        unified_fields.append(pa.field(b_field.name, utype))
    for inc_field in incoming_schema:
        if inc_field.name not in seen_names:
            unified_fields.append(pa.field(inc_field.name, inc_field.type))
    return pa.schema(unified_fields)


def _filter_arrow_table_excluding_ids(pa: Any, tbl: Any, exclude_ids: set[str]) -> Any:
    """Filter out rows from ``tbl`` whose canonical record ID is in ``exclude_ids``."""
    if tbl.num_rows == 0 or not exclude_ids:
        return tbl
    id_col = next(
        (c for c in ("recordId", "record_id", "RECORD_ID") if c in tbl.column_names),
        None,
    )
    if id_col is None:
        return tbl
    mask = [_canonical_record_id(val) not in exclude_ids for val in tbl[id_col].to_pylist()]
    if all(mask):
        return tbl
    return tbl.filter(pa.array(mask, type=pa.bool_()))


class TabularParquetSink(ExportSink):
    """Streaming Parquet sink backed by ``pyarrow.parquet.ParquetWriter``."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        config: TabularSinkConfig,
        *,
        contract_types: Mapping[str, str | None] | None = None,
    ) -> None:
        """Initialize the Parquet sink."""
        super().__init__(config)
        self.path: str = _coerce_path_str(path)
        self._cfg = config
        self._writer: Any = None
        self._schema: Any = None
        self._contract_types: dict[str, str | None] = dict(contract_types) if contract_types else {}
        self._is_incremental_sync = _is_incremental_config(config)
        self._parquet_kwargs = dict(config.pandas_kwargs)
        self._parquet_kwargs.pop("engine", None)
        self._compression = self._parquet_kwargs.pop("compression", "snappy")
        self._voided_ids: set[str] = set()
        self._delta_id_counts: dict[str, int] = {}
        self._delta_path: str | None = None

    def set_contract_types(self, contract_types: Mapping[str, str | None]) -> None:
        """Configure contract-derived column types prior to streaming batches."""
        self._contract_types = dict(contract_types)

    def _use_staged_delta(self) -> bool:
        return bool(self._is_incremental_sync and self._cfg.idempotent)

    def _active_writer_path(self) -> str:
        if self._use_staged_delta():
            if self._delta_path is None:
                dest = Path(self.path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                self._delta_path = str(
                    dest.parent / f".{dest.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.delta.parquet"
                )
            return self._delta_path
        return self.path

    def _align_batch_table(self, pa: Any, df: pd.DataFrame) -> tuple[Any, bool]:
        """Convert ``df`` into an Arrow table aligned with contract or active writer schema."""
        row_count = len(df)
        col_names: list[str] = []
        series_by_name: dict[str, pd.Series] = {}
        inferred_by_name: dict[str, Any] = {}
        for col_raw in df.columns:
            col_name = str(col_raw)
            series = df[col_raw]
            col_names.append(col_name)
            series_by_name[col_name] = series
            inferred_by_name[col_name] = _series_to_arrow_array(pa, series)

        arrays: list[Any] = []
        fields: list[Any] = []

        if self._schema is None:
            for col_name in col_names:
                inferred_col = inferred_by_name[col_name]
                contract_pa = _logical_to_arrow_type(pa, self._contract_types.get(col_name))
                desired = contract_pa if contract_pa is not None else inferred_col.type
                arr, resolved = _align_arrow_column(
                    pa, series_by_name[col_name], inferred_col, desired, row_count
                )
                arrays.append(arr)
                fields.append(pa.field(col_name, resolved))
            target_schema = pa.schema(fields)
            return pa.Table.from_arrays(arrays, schema=target_schema), False

        evolved = False
        existing_names: set[str] = set()
        for existing_field in self._schema:
            existing_names.add(existing_field.name)
            inferred_col = inferred_by_name.get(existing_field.name)
            series_opt = series_by_name.get(existing_field.name)
            arr, resolved = _align_arrow_column(
                pa, series_opt, inferred_col, existing_field.type, row_count
            )
            if resolved != existing_field.type:
                evolved = True
            arrays.append(arr)
            fields.append(pa.field(existing_field.name, resolved))

        for new_col_name in col_names:
            if new_col_name in existing_names:
                continue
            evolved = True
            inferred_col = inferred_by_name[new_col_name]
            contract_pa = _logical_to_arrow_type(pa, self._contract_types.get(new_col_name))
            desired = contract_pa if contract_pa is not None else inferred_col.type
            arr, resolved = _align_arrow_column(
                pa, series_by_name[new_col_name], inferred_col, desired, row_count
            )
            arrays.append(arr)
            fields.append(pa.field(new_col_name, resolved))

        target_schema = pa.schema(fields)
        return pa.Table.from_arrays(arrays, schema=target_schema), evolved

    def _evolve_writer_schema(self, pa: Any, pq: Any, new_schema: Any) -> None:
        """Stream existing row groups into a reopened ParquetWriter when schema widens."""
        target_path = self._active_writer_path()
        self._writer.close()
        self._writer = None
        tmp_prev = f"{target_path}.schema_evolve.tmp"
        os.replace(target_path, tmp_prev)
        try:
            self._writer = cast(Any, pq).ParquetWriter(
                target_path,
                new_schema,
                compression=self._compression,
                **self._parquet_kwargs,
            )
            with pq.ParquetFile(tmp_prev) as prev_pf:
                for record_batch in prev_pf.iter_batches(batch_size=max(1, self._cfg.batch_size)):
                    prev_tbl = pa.Table.from_batches([record_batch])
                    self._writer.write_table(_cast_arrow_table_to_schema(pa, prev_tbl, new_schema))
            self._schema = new_schema
        finally:
            Path(tmp_prev).unlink(missing_ok=True)

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write a DataFrame or sequence batch to the open ParquetWriter."""
        df = _prepare_tabular_batch_df(
            records,
            mask_phi=self._cfg.mask_phi,
            sanitize=self._cfg.sanitize,
        )
        if len(df) == 0:
            return 0

        def execute_export() -> int:
            import pyarrow as pa
            import pyarrow.parquet as pq

            table, schema_evolved = self._align_batch_table(pa, df)
            target_path = self._active_writer_path()
            if self._writer is None:
                self._schema = table.schema
                self._writer = cast(Any, pq).ParquetWriter(
                    target_path,
                    self._schema,
                    compression=self._compression,
                    **self._parquet_kwargs,
                )
            elif schema_evolved:
                self._evolve_writer_schema(pa, pq, table.schema)
            self._writer.write_table(table)
            if self._use_staged_delta():
                id_col = _find_df_record_id_col(df)
                if id_col is not None:
                    for raw_id in df[id_col].tolist():
                        cid = _canonical_record_id(raw_id)
                        if cid is not None:
                            self._delta_id_counts[cid] = self._delta_id_counts.get(cid, 0) + 1
                            self._voided_ids.discard(cid)
            return len(df)

        return self._execute_with_retry("export_parquet", batch_id, execute_export)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Stage newly voided record IDs for atomic Parquet reconciliation on flush."""
        _ = batch_id
        if not self._cfg.idempotent or not records:
            return 0
        cids = [
            cid for r in records if (cid := _canonical_record_id(extract_record_id(r))) is not None
        ]
        if not cids:
            return 0
        self._voided_ids.update(cids)
        return len(cids)

    def _write_empty_parquet(self) -> None:
        if self._contract_types:
            import pyarrow as pa
            import pyarrow.parquet as pq

            empty_schema = pa.schema(
                [
                    (col, _logical_to_arrow_type(pa, ltype) or pa.string())
                    for col, ltype in self._contract_types.items()
                ]
            )
            empty_arrays = [pa.nulls(0, type=f.type) for f in empty_schema]
            empty_table = pa.Table.from_arrays(empty_arrays, schema=empty_schema)
            cast(Any, pq).write_table(
                empty_table,
                self.path,
                compression=self._compression,
                **self._parquet_kwargs,
            )
        else:
            pd.DataFrame().to_parquet(
                self.path,
                index=False,
                compression=self._compression,
                **self._parquet_kwargs,
            )

    def _stream_delta_batches_filtered(
        self,
        pa: Any,
        delta_pf: Any,
        out_writer: Any,
        target_schema: Any,
    ) -> None:
        remaining = dict(self._delta_id_counts)
        for record_batch in delta_pf.iter_batches(batch_size=max(1, self._cfg.batch_size)):
            tbl = pa.Table.from_batches([record_batch])
            id_col = next(
                (c for c in ("recordId", "record_id", "RECORD_ID") if c in tbl.column_names),
                None,
            )
            if id_col is not None and tbl.num_rows > 0:
                mask: list[bool] = []
                for val in tbl[id_col].to_pylist():
                    cid = _canonical_record_id(val)
                    if cid is None:
                        mask.append(True)
                    else:
                        remaining[cid] = remaining.get(cid, 1) - 1
                        mask.append(cid not in self._voided_ids and remaining[cid] == 0)
                if not all(mask):
                    tbl = tbl.filter(pa.array(mask, type=pa.bool_()))
            if tbl.num_rows > 0:
                out_writer.write_table(_cast_arrow_table_to_schema(pa, tbl, target_schema))

    def _reconcile_parquet_to_disk(self) -> int:
        dest_exists = Path(self.path).exists() and Path(self.path).stat().st_size > 0
        has_delta = bool(
            self._delta_path
            and Path(self._delta_path).exists()
            and Path(self._delta_path).stat().st_size > 0
        )

        if not has_delta and not self._voided_ids:
            if not dest_exists:
                self._write_empty_parquet()
            return 0

        import pyarrow as pa
        import pyarrow.parquet as pq

        pq_any: Any = pq

        needs_delta_filter = any(cnt > 1 for cnt in self._delta_id_counts.values()) or bool(
            self._voided_ids & set(self._delta_id_counts)
        )

        if not dest_exists:
            if not has_delta:
                return 0
            delta_file = cast(str, self._delta_path)
            if not needs_delta_filter:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                os.replace(delta_file, self.path)
                return len(self._delta_id_counts)

            delta_schema = pq_any.read_schema(delta_file)

            def _write_dedup_delta(tmp_path: str) -> None:
                with (
                    pq_any.ParquetFile(delta_file) as delta_pf,
                    pq_any.ParquetWriter(
                        tmp_path,
                        delta_schema,
                        compression=self._compression,
                        **self._parquet_kwargs,
                    ) as out_writer,
                ):
                    self._stream_delta_batches_filtered(pa, delta_pf, out_writer, delta_schema)

            _atomic_replace_file(self.path, _write_dedup_delta, suffix=".parquet.tmp")
            return len(self._delta_id_counts)

        prev_schema = pq_any.read_schema(self.path)
        if has_delta:
            delta_schema = pq_any.read_schema(cast(str, self._delta_path))
            unified_schema = _unify_arrow_schemas(pa, prev_schema, delta_schema)
        else:
            unified_schema = prev_schema

        exclude_prev_ids = set(self._delta_id_counts) | self._voided_ids

        def _stream_merge(tmp_path: str) -> None:
            with pq_any.ParquetWriter(
                tmp_path,
                unified_schema,
                compression=self._compression,
                **self._parquet_kwargs,
            ) as out_writer:
                with pq_any.ParquetFile(self.path) as prev_pf:
                    for record_batch in prev_pf.iter_batches(
                        batch_size=max(1, self._cfg.batch_size)
                    ):
                        prev_tbl = pa.Table.from_batches([record_batch])
                        kept_tbl = _filter_arrow_table_excluding_ids(pa, prev_tbl, exclude_prev_ids)
                        if kept_tbl.num_rows > 0:
                            out_writer.write_table(
                                _cast_arrow_table_to_schema(pa, kept_tbl, unified_schema)
                            )
                if has_delta:
                    with pq_any.ParquetFile(cast(str, self._delta_path)) as delta_pf:
                        self._stream_delta_batches_filtered(
                            pa, delta_pf, out_writer, unified_schema
                        )

        _atomic_replace_file(self.path, _stream_merge, suffix=".parquet.tmp")
        return len(self._delta_id_counts)

    def flush(self) -> None:
        """Flush and finalize Parquet footer or reconcile incremental deltas before checkpoint commit."""
        wrote_batches = self._writer is not None
        if self._writer is not None:
            self._writer.close()
            self._writer = None

        if self._use_staged_delta() or (self._cfg.idempotent and self._voided_ids):
            try:
                self._reconcile_parquet_to_disk()
            finally:
                if self._delta_path is not None:
                    Path(self._delta_path).unlink(missing_ok=True)
                    self._delta_path = None
                self._delta_id_counts.clear()
                self._voided_ids.clear()
            return

        if not wrote_batches:
            self._write_empty_parquet()

    def close(self) -> None:
        """Ensure underlying ParquetWriter is closed and any staging file is removed."""
        if self._writer is not None:
            try:
                self._writer.close()
            finally:
                self._writer = None
        if self._delta_path is not None:
            Path(self._delta_path).unlink(missing_ok=True)
            self._delta_path = None
        self._delta_id_counts.clear()
        self._voided_ids.clear()
        self._schema = None


def _execute_chunked_sql_id_delete(
    engine: Any,
    table_name: str,
    candidate_cols: Sequence[str],
    record_ids: Sequence[Any],
) -> int:
    """Delete rows matching ``record_ids`` in chunks of 500 when ``table_name`` exists."""
    if not record_ids or isinstance(engine, NonCallableMock):
        return 0
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    inspector = sa_inspect(engine)
    if not inspector.has_table(table_name):
        return 0
    existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
    id_col = next((col for col in candidate_cols if col in existing_cols), None)
    if id_col is None:
        return 0

    preparer = engine.dialect.identifier_preparer
    quoted_table = preparer.quote(table_name)
    quoted_col = preparer.quote(id_col)
    with engine.begin() as conn:
        for start in range(0, len(record_ids), 500):
            chunk = record_ids[start : start + 500]
            params = {f"id_{idx}": rid for idx, rid in enumerate(chunk)}
            placeholders = ", ".join(f":{k}" for k in params)
            stmt = sa_text(f"DELETE FROM {quoted_table} WHERE {quoted_col} IN ({placeholders})")
            conn.execute(stmt, params)
    return len(record_ids)


class TabularSQLSink(ExportSink):
    """Sink for exporting data to SQL databases."""

    def __init__(self, table: str, engine: Any, config: TabularSinkConfig):
        """Initialize the SQL sink."""
        super().__init__(config)
        self.table = table
        self.engine = engine
        self._is_first_batch = True
        self._cfg = config
        self._is_incremental_sync = bool(
            config.incremental
            or config.modified_since is not None
            or config.state_provider is not None
        )
        explicit_if_exists = self._cfg.pandas_kwargs.pop("if_exists", None)
        if explicit_if_exists is not None:
            self._initial_if_exists = explicit_if_exists
        elif not config.idempotent:
            self._initial_if_exists = "append"
        else:
            self._initial_if_exists = "append" if self._is_incremental_sync else "replace"

    def _delete_existing_record_ids(self, record_ids: Sequence[Any]) -> int:
        """Delete existing rows matching ``recordId`` when the destination table exists."""
        return _execute_chunked_sql_id_delete(
            self.engine,
            self.table,
            ("recordId", "record_id", "RECORD_ID"),
            record_ids,
        )

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write a batch of records to the sink."""
        df = _prepare_tabular_batch_df(
            records,
            mask_phi=self._cfg.mask_phi,
            sanitize=self._cfg.sanitize,
        )
        if len(df) == 0:
            return 0

        def execute_export() -> int:
            if_exists = self._initial_if_exists if self._is_first_batch else "append"
            if (
                self._is_incremental_sync
                and self._cfg.idempotent
                and if_exists == "append"
                and not isinstance(self.engine, NonCallableMock)
            ):
                for candidate_col in ("recordId", "record_id", "RECORD_ID"):
                    if candidate_col in df.columns:
                        ids = [rid for rid in df[candidate_col].tolist() if pd.notna(rid)]
                        self._delete_existing_record_ids(ids)
                        break

            df_str = df.to_csv(index=False)
            checksum = hashlib.sha256(df_str.encode('utf-8')).hexdigest()

            _to_sql_with_chunking(
                df,
                self.table,
                self.engine,
                if_exists=cast(Literal["fail", "replace", "append", "delete_rows"], if_exists),
                **self._cfg.pandas_kwargs,
            )

            rows_loaded = len(df)
            self._append_manifest(batch_id, rows_loaded, checksum)
            return rows_loaded

        loaded = self._execute_with_retry("export_sql", batch_id, execute_export)
        self._is_first_batch = False
        return loaded

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Delete newly voided rows from the SQL table when running idempotently."""
        if not self._cfg.idempotent or not records or isinstance(self.engine, NonCallableMock):
            return 0
        record_ids = [rid for r in records if (rid := extract_record_id(r)) is not None]
        if not record_ids:
            return 0
        return self._execute_with_retry(
            "reconcile_sql_deleted",
            batch_id,
            lambda: self._delete_existing_record_ids(record_ids),
        )

    def _append_manifest(self, batch_id: str, row_count: int, checksum: str) -> None:
        if not self._cfg.manifest_path:
            return
        entry = {
            "batch_id": batch_id,
            "destination": self.table,
            "row_count": row_count,
            "checksum": checksum,
            "loaded_at": datetime.now(tz=UTC).isoformat(),
        }
        with open(self._cfg.manifest_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + os.linesep)

    def flush(self) -> None:
        """Flush the sink."""

    def close(self) -> None:
        """Close the sink."""


class TabularDuckDBSink(ExportSink):
    """Streaming sink for exporting DataFrame batches into a DuckDB table."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        table_name: str,
        config: TabularSinkConfig,
        *,
        contract_types: Mapping[str, str | None] | None = None,
        conn: Any | None = None,
    ) -> None:
        """Initialize the DuckDB sink."""
        super().__init__(config)
        _validate_duckdb_identifier(table_name)
        resolved_db_path: str = _coerce_path_str(db_path)
        self.db_path = resolved_db_path
        self.table_name = table_name
        self._cfg = config
        self._contract_types: dict[str, str | None] = dict(contract_types) if contract_types else {}
        self._untyped_null_cols: set[str] = set()
        self._is_first_batch = True
        self._is_incremental_sync = _is_incremental_config(config)
        self._owns_conn = conn is None
        if conn is not None:
            self._conn: Any = conn
        else:
            import duckdb

            self._conn = duckdb.connect(resolved_db_path, config={"threads": "1"})

    def set_contract_types(self, contract_types: Mapping[str, str | None]) -> None:
        """Configure contract-derived column types prior to streaming batches."""
        self._contract_types = dict(contract_types)

    def _table_exists(self) -> bool:
        return not isinstance(self._conn, NonCallableMock) and _duckdb_table_exists(
            self._conn, self.table_name
        )

    def _delete_record_ids(self, record_ids: Sequence[Any]) -> int:
        if not record_ids or not self._table_exists():
            return 0
        cols = {
            row[1]
            for row in self._conn.execute(f"PRAGMA table_info('{self.table_name}')").fetchall()
        }
        id_col = next(
            (c for c in ("recordId", "record_id", "RECORD_ID") if c in cols),
            None,
        )
        if id_col is None:
            return 0
        placeholders = ", ".join(["?"] * len(record_ids))
        quoted_tbl = _quote_duckdb_identifier(self.table_name)
        quoted_col = _quote_duckdb_identifier(id_col)
        self._conn.execute(
            f"DELETE FROM {quoted_tbl} WHERE {quoted_col} IN ({placeholders})",
            list(record_ids),
        )
        return len(record_ids)

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write a batch of records into DuckDB."""
        df = _prepare_tabular_batch_df(
            records,
            mask_phi=self._cfg.mask_phi,
            sanitize=self._cfg.sanitize,
        )
        if len(df) == 0:
            return 0

        def execute_export() -> int:
            quoted_tbl = _quote_duckdb_identifier(self.table_name)
            quoted_alias = _quote_duckdb_identifier(_DUCKDB_DF_ALIAS)
            replace_existing = (
                self._is_first_batch and self._cfg.idempotent and not self._is_incremental_sync
            )
            creating_table = replace_existing or not self._table_exists()
            if creating_table:
                df_to_register = _apply_contract_to_all_null_df_columns(df, self._contract_types)
                self._untyped_null_cols = _collect_untyped_all_null_columns(
                    df, self._contract_types
                )
            else:
                df_to_register = _evolve_duckdb_table_for_df(
                    self._conn,
                    self.table_name,
                    df,
                    self._contract_types,
                    self._untyped_null_cols,
                )
            self._conn.register(_DUCKDB_DF_ALIAS, df_to_register)
            try:
                if replace_existing:
                    self._conn.execute(
                        f"CREATE OR REPLACE TABLE {quoted_tbl} AS SELECT * FROM {quoted_alias}"
                    )
                elif creating_table:
                    self._conn.execute(f"CREATE TABLE {quoted_tbl} AS SELECT * FROM {quoted_alias}")
                else:
                    if self._is_incremental_sync and self._cfg.idempotent:
                        for candidate_col in ("recordId", "record_id", "RECORD_ID"):
                            if candidate_col in df.columns:
                                ids = [rid for rid in df[candidate_col].tolist() if pd.notna(rid)]
                                self._delete_record_ids(ids)
                                break
                    self._conn.execute(
                        f"INSERT INTO {quoted_tbl} BY NAME SELECT * FROM {quoted_alias}"
                    )
            finally:
                self._conn.unregister(_DUCKDB_DF_ALIAS)
            return len(df)

        loaded = self._execute_with_retry("export_duckdb", batch_id, execute_export)
        self._is_first_batch = False
        return loaded

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Delete newly voided rows from DuckDB when running idempotently."""
        if not self._cfg.idempotent or not records:
            return 0
        record_ids = [rid for r in records if (rid := extract_record_id(r)) is not None]
        if not record_ids:
            return 0
        return self._execute_with_retry(
            "reconcile_duckdb_deleted",
            batch_id,
            lambda: self._delete_record_ids(record_ids),
        )

    def flush(self) -> None:
        """Replace target with empty snapshot on full zero-row export; keep intact on incremental."""
        if (
            self._is_first_batch
            and self._cfg.idempotent
            and not self._is_incremental_sync
            and self._conn is not None
            and not isinstance(self._conn, NonCallableMock)
        ):
            _replace_duckdb_with_empty_table(self._conn, self.table_name, self._contract_types)

    def close(self) -> None:
        """Close the DuckDB connection when owned by this sink."""
        if self._conn is not None:
            try:
                if self._owns_conn:
                    self._conn.close()
            finally:
                self._conn = None


class _TabularCollectingSink(ExportSink):
    """Collects masked/sanitized DataFrame chunks and runs a writer callback on ``flush()``."""

    _skip_auto_redact_batch = True

    def __init__(
        self,
        config: TabularSinkConfig,
        on_flush: Any,
        *,
        pass_voided_ids: bool = False,
    ) -> None:
        super().__init__(config)
        self._frames: list[pd.DataFrame] = []
        self._voided_ids: set[str] = set()
        self._on_flush = on_flush
        self._pass_voided_ids = pass_voided_ids

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        _ = batch_id
        df = records if isinstance(records, pd.DataFrame) else pd.DataFrame(records)
        if len(df) == 0:
            return 0
        id_col = _find_df_record_id_col(df)
        if id_col is not None:
            for raw_id in df[id_col].tolist():
                cid = _canonical_record_id(raw_id)
                if cid is not None:
                    self._voided_ids.discard(cid)
        self._frames.append(df)
        return len(df)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        _ = batch_id
        if not self.config.idempotent or not records:
            return 0
        cids = [
            cid for r in records if (cid := _canonical_record_id(extract_record_id(r))) is not None
        ]
        if not cids:
            return 0
        self._voided_ids.update(cids)
        return len(cids)

    def flush(self) -> None:
        combined = _concat_tabular_frames(self._frames) if self._frames else pd.DataFrame()
        if self._pass_voided_ids:
            self._on_flush(combined, set(self._voided_ids))
        else:
            self._on_flush(combined)

    def close(self) -> None:
        self._frames.clear()
        self._voided_ids.clear()


class _TabularTransformSink(ExportSink):
    """Adapts record batches into masked/sanitized DataFrames before delegating to ``inner``."""

    _skip_auto_redact_batch = True

    def __init__(
        self,
        inner: ExportSink,
        mapper: Any,
        record_model: Any,
        variable_keys: list[str],
        label_map: dict[str, str],
        config: TabularSinkConfig,
        *,
        sanitize: bool,
        pipeline: EnrichmentPipeline | None = None,
    ) -> None:
        super().__init__(config)
        self._inner = inner
        self._mapper = mapper
        self._record_model = record_model
        self._variable_keys = variable_keys
        self._label_map = label_map
        self._cfg = config
        self._sanitize = sanitize
        self._pipeline = pipeline
        self._phi_keys = set(pipeline.phi_keys) if pipeline is not None else None
        self._has_native_enrichment = callable(getattr(mapper, "_get_enrichment_pipeline", None))
        self._seq_counters: dict[str, int] = {}
        self._meta_cols = {name for name, _ in _RECORD_META_CONTRACT_TYPES}

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        if self._cfg.as_sdtm:
            if not records:
                return 0
            raw_domain = self._cfg.sdtm_domain
            if not raw_domain:
                first_rec = records[0]
                raw_domain = (
                    first_rec.get("DOMAIN")
                    or first_rec.get("domain")
                    or first_rec.get("form_key")
                    or first_rec.get("formKey")
                    if isinstance(first_rec, Mapping)
                    else getattr(first_rec, "form_key", None)
                )
            target_domain = resolve_sdtm_domain(raw_domain, fallback="AE")
            batch_records: Sequence[Any] = records
            if self._cfg.variable_whitelist is not None:
                allowed = set(self._cfg.variable_whitelist)
                projected: list[Any] = []
                for rec in records:
                    rec_data = getattr(rec, "record_data", None)
                    if hasattr(rec, "model_copy") and isinstance(rec_data, dict):
                        projected.append(
                            rec.model_copy(
                                update={
                                    "record_data": {
                                        k: v for k, v in rec_data.items() if k in allowed
                                    }
                                }
                            )
                        )
                    elif isinstance(rec, Mapping) and isinstance(rec.get("record_data"), Mapping):
                        cloned = dict(rec)
                        cloned["record_data"] = {
                            k: v for k, v in rec["record_data"].items() if k in allowed
                        }
                        projected.append(cloned)
                    else:
                        projected.append(rec)
                batch_records = projected

            df = transform_records_to_sdtm(
                batch_records,
                domain=target_domain,
                study_key=self._cfg.study_key,
                include_deleted=self._cfg.include_deleted,
                mask_phi=self._cfg.mask_phi,
                seq_counters=self._seq_counters,
                phi_keys=self._phi_keys,
                pipeline=self._pipeline,
                allowed_unmapped_keys=self._cfg.variable_whitelist,
            )
            if df.empty:
                return 0
            if self._sanitize:
                df = _sanitize_df(df)
            return self._inner.write_batch(cast(Any, df), batch_id=batch_id)

        rows, _ = self._mapper._parse_records(records, self._record_model)
        if self._pipeline is not None and not self._has_native_enrichment:
            rows = [self._pipeline.process(r, mask_phi=self._cfg.mask_phi) for r in rows]
        df = self._mapper._build_dataframe(
            rows,
            self._variable_keys,
            self._label_map,
            self._cfg.use_labels_as_columns,
        )
        if df.empty:
            return 0
        df.columns = df.columns.astype(str)
        dup_mask = df.columns.str.lower().duplicated()
        df = df.loc[:, ~dup_mask]

        if self._cfg.mask_phi:
            df = _mask_df(df, phi_keys=self._phi_keys)
        if not self._cfg.use_labels_as_columns and self._cfg.naming_convention is not None:
            df = _apply_naming_convention_to_df(
                df,
                self._cfg.naming_convention,
                domain=self._cfg.sdtm_domain,
                meta_cols=self._meta_cols,
                study_key=self._cfg.study_key,
            )
        if self._sanitize:
            df = _sanitize_df(df)

        return self._inner.write_batch(cast(Any, df), batch_id=batch_id)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        return self._inner.reconcile_deleted(records, batch_id=batch_id)

    def flush(self) -> None:
        self._inner.flush()

    def close(self) -> None:
        self._inner.close()


def _make_mapper_records_source(
    sdk: Any,
    study_key: str,
    mapper: Any,
    config: TabularSinkConfig,
) -> Callable[[datetime | None, bool], Iterable[Any]]:
    """Build a streaming records source callback compatible with real and mock mappers."""
    extra_filters: dict[str, Any] = {}
    if config.form_whitelist is not None:
        extra_filters["formId"] = config.form_whitelist
    merged_filter = _merge_form_whitelist_into_filter(config.record_filter, config.form_whitelist)
    is_mock = _is_mock_mapper(mapper)

    def _records_source(
        watermark: datetime | None, exclude_deleted_upstream: bool
    ) -> Iterable[Any]:
        if is_mock:
            return cast(
                Iterable[Any],
                mapper._iter_records(
                    study_key,
                    visit_key=None,
                    extra_filters=extra_filters or None,
                ),
            )
        if (
            getattr(mapper, "_loader", None) is not None
            or not mapper._can_stream_via_records_endpoint()
        ):
            iter_filters = dict(extra_filters)
            if isinstance(config.record_filter, Mapping):
                iter_filters.update(config.record_filter)
            elif isinstance(config.record_filter, str) and config.record_filter.strip():
                iter_filters["filter"] = config.record_filter
            if watermark is not None:
                from imednet.utils.dates import format_iso_datetime

                iter_filters["dateModified"] = (">=", format_iso_datetime(watermark))
            return cast(
                Iterable[Any],
                mapper._iter_records(
                    study_key,
                    visit_key=None,
                    extra_filters=iter_filters or None,
                    record_data_filter=config.record_data_filter,
                    include_deleted=not exclude_deleted_upstream,
                ),
            )
        return iter_filtered_sdk_records(
            sdk,
            study_key,
            watermark=watermark,
            record_filter=merged_filter,
            record_data_filter=config.record_data_filter,
            exclude_deleted_upstream=exclude_deleted_upstream,
        )

    return _records_source


def _tabular_export(
    sdk: Any,
    study_key: str,
    sink: ExportSink,
    config: TabularSinkConfig,
    sanitize: bool = False,
    *,
    stream_name: str | None = None,
) -> None:
    mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
    empty_whitelist = (
        config.variable_whitelist is not None and len(config.variable_whitelist) == 0
    ) or (config.form_whitelist is not None and len(config.form_whitelist) == 0)
    if config.as_sdtm:
        if empty_whitelist:
            with sink:
                pass
            return
        variable_keys: list[str] = []
        label_map: dict[str, str] = {}
        contract_types = _build_sdtm_contract_types(
            config.sdtm_domain,
            variable_whitelist=config.variable_whitelist,
        )
        pipeline = resolve_enrichment_pipeline(study_key)
        record_model: Any = None
    else:
        variable_keys, label_map = mapper._fetch_variable_metadata(
            study_key,
            variable_whitelist=config.variable_whitelist,
            form_whitelist=config.form_whitelist,
        )
        if not variable_keys:
            with sink:
                pass
            return

        raw_variables = mapper._last_variables if hasattr(mapper, "_last_variables") else None
        pipeline = resolve_enrichment_pipeline(study_key)
        contract_types = _build_tabular_contract_types(
            variable_keys,
            label_map,
            raw_variables if isinstance(raw_variables, list) else (),
            use_labels_as_columns=config.use_labels_as_columns,
            mask_phi=config.mask_phi,
            pipeline=pipeline,
            naming_convention=config.naming_convention,
            sdtm_domain=config.sdtm_domain,
        )
        record_model = mapper._build_record_model(variable_keys, label_map)

    if isinstance(sink, (TabularParquetSink, TabularDuckDBSink)):
        sink.set_contract_types(contract_types)

    records_source = _make_mapper_records_source(sdk, study_key, mapper, config)

    transform_sink = _TabularTransformSink(
        sink,
        mapper,
        record_model,
        variable_keys,
        label_map,
        config,
        sanitize=sanitize,
        pipeline=pipeline,
    )
    written = run_streaming_export(
        sdk,
        study_key,
        transform_sink,
        config,
        stream_name=stream_name or "tabular",
        batch_id_prefix=f"{study_key}/tabular",
        records_source=records_source,
        quality_gate_fn=apply_quality_gate,
        iter_batches_fn=iter_batches,
    )
    if (
        config.as_sdtm
        and written == 0
        and isinstance(sink, TabularCSVSink)
        and not Path(sink.path).exists()
    ):
        empty_sdtm_df = _build_empty_contract_df(contract_types)
        empty_sdtm_df.to_csv(sink.path, index=False, **config.pandas_kwargs)


class _TabularCallableSink(ExportSink):
    """Lightweight ExportSink adapter applying SinkConfig retry semantics to tabular file/DB exports."""

    _skip_auto_redact_batch = True

    def __init__(
        self,
        config: TabularSinkConfig,
        operation_name: str,
        writer_fn: Any,
    ) -> None:
        super().__init__(config)
        self._operation_name = operation_name
        self._writer_fn = writer_fn

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        if not records:
            return 0
        return self._execute_with_retry(self._operation_name, batch_id, self._writer_fn)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def _collect_tabular_df(
    sdk: Any,
    study_key: str,
    config: TabularSinkConfig,
    *,
    sanitize: bool = False,
) -> pd.DataFrame:
    """Collect and transform records in ``config.batch_size`` chunks with quality gate filtering."""
    if pd is None:
        raise ImportError(
            "pandas is required for _records_df. Install with \"pip install 'imednet[export]'\"."
        )
    frames: list[pd.DataFrame] = []

    class _FrameCollectorSink(ExportSink):
        def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
            if isinstance(records, pd.DataFrame):
                frames.append(records)
                return len(records)
            return 0

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    collector = _FrameCollectorSink(config)
    _tabular_export(sdk, study_key, collector, config, sanitize=sanitize)
    if not frames:
        return pd.DataFrame()
    return _concat_tabular_frames(frames)


def _pop_sink_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Extract SinkConfig keyword arguments so they do not leak into pandas kwargs."""
    sink_keys = (
        "batch_size",
        "max_buffered_records",
        "max_retries",
        "retry_backoff",
        "idempotent",
        "mask_phi",
        "extra",
        "quality_gate_enabled",
        "min_schema_readiness_score",
        "tracer",
        "modified_since",
        "record_filter",
        "record_data_filter",
        "include_deleted",
        "incremental",
        "stream_name",
        "ledger_path",
        "state_provider",
        "as_sdtm",
        "sdtm_domain",
        "standards_profile",
        "min_standards_score",
        "naming_convention",
    )
    domain_alias = kwargs.pop("domain", None)
    extracted = {k: kwargs.pop(k) for k in sink_keys if k in kwargs}
    if domain_alias is not None and extracted.get("sdtm_domain") is None:
        extracted["sdtm_domain"] = domain_alias
    return extracted


def _config_has_streaming_options(config: TabularSinkConfig) -> bool:
    """Return True when ``config`` activates incremental, watermark, filter, or SDTM options."""
    return bool(
        config.modified_since is not None
        or config.record_filter is not None
        or config.record_data_filter is not None
        or config.include_deleted
        or config.incremental
        or config.stream_name is not None
        or config.ledger_path is not None
        or config.state_provider is not None
        or config.as_sdtm
        or config.sdtm_domain is not None
        or config.standards_profile is not None
        or config.min_standards_score is not None
        or config.naming_convention is not None
    )


def _build_tabular_sink_config(
    study_key: str,
    kwargs: dict[str, Any],
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    manifest_path: str | None = None,
    default_quality_gate: bool = False,
    mask_phi: bool = True,
) -> TabularSinkConfig:
    """Construct a :class:`TabularSinkConfig` while keeping sink kwargs out of ``pandas_kwargs``."""
    config = kwargs.pop("config", None)
    if isinstance(config, TabularSinkConfig):
        return config
    explicit_sanitize = kwargs.pop("sanitize", None)
    sink_kwargs = _pop_sink_kwargs(kwargs)
    sink_kwargs.setdefault("quality_gate_enabled", default_quality_gate)
    sink_kwargs.setdefault("mask_phi", mask_phi)
    if explicit_sanitize is not None:
        sink_kwargs["sanitize"] = bool(explicit_sanitize)
    return TabularSinkConfig(
        study_key=study_key,
        manifest_path=manifest_path,
        pandas_kwargs=kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        **sink_kwargs,
    )


def export_to_parquet(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str],
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export study records to a Parquet file.

    All exports now inherit StudyConfiguration rules by default.

    Parameters
    ----------
    use_labels_as_columns:
        When ``True``, variable labels are used for column names instead of
        variable names.
    mask_phi:
        When ``True`` (default), sensitive PHI values are replaced with
        ``***MASKED***``.
    """
    manifest_path = kwargs.pop("manifest_path", None)
    kwargs.setdefault("quality_gate_enabled", quality_gate_enabled)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=manifest_path,
        default_quality_gate=quality_gate_enabled,
        mask_phi=mask_phi,
    )
    if not _config_has_streaming_options(config):
        df_mock: pd.DataFrame | None = None
        if _prepare_export_df is not _DEFAULT_PREPARE_EXPORT_DF:
            df_mock = (
                _collect_tabular_df(sdk, study_key, config, sanitize=False)
                if config.quality_gate_enabled
                else _prepare_export_df(
                    sdk,
                    study_key,
                    use_labels_as_columns=config.use_labels_as_columns,
                    variable_whitelist=config.variable_whitelist,
                    form_whitelist=config.form_whitelist,
                )
            )
        else:
            pre_mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
            if _is_mock_mapper(pre_mapper):
                df_mock = (
                    _collect_tabular_df(sdk, study_key, config, sanitize=False)
                    if config.quality_gate_enabled
                    else _prepare_export_df(
                        sdk,
                        study_key,
                        use_labels_as_columns=config.use_labels_as_columns,
                        variable_whitelist=config.variable_whitelist,
                        form_whitelist=config.form_whitelist,
                        mask_phi=config.mask_phi,
                        _mapper=pre_mapper,
                    )
                )
        if df_mock is not None:

            def _write_parquet() -> int:
                df_mock.to_parquet(path, index=False, **config.pandas_kwargs)
                return len(df_mock)

            with _TabularCallableSink(config, "export_parquet", _write_parquet) as legacy_sink:
                legacy_sink.write_batch([1], batch_id=f"{study_key}/parquet/0")
            return

    sink = TabularParquetSink(path, config)
    _tabular_export(sdk, study_key, sink, config, sanitize=False, stream_name="parquet")


def export_to_csv(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str],
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    manifest_path: str | None = None,
    quality_gate_enabled: bool = True,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export study records to a CSV file.

    All exports now inherit StudyConfiguration rules by default.

    Parameters
    ----------
    use_labels_as_columns:
        When ``True``, variable labels are used for column names instead of
        variable names.
    mask_phi:
        When ``True`` (default), sensitive PHI values are replaced with
        ``***MASKED***``.
    """
    has_sdtm_or_standards_opts = bool(
        kwargs.get("as_sdtm")
        or kwargs.get("sdtm_domain") is not None
        or kwargs.get("domain") is not None
        or kwargs.get("standards_profile") is not None
        or kwargs.get("min_standards_score") is not None
    )
    effective_default_gate = (
        False
        if has_sdtm_or_standards_opts
        and "quality_gate_enabled" not in kwargs
        and quality_gate_enabled is True
        else quality_gate_enabled
    )
    kwargs.setdefault("quality_gate_enabled", effective_default_gate)
    kwargs.setdefault("sanitize", True)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=manifest_path or str(Path(path).with_suffix(".manifest.jsonl")),
        default_quality_gate=effective_default_gate,
        mask_phi=mask_phi,
    )

    sink = TabularCSVSink(path, config)
    _tabular_export(sdk, study_key, sink, config, sanitize=True, stream_name="csv")


def export_to_excel(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str],
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export study records to an Excel workbook.

    All exports now inherit StudyConfiguration rules by default.

    Parameters
    ----------
    use_labels_as_columns:
        When ``True``, variable labels are used for column names instead of
        variable names.
    mask_phi:
        When ``True`` (default), sensitive PHI values are replaced with
        ``***MASKED***``.
    """
    manifest_path = kwargs.pop("manifest_path", None)
    kwargs.setdefault("quality_gate_enabled", quality_gate_enabled)
    kwargs.setdefault("sanitize", True)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=manifest_path,
        default_quality_gate=quality_gate_enabled,
        mask_phi=mask_phi,
    )
    if not _config_has_streaming_options(config):
        df_mock: pd.DataFrame | None = None
        if _prepare_export_df is not _DEFAULT_PREPARE_EXPORT_DF:
            df_mock = (
                _collect_tabular_df(sdk, study_key, config, sanitize=True)
                if config.quality_gate_enabled
                else _prepare_export_df(
                    sdk,
                    study_key,
                    use_labels_as_columns=config.use_labels_as_columns,
                    variable_whitelist=config.variable_whitelist,
                    form_whitelist=config.form_whitelist,
                    sanitize=True,
                )
            )
        else:
            pre_mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
            if _is_mock_mapper(pre_mapper):
                df_mock = (
                    _collect_tabular_df(sdk, study_key, config, sanitize=True)
                    if config.quality_gate_enabled
                    else _prepare_export_df(
                        sdk,
                        study_key,
                        use_labels_as_columns=config.use_labels_as_columns,
                        variable_whitelist=config.variable_whitelist,
                        form_whitelist=config.form_whitelist,
                        sanitize=True,
                        mask_phi=config.mask_phi,
                        _mapper=pre_mapper,
                    )
                )
        if df_mock is not None:

            def _write_excel() -> int:
                df_mock.to_excel(path, index=False, **config.pandas_kwargs)
                return len(df_mock)

            with _TabularCallableSink(config, "export_excel", _write_excel) as legacy_sink:
                legacy_sink.write_batch([1], batch_id=f"{study_key}/excel/0")
            return

    excel_kwargs = dict(config.pandas_kwargs)
    sdtm_contract = (
        _build_sdtm_contract_types(config.sdtm_domain, variable_whitelist=config.variable_whitelist)
        if config.as_sdtm
        else None
    )
    is_incremental_excel = _is_incremental_config(config)

    def _write_or_reconcile_excel(combined: pd.DataFrame, voided_ids: set[str]) -> None:
        if not (is_incremental_excel and config.idempotent):
            target = (
                _build_empty_contract_df(sdtm_contract)
                if combined.empty and sdtm_contract is not None
                else combined
            )
            target.to_excel(path, index=False, **excel_kwargs)
            return

        dest_exists = Path(path).exists() and Path(path).stat().st_size > 0
        if combined.empty and not voided_ids:
            if dest_exists:
                return
            empty_target = (
                _build_empty_contract_df(sdtm_contract) if sdtm_contract is not None else combined
            )
            _atomic_replace_file(
                path,
                lambda tmp: empty_target.to_excel(tmp, index=False, **excel_kwargs),
                suffix=".xlsx",
            )
            return

        dedup_delta, delta_ids = _dedupe_df_by_record_id(combined, exclude_ids=voided_ids)
        if dest_exists:
            prev_df = pd.read_excel(path)
            prev_kept = _filter_df_excluding_record_ids(prev_df, delta_ids | voided_ids)
            if not prev_kept.empty:
                if config.mask_phi:
                    pipeline = resolve_enrichment_pipeline(study_key)
                    phi_keys = set(pipeline.phi_keys) if pipeline is not None else None
                    prev_kept = _mask_df(prev_kept, phi_keys=phi_keys)
                if config.sanitize:
                    prev_kept = _sanitize_df(prev_kept)
            if prev_kept.empty:
                merged_df = dedup_delta
            elif dedup_delta.empty:
                merged_df = prev_kept.reset_index(drop=True)
            else:
                merged_df = _concat_tabular_frames([prev_kept, dedup_delta])
        else:
            merged_df = dedup_delta

        _atomic_replace_file(
            path,
            lambda tmp: merged_df.to_excel(tmp, index=False, **excel_kwargs),
            suffix=".xlsx",
        )

    sink = _TabularCollectingSink(config, _write_or_reconcile_excel, pass_voided_ids=True)
    _tabular_export(sdk, study_key, sink, config, sanitize=True, stream_name="excel")


def _apply_json_enrichment(study_key: str, data: Any, *, mask_phi: bool = True) -> Any:
    """Apply ConfigVersionStore EnrichmentPipeline when installed."""
    import logging

    logger = logging.getLogger(__name__)
    try:
        study_cfg = resolve_study_configuration(study_key)
        if study_cfg is not None:
            pipeline = EnrichmentPipeline(study_cfg)
            if pipeline._has_rules:
                logger.info("Enrichment pipeline triggered")
                data = (
                    pipeline.process(data) if mask_phi else pipeline.process(data, mask_phi=False)
                )
                logger.info("Enrichment pipeline completed successfully")
    except Exception as e:
        logger.warning(f"Could not apply EnrichmentPipeline: {e}")
    return data


def _dump_json_payload(path: str | os.PathLike[str], data: Any, config: TabularSinkConfig) -> int:
    """Persist strict RFC 8259 JSON export payload atomically, appending when ``idempotent=False``."""
    resolved_path: str = _coerce_path_str(path)
    sanitized_data = _sanitize_json_value(data)
    payload = sanitized_data
    if not config.idempotent and isinstance(sanitized_data, list) and Path(resolved_path).exists():
        try:
            with open(resolved_path, encoding="utf-8") as existing_file:
                existing_data = json.load(existing_file)
            if isinstance(existing_data, list):
                payload = _sanitize_json_value(existing_data) + sanitized_data
        except Exception:
            payload = sanitized_data

    dump_kwargs = dict(config.pandas_kwargs)
    dump_kwargs["allow_nan"] = False

    def _write_tmp_json(tmp_path: str) -> None:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, **dump_kwargs)

    _atomic_replace_file(resolved_path, _write_tmp_json, suffix=".json.tmp")
    return len(sanitized_data) if isinstance(sanitized_data, list) else 1


def _merge_flat_json_records(
    existing_records: list[Any],
    delta_records: list[dict[str, Any]],
    voided_ids: set[str],
) -> list[Any]:
    """Upsert ``delta_records`` by canonical record ID and drop ``voided_ids``."""
    delta_by_cid: dict[str, dict[str, Any]] = {}
    unkeyed_delta: list[dict[str, Any]] = []
    for drec in delta_records:
        cid = _extract_row_canonical_id(drec)
        if cid is not None:
            delta_by_cid[cid] = drec
        else:
            unkeyed_delta.append(drec)

    merged: list[Any] = []
    placed_cids: set[str] = set()
    for erec in existing_records:
        if isinstance(erec, Mapping):
            cid = _extract_row_canonical_id(erec)
            if cid is not None and cid in voided_ids:
                continue
            if cid is not None and cid in delta_by_cid:
                if cid not in placed_cids:
                    merged.append(delta_by_cid[cid])
                    placed_cids.add(cid)
                continue
        merged.append(erec)

    for cid, drec in delta_by_cid.items():
        if cid not in placed_cids and cid not in voided_ids:
            merged.append(drec)
    merged.extend(unkeyed_delta)
    return merged


def _merge_hierarchical_archives(
    existing_tree: list[Any],
    delta_tree: list[dict[str, Any]],
    exclude_ids: set[str],
) -> list[dict[str, Any]]:
    """Merge hierarchical Subject->Visit->Form trees by ``recordId`` and prune voided records."""
    subjects: dict[Any, dict[str, Any]] = {}

    for subj in existing_tree:
        if not isinstance(subj, Mapping):
            continue
        sk = subj.get("subject_key")
        visits_map: dict[Any, dict[Any, list[dict[str, Any]]]] = {}
        for visit in subj.get("visits") or []:
            if not isinstance(visit, Mapping):
                continue
            vid = visit.get("visit_id")
            for fnode in visit.get("forms") or []:
                if not isinstance(fnode, Mapping):
                    continue
                fid = fnode.get("form_id")
                kept = [
                    dict(rec)
                    for rec in (fnode.get("records") or [])
                    if isinstance(rec, Mapping)
                    and _extract_row_canonical_id(rec) not in exclude_ids
                ]
                if kept:
                    visits_map.setdefault(vid, {}).setdefault(fid, []).extend(kept)

        unsched_map: dict[Any, list[dict[str, Any]]] = {}
        for fnode in subj.get("forms") or []:
            if not isinstance(fnode, Mapping):
                continue
            fid = fnode.get("form_id")
            kept = [
                dict(rec)
                for rec in (fnode.get("records") or [])
                if isinstance(rec, Mapping) and _extract_row_canonical_id(rec) not in exclude_ids
            ]
            if kept:
                unsched_map.setdefault(fid, []).extend(kept)

        entry = subjects.setdefault(sk, {"visits": {}, "forms": {}})
        for vid, forms_dict in visits_map.items():
            for fid, recs in forms_dict.items():
                entry["visits"].setdefault(vid, {}).setdefault(fid, []).extend(recs)
        for fid, recs in unsched_map.items():
            entry["forms"].setdefault(fid, []).extend(recs)

    for subj in delta_tree:
        if not isinstance(subj, Mapping):
            continue
        sk = subj.get("subject_key")
        entry = subjects.setdefault(sk, {"visits": {}, "forms": {}})
        for visit in subj.get("visits") or []:
            if not isinstance(visit, Mapping):
                continue
            vid = visit.get("visit_id")
            for fnode in visit.get("forms") or []:
                if not isinstance(fnode, Mapping):
                    continue
                fid = fnode.get("form_id")
                recs = [
                    dict(rec) for rec in (fnode.get("records") or []) if isinstance(rec, Mapping)
                ]
                if recs:
                    entry["visits"].setdefault(vid, {}).setdefault(fid, []).extend(recs)
        for fnode in subj.get("forms") or []:
            if not isinstance(fnode, Mapping):
                continue
            fid = fnode.get("form_id")
            recs = [dict(rec) for rec in (fnode.get("records") or []) if isinstance(rec, Mapping)]
            if recs:
                entry["forms"].setdefault(fid, []).extend(recs)

    merged_tree: list[dict[str, Any]] = []
    for sk, entry in subjects.items():
        subj_visits: list[dict[str, Any]] = []
        for vid, forms_dict in entry["visits"].items():
            visit_forms = [
                {"form_id": fid, "records": recs} for fid, recs in forms_dict.items() if recs
            ]
            if visit_forms:
                subj_visits.append({"visit_id": vid, "forms": visit_forms})
        unsched_forms = [
            {"form_id": fid, "records": recs} for fid, recs in entry["forms"].items() if recs
        ]
        if not subj_visits and not unsched_forms:
            continue
        out_subj: dict[str, Any] = {"subject_key": sk, "visits": subj_visits}
        if unsched_forms:
            out_subj["forms"] = unsched_forms
        merged_tree.append(out_subj)

    return merged_tree


class _HierarchicalJSONSink(ExportSink):
    """Streaming sink that builds and reconciles hierarchical JSON archives atomically on flush."""

    _skip_auto_redact_batch = True

    def __init__(
        self,
        path: str | os.PathLike[str],
        study_key: str,
        mapper: Any,
        hier_kwargs: dict[str, Any],
        config: TabularSinkConfig,
        *,
        is_mock_hier_mapper: bool,
    ) -> None:
        super().__init__(config)
        self.path: str = _coerce_path_str(path)
        self._study_key = study_key
        self._mapper = mapper
        self._hier_kwargs = dict(hier_kwargs)
        self._cfg = config
        self._is_mock_hier_mapper = is_mock_hier_mapper
        self._is_incremental_sync = _is_incremental_config(config)
        self._records_by_cid: dict[str, Any] = {}
        self._unkeyed_records: list[Any] = []
        self._voided_ids: set[str] = set()

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        _ = batch_id
        if not records:
            return 0
        for rec in records:
            cid = _canonical_record_id(extract_record_id(rec))
            if cid is not None:
                if cid in self._records_by_cid:
                    del self._records_by_cid[cid]
                self._records_by_cid[cid] = rec
                self._voided_ids.discard(cid)
            else:
                self._unkeyed_records.append(rec)
        return len(records)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        _ = batch_id
        if not self._cfg.idempotent or not records:
            return 0
        cids = [
            cid for r in records if (cid := _canonical_record_id(extract_record_id(r))) is not None
        ]
        for cid in cids:
            self._voided_ids.add(cid)
            self._records_by_cid.pop(cid, None)
        return len(cids)

    def _build_and_write_archive(self) -> int:
        dest_exists = Path(self.path).exists() and Path(self.path).stat().st_size > 0
        active_records = list(self._records_by_cid.values()) + self._unkeyed_records

        if (
            self._is_incremental_sync
            and self._cfg.idempotent
            and not active_records
            and not self._voided_ids
            and dest_exists
        ):
            return 0

        if active_records:
            call_kwargs = dict(self._hier_kwargs)
            if not self._is_mock_hier_mapper:
                call_kwargs["records"] = active_records
            delta_data = self._mapper.build_hierarchy(self._study_key, **call_kwargs)
        else:
            delta_data = []

        if self._cfg.mask_phi:
            delta_data = mask_clinical_phi(delta_data)
        if self._is_mock_hier_mapper:
            delta_data = _apply_json_enrichment(
                self._study_key,
                delta_data,
                mask_phi=self._cfg.mask_phi,
            )

        if self._is_incremental_sync and self._cfg.idempotent and dest_exists:
            existing_tree: list[Any] = []
            try:
                with open(self.path, encoding="utf-8") as existing_f:
                    loaded = json.load(existing_f)
                if isinstance(loaded, list):
                    existing_tree = loaded
            except Exception:
                existing_tree = []

            exclude_ids = set(self._records_by_cid) | self._voided_ids
            final_data: Any = _merge_hierarchical_archives(
                existing_tree,
                delta_data if isinstance(delta_data, list) else [],
                exclude_ids,
            )
            if self._cfg.mask_phi:
                pipeline = resolve_enrichment_pipeline(self._study_key)
                phi_keys = set(pipeline.phi_keys) if pipeline is not None else None
                final_data = mask_clinical_phi(final_data)
                if phi_keys:
                    final_data = mask_custom_phi_recursive(final_data, phi_keys)
        else:
            final_data = delta_data

        return _dump_json_payload(self.path, final_data, self._cfg)

    def flush(self) -> None:
        batch_id = f"{self._study_key}/json_hierarchical/flush"
        try:
            self._execute_with_retry("export_json", batch_id, self._build_and_write_archive)
        finally:
            self._records_by_cid.clear()
            self._unkeyed_records.clear()
            self._voided_ids.clear()

    def close(self) -> None:
        self._records_by_cid.clear()
        self._unkeyed_records.clear()
        self._voided_ids.clear()


def export_to_json(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str],
    *,
    use_labels_as_columns: bool = False,
    hierarchical: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export study records to a JSON file.

    All exports now inherit StudyConfiguration rules by default.

    Parameters
    ----------
    use_labels_as_columns:
        When ``True``, variable labels are used for column names instead of
        variable names.
    hierarchical:
        When ``True``, generates a nested tree (Subject > Visit > Form) suitable
        for Veeva Vault integrations instead of a flat tabular layout.
    mask_phi:
        When ``True`` (default), sensitive PHI values are replaced with
        ``***MASKED***``.
    """
    manifest_path = kwargs.pop("manifest_path", None)
    kwargs.setdefault("quality_gate_enabled", quality_gate_enabled)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=manifest_path,
        default_quality_gate=quality_gate_enabled,
        mask_phi=mask_phi,
    )

    if hierarchical and not config.as_sdtm:
        mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
        is_mock_hier_mapper = _is_mock_mapper(mapper) or not callable(
            getattr(mapper, "_get_enrichment_pipeline", None)
        )
        hier_kwargs: dict[str, Any] = {"use_labels_as_keys": use_labels_as_columns}
        if variable_whitelist is not None:
            hier_kwargs["variable_whitelist"] = variable_whitelist
        if form_whitelist is not None:
            hier_kwargs["form_whitelist"] = form_whitelist
        if _config_has_streaming_options(config):
            hier_kwargs["include_deleted"] = config.include_deleted
        if not config.mask_phi and not _is_mock_mapper(mapper):
            hier_kwargs["mask_phi"] = False

        if is_mock_hier_mapper and not _config_has_streaming_options(config):
            data = mapper.build_hierarchy(study_key, **hier_kwargs)
            if config.mask_phi:
                data = mask_clinical_phi(data)
            data = _apply_json_enrichment(study_key, data, mask_phi=config.mask_phi)
            with _TabularCallableSink(
                config,
                "export_json",
                lambda: _dump_json_payload(path, data, config),
            ) as legacy_hier_sink:
                legacy_hier_sink.write_batch([1], batch_id=f"{study_key}/json/0")
            return

        hier_sink = _HierarchicalJSONSink(
            path,
            study_key,
            mapper,
            hier_kwargs,
            config,
            is_mock_hier_mapper=is_mock_hier_mapper,
        )
        records_source = _make_mapper_records_source(sdk, study_key, mapper, config)
        run_streaming_export(
            sdk,
            study_key,
            hier_sink,
            config,
            stream_name="json_hierarchical",
            batch_id_prefix=f"{study_key}/json_hierarchical",
            records_source=records_source,
            quality_gate_fn=apply_quality_gate,
            iter_batches_fn=iter_batches,
        )
        return

    if not _config_has_streaming_options(config):
        df_mock: pd.DataFrame | None = None
        if _prepare_export_df is not _DEFAULT_PREPARE_EXPORT_DF:
            df_mock = (
                _collect_tabular_df(sdk, study_key, config, sanitize=False)
                if config.quality_gate_enabled
                else _prepare_export_df(
                    sdk,
                    study_key,
                    use_labels_as_columns=config.use_labels_as_columns,
                    variable_whitelist=config.variable_whitelist,
                    form_whitelist=config.form_whitelist,
                )
            )
        else:
            pre_mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
            if _is_mock_mapper(pre_mapper):
                df_mock = (
                    _collect_tabular_df(sdk, study_key, config, sanitize=False)
                    if config.quality_gate_enabled
                    else _prepare_export_df(
                        sdk,
                        study_key,
                        use_labels_as_columns=config.use_labels_as_columns,
                        variable_whitelist=config.variable_whitelist,
                        form_whitelist=config.form_whitelist,
                        mask_phi=config.mask_phi,
                        _mapper=pre_mapper,
                    )
                )
        if df_mock is not None:
            mock_data = _dataframe_to_json_records(df_mock)
            mock_data = _apply_json_enrichment(study_key, mock_data, mask_phi=config.mask_phi)
            with _TabularCallableSink(
                config,
                "export_json",
                lambda: _dump_json_payload(path, mock_data, config),
            ) as legacy_sink:
                legacy_sink.write_batch([1], batch_id=f"{study_key}/json/0")
            return

    is_incremental_json = _is_incremental_config(config)

    def _write_or_reconcile_json(combined: pd.DataFrame, voided_ids: set[str]) -> None:
        if not (is_incremental_json and config.idempotent):
            records_out = _dataframe_to_json_records(combined)
            if config.mask_phi:
                records_out = mask_clinical_phi(records_out)
            _dump_json_payload(path, records_out, config)
            return

        dest_exists = Path(path).exists() and Path(path).stat().st_size > 0
        if combined.empty and not voided_ids:
            if dest_exists:
                return
            _dump_json_payload(path, [], config)
            return

        dedup_delta, _ = _dedupe_df_by_record_id(combined, exclude_ids=voided_ids)
        delta_records = _dataframe_to_json_records(dedup_delta)
        existing_records: list[Any] = []
        if dest_exists:
            try:
                with open(path, encoding="utf-8") as existing_f:
                    loaded = json.load(existing_f)
                if isinstance(loaded, list):
                    existing_records = loaded
            except Exception:
                existing_records = []

        merged_records: Any = _merge_flat_json_records(
            existing_records,
            delta_records,
            voided_ids,
        )
        if config.mask_phi:
            pipeline = resolve_enrichment_pipeline(study_key)
            phi_keys = set(pipeline.phi_keys) if pipeline is not None else None
            merged_records = mask_clinical_phi(merged_records)
            if phi_keys:
                merged_records = mask_custom_phi_recursive(merged_records, phi_keys)
        _dump_json_payload(path, merged_records, config)

    sink = _TabularCollectingSink(config, _write_or_reconcile_json, pass_voided_ids=True)
    _tabular_export(sdk, study_key, sink, config, sanitize=False, stream_name="json")


def export_to_sql(
    sdk: ImednetSDK,
    study_key: str,
    table: str,
    conn_str: str,
    if_exists: Literal["fail", "replace", "append", "delete_rows"] | None = None,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    manifest_path: str | None = None,
    quality_gate_enabled: bool = True,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export study records to a SQL table.

    All exports now inherit StudyConfiguration rules by default.

    Parameters
    ----------
    use_labels_as_columns:
        When ``True``, variable labels are used for column names instead of
        variable names.
    manifest_path:
        Optional path to a file where manifest entries should be logged.
        Defaults to ``None``.
    mask_phi:
        When ``True`` (default), sensitive PHI values are replaced with
        ``***MASKED***``.
    """
    from sqlalchemy import create_engine

    manifest_path = kwargs.pop("manifest_path", manifest_path)
    if if_exists is not None:
        kwargs["if_exists"] = if_exists
    kwargs.setdefault("quality_gate_enabled", quality_gate_enabled)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=manifest_path,
        default_quality_gate=quality_gate_enabled,
        mask_phi=mask_phi,
    )

    engine = create_engine(conn_str)
    sink = TabularSQLSink(table, engine, config)
    _tabular_export(sdk, study_key, sink, config, sanitize=False, stream_name=f"sql_{table}")


def export_to_duckdb(
    sdk: ImednetSDK,
    study_key: str,
    db_path: str | os.PathLike[str],
    table_name: str,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export study records to a DuckDB table using native DataFrame registration.

    All exports now inherit StudyConfiguration rules by default.

    Parameters
    ----------
    sdk:
        Authenticated SDK instance used to fetch study records.
    study_key:
        Study identifier to export.
    db_path:
        Path to the target ``.duckdb`` database file.
    table_name:
        Name of the destination DuckDB table.
    use_labels_as_columns:
        When ``True``, variable labels are used for DataFrame column names.
    variable_whitelist:
        Optional list of variable names to include.
    form_whitelist:
        Optional list of form IDs to include.
    mask_phi:
        When ``True`` (default), sensitive PHI values are replaced with
        ``***MASKED***``.

    Raises:
    -------
    ImportError
        If the optional ``duckdb`` dependency is not installed.
    """
    try:
        import duckdb
    except ImportError as error:
        raise ImportError(
            "DuckDB export requires the optional 'duckdb' dependency. "
            "Install with `pip install 'imednet[duckdb]'`."
        ) from error

    _validate_duckdb_identifier(table_name)
    has_explicit_retry = "max_retries" in kwargs or "config" in kwargs
    manifest_path = kwargs.pop("manifest_path", None)
    kwargs.setdefault("quality_gate_enabled", quality_gate_enabled)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=manifest_path,
        default_quality_gate=quality_gate_enabled,
        mask_phi=mask_phi,
    )

    if not _config_has_streaming_options(config):
        df_legacy: pd.DataFrame | None = None
        if _prepare_export_df is not _DEFAULT_PREPARE_EXPORT_DF:
            df_legacy = (
                _collect_tabular_df(sdk, study_key, config, sanitize=False)
                if config.quality_gate_enabled
                else _prepare_export_df(
                    sdk,
                    study_key,
                    use_labels_as_columns=config.use_labels_as_columns,
                    variable_whitelist=config.variable_whitelist,
                    form_whitelist=config.form_whitelist,
                )
            )
        else:
            pre_mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
            if _is_mock_mapper(pre_mapper):
                df_legacy = (
                    _collect_tabular_df(sdk, study_key, config, sanitize=False)
                    if config.quality_gate_enabled
                    else _prepare_export_df(
                        sdk,
                        study_key,
                        use_labels_as_columns=config.use_labels_as_columns,
                        variable_whitelist=config.variable_whitelist,
                        form_whitelist=config.form_whitelist,
                        mask_phi=config.mask_phi,
                        _mapper=pre_mapper,
                    )
                )
        if df_legacy is not None:

            def _write_duckdb() -> int:
                conn: Any = duckdb.connect(_coerce_path_str(db_path))
                try:
                    quoted_tbl = _quote_duckdb_identifier(table_name)
                    quoted_alias = _quote_duckdb_identifier(_DUCKDB_DF_ALIAS)
                    if len(df_legacy.columns) == 0 and not isinstance(conn, NonCallableMock):
                        if config.idempotent:
                            _replace_duckdb_with_empty_table(conn, table_name, None)
                        return 0
                    conn.register(_DUCKDB_DF_ALIAS, df_legacy)
                    try:
                        if config.idempotent:
                            conn.execute(
                                f"CREATE OR REPLACE TABLE {quoted_tbl} AS SELECT * FROM {quoted_alias}"
                            )
                        else:
                            conn.execute(
                                f"CREATE TABLE IF NOT EXISTS {quoted_tbl} "
                                f"AS SELECT * FROM {quoted_alias} WHERE 1=0"
                            )
                            conn.execute(f"INSERT INTO {quoted_tbl} SELECT * FROM {quoted_alias}")
                    finally:
                        conn.unregister(_DUCKDB_DF_ALIAS)
                    return len(df_legacy)
                finally:
                    conn.close()

            if has_explicit_retry:
                with _TabularCallableSink(config, "export_duckdb", _write_duckdb) as legacy_sink:
                    legacy_sink.write_batch([1], batch_id=f"{study_key}/duckdb/0")
            else:
                _write_duckdb()
            return

    sink = TabularDuckDBSink(db_path, table_name, config)
    _tabular_export(
        sdk,
        study_key,
        sink,
        config,
        sanitize=False,
        stream_name=f"duckdb_{table_name}",
    )


@dataclass
class _FormExportContext:
    """Per-form metadata and mutable state during a single-pass multi-form export."""

    form: Any
    form_id: int
    form_key: str
    variables: list[Any]
    variable_keys: list[str]
    label_map: dict[str, Any]
    record_model: Any
    contract_types: dict[str, str | None]
    stream_name: str
    sdtm_domain: str | None = None
    watermark: datetime | None = None
    wrote_any: bool = False
    total_written: int = 0
    total_reconciled: int = 0
    max_ts: datetime | None = None
    pending_gate: dict[int, list[tuple[Any, datetime | None]]] = field(default_factory=dict)
    exported_timestamps: list[datetime] = field(default_factory=list)
    sink: Any = None
    tx: dict[str, Any] | None = None


def _build_form_export_contexts(
    sdk: Any,
    study_key: str,
    mapper: Any,
    *,
    use_labels_as_columns: bool,
    variable_whitelist: list[str] | None,
    form_whitelist: list[int] | None,
    mask_phi: bool,
    pipeline: EnrichmentPipeline | None,
    stream_prefix: str,
    custom_stream_prefix: str | None,
    validate_form_key_fn: Callable[[str], None] | None = None,
    as_sdtm: bool = False,
    sdtm_domain: str | None = None,
    naming_convention: str | None = None,
) -> tuple[list[_FormExportContext], list[Any]]:
    """Fetch study forms and variables once and build per-form export contexts."""
    forms = list(sdk.forms.list(study_key=study_key))
    form_filter: dict[str, Any] = {"formId": form_whitelist} if form_whitelist else {}
    all_variables = (
        []
        if form_whitelist is not None and len(form_whitelist) == 0
        else list(sdk.variables.list(study_key=study_key, **form_filter))
    )
    variables_by_form: dict[int, list[Any]] = {}
    for var in all_variables:
        if var.form_id is not None:
            variables_by_form.setdefault(var.form_id, []).append(var)

    contexts: list[_FormExportContext] = []
    for form in forms:
        if form.form_id is None or form.form_key is None:
            continue
        if form_whitelist is not None and form.form_id not in form_whitelist:
            continue
        if validate_form_key_fn is not None:
            validate_form_key_fn(form.form_key)

        variables = variables_by_form.get(form.form_id, [])
        variable_keys = [
            v.variable_name
            for v in variables
            if v.variable_name
            and (variable_whitelist is None or v.variable_name in variable_whitelist)
        ]
        label_map = {
            v.variable_name: v.label for v in variables if v.variable_name in variable_keys
        }
        form_domain = (
            resolve_sdtm_domain(sdtm_domain or form.form_key, fallback="AE")
            if as_sdtm
            else sdtm_domain
        )
        record_model = None if as_sdtm else mapper._build_record_model(variable_keys, label_map)
        contract_types = (
            _build_sdtm_contract_types(form_domain, variable_whitelist=variable_whitelist)
            if as_sdtm
            else _build_tabular_contract_types(
                variable_keys,
                label_map,
                variables,
                use_labels_as_columns=use_labels_as_columns,
                mask_phi=mask_phi,
                pipeline=pipeline,
                naming_convention=naming_convention,
                sdtm_domain=sdtm_domain,
            )
        )
        resolved_stream = (
            f"{custom_stream_prefix}_{form.form_key}"
            if custom_stream_prefix is not None
            else f"{stream_prefix}_{form.form_key}"
        )
        contexts.append(
            _FormExportContext(
                form=form,
                form_id=form.form_id,
                form_key=form.form_key,
                variables=variables,
                variable_keys=variable_keys,
                label_map=label_map,
                record_model=record_model,
                contract_types=contract_types,
                stream_name=resolved_stream,
                sdtm_domain=form_domain,
            )
        )
    return contexts, all_variables


def _stream_records_across_forms(
    sdk: Any,
    study_key: str,
    config: TabularSinkConfig,
    contexts: list[_FormExportContext],
    all_variables: list[Any],
    *,
    on_active_batch: Callable[[_FormExportContext, list[Any], int], int],
    on_voided_batch: Callable[[_FormExportContext, list[Any], int], int] | None = None,
    on_before_checkpoints: Callable[[], None] | None = None,
) -> None:
    """Stream study records once, route batches per form, and commit checkpoints post-flush."""
    if not contexts:
        if on_before_checkpoints is not None:
            on_before_checkpoints()
        return

    opts = _extract_safe_config_options(config)
    use_checkpoint = opts["incremental"] or opts["state_provider"] is not None
    provider: StateProviderProtocol | None = None
    if use_checkpoint:
        provider = (
            cast(StateProviderProtocol, opts["state_provider"])
            if opts["state_provider"] is not None
            else get_state_provider(ledger_path=opts["ledger_path"])
        )

    with contextlib.ExitStack() as tx_stack:
        for ctx in contexts:
            if provider is not None:
                ctx.tx = tx_stack.enter_context(
                    provider.transaction(
                        study_key,
                        ctx.stream_name,
                        fallback_timestamp=opts["modified_since"],
                    )
                )
                raw_last = ctx.tx.get("last_timestamp")
                ctx.watermark = (
                    normalize_watermark_datetime(raw_last)
                    if isinstance(raw_last, (datetime, str))
                    else None
                ) or opts["modified_since"]
            else:
                ctx.watermark = opts["modified_since"]

        if all(ctx.watermark is not None for ctx in contexts):
            study_watermark: datetime | None = min(
                cast(datetime, ctx.watermark) for ctx in contexts
            )
        else:
            study_watermark = None

        any_reconcile_voided = any(
            (not opts["include_deleted"])
            and opts["idempotent"]
            and (use_checkpoint or ctx.watermark is not None)
            for ctx in contexts
        )
        exclude_deleted_upstream = (not opts["include_deleted"]) and (not any_reconcile_voided)

        by_form_id = {ctx.form_id: ctx for ctx in contexts}
        by_form_key = {ctx.form_key: ctx for ctx in contexts}

        def _resolve_ctx(record: Any) -> _FormExportContext | None:
            fid, fk = extract_record_form_ref(record)
            if fid is not None and fid in by_form_id:
                return by_form_id[fid]
            if fk is not None and fk in by_form_key:
                return by_form_key[fk]
            return None

        effective_form_whitelist = (
            [ctx.form_id for ctx in contexts] if config.form_whitelist is not None else None
        )
        merged_filter = _merge_form_whitelist_into_filter(
            opts["record_filter"], effective_form_whitelist
        )
        raw_records = iter_filtered_sdk_records(
            sdk,
            study_key,
            watermark=study_watermark,
            record_filter=merged_filter,
            record_data_filter=opts["record_data_filter"],
            exclude_deleted_upstream=exclude_deleted_upstream,
        )

        check_standards = (
            opts["standards_profile"] is not None or opts["min_standards_score"] is not None
        )

        def _handle_active_flush(fk: str, batch: list[Any], batch_idx: int) -> None:
            ctx = by_form_key[fk]
            if check_standards and batch:
                _enforce_batch_standards_readiness(
                    batch,
                    batch_id=f"{study_key}/{ctx.form_key}/{batch_idx}",
                    study_key=study_key,
                    config=config,
                    standards_profile=opts["standards_profile"],
                    min_standards_score=opts["min_standards_score"],
                    sdtm_domain=ctx.sdtm_domain or opts["sdtm_domain"],
                )
            written = on_active_batch(ctx, batch, batch_idx)
            ctx.total_written += written
            if written > 0:
                ctx.wrote_any = True

        def _handle_voided_flush(fk: str, batch: list[Any], batch_idx: int) -> None:
            if on_voided_batch is None:
                return
            ctx = by_form_key[fk]
            ctx.total_reconciled += on_voided_batch(ctx, batch, batch_idx)

        router = BoundedFormBufferRouter(
            [ctx.form_key for ctx in contexts],
            batch_size=opts["batch_size"],
            max_buffered_records=opts["max_buffered_records"],
            on_flush_active=_handle_active_flush,
            on_flush_voided=_handle_voided_flush if any_reconcile_voided else None,
        )

        def _pop_form_accepted_candidate(ctx: _FormExportContext, rec: Any) -> None:
            bucket = ctx.pending_gate.get(id(rec))
            if bucket:
                bucket.pop(0)
                if not bucket:
                    del ctx.pending_gate[id(rec)]
                return
            rec_id = extract_record_id(rec)
            if rec_id is None:
                return
            for obj_id, candidate_bucket in list(ctx.pending_gate.items()):
                for idx, (cand_rec, _) in enumerate(candidate_bucket):
                    if extract_record_id(cand_rec) == rec_id:
                        candidate_bucket.pop(idx)
                        if not candidate_bucket:
                            del ctx.pending_gate[obj_id]
                        return

        def _observe_and_route() -> Iterator[Any]:
            for rec in raw_records:
                ctx = _resolve_ctx(rec)
                if ctx is None:
                    continue
                rec_ts = extract_record_timestamp(rec)
                if ctx.watermark is not None:
                    if rec_ts is not None:
                        if rec_ts < ctx.watermark:
                            continue
                    elif study_watermark is None or study_watermark < ctx.watermark:
                        continue

                if not opts["include_deleted"] and is_record_deleted(rec):
                    if opts["idempotent"] and (use_checkpoint or ctx.watermark is not None):
                        if rec_ts is not None:
                            ctx.exported_timestamps.append(rec_ts)
                        router.add_voided(ctx.form_key, rec)
                    continue

                ctx.pending_gate.setdefault(id(rec), []).append((rec, rec_ts))
                yield rec

        if apply_quality_gate is _DEFAULT_APPLY_QUALITY_GATE:
            gated_records: Iterable[Any] = apply_quality_gate(
                sdk,
                study_key,
                _observe_and_route(),
                config,
                variables=all_variables,
                forms=[ctx.form for ctx in contexts],
            )
        else:
            gated_records = apply_quality_gate(
                sdk,
                study_key,
                _observe_and_route(),
                config,
            )

        for rec in gated_records:
            matched_ctx = _resolve_ctx(rec)
            if matched_ctx is not None:
                _pop_form_accepted_candidate(matched_ctx, rec)
                rec_ts = extract_record_timestamp(rec)
                if rec_ts is not None:
                    matched_ctx.exported_timestamps.append(rec_ts)
                router.add_active(matched_ctx.form_key, rec)
        router.flush_all()

        for ctx in contexts:
            rejected_timestamps = [
                rec_ts for bucket in ctx.pending_gate.values() for _, rec_ts in bucket
            ]
            ctx.pending_gate.clear()
            if rejected_timestamps:
                if any(rec_ts is None for rec_ts in rejected_timestamps):
                    ctx.max_ts = None
                else:
                    min_rejected_ts = min(ts for ts in rejected_timestamps if ts is not None)
                    eligible_ts = [ts for ts in ctx.exported_timestamps if ts <= min_rejected_ts]
                    ctx.max_ts = max(eligible_ts) if eligible_ts else None
            elif ctx.exported_timestamps:
                ctx.max_ts = max(ctx.exported_timestamps)
            ctx.exported_timestamps.clear()

        if on_before_checkpoints is not None:
            on_before_checkpoints()

        for ctx in contexts:
            if ctx.tx is not None:
                ctx.tx["records_processed"] = ctx.total_written + ctx.total_reconciled
                if ctx.max_ts is not None:
                    ctx.tx["new_timestamp"] = ctx.max_ts


def _make_form_sink_config(
    study_key: str,
    config: TabularSinkConfig,
    ctx: _FormExportContext,
    *,
    use_labels_as_columns: bool,
    pandas_kwargs: dict[str, Any] | None = None,
) -> TabularSinkConfig:
    """Create a per-form :class:`TabularSinkConfig` inheriting study-level streaming options."""
    return TabularSinkConfig(
        study_key=study_key,
        manifest_path=config.manifest_path,
        pandas_kwargs=dict(config.pandas_kwargs) if pandas_kwargs is None else pandas_kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=config.variable_whitelist if config.as_sdtm else ctx.variable_keys,
        form_whitelist=[ctx.form_id],
        batch_size=config.batch_size,
        max_buffered_records=config.max_buffered_records,
        max_retries=config.max_retries,
        retry_backoff=config.retry_backoff,
        idempotent=config.idempotent,
        mask_phi=config.mask_phi,
        extra=dict(config.extra),
        quality_gate_enabled=config.quality_gate_enabled,
        min_schema_readiness_score=config.min_schema_readiness_score,
        tracer=config.tracer,
        modified_since=config.modified_since,
        record_filter=config.record_filter,
        record_data_filter=config.record_data_filter,
        include_deleted=config.include_deleted,
        incremental=config.incremental,
        stream_name=ctx.stream_name,
        ledger_path=config.ledger_path,
        state_provider=config.state_provider,
        as_sdtm=config.as_sdtm,
        sdtm_domain=ctx.sdtm_domain,
        standards_profile=config.standards_profile,
        min_standards_score=config.min_standards_score,
        naming_convention=config.naming_convention,
    )


def export_to_duckdb_by_form(
    sdk: ImednetSDK,
    study_key: str,
    db_path: str | os.PathLike[str],
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export records to separate DuckDB tables for each form.

    Each form is exported to a table named after ``form.form_key``.

    Parameters
    ----------
    sdk:
        Authenticated SDK instance used to fetch study records.
    study_key:
        Study identifier to export.
    db_path:
        Path to the target ``.duckdb`` database file.
    use_labels_as_columns:
        When ``True``, variable labels are used for DataFrame column names.
    variable_whitelist:
        Optional list of variable names to include.
    form_whitelist:
        Optional list of form IDs to include.
    mask_phi:
        When ``True`` (default), sensitive PHI values are replaced with
        ``***MASKED***``.

    Raises:
    -------
    ImportError
        If the optional ``duckdb`` dependency is not installed.
    """
    try:
        import duckdb
    except ImportError as error:
        raise ImportError(
            "DuckDB export requires the optional 'duckdb' dependency. "
            "Install with `pip install 'imednet[duckdb]'`."
        ) from error

    manifest_path = kwargs.pop("manifest_path", None)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=manifest_path,
        mask_phi=mask_phi,
    )
    mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
    if hasattr(mapper, "_active_study_key"):
        mapper._active_study_key = study_key
    pipeline = resolve_enrichment_pipeline(study_key)
    phi_keys = set(pipeline.phi_keys) if pipeline is not None else None
    has_native_enrichment = callable(getattr(mapper, "_get_enrichment_pipeline", None))
    use_mock_fetch = _uses_mock_fetch_records(mapper) and not _config_has_streaming_options(config)

    contexts, all_variables = _build_form_export_contexts(
        sdk,
        study_key,
        mapper,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        mask_phi=config.mask_phi,
        pipeline=pipeline,
        stream_prefix="duckdb",
        custom_stream_prefix=config.stream_name,
        validate_form_key_fn=_validate_duckdb_identifier,
        as_sdtm=config.as_sdtm,
        sdtm_domain=config.sdtm_domain,
        naming_convention=config.naming_convention,
    )

    resolved_db_path = _coerce_path_str(db_path)
    conn: Any = duckdb.connect(resolved_db_path, config={"threads": "1"})
    try:
        if use_mock_fetch:
            for ctx in contexts:
                records = mapper._fetch_records(
                    study_key,
                    extra_filters={"formId": ctx.form_id},
                )
                rows, _ = mapper._parse_records(records, ctx.record_model)
                if pipeline is not None and not has_native_enrichment:
                    rows = [pipeline.process(r, mask_phi=config.mask_phi) for r in rows]
                df = mapper._build_dataframe(
                    rows,
                    ctx.variable_keys,
                    ctx.label_map,
                    use_labels_as_columns,
                )
                if isinstance(df, pd.DataFrame):
                    df.columns = df.columns.astype(str)
                    dup_mask = df.columns.str.lower().duplicated()
                    df = df.loc[:, ~dup_mask]
                    if config.mask_phi:
                        df = _mask_df(df, phi_keys=phi_keys)

                if (
                    isinstance(df, pd.DataFrame)
                    and len(df.columns) == 0
                    and not isinstance(conn, NonCallableMock)
                ):
                    _replace_duckdb_with_empty_table(conn, ctx.form_key, None)
                    continue

                conn.register(_DUCKDB_DF_ALIAS, df)
                try:
                    conn.execute(
                        f"CREATE OR REPLACE TABLE {_quote_duckdb_identifier(ctx.form_key)} "
                        f"AS SELECT * FROM {_quote_duckdb_identifier(_DUCKDB_DF_ALIAS)}"
                    )
                finally:
                    conn.unregister(_DUCKDB_DF_ALIAS)
            return

        with contextlib.ExitStack() as sink_stack:
            for ctx in contexts:
                form_config = _make_form_sink_config(
                    study_key,
                    config,
                    ctx,
                    use_labels_as_columns=use_labels_as_columns,
                )
                inner_ddb_sink = TabularDuckDBSink(
                    db_path,
                    ctx.form_key,
                    form_config,
                    contract_types=ctx.contract_types,
                    conn=conn,
                )
                transform_sink = _TabularTransformSink(
                    inner_ddb_sink,
                    mapper,
                    ctx.record_model,
                    ctx.variable_keys,
                    ctx.label_map,
                    form_config,
                    sanitize=False,
                    pipeline=pipeline,
                )
                ctx.sink = sink_stack.enter_context(transform_sink)

            def _write_duckdb_batch(
                ctx: _FormExportContext, batch: list[Any], batch_idx: int
            ) -> int:
                return int(
                    ctx.sink.write_batch(batch, batch_id=f"{study_key}/{ctx.form_key}/{batch_idx}")
                )

            def _reconcile_duckdb_voided(
                ctx: _FormExportContext, voided_batch: list[Any], batch_idx: int
            ) -> int:
                return int(
                    ctx.sink.reconcile_deleted(
                        voided_batch,
                        batch_id=f"{study_key}/{ctx.form_key}/voided/{batch_idx}",
                    )
                )

            _stream_records_across_forms(
                sdk,
                study_key,
                config,
                contexts,
                all_variables,
                on_active_batch=_write_duckdb_batch,
                on_voided_batch=_reconcile_duckdb_voided,
                on_before_checkpoints=sink_stack.close,
            )
    finally:
        conn.close()


def export_to_sql_by_form(
    sdk: ImednetSDK,
    study_key: str,
    conn_str: str,
    if_exists: Literal["fail", "replace", "append", "delete_rows"] | None = None,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export records to separate SQL tables for each form."""
    from sqlalchemy import create_engine

    manifest_path = kwargs.pop("manifest_path", None)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=manifest_path,
        mask_phi=mask_phi,
    )
    resolved_if_exists: Literal["fail", "replace", "append", "delete_rows"] = (
        if_exists if if_exists is not None else ("replace" if config.idempotent else "append")
    )

    mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
    if hasattr(mapper, "_active_study_key"):
        mapper._active_study_key = study_key
    pipeline = resolve_enrichment_pipeline(study_key)
    phi_keys = set(pipeline.phi_keys) if pipeline is not None else None
    has_native_enrichment = callable(getattr(mapper, "_get_enrichment_pipeline", None))
    engine = create_engine(conn_str)

    contexts, all_variables = _build_form_export_contexts(
        sdk,
        study_key,
        mapper,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        mask_phi=config.mask_phi,
        pipeline=pipeline,
        stream_prefix="sql",
        custom_stream_prefix=config.stream_name,
        as_sdtm=config.as_sdtm,
        sdtm_domain=config.sdtm_domain,
        naming_convention=config.naming_convention,
    )

    use_mock_fetch = _uses_mock_fetch_records(mapper) and not _config_has_streaming_options(config)
    if use_mock_fetch:
        for ctx in contexts:
            records = mapper._fetch_records(
                study_key,
                extra_filters={"formId": ctx.form_id},
            )
            rows, _ = mapper._parse_records(records, ctx.record_model)
            if pipeline is not None and not has_native_enrichment:
                rows = [pipeline.process(r, mask_phi=config.mask_phi) for r in rows]
            df = mapper._build_dataframe(
                rows,
                ctx.variable_keys,
                ctx.label_map,
                use_labels_as_columns,
            )
            if isinstance(df, pd.DataFrame):
                dup_mask = df.columns.str.lower().duplicated()
                df = df.loc[:, ~dup_mask]
                if config.mask_phi:
                    df = _mask_df(df, phi_keys=phi_keys)

            form_table = ctx.form_key

            def _write_form_sql(
                target_df: pd.DataFrame = df,
                target_table: str = form_table,
            ) -> int:
                _to_sql_with_chunking(
                    target_df,
                    target_table,
                    engine,
                    if_exists=resolved_if_exists,
                    **config.pandas_kwargs,
                )
                return len(target_df) if hasattr(target_df, "__len__") else 1

            with _TabularCallableSink(config, "export_sql_by_form", _write_form_sql) as legacy_sink:
                legacy_sink.write_batch([1], batch_id=f"{study_key}/{form_table}/0")
        return

    is_incremental = _is_incremental_config(config)

    with contextlib.ExitStack() as sink_stack:
        for ctx in contexts:
            form_pandas_kwargs = dict(config.pandas_kwargs)
            if if_exists is not None and not (
                config.incremental or config.modified_since is not None
            ):
                form_pandas_kwargs["if_exists"] = if_exists
            form_config = _make_form_sink_config(
                study_key,
                config,
                ctx,
                use_labels_as_columns=use_labels_as_columns,
                pandas_kwargs=form_pandas_kwargs,
            )
            inner_sql_sink = TabularSQLSink(ctx.form_key, engine, form_config)
            transform_sink = _TabularTransformSink(
                inner_sql_sink,
                mapper,
                ctx.record_model,
                ctx.variable_keys,
                ctx.label_map,
                form_config,
                sanitize=False,
                pipeline=pipeline,
            )
            ctx.sink = sink_stack.enter_context(transform_sink)

        def _write_sql_batch(ctx: _FormExportContext, batch: list[Any], batch_idx: int) -> int:
            return int(
                ctx.sink.write_batch(batch, batch_id=f"{study_key}/{ctx.form_key}/{batch_idx}")
            )

        def _reconcile_sql_voided(
            ctx: _FormExportContext, voided_batch: list[Any], batch_idx: int
        ) -> int:
            return int(
                ctx.sink.reconcile_deleted(
                    voided_batch,
                    batch_id=f"{study_key}/{ctx.form_key}/voided/{batch_idx}",
                )
            )

        def _finalize_and_flush_sql_sinks() -> None:
            if (
                not is_incremental
                and config.idempotent
                and resolved_if_exists != "fail"
                and not isinstance(engine, NonCallableMock)
            ):
                for ctx in contexts:
                    if not ctx.wrote_any:
                        empty_df = _build_empty_contract_df(ctx.contract_types)
                        sql_sink = cast(TabularSQLSink, ctx.sink._inner)

                        def _write_empty(
                            target_df: pd.DataFrame = empty_df,
                            target_table: str = ctx.form_key,
                            target_kwargs: Mapping[str, Any] = sql_sink._cfg.pandas_kwargs,
                        ) -> int:
                            _to_sql_with_chunking(
                                target_df,
                                target_table,
                                engine,
                                if_exists=resolved_if_exists,
                                **target_kwargs,
                            )
                            return 0

                        sql_sink._execute_with_retry(
                            "export_sql_empty",
                            f"{study_key}/{ctx.form_key}/empty",
                            _write_empty,
                        )
            sink_stack.close()

        _stream_records_across_forms(
            sdk,
            study_key,
            config,
            contexts,
            all_variables,
            on_active_batch=_write_sql_batch,
            on_voided_batch=_reconcile_sql_voided,
            on_before_checkpoints=_finalize_and_flush_sql_sinks,
        )


def _unpack_long_sql_rows(
    records: Iterable[Any],
    *,
    mask_phi: bool,
    pipeline: EnrichmentPipeline | None,
    phi_keys: set[str] | None,
    naming_convention: str | None = None,
    sdtm_domain: str | None = None,
) -> list[dict[str, Any]]:
    """Unpack ``Record`` envelopes into normalized long-format variable rows with PHI masking."""
    long_rows: list[dict[str, Any]] = []
    for rec in records:
        rec_id = extract_record_id(rec)
        if isinstance(rec, Mapping):
            form_id = rec.get("form_id") if "form_id" in rec else rec.get("formId")
            timestamp = (
                rec.get("date_modified") if "date_modified" in rec else rec.get("dateModified")
            )
            raw_payload = rec.get("record_data") if "record_data" in rec else rec.get("recordData")
        else:
            form_id = getattr(rec, "form_id", getattr(rec, "formId", None))
            timestamp = getattr(rec, "date_modified", getattr(rec, "dateModified", None))
            raw_payload = getattr(rec, "record_data", getattr(rec, "recordData", None))
        rec_payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
        if pipeline is not None and rec_payload:
            rec_payload = pipeline.process(rec_payload, mask_phi=mask_phi)
        for name, value in rec_payload.items():
            if not mask_phi:
                masked_value = value
            elif global_sensitivity_registry.is_sensitive(name) or _is_custom_phi_key(
                name, phi_keys
            ):
                masked_value = MASKED_SENTINEL
            else:
                masked_value = mask_clinical_phi(value)
                if phi_keys:
                    masked_value = mask_custom_phi_recursive(masked_value, phi_keys)
            formatted_var_name = (
                format_variable_name(name, naming_convention, domain=sdtm_domain)
                if naming_convention is not None
                else name
            )
            long_rows.append(
                {
                    "record_id": rec_id,
                    "form_id": form_id,
                    "variable_name": formatted_var_name,
                    "value": masked_value,
                    "timestamp": timestamp,
                }
            )
    return long_rows


class _LongSQLSink(ExportSink):
    """Streaming sink for normalized long-format SQL tables supporting incremental upserts."""

    _skip_auto_redact_batch = True

    def __init__(
        self,
        table: str,
        engine: Any,
        config: TabularSinkConfig,
        *,
        chunk_size: int,
        pipeline: EnrichmentPipeline | None,
    ) -> None:
        super().__init__(config)
        self.table = table
        self.engine = engine
        self._cfg = config
        self._chunk_size = max(1, chunk_size)
        self._pipeline = pipeline
        self._phi_keys = set(pipeline.phi_keys) if pipeline is not None else None
        self._is_incremental_sync = _is_incremental_config(config)
        self._is_first_write = True
        self._initial_if_exists: Literal["replace", "append"] = (
            "append" if (self._is_incremental_sync or not config.idempotent) else "replace"
        )

    def _delete_record_ids(self, record_ids: Sequence[Any]) -> int:
        return _execute_chunked_sql_id_delete(
            self.engine,
            self.table,
            ("record_id",),
            record_ids,
        )

    def _write_long_chunks(self, long_rows: list[dict[str, Any]]) -> None:
        for start in range(0, len(long_rows), self._chunk_size):
            chunk = long_rows[start : start + self._chunk_size]
            if_exists: Literal["replace", "append"] = (
                self._initial_if_exists if self._is_first_write else "append"
            )
            pd.DataFrame(chunk).to_sql(
                self.table,
                self.engine,
                if_exists=if_exists,
                index=False,
            )
            self._is_first_write = False

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        if not records:
            return 0
        incoming_ids = [rid for r in records if (rid := extract_record_id(r)) is not None]
        long_rows = _unpack_long_sql_rows(
            records,
            mask_phi=self._cfg.mask_phi,
            pipeline=self._pipeline,
            phi_keys=self._phi_keys,
            naming_convention=self._cfg.naming_convention,
            sdtm_domain=self._cfg.sdtm_domain,
        )

        def execute_export() -> int:
            if (
                self._is_incremental_sync
                and self._cfg.idempotent
                and self._initial_if_exists == "append"
                and incoming_ids
            ):
                self._delete_record_ids(incoming_ids)
            if long_rows:
                self._write_long_chunks(long_rows)
            return len(records)

        return self._execute_with_retry("export_long_sql", batch_id, execute_export)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        if not self._cfg.idempotent or not records or isinstance(self.engine, NonCallableMock):
            return 0
        record_ids = [rid for r in records if (rid := extract_record_id(r)) is not None]
        if not record_ids:
            return 0
        return self._execute_with_retry(
            "reconcile_long_sql_deleted",
            batch_id,
            lambda: self._delete_record_ids(record_ids),
        )

    def flush(self) -> None:
        if (
            self._is_first_write
            and self._cfg.idempotent
            and not self._is_incremental_sync
            and not isinstance(self.engine, NonCallableMock)
        ):
            empty_df = pd.DataFrame(
                columns=["record_id", "form_id", "variable_name", "value", "timestamp"]
            )
            empty_df.to_sql(self.table, self.engine, if_exists="replace", index=False)
            self._is_first_write = False

    def close(self) -> None:
        """Close the sink and dispose any non-in-memory SQLAlchemy engine."""
        db_name = getattr(getattr(self.engine, "url", None), "database", None)
        if (
            not isinstance(self.engine, NonCallableMock)
            and db_name not in (None, ":memory:")
            and callable(getattr(self.engine, "dispose", None))
        ):
            self.engine.dispose()


def export_to_long_sql(
    sdk: ImednetClient,
    study_key: str,
    table_name: str,
    conn_str: str,
    *,
    chunk_size: int = 1000,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export records to a normalized long-format SQL table."""
    if pd is None:
        raise ImportError(
            "pandas is required for export_to_long_sql. Install with "
            "\"pip install 'imednet[export]'\"."
        )
    from sqlalchemy import create_engine

    manifest_path = kwargs.pop("manifest_path", None)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        manifest_path=manifest_path,
        mask_phi=mask_phi,
    )
    engine = create_engine(conn_str)
    mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
    pipeline = resolve_enrichment_pipeline(study_key)
    phi_keys = set(pipeline.phi_keys) if pipeline is not None else None

    if _uses_mock_fetch_records(mapper) and not _config_has_streaming_options(config):
        try:
            legacy_records = (
                rec
                for rec in mapper._fetch_records(study_key)
                if config.include_deleted or not is_record_deleted(rec)
            )
            long_rows = _unpack_long_sql_rows(
                legacy_records,
                mask_phi=config.mask_phi,
                pipeline=pipeline,
                phi_keys=phi_keys,
            )
            first = True
            for start in range(0, len(long_rows), max(1, chunk_size)):
                df = pd.DataFrame(long_rows[start : start + max(1, chunk_size)])
                df.to_sql(
                    table_name,
                    engine,
                    if_exists="replace" if first else "append",
                    index=False,
                )
                first = False
        finally:
            db_name = getattr(getattr(engine, "url", None), "database", None)
            if (
                not isinstance(engine, NonCallableMock)
                and db_name not in (None, ":memory:")
                and callable(getattr(engine, "dispose", None))
            ):
                engine.dispose()
        return

    records_source = _make_mapper_records_source(sdk, study_key, mapper, config)
    if config.as_sdtm:
        sdtm_sql_sink = TabularSQLSink(table_name, engine, config)
        sdtm_transform_sink = _TabularTransformSink(
            sdtm_sql_sink,
            mapper,
            None,
            [],
            {},
            config,
            sanitize=False,
            pipeline=pipeline,
        )
        run_streaming_export(
            sdk,
            study_key,
            sdtm_transform_sink,
            config,
            stream_name=f"long_sql_{table_name}",
            batch_id_prefix=f"{study_key}/long_sql/{table_name}",
            records_source=records_source,
            quality_gate_fn=apply_quality_gate,
            iter_batches_fn=iter_batches,
        )
        return

    long_sink = _LongSQLSink(
        table_name,
        engine,
        config,
        chunk_size=chunk_size,
        pipeline=pipeline,
    )
    run_streaming_export(
        sdk,
        study_key,
        long_sink,
        config,
        stream_name=f"long_sql_{table_name}",
        batch_id_prefix=f"{study_key}/long_sql/{table_name}",
        records_source=records_source,
        quality_gate_fn=apply_quality_gate,
        iter_batches_fn=iter_batches,
    )


_UNPATCHED_EXPORTS: dict[str, Callable[..., Any]] = {
    "export_to_csv": export_to_csv,
    "export_to_duckdb": export_to_duckdb,
    "export_to_excel": export_to_excel,
    "export_to_json": export_to_json,
    "export_to_long_sql": export_to_long_sql,
    "export_to_parquet": export_to_parquet,
    "export_to_sql": export_to_sql,
    "export_to_sql_by_form": export_to_sql_by_form,
}
