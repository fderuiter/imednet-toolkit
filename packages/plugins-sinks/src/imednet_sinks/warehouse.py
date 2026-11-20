# pylint: disable=duplicate-code
"""Snowflake warehouse export sink.

This module implements the **warehouse export path** for a Snowflake
destination.  Study records are:

1. Written to Parquet files in a local staging directory (one file per batch).
2. Uploaded to the configured Snowflake internal stage via ``PUT``.
3. Bulk-loaded into the target table with ``COPY INTO ... FROM @<stage>``.

This two-phase approach decouples data preparation from bulk ingestion,
allows the Parquet files to be independently audited or re-uploaded, and
leverages Snowflake's native columnar loader for best throughput.

Manifest
--------
After each successful ``COPY INTO``, a manifest entry is appended to
``SinkConfig.extra["manifest_path"]`` (if provided):

.. code-block:: json

    {
        "batch_id":   "MYSTUDY/FORM1/0",
        "stage_path": "@MY_STAGE/imednet/MYSTUDY/FORM1/batch_0.parquet",
        "row_count":  500,
        "loaded_at":  "2024-01-15T12:00:00Z"
    }

Optional dependencies
---------------------
* ``snowflake-connector-python`` (``pip install 'imednet[snowflake]'``)
* ``pyarrow`` (included in ``imednet[snowflake]``)

Both are imported lazily at connection / write time.

Idempotency
-----------
When ``SinkConfig.idempotent`` is ``True`` (default) the sink overwrites
staged Parquet files (``PUT ... OVERWRITE = TRUE``), bulk-loads each batch
into a temporary staging table, and executes ``MERGE INTO`` keyed on
``(study_key, record_id)`` so that modified Study Records update in place
without creating duplicate rows.  Set ``idempotent = False`` to load batches
directly into the target table via ``COPY INTO ... FORCE = TRUE``.

Usage
-----
.. code-block:: python

    import os
    from imednet import ImednetSDK
    from imednet.spi.sink_base import iter_batches
    from imednet_sinks import SnowflakeExportSink, SnowflakeSinkConfig

    sdk = ImednetSDK(api_key=os.environ["IMEDNET_API_KEY"])
    records = list(sdk.records.list(study_key="MYSTUDY"))

    config = SnowflakeSinkConfig(
        study_key="MYSTUDY",
        account="myorg-myaccount",
        user="loader",
        password=os.environ["SNOWFLAKE_PASSWORD"],  # keep credentials out of source code
        database="IMEDNET_DB",
        schema="PUBLIC",
        warehouse="COMPUTE_WH",
        stage="MY_STAGE",
        table="RECORDS",
        stage_prefix="imednet",
        local_staging_dir="/tmp/imednet_stage",
    )
    with SnowflakeExportSink(config=config) as sink:
        for i, batch in enumerate(iter_batches(records, config.batch_size)):
            sink.write_batch(batch, batch_id=f"MYSTUDY/FORM1/{i}")
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from imednet.errors import ExportBatchError, ExportConfigurationError
from imednet.sdk import ImednetSDK
from imednet.spi.sink_base import _require_optional_dep
from imednet.spi.sinks import (
    CentralizedMapper,
    ExportSink,
    SinkConfig,
    apply_quality_gate,
    build_sink_mapper,
    extract_record_id,
    run_streaming_export,
)
from imednet.spi.utils import flatten, global_sensitivity_registry, mask_clinical_phi

logger = logging.getLogger(__name__)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_$]+$")
_FLATTENED_PART_PATTERN = re.compile(r"^([A-Za-z0-9_$]+)((?:\[\d+\])*)$")
_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")
_BATCH_UNSAFE_PATTERN = re.compile(r"[^A-Za-z0-9_]+")
_FIRST_CAP_RE = re.compile(r"(.)([A-Z][a-z]+)")
_ALL_CAP_RE = re.compile(r"([a-z0-9])([A-Z])")
_WAREHOUSE_RECORD_COLUMNS: tuple[str, ...] = (
    "study_key",
    "interval_id",
    "form_id",
    "form_key",
    "site_id",
    "record_id",
    "record_oid",
    "record_type",
    "record_status",
    "deleted",
    "date_created",
    "date_modified",
    "subject_id",
    "subject_oid",
    "subject_key",
    "visit_id",
    "parent_record_id",
    "record_data",
    "last_updated",
    "keywords",
)
_RECORD_METADATA_KEYS: frozenset[str] = frozenset(_WAREHOUSE_RECORD_COLUMNS)
_BIGINT_METADATA_COLUMNS: frozenset[str] = frozenset(
    {
        "interval_id",
        "form_id",
        "site_id",
        "record_id",
        "subject_id",
        "visit_id",
        "parent_record_id",
    }
)


def _quote_identifier(ident: str) -> str:
    """Safely quote a Snowflake SQL identifier with double quotes."""
    escaped = ident.replace('"', '""')
    return f'"{escaped}"'


@functools.lru_cache(maxsize=2048)
def _to_warehouse_snake_case(name: str) -> str:
    """Convert an identifier segment into lowercase snake_case."""
    s1 = _FIRST_CAP_RE.sub(r"\1_\2", name)
    return _ALL_CAP_RE.sub(r"\1_\2", s1).lower()


@dataclass
class SnowflakeSinkConfig(SinkConfig):
    """Configuration for :class:`SnowflakeExportSink`.

    Parameters
    ----------
    account:
        Snowflake account identifier (``<org>-<account>`` or legacy format).
    user:
        Snowflake user name.
    password:
        Snowflake password.  Never logged.
    database:
        Target database.
    schema:
        Target schema.
    warehouse:
        Virtual warehouse used for the ``COPY INTO`` command.
    stage:
        Snowflake internal stage name (e.g. ``"MY_STAGE"``).
    table:
        Destination table name inside *database*.*schema*.
    stage_prefix:
        Path prefix inside the stage (default ``"imednet"``).
    prefix:
        Optional alias for ``stage_prefix``.
    local_staging_dir:
        Local directory used to write Parquet files before ``PUT``.
        Defaults to a temporary directory created by :mod:`tempfile`.
    manifest_path:
        Optional path to a JSON-lines file where each loaded batch is
        recorded.
    hard_delete:
        When ``True`` (and ``idempotent=True``), matched records with
        ``deleted=True`` are deleted from the target table during
        ``MERGE INTO`` rather than retained with ``deleted=True`` metadata.
    """

    account: str = ""
    user: str = ""
    password: str = field(default="", repr=False)
    database: str = ""
    schema: str = "PUBLIC"
    warehouse: str = ""
    stage: str = ""
    table: str = ""
    stage_prefix: str = "imednet"
    prefix: str | None = None
    local_staging_dir: str | os.PathLike[str] | None = None
    manifest_path: str | os.PathLike[str] | None = None
    hard_delete: bool = False

    def __post_init__(self) -> None:
        """Validate configuration parameters."""
        super().__post_init__()
        if not isinstance(self.hard_delete, bool):
            raise ExportConfigurationError(
                "SnowflakeSinkConfig parameter 'hard_delete' must be a boolean."
            )
        if self.prefix is not None:
            self.stage_prefix = self.prefix
        self.validate_identifiers(require_coordinates=False)

    def validate_identifiers(self, *, require_coordinates: bool = False) -> None:
        """Validate Snowflake database coordinates and stage prefix against strict patterns."""
        coordinates = {
            "database": self.database,
            "schema": self.schema,
            "warehouse": self.warehouse,
            "stage": self.stage,
            "table": self.table,
        }
        for name, value in coordinates.items():
            if value:
                if not _IDENTIFIER_PATTERN.match(value):
                    raise ExportConfigurationError(
                        f"SnowflakeSinkConfig parameter '{name}' contains invalid characters: {value!r}"
                    )
            elif require_coordinates:
                raise ExportConfigurationError(
                    f"SnowflakeSinkConfig parameter '{name}' must be non-empty."
                )

        prefix_val = self.stage_prefix
        if prefix_val:
            if not _PREFIX_PATTERN.match(prefix_val):
                raise ExportConfigurationError(
                    f"SnowflakeSinkConfig parameter 'stage_prefix' contains invalid characters: {prefix_val!r}"
                )
        elif require_coordinates:
            raise ExportConfigurationError(
                "SnowflakeSinkConfig parameter 'stage_prefix' must be non-empty."
            )

    _validate_identifiers = validate_identifiers


def _normalize_warehouse_value(value: Any) -> Any:
    """Recursively normalize Pydantic models and tuples into JSON-serializable primitives."""
    if value is None or value.__class__ in (str, int, float, bool):
        return value
    if value.__class__ is dict:
        return {str(k): _normalize_warehouse_value(v) for k, v in value.items()}
    if value.__class__ in (list, tuple):
        return [_normalize_warehouse_value(item) for item in value]
    model_dump = getattr(type(value), "model_dump", None)
    if callable(model_dump):
        dumped = value.model_dump(mode="json")
        if dumped is not value and isinstance(dumped, (dict, list, tuple, str, int, float, bool)):
            return _normalize_warehouse_value(dumped)
    if isinstance(value, dict):
        return {str(k): _normalize_warehouse_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_warehouse_value(item) for item in value]
    return value


@functools.lru_cache(maxsize=2048)
def _validate_variable_key_segments(raw_key: str) -> tuple[tuple[str, str], ...]:
    """Validate dot- and list-index-delimited variable key segments against ``_IDENTIFIER_PATTERN``."""
    if not isinstance(raw_key, str) or not raw_key:
        raise ExportConfigurationError(
            f"Warehouse column name contains invalid characters: {raw_key!r}"
        )
    parts = raw_key.split(".")
    if any(not part for part in parts):
        raise ExportConfigurationError(
            f"Warehouse column name contains invalid characters: {raw_key!r}"
        )
    parsed: list[tuple[str, str]] = []
    for part in parts:
        match = _FLATTENED_PART_PATTERN.match(part)
        if match is None:
            raise ExportConfigurationError(
                f"Warehouse column name contains invalid characters: {raw_key!r}"
            )
        base, indices = match.groups()
        parsed.append((base, indices))
    return tuple(parsed)


def _validate_nested_variable_keys(value: Any) -> None:
    """Validate all dictionary keys in *value* prior to flattening so empty unsafe keys still fail."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            _validate_variable_key_segments(str(k) if not isinstance(k, str) else k)
            if isinstance(v, (Mapping, list, tuple)):
                _validate_nested_variable_keys(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (Mapping, list, tuple)):
                _validate_nested_variable_keys(item)


@functools.lru_cache(maxsize=2048)
def _normalize_variable_column(raw_key: str, metadata_keys: frozenset[str]) -> str:
    """Normalize a flattened clinical variable key and prefix ``Record`` metadata collisions with ``var_``."""
    parsed = _validate_variable_key_segments(raw_key)
    normalized_parts: list[str] = []
    for base, indices in parsed:
        snake_base = _to_warehouse_snake_case(base)
        if indices:
            idx_suffix = indices.replace("[", "_").replace("]", "")
            normalized_parts.append(f"{snake_base}{idx_suffix}")
        else:
            normalized_parts.append(snake_base)
    col = "_".join(normalized_parts)
    if col in metadata_keys:
        col = f"var_{col}"
    if not _IDENTIFIER_PATTERN.match(col):
        raise ExportConfigurationError(
            f"Warehouse column name contains invalid characters: {raw_key!r}"
        )
    return col


def _omit_empty_structs(row: dict[str, Any]) -> dict[str, Any]:
    """Omit empty nested dictionaries so PyArrow never infers zero-field struct columns."""
    if not any(isinstance(v, dict) for v in row.values()):
        return row
    cleaned: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, dict):
            nested = _omit_empty_structs(v)
            if not nested:
                continue
            cleaned[k] = nested
        else:
            cleaned[k] = v
    return cleaned


_SCALAR_CLASSES = (str, int, float, bool, type(None))


def _post_process_warehouse(
    row: dict[str, Any],
    *,
    mask_phi: bool = True,
    _record_data_masked: bool = False,
) -> dict[str, Any]:
    """Apply PHI masking, flatten ``record_data`` variables, and protect metadata columns."""
    if any(v.__class__ not in _SCALAR_CLASSES for k, v in row.items() if k != "record_data"):
        normalized_row: dict[str, Any] = _normalize_warehouse_value(row)
    else:
        rd_raw = row.get("record_data")
        if rd_raw is not None and rd_raw.__class__ not in _SCALAR_CLASSES:
            row["record_data"] = _normalize_warehouse_value(rd_raw)
        normalized_row = row

    if mask_phi:
        if not _record_data_masked or any(
            isinstance(k, str) and global_sensitivity_registry.is_sensitive(k)
            for k in normalized_row
            if k != "record_data"
        ):
            masked_row: dict[str, Any] = mask_clinical_phi(normalized_row)
        else:
            masked_row = normalized_row
    else:
        masked_row = normalized_row

    extra_meta = [
        k
        for k in masked_row
        if k != "record_data" and isinstance(k, str) and k not in _RECORD_METADATA_KEYS
    ]
    metadata_keys = (
        _RECORD_METADATA_KEYS | frozenset(extra_meta) if extra_meta else _RECORD_METADATA_KEYS
    )

    raw_payload = masked_row.get("record_data")
    flat_vars: dict[str, Any] = {}
    if raw_payload is None:
        masked_row["record_data"] = "{}"
    elif isinstance(raw_payload, str):
        masked_row["record_data"] = raw_payload
        try:
            parsed_payload = json.loads(raw_payload)
        except ValueError:
            parsed_payload = None
        if isinstance(parsed_payload, Mapping) and parsed_payload:
            masked_parsed = (
                mask_clinical_phi(dict(parsed_payload)) if mask_phi else dict(parsed_payload)
            )
            _validate_nested_variable_keys(masked_parsed)
            flat_vars = flatten(masked_parsed)
    elif isinstance(raw_payload, Mapping):
        payload_dict: dict[str, Any] = (
            raw_payload if isinstance(raw_payload, dict) else dict(raw_payload)
        )
        _validate_nested_variable_keys(payload_dict)
        masked_row["record_data"] = json.dumps(payload_dict, default=str)
        if payload_dict:
            if any(isinstance(v, (Mapping, list, tuple)) for v in payload_dict.values()):
                flat_vars = flatten(payload_dict)
            else:
                flat_vars = payload_dict
    else:
        masked_row["record_data"] = json.dumps(raw_payload, default=str)

    keywords = masked_row.get("keywords")
    if isinstance(keywords, (list, dict, tuple)):
        masked_row["keywords"] = json.dumps(keywords, default=str)

    for var_key, var_val in flat_vars.items():
        col_name = _normalize_variable_column(var_key, metadata_keys)
        if isinstance(var_val, dict) and not var_val:
            continue
        if isinstance(var_val, (dict, list, tuple)):
            masked_row[col_name] = json.dumps(var_val, default=str)
        else:
            masked_row[col_name] = var_val

    return _omit_empty_structs(masked_row)


def _post_process_warehouse_mapped(row: dict[str, Any]) -> dict[str, Any]:
    """Post-process a ``CentralizedMapper`` row whose ``record_data`` was already PHI-masked."""
    return _post_process_warehouse(row, mask_phi=True, _record_data_masked=True)


def _post_process_warehouse_unmasked(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten ``record_data`` variables for Parquet staging without PHI masking."""
    return _post_process_warehouse(row, mask_phi=False)


def _scalar_family(value: Any) -> str:
    """Classify scalar values into Arrow-compatible type families."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if isinstance(value, str):
        return "str"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"
    if isinstance(value, (dict, list, tuple)):
        return "json"
    return "other"


def _harmonize_tabular_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Align column keys across heterogeneous form rows and coerce conflicting scalar types."""
    if not rows:
        return rows

    ordered_cols: list[str] = []
    seen_cols: set[str] = set()
    for row in rows:
        for col in row:
            if col not in seen_cols:
                seen_cols.add(col)
                ordered_cols.append(col)

    if len(rows) == 1 and not any(isinstance(v, (dict, list, tuple)) for v in rows[0].values()):
        return rows

    coerce_to_str: set[str] = set()
    for col in ordered_cols:
        families = {_scalar_family(r[col]) for r in rows if r.get(col) is not None}
        if not families or "other" in families:
            continue
        if "json" in families or len(families) > 1:
            coerce_to_str.add(col)

    if not coerce_to_str and all(len(r) == len(ordered_cols) for r in rows):
        return rows

    harmonized: list[dict[str, Any]] = []
    for row in rows:
        aligned: dict[str, Any] = {}
        for col in ordered_cols:
            val = row.get(col)
            if val is not None and col in coerce_to_str:
                aligned[col] = (
                    json.dumps(val, default=str)
                    if isinstance(val, (dict, list, tuple))
                    else str(val)
                )
            else:
                aligned[col] = val
        harmonized.append(aligned)
    return harmonized


def _resolve_merge_columns(arrow_table: Any) -> list[str]:
    """Resolve and validate column identifiers from *arrow_table* for DDL and MERGE INTO generation."""
    raw_cols = getattr(arrow_table, "column_names", None)
    if (
        isinstance(raw_cols, (list, tuple))
        and raw_cols
        and all(isinstance(col, str) for col in raw_cols)
    ):
        columns = list(raw_cols)
    else:
        columns = list(_WAREHOUSE_RECORD_COLUMNS)

    for col in columns:
        if not _IDENTIFIER_PATTERN.match(col):
            raise ExportConfigurationError(
                f"Warehouse column name contains invalid characters: {col!r}"
            )
    return columns


def _resolve_column_types(arrow_table: Any, columns: Sequence[str]) -> dict[str, str]:
    """Map Arrow schema fields to Snowflake SQL column data types."""
    schema = getattr(arrow_table, "schema", None)
    field_lookup = getattr(schema, "field", None) if schema is not None else None
    resolved: dict[str, str] = {}

    for col in columns:
        sf_type: str | None = None
        if callable(field_lookup):
            try:
                arrow_field = field_lookup(col)
                raw_type = getattr(arrow_field, "type", None)
                type_name = str(raw_type).lower() if raw_type is not None else ""
            except Exception:
                type_name = ""

            if type_name == "bool":
                sf_type = "BOOLEAN"
            elif type_name.startswith(("int", "uint")):
                sf_type = "BIGINT"
            elif type_name.startswith(("float", "double", "halffloat", "decimal")):
                sf_type = "FLOAT"
            elif type_name.startswith("timestamp"):
                sf_type = "TIMESTAMP_TZ"
            elif type_name.startswith("date"):
                sf_type = "DATE"
            elif type_name in ("string", "large_string", "utf8", "large_utf8"):
                sf_type = "VARCHAR"

        if sf_type is None:
            if col == "deleted":
                sf_type = "BOOLEAN"
            elif col in _BIGINT_METADATA_COLUMNS:
                sf_type = "BIGINT"
            else:
                sf_type = "VARCHAR"
        resolved[col] = sf_type

    return resolved


def _records_to_arrow_table(
    records: Sequence[Any],
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
    mapper: Any | None = None,
) -> Any:
    """Convert *records* to a flattened tabular ``pyarrow.Table``."""
    pa = _require_optional_dep("pyarrow", "snowflake")
    for rec in records:
        raw_rd = (
            rec.get("record_data")
            if isinstance(rec, Mapping)
            else getattr(rec, "record_data", None)
        )
        if isinstance(raw_rd, Mapping):
            _validate_nested_variable_keys(raw_rd)
    post_proc = _post_process_warehouse_mapped if mask_phi else _post_process_warehouse_unmasked
    if mapper is None:
        mapper = CentralizedMapper(
            mode="tabular",
            post_processor=post_proc,
            mask_phi=mask_phi,
        )
        raw_rows = [mapper.map_record(r, study_key=study_key) for r in records]
    else:
        mapped_rows = [mapper.map_record(r, study_key=study_key) for r in records]
        if getattr(mapper, "post_processor", None) in (
            _post_process_warehouse,
            _post_process_warehouse_mapped,
            _post_process_warehouse_unmasked,
        ):
            raw_rows = mapped_rows
        else:
            effective_mask = getattr(mapper, "mask_phi", mask_phi) is not False
            fallback_proc = (
                _post_process_warehouse if effective_mask else _post_process_warehouse_unmasked
            )
            raw_rows = [fallback_proc(dict(row)) for row in mapped_rows]

    rows = _harmonize_tabular_rows(raw_rows)
    return pa.Table.from_pylist(rows)


class SnowflakeExportSink(ExportSink):
    """Stage Parquet files and bulk-load them into Snowflake.

    Parameters
    ----------
    config:
        :class:`SnowflakeSinkConfig` containing all connection details and
        staging paths.

    Raises:
        ~imednet.errors.ExportConfigurationError: When the Snowflake connector cannot be initialised or the required
            configuration values are missing.
        ImportError: When ``snowflake-connector-python`` or ``pyarrow`` are not installed.
    """

    def __init__(self, config: SnowflakeSinkConfig) -> None:
        """Initialize the Snowflake export sink.

        Args:
            config: Mandatory Snowflake-specific sink configuration.
        """
        super().__init__(config)
        self._cfg: SnowflakeSinkConfig = config
        self._conn: Any = None
        self._tmp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._connect()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """Establish connection to Snowflake and initialize local staging."""
        cfg = self._cfg
        missing = [
            name
            for name, val in [
                ("account", cfg.account),
                ("user", cfg.user),
                ("password", cfg.password),
                ("database", cfg.database),
                ("warehouse", cfg.warehouse),
                ("stage", cfg.stage),
                ("table", cfg.table),
            ]
            if not val
        ]
        if missing:
            raise ExportConfigurationError(
                f"SnowflakeSinkConfig is missing required fields: {missing}"
            )

        cfg.validate_identifiers(require_coordinates=True)

        snowflake = _require_optional_dep("snowflake.connector", "snowflake")
        logger.debug("Connecting to Snowflake account=%s database=%s", cfg.account, cfg.database)
        try:
            self._conn = snowflake.connect(
                account=cfg.account,
                user=cfg.user,
                password=cfg.password,
                database=cfg.database,
                schema=cfg.schema,
                warehouse=cfg.warehouse,
            )
        except Exception as exc:
            raise ExportConfigurationError(
                f"Cannot connect to Snowflake account '{cfg.account}': {exc}"
            ) from exc

        # Set up local staging directory
        if cfg.local_staging_dir:
            resolved_staging_dir = os.fspath(cfg.local_staging_dir)
            Path(resolved_staging_dir).mkdir(parents=True, exist_ok=True)
            self._staging_dir: str = resolved_staging_dir
        else:
            self._tmp_dir = tempfile.TemporaryDirectory()
            self._staging_dir = self._tmp_dir.name

    # ------------------------------------------------------------------
    # ExportSink interface
    # ------------------------------------------------------------------

    def _ensure_table_schema(
        self,
        cur: Any,
        target_table: str,
        arrow_table: Any,
        columns: Sequence[str],
    ) -> None:
        """Create missing Snowflake destination table or evolve new variable columns."""
        cfg = self._cfg
        col_types = _resolve_column_types(arrow_table, columns)
        info_schema_cols = f"{_quote_identifier(cfg.database)}.INFORMATION_SCHEMA.COLUMNS"
        cur.execute(
            f"SELECT COLUMN_NAME FROM {info_schema_cols} "  # noqa: S608
            f"WHERE UPPER(TABLE_SCHEMA) = UPPER('{cfg.schema}') "
            f"AND UPPER(TABLE_NAME) = UPPER('{cfg.table}')"
        )  # nosem

        fetched = cur.fetchall()
        existing_cols: set[str] = set()
        if hasattr(fetched, "__iter__"):
            for item in fetched:
                if isinstance(item, Mapping) and "COLUMN_NAME" in item:
                    existing_cols.add(str(item["COLUMN_NAME"]).lower())
                elif isinstance(item, Mapping) and "column_name" in item:
                    existing_cols.add(str(item["column_name"]).lower())
                elif isinstance(item, (tuple, list)) and item:
                    existing_cols.add(str(item[0]).lower())
                elif isinstance(item, str):
                    existing_cols.add(item.lower())

        if not existing_cols:
            col_defs = ", ".join(
                f"{_quote_identifier(col.upper())} {col_types[col]}" for col in columns
            )
            cur.execute(f"CREATE TABLE IF NOT EXISTS {target_table} ({col_defs})")  # nosem
            return

        missing_cols = [col for col in columns if col.lower() not in existing_cols]
        if missing_cols:
            add_defs = ", ".join(
                f"{_quote_identifier(col.upper())} {col_types[col]}" for col in missing_cols
            )
            cur.execute(f"ALTER TABLE {target_table} ADD COLUMN {add_defs}")  # nosem

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write *records* to Snowflake via Parquet staging, DDL evolution, and MERGE INTO (or COPY INTO)."""
        if not records:
            return 0

        cfg = self._cfg
        cfg.validate_identifiers(require_coordinates=True)

        safe_batch = _BATCH_UNSAFE_PATTERN.sub("_", batch_id).strip("_")
        if not safe_batch:
            raise ExportBatchError(
                f"Batch identifier {batch_id!r} resolved to an empty string after sanitization.",
                batch_id=batch_id,
            )

        post_proc = (
            _post_process_warehouse_mapped
            if self.config.mask_phi
            else _post_process_warehouse_unmasked
        )
        mapper = build_sink_mapper(cfg, mode="tabular", post_processor=post_proc)

        arrow_table = _records_to_arrow_table(
            records,
            mask_phi=self.config.mask_phi,
            study_key=cfg.study_key,
            mapper=mapper,
        )
        merge_columns = _resolve_merge_columns(arrow_table)
        local_path = Path(self._staging_dir) / f"{safe_batch}.parquet"
        pq = _require_optional_dep("pyarrow.parquet", "snowflake")
        pq.write_table(arrow_table, str(local_path))

        stage_path = f"@{cfg.stage}/{cfg.stage_prefix}/{safe_batch}.parquet"

        def execute_export() -> int:
            """Execute PUT, schema evolution DDL, and staged MERGE INTO (or COPY INTO) in Snowflake."""
            cur = self._conn.cursor()
            try:
                cur.execute(
                    f"PUT file://{local_path} '@{cfg.stage}/{cfg.stage_prefix}/' "
                    f"AUTO_COMPRESS = FALSE OVERWRITE = TRUE"
                )  # nosem
                target_table = (
                    f"{_quote_identifier(cfg.database)}."
                    f"{_quote_identifier(cfg.schema)}."
                    f"{_quote_identifier(cfg.table)}"
                )
                self._ensure_table_schema(cur, target_table, arrow_table, merge_columns)

                if self.config.idempotent:
                    staging_table = (
                        f"{_quote_identifier(cfg.database)}."
                        f"{_quote_identifier(cfg.schema)}."
                        f"{_quote_identifier(f'{cfg.table}__imednet_stage')}"
                    )
                    cur.execute(
                        f"CREATE OR REPLACE TEMPORARY TABLE {staging_table} LIKE {target_table}"
                    )  # nosem
                    cur.execute(
                        f"COPY INTO {staging_table} "
                        f"FROM '{stage_path}' "
                        f"FILE_FORMAT = (TYPE = PARQUET) "
                        f"MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE "
                        f"FORCE = TRUE"
                    )  # nosem
                    non_key_cols = [
                        col for col in merge_columns if col not in ("study_key", "record_id")
                    ]
                    if non_key_cols:
                        update_clause = ", ".join(
                            f"target.{_quote_identifier(col.upper())} = "
                            f"source.{_quote_identifier(col.upper())}"
                            for col in non_key_cols
                        )
                    else:
                        update_clause = 'target."RECORD_ID" = source."RECORD_ID"'
                    insert_cols = ", ".join(_quote_identifier(col.upper()) for col in merge_columns)
                    insert_vals = ", ".join(
                        f"source.{_quote_identifier(col.upper())}" for col in merge_columns
                    )
                    order_expr = (
                        "ORDER BY date_modified DESC NULLS LAST"
                        if "date_modified" in merge_columns
                        else "ORDER BY record_id"
                    )
                    if cfg.hard_delete and "deleted" in merge_columns:
                        matched_clause = (
                            'WHEN MATCHED AND COALESCE(source."DELETED", FALSE) = TRUE THEN DELETE '  # noqa: S608
                            f"WHEN MATCHED THEN UPDATE SET {update_clause} "
                            'WHEN NOT MATCHED AND COALESCE(source."DELETED", FALSE) = FALSE '
                            f"THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
                        )
                    else:
                        matched_clause = (
                            f"WHEN MATCHED THEN UPDATE SET {update_clause} "  # noqa: S608
                            f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
                        )
                    cur.execute(
                        f"MERGE INTO {target_table} AS target "  # noqa: S608
                        f"USING ("
                        f"SELECT * FROM {staging_table} "
                        f"QUALIFY ROW_NUMBER() OVER ("
                        f"PARTITION BY study_key, record_id {order_expr}"
                        f") = 1"
                        f") AS source "
                        f"ON target.study_key = source.study_key "
                        f"AND target.record_id = source.record_id "
                        f"{matched_clause}"
                    )  # nosem
                else:
                    cur.execute(
                        f"COPY INTO {target_table} "
                        f"FROM '{stage_path}' "
                        f"FILE_FORMAT = (TYPE = PARQUET) "
                        f"MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE "
                        f"FORCE = TRUE"
                    )  # nosem
                rows_loaded = len(records)
                logger.debug(
                    "Loaded batch %s (%d rows) via stage %s",
                    batch_id,
                    rows_loaded,
                    stage_path,
                )
                self._append_manifest(batch_id, stage_path, rows_loaded)
                return rows_loaded
            finally:
                cur.close()

        return self._execute_with_retry("export_warehouse", batch_id, execute_export)

    def flush(self) -> None:
        """No-op: each batch is committed individually."""

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Delete newly voided rows from Snowflake when running idempotently."""
        if not self.config.idempotent or not records:
            return 0
        record_ids = [rid for r in records if (rid := extract_record_id(r)) is not None]
        if not record_ids:
            return 0

        cfg = self._cfg
        target_table = (
            f"{_quote_identifier(cfg.database)}."
            f"{_quote_identifier(cfg.schema)}."
            f"{_quote_identifier(cfg.table)}"
        )
        placeholders = ", ".join(["%s"] * len(record_ids))

        def execute_reconcile() -> int:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    f"DELETE FROM {target_table} "  # noqa: S608
                    f"WHERE STUDY_KEY = %s AND RECORD_ID IN ({placeholders})",
                    [cfg.study_key, *record_ids],
                )  # nosem
                logger.debug("Reconciled %d voided records in batch %s", len(record_ids), batch_id)
                return len(record_ids)
            finally:
                cur.close()

        return self._execute_with_retry("reconcile_warehouse_deleted", batch_id, execute_reconcile)

    def close(self) -> None:
        """Close the Snowflake connection and clean up temporary staging files."""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        if self._tmp_dir is not None:
            try:
                self._tmp_dir.cleanup()
            finally:
                self._tmp_dir = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_manifest(self, batch_id: str, stage_path: str, row_count: int) -> None:
        """Append a manifest entry for the loaded batch (JSON-lines format)."""
        manifest_path = self._cfg.manifest_path or self._cfg.extra.get("manifest_path")
        if not manifest_path:
            return
        entry = {
            "batch_id": batch_id,
            "stage_path": stage_path,
            "row_count": row_count,
            "loaded_at": datetime.now(tz=UTC).isoformat(),
        }
        with open(manifest_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + os.linesep)


def export_to_snowflake(
    sdk: ImednetSDK,
    study_key: str,
    *,
    config: SnowflakeSinkConfig,
    mask_phi: bool | None = None,
) -> int:
    """Export study records to Snowflake using :class:`SnowflakeExportSink`."""
    resolved_cfg = replace(config, mask_phi=mask_phi) if mask_phi is not None else config
    stream_cfg = resolved_cfg
    if (
        resolved_cfg.idempotent
        and not resolved_cfg.include_deleted
        and not resolved_cfg.incremental
        and resolved_cfg.modified_since is None
        and resolved_cfg.state_provider is None
    ):
        stream_cfg = replace(resolved_cfg, include_deleted=True)

    run_id = uuid.uuid4().hex[:12]
    return run_streaming_export(
        sdk,
        study_key,
        lambda: SnowflakeExportSink(config=resolved_cfg),
        stream_cfg,
        stream_name=f"snowflake_{resolved_cfg.table}",
        batch_id_prefix=f"{study_key}/records/{run_id}",
        quality_gate_fn=apply_quality_gate,
    )


__all__ = ["SnowflakeExportSink", "SnowflakeSinkConfig", "export_to_snowflake"]
