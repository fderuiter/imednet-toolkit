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

    sdk = ImednetSDK(
        api_key=os.environ["IMEDNET_API_KEY"],
        security_key=os.environ["IMEDNET_SECURITY_KEY"],
    )
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
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Self
from unittest.mock import NonCallableMock

from imednet.errors import ExportBatchError, ExportConfigurationError
from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
from imednet.spi.sink_base import _require_optional_dep, is_record_deleted
from imednet.spi.sinks import (
    AsyncExportSink,
    CentralizedMapper,
    ExportSink,
    SinkConfig,
    apply_quality_gate,
    async_apply_quality_gate,
    async_run_streaming_export,
    bind_sdtm_stream_metadata,
    build_sink_mapper,
    commit_sdtm_lineage,
    deduplicate_sdtm_rows,
    extract_record_id,
    format_variable_name,
    normalize_sink_value,
    redact_batch_records,
    rehydrate_sdtm_lineage,
    resolve_voided_sdtm_keys,
    run_streaming_export,
    should_preserve_sdtm_record_id,
)
from imednet.spi.utils import (
    flatten,
    global_sensitivity_registry,
    is_async_facade,
    mask_clinical_phi,
    maybe_to_thread,
)

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
        When ``True`` (with ``idempotent=True`` and ``include_deleted=True``),
        matched records with ``deleted=True`` are deleted from the target table
        during ``MERGE INTO`` rather than retained as ``deleted=True`` tombstones.
        When ``include_deleted=False`` (default), voided records are routed to
        :meth:`SnowflakeExportSink.reconcile_deleted`.
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


_SCALAR_CLASSES = (str, int, float, bool, type(None))


def _needs_warehouse_normalization(value: Any) -> bool:
    """Return True when *value* contains nested models, tuples, enums, or non-str dict keys."""
    cls = value.__class__
    if cls in _SCALAR_CLASSES:
        return False
    if cls is dict:
        for k, v in value.items():
            if k.__class__ is not str or (
                v.__class__ not in _SCALAR_CLASSES and _needs_warehouse_normalization(v)
            ):
                return True
        return False
    if cls is list:
        return any(
            item.__class__ not in _SCALAR_CLASSES and _needs_warehouse_normalization(item)
            for item in value
        )
    if cls is tuple or isinstance(value, Enum):
        return True
    if callable(getattr(cls, "model_dump", None)):
        return True
    return isinstance(value, (dict, list, tuple))


def _normalize_warehouse_value(value: Any) -> Any:
    """Recursively normalize Pydantic models, enums, and tuples into plain containers."""
    return normalize_sink_value(value)


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
    cls = value.__class__
    if cls is dict or isinstance(value, Mapping):
        for k, v in value.items():
            _validate_variable_key_segments(k if k.__class__ is str else str(k))
            v_cls = v.__class__
            if v_cls not in _SCALAR_CLASSES and (
                v_cls in (dict, list, tuple) or isinstance(v, Mapping)
            ):
                _validate_nested_variable_keys(v)
    elif cls in (list, tuple) or isinstance(value, (list, tuple)):
        for item in value:
            item_cls = item.__class__
            if item_cls not in _SCALAR_CLASSES and (
                item_cls in (dict, list, tuple) or isinstance(item, Mapping)
            ):
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


@functools.lru_cache(maxsize=2048)
def _normalize_variable_column_for_convention(
    raw_key: str,
    metadata_keys: frozenset[str],
    naming_convention: str | None = None,
) -> str:
    """Normalize a flattened variable key according to *naming_convention*."""
    if naming_convention in (None, "snake_case"):
        return _normalize_variable_column(raw_key, metadata_keys)
    parsed = _validate_variable_key_segments(raw_key)
    parts: list[str] = []
    for idx, (base, indices) in enumerate(parsed):
        conv_base = format_variable_name(base, naming_convention)
        if naming_convention == "camelCase" and idx > 0 and conv_base:
            conv_base = conv_base[0].upper() + conv_base[1:]
        if indices:
            idx_suffix = indices.replace("[", "_").replace("]", "")
            parts.append(f"{conv_base}{idx_suffix}")
        else:
            parts.append(conv_base)
    if naming_convention == "camelCase":
        col = "".join(parts)
        if col in metadata_keys:
            col = f"var{col[0].upper()}{col[1:]}" if col else "var"
    else:
        col = "_".join(parts)
        if col in metadata_keys:
            col = f"VAR_{col}"
    if not _IDENTIFIER_PATTERN.match(col):
        raise ExportConfigurationError(
            f"Warehouse column name contains invalid characters: {raw_key!r}"
        )
    return col


def _contains_empty_dict(value: dict[str, Any]) -> bool:
    """Return True when *value* is empty or recursively contains an empty dict."""
    if not value:
        return True
    return any(isinstance(v, dict) and _contains_empty_dict(v) for v in value.values())


def _omit_empty_structs(row: dict[str, Any]) -> dict[str, Any]:
    """Omit empty nested dictionaries so PyArrow never infers zero-field struct columns."""
    if not any(isinstance(v, dict) and _contains_empty_dict(v) for v in row.values()):
        return row
    cleaned: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, dict):
            if not v:
                continue
            nested = _omit_empty_structs(v)
            if not nested:
                continue
            cleaned[k] = nested
        else:
            cleaned[k] = v
    return cleaned


def _has_sensitive_record_metadata() -> bool:
    """Return True if any canonical ``Record`` metadata key is marked sensitive."""
    return any(
        global_sensitivity_registry.is_sensitive(k)
        for k in _RECORD_METADATA_KEYS
        if k != "record_data"
    )


_DEFAULT_VAR_COL_CACHE: dict[str, str] = {}


def _post_process_warehouse(
    row: dict[str, Any],
    *,
    mask_phi: bool = True,
    _record_data_masked: bool = False,
    _sensitive_metadata: bool | None = None,
    _record_data_validated: bool = False,
    naming_convention: str | None = None,
) -> dict[str, Any]:
    """Apply PHI masking, flatten ``record_data`` variables, and protect metadata columns."""
    is_default_conv = naming_convention in (None, "snake_case")
    rd_key = (
        "record_data"
        if is_default_conv
        else ("recordData" if naming_convention == "camelCase" else "RECORD_DATA")
    )
    kw_key = "KEYWORDS" if naming_convention == "sdtm" else "keywords"

    has_nested_dict = False
    for k, v in row.items():
        if k == rd_key:
            continue
        v_cls = v.__class__
        if v_cls not in _SCALAR_CLASSES:
            if k == kw_key and v_cls is list and not v:
                row[k] = "[]"
                continue
            if isinstance(v, Enum):
                row[k] = v.value if v.value.__class__ is str else str(v)
                continue
            if _needs_warehouse_normalization(v):
                norm_v = _normalize_warehouse_value(v)
                row[k] = norm_v
                if k != kw_key and (norm_v.__class__ is dict or isinstance(norm_v, dict)):
                    has_nested_dict = True
            elif k != kw_key and (v_cls is dict or isinstance(v, dict)):
                has_nested_dict = True

    base_meta = (
        _RECORD_METADATA_KEYS
        if is_default_conv
        else frozenset(
            format_variable_name(c, naming_convention, is_metadata=True)
            for c in _WAREHOUSE_RECORD_COLUMNS
        )
    )
    extra_meta: list[str] = (
        []
        if row.keys() <= base_meta
        else [k for k in row if k not in base_meta and isinstance(k, str)]
    )

    need_row_mask = False
    if mask_phi:
        if not _record_data_masked:
            need_row_mask = True
        else:
            sensitive_meta = (
                _has_sensitive_record_metadata()
                if _sensitive_metadata is None
                else _sensitive_metadata
            )
            need_row_mask = sensitive_meta or bool(
                extra_meta and any(global_sensitivity_registry.is_sensitive(k) for k in extra_meta)
            )

    rd_normalized = False
    if need_row_mask:
        rd_raw = row.get(rd_key)
        if (
            rd_raw is not None
            and rd_raw.__class__ not in _SCALAR_CLASSES
            and _needs_warehouse_normalization(rd_raw)
        ):
            row[rd_key] = _normalize_warehouse_value(rd_raw)
        rd_normalized = True
        masked_row: dict[str, Any] = mask_clinical_phi(row)
    else:
        masked_row = row

    metadata_keys = base_meta | frozenset(extra_meta) if extra_meta else base_meta

    raw_payload = masked_row.get(rd_key)
    flat_vars: dict[str, Any] = {}
    if raw_payload is None:
        masked_row[rd_key] = "{}"
    elif raw_payload.__class__ is str or isinstance(raw_payload, str):
        masked_row[rd_key] = raw_payload
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
    elif raw_payload.__class__ is dict or isinstance(raw_payload, Mapping):
        payload_dict: dict[str, Any] = (
            raw_payload if raw_payload.__class__ is dict else dict(raw_payload)
        )
        if payload_dict:
            has_nested_payload = False
            all_json_primitives = True
            needs_rd_norm = False
            for pk, pv in payload_dict.items():
                if not rd_normalized and pk.__class__ is not str:
                    needs_rd_norm = True
                pv_cls = pv.__class__
                if pv_cls not in _SCALAR_CLASSES:
                    all_json_primitives = False
                    if not rd_normalized and _needs_warehouse_normalization(pv):
                        needs_rd_norm = True
                    elif pv_cls in (dict, list, tuple) or isinstance(pv, Mapping):
                        has_nested_payload = True
            if needs_rd_norm:
                payload_dict = _normalize_warehouse_value(payload_dict)
                has_nested_payload = any(
                    v.__class__ not in _SCALAR_CLASSES
                    and (v.__class__ in (dict, list, tuple) or isinstance(v, Mapping))
                    for v in payload_dict.values()
                )
            if not _record_data_validated:
                _validate_nested_variable_keys(payload_dict)
            masked_row[rd_key] = (
                json.dumps(payload_dict)
                if all_json_primitives and not needs_rd_norm
                else json.dumps(payload_dict, default=str)
            )
            flat_vars = flatten(payload_dict) if has_nested_payload else payload_dict
        else:
            masked_row[rd_key] = "{}"
    else:
        if not rd_normalized and _needs_warehouse_normalization(raw_payload):
            raw_payload = _normalize_warehouse_value(raw_payload)
        masked_row[rd_key] = json.dumps(raw_payload, default=str)

    keywords = masked_row.get(kw_key)
    if keywords.__class__ not in _SCALAR_CLASSES and isinstance(keywords, (list, dict, tuple)):
        masked_row[kw_key] = json.dumps(keywords, default=str)

    default_col_cache = _DEFAULT_VAR_COL_CACHE if (is_default_conv and not extra_meta) else None
    for var_key, var_val in flat_vars.items():
        if default_col_cache is not None:
            col_name_cached = default_col_cache.get(var_key)
            if col_name_cached is None:
                col_name = _normalize_variable_column(var_key, _RECORD_METADATA_KEYS)
                if len(default_col_cache) < 2048:
                    default_col_cache[var_key] = col_name
            else:
                col_name = col_name_cached
        elif is_default_conv:
            col_name = _normalize_variable_column(var_key, metadata_keys)
        else:
            col_name = _normalize_variable_column_for_convention(
                var_key,
                metadata_keys,
                naming_convention=naming_convention,
            )
        val_cls = var_val.__class__
        if val_cls in _SCALAR_CLASSES:
            masked_row[col_name] = var_val
        elif isinstance(var_val, Enum):
            masked_row[col_name] = var_val.value if var_val.value.__class__ is str else str(var_val)
        elif isinstance(var_val, dict) and not var_val:
            continue
        elif isinstance(var_val, (dict, list, tuple)):
            masked_row[col_name] = json.dumps(var_val, default=str)
        else:
            masked_row[col_name] = var_val

    if has_nested_dict:
        return _omit_empty_structs(masked_row)
    return masked_row


def _post_process_warehouse_sdtm(
    row: dict[str, Any],
    *,
    mask_phi: bool = True,
    _already_masked: bool = False,
) -> dict[str, Any]:
    """Normalize and validate an SDTM domain row for Snowflake Parquet staging."""
    normalized = _normalize_warehouse_value(row)
    if mask_phi and not _already_masked:
        normalized = redact_batch_records(normalized, mask_phi=True, as_sdtm=True)
    cleaned: dict[str, Any] = {}
    for col, val in normalized.items():
        if not isinstance(col, str) or not _IDENTIFIER_PATTERN.match(col):
            raise ExportConfigurationError(
                f"Warehouse column name contains invalid characters: {col!r}"
            )
        if isinstance(val, dict) and not val:
            continue
        if isinstance(val, (dict, list, tuple)):
            cleaned[col] = json.dumps(val, default=str)
        else:
            cleaned[col] = val
    return _omit_empty_structs(cleaned)


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
    if isinstance(value, Decimal):
        return "decimal"
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


_EXACT_SCALAR_FAMILIES: dict[type[Any], str] = {
    str: "str",
    int: "num",
    float: "num",
    bool: "bool",
    Decimal: "decimal",
    datetime: "datetime",
    date: "date",
    dict: "json",
    list: "json",
    tuple: "json",
}


def _coerce_cell_to_str(val: Any) -> str:
    """Convert a conflicting non-null cell value into a string for Arrow coercion."""
    if isinstance(val, (dict, list, tuple)):
        return json.dumps(val, default=str)
    if isinstance(val, Enum):
        return val.value if val.value.__class__ is str else str(val)
    return str(val)


_UNSEEN_CELL = object()
_MIXED_CELL_CLS = object()


def _harmonize_tabular_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Align column keys across heterogeneous form rows and coerce conflicting scalar types."""
    if not rows:
        return rows

    ordered_cols: list[str] = []
    col_exact_cls: dict[str, Any] = {}
    col_families: dict[str, str | None] = {}
    exact_families = _EXACT_SCALAR_FAMILIES
    unseen = _UNSEEN_CELL
    mixed_cls = _MIXED_CELL_CLS

    for row in rows:
        for col, val in row.items():
            expected_cls = col_exact_cls.get(col, unseen)
            if val is None:
                if expected_cls is unseen:
                    ordered_cols.append(col)
                    col_exact_cls[col] = None
                    col_families[col] = None
                continue
            v_cls = val.__class__
            if v_cls is expected_cls:
                continue
            if expected_cls is unseen:
                ordered_cols.append(col)
                fam = exact_families.get(v_cls)
                if fam is None:
                    fam = _scalar_family(val)
                    col_exact_cls[col] = mixed_cls
                elif fam == "json":
                    col_exact_cls[col] = mixed_cls
                else:
                    col_exact_cls[col] = v_cls
                col_families[col] = "coerce" if fam == "json" else fam
            elif expected_cls is None:
                fam = exact_families.get(v_cls)
                if fam is None:
                    fam = _scalar_family(val)
                    col_exact_cls[col] = mixed_cls
                elif fam == "json":
                    col_exact_cls[col] = mixed_cls
                else:
                    col_exact_cls[col] = v_cls
                col_families[col] = "coerce" if fam == "json" else fam
            else:
                col_exact_cls[col] = mixed_cls
                prev = col_families[col]
                if prev == "other":
                    continue
                fam = exact_families.get(v_cls)
                if fam is None:
                    fam = _scalar_family(val)
                if fam == "other":
                    col_families[col] = "other"
                elif prev != "coerce" and (fam == "json" or fam != prev):
                    col_families[col] = "coerce"

    coerce_to_str: set[str] = {col for col, state in col_families.items() if state == "coerce"}
    first_row = rows[0]
    needs_first_align = len(first_row) < len(ordered_cols)

    if not coerce_to_str:
        if not needs_first_align:
            return rows
        aligned_first = dict(first_row)
        for col in ordered_cols[len(first_row) :]:
            aligned_first[col] = None
        return [aligned_first, *rows[1:]]

    aligned_first = dict(first_row)
    for col in coerce_to_str:
        val = aligned_first.get(col)
        if val is not None and val.__class__ is not str:
            aligned_first[col] = _coerce_cell_to_str(val)
    if needs_first_align:
        for col in ordered_cols[len(first_row) :]:
            aligned_first[col] = None

    harmonized: list[dict[str, Any]] = [aligned_first]
    for row in rows[1:]:
        copied: dict[str, Any] | None = None
        for col in coerce_to_str:
            val = row.get(col)
            if val is not None and val.__class__ is not str:
                if copied is None:
                    copied = dict(row)
                copied[col] = _coerce_cell_to_str(val)
        harmonized.append(row if copied is None else copied)
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
            elif type_name.startswith("decimal"):
                scale = getattr(raw_type, "scale", None)
                if isinstance(scale, int) and 0 <= scale <= 37:
                    sf_type = f"NUMBER(38, {scale})"
                else:
                    sf_type = "NUMBER(38, 9)"
            elif type_name.startswith(("float", "double", "halffloat")):
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
            elif col in _BIGINT_METADATA_COLUMNS or col == "RECORD_ID":
                sf_type = "BIGINT"
            else:
                sf_type = "VARCHAR"
        resolved[col] = sf_type

    return resolved


def _canonical_snowflake_type(dtype: str) -> str:
    """Normalize a Snowflake SQL or ``INFORMATION_SCHEMA`` ``DATA_TYPE`` into a canonical family."""
    upper = dtype.strip().upper()
    base = upper.split("(", 1)[0].strip()
    if base in {
        "VARCHAR",
        "TEXT",
        "STRING",
        "CHAR",
        "CHARACTER",
        "NVARCHAR",
        "NVARCHAR2",
        "CHAR VARYING",
        "CHARACTER VARYING",
    }:
        return "VARCHAR"
    if base in {"BOOLEAN", "BOOL"}:
        return "BOOLEAN"
    if base in {"FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "REAL"}:
        return "FLOAT"
    if base == "DATE":
        return "DATE"
    if base.startswith("TIMESTAMP") or base == "DATETIME":
        return "TIMESTAMP_TZ"
    if base in {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "BYTEINT"}:
        return "BIGINT"
    if base in {"NUMBER", "NUMERIC", "DECIMAL", "DEC"}:
        if "(" not in upper:
            return "NUMBER"
        parts = upper.split("(", 1)[1].rstrip(")").split(",")
        if len(parts) == 2 and parts[1].strip() == "0":
            return "BIGINT"
        return "DECIMAL"
    return upper


def _is_null_arrow_column(arrow_table: Any, col: str) -> bool:
    """Return ``True`` when *col* in *arrow_table* has Arrow ``null`` type (all-null batch)."""
    schema = getattr(arrow_table, "schema", None)
    field_lookup = getattr(schema, "field", None) if schema is not None else None
    if not callable(field_lookup):
        return False
    try:
        raw_type = getattr(field_lookup(col), "type", None)
    except Exception:
        return False
    return raw_type is not None and str(raw_type).strip().lower() == "null"


def _should_widen_to_varchar(
    existing_dtype: str | None,
    desired_dtype: str,
    *,
    is_null_col: bool,
) -> bool:
    """Return ``True`` when an existing Snowflake column must be widened to ``VARCHAR``."""
    if existing_dtype is None or is_null_col:
        return False
    existing_canon = _canonical_snowflake_type(existing_dtype)
    if existing_canon == "VARCHAR":
        return False
    desired_canon = _canonical_snowflake_type(desired_dtype)
    if existing_canon == desired_canon:
        return False
    return not (existing_canon == "NUMBER" and desired_canon in {"BIGINT", "DECIMAL"})


def _resolve_merge_keys(
    columns: Sequence[str],
    *,
    as_sdtm: bool = False,
    naming_convention: str | None = None,
    sdtm_domain: str | None = None,
) -> tuple[str, ...]:
    """Resolve Snowflake ``MERGE INTO`` natural key columns for the active schema mode."""
    col_set = set(columns)
    if as_sdtm:
        if "STUDYID" in col_set and "RECORD_ID" in col_set:
            return ("STUDYID", "RECORD_ID")
        dom_upper = sdtm_domain.strip().upper() if isinstance(sdtm_domain, str) else ""
        if dom_upper == "DM":
            seq_col: str | None = None
        elif dom_upper and f"{dom_upper}SEQ" in col_set:
            seq_col = f"{dom_upper}SEQ"
        else:
            seq_col = next(
                (
                    c
                    for c in columns
                    if c.endswith("SEQ") and c not in ("STUDYID", "DOMAIN", "USUBJID", "DMSEQ")
                ),
                None,
            )
        candidates = (
            ("STUDYID", "DOMAIN", "USUBJID", seq_col)
            if seq_col is not None
            else ("STUDYID", "DOMAIN", "USUBJID")
        )
        present = tuple(c for c in candidates if c in col_set)
        return present if present else (columns[0],)
    if naming_convention == "camelCase":
        return ("studyKey", "recordId")
    if naming_convention == "sdtm":
        return ("STUDYID", "RECORD_ID")
    return ("study_key", "record_id")


def _resolve_sdtm_domain_merge_partitions(
    arrow_table: Any,
    merge_columns: Sequence[str],
    *,
    sdtm_domain: str | None = None,
) -> list[tuple[str | None, tuple[str, ...]]]:
    """Resolve ``(domain_filter, merge_keys)`` partitions for single- or mixed-domain SDTM merges."""
    if "STUDYID" in merge_columns and "RECORD_ID" in merge_columns:
        return [(None, ("STUDYID", "RECORD_ID"))]

    domains_in_batch: list[str] = []
    seen_domains: set[str] = set()
    col_names = getattr(arrow_table, "column_names", ())
    if (
        isinstance(col_names, (list, tuple))
        and "DOMAIN" in col_names
        and hasattr(arrow_table, "column")
    ):
        try:
            for val in arrow_table.column("DOMAIN").to_pylist():
                if val is not None:
                    dom_str = str(val).strip().upper()
                    if dom_str and dom_str not in seen_domains:
                        seen_domains.add(dom_str)
                        domains_in_batch.append(dom_str)
        except Exception:
            domains_in_batch = []

    if len(domains_in_batch) <= 1:
        single_dom = domains_in_batch[0] if domains_in_batch else sdtm_domain
        return [
            (
                None,
                _resolve_merge_keys(
                    merge_columns,
                    as_sdtm=True,
                    sdtm_domain=single_dom,
                ),
            )
        ]

    return [
        (
            dom,
            _resolve_merge_keys(
                merge_columns,
                as_sdtm=True,
                sdtm_domain=dom,
            ),
        )
        for dom in domains_in_batch
    ]


def _records_to_arrow_table(
    records: Sequence[Any],
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
    mapper: Any | None = None,
    as_sdtm: bool = False,
    sdtm_domain: str | None = None,
    naming_convention: str | None = None,
) -> Any:
    """Convert *records* to a flattened tabular ``pyarrow.Table``."""
    pa = _require_optional_dep("pyarrow", "snowflake")
    last_model_cls: type[Any] | None = None
    for rec in records:
        rec_cls = rec.__class__
        if rec_cls is last_model_cls:
            raw_rd = rec.record_data
        elif rec_cls is dict or isinstance(rec, Mapping):
            raw_rd = rec.get("record_data")
        else:
            raw_rd = getattr(rec, "record_data", None)
            if "record_data" in getattr(rec_cls, "model_fields", ()):
                last_model_cls = rec_cls
        if raw_rd is not None and (raw_rd.__class__ is dict or isinstance(raw_rd, Mapping)):
            _validate_nested_variable_keys(raw_rd)
    mapper_as_sdtm = bool(as_sdtm or getattr(mapper, "as_sdtm", False))
    mapper_convention = getattr(mapper, "naming_convention", naming_convention)
    is_default_conv = mapper_convention in (None, "snake_case")

    def _make_mapped_post_proc(batch_sensitive: bool, conv: str | None = None) -> Any:
        def _batch_post_proc_mapped(row: dict[str, Any]) -> dict[str, Any]:
            return _post_process_warehouse(
                row,
                mask_phi=True,
                _record_data_masked=True,
                _sensitive_metadata=batch_sensitive,
                _record_data_validated=True,
                naming_convention=conv,
            )

        return _batch_post_proc_mapped

    def _make_unmasked_post_proc(conv: str | None = None) -> Any:
        def _batch_post_proc_unmasked(row: dict[str, Any]) -> dict[str, Any]:
            return _post_process_warehouse(
                row,
                mask_phi=False,
                _record_data_validated=True,
                naming_convention=conv,
            )

        return _batch_post_proc_unmasked

    if mapper is None:
        if mapper_as_sdtm:
            sdtm_proc = functools.partial(
                _post_process_warehouse_sdtm,
                mask_phi=mask_phi,
                _already_masked=mask_phi,
            )
            mapper = CentralizedMapper(
                mode="tabular",
                post_processor=sdtm_proc,
                mask_phi=mask_phi,
                as_sdtm=True,
                sdtm_domain=sdtm_domain,
            )
            raw_rows = deduplicate_sdtm_rows(mapper.map_batch(records, study_key=study_key))
        else:
            post_proc = (
                _make_mapped_post_proc(False, mapper_convention)
                if mask_phi
                else _make_unmasked_post_proc(mapper_convention)
            )
            mapper = CentralizedMapper(
                mode="tabular",
                post_processor=post_proc,
                mask_phi=mask_phi,
                naming_convention=None if is_default_conv else mapper_convention,
            )
            raw_rows = [mapper.map_record(r, study_key=study_key) for r in records]
    elif mapper_as_sdtm:
        if isinstance(mapper, CentralizedMapper):
            raw_sdtm = mapper.map_batch(records, study_key=study_key)
        else:
            raw_sdtm = [mapper.map_record(r, study_key=study_key) for r in records]
        if raw_sdtm and any("RECORD_ID" in r for r in raw_sdtm):
            dedup_by_rec: dict[tuple[Any, Any], int] = {}
            staged_sdtm: list[dict[str, Any] | None] = []
            without_rid: list[dict[str, Any]] = []
            for r_row in raw_sdtm:
                if "RECORD_ID" not in r_row or r_row.get("RECORD_ID") is None:
                    without_rid.append(r_row)
                    continue
                rk = (r_row.get("STUDYID"), r_row.get("RECORD_ID"))
                if rk in dedup_by_rec:
                    staged_sdtm[dedup_by_rec[rk]] = None
                dedup_by_rec[rk] = len(staged_sdtm)
                staged_sdtm.append(r_row)
            mapped_rows = [r_row for r_row in staged_sdtm if r_row is not None]
            if without_rid:
                mapped_rows.extend(deduplicate_sdtm_rows(without_rid))
        else:
            mapped_rows = deduplicate_sdtm_rows(raw_sdtm)
        orig_post_proc = getattr(mapper, "post_processor", None)
        if getattr(orig_post_proc, "func", orig_post_proc) is _post_process_warehouse_sdtm:
            raw_rows = mapped_rows
        else:
            effective_mask = getattr(mapper, "mask_phi", mask_phi) is not False
            raw_rows = [
                _post_process_warehouse_sdtm(
                    dict(row),
                    mask_phi=effective_mask,
                    _already_masked=effective_mask,
                )
                for row in mapped_rows
            ]
    else:
        orig_post_proc = getattr(mapper, "post_processor", None)
        if isinstance(mapper, CentralizedMapper) and orig_post_proc in (
            _post_process_warehouse_mapped,
            _post_process_warehouse_unmasked,
        ):
            mapper.post_processor = (
                _make_mapped_post_proc(
                    False if mapper.mask_phi else _has_sensitive_record_metadata(),
                    mapper_convention,
                )
                if orig_post_proc is _post_process_warehouse_mapped
                else _make_unmasked_post_proc(mapper_convention)
            )
            try:
                raw_rows = [mapper.map_record(r, study_key=study_key) for r in records]
            finally:
                mapper.post_processor = orig_post_proc
        else:
            mapped_rows = [mapper.map_record(r, study_key=study_key) for r in records]
            if (
                orig_post_proc
                in (
                    _post_process_warehouse,
                    _post_process_warehouse_mapped,
                    _post_process_warehouse_unmasked,
                )
                or getattr(orig_post_proc, "func", None) is _post_process_warehouse
            ):
                raw_rows = mapped_rows
            else:
                effective_mask = getattr(mapper, "mask_phi", mask_phi) is not False
                is_tabular_centralized = (
                    isinstance(mapper, CentralizedMapper) and mapper.mode == "tabular"
                )
                centralized_masked_meta = (
                    isinstance(mapper, CentralizedMapper) and mapper.mask_phi is True
                )
                batch_sensitive = (
                    False
                    if (not effective_mask or centralized_masked_meta)
                    else _has_sensitive_record_metadata()
                )
                raw_rows = [
                    _post_process_warehouse(
                        dict(row),
                        mask_phi=effective_mask,
                        _record_data_masked=effective_mask and is_tabular_centralized,
                        _sensitive_metadata=batch_sensitive,
                        _record_data_validated=is_tabular_centralized,
                        naming_convention=mapper_convention,
                    )
                    for row in mapped_rows
                ]

    rows = _harmonize_tabular_rows(raw_rows)
    return pa.Table.from_pylist(rows)


class _SnowflakeSinkCoreMixin:
    """Shared Snowflake staging, schema evolution, SQL generation, and SDTM lineage logic."""

    config: SinkConfig
    _cfg: SnowflakeSinkConfig
    _seq_counters: dict[str, int]
    _domain_seq_counters: dict[str, dict[str, int]]
    _record_seqs: dict[str, int]
    _form_domain_map_ref: dict[Any, str]
    _seq_overrides_ref: dict[str, int]
    _lineage_out_ref: dict[str, dict[str, Any]]
    _mapper: CentralizedMapper | None
    _mapper_key: tuple[Any, ...] | None
    _synced_sdtm_session_id: int | None
    _ensured_tables: set[str]
    _conn: Any
    _tmp_dir: tempfile.TemporaryDirectory[str] | None
    _staging_dir: str

    def _init_warehouse_state(self, config: SnowflakeSinkConfig) -> None:
        """Initialize shared Snowflake mapping, DDL cache, and connection attributes."""
        self._cfg = config
        self._seq_counters = {}
        self._domain_seq_counters = {}
        self._record_seqs = {}
        self._form_domain_map_ref = {}
        self._seq_overrides_ref = {}
        self._lineage_out_ref = {}
        self._mapper = None
        self._mapper_key = None
        self._synced_sdtm_session_id = None
        self._ensured_tables = set()
        self._conn = None
        self._tmp_dir = None

    def bind_stream_metadata(self, metadata: dict[str, Any]) -> None:
        """Hydrate and bind SDTM sequence counters into transactional stream metadata."""
        if self._cfg.as_sdtm:
            bind_sdtm_stream_metadata(metadata, self._seq_counters, self._record_seqs)

    def _validate_required_snowflake_config(self) -> None:
        """Validate mandatory Snowflake connection and staging parameters."""
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

    def _connect(self) -> None:
        """Establish connection to Snowflake and initialize local staging."""
        self._validate_required_snowflake_config()
        cfg = self._cfg
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

        if cfg.local_staging_dir:
            resolved_staging_dir = os.fspath(cfg.local_staging_dir)
            Path(resolved_staging_dir).mkdir(parents=True, exist_ok=True)
            self._staging_dir = resolved_staging_dir
        else:
            self._tmp_dir = tempfile.TemporaryDirectory()
            self._staging_dir = self._tmp_dir.name

    def _table_exists(self, cur: Any, target_table: str) -> bool:
        """Return ``True`` when *target_table* was ensured or exists in ``INFORMATION_SCHEMA``."""
        if target_table in self._ensured_tables:
            return True
        cfg = self._cfg
        info_schema_cols = f"{_quote_identifier(cfg.database)}.INFORMATION_SCHEMA.COLUMNS"
        cur.execute(
            f"SELECT COLUMN_NAME FROM {info_schema_cols} "  # noqa: S608
            f"WHERE TABLE_SCHEMA = '{cfg.schema}' "
            f"AND TABLE_NAME = '{cfg.table}'"
        )  # nosem
        fetched = cur.fetchall()
        if isinstance(fetched, NonCallableMock):
            return True
        if hasattr(fetched, "__iter__"):
            for item in fetched:
                if item:
                    self._ensured_tables.add(target_table)
                    return True
        logger.debug(
            "Skipping voided record reconciliation for %s (table does not exist yet)",
            target_table,
        )
        return False

    def _ensure_table_schema(
        self,
        cur: Any,
        target_table: str,
        arrow_table: Any,
        columns: Sequence[str],
    ) -> None:
        """Create missing Snowflake destination table or evolve variable columns and types."""
        cfg = self._cfg
        col_types = _resolve_column_types(arrow_table, columns)
        info_schema_cols = f"{_quote_identifier(cfg.database)}.INFORMATION_SCHEMA.COLUMNS"
        cur.execute(
            f"SELECT COLUMN_NAME, DATA_TYPE FROM {info_schema_cols} "  # noqa: S608
            f"WHERE TABLE_SCHEMA = '{cfg.schema}' "
            f"AND TABLE_NAME = '{cfg.table}'"
        )  # nosem

        fetched = cur.fetchall()
        existing_cols: dict[str, tuple[str, str | None]] = {}
        if hasattr(fetched, "__iter__"):
            for item in fetched:
                raw_name: Any = None
                raw_dtype: Any = None
                if isinstance(item, Mapping):
                    if "COLUMN_NAME" in item:
                        raw_name = item["COLUMN_NAME"]
                    elif "column_name" in item:
                        raw_name = item["column_name"]
                    if "DATA_TYPE" in item:
                        raw_dtype = item["DATA_TYPE"]
                    elif "data_type" in item:
                        raw_dtype = item["data_type"]
                elif isinstance(item, (tuple, list)) and item:
                    raw_name = item[0]
                    if len(item) >= 2:
                        raw_dtype = item[1]
                elif isinstance(item, str):
                    raw_name = item
                if raw_name is not None:
                    name_str = str(raw_name)
                    dtype_str = (
                        str(raw_dtype).strip().upper()
                        if isinstance(raw_dtype, str) and raw_dtype.strip()
                        else None
                    )
                    existing_cols[name_str.lower()] = (name_str, dtype_str)

        if not existing_cols:
            col_defs = ", ".join(
                f"{_quote_identifier(col.upper())} {col_types[col]}" for col in columns
            )
            cur.execute(f"CREATE TABLE IF NOT EXISTS {target_table} ({col_defs})")  # nosem
            self._ensured_tables.add(target_table)
            return

        for col in columns:
            col_lower = col.lower()
            if col_lower not in existing_cols:
                continue
            actual_name, existing_dtype = existing_cols[col_lower]
            if _should_widen_to_varchar(
                existing_dtype,
                col_types[col],
                is_null_col=_is_null_arrow_column(arrow_table, col),
            ):
                safe_existing = (
                    actual_name if _IDENTIFIER_PATTERN.match(actual_name) else col.upper()
                )
                quoted_existing = _quote_identifier(safe_existing)
                quoted_target = _quote_identifier(col.upper())
                quoted_swap = _quote_identifier(f"{col.upper()}__IMEDNET_TMP")
                cur.execute(f"ALTER TABLE {target_table} ADD COLUMN {quoted_swap} VARCHAR")  # nosem
                cur.execute(
                    f"UPDATE {target_table} SET {quoted_swap} = TO_VARCHAR({quoted_existing})"  # noqa: S608
                )  # nosem
                cur.execute(f"ALTER TABLE {target_table} DROP COLUMN {quoted_existing}")  # nosem
                cur.execute(
                    f"ALTER TABLE {target_table} RENAME COLUMN {quoted_swap} TO {quoted_target}"
                )  # nosem

        missing_cols = [col for col in columns if col.lower() not in existing_cols]
        if missing_cols:
            add_defs = ", ".join(
                f"{_quote_identifier(col.upper())} {col_types[col]}" for col in missing_cols
            )
            cur.execute(f"ALTER TABLE {target_table} ADD COLUMN {add_defs}")  # nosem
        self._ensured_tables.add(target_table)

    def _get_mapper(
        self,
        post_proc: Any,
        seq_overrides: dict[str, int] | None,
        domain_seq_counters: dict[str, dict[str, int]] | None = None,
    ) -> CentralizedMapper:
        """Return a reusable sink-scoped mapper synchronized for the active warehouse batch."""
        cfg = self._cfg
        self._form_domain_map_ref.clear()
        active_map = getattr(self, "_active_form_domain_map", None)
        if isinstance(active_map, Mapping):
            self._form_domain_map_ref.update(active_map)
        if seq_overrides is not None and self._seq_overrides_ref is not seq_overrides:
            self._seq_overrides_ref = seq_overrides
        self._lineage_out_ref.clear()

        live_domain_counters = (
            domain_seq_counters if domain_seq_counters is not None else self._domain_seq_counters
        )
        live_seq_counters = (
            self._seq_counters if (not cfg.as_sdtm or cfg.sdtm_domain is not None) else None
        )

        extra = cfg.extra if isinstance(cfg.extra, dict) else {}
        key = (
            bool(self.config.mask_phi),
            bool(cfg.as_sdtm),
            cfg.sdtm_domain,
            cfg.naming_convention,
            should_preserve_sdtm_record_id(cfg),
            id(extra.get("study_config")),
            id(extra.get("enrichment_pipeline")),
        )
        if self._mapper is None or self._mapper_key != key:
            mapper_cfg = (
                replace(cfg, mask_phi=self.config.mask_phi)
                if cfg.mask_phi != self.config.mask_phi
                else cfg
            )
            self._mapper = build_sink_mapper(
                mapper_cfg,
                mode="tabular",
                post_processor=post_proc,
                seq_counters=live_seq_counters,
                domain_seq_counters=live_domain_counters if cfg.as_sdtm else None,
                form_domain_map=self._form_domain_map_ref,
                seq_overrides=seq_overrides if cfg.as_sdtm else None,
                lineage_out=self._lineage_out_ref if cfg.as_sdtm else None,
                record_seqs=self._record_seqs,
            )
            self._mapper_key = key
        else:
            self._mapper.post_processor = post_proc
            if cfg.as_sdtm:
                self._mapper.bind_sdtm_lineage(
                    domain_seq_counters=live_domain_counters,
                    seq_overrides=seq_overrides,
                    seq_counters=live_seq_counters,
                )
        return self._mapper

    def _prepare_arrow_batch(
        self,
        active_records: Sequence[Any],
    ) -> tuple[Any, list[str], dict[str, dict[str, Any]]]:
        """Map *active_records* into an Arrow table and return ``(arrow_table, merge_columns, lineage_out)``."""
        cfg = self._cfg
        domain_seq_counters: dict[str, dict[str, int]] | None = None
        seq_overrides: dict[str, int] | None = None
        if cfg.as_sdtm:
            domain_seq_counters, seq_overrides, _ = rehydrate_sdtm_lineage(self)
            session_id = id(domain_seq_counters)
            if self._synced_sdtm_session_id != session_id:
                if self._domain_seq_counters:
                    for dom_code, subj_map in self._domain_seq_counters.items():
                        live_map = domain_seq_counters.setdefault(dom_code, {})
                        for subj_key, wm_val in subj_map.items():
                            live_map[subj_key] = max(live_map.get(subj_key, 0), wm_val)
                self._synced_sdtm_session_id = session_id
            post_proc: Any = functools.partial(
                _post_process_warehouse_sdtm,
                mask_phi=self.config.mask_phi,
                _already_masked=self.config.mask_phi,
            )
        elif cfg.naming_convention not in (None, "snake_case"):
            post_proc = functools.partial(
                _post_process_warehouse,
                mask_phi=self.config.mask_phi,
                _record_data_masked=self.config.mask_phi,
                naming_convention=cfg.naming_convention,
            )
        else:
            post_proc = (
                _post_process_warehouse_mapped
                if self.config.mask_phi
                else _post_process_warehouse_unmasked
            )
        mapper = self._get_mapper(post_proc, seq_overrides, domain_seq_counters)
        lineage_out = self._lineage_out_ref

        arrow_table = _records_to_arrow_table(
            active_records,
            mask_phi=self.config.mask_phi,
            study_key=cfg.study_key,
            mapper=mapper,
            as_sdtm=cfg.as_sdtm,
            sdtm_domain=cfg.sdtm_domain,
            naming_convention=cfg.naming_convention,
        )
        merge_columns = _resolve_merge_columns(arrow_table)
        return arrow_table, merge_columns, lineage_out

    def _execute_staged_load_sync(
        self,
        local_path: Path,
        stage_path: str,
        arrow_table: Any,
        merge_columns: list[str],
        active_record_count: int,
        batch_id: str,
    ) -> int:
        """Execute ``PUT``, schema evolution DDL, and staged ``MERGE INTO`` (or ``COPY INTO``) in Snowflake."""
        cfg = self._cfg
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
                insert_cols = ", ".join(_quote_identifier(col.upper()) for col in merge_columns)
                insert_vals = ", ".join(
                    f"source.{_quote_identifier(col.upper())}" for col in merge_columns
                )
                deleted_col = next(
                    (c for c in ("deleted", "DELETED") if c in merge_columns),
                    None,
                )
                merge_partitions = (
                    _resolve_sdtm_domain_merge_partitions(
                        arrow_table,
                        merge_columns,
                        sdtm_domain=cfg.sdtm_domain,
                    )
                    if cfg.as_sdtm
                    else [
                        (
                            None,
                            _resolve_merge_keys(
                                merge_columns,
                                as_sdtm=False,
                                naming_convention=cfg.naming_convention,
                            ),
                        )
                    ]
                )
                for domain_filter, merge_keys in merge_partitions:
                    non_key_cols = [col for col in merge_columns if col not in merge_keys]
                    if non_key_cols:
                        update_clause = ", ".join(
                            f"target.{_quote_identifier(col.upper())} = "
                            f"source.{_quote_identifier(col.upper())}"
                            for col in non_key_cols
                        )
                    else:
                        first_key = _quote_identifier(merge_keys[0].upper())
                        update_clause = f"target.{first_key} = source.{first_key}"

                    if not cfg.as_sdtm and cfg.naming_convention in (None, "snake_case"):
                        partition_expr = "study_key, record_id"
                        order_expr = (
                            "ORDER BY date_modified DESC NULLS LAST"
                            if "date_modified" in merge_columns
                            else "ORDER BY record_id"
                        )
                        on_expr = (
                            "target.study_key = source.study_key "
                            "AND target.record_id = source.record_id"
                        )
                    else:
                        partition_expr = ", ".join(_quote_identifier(k.upper()) for k in merge_keys)
                        modified_col = next(
                            (
                                c
                                for c in ("date_modified", "dateModified", "DATE_MODIFIED")
                                if c in merge_columns
                            ),
                            None,
                        )
                        order_expr = (
                            f"ORDER BY {_quote_identifier(modified_col.upper())} DESC NULLS LAST"
                            if modified_col is not None
                            else f"ORDER BY {_quote_identifier(merge_keys[-1].upper())}"
                        )
                        on_expr = " AND ".join(
                            f"target.{_quote_identifier(k.upper())} = "
                            f"source.{_quote_identifier(k.upper())}"
                            for k in merge_keys
                        )
                    if cfg.hard_delete and deleted_col is not None:
                        quoted_del = _quote_identifier(deleted_col.upper())
                        matched_clause = (
                            f"WHEN MATCHED AND COALESCE(source.{quoted_del}, FALSE) = TRUE THEN DELETE "  # noqa: S608
                            f"WHEN MATCHED THEN UPDATE SET {update_clause} "
                            f"WHEN NOT MATCHED AND COALESCE(source.{quoted_del}, FALSE) = FALSE "
                            f"THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
                        )
                    else:
                        matched_clause = (
                            f"WHEN MATCHED THEN UPDATE SET {update_clause} "  # noqa: S608
                            f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
                        )
                    if domain_filter is None:
                        cur.execute(
                            f"MERGE INTO {target_table} AS target "  # noqa: S608
                            f"USING ("
                            f"SELECT * FROM {staging_table} "
                            f"QUALIFY ROW_NUMBER() OVER ("
                            f"PARTITION BY {partition_expr} {order_expr}"
                            f") = 1"
                            f") AS source "
                            f"ON {on_expr} "
                            f"{matched_clause}"
                        )  # nosem
                    else:
                        cur.execute(
                            f"MERGE INTO {target_table} AS target "  # noqa: S608
                            f"USING ("
                            f'SELECT * FROM {staging_table} WHERE "DOMAIN" = %s '
                            f"QUALIFY ROW_NUMBER() OVER ("
                            f"PARTITION BY {partition_expr} {order_expr}"
                            f") = 1"
                            f") AS source "
                            f"ON {on_expr} "
                            f"{matched_clause}",
                            [domain_filter],
                        )  # nosem
            else:
                cur.execute(
                    f"COPY INTO {target_table} "
                    f"FROM '{stage_path}' "
                    f"FILE_FORMAT = (TYPE = PARQUET) "
                    f"MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE "
                    f"FORCE = TRUE"
                )  # nosem
            rows_loaded = getattr(arrow_table, "num_rows", active_record_count)
            if not isinstance(rows_loaded, int):
                rows_loaded = active_record_count
            logger.debug(
                "Loaded batch %s (%d rows) via stage %s",
                batch_id,
                rows_loaded,
                stage_path,
            )
            self._append_manifest(batch_id, stage_path, rows_loaded)
            return int(rows_loaded)
        finally:
            cur.close()

    def _execute_sdtm_reconcile_sync(
        self,
        target_table: str,
        sdtm_entries: Sequence[dict[str, Any]],
        batch_id: str,
    ) -> int:
        """Delete voided SDTM rows from *target_table* synchronously."""
        cur = self._conn.cursor()
        deleted = 0
        try:
            if not self._table_exists(cur, target_table):
                return 0
            for entry in sdtm_entries:
                st_id = entry.get("study_id")
                dom = str(entry.get("domain") or "").strip().upper()
                usubj = entry.get("usubjid")
                seq_col = entry.get("seq_col")
                seq_val = entry.get("seq_val")
                if not st_id or not dom or not usubj:
                    continue
                if dom == "DM" or seq_col is None:
                    cur.execute(
                        f"DELETE FROM {target_table} "  # noqa: S608
                        'WHERE "STUDYID" = %s AND "DOMAIN" = %s AND "USUBJID" = %s',
                        [st_id, dom, usubj],
                    )  # nosem
                    deleted += 1
                elif seq_val is not None and _IDENTIFIER_PATTERN.match(str(seq_col)):
                    q_seq = _quote_identifier(str(seq_col).upper())
                    cur.execute(
                        f"DELETE FROM {target_table} "  # noqa: S608
                        f'WHERE "STUDYID" = %s AND "DOMAIN" = %s '
                        f'AND "USUBJID" = %s AND {q_seq} = %s',
                        [st_id, dom, usubj, int(seq_val)],
                    )  # nosem
                    deleted += 1
            logger.debug("Reconciled %d voided SDTM records in batch %s", deleted, batch_id)
            return deleted
        except Exception as exc:
            if getattr(exc, "errno", None) == 2003 or getattr(exc, "sqlstate", None) == "42S02":
                logger.debug(
                    "Skipping voided SDTM record reconciliation for %s (table does not exist yet)",
                    target_table,
                )
                return 0
            raise
        finally:
            cur.close()

    def _execute_record_reconcile_sync(
        self,
        target_table: str,
        where_clause: str,
        record_ids: Sequence[Any],
        batch_id: str,
    ) -> int:
        """Delete voided record rows by ``RECORD_ID`` from *target_table* synchronously."""
        cur = self._conn.cursor()
        try:
            if not self._table_exists(cur, target_table):
                return 0
            cur.execute(
                f"DELETE FROM {target_table} "  # noqa: S608
                f"WHERE {where_clause}",
                [self._cfg.study_key, *record_ids],
            )  # nosem
            logger.debug("Reconciled %d voided records in batch %s", len(record_ids), batch_id)
            return len(record_ids)
        except Exception as exc:
            if getattr(exc, "errno", None) == 2003 or getattr(exc, "sqlstate", None) == "42S02":
                logger.debug(
                    "Skipping voided record reconciliation for %s (table does not exist yet)",
                    target_table,
                )
                return 0
            raise
        finally:
            cur.close()

    def _close_sync(self) -> None:
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


class SnowflakeExportSink(_SnowflakeSinkCoreMixin, ExportSink):
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

    _delegates_sdtm_redaction = True
    reconciles_deleted_on_full_sync = True

    def __init__(self, config: SnowflakeSinkConfig) -> None:
        """Initialize the Snowflake export sink.

        Args:
            config: Mandatory Snowflake-specific sink configuration.
        """
        super().__init__(config)
        self._init_warehouse_state(config)
        self._connect()

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

        active_records: Sequence[Any] = records
        should_reconcile_in_batch = self.config.idempotent and (
            not cfg.include_deleted or (cfg.as_sdtm and cfg.hard_delete)
        )
        if should_reconcile_in_batch:
            voided_records = [r for r in records if is_record_deleted(r)]
            if voided_records:
                self.reconcile_deleted(voided_records, batch_id=batch_id)
                active_records = [r for r in records if not is_record_deleted(r)]
                if not active_records:
                    return 0
        elif not cfg.include_deleted:
            active_records = [r for r in records if not is_record_deleted(r)]
            if not active_records:
                return 0

        arrow_table, merge_columns, lineage_out = self._prepare_arrow_batch(active_records)
        local_path = Path(self._staging_dir) / f"{safe_batch}.parquet"
        pq = _require_optional_dep("pyarrow.parquet", "snowflake")
        pq.write_table(arrow_table, str(local_path))

        stage_path = f"@{cfg.stage}/{cfg.stage_prefix}/{safe_batch}.parquet"

        def execute_export() -> int:
            return self._execute_staged_load_sync(
                local_path,
                stage_path,
                arrow_table,
                merge_columns,
                len(active_records),
                batch_id,
            )

        written_total = self._execute_with_retry("export_warehouse", batch_id, execute_export)
        if cfg.as_sdtm and lineage_out:
            commit_sdtm_lineage(self, lineage_out)
        return written_total

    def flush(self) -> None:
        """No-op: each batch is committed individually."""

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Delete newly voided rows from Snowflake when running idempotently."""
        if not self.config.idempotent or not records:
            return 0
        cfg = self._cfg
        cfg.validate_identifiers(require_coordinates=True)
        target_table = (
            f"{_quote_identifier(cfg.database)}."
            f"{_quote_identifier(cfg.schema)}."
            f"{_quote_identifier(cfg.table)}"
        )

        record_ids = [rid for r in records if (rid := extract_record_id(r)) is not None]
        if cfg.as_sdtm and (not should_preserve_sdtm_record_id(cfg) or not record_ids):
            sdtm_entries = resolve_voided_sdtm_keys(
                records,
                self,
                study_key=getattr(self, "_active_study_key", None) or cfg.study_key,
                stream_name=getattr(self, "_active_stream_name", None) or cfg.stream_name,
                sdtm_domain=cfg.sdtm_domain,
                form_domain_map=getattr(self, "_active_form_domain_map", None),
            )
            if not sdtm_entries:
                return 0

            return self._execute_with_retry(
                "reconcile_warehouse_deleted",
                batch_id,
                lambda: self._execute_sdtm_reconcile_sync(target_table, sdtm_entries, batch_id),
            )

        if not record_ids:
            return 0

        placeholders = ", ".join(["%s"] * len(record_ids))
        if cfg.as_sdtm or cfg.naming_convention == "sdtm":
            where_clause = f'"STUDYID" = %s AND "RECORD_ID" IN ({placeholders})'
        elif cfg.naming_convention == "camelCase":
            where_clause = f'"STUDYKEY" = %s AND "RECORDID" IN ({placeholders})'
        else:
            where_clause = f"STUDY_KEY = %s AND RECORD_ID IN ({placeholders})"

        return self._execute_with_retry(
            "reconcile_warehouse_deleted",
            batch_id,
            lambda: self._execute_record_reconcile_sync(
                target_table,
                where_clause,
                record_ids,
                batch_id,
            ),
        )

    def close(self) -> None:
        """Close the Snowflake connection and clean up temporary staging files."""
        self._close_sync()


class AsyncSnowflakeExportSink(_SnowflakeSinkCoreMixin, AsyncExportSink):
    """Asynchronously stage Parquet files and bulk-load them into Snowflake without blocking the event loop."""

    _delegates_sdtm_redaction = True
    reconciles_deleted_on_full_sync = True

    def __init__(self, config: SnowflakeSinkConfig) -> None:
        """Initialize the asynchronous Snowflake export sink."""
        super().__init__(config)
        self._init_warehouse_state(config)
        self._validate_required_snowflake_config()
        _require_optional_dep("snowflake.connector", "snowflake")
        self._staging_dir = ""
        self._connected: bool = False

    async def __aenter__(self) -> Self:
        """Establish Snowflake connection and local staging without blocking the event loop."""
        await self._ensure_connected()
        return await super().__aenter__()

    async def _ensure_connected(self) -> None:
        """Lazily initialize the Snowflake connection and staging directory off the event loop."""
        if self._connected and self._conn is not None:
            return
        await maybe_to_thread(self._connect)
        self._connected = True

    async def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Asynchronously stage *records* to Parquet and execute Snowflake ``PUT`` + ``MERGE INTO``/``COPY INTO``."""
        await self._ensure_connected()
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

        active_records: Sequence[Any] = records
        should_reconcile_in_batch = self.config.idempotent and (
            not cfg.include_deleted or (cfg.as_sdtm and cfg.hard_delete)
        )
        if should_reconcile_in_batch:
            voided_records = [r for r in records if is_record_deleted(r)]
            if voided_records:
                await self.reconcile_deleted(voided_records, batch_id=batch_id)
                active_records = [r for r in records if not is_record_deleted(r)]
                if not active_records:
                    return 0
        elif not cfg.include_deleted:
            active_records = [r for r in records if not is_record_deleted(r)]
            if not active_records:
                return 0

        arrow_table, merge_columns, lineage_out = self._prepare_arrow_batch(active_records)
        local_path = Path(self._staging_dir) / f"{safe_batch}.parquet"
        pq = _require_optional_dep("pyarrow.parquet", "snowflake")
        await maybe_to_thread(pq.write_table, arrow_table, str(local_path))

        stage_path = f"@{cfg.stage}/{cfg.stage_prefix}/{safe_batch}.parquet"

        async def execute_export() -> int:
            return await maybe_to_thread(
                self._execute_staged_load_sync,
                local_path,
                stage_path,
                arrow_table,
                merge_columns,
                len(active_records),
                batch_id,
            )

        written_total = await self._execute_with_retry("export_warehouse", batch_id, execute_export)
        if cfg.as_sdtm and lineage_out:
            commit_sdtm_lineage(self, lineage_out)
        return written_total

    async def flush(self) -> None:
        """No-op: each batch is committed individually."""

    async def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Asynchronously delete newly voided rows from Snowflake when running idempotently."""
        if not self.config.idempotent or not records:
            return 0
        await self._ensure_connected()
        cfg = self._cfg
        cfg.validate_identifiers(require_coordinates=True)
        target_table = (
            f"{_quote_identifier(cfg.database)}."
            f"{_quote_identifier(cfg.schema)}."
            f"{_quote_identifier(cfg.table)}"
        )

        record_ids = [rid for r in records if (rid := extract_record_id(r)) is not None]
        if cfg.as_sdtm and (not should_preserve_sdtm_record_id(cfg) or not record_ids):
            sdtm_entries = resolve_voided_sdtm_keys(
                records,
                self,
                study_key=getattr(self, "_active_study_key", None) or cfg.study_key,
                stream_name=getattr(self, "_active_stream_name", None) or cfg.stream_name,
                sdtm_domain=cfg.sdtm_domain,
                form_domain_map=getattr(self, "_active_form_domain_map", None),
            )
            if not sdtm_entries:
                return 0

            async def execute_sdtm_reconcile() -> int:
                return await maybe_to_thread(
                    self._execute_sdtm_reconcile_sync,
                    target_table,
                    sdtm_entries,
                    batch_id,
                )

            return await self._execute_with_retry(
                "reconcile_warehouse_deleted",
                batch_id,
                execute_sdtm_reconcile,
            )

        if not record_ids:
            return 0

        placeholders = ", ".join(["%s"] * len(record_ids))
        if cfg.as_sdtm or cfg.naming_convention == "sdtm":
            where_clause = f'"STUDYID" = %s AND "RECORD_ID" IN ({placeholders})'
        elif cfg.naming_convention == "camelCase":
            where_clause = f'"STUDYKEY" = %s AND "RECORDID" IN ({placeholders})'
        else:
            where_clause = f"STUDY_KEY = %s AND RECORD_ID IN ({placeholders})"

        async def execute_reconcile() -> int:
            return await maybe_to_thread(
                self._execute_record_reconcile_sync,
                target_table,
                where_clause,
                record_ids,
                batch_id,
            )

        return await self._execute_with_retry(
            "reconcile_warehouse_deleted",
            batch_id,
            execute_reconcile,
        )

    async def close(self) -> None:
        """Asynchronously close the Snowflake connection and clean up temporary staging files."""
        await maybe_to_thread(self._close_sync)
        self._connected = False


def export_to_snowflake(
    sdk: ImednetFacade,
    study_key: str,
    *,
    config: SnowflakeSinkConfig,
    mask_phi: bool | None = None,
) -> int:
    """Export study records to Snowflake using :class:`SnowflakeExportSink`."""
    if is_async_facade(sdk):
        raise TypeError(
            "export_to_snowflake() requires a synchronous ImednetSDK (ImednetFacade); "
            "use await async_export_to_snowflake(...) with AsyncImednetSDK."
        )
    resolved_cfg = replace(config, mask_phi=mask_phi) if mask_phi is not None else config

    run_id = uuid.uuid4().hex[:12]
    return run_streaming_export(
        sdk,
        study_key,
        lambda: SnowflakeExportSink(config=resolved_cfg),
        resolved_cfg,
        stream_name=f"snowflake_{resolved_cfg.table}",
        batch_id_prefix=f"{study_key}/records/{run_id}",
        quality_gate_fn=apply_quality_gate,
    )


async def async_export_to_snowflake(
    sdk: AsyncImednetFacade,
    study_key: str,
    *,
    config: SnowflakeSinkConfig,
    mask_phi: bool | None = None,
) -> int:
    """Asynchronously export study records to Snowflake using :class:`AsyncSnowflakeExportSink`."""
    if not is_async_facade(sdk):
        raise TypeError(
            "async_export_to_snowflake() requires an AsyncImednetSDK (AsyncImednetFacade); "
            "use export_to_snowflake(...) with ImednetSDK."
        )
    resolved_cfg = replace(config, mask_phi=mask_phi) if mask_phi is not None else config

    run_id = uuid.uuid4().hex[:12]
    return await async_run_streaming_export(
        sdk,
        study_key,
        lambda: AsyncSnowflakeExportSink(config=resolved_cfg),
        resolved_cfg,
        stream_name=f"snowflake_{resolved_cfg.table}",
        batch_id_prefix=f"{study_key}/records/{run_id}",
        quality_gate_fn=async_apply_quality_gate,
    )


__all__ = [
    "AsyncSnowflakeExportSink",
    "SnowflakeExportSink",
    "SnowflakeSinkConfig",
    "async_export_to_snowflake",
    "export_to_snowflake",
]
