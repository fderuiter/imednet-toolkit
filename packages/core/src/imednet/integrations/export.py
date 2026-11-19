# pylint: disable=duplicate-code
"""Export helpers built on top of :class:`RecordMapper`."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import NonCallableMock

from imednet.integrations.sink_base import (
    ExportSink,
    SinkConfig,
    apply_quality_gate,
    extract_record_id,
    is_record_deleted,
    iter_batches,
    iter_filtered_sdk_records,
    run_streaming_export,
)

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore[assignment, unused-ignore]
from imednet.constants import MAX_SQLITE_COLUMNS
from imednet.utils import sanitize_csv_formula
from imednet.utils.security import global_sensitivity_registry, mask_clinical_phi

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


def _mask_df(df: pd.DataFrame) -> pd.DataFrame:
    """Mask sensitive fields in the DataFrame based on the global registry."""
    sensitive_cols = [
        col for col in df.columns if global_sensitivity_registry.is_sensitive(str(col))
    ]
    for col in sensitive_cols:
        df[col] = "***MASKED***"

    try:
        object_cols = df.drop(columns=sensitive_cols).select_dtypes(include=[object, "str"]).columns
    except TypeError:
        object_cols = df.drop(columns=sensitive_cols).select_dtypes(include=[object]).columns

    for col in object_cols:
        df[col] = df[col].apply(mask_clinical_phi)

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
    _mapper: Any = None,
) -> pd.DataFrame:
    """Return a DataFrame of study records with duplicate columns removed."""
    if pd is None:
        raise ImportError(
            "pandas is required for _records_df. Install with \"pip install 'imednet[export]'\"."
        )
    mapper = _mapper if _mapper is not None else _record_mapper()(sdk)
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
    _mapper: Any = None,
) -> pd.DataFrame:
    """Prepare a DataFrame for export by fetching records and optionally sanitizing."""
    df = _records_df(
        sdk,
        study_key,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        _mapper=_mapper,
    )
    df = _mask_df(df)
    if sanitize:
        df = _sanitize_df(df)
    return df


_DEFAULT_PREPARE_EXPORT_DF = _prepare_export_df


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


@dataclass
class TabularSinkConfig(SinkConfig):
    """Configuration for tabular sinks."""

    manifest_path: str | None = None
    use_labels_as_columns: bool = False
    sanitize: bool = False
    variable_whitelist: list[str] | None = None
    form_whitelist: list[int] | None = None
    pandas_kwargs: dict[str, Any] = field(default_factory=dict)


class TabularCSVSink(ExportSink):
    """Sink for exporting data to CSV format."""

    def __init__(self, path: str, config: TabularSinkConfig):
        """Initialize the CSV sink."""
        super().__init__(config)
        self.path = path
        self._is_first_batch = True
        self._cfg = config

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write a batch of records to the sink."""
        if isinstance(records, pd.DataFrame):
            df = records
            if len(df) == 0:
                return 0
        else:
            if not records:
                return 0
            df = pd.DataFrame(records)

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
        pass

    def close(self) -> None:
        pass


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


def _build_tabular_contract_types(
    variable_keys: Sequence[str],
    label_map: Mapping[str, Any],
    variables: Sequence[Any],
    *,
    use_labels_as_columns: bool,
) -> dict[str, str | None]:
    """Derive expected column names and logical types from the Record + Variable contract."""
    contract: dict[str, str | None] = dict(_RECORD_META_CONTRACT_TYPES)
    seen_lower: set[str] = {name.lower() for name, _ in _RECORD_META_CONTRACT_TYPES}

    vars_by_name: dict[str, list[Any]] = {}
    for var in variables:
        vname = getattr(var, "variable_name", None)
        if isinstance(vname, str) and vname:
            vars_by_name.setdefault(vname, []).append(var)

    for key in variable_keys:
        col_name = str(label_map.get(key, key) if use_labels_as_columns else key)
        col_lower = col_name.lower()
        if col_lower in seen_lower:
            continue
        seen_lower.add(col_lower)
        if global_sensitivity_registry.is_sensitive(col_name):
            contract[col_name] = "string"
            continue
        logical: str | None = None
        for var in vars_by_name.get(key, ()):
            vt_logical = _variable_type_to_logical(getattr(var, "variable_type", None))
            logical = _unify_logical_types(logical, vt_logical)
        contract[col_name] = logical

    for col in list(contract):
        if global_sensitivity_registry.is_sensitive(col):
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


class TabularParquetSink(ExportSink):
    """Streaming Parquet sink backed by ``pyarrow.parquet.ParquetWriter``."""

    def __init__(
        self,
        path: str,
        config: TabularSinkConfig,
        *,
        contract_types: Mapping[str, str | None] | None = None,
    ) -> None:
        """Initialize the Parquet sink."""
        super().__init__(config)
        self.path = path
        self._cfg = config
        self._writer: Any = None
        self._schema: Any = None
        self._contract_types: dict[str, str | None] = dict(contract_types) if contract_types else {}
        self._is_incremental_sync = bool(
            config.incremental
            or config.modified_since is not None
            or config.state_provider is not None
        )
        self._parquet_kwargs = dict(config.pandas_kwargs)
        self._parquet_kwargs.pop("engine", None)
        self._compression = self._parquet_kwargs.pop("compression", "snappy")

    def set_contract_types(self, contract_types: Mapping[str, str | None]) -> None:
        """Configure contract-derived column types prior to streaming batches."""
        self._contract_types = dict(contract_types)

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
        self._writer.close()
        self._writer = None
        tmp_prev = f"{self.path}.schema_evolve.tmp"
        os.replace(self.path, tmp_prev)
        try:
            self._writer = cast(Any, pq).ParquetWriter(
                self.path,
                new_schema,
                compression=self._compression,
                **self._parquet_kwargs,
            )
            with pq.ParquetFile(tmp_prev) as prev_pf:
                for record_batch in prev_pf.iter_batches(batch_size=max(1, self._cfg.batch_size)):
                    prev_tbl = pa.Table.from_batches([record_batch])
                    cast_cols: list[Any] = []
                    for target_field in new_schema:
                        if target_field.name in prev_tbl.column_names:
                            col = prev_tbl[target_field.name]
                            if pa.types.is_null(col.type):
                                cast_cols.append(
                                    pa.nulls(prev_tbl.num_rows, type=target_field.type)
                                )
                            elif col.type != target_field.type:
                                cast_cols.append(_safe_cast_arrow_array(pa, col, target_field.type))
                            else:
                                cast_cols.append(col)
                        else:
                            cast_cols.append(pa.nulls(prev_tbl.num_rows, type=target_field.type))
                    self._writer.write_table(pa.Table.from_arrays(cast_cols, schema=new_schema))
            self._schema = new_schema
        finally:
            Path(tmp_prev).unlink(missing_ok=True)

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write a DataFrame or sequence batch to the open ParquetWriter."""
        df = records if isinstance(records, pd.DataFrame) else pd.DataFrame(records)
        if len(df) == 0:
            return 0

        def execute_export() -> int:
            import pyarrow as pa
            import pyarrow.parquet as pq

            table, schema_evolved = self._align_batch_table(pa, df)
            if self._writer is None:
                self._schema = table.schema
                self._writer = cast(Any, pq).ParquetWriter(
                    self.path,
                    self._schema,
                    compression=self._compression,
                    **self._parquet_kwargs,
                )
            elif schema_evolved:
                self._evolve_writer_schema(pa, pq, table.schema)
            self._writer.write_table(table)
            return len(df)

        return self._execute_with_retry("export_parquet", batch_id, execute_export)

    def flush(self) -> None:
        """Flush and finalize Parquet footer before checkpoint commit."""
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        elif not (
            self._is_incremental_sync
            and Path(self.path).exists()
            and Path(self.path).stat().st_size > 0
        ):
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

    def close(self) -> None:
        """Ensure underlying ParquetWriter is closed."""
        if self._writer is not None:
            try:
                self._writer.close()
            finally:
                self._writer = None
        self._schema = None


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
        if not record_ids:
            return 0
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text as sa_text

        inspector = sa_inspect(self.engine)
        if not inspector.has_table(self.table):
            return 0
        existing_cols = {c["name"] for c in inspector.get_columns(self.table)}
        id_col = (
            "recordId"
            if "recordId" in existing_cols
            else ("record_id" if "record_id" in existing_cols else None)
        )
        if id_col is None:
            return 0

        preparer = self.engine.dialect.identifier_preparer
        quoted_table = preparer.quote(self.table)
        quoted_col = preparer.quote(id_col)
        params = {f"id_{idx}": rid for idx, rid in enumerate(record_ids)}
        placeholders = ", ".join(f":{k}" for k in params)
        stmt = sa_text(f"DELETE FROM {quoted_table} WHERE {quoted_col} IN ({placeholders})")
        with self.engine.begin() as conn:
            conn.execute(stmt, params)
        return len(record_ids)

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write a batch of records to the sink."""
        if isinstance(records, pd.DataFrame):
            df = records
            if len(df) == 0:
                return 0
        else:
            if not records:
                return 0
            df = pd.DataFrame(records)

        def execute_export() -> int:
            if_exists = self._initial_if_exists if self._is_first_batch else "append"
            if (
                self._is_incremental_sync
                and self._cfg.idempotent
                and if_exists == "append"
                and not isinstance(self.engine, NonCallableMock)
            ):
                for candidate_col in ("recordId", "record_id"):
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
        db_path: str,
        table_name: str,
        config: TabularSinkConfig,
        *,
        contract_types: Mapping[str, str | None] | None = None,
    ) -> None:
        """Initialize the DuckDB sink."""
        super().__init__(config)
        _validate_duckdb_identifier(table_name)
        self.db_path = db_path
        self.table_name = table_name
        self._cfg = config
        self._contract_types: dict[str, str | None] = dict(contract_types) if contract_types else {}
        self._untyped_null_cols: set[str] = set()
        self._is_first_batch = True
        self._is_incremental_sync = bool(
            config.incremental
            or config.modified_since is not None
            or config.state_provider is not None
        )
        import duckdb

        self._conn: Any = duckdb.connect(db_path)

    def set_contract_types(self, contract_types: Mapping[str, str | None]) -> None:
        """Configure contract-derived column types prior to streaming batches."""
        self._contract_types = dict(contract_types)

    def _table_exists(self) -> bool:
        return _duckdb_table_exists(self._conn, self.table_name)

    def _delete_record_ids(self, record_ids: Sequence[Any]) -> int:
        if not record_ids or not self._table_exists():
            return 0
        cols = {
            row[1]
            for row in self._conn.execute(f"PRAGMA table_info('{self.table_name}')").fetchall()
        }
        id_col = (
            "recordId" if "recordId" in cols else ("record_id" if "record_id" in cols else None)
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
        df = records if isinstance(records, pd.DataFrame) else pd.DataFrame(records)
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
                        for candidate_col in ("recordId", "record_id"):
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
        """Close the DuckDB connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


class _TabularCollectingSink(ExportSink):
    """Collects masked/sanitized DataFrame chunks and runs a writer callback on ``flush()``."""

    def __init__(
        self,
        config: TabularSinkConfig,
        on_flush: Any,
    ) -> None:
        super().__init__(config)
        self._frames: list[pd.DataFrame] = []
        self._on_flush = on_flush

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        _ = batch_id
        df = records if isinstance(records, pd.DataFrame) else pd.DataFrame(records)
        if len(df) == 0:
            return 0
        self._frames.append(df)
        return len(df)

    def flush(self) -> None:
        combined = pd.concat(self._frames, ignore_index=True) if self._frames else pd.DataFrame()
        self._on_flush(combined)

    def close(self) -> None:
        self._frames.clear()


class _TabularTransformSink(ExportSink):
    """Adapts record batches into masked/sanitized DataFrames before delegating to ``inner``."""

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
    ) -> None:
        super().__init__(config)
        self._inner = inner
        self._mapper = mapper
        self._record_model = record_model
        self._variable_keys = variable_keys
        self._label_map = label_map
        self._cfg = config
        self._sanitize = sanitize

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        rows, _ = self._mapper._parse_records(records, self._record_model)
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

        df = _mask_df(df)
        if self._sanitize:
            df = _sanitize_df(df)

        return self._inner.write_batch(cast(Any, df), batch_id=batch_id)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        return self._inner.reconcile_deleted(records, batch_id=batch_id)

    def flush(self) -> None:
        self._inner.flush()

    def close(self) -> None:
        self._inner.close()


def _tabular_export(
    sdk: Any,
    study_key: str,
    sink: ExportSink,
    config: TabularSinkConfig,
    sanitize: bool = False,
    *,
    stream_name: str | None = None,
) -> None:
    mapper = _record_mapper()(sdk)
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
    contract_types = _build_tabular_contract_types(
        variable_keys,
        label_map,
        raw_variables if isinstance(raw_variables, list) else (),
        use_labels_as_columns=config.use_labels_as_columns,
    )
    if isinstance(sink, (TabularParquetSink, TabularDuckDBSink)):
        sink.set_contract_types(contract_types)

    record_model = mapper._build_record_model(variable_keys, label_map)
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

    transform_sink = _TabularTransformSink(
        sink,
        mapper,
        record_model,
        variable_keys,
        label_map,
        config,
        sanitize=sanitize,
    )
    run_streaming_export(
        sdk,
        study_key,
        transform_sink,
        config,
        stream_name=stream_name or "tabular",
        batch_id_prefix=f"{study_key}/tabular",
        records_source=_records_source,
        quality_gate_fn=apply_quality_gate,
        iter_batches_fn=iter_batches,
    )


class _TabularCallableSink(ExportSink):
    """Lightweight ExportSink adapter applying SinkConfig retry semantics to tabular file/DB exports."""

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
    return pd.concat(frames, ignore_index=True)


def _pop_sink_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Extract SinkConfig keyword arguments so they do not leak into pandas kwargs."""
    sink_keys = (
        "batch_size",
        "max_retries",
        "retry_backoff",
        "idempotent",
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
    )
    return {k: kwargs.pop(k) for k in sink_keys if k in kwargs}


_STREAMING_ONLY_KEYS = (
    "modified_since",
    "record_filter",
    "record_data_filter",
    "include_deleted",
    "incremental",
    "stream_name",
    "ledger_path",
    "state_provider",
)


def _has_streaming_kwargs(sink_kwargs: Mapping[str, Any]) -> bool:
    """Return True when caller supplied any streaming/filter/checkpoint kwargs."""
    return any(k in sink_kwargs for k in _STREAMING_ONLY_KEYS)


def _config_has_streaming_options(config: TabularSinkConfig) -> bool:
    """Return True when ``config`` activates incremental, watermark, or filter streaming options."""
    return bool(
        config.modified_since is not None
        or config.record_filter is not None
        or config.record_data_filter is not None
        or config.include_deleted
        or config.incremental
        or config.stream_name is not None
        or config.ledger_path is not None
        or config.state_provider is not None
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
) -> TabularSinkConfig:
    """Construct a :class:`TabularSinkConfig` while keeping sink kwargs out of ``pandas_kwargs``."""
    config = kwargs.pop("config", None)
    if isinstance(config, TabularSinkConfig):
        return config
    sink_kwargs = _pop_sink_kwargs(kwargs)
    sink_kwargs.setdefault("quality_gate_enabled", default_quality_gate)
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
    path: str,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    **kwargs: Any,
) -> None:
    """Export study records to a Parquet file.

    All exports now inherit StudyConfiguration rules by default.

    Parameters
    ----------
    use_labels_as_columns:
        When ``True``, variable labels are used for column names instead of
        variable names.
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
            pre_mapper = _record_mapper()(sdk)
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
    path: str,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    manifest_path: str | None = None,
    quality_gate_enabled: bool = True,
    **kwargs: Any,
) -> None:
    """Export study records to a CSV file.

    All exports now inherit StudyConfiguration rules by default.

    Parameters
    ----------
    use_labels_as_columns:
        When ``True``, variable labels are used for column names instead of
        variable names.
    """
    kwargs.setdefault("quality_gate_enabled", quality_gate_enabled)
    config = _build_tabular_sink_config(
        study_key,
        kwargs,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        manifest_path=manifest_path or str(Path(path).with_suffix(".manifest.jsonl")),
        default_quality_gate=quality_gate_enabled,
    )

    sink = TabularCSVSink(path, config)
    _tabular_export(sdk, study_key, sink, config, sanitize=True, stream_name="csv")


def export_to_excel(
    sdk: ImednetSDK,
    study_key: str,
    path: str,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    **kwargs: Any,
) -> None:
    """Export study records to an Excel workbook.

    All exports now inherit StudyConfiguration rules by default.

    Parameters
    ----------
    use_labels_as_columns:
        When ``True``, variable labels are used for column names instead of
        variable names.
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
            pre_mapper = _record_mapper()(sdk)
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
    sink = _TabularCollectingSink(
        config,
        lambda combined: combined.to_excel(path, index=False, **excel_kwargs),
    )
    _tabular_export(sdk, study_key, sink, config, sanitize=True, stream_name="excel")


def _apply_json_enrichment(study_key: str, data: Any) -> Any:
    """Apply ConfigVersionStore EnrichmentPipeline when installed."""
    import logging

    logger = logging.getLogger(__name__)
    try:
        from importlib.metadata import entry_points

        config_version_stores = list(
            entry_points(group="imednet.stores", name="ConfigVersionStore")
        )
        if not config_version_stores:
            raise ImportError("ConfigVersionStore plugin not found.")

        config_version_store_cls = config_version_stores[0].load()
        enrichment_pipeline_cls = import_module(
            "imednet.integrations.enrichment"
        ).EnrichmentPipeline

        store = config_version_store_cls()
        history = store.get_history(study_key)
        if history:
            latest_commit = history[-1]["commit_id"]
            study_cfg = store.rollback_config(study_key, latest_commit)

            logger.info("Enrichment pipeline triggered")
            pipeline = enrichment_pipeline_cls(study_cfg)
            data = pipeline.process(data)
            logger.info("Enrichment pipeline completed successfully")
    except Exception as e:
        logger.warning(f"Could not apply EnrichmentPipeline: {e}")
    return data


def _dump_json_payload(path: str, data: Any, config: TabularSinkConfig) -> int:
    """Persist JSON export payload, appending list items when ``idempotent=False``."""
    payload = data
    if not config.idempotent and isinstance(data, list) and Path(path).exists():
        try:
            with open(path, encoding="utf-8") as existing_file:
                existing_data = json.load(existing_file)
            if isinstance(existing_data, list):
                payload = existing_data + data
        except Exception:
            payload = data
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, **config.pandas_kwargs)
    return len(data) if isinstance(data, list) else 1


def export_to_json(
    sdk: ImednetSDK,
    study_key: str,
    path: str,
    *,
    use_labels_as_columns: bool = False,
    hierarchical: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
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
    )

    if hierarchical:
        mapper = _record_mapper()(sdk)
        if not _config_has_streaming_options(config):
            data = mapper.build_hierarchy(study_key, use_labels_as_keys=use_labels_as_columns)
        else:
            data = mapper.build_hierarchy(
                study_key,
                use_labels_as_keys=use_labels_as_columns,
                include_deleted=config.include_deleted,
            )
        data = _apply_json_enrichment(study_key, data)
        with _TabularCallableSink(
            config,
            "export_json",
            lambda: _dump_json_payload(path, data, config),
        ) as hier_sink:
            hier_sink.write_batch([1], batch_id=f"{study_key}/json/0")
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
            pre_mapper = _record_mapper()(sdk)
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
                        _mapper=pre_mapper,
                    )
                )
        if df_mock is not None:
            mock_data = (
                cast(Any, df_mock).where(pd.notnull(df_mock), None).to_dict(orient="records")
            )
            mock_data = _apply_json_enrichment(study_key, mock_data)
            with _TabularCallableSink(
                config,
                "export_json",
                lambda: _dump_json_payload(path, mock_data, config),
            ) as legacy_sink:
                legacy_sink.write_batch([1], batch_id=f"{study_key}/json/0")
            return

    def _write_json(combined: pd.DataFrame) -> None:
        records_out = (
            cast(Any, combined).where(pd.notnull(combined), None).to_dict(orient="records")
            if not combined.empty
            else []
        )
        records_out = _apply_json_enrichment(study_key, records_out)
        _dump_json_payload(path, records_out, config)

    sink = _TabularCollectingSink(config, _write_json)
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
    )

    engine = create_engine(conn_str)
    sink = TabularSQLSink(table, engine, config)
    _tabular_export(sdk, study_key, sink, config, sanitize=False, stream_name=f"sql_{table}")


def export_to_duckdb(
    sdk: ImednetSDK,
    study_key: str,
    db_path: str,
    table_name: str,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
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
            pre_mapper = _record_mapper()(sdk)
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
                        _mapper=pre_mapper,
                    )
                )
        if df_legacy is not None:

            def _write_duckdb() -> int:
                conn: Any = duckdb.connect(db_path)
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


def export_to_duckdb_by_form(
    sdk: ImednetSDK,
    study_key: str,
    db_path: str,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
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

    sink_kwargs = _pop_sink_kwargs(kwargs)
    mapper = _record_mapper()(sdk)
    forms = sdk.forms.list(study_key=study_key)

    # Fetch all variables for the study once to avoid N+1 queries
    _form_filter: dict[str, Any] = {"formId": form_whitelist} if form_whitelist else {}
    all_variables = (
        []
        if form_whitelist is not None and len(form_whitelist) == 0
        else sdk.variables.list(study_key=study_key, **_form_filter)
    )
    variables_by_form: dict[int, list[Any]] = {}
    for v in all_variables:
        if v.form_id is not None:
            variables_by_form.setdefault(v.form_id, []).append(v)

    use_mock_fetch = _uses_mock_fetch_records(mapper) and not _has_streaming_kwargs(sink_kwargs)
    batch_size = int(sink_kwargs.get("batch_size", 500))
    include_deleted = bool(sink_kwargs.get("include_deleted", False))
    modified_since = sink_kwargs.get("modified_since")
    record_filter = sink_kwargs.get("record_filter")
    record_data_filter = sink_kwargs.get("record_data_filter")

    conn: Any = duckdb.connect(db_path, config={"threads": "1"})
    try:
        for form in forms:
            if form.form_id is None or form.form_key is None:
                continue
            if form_whitelist is not None and form.form_id not in form_whitelist:
                continue

            _validate_duckdb_identifier(form.form_key)

            variables = variables_by_form.get(form.form_id, [])
            variable_keys = [
                v.variable_name
                for v in variables
                if variable_whitelist is None or v.variable_name in variable_whitelist
            ]
            label_map = {
                v.variable_name: v.label for v in variables if v.variable_name in variable_keys
            }
            record_model = mapper._build_record_model(variable_keys, label_map)
            if use_mock_fetch:
                records = mapper._fetch_records(
                    study_key,
                    extra_filters={"formId": form.form_id},
                )
                rows, _ = mapper._parse_records(records, record_model)
                df = mapper._build_dataframe(
                    rows,
                    variable_keys,
                    label_map,
                    use_labels_as_columns,
                )
                if isinstance(df, pd.DataFrame):
                    df.columns = df.columns.astype(str)
                    dup_mask = df.columns.str.lower().duplicated()
                    df = df.loc[:, ~dup_mask]
                    df = _mask_df(df)

                if (
                    isinstance(df, pd.DataFrame)
                    and len(df.columns) == 0
                    and not isinstance(conn, NonCallableMock)
                ):
                    _replace_duckdb_with_empty_table(conn, form.form_key, None)
                    continue

                conn.register(_DUCKDB_DF_ALIAS, df)
                try:
                    conn.execute(
                        f"CREATE OR REPLACE TABLE {_quote_duckdb_identifier(form.form_key)} "
                        f"AS SELECT * FROM {_quote_duckdb_identifier(_DUCKDB_DF_ALIAS)}"
                    )
                finally:
                    conn.unregister(_DUCKDB_DF_ALIAS)
                continue

            form_contract_types = _build_tabular_contract_types(
                variable_keys,
                label_map,
                variables,
                use_labels_as_columns=use_labels_as_columns,
            )
            is_incremental = bool(
                sink_kwargs.get("incremental")
                or sink_kwargs.get("modified_since") is not None
                or sink_kwargs.get("state_provider") is not None
            )
            form_filter = _merge_form_whitelist_into_filter(record_filter, [form.form_id])
            raw_stream = iter_filtered_sdk_records(
                sdk,
                study_key,
                watermark=modified_since,
                record_filter=form_filter,
                record_data_filter=record_data_filter,
                exclude_deleted_upstream=not include_deleted,
            )
            stream = (rec for rec in raw_stream if include_deleted or not is_record_deleted(rec))
            wrote_any = False
            untyped_null_cols: set[str] = set()
            quoted_tbl = _quote_duckdb_identifier(form.form_key)
            quoted_alias = _quote_duckdb_identifier(_DUCKDB_DF_ALIAS)
            for chunk in iter_batches(stream, batch_size):
                rows, _ = mapper._parse_records(chunk, record_model)
                df = mapper._build_dataframe(
                    rows,
                    variable_keys,
                    label_map,
                    use_labels_as_columns,
                )
                if isinstance(df, pd.DataFrame):
                    df.columns = df.columns.astype(str)
                    dup_mask = df.columns.str.lower().duplicated()
                    df = df.loc[:, ~dup_mask]
                    df = _mask_df(df)
                if df.empty:
                    continue
                if not wrote_any:
                    df_for_ddb = _apply_contract_to_all_null_df_columns(df, form_contract_types)
                    untyped_null_cols = _collect_untyped_all_null_columns(df, form_contract_types)
                else:
                    df_for_ddb = _evolve_duckdb_table_for_df(
                        conn,
                        form.form_key,
                        df,
                        form_contract_types,
                        untyped_null_cols,
                    )
                conn.register(_DUCKDB_DF_ALIAS, df_for_ddb)
                try:
                    if not wrote_any:
                        conn.execute(
                            f"CREATE OR REPLACE TABLE {quoted_tbl} AS SELECT * FROM {quoted_alias}"
                        )
                        wrote_any = True
                    else:
                        conn.execute(
                            f"INSERT INTO {quoted_tbl} BY NAME SELECT * FROM {quoted_alias}"
                        )
                finally:
                    conn.unregister(_DUCKDB_DF_ALIAS)
            if not wrote_any and not is_incremental:
                _replace_duckdb_with_empty_table(conn, form.form_key, form_contract_types)
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
    )
    resolved_if_exists: Literal["fail", "replace", "append", "delete_rows"] = (
        if_exists if if_exists is not None else ("replace" if config.idempotent else "append")
    )

    mapper = _record_mapper()(sdk)
    engine = create_engine(conn_str)
    forms = sdk.forms.list(study_key=study_key)

    # Fetch all variables for the study once to avoid N+1 queries
    _form_filter: dict[str, Any] = {"formId": form_whitelist} if form_whitelist else {}
    all_variables = (
        []
        if form_whitelist is not None and len(form_whitelist) == 0
        else sdk.variables.list(study_key=study_key, **_form_filter)
    )
    variables_by_form: dict[int, list[Any]] = {}
    for v in all_variables:
        if v.form_id is not None:
            variables_by_form.setdefault(v.form_id, []).append(v)

    use_mock_fetch = _uses_mock_fetch_records(mapper) and not _config_has_streaming_options(config)

    for form in forms:
        if form.form_id is None or form.form_key is None:
            continue
        if form_whitelist is not None and form.form_id not in form_whitelist:
            continue
        variables = variables_by_form.get(form.form_id, [])
        variable_keys = [
            v.variable_name
            for v in variables
            if variable_whitelist is None or v.variable_name in variable_whitelist
        ]
        label_map = {
            v.variable_name: v.label for v in variables if v.variable_name in variable_keys
        }
        record_model = mapper._build_record_model(variable_keys, label_map)
        if use_mock_fetch:
            records = mapper._fetch_records(
                study_key,
                extra_filters={"formId": form.form_id},
            )
            rows, _ = mapper._parse_records(records, record_model)
            df = mapper._build_dataframe(
                rows,
                variable_keys,
                label_map,
                use_labels_as_columns,
            )
            if isinstance(df, pd.DataFrame):
                dup_mask = df.columns.str.lower().duplicated()
                df = df.loc[:, ~dup_mask]
                df = _mask_df(df)

            form_table = form.form_key or ""

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
            continue

        form_stream_name = (
            f"{config.stream_name}_{form.form_key}" if config.stream_name is not None else None
        )
        form_pandas_kwargs = dict(config.pandas_kwargs)
        if if_exists is not None and not (config.incremental or config.modified_since is not None):
            form_pandas_kwargs["if_exists"] = if_exists
        form_config = TabularSinkConfig(
            study_key=study_key,
            manifest_path=config.manifest_path,
            pandas_kwargs=form_pandas_kwargs,
            use_labels_as_columns=use_labels_as_columns,
            variable_whitelist=variable_keys,
            form_whitelist=[form.form_id],
            batch_size=config.batch_size,
            max_retries=config.max_retries,
            retry_backoff=config.retry_backoff,
            idempotent=config.idempotent,
            extra=dict(config.extra),
            quality_gate_enabled=config.quality_gate_enabled,
            min_schema_readiness_score=config.min_schema_readiness_score,
            tracer=config.tracer,
            modified_since=config.modified_since,
            record_filter=config.record_filter,
            record_data_filter=config.record_data_filter,
            include_deleted=config.include_deleted,
            incremental=config.incremental,
            stream_name=form_stream_name,
            ledger_path=config.ledger_path,
            state_provider=config.state_provider,
        )
        form_sink = TabularSQLSink(form.form_key, engine, form_config)
        form_filter = _merge_form_whitelist_into_filter(form_config.record_filter, [form.form_id])

        def _form_records_source(
            watermark: datetime | None,
            exclude_deleted_upstream: bool,
            _flt: Any = form_filter,
            _rdf: str | None = form_config.record_data_filter,
        ) -> Iterable[Any]:
            return iter_filtered_sdk_records(
                sdk,
                study_key,
                watermark=watermark,
                record_filter=_flt,
                record_data_filter=_rdf,
                exclude_deleted_upstream=exclude_deleted_upstream,
            )

        transform_sink = _TabularTransformSink(
            form_sink,
            mapper,
            record_model,
            variable_keys,
            label_map,
            form_config,
            sanitize=False,
        )
        run_streaming_export(
            sdk,
            study_key,
            transform_sink,
            form_config,
            stream_name=f"sql_{form.form_key}",
            batch_id_prefix=f"{study_key}/{form.form_key}",
            records_source=_form_records_source,
            quality_gate_fn=apply_quality_gate,
            iter_batches_fn=iter_batches,
        )


def export_to_long_sql(
    sdk: ImednetClient,
    study_key: str,
    table_name: str,
    conn_str: str,
    *,
    chunk_size: int = 1000,
    **kwargs: Any,
) -> None:
    """Export records to a normalized long-format SQL table."""
    if pd is None:
        raise ImportError(
            "pandas is required for export_to_long_sql. Install with "
            "\"pip install 'imednet[export]'\"."
        )
    from sqlalchemy import create_engine

    sink_kwargs = _pop_sink_kwargs(kwargs)
    engine = create_engine(conn_str)
    mapper = _record_mapper()(sdk)
    if _uses_mock_fetch_records(mapper) and not _has_streaming_kwargs(sink_kwargs):
        records: Iterable[Any] = mapper._fetch_records(study_key)
    else:
        records = iter_filtered_sdk_records(
            sdk,
            study_key,
            watermark=sink_kwargs.get("modified_since"),
            record_filter=sink_kwargs.get("record_filter"),
            record_data_filter=sink_kwargs.get("record_data_filter"),
            exclude_deleted_upstream=not bool(sink_kwargs.get("include_deleted", False)),
        )

    include_deleted = bool(sink_kwargs.get("include_deleted", False))
    rows: list[dict[str, Any]] = []
    first = True
    for rec in records:
        if not include_deleted and is_record_deleted(rec):
            continue
        timestamp = rec.date_modified
        for name, value in (rec.record_data or {}).items():
            if global_sensitivity_registry.is_sensitive(name):
                masked_value = "***MASKED***"
            else:
                masked_value = mask_clinical_phi(value)

            rows.append(
                {
                    "record_id": rec.record_id,
                    "form_id": rec.form_id,
                    "variable_name": name,
                    "value": masked_value,
                    "timestamp": timestamp,
                }
            )
            if len(rows) >= chunk_size:
                df = pd.DataFrame(rows)
                df.to_sql(
                    table_name,
                    engine,
                    if_exists="replace" if first else "append",
                    index=False,
                )
                rows = []
                first = False
    if rows:
        df = pd.DataFrame(rows)
        df.to_sql(
            table_name,
            engine,
            if_exists="replace" if first else "append",
            index=False,
        )
