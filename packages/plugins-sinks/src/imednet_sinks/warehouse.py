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
    from imednet_sinks import SnowflakeExportSink, SnowflakeSinkConfig

    config = SnowflakeSinkConfig(
        study_key="MYSTUDY",
        account="myorg-myaccount",
        user="loader",
        **{"password": os.environ["SF_PASS"]},  # keep credentials out of source code
        database="IMEDNET_DB",
        schema="PUBLIC",
        warehouse="COMPUTE_WH",
        stage="MY_STAGE",
        table="RECORDS",
        stage_prefix="imednet",
        local_staging_dir="/tmp/imednet_stage",
    )
    with SnowflakeExportSink(config=config) as sink:
        for i, batch in enumerate(batched(records, config.batch_size)):
            sink.write_batch(batch, batch_id=f"MYSTUDY/FORM1/{i}")
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from imednet.errors import ExportBatchError, ExportConfigurationError
from imednet.sdk import ImednetSDK
from imednet.spi.sink_base import (
    ExportSink,
    SinkConfig,
    _require_optional_dep,
    extract_record_id,
    run_streaming_export,
)

logger = logging.getLogger(__name__)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_$]+$")
_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")
_BATCH_UNSAFE_PATTERN = re.compile(r"[^A-Za-z0-9_]+")
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


def _quote_identifier(ident: str) -> str:
    """Safely quote a Snowflake SQL identifier with double quotes."""
    escaped = ident.replace('"', '""')
    return f'"{escaped}"'


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

    def __post_init__(self) -> None:
        """Validate configuration parameters."""
        super().__post_init__()
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


def _post_process_warehouse(row: dict[str, Any], *, mask_phi: bool = True) -> dict[str, Any]:
    """Apply PHI masking and JSON-encode structured fields for single-table Parquet staging."""
    from imednet.spi.utils import mask_clinical_phi

    normalized_row: dict[str, Any] = _normalize_warehouse_value(row)
    masked_row: dict[str, Any] = mask_clinical_phi(normalized_row) if mask_phi else normalized_row
    record_payload = masked_row.get("record_data")
    if record_payload is None:
        record_payload = {}
    if isinstance(record_payload, str):
        masked_row["record_data"] = record_payload
    else:
        masked_row["record_data"] = json.dumps(record_payload, default=str)

    keywords = masked_row.get("keywords")
    if isinstance(keywords, (list, dict, tuple)):
        masked_row["keywords"] = json.dumps(keywords, default=str)
    return masked_row


def _post_process_warehouse_unmasked(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-encode structured fields for single-table Parquet staging without PHI masking."""
    return _post_process_warehouse(row, mask_phi=False)


def _resolve_merge_columns(arrow_table: Any) -> list[str]:
    """Resolve and validate column identifiers from *arrow_table* for MERGE INTO generation."""
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


def _records_to_arrow_table(
    records: Sequence[Any],
    *,
    mask_phi: bool = True,
    study_key: str | None = None,
    mapper: Any | None = None,
) -> Any:
    """Convert *records* to a ``pyarrow.Table`` with JSON-encoded ``record_data``."""
    pa = _require_optional_dep("pyarrow", "snowflake")
    if mapper is None:
        from imednet.spi.sinks import CentralizedMapper

        post_proc = _post_process_warehouse if mask_phi else _post_process_warehouse_unmasked
        mapper = CentralizedMapper(
            mode="document",
            post_processor=post_proc,
            mask_phi=mask_phi,
        )
    rows = [mapper.map_record(r, study_key=study_key) for r in records]

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

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write *records* to Snowflake via Parquet staging + MERGE INTO (or COPY INTO)."""
        if not records:
            return 0

        safe_batch = _BATCH_UNSAFE_PATTERN.sub("_", batch_id).strip("_")
        if not safe_batch:
            raise ExportBatchError(
                f"Batch identifier {batch_id!r} resolved to an empty string after sanitization.",
                batch_id=batch_id,
            )

        cfg = self._cfg
        from imednet.spi.sinks import build_sink_mapper

        post_proc = (
            _post_process_warehouse if self.config.mask_phi else _post_process_warehouse_unmasked
        )
        mapper = build_sink_mapper(cfg, mode="document", post_processor=post_proc)

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
            """Execute PUT and staged MERGE INTO (or COPY INTO) commands in Snowflake."""
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
                    update_clause = ", ".join(
                        f"target.{col} = source.{col}" for col in non_key_cols
                    )
                    insert_cols = ", ".join(merge_columns)
                    insert_vals = ", ".join(f"source.{col}" for col in merge_columns)
                    order_expr = (
                        "ORDER BY date_modified DESC NULLS LAST"
                        if "date_modified" in merge_columns
                        else "ORDER BY record_id"
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
                        f"WHEN MATCHED THEN UPDATE SET {update_clause} "
                        f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
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
) -> int:
    """Export study records to Snowflake using :class:`SnowflakeExportSink`."""
    return run_streaming_export(
        sdk,
        study_key,
        lambda: SnowflakeExportSink(config=config),
        config,
        stream_name=f"snowflake_{config.table}",
    )


__all__ = ["SnowflakeExportSink", "SnowflakeSinkConfig", "export_to_snowflake"]
