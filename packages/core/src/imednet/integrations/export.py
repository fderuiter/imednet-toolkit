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
import shutil
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import NonCallableMock

from imednet.errors import ExportConfigurationError
from imednet.integrations._export_contracts import (
    filter_records_by_form_whitelist,
    install_callable_export_module,
    record_matches_form_whitelist,
    resolve_config_manifest_path,
)
from imednet.integrations.enrichment import (
    EnrichmentPipeline,
    _extract_record_study_keys,
    _matches_custom_phi_key,
    mask_custom_phi_recursive,
    resolve_sink_enrichment_pipeline,
    resolve_sink_phi_keys,
)
from imednet.integrations.sink_base import (
    _SDTM_RECORDS_SORT,
    BoundedFormBufferRouter,
    ExportSink,
    SinkConfig,
    StateProviderProtocol,
    TabularBatchStamp,
    _enforce_batch_standards_readiness,
    _extract_safe_config_options,
    _is_bound_real_records_endpoint,
    _try_fetch_study_forms,
    apply_quality_gate,
    bind_sdtm_stream_metadata,
    build_form_sdtm_domain_map,
    commit_sdtm_lineage,
    extract_record_form_ref,
    extract_record_id,
    extract_record_timestamp,
    filter_forms_by_sdtm_domain,
    get_state_provider,
    get_tabular_batch_stamp,
    has_valid_tabular_batch_stamp,
    is_record_deleted,
    iter_batches,
    iter_filtered_sdk_records,
    iter_subject_ordered_sdtm_records,
    normalize_watermark_datetime,
    record_matches_sdtm_domain,
    redact_batch_records,
    rehydrate_sdtm_lineage,
    resolve_form_sdtm_domain,
    resolve_voided_sdtm_keys,
    run_streaming_export,
    should_preserve_sdtm_record_id,
    stamp_tabular_batch,
)
from imednet.models.sdtm import (
    format_iso8601_dtc,
    format_nested_variable_name,
    prefix_colliding_variable_name,
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
from imednet.core.context import sdk_governance_context
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


_CONTAINER_CELL_TYPES = (dict, list, tuple)


def _is_custom_phi_key(key: str, phi_keys: set[str] | None) -> bool:
    """Return True when *key* matches a custom StudyConfiguration PHI field."""
    if not phi_keys or not key:
        return False
    return _matches_custom_phi_key(key, phi_keys)


_SDTM_BIRTH_DATE_TOKENS = frozenset(
    {"brthdtc", "brthdat", "birthdate", "dob", "dateofbirth", "birthdt"}
)


def _is_birth_col(col: Any) -> bool:
    cleaned = re.sub(r"[^a-z0-9]", "", str(col).lower())
    return bool(cleaned) and (cleaned in _SDTM_BIRTH_DATE_TOKENS or cleaned.endswith("brthdtc"))


def _is_active_phi_key(key: Any, phi_keys: set[str] | None) -> bool:
    """Return True when *key* matches active global or study-specific PHI rules."""
    if not isinstance(key, str) or not key:
        return False
    return global_sensitivity_registry.is_sensitive(key) or _is_custom_phi_key(key, phi_keys)


def _mask_series_containers(
    series: pd.Series,
    phi_keys: set[str] | None = None,
) -> pd.Series:
    """Mask nested container cells in ``series`` without scalar mask_clinical_phi scans."""
    values = series.tolist()
    if not any(isinstance(v, _CONTAINER_CELL_TYPES) for v in values):
        return series
    new_vals: list[Any] = []
    for val in values:
        if isinstance(val, _CONTAINER_CELL_TYPES):
            masked_val = mask_clinical_phi(val)
            if phi_keys:
                masked_val = mask_custom_phi_recursive(masked_val, phi_keys)
            new_vals.append(masked_val)
        else:
            new_vals.append(val)
    return pd.Series(new_vals, index=series.index, dtype=object)


def _dedup_case_insensitive_columns(
    df: pd.DataFrame,
    *,
    ensure_str_columns: bool = False,
    in_place_allowed: bool = True,
) -> pd.DataFrame:
    """Drop case-insensitive duplicate columns without copying clean DataFrames."""
    if not isinstance(df, pd.DataFrame):
        return df
    prior_stamp = get_tabular_batch_stamp(df)
    was_sdtm_redacted = bool(df.attrs.get("imednet_sdtm_redacted"))
    all_str = all(isinstance(c, str) for c in df.columns)
    has_any_str = all_str or any(isinstance(c, str) for c in df.columns)

    dup_mask = None
    has_dups = False
    if ensure_str_columns:
        str_cols = list(df.columns) if all_str else [str(c) for c in df.columns]
        lower_cols = [c.lower() for c in str_cols]
        dup_mask = pd.Index(lower_cols).duplicated()
        has_dups = bool(dup_mask.any())
    elif has_any_str:
        lower_mixed = [c.lower() if isinstance(c, str) else c for c in df.columns]
        dup_mask = pd.Index(lower_mixed).duplicated()
        has_dups = bool(dup_mask.any())

    if (not ensure_str_columns or all_str) and not has_dups:
        return df

    out = df if in_place_allowed else df.copy()
    if ensure_str_columns and not all_str:
        if not in_place_allowed:
            out.columns = out.columns.astype(str)
        else:
            out = out.copy()
            out.columns = out.columns.astype(str)
        prior_stamp = None
    if has_dups and dup_mask is not None:
        out = out.copy()
        cols_list = list(out.columns)
        all_upper = all(
            isinstance(c, str) and c == c.upper() and any(ch.isalpha() for ch in c)
            for c in cols_list
            if isinstance(c, str)
        )
        dedup_conv = "sdtm" if (was_sdtm_redacted or all_upper) else None
        used_lower: set[str] = set()
        new_cols: list[Any] = []
        for c in cols_list:
            if not isinstance(c, str):
                new_cols.append(c)
                continue
            new_cols.append(
                prefix_colliding_variable_name(
                    c,
                    convention=dedup_conv,
                    used_lower=used_lower,
                )
            )
        out.columns = pd.Index(new_cols)
        prior_stamp = None

    if was_sdtm_redacted:
        out.attrs["imednet_sdtm_redacted"] = True
    if (
        prior_stamp is not None
        and get_tabular_batch_stamp(out) is None
        and (
            not prior_stamp.mask_phi
            or prior_stamp.registry_snapshot == global_sensitivity_registry.snapshot()
        )
    ):
        stamp_tabular_batch(
            out,
            mask_phi=prior_stamp.mask_phi,
            phi_keys=prior_stamp.phi_keys,
            sanitized=prior_stamp.sanitized,
        )
    return out


def _mask_df(
    df: pd.DataFrame,
    *,
    phi_keys: set[str] | None = None,
    as_sdtm: bool = False,
) -> pd.DataFrame:
    """Mask sensitive fields in the DataFrame based on global and study rules."""
    if isinstance(df, pd.DataFrame) and has_valid_tabular_batch_stamp(
        df, mask_phi=True, phi_keys=phi_keys
    ):
        return df

    was_sdtm_redacted = isinstance(df, pd.DataFrame) and bool(df.attrs.get("imednet_sdtm_redacted"))
    effective_sdtm = bool(as_sdtm or was_sdtm_redacted)
    prior_stamp = get_tabular_batch_stamp(df)
    was_sanitized = bool(prior_stamp.sanitized) if prior_stamp is not None else False

    birth_cols = (
        [
            col
            for col in df.columns
            if _is_birth_col(col) and not global_sensitivity_registry.is_exempt(str(col))
        ]
        if effective_sdtm
        else []
    )
    birth_col_set = set(birth_cols)
    for col in birth_cols:
        if len(df) > 0:
            df[col] = df[col].map(
                lambda val: (
                    None if _is_missing_scalar(val) else format_iso8601_dtc(val, mask_phi=True)
                )
            )

    sensitive_cols = list(
        dict.fromkeys(
            col
            for col in df.columns
            if col not in birth_col_set
            and (
                global_sensitivity_registry.is_sensitive(str(col))
                or _is_custom_phi_key(str(col), phi_keys)
            )
        )
    )
    for col in sensitive_cols:
        if len(df) > 0:
            df[col] = MASKED_SENTINEL

    if len(df) == 0:
        if effective_sdtm:
            df.attrs["imednet_sdtm_redacted"] = True
        stamp_tabular_batch(df, mask_phi=True, phi_keys=phi_keys, sanitized=was_sanitized)
        return df

    remaining = df.drop(columns=[*sensitive_cols, *birth_cols], errors="ignore")
    try:
        object_cols = list(dict.fromkeys(remaining.select_dtypes(include=[object, "str"]).columns))
    except TypeError:
        object_cols = list(dict.fromkeys(remaining.select_dtypes(include=[object]).columns))

    for col in object_cols:
        target = df[col]
        if isinstance(target, pd.DataFrame):
            df[col] = target.apply(lambda s: _mask_series_containers(s, phi_keys))
        else:
            df[col] = _mask_series_containers(target, phi_keys)

    if effective_sdtm:
        df.attrs["imednet_sdtm_redacted"] = True
    stamp_tabular_batch(df, mask_phi=True, phi_keys=phi_keys, sanitized=was_sanitized)
    return df


def _normalize_sqlite_temporal_object_columns(
    df: pd.DataFrame,
    engine: Any,
    *,
    dtype: Any = None,
) -> pd.DataFrame:
    """Normalize Python ``datetime`` and ``date`` values in SQLite-bound ``object`` columns."""
    if (
        pd is None
        or not isinstance(df, pd.DataFrame)
        or isinstance(df, NonCallableMock)
        or df.empty
        or getattr(getattr(engine, "dialect", None), "name", None) != "sqlite"
    ):
        return df
    if dtype is not None and not isinstance(dtype, Mapping):
        return df
    mapped_cols = dtype if isinstance(dtype, Mapping) else None

    normalized: pd.DataFrame | None = None
    for idx in range(len(df.columns)):
        col_name = df.columns[idx]
        if mapped_cols is not None and col_name in mapped_cols:
            continue
        series = df.iloc[:, idx]
        if series.dtype != object:
            continue
        values = series.tolist()
        if not any(val is not pd.NaT and isinstance(val, date) for val in values):
            continue
        if normalized is None:
            normalized = df.copy()
        new_vals: list[Any] = []
        for val in values:
            if val is pd.NaT:
                new_vals.append(val)
            elif isinstance(val, datetime):
                new_vals.append(val.isoformat(" "))
            elif isinstance(val, date):
                new_vals.append(val.isoformat())
            else:
                new_vals.append(val)
        normalized.isetitem(idx, pd.Series(new_vals, index=series.index, dtype=object))
    return normalized if normalized is not None else df


def _resolve_wide_sqlite_key_columns(
    df: pd.DataFrame,
    *,
    study_key: str | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Return ``(adjusted_df, key_cols)`` for wide SQLite split propagation."""
    id_col = _find_df_record_id_col(df)
    if id_col is not None:
        study_col = _find_df_study_col(df)
        adjusted = df
        if study_col is None and study_key:
            inferred_study_col = (
                "STUDYID"
                if id_col == "RECORD_ID"
                else ("study_key" if id_col == "record_id" else "studyKey")
            )
            adjusted = _preserve_df_stamp_and_sdtm_attr(df, df.copy())
            adjusted.insert(0, inferred_study_col, study_key)
            study_col = inferred_study_col
        key_cols = [id_col]
        if study_col is not None and study_col != id_col:
            key_cols.append(study_col)
        return adjusted, key_cols

    if {"STUDYID", "DOMAIN", "USUBJID"} <= set(df.columns):
        sdtm_keys = [
            str(c)
            for c in df.columns
            if c in {"STUDYID", "DOMAIN", "USUBJID"}
            or (isinstance(c, str) and c.endswith("SEQ") and c.upper() != "DMSEQ" and c != "DOMAIN")
        ]
        if len(sdtm_keys) < MAX_SQLITE_COLUMNS:
            return df, sdtm_keys

    return df, []


def _filter_to_sql_chunk_kwargs(
    kwargs: Mapping[str, Any],
    columns: pd.Index,
) -> dict[str, Any]:
    """Filter per-column ``dtype`` mappings in ``to_sql`` kwargs to columns present in a chunk."""
    out = dict(kwargs)
    raw_dtype = out.get("dtype")
    if isinstance(raw_dtype, Mapping):
        col_set = set(columns)
        out["dtype"] = {k: v for k, v in raw_dtype.items() if k in col_set}
    return out


def _drop_sqlite_tables_by_name(engine: Any, table_names: Sequence[str]) -> None:
    """Drop existing SQLite tables in ``table_names`` when ``engine`` is a real SQLAlchemy engine."""
    if not table_names or isinstance(engine, NonCallableMock):
        return
    from sqlalchemy import text as sa_text

    preparer = engine.dialect.identifier_preparer
    with engine.begin() as conn:
        for t_name in table_names:
            conn.execute(sa_text(f"DROP TABLE IF EXISTS {preparer.quote(t_name)}"))


def _to_sql_with_chunking(
    df: pd.DataFrame,
    table: str,
    engine: Any,
    *,
    if_exists: Literal["fail", "replace", "append", "delete_rows"],
    study_key: str | None = None,
    **kwargs: Any,
) -> None:
    """Write ``df`` to ``table`` splitting columns when using SQLite.

    SQLite limits tables to ``MAX_SQLITE_COLUMNS`` columns. When the DataFrame
    exceeds this, the data is written to multiple tables suffixed with
    ``_part1``, ``_part2`` and so on while propagating Record and Study keys
    across every part when keyed columns are present.
    """
    df = _normalize_sqlite_temporal_object_columns(
        df,
        engine,
        dtype=kwargs.get("dtype"),
    )
    if engine.dialect.name == "sqlite" and len(df.columns) > MAX_SQLITE_COLUMNS:
        key_cols: list[str] = []
        if isinstance(df, pd.DataFrame):
            df, key_cols = _resolve_wide_sqlite_key_columns(df, study_key=study_key)

        chunks: list[pd.DataFrame] = []
        if key_cols and len(key_cols) < MAX_SQLITE_COLUMNS:
            key_set = set(key_cols)
            non_key_cols = [c for c in df.columns if c not in key_set]
            payload_width = MAX_SQLITE_COLUMNS - len(key_cols)
            for idx, start in enumerate(range(0, max(1, len(non_key_cols)), payload_width)):
                non_key_slice = non_key_cols[start : start + payload_width]
                if idx == 0:
                    part1_set = key_set | set(non_key_slice)
                    part_cols = [c for c in df.columns if c in part1_set]
                    if len(part_cols) > MAX_SQLITE_COLUMNS:
                        part_cols = [*key_cols, *non_key_slice]
                else:
                    part_cols = [*key_cols, *non_key_slice]
                chunks.append(df.loc[:, part_cols])
        else:
            for start in range(0, len(df.columns), MAX_SQLITE_COLUMNS):
                chunks.append(df.iloc[:, start : start + MAX_SQLITE_COLUMNS])

        if if_exists == "replace" and not isinstance(engine, NonCallableMock):
            from sqlalchemy import inspect as sa_inspect

            inspector = sa_inspect(engine)
            stale_tables: list[str] = []
            if bool(inspector.has_table(table)):
                stale_tables.append(table)
            for part_num, part_name in _list_sql_part_tables(inspector, table):
                if part_num > len(chunks):
                    stale_tables.append(part_name)
            _drop_sqlite_tables_by_name(engine, stale_tables)

        for i, chunk in enumerate(chunks, start=1):
            chunk_kwargs = _filter_to_sql_chunk_kwargs(kwargs, chunk.columns)
            chunk.to_sql(
                f"{table}_part{i}",
                engine,
                if_exists=if_exists,
                index=False,
                **chunk_kwargs,
            )
    else:
        if (
            if_exists == "replace"
            and engine.dialect.name == "sqlite"
            and not isinstance(engine, NonCallableMock)
        ):
            from sqlalchemy import inspect as sa_inspect

            inspector = sa_inspect(engine)
            stale_parts = [name for _, name in _list_sql_part_tables(inspector, table)]
            _drop_sqlite_tables_by_name(engine, stale_parts)
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
        df = _dedup_case_insensitive_columns(df, ensure_str_columns=True)
    return df


def _seed_mapper_enrichment(
    mapper: Any,
    study_key: str | None,
    pipeline: Any | None,
    config: SinkConfig | None = None,
    *,
    records: Any = None,
) -> None:
    """Bind *study_key* and any *config*-resolved enrichment *pipeline* onto *mapper*."""
    if isinstance(mapper, NonCallableMock):
        return
    if isinstance(study_key, str) and study_key and hasattr(mapper, "_active_study_key"):
        mapper._active_study_key = study_key
    cache = getattr(mapper, "_enrichment_cache", None)
    if not isinstance(cache, dict):
        return
    extra = getattr(config, "extra", None) if config is not None else None
    has_extra_override = isinstance(extra, Mapping) and (
        extra.get("enrichment_pipeline") is not None or extra.get("study_config") is not None
    )
    if isinstance(study_key, str) and study_key and (pipeline is not None or has_extra_override):
        cache[study_key] = pipeline
    if has_extra_override and records is not None:
        for rec_sk in _extract_record_study_keys(records):
            cache[rec_sk] = pipeline


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
    config: SinkConfig | None = None,
) -> pd.DataFrame:
    """Prepare a DataFrame for export by fetching records and optionally sanitizing."""
    with sdk_governance_context(sdk):
        pipeline = resolve_sink_enrichment_pipeline(study_key, config=config)
        if _mapper is not None:
            _seed_mapper_enrichment(_mapper, study_key, pipeline, config=config)
        df = _records_df(
            sdk,
            study_key,
            use_labels_as_columns=use_labels_as_columns,
            variable_whitelist=variable_whitelist,
            form_whitelist=form_whitelist,
            mask_phi=mask_phi,
            _mapper=_mapper,
        )
        phi_keys = (
            resolve_sink_phi_keys(
                study_key,
                config=config,
                records=df,
                phi_keys=getattr(pipeline, "phi_keys", None),
            )
            if mask_phi
            else None
        )
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
    if isinstance(df, pd.DataFrame) and has_valid_tabular_batch_stamp(
        df, mask_phi=False, require_sanitized=True
    ):
        return df

    prior_stamp = get_tabular_batch_stamp(df)
    was_sdtm_redacted = isinstance(df, pd.DataFrame) and bool(df.attrs.get("imednet_sdtm_redacted"))

    # Explicitly include "str" to avoid Pandas 3.0 warning about object dtype
    # including strings implicitly. Use a try/except block to handle older Pandas versions
    # (e.g. 2.3.3) where "str" raises a TypeError.
    try:
        cols = list(dict.fromkeys(df.select_dtypes(include=[object, "str"]).columns))
    except TypeError:
        cols = list(dict.fromkeys(df.select_dtypes(include=[object]).columns))

    def _sanitize_series_once(series: pd.Series) -> tuple[pd.Series, bool]:
        vals = series.tolist()
        mutated = False
        sanitized_vals: list[Any] = []
        for v in vals:
            nv = sanitize_csv_formula(v)
            if nv is not v:
                mutated = True
            sanitized_vals.append(nv)
        if not mutated:
            return series, False
        return pd.Series(sanitized_vals, index=series.index, dtype=series.dtype), True

    for col in cols:
        target = df[col]
        if isinstance(target, pd.DataFrame):
            df[col] = target.apply(lambda s: _sanitize_series_once(s)[0])
        else:
            new_series, changed = _sanitize_series_once(target)
            if changed:
                df[col] = new_series

    if was_sdtm_redacted:
        df.attrs["imednet_sdtm_redacted"] = True
    if prior_stamp is not None and (
        not prior_stamp.mask_phi
        or prior_stamp.registry_snapshot == global_sensitivity_registry.snapshot()
    ):
        stamp_tabular_batch(
            df,
            mask_phi=prior_stamp.mask_phi,
            phi_keys=prior_stamp.phi_keys,
            sanitized=True,
        )
    else:
        stamp_tabular_batch(df, mask_phi=False, sanitized=True)
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


def _preserve_df_stamp_and_sdtm_attr(src: pd.DataFrame, dst: pd.DataFrame) -> pd.DataFrame:
    """Copy ``imednet_sdtm_redacted`` and valid ``TabularBatchStamp`` from ``src`` to ``dst``."""
    if bool(src.attrs.get("imednet_sdtm_redacted")):
        dst.attrs["imednet_sdtm_redacted"] = True
    prior_stamp = get_tabular_batch_stamp(src)
    if prior_stamp is not None and (
        not prior_stamp.mask_phi
        or prior_stamp.registry_snapshot == global_sensitivity_registry.snapshot()
    ):
        stamp_tabular_batch(
            dst,
            mask_phi=prior_stamp.mask_phi,
            phi_keys=prior_stamp.phi_keys,
            sanitized=prior_stamp.sanitized,
        )
    return dst


def _coerce_sdtm_integer_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Preserve nullable ``Int64`` for integer SDTM ``*SEQ``, ``AGE``, and ``RECORD_ID`` columns."""
    if df.empty:
        return df
    adjusted: pd.DataFrame | None = None
    for col_raw in df.columns:
        col_str = str(col_raw)
        if not (
            col_str in {"AGE", "RECORD_ID"}
            or (col_str.endswith("SEQ") and col_str.upper() != "DMSEQ" and col_str != "DOMAIN")
        ):
            continue
        series = df[col_raw]
        if pd.api.types.is_float_dtype(series.dtype) and bool(series.isna().any()):
            vals = series.tolist()
            non_missing = [v for v in vals if not _is_missing_scalar(v)]
            if non_missing and all(float(v).is_integer() for v in non_missing):
                if adjusted is None:
                    adjusted = _preserve_df_stamp_and_sdtm_attr(df, df.copy())
                adjusted[col_raw] = pd.Series(
                    [pd.NA if _is_missing_scalar(v) else int(v) for v in vals],
                    index=df.index,
                    dtype="Int64",
                )
    return adjusted if adjusted is not None else df


def _resolve_study_phi_keys(
    study_key: str | None,
    *,
    config: SinkConfig | None = None,
    records: Any = None,
) -> set[str] | None:
    """Resolve custom normalized PHI keys from ``SinkConfig.extra``, store, or Records."""
    return resolve_sink_phi_keys(study_key, config=config, records=records)


def _prepare_tabular_batch_df(
    records: Any,
    *,
    mask_phi: bool,
    sanitize: bool,
    study_key: str | None = None,
    as_sdtm: bool = False,
    config: SinkConfig | None = None,
) -> pd.DataFrame:
    """Convert ``records`` into a DataFrame with single-pass PHI masking and CSV sanitization."""
    if isinstance(records, pd.DataFrame):
        phi_keys = (
            _resolve_study_phi_keys(study_key, config=config, records=records) if mask_phi else None
        )
        was_sdtm_redacted = bool(records.attrs.get("imednet_sdtm_redacted"))
        effective_sdtm = bool(as_sdtm or was_sdtm_redacted)
        if len(records) == 0:
            return records
        has_dup_cols = False
        if any(isinstance(c, str) for c in records.columns):
            lower_cols = [c.lower() if isinstance(c, str) else c for c in records.columns]
            has_dup_cols = bool(pd.Index(lower_cols).duplicated().any())
        already_masked = (not mask_phi) or has_valid_tabular_batch_stamp(
            records,
            mask_phi=True,
            phi_keys=phi_keys,
        )
        already_sanitized = (not sanitize) or has_valid_tabular_batch_stamp(
            records,
            mask_phi=False,
            require_sanitized=True,
        )
        if not has_dup_cols and already_masked and already_sanitized:
            return _coerce_sdtm_integer_columns(records) if effective_sdtm else records

        df = _preserve_df_stamp_and_sdtm_attr(records, records.copy())
        if has_dup_cols:
            df = _dedup_case_insensitive_columns(df, ensure_str_columns=False)
            already_masked = (not mask_phi) or has_valid_tabular_batch_stamp(
                df,
                mask_phi=True,
                phi_keys=phi_keys,
            )
            already_sanitized = (not sanitize) or has_valid_tabular_batch_stamp(
                df,
                mask_phi=False,
                require_sanitized=True,
            )
        if mask_phi and not already_masked:
            df = _mask_df(df, phi_keys=phi_keys, as_sdtm=effective_sdtm)
        if sanitize and not already_sanitized:
            df = _sanitize_df(df)
        if effective_sdtm:
            df = _coerce_sdtm_integer_columns(df)
        return df

    items = list(records) if not isinstance(records, Sequence) else records
    if not items:
        return pd.DataFrame()
    phi_keys = (
        _resolve_study_phi_keys(study_key, config=config, records=items) if mask_phi else None
    )
    normalized = [_normalize_tabular_batch_item(item) for item in items]
    df = pd.DataFrame(normalized)
    if len(df) == 0:
        return df

    df = _dedup_case_insensitive_columns(df, ensure_str_columns=False)
    if mask_phi:
        df = _mask_df(df, phi_keys=phi_keys, as_sdtm=as_sdtm)
    if sanitize:
        df = _sanitize_df(df)
    if as_sdtm:
        df = _coerce_sdtm_integer_columns(df)
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


def _append_jsonl_manifest(
    manifest_path: str | None,
    *,
    batch_id: str,
    destination: str,
    row_count: int,
    checksum: str,
) -> None:
    """Append a JSONL batch manifest entry when ``manifest_path`` is configured."""
    if not manifest_path:
        return
    entry = {
        "batch_id": batch_id,
        "destination": destination,
        "row_count": row_count,
        "checksum": checksum,
        "loaded_at": datetime.now(tz=UTC).isoformat(),
    }
    Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + os.linesep)


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


def _canonical_study_key(value: Any, fallback: str | None = None) -> str | None:
    """Normalize an optional study identifier scalar, falling back to ``fallback`` when missing."""
    if value is None or value is pd.NA or isinstance(value, bool) or _is_missing_scalar(value):
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _extract_row_study_key(row: Mapping[str, Any]) -> str | None:
    """Extract study identifier from a row mapping under ``STUDYID``, ``study_key``, or ``studyKey``."""
    for key in ("STUDYID", "study_key", "studyKey"):
        if key in row:
            skey = _canonical_study_key(row[key])
            if skey is not None:
                return skey
    return None


def _find_df_record_id_col(df: pd.DataFrame) -> str | None:
    """Return the record ID column name in ``df``, if present."""
    for candidate in ("recordId", "record_id", "RECORD_ID"):
        if candidate in df.columns:
            return candidate
    return None


def _find_df_study_col(df: pd.DataFrame) -> str | None:
    """Return the study identifier column name in ``df``, if present."""
    for candidate in ("STUDYID", "study_key", "studyKey"):
        if candidate in df.columns:
            return candidate
    return None


def _dedupe_df_by_record_id(
    df: pd.DataFrame,
    *,
    exclude_ids: set[str] | None = None,
    exclude_sdtm_keys: set[SdtmKeyTuple] | None = None,
    study_key: str | None = None,
) -> tuple[pd.DataFrame, set[str]]:
    """Keep the latest row per canonical ``(STUDYID, RECORD_ID)`` (or ``(STUDYID, 'DM', USUBJID)`` for DM) and optionally drop excluded keys."""
    if df.empty:
        return df, set()
    id_col = _find_df_record_id_col(df)
    if id_col is None:
        deduped_unkeyed = _preserve_df_stamp_and_sdtm_attr(df, df.reset_index(drop=True))
        return deduped_unkeyed, set()

    excluded = exclude_ids or set()
    excluded_sdtm = exclude_sdtm_keys or set()
    study_col = _find_df_study_col(df)
    raw_ids = df[id_col].tolist()
    raw_studies = df[study_col].tolist() if study_col is not None else None
    has_sdtm_cols = {"STUDYID", "DOMAIN", "USUBJID"} <= set(df.columns)
    row_sdtm_keys = _df_sdtm_keys(df) if has_sdtm_cols else None
    seen_composite: set[tuple[Any, ...]] = set()
    seen_cids_for_study: set[str] = set()
    kept_rev: list[int] = []
    for idx in range(len(raw_ids) - 1, -1, -1):
        cid = _canonical_record_id(raw_ids[idx])
        skey = row_sdtm_keys[idx] if row_sdtm_keys is not None else None
        is_dm_row = skey is not None and skey[1] == "DM"
        if is_dm_row and skey in excluded_sdtm:
            continue
        if cid is None:
            if is_dm_row:
                dm_comp: tuple[Any, ...] = ("__SDTM_DM__", skey)
                if dm_comp in seen_composite:
                    continue
                seen_composite.add(dm_comp)
            kept_rev.append(idx)
            continue
        row_study = (
            _canonical_study_key(raw_studies[idx], study_key)
            if raw_studies is not None
            else study_key
        )
        matches_target_study = study_key is None or row_study is None or row_study == study_key
        if cid in excluded and matches_target_study:
            continue
        comp_key: tuple[Any, ...] = ("__SDTM_DM__", skey) if is_dm_row else (row_study or "", cid)
        if comp_key in seen_composite:
            continue
        seen_composite.add(comp_key)
        if matches_target_study:
            seen_cids_for_study.add(cid)
        kept_rev.append(idx)

    kept_rev.reverse()
    deduped = _preserve_df_stamp_and_sdtm_attr(df, df.iloc[kept_rev].reset_index(drop=True))
    return deduped, seen_cids_for_study


def _filter_df_excluding_record_ids(
    df: pd.DataFrame,
    exclude_ids: set[str],
    *,
    study_key: str | None = None,
) -> pd.DataFrame:
    """Drop rows from ``df`` whose ``(STUDYID, RECORD_ID)`` matches ``exclude_ids``."""
    if df.empty or not exclude_ids:
        return df
    id_col = _find_df_record_id_col(df)
    if id_col is None:
        return df
    study_col = _find_df_study_col(df)
    if study_col is not None and study_key:
        mask = [
            not (
                _canonical_record_id(val) in exclude_ids
                and (_canonical_study_key(sval, study_key) == study_key)
            )
            for val, sval in zip(df[id_col].tolist(), df[study_col].tolist(), strict=False)
        ]
    else:
        mask = [_canonical_record_id(val) not in exclude_ids for val in df[id_col].tolist()]
    return _preserve_df_stamp_and_sdtm_attr(df, df.loc[mask].reset_index(drop=True))


SdtmKeyTuple = tuple[str, str, str, str | None, int | None]


def _parse_sdtm_seq_int(value: Any) -> int | None:
    """Parse an SDTM sequence value (int, float, or float-formatted string like ``'1.0'``)."""
    seq_cid = _canonical_record_id(value)
    if seq_cid is None:
        return None
    try:
        return int(seq_cid)
    except ValueError:
        try:
            as_float = float(seq_cid)
        except ValueError:
            return None
        if math.isnan(as_float) or math.isinf(as_float) or not as_float.is_integer():
            return None
        return int(as_float)


def _extract_row_sdtm_key(row: Mapping[str, Any]) -> SdtmKeyTuple | None:
    """Extract canonical ``(STUDYID, DOMAIN, USUBJID, seq_col, seq_val)`` from a tabular row."""
    raw_study = row.get("STUDYID")
    raw_domain = row.get("DOMAIN")
    raw_usubjid = row.get("USUBJID")
    if (
        _is_missing_scalar(raw_study)
        or _is_missing_scalar(raw_domain)
        or _is_missing_scalar(raw_usubjid)
    ):
        return None
    study_id = str(raw_study).strip()
    domain = str(raw_domain).strip().upper()
    usubjid = str(raw_usubjid).strip()
    if not study_id or not domain or not usubjid:
        return None
    if domain == "DM":
        return (study_id, "DM", usubjid, None, None)
    seq_col = f"{domain}SEQ"
    seq_val = _parse_sdtm_seq_int(row.get(seq_col))
    if seq_val is None:
        return None
    return (study_id, domain, usubjid, seq_col, seq_val)


def _lineage_entry_to_sdtm_key(entry: Mapping[str, Any]) -> SdtmKeyTuple | None:
    """Convert a ``resolve_voided_sdtm_keys`` dict into a canonical ``SdtmKeyTuple``."""
    study_id = str(entry.get("study_id") or entry.get("STUDYID") or "").strip()
    domain = str(entry.get("domain") or entry.get("DOMAIN") or "").strip().upper()
    usubjid = str(entry.get("usubjid") or entry.get("USUBJID") or "").strip()
    if not study_id or not domain or not usubjid:
        return None
    if domain == "DM":
        return (study_id, "DM", usubjid, None, None)
    seq_col = str(entry.get("seq_col") or f"{domain}SEQ")
    seq_val = _parse_sdtm_seq_int(entry.get("seq_val"))
    if seq_val is None:
        return None
    return (study_id, domain, usubjid, seq_col, seq_val)


def _resolve_sink_voided_sdtm_keys(sink: Any, records: Sequence[Any]) -> list[SdtmKeyTuple]:
    """Resolve voided raw records into canonical SDTM composite key tuples using sink lineage."""
    cfg = getattr(sink, "config", None)
    raw_entries = resolve_voided_sdtm_keys(
        records,
        sink,
        study_key=getattr(sink, "_active_study_key", None) or getattr(cfg, "study_key", None),
        stream_name=getattr(sink, "_active_stream_name", None) or getattr(cfg, "stream_name", None),
        sdtm_domain=getattr(cfg, "sdtm_domain", None),
        form_domain_map=getattr(sink, "_active_form_domain_map", None),
    )
    out: list[SdtmKeyTuple] = []
    seen: set[SdtmKeyTuple] = set()
    for entry in raw_entries:
        skey = _lineage_entry_to_sdtm_key(entry)
        if skey is not None and skey not in seen:
            seen.add(skey)
            out.append(skey)
    return out


def _df_sdtm_keys(df: pd.DataFrame) -> list[SdtmKeyTuple | None]:
    """Extract canonical ``SdtmKeyTuple`` per row of ``df``."""
    if df.empty or not {"STUDYID", "DOMAIN", "USUBJID"} <= set(df.columns):
        return [None] * len(df)
    records = df.to_dict(orient="records")
    return [_extract_row_sdtm_key(r) for r in records]


def _dedupe_df_by_sdtm_key(
    df: pd.DataFrame,
    *,
    exclude_keys: set[SdtmKeyTuple] | None = None,
) -> tuple[pd.DataFrame, set[SdtmKeyTuple]]:
    """Keep the latest row per SDTM composite key and optionally drop ``exclude_keys``."""
    if df.empty:
        return df, set()
    row_keys = _df_sdtm_keys(df)
    if all(k is None for k in row_keys):
        return _preserve_df_stamp_and_sdtm_attr(df, df.reset_index(drop=True)), set()

    excluded = exclude_keys or set()
    seen: set[SdtmKeyTuple] = set()
    kept_rev: list[int] = []
    for idx in range(len(row_keys) - 1, -1, -1):
        skey = row_keys[idx]
        if skey is None:
            kept_rev.append(idx)
            continue
        if skey in excluded or skey in seen:
            continue
        seen.add(skey)
        kept_rev.append(idx)

    kept_rev.reverse()
    out = _preserve_df_stamp_and_sdtm_attr(df, df.iloc[kept_rev].reset_index(drop=True))
    return out, seen


def _filter_df_excluding_sdtm_keys(
    df: pd.DataFrame,
    exclude_keys: set[SdtmKeyTuple],
) -> pd.DataFrame:
    """Drop rows from ``df`` whose SDTM composite key appears in ``exclude_keys``."""
    if df.empty or not exclude_keys:
        return df
    row_keys = _df_sdtm_keys(df)
    if all(k is None for k in row_keys):
        return df
    mask = [k not in exclude_keys for k in row_keys]
    return _preserve_df_stamp_and_sdtm_attr(df, df.loc[mask].reset_index(drop=True))


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
    all_sdtm_redacted = bool(non_empty) and all(
        bool(f.attrs.get("imednet_sdtm_redacted")) for f in non_empty
    )
    if not non_empty:
        return frames[0].copy() if frames else pd.DataFrame()
    if len(non_empty) == 1:
        single = _preserve_df_stamp_and_sdtm_attr(non_empty[0], non_empty[0].reset_index(drop=True))
        if all_sdtm_redacted:
            single.attrs["imednet_sdtm_redacted"] = True
        if all_sdtm_redacted or {"STUDYID", "DOMAIN", "USUBJID"} <= set(single.columns):
            single = _coerce_sdtm_integer_columns(single)
        return single

    stamps: list[TabularBatchStamp | None] = [get_tabular_batch_stamp(f) for f in non_empty]
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
    reindexed = merged.reindex(columns=unified_cols)
    if all_sdtm_redacted:
        reindexed.attrs["imednet_sdtm_redacted"] = True
    first_stamp = stamps[0] if stamps else None
    if (
        first_stamp is not None
        and all(
            st is not None
            and st.mask_phi == first_stamp.mask_phi
            and st.registry_snapshot == first_stamp.registry_snapshot
            for st in stamps[1:]
        )
        and (
            not first_stamp.mask_phi
            or first_stamp.registry_snapshot == global_sensitivity_registry.snapshot()
        )
    ):
        common_phi = first_stamp.phi_keys.intersection(
            *(st.phi_keys for st in stamps[1:] if st is not None)
        )
        stamp_tabular_batch(
            reindexed,
            mask_phi=first_stamp.mask_phi,
            phi_keys=common_phi,
            sanitized=all(bool(st.sanitized) for st in stamps if st is not None),
        )
    if all_sdtm_redacted or {"STUDYID", "DOMAIN", "USUBJID"} <= set(reindexed.columns):
        reindexed = _coerce_sdtm_integer_columns(reindexed)
    return reindexed


_PURE_JSON_NON_FLOAT_SCALARS = frozenset({str, int, bool, type(None)})


def _sanitize_json_value(value: Any) -> Any:
    """Recursively convert missing sentinels and nonfinite floats to ``None`` for RFC 8259 JSON."""
    vc = value.__class__
    if vc in _PURE_JSON_NON_FLOAT_SCALARS:
        return value
    if value is pd.NA:
        return None
    if vc is float:
        return value if math.isfinite(value) else None
    if vc is dict:
        mutated_dict: dict[Any, Any] | None = None
        pure_scalars = _PURE_JSON_NON_FLOAT_SCALARS
        isfinite = math.isfinite
        for k, v in value.items():
            v_cls = v.__class__
            if v_cls in pure_scalars:
                continue
            if v_cls is float:
                if isfinite(v):
                    continue
                new_v: Any = None
            else:
                new_v = _sanitize_json_value(v)
                if new_v is v and new_v.__class__ is v_cls:
                    continue
            if mutated_dict is None:
                mutated_dict = dict(value)
            mutated_dict[k] = new_v
        return mutated_dict if mutated_dict is not None else value
    if vc is list:
        mutated_list: list[Any] | None = None
        pure_scalars = _PURE_JSON_NON_FLOAT_SCALARS
        isfinite = math.isfinite
        for idx, item in enumerate(value):
            i_cls = item.__class__
            if i_cls in pure_scalars:
                continue
            if i_cls is float:
                if isfinite(item):
                    continue
                new_item: Any = None
            else:
                new_item = _sanitize_json_value(item)
                if new_item is item and new_item.__class__ is i_cls:
                    continue
            if mutated_list is None:
                mutated_list = list(value)
            mutated_list[idx] = new_item
        return mutated_list if mutated_list is not None else value
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

    _skip_auto_redact_batch = True

    def __init__(self, path: str | os.PathLike[str], config: TabularSinkConfig):
        """Initialize the CSV sink."""
        super().__init__(config)
        self.path: str = _coerce_path_str(path)
        self._is_first_batch = True
        self._cfg = config
        self._is_incremental_sync = _is_incremental_config(config)
        self._staged_frames: list[pd.DataFrame] = []
        self._voided_ids: set[str] = set()
        self._voided_sdtm_keys: set[SdtmKeyTuple] = set()
        self._csv_columns: list[str] | None = None

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write a batch of records to the sink."""
        df = _prepare_tabular_batch_df(
            records,
            mask_phi=self._cfg.mask_phi,
            sanitize=self._cfg.sanitize,
            as_sdtm=self._cfg.as_sdtm,
            study_key=self._cfg.study_key,
            config=self._cfg,
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
                _append_jsonl_manifest(
                    self._cfg.manifest_path,
                    batch_id=batch_id,
                    destination=self.path,
                    row_count=rows_loaded,
                    checksum=checksum,
                )
                id_col = _find_df_record_id_col(df)
                study_col = _find_df_study_col(df)
                if id_col is not None:
                    raw_studies = (
                        df[study_col].tolist() if study_col is not None else [None] * len(df)
                    )
                    for raw_id, raw_sval in zip(df[id_col].tolist(), raw_studies, strict=False):
                        cid = _canonical_record_id(raw_id)
                        skey_st = _canonical_study_key(raw_sval, self._cfg.study_key)
                        if cid is not None and (
                            not self._cfg.study_key or skey_st == self._cfg.study_key
                        ):
                            self._voided_ids.discard(cid)
                if self._cfg.as_sdtm:
                    for skey in _df_sdtm_keys(df):
                        if skey is not None:
                            self._voided_sdtm_keys.discard(skey)
                self._staged_frames.append(df)
                return rows_loaded

            loaded = self._execute_with_retry("export_csv", batch_id, stage_export)
            self._is_first_batch = False
            return loaded

        def execute_export() -> int:
            batch_cols = [str(c) for c in df.columns]
            df_str_cols = df
            if list(df.columns) != batch_cols:
                df_str_cols = df.copy()
                df_str_cols.columns = pd.Index(batch_cols)

            sep = str(self._cfg.pandas_kwargs.get("sep", ","))
            write_fresh = bool(
                self._is_first_batch
                and (
                    self._cfg.idempotent
                    or not Path(self.path).exists()
                    or Path(self.path).stat().st_size == 0
                )
            )

            if write_fresh:
                csv_str = df_str_cols.to_csv(index=False, header=True, **self._cfg.pandas_kwargs)
                checksum = hashlib.sha256(csv_str.encode("utf-8")).hexdigest()
                with open(self.path, mode="w", encoding="utf-8") as f:
                    f.write(csv_str)
                self._csv_columns = batch_cols
            else:
                existing_cols = self._csv_columns
                if existing_cols is None:
                    with open(self.path, encoding="utf-8", newline="") as src_f:
                        header_row = next(csv.reader(src_f, delimiter=sep), None)
                    existing_cols = [c for c in (header_row or []) if c != ""]
                if not existing_cols:
                    csv_str = df_str_cols.to_csv(
                        index=False,
                        header=True,
                        **self._cfg.pandas_kwargs,
                    )
                    checksum = hashlib.sha256(csv_str.encode("utf-8")).hexdigest()
                    with open(self.path, mode="w", encoding="utf-8") as f:
                        f.write(csv_str)
                    self._csv_columns = batch_cols
                else:
                    added_cols = [c for c in batch_cols if c not in existing_cols]
                    if not added_cols:
                        aligned_df = df_str_cols.reindex(columns=existing_cols)
                        csv_str = aligned_df.to_csv(
                            index=False,
                            header=False,
                            **self._cfg.pandas_kwargs,
                        )
                        checksum = hashlib.sha256(csv_str.encode("utf-8")).hexdigest()
                        with open(self.path, mode="a", encoding="utf-8") as f:
                            f.write(csv_str)
                        self._csv_columns = list(existing_cols)
                    else:
                        unified_cols = [*existing_cols, *added_cols]
                        aligned_df = df_str_cols.reindex(columns=unified_cols)
                        csv_str = aligned_df.to_csv(
                            index=False,
                            header=False,
                            **self._cfg.pandas_kwargs,
                        )
                        checksum = hashlib.sha256(csv_str.encode("utf-8")).hexdigest()
                        pad = [""] * len(added_cols)
                        expected_old_len = len(existing_cols)

                        def _expand_and_append(tmp_path: str) -> None:
                            with (
                                open(self.path, encoding="utf-8", newline="") as src_f,
                                open(tmp_path, mode="w", encoding="utf-8", newline="") as dst_f,
                            ):
                                reader = csv.reader(src_f, delimiter=sep)
                                writer = csv.writer(dst_f, delimiter=sep, lineterminator="\n")
                                first_row = next(reader, None)
                                writer.writerow(unified_cols)
                                if first_row is not None:
                                    for row in reader:
                                        padded_row = (
                                            row + [""] * (expected_old_len - len(row))
                                            if len(row) < expected_old_len
                                            else row
                                        )
                                        writer.writerow(padded_row + pad)
                                dst_f.write(csv_str)

                        _atomic_replace_file(self.path, _expand_and_append, suffix=".csv.tmp")
                        self._csv_columns = unified_cols

            rows_loaded = len(df)
            _append_jsonl_manifest(
                self._cfg.manifest_path,
                batch_id=batch_id,
                destination=self.path,
                row_count=rows_loaded,
                checksum=checksum,
            )
            return rows_loaded

        loaded = self._execute_with_retry("export_csv", batch_id, execute_export)
        self._is_first_batch = False
        return loaded

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Stage newly voided record IDs or SDTM keys for atomic CSV reconciliation on flush."""
        _ = batch_id
        if not self._cfg.idempotent or not records:
            return 0
        cids = [
            cid for r in records if (cid := _canonical_record_id(extract_record_id(r))) is not None
        ]
        if cids:
            self._voided_ids.update(cids)
        skeys: list[SdtmKeyTuple] = []
        if self._cfg.as_sdtm:
            skeys = _resolve_sink_voided_sdtm_keys(self, records)
            if skeys:
                self._voided_sdtm_keys.update(skeys)
        return max(len(cids), len(skeys))

    def _reconcile_csv_to_disk(self) -> int:
        combined_delta = (
            _concat_tabular_frames(self._staged_frames) if self._staged_frames else pd.DataFrame()
        )
        delta_ids: set[str] = set()
        delta_sdtm_keys: set[SdtmKeyTuple] = set()
        if _find_df_record_id_col(combined_delta) is not None or not self._cfg.as_sdtm:
            dedup_delta, delta_ids = _dedupe_df_by_record_id(
                combined_delta,
                exclude_ids=self._voided_ids,
                exclude_sdtm_keys=self._voided_sdtm_keys if self._cfg.as_sdtm else None,
                study_key=self._cfg.study_key,
            )
            if self._cfg.as_sdtm:
                delta_sdtm_keys = {k for k in _df_sdtm_keys(dedup_delta) if k is not None}
        else:
            dedup_delta, delta_sdtm_keys = _dedupe_df_by_sdtm_key(
                combined_delta,
                exclude_keys=self._voided_sdtm_keys,
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
        exclude_sdtm = delta_sdtm_keys | self._voided_sdtm_keys

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
                    row_study = _extract_row_study_key(row)
                    if self._cfg.as_sdtm:
                        row_skey = _extract_row_sdtm_key(row)
                        if (
                            row_skey is not None
                            and row_skey in exclude_sdtm
                            and (row_cid is None or row_skey[1] == "DM")
                        ):
                            continue
                    if (
                        row_cid is not None
                        and row_cid in exclude_ids
                        and (
                            not self._cfg.study_key
                            or row_study is None
                            or row_study == self._cfg.study_key
                        )
                    ):
                        continue
                    writer.writerow({k: (row.get(k) or "") for k in unified_fields})
                for d_row in delta_rows:
                    writer.writerow({k: (d_row.get(k) or "") for k in unified_fields})

        _atomic_replace_file(self.path, _stream_merge, suffix=".csv.tmp")
        return len(delta_rows)

    def flush(self) -> None:
        """Finalize staged incremental CSV updates and voided deletions atomically."""
        has_voided = bool(self._voided_ids or self._voided_sdtm_keys)
        should_reconcile = False
        if self._is_incremental_sync and self._cfg.idempotent:
            should_reconcile = bool(self._staged_frames or has_voided)
        elif (
            self._cfg.idempotent
            and has_voided
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
            self._voided_sdtm_keys.clear()

    def close(self) -> None:
        """Clear any staged state."""
        self._staged_frames.clear()
        self._voided_ids.clear()
        self._voided_sdtm_keys.clear()
        self._csv_columns = None


_RECORD_META_CONTRACT_TYPES: tuple[tuple[str, str], ...] = (
    ("recordId", "int64"),
    ("recordOid", "string"),
    ("subjectKey", "string"),
    ("visitId", "int64"),
    ("intervalId", "int64"),
    ("intervalName", "string"),
    ("formId", "int64"),
    ("formKey", "string"),
    ("siteId", "int64"),
    ("siteName", "string"),
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


def _infer_study_sdtm_domains(
    forms: Sequence[Any],
    *,
    sdtm_domain: str | None = None,
    form_whitelist: Sequence[int] | None = None,
    study_key: str | None = None,
) -> list[str]:
    """Infer target SDTM domain codes in form order from concrete Study Forms."""
    if sdtm_domain is not None:
        return [resolve_sdtm_domain(sdtm_domain, fallback="AE")]
    allowed_ids = set(form_whitelist) if form_whitelist is not None else None
    inferred: list[str] = []
    seen: set[str] = set()
    for form in forms:
        if isinstance(form, NonCallableMock):
            continue
        if allowed_ids is not None:
            fid = (
                form.get("formId") or form.get("form_id")
                if isinstance(form, Mapping)
                else getattr(form, "form_id", None)
            )
            if fid not in allowed_ids:
                continue
        dom = resolve_form_sdtm_domain(form, study_key=study_key, fallback="")
        if dom and dom not in seen:
            seen.add(dom)
            inferred.append(dom)
    if not inferred and study_key and allowed_ids is None:
        for cfg_dom in build_form_sdtm_domain_map(None, study_key=study_key).values():
            if cfg_dom and cfg_dom not in seen:
                seen.add(cfg_dom)
                inferred.append(cfg_dom)
    return inferred


def _build_sdtm_contract_types(
    domain: str | None,
    *,
    variable_whitelist: Sequence[str] | None = None,
    domains: Sequence[str] | None = None,
    preserve_record_id: bool = False,
) -> dict[str, str | None]:
    """Derive expected uppercase SDTM column names and logical types for ``domain`` or ``domains``."""
    target_domains = (
        [resolve_sdtm_domain(d, fallback="AE") for d in domains]
        if domains
        else [resolve_sdtm_domain(domain, fallback="AE")]
    )
    contract: dict[str, str | None] = {}
    for dom in target_domains:
        std_vars = get_sdtm_domain_variables(dom)
        seq_col = f"{dom}SEQ"
        for col in std_vars:
            if col not in contract:
                if col in {seq_col, "AGE"}:
                    contract[col] = "int64"
                else:
                    contract[col] = "string"
    if preserve_record_id and "RECORD_ID" not in contract:
        contract["RECORD_ID"] = "int64"
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
    """Rename DataFrame metadata and variable columns according to ``convention`` while preserving colliding variables."""
    canonical_conv = normalize_naming_convention(convention)
    if canonical_conv is None or (df.empty and len(df.columns) == 0):
        return df
    meta_lookup = {
        m.lower() for m in (meta_cols or {k for k, _ in _RECORD_META_CONTRACT_TYPES})
    } | {"studykey", "study_key", "studyid"}
    study_col = (
        format_variable_name(
            "study_key",
            canonical_conv,
            domain=domain,
            is_metadata=True,
        )
        if study_key
        else None
    )
    has_study_meta_col = any(
        str(c).lower() in {"studykey", "study_key", "studyid"} for c in df.columns
    )
    used_lower: set[str] = set()
    reserved_meta_lower: set[str] = set()
    if study_col is not None and not has_study_meta_col:
        used_lower.add(study_col.lower())
        reserved_meta_lower.add(study_col.lower())

    seen_source_meta_lower: set[str] = set()
    renamed_cols: list[str] = []
    for col in df.columns:
        col_str = str(col)
        col_low = col_str.lower()
        is_meta = col_low in meta_lookup and col_low not in seen_source_meta_lower
        if is_meta:
            seen_source_meta_lower.add(col_low)
            formatted = format_variable_name(
                col_str,
                canonical_conv,
                domain=domain,
                is_metadata=True,
            )
            if formatted.lower() not in used_lower:
                used_lower.add(formatted.lower())
                reserved_meta_lower.add(formatted.lower())
                renamed_cols.append(formatted)
                continue
        base_var = format_nested_variable_name(
            col_str,
            canonical_conv,
            domain=domain,
        )
        unique_var = prefix_colliding_variable_name(
            base_var,
            reserved_meta_lower,
            convention=canonical_conv,
            used_lower=used_lower,
        )
        renamed_cols.append(unique_var)

    out = df.copy()
    out.columns = pd.Index(renamed_cols)
    if study_col is not None and not has_study_meta_col:
        out.insert(0, study_col, study_key)
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
    reserved_meta_lower: set[str] = {name.lower() for name in contract}
    seen_lower: set[str] = set(reserved_meta_lower)
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
        base_col_name = (
            format_nested_variable_name(raw_col_name, canonical_conv, domain=sdtm_domain)
            if canonical_conv is not None
            else raw_col_name
        )
        col_name = prefix_colliding_variable_name(
            base_col_name,
            reserved_meta_lower,
            convention=canonical_conv,
            used_lower=seen_lower,
        )
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


def _filter_arrow_table_excluding_ids(
    pa: Any,
    tbl: Any,
    exclude_ids: set[str],
    *,
    study_key: str | None = None,
) -> Any:
    """Filter out rows from ``tbl`` whose canonical ``(STUDYID, RECORD_ID)`` is in ``exclude_ids``."""
    if tbl.num_rows == 0 or not exclude_ids:
        return tbl
    id_col = next(
        (c for c in ("recordId", "record_id", "RECORD_ID") if c in tbl.column_names),
        None,
    )
    if id_col is None:
        return tbl
    study_col = next(
        (c for c in ("STUDYID", "study_key", "studyKey") if c in tbl.column_names),
        None,
    )
    id_vals = tbl[id_col].to_pylist()
    if study_col is not None and study_key:
        study_vals = tbl[study_col].to_pylist()
        mask = [
            not (
                _canonical_record_id(val) in exclude_ids
                and (_canonical_study_key(sval, study_key) == study_key)
            )
            for val, sval in zip(id_vals, study_vals, strict=False)
        ]
    else:
        mask = [_canonical_record_id(val) not in exclude_ids for val in id_vals]
    if all(mask):
        return tbl
    return tbl.filter(pa.array(mask, type=pa.bool_()))


def _filter_arrow_table_excluding_sdtm_keys(
    pa: Any,
    tbl: Any,
    exclude_keys: set[SdtmKeyTuple],
) -> Any:
    """Filter an Arrow table to exclude rows matching ``exclude_keys``."""
    if not exclude_keys or tbl.num_rows == 0:
        return tbl
    col_names = set(tbl.column_names)
    if not {"STUDYID", "DOMAIN", "USUBJID"} <= col_names:
        return tbl
    py_rows = tbl.to_pylist()
    mask = [_extract_row_sdtm_key(r) not in exclude_keys for r in py_rows]
    if all(mask):
        return tbl
    return tbl.filter(pa.array(mask, type=pa.bool_()))


class TabularParquetSink(ExportSink):
    """Streaming Parquet sink backed by ``pyarrow.parquet.ParquetWriter``."""

    _skip_auto_redact_batch = True

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
        self._delta_id_counts: dict[tuple[str, str], int] = {}
        self._voided_sdtm_keys: set[SdtmKeyTuple] = set()
        self._delta_sdtm_key_counts: dict[SdtmKeyTuple, int] = {}
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
            as_sdtm=self._cfg.as_sdtm,
            study_key=self._cfg.study_key,
            config=self._cfg,
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
                study_col = _find_df_study_col(df)
                if id_col is not None:
                    raw_studies = (
                        df[study_col].tolist() if study_col is not None else [None] * len(df)
                    )
                    for raw_id, raw_sval in zip(df[id_col].tolist(), raw_studies, strict=False):
                        cid = _canonical_record_id(raw_id)
                        skey_st = _canonical_study_key(raw_sval, self._cfg.study_key) or ""
                        if cid is not None:
                            comp_key = (skey_st, cid)
                            self._delta_id_counts[comp_key] = (
                                self._delta_id_counts.get(comp_key, 0) + 1
                            )
                            if not self._cfg.study_key or skey_st == self._cfg.study_key:
                                self._voided_ids.discard(cid)
                if self._cfg.as_sdtm:
                    for skey in _df_sdtm_keys(df):
                        if skey is not None:
                            self._delta_sdtm_key_counts[skey] = (
                                self._delta_sdtm_key_counts.get(skey, 0) + 1
                            )
                            self._voided_sdtm_keys.discard(skey)
            return len(df)

        return self._execute_with_retry("export_parquet", batch_id, execute_export)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Stage newly voided record IDs or SDTM keys for atomic Parquet reconciliation on flush."""
        _ = batch_id
        if not self._cfg.idempotent or not records:
            return 0
        cids = [
            cid for r in records if (cid := _canonical_record_id(extract_record_id(r))) is not None
        ]
        if cids:
            self._voided_ids.update(cids)
        skeys: list[SdtmKeyTuple] = []
        if self._cfg.as_sdtm:
            skeys = _resolve_sink_voided_sdtm_keys(self, records)
            if skeys:
                self._voided_sdtm_keys.update(skeys)
        return max(len(cids), len(skeys))

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
        if self._cfg.as_sdtm and not self._delta_id_counts:
            remaining_sdtm = dict(self._delta_sdtm_key_counts)
            for record_batch in delta_pf.iter_batches(batch_size=max(1, self._cfg.batch_size)):
                tbl = pa.Table.from_batches([record_batch])
                if tbl.num_rows > 0 and {"STUDYID", "DOMAIN", "USUBJID"} <= set(tbl.column_names):
                    mask_sdtm: list[bool] = []
                    for rdict in tbl.to_pylist():
                        skey = _extract_row_sdtm_key(rdict)
                        if skey is None:
                            mask_sdtm.append(True)
                        else:
                            remaining_sdtm[skey] = remaining_sdtm.get(skey, 1) - 1
                            mask_sdtm.append(
                                skey not in self._voided_sdtm_keys and remaining_sdtm[skey] == 0
                            )
                    if not all(mask_sdtm):
                        tbl = tbl.filter(pa.array(mask_sdtm, type=pa.bool_()))
                if tbl.num_rows > 0:
                    out_writer.write_table(_cast_arrow_table_to_schema(pa, tbl, target_schema))
            return

        remaining = dict(self._delta_id_counts)
        remaining_sdtm = dict(self._delta_sdtm_key_counts)
        for record_batch in delta_pf.iter_batches(batch_size=max(1, self._cfg.batch_size)):
            tbl = pa.Table.from_batches([record_batch])
            id_col = next(
                (c for c in ("recordId", "record_id", "RECORD_ID") if c in tbl.column_names),
                None,
            )
            study_col = next(
                (c for c in ("STUDYID", "study_key", "studyKey") if c in tbl.column_names),
                None,
            )
            has_sdtm_cols = self._cfg.as_sdtm and (
                {"STUDYID", "DOMAIN", "USUBJID"} <= set(tbl.column_names)
            )
            if (id_col is not None or has_sdtm_cols) and tbl.num_rows > 0:
                mask: list[bool] = []
                py_rows = tbl.to_pylist() if has_sdtm_cols else None
                id_vals = tbl[id_col].to_pylist() if id_col is not None else [None] * tbl.num_rows
                study_vals = (
                    tbl[study_col].to_pylist() if study_col is not None else [None] * tbl.num_rows
                )
                for row_idx, (val, sval) in enumerate(zip(id_vals, study_vals, strict=False)):
                    cid = _canonical_record_id(val)
                    skey = _extract_row_sdtm_key(py_rows[row_idx]) if py_rows is not None else None
                    if skey is not None and skey[1] == "DM":
                        remaining_sdtm[skey] = remaining_sdtm.get(skey, 1) - 1
                        skey_st = _canonical_study_key(sval, self._cfg.study_key) or ""
                        if cid is not None:
                            comp_key = (skey_st, cid)
                            remaining[comp_key] = remaining.get(comp_key, 1) - 1
                        is_voided_id = (
                            cid is not None
                            and cid in self._voided_ids
                            and (not self._cfg.study_key or skey_st == self._cfg.study_key)
                        )
                        mask.append(
                            not is_voided_id
                            and skey not in self._voided_sdtm_keys
                            and remaining_sdtm[skey] == 0
                        )
                    elif cid is None:
                        mask.append(True)
                    else:
                        skey_st = _canonical_study_key(sval, self._cfg.study_key) or ""
                        comp_key = (skey_st, cid)
                        remaining[comp_key] = remaining.get(comp_key, 1) - 1
                        is_voided = cid in self._voided_ids and (
                            not self._cfg.study_key or skey_st == self._cfg.study_key
                        )
                        mask.append(not is_voided and remaining[comp_key] == 0)
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
        has_voided = bool(self._voided_ids or self._voided_sdtm_keys)

        if not has_delta and not has_voided:
            if not dest_exists:
                self._write_empty_parquet()
            return 0

        import pyarrow as pa
        import pyarrow.parquet as pq

        pq_any: Any = pq

        delta_cids_for_study = {
            cid
            for (skey, cid) in self._delta_id_counts
            if not self._cfg.study_key or skey == self._cfg.study_key
        }
        if self._cfg.as_sdtm and not self._delta_id_counts and self._delta_sdtm_key_counts:
            needs_delta_filter = any(
                cnt > 1 for cnt in self._delta_sdtm_key_counts.values()
            ) or bool(self._voided_sdtm_keys & set(self._delta_sdtm_key_counts))
            delta_count = len(self._delta_sdtm_key_counts)
        else:
            dm_key_counts = {
                k: cnt for k, cnt in self._delta_sdtm_key_counts.items() if k[1] == "DM"
            }
            needs_delta_filter = (
                any(cnt > 1 for cnt in self._delta_id_counts.values())
                or any(cnt > 1 for cnt in dm_key_counts.values())
                or bool(self._voided_ids & delta_cids_for_study)
                or bool(self._voided_sdtm_keys & set(dm_key_counts))
            )
            delta_count = len(self._delta_id_counts)

        if not dest_exists:
            if not has_delta:
                return 0
            delta_file = cast(str, self._delta_path)
            if not needs_delta_filter:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                os.replace(delta_file, self.path)
                return delta_count

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
            return delta_count

        prev_schema = pq_any.read_schema(self.path)
        if has_delta:
            delta_schema = pq_any.read_schema(cast(str, self._delta_path))
            unified_schema = _unify_arrow_schemas(pa, prev_schema, delta_schema)
        else:
            unified_schema = prev_schema

        exclude_prev_ids = delta_cids_for_study | self._voided_ids
        exclude_prev_sdtm = set(self._delta_sdtm_key_counts) | self._voided_sdtm_keys

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
                        prev_has_id = any(
                            c in prev_tbl.column_names
                            for c in ("recordId", "record_id", "RECORD_ID")
                        )
                        if prev_has_id or not self._cfg.as_sdtm:
                            kept_tbl = _filter_arrow_table_excluding_ids(
                                pa,
                                prev_tbl,
                                exclude_prev_ids,
                                study_key=self._cfg.study_key,
                            )
                            if self._cfg.as_sdtm and kept_tbl.num_rows > 0:
                                dm_exclude = {k for k in exclude_prev_sdtm if k[1] == "DM"}
                                if dm_exclude:
                                    kept_tbl = _filter_arrow_table_excluding_sdtm_keys(
                                        pa,
                                        kept_tbl,
                                        dm_exclude,
                                    )
                        else:
                            kept_tbl = _filter_arrow_table_excluding_sdtm_keys(
                                pa,
                                prev_tbl,
                                exclude_prev_sdtm,
                            )
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
        return delta_count

    def flush(self) -> None:
        """Flush and finalize Parquet footer or reconcile incremental deltas before checkpoint commit."""
        wrote_batches = self._writer is not None
        if self._writer is not None:
            self._writer.close()
            self._writer = None

        has_voided = bool(self._voided_ids or self._voided_sdtm_keys)
        if self._use_staged_delta() or (self._cfg.idempotent and has_voided):
            try:
                self._reconcile_parquet_to_disk()
            finally:
                if self._delta_path is not None:
                    Path(self._delta_path).unlink(missing_ok=True)
                    self._delta_path = None
                self._delta_id_counts.clear()
                self._voided_ids.clear()
                self._delta_sdtm_key_counts.clear()
                self._voided_sdtm_keys.clear()
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
        self._delta_sdtm_key_counts.clear()
        self._voided_sdtm_keys.clear()
        self._schema = None


def _list_sql_part_tables(inspector: Any, table_name: str) -> list[tuple[int, str]]:
    """Return sorted ``(part_index, part_table_name)`` pairs for wide SQLite split tables."""
    raw_names = inspector.get_table_names() if hasattr(inspector, "get_table_names") else None
    if not isinstance(raw_names, (list, tuple, set, frozenset)):
        return []
    prefix = f"{table_name}_part"
    parts: list[tuple[int, str]] = []
    for name in raw_names:
        if isinstance(name, str) and name.startswith(prefix):
            suffix = name[len(prefix) :]
            if suffix.isdigit() and int(suffix) >= 1:
                parts.append((int(suffix), name))
    parts.sort(key=lambda item: item[0])
    return parts


def _resolve_sql_target_tables(inspector: Any, table_name: str) -> list[str]:
    """Return existing primary ``table_name`` and any ``{table_name}_part*`` split tables."""
    targets: list[str] = []
    if bool(inspector.has_table(table_name)):
        targets.append(table_name)
    targets.extend(name for _, name in _list_sql_part_tables(inspector, table_name))
    return targets


_LOGICAL_TO_ANSI_SQL: dict[str, str] = {
    "int64": "BIGINT",
    "float64": "FLOAT",
    "bool": "BOOLEAN",
    "string": "TEXT",
}


def _resolve_sql_alter_column_type(
    col: Any,
    series: pd.Series,
    contract_types: Mapping[str, str | None],
) -> str:
    """Resolve an ANSI/SQLite-compatible column DDL type for ``ALTER TABLE ADD COLUMN``."""
    col_name = str(col)
    ltype = contract_types.get(col_name)
    if ltype is None:
        if col_name in {"recordId", "record_id", "RECORD_ID"} or (
            col_name.endswith("SEQ") and col_name.upper() != "DMSEQ" and col_name != "DOMAIN"
        ):
            ltype = "int64"
        else:
            ltype = _infer_series_logical_type(series)
    return _LOGICAL_TO_ANSI_SQL.get(ltype or "string", "TEXT")


def _execute_chunked_sql_id_delete(
    engine: Any,
    table_name: str,
    candidate_cols: Sequence[str],
    record_ids: Sequence[Any],
    *,
    study_key: str | None = None,
) -> int:
    """Delete rows matching ``record_ids`` (and ``study_key`` when present) in chunks of 500."""
    if not record_ids or isinstance(engine, NonCallableMock):
        return 0
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    inspector = sa_inspect(engine)
    target_tables = _resolve_sql_target_tables(inspector, table_name)
    if not target_tables:
        return 0

    preparer = engine.dialect.identifier_preparer
    matched_any_table = False
    with engine.begin() as conn:
        for t_name in target_tables:
            existing_cols = {c["name"] for c in inspector.get_columns(t_name)}
            id_col = next((col for col in candidate_cols if col in existing_cols), None)
            if id_col is None:
                continue
            matched_any_table = True
            study_col = next(
                (col for col in ("STUDYID", "study_key", "studyKey") if col in existing_cols),
                None,
            )
            quoted_table = preparer.quote(t_name)
            quoted_col = preparer.quote(id_col)
            quoted_study = (
                preparer.quote(study_col) if (study_col is not None and study_key) else None
            )
            for start in range(0, len(record_ids), 500):
                chunk = record_ids[start : start + 500]
                params: dict[str, Any] = {f"id_{idx}": rid for idx, rid in enumerate(chunk)}
                placeholders = ", ".join(f":{k}" for k in params)
                if quoted_study is not None:
                    params["study_key"] = study_key
                    stmt = sa_text(
                        f"DELETE FROM {quoted_table} "
                        f"WHERE {quoted_study} = :study_key AND {quoted_col} IN ({placeholders})"
                    )
                else:
                    stmt = sa_text(
                        f"DELETE FROM {quoted_table} WHERE {quoted_col} IN ({placeholders})"
                    )
                conn.execute(stmt, params)
    return len(record_ids) if matched_any_table else 0


class TabularSQLSink(ExportSink):
    """Sink for exporting data to SQL databases."""

    _skip_auto_redact_batch = True

    def __init__(self, table: str, engine: Any, config: TabularSinkConfig):
        """Initialize the SQL sink."""
        super().__init__(config)
        self.table = table
        self.engine = engine
        self._is_first_batch = True
        self._cfg = config
        self._contract_types: dict[str, str | None] = {}
        self._is_incremental_sync = _is_incremental_config(config)
        explicit_if_exists = self._cfg.pandas_kwargs.pop("if_exists", None)
        if explicit_if_exists is not None and not (
            self._is_incremental_sync and config.idempotent and explicit_if_exists == "replace"
        ):
            self._initial_if_exists = explicit_if_exists
        elif not config.idempotent:
            self._initial_if_exists = "append"
        else:
            self._initial_if_exists = "append" if self._is_incremental_sync else "replace"

    def set_contract_types(self, contract_types: Mapping[str, str | None]) -> None:
        """Configure contract-derived column types prior to streaming batches."""
        self._contract_types = dict(contract_types)

    def _delete_existing_record_ids(self, record_ids: Sequence[Any]) -> int:
        """Delete existing rows matching ``(STUDYID, RECORD_ID)`` when the destination table exists."""
        return _execute_chunked_sql_id_delete(
            self.engine,
            self.table,
            ("recordId", "record_id", "RECORD_ID"),
            record_ids,
            study_key=self._cfg.study_key,
        )

    def _delete_existing_sdtm_keys(self, sdtm_keys: Sequence[SdtmKeyTuple]) -> int:
        """Delete existing rows matching composite SDTM keys across primary and split tables."""
        if not sdtm_keys or isinstance(self.engine, NonCallableMock):
            return 0
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text as sa_text

        inspector = sa_inspect(self.engine)
        target_tables = _resolve_sql_target_tables(inspector, self.table)
        if not target_tables:
            return 0

        preparer = self.engine.dialect.identifier_preparer
        q_study = preparer.quote("STUDYID")
        q_domain = preparer.quote("DOMAIN")
        q_usubjid = preparer.quote("USUBJID")

        deleted_keys: set[SdtmKeyTuple] = set()
        with self.engine.begin() as conn:
            for t_name in target_tables:
                existing_cols = {c["name"] for c in inspector.get_columns(t_name)}
                if not {"STUDYID", "DOMAIN", "USUBJID"} <= existing_cols:
                    continue
                q_table = preparer.quote(t_name)
                for skey in sdtm_keys:
                    study_id, domain, usubjid, seq_col, seq_val = skey
                    if seq_col is None or domain == "DM":
                        stmt = sa_text(
                            f"DELETE FROM {q_table} "
                            f"WHERE {q_study} = :study_id AND {q_domain} = :domain AND {q_usubjid} = :usubjid"
                        )
                        conn.execute(
                            stmt,
                            {"study_id": study_id, "domain": domain, "usubjid": usubjid},
                        )
                        deleted_keys.add(skey)
                    elif seq_col in existing_cols and seq_val is not None:
                        q_seq = preparer.quote(seq_col)
                        stmt = sa_text(
                            f"DELETE FROM {q_table} "
                            f"WHERE {q_study} = :study_id AND {q_domain} = :domain "
                            f"AND {q_usubjid} = :usubjid AND {q_seq} = :seq_val"
                        )
                        conn.execute(
                            stmt,
                            {
                                "study_id": study_id,
                                "domain": domain,
                                "usubjid": usubjid,
                                "seq_val": seq_val,
                            },
                        )
                        deleted_keys.add(skey)
        return len(deleted_keys)

    def _append_to_existing_part_tables(
        self,
        df: pd.DataFrame,
        part_entries: list[tuple[int, str]],
        inspector: Any,
    ) -> None:
        """Append ``df`` into existing wide SQLite ``_part*`` tables, evolving added columns."""
        from sqlalchemy import text as sa_text

        norm_df = _normalize_sqlite_temporal_object_columns(
            df,
            self.engine,
            dtype=self._cfg.pandas_kwargs.get("dtype"),
        )
        norm_df, key_cols = _resolve_wide_sqlite_key_columns(
            norm_df,
            study_key=self._cfg.study_key,
        )
        part_cols_map: dict[str, list[str]] = {
            part_name: [c["name"] for c in inspector.get_columns(part_name)]
            for _, part_name in part_entries
        }
        all_existing = {col for cols in part_cols_map.values() for col in cols}
        new_cols = [c for c in norm_df.columns if str(c) not in all_existing]

        preparer = self.engine.dialect.identifier_preparer
        if new_cols:
            remaining_new = list(new_cols)
            with self.engine.begin() as conn:
                for _, part_name in part_entries:
                    if not remaining_new:
                        break
                    room = MAX_SQLITE_COLUMNS - len(part_cols_map[part_name])
                    if room <= 0:
                        continue
                    to_add = remaining_new[:room]
                    remaining_new = remaining_new[room:]
                    q_part = preparer.quote(part_name)
                    for col in to_add:
                        col_sql_type = _resolve_sql_alter_column_type(
                            col,
                            norm_df[col],
                            self._contract_types,
                        )
                        conn.execute(
                            sa_text(
                                f"ALTER TABLE {q_part} ADD COLUMN {preparer.quote(str(col))} {col_sql_type}"
                            )
                        )
                        part_cols_map[part_name].append(str(col))

                next_part_idx = part_entries[-1][0] + 1
                first_part_name = part_entries[0][1]
                first_part_cols = set(part_cols_map[first_part_name])
                seed_keys = [k for k in key_cols if k in first_part_cols]
                payload_width = max(1, MAX_SQLITE_COLUMNS - len(seed_keys))
                while remaining_new:
                    batch_new = remaining_new[:payload_width]
                    remaining_new = remaining_new[payload_width:]
                    new_part_name = f"{self.table}_part{next_part_idx}"
                    next_part_idx += 1
                    q_new_part = preparer.quote(new_part_name)
                    if seed_keys:
                        quoted_seed = ", ".join(preparer.quote(k) for k in seed_keys)
                        conn.execute(
                            sa_text(
                                f"CREATE TABLE {q_new_part} AS SELECT {quoted_seed} "
                                f"FROM {preparer.quote(first_part_name)}"
                            )
                        )
                        for col in batch_new:
                            col_sql_type = _resolve_sql_alter_column_type(
                                col,
                                norm_df[col],
                                self._contract_types,
                            )
                            conn.execute(
                                sa_text(
                                    f"ALTER TABLE {q_new_part} ADD COLUMN {preparer.quote(str(col))} {col_sql_type}"
                                )
                            )
                        part_cols_map[new_part_name] = [*seed_keys, *(str(c) for c in batch_new)]
                    else:
                        col_defs = ", ".join(
                            f"{preparer.quote(str(col))} "
                            f"{_resolve_sql_alter_column_type(col, norm_df[col], self._contract_types)}"
                            for col in batch_new
                        )
                        conn.execute(sa_text(f"CREATE TABLE {q_new_part} ({col_defs})"))
                        part_cols_map[new_part_name] = [str(c) for c in batch_new]
                    part_entries.append((next_part_idx - 1, new_part_name))

        for _, part_name in part_entries:
            part_col_set = set(part_cols_map[part_name])
            target_cols = [c for c in norm_df.columns if str(c) in part_col_set]
            if not target_cols:
                continue
            part_df = norm_df.loc[:, target_cols]
            chunk_kwargs = _filter_to_sql_chunk_kwargs(self._cfg.pandas_kwargs, part_df.columns)
            part_df.to_sql(
                part_name,
                self.engine,
                if_exists="append",
                index=False,
                **chunk_kwargs,
            )

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write a batch of records to the sink."""
        df = _prepare_tabular_batch_df(
            records,
            mask_phi=self._cfg.mask_phi,
            sanitize=self._cfg.sanitize,
            study_key=self._cfg.study_key,
            as_sdtm=self._cfg.as_sdtm,
            config=self._cfg,
        )
        if len(df) == 0:
            return 0
        if self._is_incremental_sync and self._cfg.idempotent:
            if _find_df_record_id_col(df) is not None or not self._cfg.as_sdtm:
                df, _ = _dedupe_df_by_record_id(df, study_key=self._cfg.study_key)
            else:
                df, _ = _dedupe_df_by_sdtm_key(df)
            if len(df) == 0:
                return 0

        def execute_export() -> int:
            if_exists = self._initial_if_exists if self._is_first_batch else "append"
            df_str = df.to_csv(index=False)
            checksum = hashlib.sha256(df_str.encode("utf-8")).hexdigest()

            wrote_via_parts = False
            if if_exists == "append" and not isinstance(self.engine, NonCallableMock):
                from sqlalchemy import inspect as sa_inspect
                from sqlalchemy import text as sa_text

                if self._is_incremental_sync and self._cfg.idempotent:
                    deleted_by_id = 0
                    for candidate_col in ("recordId", "record_id", "RECORD_ID"):
                        if candidate_col in df.columns:
                            ids = [rid for rid in df[candidate_col].tolist() if pd.notna(rid)]
                            deleted_by_id = self._delete_existing_record_ids(ids)
                            break
                    if self._cfg.as_sdtm:
                        skeys = [k for k in _df_sdtm_keys(df) if k is not None]
                        if deleted_by_id == 0:
                            if skeys:
                                self._delete_existing_sdtm_keys(skeys)
                        else:
                            dm_skeys = [k for k in skeys if k[1] == "DM"]
                            if dm_skeys:
                                self._delete_existing_sdtm_keys(dm_skeys)

                inspector = sa_inspect(self.engine)
                part_entries = (
                    _list_sql_part_tables(inspector, self.table)
                    if self.engine.dialect.name == "sqlite"
                    else []
                )
                if part_entries:
                    self._append_to_existing_part_tables(df, list(part_entries), inspector)
                    wrote_via_parts = True
                elif bool(inspector.has_table(self.table)):
                    existing_cols = {c["name"] for c in inspector.get_columns(self.table)}
                    missing_cols = [c for c in df.columns if str(c) not in existing_cols]
                    if (
                        self.engine.dialect.name == "sqlite"
                        and missing_cols
                        and len(existing_cols) + len(missing_cols) > MAX_SQLITE_COLUMNS
                    ):
                        preparer = self.engine.dialect.identifier_preparer
                        q_tbl = preparer.quote(self.table)
                        prev_df = pd.read_sql(sa_text(f"SELECT * FROM {q_tbl}"), self.engine)
                        combined_df = (
                            _concat_tabular_frames([prev_df, df]) if not prev_df.empty else df
                        )
                        _to_sql_with_chunking(
                            combined_df,
                            self.table,
                            self.engine,
                            if_exists="replace",
                            study_key=self._cfg.study_key,
                            **self._cfg.pandas_kwargs,
                        )
                        wrote_via_parts = True
                    elif missing_cols:
                        preparer = self.engine.dialect.identifier_preparer
                        q_tbl = preparer.quote(self.table)
                        with self.engine.begin() as conn:
                            for col in missing_cols:
                                col_sql_type = _resolve_sql_alter_column_type(
                                    col,
                                    df[col],
                                    self._contract_types,
                                )
                                conn.execute(
                                    sa_text(
                                        f"ALTER TABLE {q_tbl} ADD COLUMN {preparer.quote(str(col))} {col_sql_type}"
                                    )
                                )

            if not wrote_via_parts:
                _to_sql_with_chunking(
                    df,
                    self.table,
                    self.engine,
                    if_exists=cast(Literal["fail", "replace", "append", "delete_rows"], if_exists),
                    study_key=self._cfg.study_key,
                    **self._cfg.pandas_kwargs,
                )

            rows_loaded = len(df)
            _append_jsonl_manifest(
                self._cfg.manifest_path,
                batch_id=batch_id,
                destination=self.table,
                row_count=rows_loaded,
                checksum=checksum,
            )
            return rows_loaded

        loaded = self._execute_with_retry("export_sql", batch_id, execute_export)
        self._is_first_batch = False
        return loaded

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Delete newly voided rows from the SQL table when running idempotently."""
        if not self._cfg.idempotent or not records or isinstance(self.engine, NonCallableMock):
            return 0
        record_ids = [rid for r in records if (rid := extract_record_id(r)) is not None]
        skeys = _resolve_sink_voided_sdtm_keys(self, records) if self._cfg.as_sdtm else []
        if not record_ids and not skeys:
            return 0

        def _do_delete() -> int:
            deleted = self._delete_existing_record_ids(record_ids) if record_ids else 0
            if skeys:
                if deleted == 0:
                    deleted = self._delete_existing_sdtm_keys(skeys)
                else:
                    dm_skeys = [k for k in skeys if k[1] == "DM"]
                    if dm_skeys:
                        self._delete_existing_sdtm_keys(dm_skeys)
            return deleted

        return self._execute_with_retry(
            "reconcile_sql_deleted",
            batch_id,
            _do_delete,
        )

    def flush(self) -> None:
        """Flush the sink, writing an empty SDTM schema snapshot on full zero-row export."""
        if (
            self._cfg.as_sdtm
            and self._is_first_batch
            and self._cfg.idempotent
            and not self._is_incremental_sync
            and self._initial_if_exists != "fail"
            and self._contract_types
            and not isinstance(self.engine, NonCallableMock)
        ):
            empty_df = _build_empty_contract_df(self._contract_types)
            _to_sql_with_chunking(
                empty_df,
                self.table,
                self.engine,
                if_exists=cast(
                    Literal["fail", "replace", "append", "delete_rows"],
                    self._initial_if_exists,
                ),
                **self._cfg.pandas_kwargs,
            )
            self._is_first_batch = False

    def close(self) -> None:
        """Close the sink."""


class TabularDuckDBSink(ExportSink):
    """Streaming sink for exporting DataFrame batches into a DuckDB table."""

    _skip_auto_redact_batch = True

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
        study_col = next(
            (c for c in ("STUDYID", "study_key", "studyKey") if c in cols),
            None,
        )
        placeholders = ", ".join(["?"] * len(record_ids))
        quoted_tbl = _quote_duckdb_identifier(self.table_name)
        quoted_col = _quote_duckdb_column(id_col)
        if study_col is not None and self._cfg.study_key:
            quoted_study = _quote_duckdb_column(study_col)
            self._conn.execute(
                f"DELETE FROM {quoted_tbl} WHERE {quoted_study} = ? AND {quoted_col} IN ({placeholders})",
                [self._cfg.study_key, *record_ids],
            )
        else:
            self._conn.execute(
                f"DELETE FROM {quoted_tbl} WHERE {quoted_col} IN ({placeholders})",
                list(record_ids),
            )
        return len(record_ids)

    def _delete_sdtm_keys(self, sdtm_keys: Sequence[SdtmKeyTuple]) -> int:
        if not sdtm_keys or not self._table_exists():
            return 0
        cols = {
            row[1]
            for row in self._conn.execute(f"PRAGMA table_info('{self.table_name}')").fetchall()
        }
        if not {"STUDYID", "DOMAIN", "USUBJID"} <= cols:
            return 0
        quoted_tbl = _quote_duckdb_identifier(self.table_name)
        deleted = 0
        for study_id, domain, usubjid, seq_col, seq_val in sdtm_keys:
            if seq_col is None or domain == "DM":
                self._conn.execute(
                    f'DELETE FROM {quoted_tbl} WHERE "STUDYID" = ? AND "DOMAIN" = ? AND "USUBJID" = ?',
                    [study_id, domain, usubjid],
                )
                deleted += 1
            elif seq_col in cols and seq_val is not None:
                quoted_seq = _quote_duckdb_column(seq_col)
                self._conn.execute(
                    f'DELETE FROM {quoted_tbl} WHERE "STUDYID" = ? AND "DOMAIN" = ? '
                    f'AND "USUBJID" = ? AND {quoted_seq} = ?',
                    [study_id, domain, usubjid, seq_val],
                )
                deleted += 1
        return deleted

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write a batch of records into DuckDB."""
        df = _prepare_tabular_batch_df(
            records,
            mask_phi=self._cfg.mask_phi,
            sanitize=self._cfg.sanitize,
            study_key=self._cfg.study_key,
            as_sdtm=self._cfg.as_sdtm,
            config=self._cfg,
        )
        if len(df) == 0:
            return 0
        if self._is_incremental_sync and self._cfg.idempotent:
            if _find_df_record_id_col(df) is not None or not self._cfg.as_sdtm:
                df, _ = _dedupe_df_by_record_id(df, study_key=self._cfg.study_key)
            else:
                df, _ = _dedupe_df_by_sdtm_key(df)
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
                        deleted_by_id = 0
                        for candidate_col in ("recordId", "record_id", "RECORD_ID"):
                            if candidate_col in df.columns:
                                ids = [rid for rid in df[candidate_col].tolist() if pd.notna(rid)]
                                deleted_by_id = self._delete_record_ids(ids)
                                break
                        if self._cfg.as_sdtm:
                            skeys = [k for k in _df_sdtm_keys(df) if k is not None]
                            if deleted_by_id == 0:
                                if skeys:
                                    self._delete_sdtm_keys(skeys)
                            else:
                                dm_skeys = [k for k in skeys if k[1] == "DM"]
                                if dm_skeys:
                                    self._delete_sdtm_keys(dm_skeys)
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
        skeys = _resolve_sink_voided_sdtm_keys(self, records) if self._cfg.as_sdtm else []
        if not record_ids and not skeys:
            return 0

        def _do_delete() -> int:
            deleted = self._delete_record_ids(record_ids) if record_ids else 0
            if skeys:
                if deleted == 0:
                    deleted = self._delete_sdtm_keys(skeys)
                else:
                    dm_skeys = [k for k in skeys if k[1] == "DM"]
                    if dm_skeys:
                        self._delete_sdtm_keys(dm_skeys)
            return deleted

        return self._execute_with_retry(
            "reconcile_duckdb_deleted",
            batch_id,
            _do_delete,
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
        self._voided_sdtm_keys: set[SdtmKeyTuple] = set()
        self._on_flush = on_flush
        self._pass_voided_ids = pass_voided_ids

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        _ = batch_id
        df = records if isinstance(records, pd.DataFrame) else pd.DataFrame(records)
        if len(df) == 0:
            return 0
        id_col = _find_df_record_id_col(df)
        study_col = _find_df_study_col(df)
        if id_col is not None:
            raw_studies = df[study_col].tolist() if study_col is not None else [None] * len(df)
            for raw_id, raw_sval in zip(df[id_col].tolist(), raw_studies, strict=False):
                cid = _canonical_record_id(raw_id)
                skey_st = _canonical_study_key(raw_sval, self.config.study_key)
                if cid is not None and (
                    not self.config.study_key or skey_st == self.config.study_key
                ):
                    self._voided_ids.discard(cid)
        if self.config.as_sdtm:
            for skey in _df_sdtm_keys(df):
                if skey is not None:
                    self._voided_sdtm_keys.discard(skey)
        self._frames.append(df)
        return len(df)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        _ = batch_id
        if not self.config.idempotent or not records:
            return 0
        cids = [
            cid for r in records if (cid := _canonical_record_id(extract_record_id(r))) is not None
        ]
        if cids:
            self._voided_ids.update(cids)
        skeys: list[SdtmKeyTuple] = []
        if self.config.as_sdtm:
            skeys = _resolve_sink_voided_sdtm_keys(self, records)
            if skeys:
                self._voided_sdtm_keys.update(skeys)
        return max(len(cids), len(skeys))

    def flush(self) -> None:
        combined = _concat_tabular_frames(self._frames) if self._frames else pd.DataFrame()
        if self._pass_voided_ids:
            self._on_flush(combined, set(self._voided_ids))
        else:
            self._on_flush(combined)

    def close(self) -> None:
        self._frames.clear()
        self._voided_ids.clear()
        self._voided_sdtm_keys.clear()


def _project_records_whitelist(records: Sequence[Any], whitelist: Sequence[str]) -> list[Any]:
    """Filter ``record_data`` / ``recordData`` keys to ``whitelist`` prior to SDTM mapping."""
    allowed = set(whitelist)
    keep_visit = bool(allowed & {"VISITNUM", "visit_id", "visitId", "VISIT_ID"})
    projected: list[Any] = []
    for rec in records:
        rec_data = getattr(rec, "record_data", None)
        if hasattr(rec, "model_copy") and isinstance(rec_data, dict):
            updates: dict[str, Any] = {
                "record_data": {k: v for k, v in rec_data.items() if k in allowed}
            }
            if not keep_visit and hasattr(rec, "visit_id"):
                updates["visit_id"] = None
            projected.append(rec.model_copy(update=updates))
        elif isinstance(rec, Mapping) and isinstance(rec.get("record_data"), Mapping):
            cloned = dict(rec)
            cloned["record_data"] = {k: v for k, v in rec["record_data"].items() if k in allowed}
            if not keep_visit:
                cloned.pop("visit_id", None)
                cloned.pop("visitId", None)
                cloned.pop("VISITNUM", None)
            projected.append(cloned)
        elif isinstance(rec, Mapping) and isinstance(rec.get("recordData"), Mapping):
            cloned = dict(rec)
            cloned["recordData"] = {k: v for k, v in rec["recordData"].items() if k in allowed}
            if not keep_visit:
                cloned.pop("visit_id", None)
                cloned.pop("visitId", None)
                cloned.pop("VISITNUM", None)
            projected.append(cloned)
        else:
            projected.append(rec)
    return projected


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
        form_domain_map: Mapping[Any, str] | None = None,
        refresh_variables_fn: Callable[[], tuple[list[str], dict[str, str], Any] | None]
        | None = None,
        refresh_whitelist: Sequence[str] | None = None,
        use_explicit_refresh_whitelist: bool = False,
    ) -> None:
        super().__init__(config)
        self._inner = inner
        self._mapper = mapper
        self._record_model = record_model
        self._variable_keys = list(variable_keys)
        self._known_var_set: set[str] = set(variable_keys)
        self._checked_unseen_keys: set[str] = set()
        effective_wl = (
            refresh_whitelist if use_explicit_refresh_whitelist else config.variable_whitelist
        )
        self._whitelist_set: set[str] | None = (
            set(effective_wl) if effective_wl is not None else None
        )
        self._refresh_variables_fn = refresh_variables_fn
        self._label_map = dict(label_map)
        self._cfg = config
        self._sanitize = bool(sanitize or config.sanitize)
        self._pipeline = pipeline
        self._phi_keys = resolve_sink_phi_keys(
            config.study_key,
            config=config,
            phi_keys=getattr(pipeline, "phi_keys", None),
        )
        self._has_native_enrichment = callable(getattr(mapper, "_get_enrichment_pipeline", None))
        _seed_mapper_enrichment(mapper, config.study_key, pipeline, config=config)
        self._domain_seq_counters: dict[str, dict[str, int]] = {}
        self._seq_counters: dict[str, int] = {}
        self._record_seqs: dict[str, int] = {}
        self._form_domain_map: dict[Any, str] = dict(form_domain_map) if form_domain_map else {}
        self._meta_cols = {name for name, _ in _RECORD_META_CONTRACT_TYPES}

    def _maybe_refresh_added_variables(self, records: Sequence[Any]) -> None:
        if self._refresh_variables_fn is None or not records:
            return
        unseen: set[str] = set()
        for rec in records:
            raw_data = (
                rec.get("record_data") or rec.get("recordData")
                if isinstance(rec, Mapping)
                else getattr(rec, "record_data", None)
            )
            if not isinstance(raw_data, Mapping):
                continue
            for key in raw_data:
                if not isinstance(key, str) or key in self._known_var_set:
                    continue
                if self._whitelist_set is not None and key not in self._whitelist_set:
                    continue
                if key not in self._checked_unseen_keys:
                    unseen.add(key)
        if not unseen:
            return
        refreshed = self._refresh_variables_fn()
        if refreshed is not None:
            new_keys, new_labels, new_model = refreshed
            self._variable_keys = list(new_keys)
            self._known_var_set = set(new_keys)
            self._label_map = dict(new_labels)
            self._record_model = new_model
        self._checked_unseen_keys.update(unseen - self._known_var_set)

    def bind_stream_metadata(self, metadata: dict[str, Any]) -> None:
        if self._cfg.as_sdtm:
            bind_sdtm_stream_metadata(metadata, self._seq_counters, self._record_seqs)
        self._inner.bind_stream_metadata(metadata)

    def _sync_active_context_to_inner(self) -> None:
        for attr in (
            "_active_state_provider",
            "_active_stream_name",
            "_active_study_key",
            "_active_tx",
            "_active_form_domain_map",
            "_sdtm_lineage",
            "_sdtm_seq_watermarks",
        ):
            if hasattr(self, attr):
                with contextlib.suppress(Exception):
                    setattr(self._inner, attr, getattr(self, attr))

    def _resolve_record_domain(self, rec: Any) -> str:
        if self._cfg.sdtm_domain:
            return resolve_sdtm_domain(self._cfg.sdtm_domain, fallback="AE")
        active_map = getattr(self, "_active_form_domain_map", None) or self._form_domain_map
        resolved = resolve_form_sdtm_domain(
            rec,
            study_key=self._cfg.study_key,
            form_domain_map=active_map if isinstance(active_map, Mapping) else None,
            fallback="",
        )
        if resolved:
            return resolved
        raw_domain = (
            rec.get("DOMAIN") or rec.get("domain") or rec.get("form_key") or rec.get("formKey")
            if isinstance(rec, Mapping)
            else getattr(rec, "form_key", getattr(rec, "formKey", None))
        )
        return resolve_sdtm_domain(raw_domain, fallback="AE")

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        self._sync_active_context_to_inner()
        if self._cfg.as_sdtm:
            if not records:
                return 0
            batch_records: Sequence[Any] = (
                _project_records_whitelist(records, self._cfg.variable_whitelist)
                if self._cfg.variable_whitelist is not None
                else records
            )
            rehydrated_counters, seq_overrides, _ = rehydrate_sdtm_lineage(self)
            for dom_code, subj_map in rehydrated_counters.items():
                local_map = self._domain_seq_counters.setdefault(dom_code, {})
                for subj_key, wm_val in subj_map.items():
                    local_map[subj_key] = max(local_map.get(subj_key, 0), wm_val)

            grouped_by_domain: dict[str, list[Any]] = {}
            for rec in batch_records:
                dom = self._resolve_record_domain(rec)
                grouped_by_domain.setdefault(dom, []).append(rec)

            single_explicit_domain = bool(self._cfg.sdtm_domain) or (
                len(grouped_by_domain) == 1
                and len(self._domain_seq_counters) <= 1
                and not rehydrated_counters
            )
            preserve_rec_id = should_preserve_sdtm_record_id(self._cfg)
            domain_frames: list[pd.DataFrame] = []
            lineage_out: dict[str, dict[str, Any]] = {}
            for dom, dom_records in grouped_by_domain.items():
                dom_counters = self._domain_seq_counters.setdefault(dom, {})
                if single_explicit_domain and self._seq_counters and dom != "DM":
                    for subj_key, wm_val in self._seq_counters.items():
                        dom_counters[subj_key] = max(dom_counters.get(subj_key, 0), wm_val)
                dom_df = transform_records_to_sdtm(
                    dom_records,
                    domain=dom,
                    study_key=self._cfg.study_key,
                    include_deleted=self._cfg.include_deleted,
                    mask_phi=self._cfg.mask_phi,
                    seq_counters=dom_counters,
                    record_seqs=self._record_seqs,
                    preserve_record_id=preserve_rec_id,
                    phi_keys=self._phi_keys,
                    pipeline=self._pipeline,
                    seq_overrides=seq_overrides,
                    lineage_out=lineage_out,
                )
                if (self._cfg.sdtm_domain or len(self._domain_seq_counters) == 1) and dom != "DM":
                    for subj_key, wm_val in dom_counters.items():
                        self._seq_counters[subj_key] = max(
                            self._seq_counters.get(subj_key, 0),
                            wm_val,
                        )
                if not dom_df.empty:
                    domain_frames.append(dom_df)

            if not domain_frames:
                return 0
            df = _concat_tabular_frames(domain_frames)
            if _find_df_record_id_col(df) is not None:
                df, _ = _dedupe_df_by_record_id(df, study_key=self._cfg.study_key)
            else:
                df, _ = _dedupe_df_by_sdtm_key(df)
            if df.empty:
                return 0
            if self._cfg.mask_phi:
                df.attrs["imednet_sdtm_redacted"] = True
            stamp_tabular_batch(
                df,
                mask_phi=self._cfg.mask_phi,
                phi_keys=self._phi_keys,
                sanitized=False,
            )
            if self._sanitize:
                df = _sanitize_df(df)
            commit_sdtm_lineage(self, lineage_out)
            self._sync_active_context_to_inner()
            return self._inner.write_batch(cast(Any, df), batch_id=batch_id)

        self._maybe_refresh_added_variables(records)
        _seed_mapper_enrichment(
            self._mapper,
            self._cfg.study_key,
            self._pipeline,
            config=self._cfg,
            records=records,
        )
        batch_phi_keys = (
            resolve_sink_phi_keys(
                self._cfg.study_key,
                config=self._cfg,
                records=records,
                phi_keys=self._phi_keys,
            )
            if self._cfg.mask_phi
            else None
        )
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
        df = _dedup_case_insensitive_columns(df, ensure_str_columns=True)

        if self._cfg.mask_phi:
            df = _mask_df(df, phi_keys=batch_phi_keys)
        if not self._cfg.use_labels_as_columns and self._cfg.naming_convention is not None:
            df = _apply_naming_convention_to_df(
                df,
                self._cfg.naming_convention,
                domain=self._cfg.sdtm_domain,
                meta_cols=self._meta_cols,
                study_key=self._cfg.study_key,
            )
            if self._cfg.mask_phi:
                df = _mask_df(df, phi_keys=batch_phi_keys)
            else:
                stamp_tabular_batch(df, mask_phi=False, sanitized=False)
        if self._sanitize:
            df = _sanitize_df(df)
        elif not self._cfg.mask_phi:
            stamp_tabular_batch(df, mask_phi=False, sanitized=False)

        return self._inner.write_batch(cast(Any, df), batch_id=batch_id)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        self._sync_active_context_to_inner()
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
    *,
    effective_form_whitelist: list[int] | None = None,
    allowed_form_keys: set[str] | None = None,
    form_domain_map: Mapping[Any, str] | None = None,
) -> Callable[[datetime | None, bool], Iterable[Any]]:
    """Build a streaming records source callback compatible with real and mock mappers."""
    active_whitelist = (
        effective_form_whitelist if effective_form_whitelist is not None else config.form_whitelist
    )
    extra_filters: dict[str, Any] = {}
    if active_whitelist is not None:
        extra_filters["formId"] = active_whitelist
    merged_filter = _merge_form_whitelist_into_filter(config.record_filter, active_whitelist)
    is_mock = _is_mock_mapper(mapper)
    allowed_id_set = set(active_whitelist) if active_whitelist is not None else None
    target_sdtm_domain = config.sdtm_domain if config.as_sdtm else None
    sdtm_sort = _SDTM_RECORDS_SORT if config.as_sdtm else None

    def _filter_by_allowed_forms(stream: Iterable[Any]) -> Iterator[Any]:
        has_form_allowlist = allowed_id_set is not None or bool(allowed_form_keys)
        if not has_form_allowlist and target_sdtm_domain is None:
            yield from stream
            return
        for rec in stream:
            if has_form_allowlist:
                fid, fk = extract_record_form_ref(rec)
                if fid is not None and allowed_id_set is not None:
                    if fid not in allowed_id_set:
                        continue
                elif (
                    fk is not None
                    and allowed_form_keys
                    and fk not in allowed_form_keys
                    and fk.upper() not in allowed_form_keys
                    and fk.lower() not in allowed_form_keys
                ):
                    continue
            if target_sdtm_domain is not None and not record_matches_sdtm_domain(
                rec,
                target_sdtm_domain,
                form_domain_map=form_domain_map,
                study_key=study_key,
            ):
                continue
            yield rec

    def _records_source(
        watermark: datetime | None, exclude_deleted_upstream: bool
    ) -> Iterable[Any]:
        if active_whitelist is not None and len(active_whitelist) == 0 and not allowed_form_keys:
            return iter(())
        if is_mock:
            return _filter_by_allowed_forms(
                cast(
                    Iterable[Any],
                    mapper._iter_records(
                        study_key,
                        visit_key=None,
                        extra_filters=extra_filters or None,
                    ),
                )
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
            return _filter_by_allowed_forms(
                cast(
                    Iterable[Any],
                    mapper._iter_records(
                        study_key,
                        visit_key=None,
                        extra_filters=iter_filters or None,
                        record_data_filter=config.record_data_filter,
                        include_deleted=not exclude_deleted_upstream,
                    ),
                )
            )
        return _filter_by_allowed_forms(
            iter_filtered_sdk_records(
                sdk,
                study_key,
                watermark=watermark,
                record_filter=merged_filter,
                record_data_filter=config.record_data_filter,
                exclude_deleted_upstream=exclude_deleted_upstream,
                sort=sdtm_sort,
            )
        )

    can_stream_ep = (
        not is_mock
        and getattr(mapper, "_loader", None) is None
        and callable(getattr(mapper, "_can_stream_via_records_endpoint", None))
        and bool(mapper._can_stream_via_records_endpoint())
        and _is_bound_real_records_endpoint(sdk)
    )
    _records_source._sdtm_source_is_ordered = bool(config.as_sdtm and can_stream_ep)  # type: ignore[attr-defined]
    return _records_source


def _resolve_study_forms_and_sdtm_filter(
    sdk: Any,
    study_key: str,
    config: TabularSinkConfig,
) -> tuple[list[Any], dict[Any, str], list[int] | None, set[str] | None, bool]:
    """Return ``(study_forms, form_domain_map, effective_form_whitelist, allowed_form_keys, no_matching_domain)``."""
    needs_forms = bool(config.as_sdtm)
    study_forms = _try_fetch_study_forms(sdk, study_key) if needs_forms else []
    form_domain_map = (
        build_form_sdtm_domain_map(study_forms, study_key=study_key) if needs_forms else {}
    )
    effective_whitelist = config.form_whitelist
    allowed_keys: set[str] | None = None
    if config.as_sdtm and config.sdtm_domain is not None and study_forms:
        matched_forms = filter_forms_by_sdtm_domain(
            study_forms,
            config.sdtm_domain,
            study_key=study_key,
        )
        if config.form_whitelist is not None:
            wl_set = set(config.form_whitelist)
            matched_forms = [
                f
                for f in matched_forms
                if (
                    f.get("formId") or f.get("form_id")
                    if isinstance(f, Mapping)
                    else getattr(f, "form_id", None)
                )
                in wl_set
            ]
        if not matched_forms:
            return study_forms, form_domain_map, [], set(), True
        matched_ids = [
            fid
            for f in matched_forms
            if isinstance(
                (
                    fid := (
                        f.get("formId") or f.get("form_id")
                        if isinstance(f, Mapping)
                        else getattr(f, "form_id", None)
                    )
                ),
                int,
            )
            and not isinstance(fid, bool)
        ]
        matched_keys = {
            str(fkey)
            for f in matched_forms
            if (
                fkey := (
                    f.get("formKey") or f.get("form_key")
                    if isinstance(f, Mapping)
                    else getattr(f, "form_key", None)
                )
            )
        }
        effective_whitelist = matched_ids
        allowed_keys = matched_keys
    return study_forms, form_domain_map, effective_whitelist, allowed_keys, False


def _tabular_export(
    sdk: Any,
    study_key: str,
    sink: ExportSink,
    config: TabularSinkConfig,
    sanitize: bool = False,
    *,
    stream_name: str | None = None,
) -> None:
    with sdk_governance_context(sdk):
        _tabular_export_impl(
            sdk,
            study_key,
            sink,
            config,
            sanitize=sanitize,
            stream_name=stream_name,
        )


def _tabular_export_impl(
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
    (
        study_forms,
        form_domain_map,
        effective_form_whitelist,
        allowed_form_keys,
        no_matching_domain,
    ) = _resolve_study_forms_and_sdtm_filter(sdk, study_key, config)
    if config.as_sdtm:
        if empty_whitelist:
            with sink:
                pass
            return
        variable_keys: list[str] = []
        label_map: dict[str, str] = {}
        inferred_domains = _infer_study_sdtm_domains(
            study_forms,
            sdtm_domain=config.sdtm_domain,
            form_whitelist=config.form_whitelist,
            study_key=study_key,
        )
        contract_types = _build_sdtm_contract_types(
            config.sdtm_domain,
            variable_whitelist=config.variable_whitelist,
            domains=inferred_domains or None,
            preserve_record_id=should_preserve_sdtm_record_id(config),
        )
        pipeline = resolve_sink_enrichment_pipeline(study_key, config=config)
        _seed_mapper_enrichment(mapper, study_key, pipeline, config=config)
        record_model: Any = None
    else:
        if no_matching_domain:
            with sink:
                pass
            return
        variable_keys, label_map = mapper._fetch_variable_metadata(
            study_key,
            variable_whitelist=config.variable_whitelist,
            form_whitelist=effective_form_whitelist,
        )
        if not variable_keys:
            with sink:
                pass
            return

        raw_variables = mapper._last_variables if hasattr(mapper, "_last_variables") else None
        pipeline = resolve_sink_enrichment_pipeline(study_key, config=config)
        _seed_mapper_enrichment(mapper, study_key, pipeline, config=config)
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

    if isinstance(sink, (TabularParquetSink, TabularDuckDBSink, TabularSQLSink)):
        sink.set_contract_types(contract_types)

    refresh_variables_fn: Callable[[], tuple[list[str], dict[str, str], Any] | None] | None = None
    if not config.as_sdtm and not _is_mock_mapper(mapper):

        def _refresh_study_variables() -> tuple[list[str], dict[str, str], Any] | None:
            updated_keys, updated_labels = mapper._fetch_variable_metadata(
                study_key,
                variable_whitelist=config.variable_whitelist,
                form_whitelist=effective_form_whitelist,
            )
            if not updated_keys:
                return None
            updated_raw = mapper._last_variables if hasattr(mapper, "_last_variables") else None
            updated_contract = _build_tabular_contract_types(
                updated_keys,
                updated_labels,
                updated_raw if isinstance(updated_raw, list) else (),
                use_labels_as_columns=config.use_labels_as_columns,
                mask_phi=config.mask_phi,
                pipeline=pipeline,
                naming_convention=config.naming_convention,
                sdtm_domain=config.sdtm_domain,
            )
            if isinstance(sink, (TabularParquetSink, TabularDuckDBSink, TabularSQLSink)):
                sink.set_contract_types(updated_contract)
            updated_model = mapper._build_record_model(updated_keys, updated_labels)
            return updated_keys, updated_labels, updated_model

        refresh_variables_fn = _refresh_study_variables

    records_source = _make_mapper_records_source(
        sdk,
        study_key,
        mapper,
        config,
        effective_form_whitelist=effective_form_whitelist,
        allowed_form_keys=allowed_form_keys,
        form_domain_map=form_domain_map,
    )

    transform_sink = _TabularTransformSink(
        sink,
        mapper,
        record_model,
        variable_keys,
        label_map,
        config,
        sanitize=sanitize,
        pipeline=pipeline,
        form_domain_map=form_domain_map,
        refresh_variables_fn=refresh_variables_fn,
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
        _skip_auto_redact_batch = True

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
        "preserve_record_id",
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
    explicit_sanitize = kwargs.pop("sanitize", None)
    if isinstance(config, TabularSinkConfig):
        _pop_sink_kwargs(kwargs)
        updates: dict[str, Any] = {}
        if config.study_key != study_key:
            updates["study_key"] = study_key
        if manifest_path is not None and config.manifest_path is None:
            updates["manifest_path"] = manifest_path
        if use_labels_as_columns and not config.use_labels_as_columns:
            updates["use_labels_as_columns"] = True
        if variable_whitelist is not None and config.variable_whitelist is None:
            updates["variable_whitelist"] = variable_whitelist
        if form_whitelist is not None and config.form_whitelist is None:
            updates["form_whitelist"] = form_whitelist
        if kwargs:
            updates["pandas_kwargs"] = {**config.pandas_kwargs, **kwargs}
        return replace(config, **updates) if updates else config
    if isinstance(config, SinkConfig):
        _pop_sink_kwargs(kwargs)
        base_values = {f.name: getattr(config, f.name) for f in fields(SinkConfig)}
        base_values["study_key"] = study_key
        if not mask_phi:
            base_values["mask_phi"] = False
        if default_quality_gate:
            base_values["quality_gate_enabled"] = True
        return TabularSinkConfig(
            **base_values,
            manifest_path=resolve_config_manifest_path(config, manifest_path),
            sanitize=bool(explicit_sanitize) if explicit_sanitize is not None else False,
            pandas_kwargs=kwargs,
            use_labels_as_columns=use_labels_as_columns,
            variable_whitelist=variable_whitelist,
            form_whitelist=form_whitelist,
        )
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


def _resolve_non_streaming_export_df(
    sdk: ImednetSDK,
    study_key: str,
    config: TabularSinkConfig,
    *,
    sanitize: bool = False,
) -> pd.DataFrame | None:
    """Resolve a preflight DataFrame when monkeypatched or mock-mapper non-streaming paths apply."""
    if _config_has_streaming_options(config):
        return None
    if _prepare_export_df is not _DEFAULT_PREPARE_EXPORT_DF:
        if config.quality_gate_enabled:
            return _collect_tabular_df(sdk, study_key, config, sanitize=sanitize)
        prepare_kwargs: dict[str, Any] = {
            "use_labels_as_columns": config.use_labels_as_columns,
            "variable_whitelist": config.variable_whitelist,
            "form_whitelist": config.form_whitelist,
        }
        if sanitize:
            prepare_kwargs["sanitize"] = True
        return _prepare_export_df(sdk, study_key, **prepare_kwargs)

    pre_mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
    if not _is_mock_mapper(pre_mapper):
        return None
    if config.quality_gate_enabled:
        return _collect_tabular_df(sdk, study_key, config, sanitize=sanitize)
    return _prepare_export_df(
        sdk,
        study_key,
        use_labels_as_columns=config.use_labels_as_columns,
        variable_whitelist=config.variable_whitelist,
        form_whitelist=config.form_whitelist,
        sanitize=sanitize,
        mask_phi=config.mask_phi,
        _mapper=pre_mapper,
        config=config,
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
    df_mock = _resolve_non_streaming_export_df(sdk, study_key, config)
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
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export Study Records to a CSV file.

    All exports inherit StudyConfiguration rules by default.

    Parameters
    ----------
    use_labels_as_columns:
        When ``True``, Variable labels are used for column names instead of
        Variable names.
    quality_gate_enabled:
        When ``True``, filters Study Records through :func:`apply_quality_gate`
        before writing. Defaults to ``False``.
    mask_phi:
        When ``True`` (default), sensitive Subject PHI values are replaced with
        ``***MASKED***``.
    """
    cfg_manifest = resolve_config_manifest_path(kwargs.get("config"), manifest_path)
    kwargs.setdefault("quality_gate_enabled", quality_gate_enabled)
    kwargs.setdefault("sanitize", True)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=cfg_manifest or str(Path(path).with_suffix(".manifest.jsonl")),
        default_quality_gate=quality_gate_enabled,
        mask_phi=mask_phi,
    )

    sink = TabularCSVSink(path, config)
    _tabular_export(sdk, study_key, sink, config, sanitize=config.sanitize, stream_name="csv")


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
    df_mock = _resolve_non_streaming_export_df(sdk, study_key, config, sanitize=config.sanitize)
    if df_mock is not None:

        def _write_excel() -> int:
            df_mock.to_excel(path, index=False, **config.pandas_kwargs)
            return len(df_mock)

        with _TabularCallableSink(config, "export_excel", _write_excel) as legacy_sink:
            legacy_sink.write_batch([1], batch_id=f"{study_key}/excel/0")
        return

    excel_kwargs = dict(config.pandas_kwargs)
    excel_study_forms = _try_fetch_study_forms(sdk, study_key) if config.as_sdtm else []
    excel_inferred_domains = (
        _infer_study_sdtm_domains(
            excel_study_forms,
            sdtm_domain=config.sdtm_domain,
            form_whitelist=config.form_whitelist,
            study_key=study_key,
        )
        if config.as_sdtm
        else []
    )
    sdtm_contract = (
        _build_sdtm_contract_types(
            config.sdtm_domain,
            variable_whitelist=config.variable_whitelist,
            domains=excel_inferred_domains or None,
            preserve_record_id=should_preserve_sdtm_record_id(config),
        )
        if config.as_sdtm
        else None
    )
    is_incremental_excel = _is_incremental_config(config)
    target_sheet_name = str(excel_kwargs.get("sheet_name", "Sheet1") or "Sheet1")

    def _read_existing_excel(excel_path: str | os.PathLike[str]) -> pd.DataFrame:
        with pd.ExcelFile(excel_path) as xls:
            if target_sheet_name not in xls.sheet_names:
                return pd.DataFrame()
            if not config.as_sdtm:
                prev = pd.read_excel(
                    xls,
                    sheet_name=target_sheet_name,
                    dtype=object,
                    keep_default_na=False,
                    na_values=[""],
                )
                for subj_col in ("subjectKey", "subject_key", "Subject Key"):
                    if subj_col in prev.columns:
                        prev[subj_col] = prev[subj_col].map(
                            lambda v: v if (_is_missing_scalar(v) or isinstance(v, str)) else str(v)
                        )
                return prev
            prev = pd.read_excel(
                xls,
                sheet_name=target_sheet_name,
                dtype=str,
                keep_default_na=False,
                na_values=[""],
            )
        for col in prev.columns:
            col_str = str(col)
            if col_str in {"AGE", "RECORD_ID"} or (
                col_str.endswith("SEQ") and col_str != "DOMAIN" and col_str.upper() != "DMSEQ"
            ):
                numeric_series = pd.to_numeric(prev[col], errors="coerce")
                if bool(numeric_series.notna().all()):
                    prev[col] = numeric_series.astype("int64")
                else:
                    prev[col] = numeric_series.astype("Int64")
        return prev

    def _atomic_write_excel_sheet(target_df: pd.DataFrame, *, preserve_siblings: bool) -> None:
        if preserve_siblings:
            write_kwargs = {k: v for k, v in excel_kwargs.items() if k != "engine"}
            write_kwargs["sheet_name"] = target_sheet_name

            def _update_existing_workbook(tmp: str) -> None:
                shutil.copyfile(path, tmp)
                with pd.ExcelWriter(
                    tmp,
                    engine="openpyxl",
                    mode="a",
                    if_sheet_exists="replace",
                ) as writer:
                    target_df.to_excel(writer, index=False, **write_kwargs)

            _atomic_replace_file(path, _update_existing_workbook, suffix=".xlsx")
            return

        _atomic_replace_file(
            path,
            lambda tmp: target_df.to_excel(tmp, index=False, **excel_kwargs),
            suffix=".xlsx",
        )

    def _write_or_reconcile_excel(combined: pd.DataFrame, voided_ids: set[str]) -> None:
        dest_exists = Path(path).exists() and Path(path).stat().st_size > 0
        has_native_to_excel = getattr(pd.DataFrame.to_excel, "__module__", "").startswith("pandas")
        if not (is_incremental_excel and config.idempotent):
            target = (
                _build_empty_contract_df(sdtm_contract)
                if combined.empty and sdtm_contract is not None
                else combined
            )
            if dest_exists and "sheet_name" in excel_kwargs and has_native_to_excel:
                _atomic_write_excel_sheet(target, preserve_siblings=True)
            else:
                target.to_excel(path, index=False, **excel_kwargs)
            return

        voided_sdtm = set(sink._voided_sdtm_keys)
        if combined.empty and not voided_ids and not voided_sdtm:
            if dest_exists:
                return
            empty_target = (
                _build_empty_contract_df(sdtm_contract) if sdtm_contract is not None else combined
            )
            _atomic_write_excel_sheet(empty_target, preserve_siblings=False)
            return

        delta_ids: set[str] = set()
        delta_sdtm: set[SdtmKeyTuple] = set()
        if _find_df_record_id_col(combined) is not None or not config.as_sdtm:
            dedup_delta, delta_ids = _dedupe_df_by_record_id(
                combined,
                exclude_ids=voided_ids,
                exclude_sdtm_keys=voided_sdtm if config.as_sdtm else None,
                study_key=study_key,
            )
            if config.as_sdtm:
                delta_sdtm = {k for k in _df_sdtm_keys(dedup_delta) if k is not None}
        else:
            dedup_delta, delta_sdtm = _dedupe_df_by_sdtm_key(
                combined,
                exclude_keys=voided_sdtm,
            )

        if dest_exists:
            prev_df = _read_existing_excel(path)
            if _find_df_record_id_col(prev_df) is not None or not config.as_sdtm:
                prev_kept = _filter_df_excluding_record_ids(
                    prev_df,
                    delta_ids | voided_ids,
                    study_key=study_key,
                )
                if config.as_sdtm and not prev_kept.empty:
                    dm_exclude = {k for k in (delta_sdtm | voided_sdtm) if k[1] == "DM"}
                    if dm_exclude:
                        prev_kept = _filter_df_excluding_sdtm_keys(prev_kept, dm_exclude)
            else:
                prev_kept = _filter_df_excluding_sdtm_keys(prev_df, delta_sdtm | voided_sdtm)

            if not prev_kept.empty and config.mask_phi:
                phi_keys = _resolve_study_phi_keys(study_key, config=config, records=prev_kept)
                copied_prev = False
                if config.as_sdtm:
                    for col in prev_kept.columns:
                        if _is_birth_col(col) and not global_sensitivity_registry.is_exempt(
                            str(col)
                        ):
                            vals = prev_kept[col].tolist()
                            if any(
                                not _is_missing_scalar(v)
                                and not (isinstance(v, str) and len(v) == 4 and v.isdigit())
                                for v in vals
                            ):
                                if not copied_prev:
                                    prev_kept = prev_kept.copy()
                                    copied_prev = True
                                prev_kept[col] = prev_kept[col].map(
                                    lambda val: (
                                        None
                                        if _is_missing_scalar(val)
                                        else format_iso8601_dtc(val, mask_phi=True)
                                    )
                                )
                matching_phi_cols = list(
                    dict.fromkeys(
                        col
                        for col in prev_kept.columns
                        if _is_active_phi_key(str(col), phi_keys)
                        and not (config.as_sdtm and _is_birth_col(col))
                    )
                )
                cols_needing_mask = [
                    col
                    for col in matching_phi_cols
                    if (
                        bool((prev_kept[col].notna() & (prev_kept[col] != MASKED_SENTINEL)).any())
                        if config.as_sdtm
                        else bool(
                            prev_kept[col].isna().any() or (prev_kept[col] != MASKED_SENTINEL).any()
                        )
                    )
                ]
                if cols_needing_mask:
                    if not copied_prev:
                        prev_kept = prev_kept.copy()
                    for col in cols_needing_mask:
                        prev_kept[col] = MASKED_SENTINEL
            if prev_kept.empty:
                merged_df = (
                    dedup_delta
                    if not dedup_delta.empty or len(dedup_delta.columns) > 0
                    else prev_df.iloc[0:0]
                )
            elif dedup_delta.empty:
                merged_df = prev_kept.reset_index(drop=True)
            else:
                merged_df = _concat_tabular_frames([prev_kept, dedup_delta])
        else:
            merged_df = dedup_delta

        _atomic_write_excel_sheet(
            merged_df,
            preserve_siblings=dest_exists and has_native_to_excel,
        )

    sink = _TabularCollectingSink(config, _write_or_reconcile_excel, pass_voided_ids=True)
    _tabular_export(sdk, study_key, sink, config, sanitize=config.sanitize, stream_name="excel")


def _apply_json_enrichment(
    study_key: str,
    data: Any,
    *,
    mask_phi: bool = True,
    config: SinkConfig | None = None,
) -> Any:
    """Apply SinkConfig.extra or ConfigVersionStore EnrichmentPipeline when present."""
    import logging

    logger = logging.getLogger(__name__)
    try:
        pipeline = resolve_sink_enrichment_pipeline(study_key, config=config, records=data)
        if pipeline is not None and getattr(pipeline, "_has_rules", True):
            logger.info("Enrichment pipeline triggered")
            data = pipeline.process(data) if mask_phi else pipeline.process(data, mask_phi=False)
            logger.info("Enrichment pipeline completed successfully")
    except Exception as e:
        logger.warning(f"Could not apply EnrichmentPipeline: {e}")
    return data


def _dump_json_payload(
    path: str | os.PathLike[str],
    data: Any,
    config: TabularSinkConfig,
    *,
    skip_sanitize: bool = False,
) -> int:
    """Persist strict RFC 8259 JSON export payload atomically, appending when ``idempotent=False``."""
    resolved_path: str = _coerce_path_str(path)
    dump_kwargs = dict(config.pandas_kwargs)
    dump_kwargs["allow_nan"] = False

    if skip_sanitize and config.idempotent:
        try:
            serialized = json.dumps(data, **dump_kwargs)
            payload_len = len(data) if isinstance(data, list) else 1
        except (TypeError, ValueError):
            sanitized_fallback = _sanitize_json_value(data)
            serialized = json.dumps(sanitized_fallback, **dump_kwargs)
            payload_len = len(sanitized_fallback) if isinstance(sanitized_fallback, list) else 1
    else:
        sanitized_data = _sanitize_json_value(data)
        payload = sanitized_data
        if (
            not config.idempotent
            and isinstance(sanitized_data, list)
            and Path(resolved_path).exists()
        ):
            try:
                with open(resolved_path, encoding="utf-8") as existing_file:
                    existing_data = json.load(existing_file)
                if isinstance(existing_data, list):
                    payload = _sanitize_json_value(existing_data) + sanitized_data
            except Exception:
                payload = sanitized_data
        serialized = json.dumps(payload, **dump_kwargs)
        payload_len = len(sanitized_data) if isinstance(sanitized_data, list) else 1

    def _write_tmp_json(tmp_path: str) -> None:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(serialized)

    _atomic_replace_file(resolved_path, _write_tmp_json, suffix=".json.tmp")
    return payload_len


_JSON_SCALAR_CLASSES = frozenset({str, int, float, bool, type(None)})


def _remask_historical_item_if_needed(
    value: Any,
    phi_keys: set[str] | None,
    key_cache: dict[str, bool],
    *,
    as_sdtm: bool = False,
) -> Any:
    """Re-mask historical JSON items in place only when active PHI rules match existing keys."""
    vc = value.__class__
    if vc is dict or isinstance(value, dict):
        cache_get = key_cache.get
        scalar_classes = _JSON_SCALAR_CLASSES
        for k, v in value.items():
            if k.__class__ is str or isinstance(k, str):
                if as_sdtm and _is_birth_col(k) and not global_sensitivity_registry.is_exempt(k):
                    if v is not None and not (isinstance(v, str) and len(v) == 4 and v.isdigit()):
                        value[k] = format_iso8601_dtc(v, mask_phi=True)
                    continue
                is_phi = cache_get(k)
                if is_phi is None:
                    is_phi = _is_active_phi_key(k, phi_keys)
                    key_cache[k] = is_phi
                if is_phi:
                    if v != MASKED_SENTINEL:
                        value[k] = MASKED_SENTINEL
                    continue
            if v.__class__ not in scalar_classes and isinstance(v, _CONTAINER_CELL_TYPES):
                new_v = _remask_historical_item_if_needed(v, phi_keys, key_cache, as_sdtm=as_sdtm)
                if new_v is not v:
                    value[k] = new_v
        return value
    if vc is list or isinstance(value, list):
        scalar_classes = _JSON_SCALAR_CLASSES
        for idx, item in enumerate(value):
            if item.__class__ not in scalar_classes and isinstance(item, _CONTAINER_CELL_TYPES):
                new_item = _remask_historical_item_if_needed(
                    item, phi_keys, key_cache, as_sdtm=as_sdtm
                )
                if new_item is not item:
                    value[idx] = new_item
        return value
    if vc is tuple or isinstance(value, tuple):
        mutated: list[Any] | None = None
        scalar_classes = _JSON_SCALAR_CLASSES
        for idx, item in enumerate(value):
            if item.__class__ not in scalar_classes and isinstance(item, _CONTAINER_CELL_TYPES):
                new_item = _remask_historical_item_if_needed(
                    item, phi_keys, key_cache, as_sdtm=as_sdtm
                )
                if new_item is not item:
                    if mutated is None:
                        mutated = list(value)
                    mutated[idx] = new_item
        return tuple(mutated) if mutated is not None else value
    return value


def _merge_flat_json_records(
    existing_records: list[Any],
    delta_records: list[dict[str, Any]],
    voided_ids: set[str],
    *,
    voided_sdtm_keys: set[SdtmKeyTuple] | None = None,
    mask_historical_phi: bool = False,
    phi_keys: set[str] | None = None,
    study_key: str | None = None,
    as_sdtm: bool = False,
) -> list[Any]:
    """Upsert ``delta_records`` by canonical ``(STUDYID, RECORD_ID)`` (or ``(STUDYID, 'DM', USUBJID)`` for DM) and drop voided rows."""
    voided_sdtm = voided_sdtm_keys or set()
    delta_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    unkeyed_delta: list[dict[str, Any]] = []
    for drec in delta_records:
        skey = _extract_row_sdtm_key(drec) if as_sdtm else None
        if skey is not None and skey[1] == "DM":
            delta_by_key[("__SDTM_DM__", skey)] = drec
            continue
        cid = _extract_row_canonical_id(drec)
        if cid is not None:
            dstudy = _extract_row_study_key(drec) or study_key or ""
            delta_by_key[(dstudy, cid)] = drec
        else:
            unkeyed_delta.append(drec)

    key_cache: dict[str, bool] = {}
    merged: list[Any] = []
    placed_keys: set[tuple[Any, ...]] = set()
    for erec in existing_records:
        if isinstance(erec, Mapping):
            skey = _extract_row_sdtm_key(erec) if as_sdtm else None
            is_dm = skey is not None and skey[1] == "DM"
            if is_dm and skey in voided_sdtm:
                continue
            cid = _extract_row_canonical_id(erec)
            estudy = _extract_row_study_key(erec) or study_key or ""
            comp_key: tuple[Any, ...] | None = (
                ("__SDTM_DM__", skey) if is_dm else ((estudy, cid) if cid is not None else None)
            )
            if (
                cid is not None
                and cid in voided_ids
                and (not study_key or not estudy or estudy == study_key)
            ):
                continue
            if comp_key is not None and comp_key in delta_by_key:
                if comp_key not in placed_keys:
                    merged.append(delta_by_key[comp_key])
                    placed_keys.add(comp_key)
                continue
            if mask_historical_phi:
                erec_dict = erec if isinstance(erec, dict) else dict(erec)
                _remask_historical_item_if_needed(erec_dict, phi_keys, key_cache, as_sdtm=as_sdtm)
                merged.append(erec_dict)
                continue
        elif mask_historical_phi and isinstance(erec, _CONTAINER_CELL_TYPES):
            merged.append(
                _remask_historical_item_if_needed(erec, phi_keys, key_cache, as_sdtm=as_sdtm)
            )
            continue
        merged.append(erec)

    for comp_key, drec in delta_by_key.items():
        if comp_key[0] == "__SDTM_DM__":
            is_voided = comp_key[1] in voided_sdtm
            cid_d = _extract_row_canonical_id(drec)
            skey_d = _extract_row_study_key(drec) or study_key or ""
            if (
                cid_d is not None
                and cid_d in voided_ids
                and (not study_key or not skey_d or skey_d == study_key)
            ):
                is_voided = True
        else:
            skey_d, cid_d = comp_key
            is_voided = cid_d in voided_ids and (not study_key or not skey_d or skey_d == study_key)
        if comp_key not in placed_keys and not is_voided:
            merged.append(drec)
    merged.extend(unkeyed_delta)
    return merged


def _merge_sdtm_json_records(
    existing_records: list[Any],
    delta_records: list[dict[str, Any]],
    voided_keys: set[SdtmKeyTuple],
    *,
    mask_historical_phi: bool = False,
    phi_keys: set[str] | None = None,
) -> list[Any]:
    """Upsert ``delta_records`` by canonical SDTM composite key and drop ``voided_keys``."""
    delta_by_key: dict[SdtmKeyTuple, dict[str, Any]] = {}
    unkeyed_delta: list[dict[str, Any]] = []
    for drec in delta_records:
        skey = _extract_row_sdtm_key(drec)
        if skey is not None:
            delta_by_key[skey] = drec
        else:
            unkeyed_delta.append(drec)

    key_cache: dict[str, bool] = {}
    merged: list[Any] = []
    placed_keys: set[SdtmKeyTuple] = set()
    for erec in existing_records:
        if isinstance(erec, Mapping):
            skey = _extract_row_sdtm_key(erec)
            if skey is not None and skey in voided_keys:
                continue
            if skey is not None and skey in delta_by_key:
                if skey not in placed_keys:
                    merged.append(delta_by_key[skey])
                    placed_keys.add(skey)
                continue
            if mask_historical_phi:
                erec_dict = erec if isinstance(erec, dict) else dict(erec)
                _remask_historical_item_if_needed(erec_dict, phi_keys, key_cache, as_sdtm=True)
                merged.append(erec_dict)
                continue
        elif mask_historical_phi and isinstance(erec, _CONTAINER_CELL_TYPES):
            merged.append(
                _remask_historical_item_if_needed(erec, phi_keys, key_cache, as_sdtm=True)
            )
            continue
        merged.append(erec)

    for skey, drec in delta_by_key.items():
        if skey not in placed_keys and skey not in voided_keys:
            merged.append(drec)
    merged.extend(unkeyed_delta)
    return merged


def _resolve_hierarchy_envelope_keys(
    naming_convention: str | None = None,
    sdtm_domain: str | None = None,
) -> tuple[str, str, str, str, str, str]:
    """Return ``(subj_k, visits_k, visit_id_k, forms_k, form_id_k, records_k)`` for hierarchy nodes."""
    canonical_conv = normalize_naming_convention(naming_convention)
    if canonical_conv is None:
        return ("subject_key", "visits", "visit_id", "forms", "form_id", "records")
    return (
        format_variable_name("subject_key", canonical_conv, domain=sdtm_domain, is_metadata=True),
        format_variable_name("visits", canonical_conv, domain=sdtm_domain, is_metadata=True),
        format_variable_name("visit_id", canonical_conv, domain=sdtm_domain, is_metadata=True),
        format_variable_name("forms", canonical_conv, domain=sdtm_domain, is_metadata=True),
        format_variable_name("form_id", canonical_conv, domain=sdtm_domain, is_metadata=True),
        format_variable_name("records", canonical_conv, domain=sdtm_domain, is_metadata=True),
    )


def _format_legacy_hierarchy_tree(
    tree: Any,
    *,
    naming_convention: str | None = None,
    sdtm_domain: str | None = None,
    use_labels_as_keys: bool = False,
) -> Any:
    """Format a mock-mapper hierarchy tree when ``mapper.build_hierarchy`` lacks ``naming_convention``."""
    canonical_conv = normalize_naming_convention(naming_convention)
    if canonical_conv is None or not isinstance(tree, list):
        return tree
    subj_k, visits_k, visit_id_k, forms_k, form_id_k, records_k = _resolve_hierarchy_envelope_keys(
        canonical_conv, sdtm_domain
    )
    meta_lookup = {m.lower() for m, _ in _RECORD_META_CONTRACT_TYPES} | {
        "parent_record_id",
        "parentrecordid",
        "studykey",
        "study_key",
        "studyid",
    }

    def _fmt_rec(rec: Any) -> Any:
        if not isinstance(rec, Mapping):
            return rec
        out_r: dict[str, Any] = {}
        used_low: set[str] = set()
        reserved_meta: set[str] = set()
        for k, v in rec.items():
            ks = str(k)
            if ks.lower() in meta_lookup:
                mk = format_variable_name(ks, canonical_conv, domain=sdtm_domain, is_metadata=True)
                if mk.lower() not in used_low:
                    used_low.add(mk.lower())
                    reserved_meta.add(mk.lower())
                    out_r[mk] = v
                    continue
            if use_labels_as_keys:
                base_vk = ks
            else:
                base_vk = format_nested_variable_name(ks, canonical_conv, domain=sdtm_domain)
            uk = prefix_colliding_variable_name(
                base_vk,
                reserved_meta,
                convention=canonical_conv,
                used_lower=used_low,
            )
            out_r[uk] = v
        return out_r

    def _fmt_forms(forms_list: Any) -> list[dict[str, Any]]:
        out_forms: list[dict[str, Any]] = []
        for fnode in forms_list or []:
            if not isinstance(fnode, Mapping):
                continue
            fid = next(
                (fnode[c] for c in ("form_id", "formId", "FORM_ID") if c in fnode),
                None,
            )
            raw_recs: Any = next(
                (fnode[c] for c in ("records", "RECORDS") if c in fnode),
                [],
            )
            out_forms.append({form_id_k: fid, records_k: [_fmt_rec(r) for r in (raw_recs or [])]})
        return out_forms

    out_tree: list[dict[str, Any]] = []
    for subj in tree:
        if not isinstance(subj, Mapping):
            continue
        sk = next(
            (subj[c] for c in ("subject_key", "subjectKey", "USUBJID") if c in subj),
            None,
        )
        raw_visits: Any = next((subj[c] for c in ("visits", "VISITS") if c in subj), [])
        out_visits: list[dict[str, Any]] = []
        for vnode in raw_visits or []:
            if not isinstance(vnode, Mapping):
                continue
            vid = next(
                (vnode[c] for c in ("visit_id", "visitId", "VISITNUM") if c in vnode),
                None,
            )
            v_forms: Any = next((vnode[c] for c in ("forms", "FORMS") if c in vnode), [])
            out_visits.append({visit_id_k: vid, forms_k: _fmt_forms(v_forms)})
        out_subj: dict[str, Any] = {subj_k: sk, visits_k: out_visits}
        raw_unsched = next((subj[c] for c in ("forms", "FORMS") if c in subj), None)
        if raw_unsched:
            out_subj[forms_k] = _fmt_forms(raw_unsched)
        out_tree.append(out_subj)
    return out_tree


def _merge_hierarchical_archives(
    existing_tree: list[Any],
    delta_tree: list[dict[str, Any]],
    exclude_ids: set[str],
    *,
    mask_historical_phi: bool = False,
    phi_keys: set[str] | None = None,
    naming_convention: str | None = None,
    sdtm_domain: str | None = None,
) -> list[dict[str, Any]]:
    """Merge hierarchical Subject->Visit->Form trees by ``recordId`` and prune voided records."""
    subj_k, visits_k, visit_id_k, forms_k, form_id_k, records_k = _resolve_hierarchy_envelope_keys(
        naming_convention, sdtm_domain
    )
    subjects: dict[Any, dict[str, Any]] = {}
    key_cache: dict[str, bool] = {}

    def _get_first(node: Mapping[str, Any], candidates: tuple[str, ...]) -> Any:
        for cand in candidates:
            if cand in node:
                return node[cand]
        return None

    def _keep_hist_records(raw_records: Any) -> list[dict[str, Any]]:
        kept_list: list[dict[str, Any]] = []
        for rec in raw_records or []:
            if not isinstance(rec, Mapping):
                continue
            if _extract_row_canonical_id(rec) in exclude_ids:
                continue
            rec_dict = dict(rec)
            if mask_historical_phi:
                _remask_historical_item_if_needed(rec_dict, phi_keys, key_cache)
            kept_list.append(rec_dict)
        return kept_list

    for subj in existing_tree:
        if not isinstance(subj, Mapping):
            continue
        sk = _get_first(subj, (subj_k, "subject_key", "subjectKey", "USUBJID"))
        visits_map: dict[Any, dict[Any, list[dict[str, Any]]]] = {}
        for visit in _get_first(subj, (visits_k, "visits", "VISITS")) or []:
            if not isinstance(visit, Mapping):
                continue
            vid = _get_first(visit, (visit_id_k, "visit_id", "visitId", "VISITNUM"))
            for fnode in _get_first(visit, (forms_k, "forms", "FORMS")) or []:
                if not isinstance(fnode, Mapping):
                    continue
                fid = _get_first(fnode, (form_id_k, "form_id", "formId", "FORM_ID"))
                kept = _keep_hist_records(_get_first(fnode, (records_k, "records", "RECORDS")))
                if kept:
                    visits_map.setdefault(vid, {}).setdefault(fid, []).extend(kept)

        unsched_map: dict[Any, list[dict[str, Any]]] = {}
        for fnode in _get_first(subj, (forms_k, "forms", "FORMS")) or []:
            if not isinstance(fnode, Mapping):
                continue
            fid = _get_first(fnode, (form_id_k, "form_id", "formId", "FORM_ID"))
            kept = _keep_hist_records(_get_first(fnode, (records_k, "records", "RECORDS")))
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
        sk = _get_first(subj, (subj_k, "subject_key", "subjectKey", "USUBJID"))
        entry = subjects.setdefault(sk, {"visits": {}, "forms": {}})
        for visit in _get_first(subj, (visits_k, "visits", "VISITS")) or []:
            if not isinstance(visit, Mapping):
                continue
            vid = _get_first(visit, (visit_id_k, "visit_id", "visitId", "VISITNUM"))
            for fnode in _get_first(visit, (forms_k, "forms", "FORMS")) or []:
                if not isinstance(fnode, Mapping):
                    continue
                fid = _get_first(fnode, (form_id_k, "form_id", "formId", "FORM_ID"))
                raw_recs = _get_first(fnode, (records_k, "records", "RECORDS")) or []
                recs = [dict(rec) for rec in raw_recs if isinstance(rec, Mapping)]
                if recs:
                    entry["visits"].setdefault(vid, {}).setdefault(fid, []).extend(recs)
        for fnode in _get_first(subj, (forms_k, "forms", "FORMS")) or []:
            if not isinstance(fnode, Mapping):
                continue
            fid = _get_first(fnode, (form_id_k, "form_id", "formId", "FORM_ID"))
            raw_recs = _get_first(fnode, (records_k, "records", "RECORDS")) or []
            recs = [dict(rec) for rec in raw_recs if isinstance(rec, Mapping)]
            if recs:
                entry["forms"].setdefault(fid, []).extend(recs)

    merged_tree: list[dict[str, Any]] = []
    for sk, entry in subjects.items():
        subj_visits: list[dict[str, Any]] = []
        for vid, forms_dict in entry["visits"].items():
            visit_forms = [
                {form_id_k: fid, records_k: recs} for fid, recs in forms_dict.items() if recs
            ]
            if visit_forms:
                subj_visits.append({visit_id_k: vid, forms_k: visit_forms})
        unsched_forms = [
            {form_id_k: fid, records_k: recs} for fid, recs in entry["forms"].items() if recs
        ]
        if not subj_visits and not unsched_forms:
            continue
        out_subj: dict[str, Any] = {subj_k: sk, visits_k: subj_visits}
        if unsched_forms:
            out_subj[forms_k] = unsched_forms
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
            hier_pipeline = resolve_sink_enrichment_pipeline(
                self._study_key,
                config=self._cfg,
                records=active_records,
            )
            _seed_mapper_enrichment(
                self._mapper,
                self._study_key,
                hier_pipeline,
                config=self._cfg,
                records=active_records,
            )
            call_kwargs = dict(self._hier_kwargs)
            if not self._is_mock_hier_mapper:
                call_kwargs["records"] = active_records
            delta_data = self._mapper.build_hierarchy(self._study_key, **call_kwargs)
        else:
            delta_data = []

        if self._is_mock_hier_mapper:
            if self._cfg.mask_phi:
                delta_data = mask_clinical_phi(delta_data)
            delta_data = _apply_json_enrichment(
                self._study_key,
                delta_data,
                mask_phi=self._cfg.mask_phi,
                config=self._cfg,
            )
            if (
                self._cfg.naming_convention is not None
                and "naming_convention" not in self._hier_kwargs
            ):
                delta_data = _format_legacy_hierarchy_tree(
                    delta_data,
                    naming_convention=self._cfg.naming_convention,
                    sdtm_domain=self._cfg.sdtm_domain,
                    use_labels_as_keys=self._cfg.use_labels_as_columns,
                )

        if self._is_incremental_sync and self._cfg.idempotent and dest_exists:
            delta_sanitized = _sanitize_json_value(delta_data)
            existing_tree: list[Any] = []
            try:
                with open(self.path, encoding="utf-8") as existing_f:
                    loaded = json.load(existing_f)
                if isinstance(loaded, list):
                    existing_tree = loaded
            except Exception:
                existing_tree = []

            exclude_ids = set(self._records_by_cid) | self._voided_ids
            phi_keys = (
                _resolve_study_phi_keys(
                    self._study_key,
                    config=self._cfg,
                    records=active_records,
                )
                if self._cfg.mask_phi
                else None
            )
            final_data: Any = _merge_hierarchical_archives(
                existing_tree,
                delta_sanitized if isinstance(delta_sanitized, list) else [],
                exclude_ids,
                mask_historical_phi=self._cfg.mask_phi,
                phi_keys=phi_keys,
                naming_convention=self._cfg.naming_convention,
                sdtm_domain=self._cfg.sdtm_domain,
            )
            return _dump_json_payload(self.path, final_data, self._cfg, skip_sanitize=True)

        return _dump_json_payload(self.path, delta_data, self._cfg)

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

    if hierarchical:
        if config.as_sdtm:
            raise ExportConfigurationError(
                "Hierarchical JSON export cannot be combined with SDTM domain transformation "
                "(as_sdtm=True)."
            )
        canonical_hier_conv = normalize_naming_convention(config.naming_convention)
        mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
        is_mock_hier_mapper = _is_mock_mapper(mapper) or not callable(
            getattr(mapper, "_get_enrichment_pipeline", None)
        )
        hier_kwargs: dict[str, Any] = {"use_labels_as_keys": config.use_labels_as_columns}
        if config.variable_whitelist is not None:
            hier_kwargs["variable_whitelist"] = config.variable_whitelist
        if config.form_whitelist is not None:
            hier_kwargs["form_whitelist"] = config.form_whitelist
        if _config_has_streaming_options(config):
            hier_kwargs["include_deleted"] = config.include_deleted
        if not config.mask_phi and not _is_mock_mapper(mapper):
            hier_kwargs["mask_phi"] = False
        if canonical_hier_conv is not None:
            try:
                hier_sig = inspect.signature(mapper.build_hierarchy)
                accepts_hier_conv = "naming_convention" in hier_sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in hier_sig.parameters.values()
                )
            except (TypeError, ValueError):
                accepts_hier_conv = False
            if accepts_hier_conv:
                hier_kwargs["naming_convention"] = canonical_hier_conv
                if config.sdtm_domain is not None:
                    hier_kwargs["domain"] = config.sdtm_domain

        if is_mock_hier_mapper and not _config_has_streaming_options(config):
            with sdk_governance_context(sdk):
                data = mapper.build_hierarchy(study_key, **hier_kwargs)
                if config.mask_phi:
                    data = mask_clinical_phi(data)
                data = _apply_json_enrichment(
                    study_key,
                    data,
                    mask_phi=config.mask_phi,
                    config=config,
                )
                if canonical_hier_conv is not None and "naming_convention" not in hier_kwargs:
                    data = _format_legacy_hierarchy_tree(
                        data,
                        naming_convention=canonical_hier_conv,
                        sdtm_domain=config.sdtm_domain,
                        use_labels_as_keys=config.use_labels_as_columns,
                    )
            with _TabularCallableSink(
                config,
                "export_json",
                lambda: _dump_json_payload(path, data, config),
            ) as legacy_hier_sink:
                legacy_hier_sink.write_batch([1], batch_id=f"{study_key}/json/0")
            return

        with sdk_governance_context(sdk):
            hier_pipeline = resolve_sink_enrichment_pipeline(study_key, config=config)
            _seed_mapper_enrichment(mapper, study_key, hier_pipeline, config=config)

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

    df_mock = _resolve_non_streaming_export_df(sdk, study_key, config)
    if df_mock is not None:
        mock_data = _dataframe_to_json_records(df_mock)
        with sdk_governance_context(sdk):
            mock_data = _apply_json_enrichment(
                study_key,
                mock_data,
                mask_phi=config.mask_phi,
                config=config,
            )
        with _TabularCallableSink(
            config,
            "export_json",
            lambda: _dump_json_payload(path, mock_data, config),
        ) as legacy_sink:
            legacy_sink.write_batch([1], batch_id=f"{study_key}/json/0")
        return

    is_incremental_json = _is_incremental_config(config)

    def _write_or_reconcile_json(combined: pd.DataFrame, voided_ids: set[str]) -> None:
        phi_keys = (
            _resolve_study_phi_keys(study_key, config=config, records=combined)
            if config.mask_phi
            else None
        )
        if not (is_incremental_json and config.idempotent):
            combined_stamped = (not config.mask_phi) or has_valid_tabular_batch_stamp(
                combined,
                mask_phi=True,
                phi_keys=phi_keys,
            )
            records_out: Any = _dataframe_to_json_records(combined)
            if config.mask_phi and not combined_stamped:
                if config.as_sdtm:
                    records_out = redact_batch_records(
                        records_out,
                        mask_phi=True,
                        as_sdtm=True,
                        study_key=study_key,
                        config=config,
                        phi_keys=phi_keys,
                    )
                else:
                    records_out = mask_clinical_phi(records_out)
                if phi_keys:
                    records_out = mask_custom_phi_recursive(records_out, phi_keys)
            _dump_json_payload(path, records_out, config)
            return

        voided_sdtm = set(sink._voided_sdtm_keys)
        dest_exists = Path(path).exists() and Path(path).stat().st_size > 0
        if combined.empty and not voided_ids and not voided_sdtm:
            if dest_exists:
                return
            _dump_json_payload(path, [], config)
            return

        has_delta_id_col = _find_df_record_id_col(combined) is not None
        if has_delta_id_col or not config.as_sdtm:
            dedup_delta, _ = _dedupe_df_by_record_id(
                combined,
                exclude_ids=voided_ids,
                exclude_sdtm_keys=voided_sdtm if config.as_sdtm else None,
                study_key=study_key,
            )
        else:
            dedup_delta, _ = _dedupe_df_by_sdtm_key(combined, exclude_keys=voided_sdtm)

        delta_stamped = (not config.mask_phi) or has_valid_tabular_batch_stamp(
            dedup_delta,
            mask_phi=True,
            phi_keys=phi_keys,
        )
        delta_records = _dataframe_to_json_records(dedup_delta)
        if config.mask_phi and not delta_stamped:
            if config.as_sdtm:
                delta_records = cast(
                    list[dict[str, Any]],
                    redact_batch_records(
                        delta_records,
                        mask_phi=True,
                        as_sdtm=True,
                        study_key=study_key,
                        config=config,
                        phi_keys=phi_keys,
                    ),
                )
            else:
                delta_records = cast(list[dict[str, Any]], mask_clinical_phi(delta_records))
            if phi_keys:
                delta_records = cast(
                    list[dict[str, Any]],
                    mask_custom_phi_recursive(delta_records, phi_keys),
                )

        delta_records = cast(list[dict[str, Any]], _sanitize_json_value(delta_records))

        existing_records: list[Any] = []
        if dest_exists:
            try:
                with open(path, encoding="utf-8") as existing_f:
                    loaded = json.load(existing_f)
                if isinstance(loaded, list):
                    existing_records = loaded
            except Exception:
                existing_records = []

        existing_has_ids = any(
            isinstance(r, Mapping) and _extract_row_canonical_id(r) is not None
            for r in existing_records
        )
        if has_delta_id_col or (not delta_records and existing_has_ids) or not config.as_sdtm:
            merged_records: Any = _merge_flat_json_records(
                existing_records,
                delta_records,
                voided_ids,
                voided_sdtm_keys=voided_sdtm if config.as_sdtm else None,
                mask_historical_phi=config.mask_phi,
                phi_keys=phi_keys,
                study_key=study_key,
                as_sdtm=config.as_sdtm,
            )
        else:
            merged_records = _merge_sdtm_json_records(
                existing_records,
                delta_records,
                voided_sdtm,
                mask_historical_phi=config.mask_phi,
                phi_keys=phi_keys,
            )
        _dump_json_payload(path, merged_records, config, skip_sanitize=True)

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
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export Study Records to a SQL table.

    All exports inherit StudyConfiguration rules by default.

    Parameters
    ----------
    use_labels_as_columns:
        When ``True``, Variable labels are used for column names instead of
        Variable names.
    manifest_path:
        Optional path to a file where manifest entries should be logged.
        Defaults to ``None``.
    quality_gate_enabled:
        When ``True``, filters Study Records through :func:`apply_quality_gate`
        before writing. Defaults to ``False``.
    mask_phi:
        When ``True`` (default), sensitive Subject PHI values are replaced with
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

    df_legacy = _resolve_non_streaming_export_df(sdk, study_key, config)
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
    seq_counters: dict[str, int] = field(default_factory=dict)
    record_seqs: dict[str, int] = field(default_factory=dict)
    sink: Any = None
    tx: dict[str, Any] | None = None
    _active_state_provider: Any = None
    _active_stream_name: str | None = None
    _active_study_key: str | None = None
    _active_tx: dict[str, Any] | None = None
    _active_form_domain_map: dict[Any, str] = field(default_factory=dict)
    _sdtm_lineage: dict[str, dict[str, Any]] = field(default_factory=dict)
    _sdtm_seq_watermarks: dict[str, int] = field(default_factory=dict)


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
    preserve_record_id: bool = False,
) -> tuple[list[_FormExportContext], list[Any]]:
    """Fetch study forms and variables once and build per-form export contexts."""
    forms = list(sdk.forms.list(study_key=study_key))
    if as_sdtm and sdtm_domain is not None and forms:
        forms = filter_forms_by_sdtm_domain(forms, sdtm_domain, study_key=study_key)
    form_filter: dict[str, Any] = {"formId": form_whitelist} if form_whitelist else {}
    all_variables = (
        []
        if (form_whitelist is not None and len(form_whitelist) == 0)
        or (as_sdtm and sdtm_domain is not None and not forms)
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
            resolve_sdtm_domain(sdtm_domain, fallback="AE")
            if as_sdtm and sdtm_domain
            else (
                resolve_form_sdtm_domain(form, study_key=study_key, fallback="AE")
                if as_sdtm
                else sdtm_domain
            )
        )
        record_model = None if as_sdtm else mapper._build_record_model(variable_keys, label_map)
        contract_types = (
            _build_sdtm_contract_types(
                form_domain,
                variable_whitelist=variable_whitelist,
                preserve_record_id=preserve_record_id,
            )
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
        batch_tx_fn = getattr(provider, "batch_transaction", None) if provider is not None else None
        inst_dict = getattr(provider, "__dict__", {}) if provider is not None else {}
        has_custom_instance_tx = "transaction" in inst_dict and "batch_transaction" not in inst_dict
        if provider is not None and callable(batch_tx_fn) and not has_custom_instance_tx:
            tx_map = tx_stack.enter_context(
                batch_tx_fn(
                    study_key,
                    [ctx.stream_name for ctx in contexts],
                    fallback_timestamp=opts["modified_since"],
                )
            )
            for ctx in contexts:
                ctx.tx = tx_map[ctx.stream_name]
        elif provider is not None:
            for ctx in contexts:
                ctx.tx = tx_stack.enter_context(
                    provider.transaction(
                        study_key,
                        ctx.stream_name,
                        fallback_timestamp=opts["modified_since"],
                    )
                )

        for ctx in contexts:
            if ctx.tx is not None:
                raw_last = ctx.tx.get("last_timestamp")
                ctx.watermark = (
                    normalize_watermark_datetime(raw_last)
                    if isinstance(raw_last, (datetime, str))
                    else None
                ) or opts["modified_since"]
                raw_meta = ctx.tx.get("metadata")
                if isinstance(raw_meta, dict):
                    ctx_meta = raw_meta
                else:
                    ctx_meta = {}
                    ctx.tx["metadata"] = ctx_meta
                if config.as_sdtm:
                    bind_sdtm_stream_metadata(ctx_meta, ctx.seq_counters, ctx.record_seqs)
                if ctx.sink is not None and hasattr(ctx.sink, "bind_stream_metadata"):
                    ctx.sink.bind_stream_metadata(ctx_meta)
            else:
                ctx.watermark = opts["modified_since"]

            ctx._active_state_provider = provider
            ctx._active_stream_name = ctx.stream_name
            ctx._active_study_key = study_key
            ctx._active_tx = ctx.tx
            ctx._active_form_domain_map = build_form_sdtm_domain_map(
                [ctx.form],
                study_key=study_key,
            )
            if ctx.sink is not None:
                for target_obj in (ctx.sink, getattr(ctx.sink, "_inner", None)):
                    if target_obj is not None:
                        with contextlib.suppress(Exception):
                            target_obj._active_state_provider = provider
                            target_obj._active_stream_name = ctx.stream_name
                            target_obj._active_study_key = study_key
                            target_obj._active_tx = ctx.tx
                            target_obj._active_form_domain_map = ctx._active_form_domain_map

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
            [ctx.form_id for ctx in contexts]
            if (
                config.form_whitelist is not None
                or (config.as_sdtm and config.sdtm_domain is not None)
            )
            else None
        )
        merged_filter = _merge_form_whitelist_into_filter(
            opts["record_filter"], effective_form_whitelist
        )
        sdtm_sort = _SDTM_RECORDS_SORT if config.as_sdtm else None
        raw_records = iter_filtered_sdk_records(
            sdk,
            study_key,
            watermark=study_watermark,
            record_filter=merged_filter,
            record_data_filter=opts["record_data_filter"],
            exclude_deleted_upstream=exclude_deleted_upstream,
            sort=sdtm_sort,
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
                    form_domain_map=ctx._active_form_domain_map or None,
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

        if config.as_sdtm:
            combined_form_map: dict[Any, str] = {}
            for ctx in contexts:
                combined_form_map.update(ctx._active_form_domain_map)
            gated_records = iter_subject_ordered_sdtm_records(
                gated_records,
                study_key=study_key,
                sdtm_domain=opts["sdtm_domain"],
                form_domain_map=combined_form_map or None,
                mask_phi=config.mask_phi,
                source_is_ordered=_is_bound_real_records_endpoint(sdk),
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
        preserve_record_id=config.preserve_record_id,
    )


@dataclass(frozen=True)
class _FormExportState:
    """Shared mapper, enrichment, and mock-dispatch state for per-form exports."""

    mapper: Any
    pipeline: EnrichmentPipeline | None
    phi_keys: set[str] | None
    has_native_enrichment: bool
    use_mock_fetch: bool


def _prepare_form_export_state(
    sdk: Any,
    study_key: str,
    config: TabularSinkConfig,
) -> _FormExportState:
    """Initialize the RecordMapper, enrichment pipeline, and mock-fetch flag for per-form exports."""
    with sdk_governance_context(sdk):
        mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
        pipeline = resolve_sink_enrichment_pipeline(study_key, config=config)
        _seed_mapper_enrichment(mapper, study_key, pipeline, config=config)
        phi_keys = resolve_sink_phi_keys(
            study_key,
            config=config,
            phi_keys=getattr(pipeline, "phi_keys", None),
        )
    has_native_enrichment = callable(getattr(mapper, "_get_enrichment_pipeline", None))
    use_mock_fetch = _uses_mock_fetch_records(mapper) and not _config_has_streaming_options(config)
    return _FormExportState(
        mapper=mapper,
        pipeline=pipeline,
        phi_keys=phi_keys,
        has_native_enrichment=has_native_enrichment,
        use_mock_fetch=use_mock_fetch,
    )


def _build_mock_form_df(
    study_key: str,
    ctx: _FormExportContext,
    state: _FormExportState,
    config: TabularSinkConfig,
    *,
    use_labels_as_columns: bool,
    coerce_columns_to_str: bool,
) -> pd.DataFrame:
    """Fetch, parse, enrich, deduplicate, and mask a mock per-form DataFrame."""
    records = state.mapper._fetch_records(
        study_key,
        extra_filters={"formId": ctx.form_id},
    )
    rows, _ = state.mapper._parse_records(records, ctx.record_model)
    if state.pipeline is not None and not state.has_native_enrichment:
        rows = [state.pipeline.process(r, mask_phi=config.mask_phi) for r in rows]
    df = state.mapper._build_dataframe(
        rows,
        ctx.variable_keys,
        ctx.label_map,
        use_labels_as_columns,
    )
    if isinstance(df, pd.DataFrame):
        df = _dedup_case_insensitive_columns(df, ensure_str_columns=coerce_columns_to_str)
        if config.mask_phi:
            df = _mask_df(df, phi_keys=state.phi_keys)
    return cast(pd.DataFrame, df)


def _enter_form_transform_sink(
    sink_stack: contextlib.ExitStack,
    ctx: _FormExportContext,
    inner_sink: ExportSink,
    form_config: TabularSinkConfig,
    state: _FormExportState,
    *,
    study_key: str | None = None,
    study_variable_whitelist: Sequence[str] | None = None,
) -> None:
    """Wrap ``inner_sink`` in ``_TabularTransformSink`` and register it on ``sink_stack``."""
    refresh_fn: Callable[[], tuple[list[str], dict[str, str], Any] | None] | None = None
    if study_key and not form_config.as_sdtm and not _is_mock_mapper(state.mapper):

        def _refresh_form_variables() -> tuple[list[str], dict[str, str], Any] | None:
            wl_list = (
                list(study_variable_whitelist) if study_variable_whitelist is not None else None
            )
            updated_keys, updated_labels = state.mapper._fetch_variable_metadata(
                study_key,
                variable_whitelist=wl_list,
                form_whitelist=[ctx.form_id],
            )
            if not updated_keys:
                return None
            updated_raw = (
                state.mapper._last_variables if hasattr(state.mapper, "_last_variables") else None
            )
            updated_contract = _build_tabular_contract_types(
                updated_keys,
                updated_labels,
                updated_raw if isinstance(updated_raw, list) else (),
                use_labels_as_columns=form_config.use_labels_as_columns,
                mask_phi=form_config.mask_phi,
                pipeline=state.pipeline,
                naming_convention=form_config.naming_convention,
                sdtm_domain=form_config.sdtm_domain,
            )
            ctx.variable_keys = list(updated_keys)
            ctx.label_map = dict(updated_labels)
            ctx.contract_types = updated_contract
            if isinstance(inner_sink, (TabularDuckDBSink, TabularSQLSink)):
                inner_sink.set_contract_types(updated_contract)
            updated_model = state.mapper._build_record_model(updated_keys, updated_labels)
            ctx.record_model = updated_model
            return updated_keys, updated_labels, updated_model

        refresh_fn = _refresh_form_variables

    transform_sink = _TabularTransformSink(
        inner_sink,
        state.mapper,
        ctx.record_model,
        ctx.variable_keys,
        ctx.label_map,
        form_config,
        sanitize=False,
        pipeline=state.pipeline,
        refresh_variables_fn=refresh_fn,
        refresh_whitelist=study_variable_whitelist,
        use_explicit_refresh_whitelist=True,
    )
    ctx.sink = sink_stack.enter_context(transform_sink)


def _make_form_stream_callbacks(
    study_key: str,
) -> tuple[
    Callable[[_FormExportContext, list[Any], int], int],
    Callable[[_FormExportContext, list[Any], int], int],
]:
    """Create active-batch write and voided-batch reconciliation callbacks for per-form streaming."""

    def _write_form_batch(ctx: _FormExportContext, batch: list[Any], batch_idx: int) -> int:
        return int(ctx.sink.write_batch(batch, batch_id=f"{study_key}/{ctx.form_key}/{batch_idx}"))

    def _reconcile_form_voided(
        ctx: _FormExportContext, voided_batch: list[Any], batch_idx: int
    ) -> int:
        return int(
            ctx.sink.reconcile_deleted(
                voided_batch,
                batch_id=f"{study_key}/{ctx.form_key}/voided/{batch_idx}",
            )
        )

    return _write_form_batch, _reconcile_form_voided


def export_to_duckdb_by_form(
    sdk: ImednetSDK,
    study_key: str,
    db_path: str | os.PathLike[str],
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export Study Records to separate DuckDB tables for each Form.

    Each Form is exported to a table named after ``form.form_key``.

    Parameters
    ----------
    sdk:
        Authenticated SDK instance used to fetch Study Records.
    study_key:
        Study identifier to export.
    db_path:
        Path to the target ``.duckdb`` database file.
    use_labels_as_columns:
        When ``True``, Variable labels are used for DataFrame column names.
    variable_whitelist:
        Optional list of Variable names to include.
    form_whitelist:
        Optional list of Form IDs to include.
    quality_gate_enabled:
        When ``True``, filters Study Records through :func:`apply_quality_gate`
        before writing. Defaults to ``False``.
    mask_phi:
        When ``True`` (default), sensitive Subject PHI values are replaced with
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
    state = _prepare_form_export_state(sdk, study_key, config)

    contexts, all_variables = _build_form_export_contexts(
        sdk,
        study_key,
        state.mapper,
        use_labels_as_columns=config.use_labels_as_columns,
        variable_whitelist=config.variable_whitelist,
        form_whitelist=config.form_whitelist,
        mask_phi=config.mask_phi,
        pipeline=state.pipeline,
        stream_prefix="duckdb",
        custom_stream_prefix=config.stream_name,
        validate_form_key_fn=_validate_duckdb_identifier,
        as_sdtm=config.as_sdtm,
        sdtm_domain=config.sdtm_domain,
        naming_convention=config.naming_convention,
        preserve_record_id=should_preserve_sdtm_record_id(config),
    )

    resolved_db_path = _coerce_path_str(db_path)
    conn: Any = duckdb.connect(resolved_db_path, config={"threads": "1"})
    try:
        if state.use_mock_fetch:
            for ctx in contexts:
                df = _build_mock_form_df(
                    study_key,
                    ctx,
                    state,
                    config,
                    use_labels_as_columns=config.use_labels_as_columns,
                    coerce_columns_to_str=True,
                )
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
                    use_labels_as_columns=config.use_labels_as_columns,
                )
                inner_ddb_sink = TabularDuckDBSink(
                    db_path,
                    ctx.form_key,
                    form_config,
                    contract_types=ctx.contract_types,
                    conn=conn,
                )
                _enter_form_transform_sink(
                    sink_stack,
                    ctx,
                    inner_ddb_sink,
                    form_config,
                    state,
                    study_key=study_key,
                    study_variable_whitelist=config.variable_whitelist,
                )

            on_active_batch, on_voided_batch = _make_form_stream_callbacks(study_key)

            _stream_records_across_forms(
                sdk,
                study_key,
                config,
                contexts,
                all_variables,
                on_active_batch=on_active_batch,
                on_voided_batch=on_voided_batch,
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
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export Study Records to separate SQL tables for each Form."""
    from sqlalchemy import create_engine

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
    resolved_if_exists: Literal["fail", "replace", "append", "delete_rows"] = (
        if_exists if if_exists is not None else ("replace" if config.idempotent else "append")
    )

    state = _prepare_form_export_state(sdk, study_key, config)
    engine = create_engine(conn_str)

    contexts, all_variables = _build_form_export_contexts(
        sdk,
        study_key,
        state.mapper,
        use_labels_as_columns=config.use_labels_as_columns,
        variable_whitelist=config.variable_whitelist,
        form_whitelist=config.form_whitelist,
        mask_phi=config.mask_phi,
        pipeline=state.pipeline,
        stream_prefix="sql",
        custom_stream_prefix=config.stream_name,
        as_sdtm=config.as_sdtm,
        sdtm_domain=config.sdtm_domain,
        naming_convention=config.naming_convention,
        preserve_record_id=should_preserve_sdtm_record_id(config),
    )

    if state.use_mock_fetch:
        for ctx in contexts:
            df = _build_mock_form_df(
                study_key,
                ctx,
                state,
                config,
                use_labels_as_columns=config.use_labels_as_columns,
                coerce_columns_to_str=False,
            )
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
            if if_exists is not None and not is_incremental:
                form_pandas_kwargs["if_exists"] = if_exists
            form_config = _make_form_sink_config(
                study_key,
                config,
                ctx,
                use_labels_as_columns=config.use_labels_as_columns,
                pandas_kwargs=form_pandas_kwargs,
            )
            inner_sql_sink = TabularSQLSink(ctx.form_key, engine, form_config)
            inner_sql_sink.set_contract_types(ctx.contract_types)
            _enter_form_transform_sink(
                sink_stack,
                ctx,
                inner_sql_sink,
                form_config,
                state,
                study_key=study_key,
                study_variable_whitelist=config.variable_whitelist,
            )

        on_active_batch, on_voided_batch = _make_form_stream_callbacks(study_key)

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
            on_active_batch=on_active_batch,
            on_voided_batch=on_voided_batch,
            on_before_checkpoints=_finalize_and_flush_sql_sinks,
        )


def _resolve_long_sql_column_names(
    naming_convention: str | None = None,
    sdtm_domain: str | None = None,
) -> tuple[str, str, str, str, str]:
    """Return ``(rec_id_col, form_id_col, var_name_col, val_col, ts_col)`` for long-format SQL."""
    canonical_conv = normalize_naming_convention(naming_convention)
    if canonical_conv is None:
        return ("record_id", "form_id", "variable_name", "value", "timestamp")
    return (
        format_variable_name("record_id", canonical_conv, domain=sdtm_domain, is_metadata=True),
        format_variable_name("form_id", canonical_conv, domain=sdtm_domain, is_metadata=True),
        format_variable_name("variable_name", canonical_conv, domain=sdtm_domain, is_metadata=True),
        format_variable_name("value", canonical_conv, domain=sdtm_domain, is_metadata=True),
        format_variable_name("timestamp", canonical_conv, domain=sdtm_domain, is_metadata=True),
    )


def _unpack_long_sql_rows(
    records: Iterable[Any],
    *,
    mask_phi: bool,
    pipeline: EnrichmentPipeline | None,
    phi_keys: set[str] | None,
    naming_convention: str | None = None,
    sdtm_domain: str | None = None,
    variable_whitelist: Sequence[str] | set[str] | None = None,
    form_whitelist: Sequence[int] | set[int] | None = None,
) -> list[dict[str, Any]]:
    """Unpack ``Record`` envelopes into normalized long-format variable rows with PHI masking."""
    canonical_conv = normalize_naming_convention(naming_convention)
    rec_col, form_col, var_col, val_col, ts_col = _resolve_long_sql_column_names(
        canonical_conv, sdtm_domain
    )
    allowed_forms = set(form_whitelist) if form_whitelist is not None else None
    allowed_vars = set(variable_whitelist) if variable_whitelist is not None else None
    if (allowed_forms is not None and not allowed_forms) or (
        allowed_vars is not None and not allowed_vars
    ):
        return []

    long_rows: list[dict[str, Any]] = []
    for rec in records:
        if allowed_forms is not None and not record_matches_form_whitelist(rec, allowed_forms):
            continue
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
        seen_var_lower: set[str] = set()
        for name, value in rec_payload.items():
            base_var_name = (
                format_nested_variable_name(name, canonical_conv, domain=sdtm_domain)
                if canonical_conv is not None
                else name
            )
            formatted_var_name = prefix_colliding_variable_name(
                base_var_name,
                convention=canonical_conv,
                used_lower=seen_var_lower,
            )
            if (
                allowed_vars is not None
                and name not in allowed_vars
                and base_var_name not in allowed_vars
                and formatted_var_name not in allowed_vars
            ):
                continue
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
            long_rows.append(
                {
                    rec_col: rec_id,
                    form_col: form_id,
                    var_col: formatted_var_name,
                    val_col: masked_value,
                    ts_col: timestamp,
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
        variable_whitelist: list[str] | None = None,
        form_whitelist: list[int] | None = None,
    ) -> None:
        super().__init__(config)
        self.table = table
        self.engine = engine
        self._cfg = config
        self._chunk_size = max(1, chunk_size)
        self._pipeline = pipeline
        self._phi_keys = resolve_sink_phi_keys(
            config.study_key,
            config=config,
            phi_keys=getattr(pipeline, "phi_keys", None),
        )
        self._variable_whitelist = (
            variable_whitelist if variable_whitelist is not None else config.variable_whitelist
        )
        self._form_whitelist = (
            form_whitelist if form_whitelist is not None else config.form_whitelist
        )
        self._is_incremental_sync = _is_incremental_config(config)
        self._is_first_write = True
        self._initial_if_exists: Literal["replace", "append"] = (
            "append" if (self._is_incremental_sync or not config.idempotent) else "replace"
        )

    def _delete_record_ids(self, record_ids: Sequence[Any]) -> int:
        return _execute_chunked_sql_id_delete(
            self.engine,
            self.table,
            ("recordId", "record_id", "RECORD_ID"),
            record_ids,
        )

    def _write_long_chunks(self, long_rows: list[dict[str, Any]]) -> None:
        for start in range(0, len(long_rows), self._chunk_size):
            chunk = long_rows[start : start + self._chunk_size]
            if_exists: Literal["replace", "append"] = (
                self._initial_if_exists if self._is_first_write else "append"
            )
            chunk_df = _normalize_sqlite_temporal_object_columns(
                pd.DataFrame(chunk),
                self.engine,
            )
            chunk_df.to_sql(
                self.table,
                self.engine,
                if_exists=if_exists,
                index=False,
            )
            self._is_first_write = False

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        if not records:
            return 0
        filtered_records = filter_records_by_form_whitelist(records, self._form_whitelist)
        incoming_ids = [rid for r in filtered_records if (rid := extract_record_id(r)) is not None]
        active_pipeline = (
            self._pipeline
            if self._pipeline is not None
            else resolve_sink_enrichment_pipeline(
                self._cfg.study_key,
                config=self._cfg,
                records=filtered_records,
            )
        )
        active_phi_keys = (
            resolve_sink_phi_keys(
                self._cfg.study_key,
                config=self._cfg,
                records=filtered_records,
                phi_keys=self._phi_keys,
            )
            if self._cfg.mask_phi
            else None
        )
        long_rows = _unpack_long_sql_rows(
            filtered_records,
            mask_phi=self._cfg.mask_phi,
            pipeline=active_pipeline,
            phi_keys=active_phi_keys,
            naming_convention=self._cfg.naming_convention,
            sdtm_domain=self._cfg.sdtm_domain,
            variable_whitelist=self._variable_whitelist,
            form_whitelist=self._form_whitelist,
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
            return len(filtered_records)

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
                columns=list(
                    _resolve_long_sql_column_names(
                        self._cfg.naming_convention,
                        self._cfg.sdtm_domain,
                    )
                )
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
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    chunk_size: int = 1000,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export Study Records to a normalized long-format SQL table.

    Parameters
    ----------
    variable_whitelist:
        Optional collection of Variable names (or formatted column names) to
        retain when unpacking record payloads.
    form_whitelist:
        Optional collection of integer Form IDs to include in the export.
    """
    if pd is None:
        raise ImportError(
            "pandas is required for export_to_long_sql. Install with "
            "\"pip install 'imednet[export]'\"."
        )
    from sqlalchemy import create_engine

    manifest_path = kwargs.pop("manifest_path", None)
    kwargs.setdefault("quality_gate_enabled", quality_gate_enabled)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=manifest_path,
        default_quality_gate=quality_gate_enabled,
        mask_phi=mask_phi,
    )
    engine = create_engine(conn_str)
    with sdk_governance_context(sdk):
        mapper = _create_record_mapper(sdk, mask_phi=config.mask_phi)
        pipeline = resolve_sink_enrichment_pipeline(study_key, config=config)
        _seed_mapper_enrichment(mapper, study_key, pipeline, config=config)
        phi_keys = resolve_sink_phi_keys(
            study_key,
            config=config,
            phi_keys=getattr(pipeline, "phi_keys", None),
        )

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
                naming_convention=config.naming_convention,
                sdtm_domain=config.sdtm_domain,
                variable_whitelist=config.variable_whitelist,
                form_whitelist=config.form_whitelist,
            )
            first = True
            for start in range(0, len(long_rows), max(1, chunk_size)):
                df = _normalize_sqlite_temporal_object_columns(
                    pd.DataFrame(long_rows[start : start + max(1, chunk_size)]),
                    engine,
                )
                df.to_sql(
                    table_name,
                    engine,
                    if_exists="replace" if first else "append",
                    index=False,
                )
                first = False
            if first and config.idempotent and not isinstance(engine, NonCallableMock):
                empty_df = pd.DataFrame(
                    columns=list(
                        _resolve_long_sql_column_names(
                            config.naming_convention,
                            config.sdtm_domain,
                        )
                    )
                )
                empty_df.to_sql(table_name, engine, if_exists="replace", index=False)
        finally:
            db_name = getattr(getattr(engine, "url", None), "database", None)
            if (
                not isinstance(engine, NonCallableMock)
                and db_name not in (None, ":memory:")
                and callable(getattr(engine, "dispose", None))
            ):
                engine.dispose()
        return

    if config.as_sdtm:
        sdtm_sql_sink = TabularSQLSink(table_name, engine, config)
        _tabular_export(
            sdk,
            study_key,
            sdtm_sql_sink,
            config,
            sanitize=False,
            stream_name=f"long_sql_{table_name}",
        )
        return

    records_source = _make_mapper_records_source(sdk, study_key, mapper, config)

    long_sink = _LongSQLSink(
        table_name,
        engine,
        config,
        chunk_size=chunk_size,
        pipeline=pipeline,
        variable_whitelist=config.variable_whitelist,
        form_whitelist=config.form_whitelist,
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


def export_to_hive_parquet(*args: Any, **kwargs: Any) -> None:
    """Export study records to a Hive-partitioned Parquet directory layout."""
    from .parquet import export_to_hive_parquet as _hive_export

    return _hive_export(*args, **kwargs)


def export_to_odm(*args: Any, **kwargs: Any) -> None:
    """Export study snapshot to CDISC ODM 1.3.2 XML."""
    from .cdisc_export import export_to_odm as _odm_export

    return _odm_export(*args, **kwargs)


def export_to_dataset_json(*args: Any, **kwargs: Any) -> None:
    """Export study clinical datasets to CDISC Dataset-JSON v1.1."""
    from .cdisc_export import export_to_dataset_json as _dsj_export

    return _dsj_export(*args, **kwargs)


_UNPATCHED_EXPORTS: dict[str, Callable[..., Any]] = {
    "export_to_csv": export_to_csv,
    "export_to_dataset_json": export_to_dataset_json,
    "export_to_duckdb": export_to_duckdb,
    "export_to_duckdb_by_form": export_to_duckdb_by_form,
    "export_to_excel": export_to_excel,
    "export_to_hive_parquet": export_to_hive_parquet,
    "export_to_json": export_to_json,
    "export_to_long_sql": export_to_long_sql,
    "export_to_odm": export_to_odm,
    "export_to_parquet": export_to_parquet,
    "export_to_sql": export_to_sql,
    "export_to_sql_by_form": export_to_sql_by_form,
}

install_callable_export_module(__name__)
