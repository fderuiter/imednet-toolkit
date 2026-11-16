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
When ``SinkConfig.idempotent`` is ``True`` (default) the sink uses
``COPY INTO ... FORCE = FALSE`` so that Snowflake skips files that have
already been loaded, making re-runs safe.  Set ``idempotent = False``
to force re-ingestion of previously loaded files.

Usage
-----
.. code-block:: python

    from imednet.integrations.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

    config = SnowflakeSinkConfig(
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
from imednet.integrations.sink_base import (
    ExportSink,
    SinkConfig,
    _require_optional_dep,
    iter_batches,
)
from imednet.sdk import ImednetSDK

logger = logging.getLogger(__name__)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_$]+$")
_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")
_BATCH_UNSAFE_PATTERN = re.compile(r"[^A-Za-z0-9_]+")


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


def _records_to_arrow_table(records: Sequence[Any]) -> Any:
    """Convert *records* to a ``pyarrow.Table``."""
    pa = _require_optional_dep("pyarrow", "snowflake")
    from imednet.integrations.enrichment import CentralizedMapper

    mapper = CentralizedMapper(mode="tabular")
    rows = [mapper.map_record(r) for r in records]

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
        """Write *records* to Snowflake via Parquet staging + COPY INTO."""
        if not records:
            return 0

        safe_batch = _BATCH_UNSAFE_PATTERN.sub("_", batch_id).strip("_")
        if not safe_batch:
            raise ExportBatchError(
                f"Batch identifier {batch_id!r} resolved to an empty string after sanitization.",
                batch_id=batch_id,
            )

        arrow_table = _records_to_arrow_table(records)
        local_path = Path(self._staging_dir) / f"{safe_batch}.parquet"
        pq = _require_optional_dep("pyarrow.parquet", "snowflake")
        pq.write_table(arrow_table, str(local_path))

        cfg = self._cfg
        stage_path = f"@{cfg.stage}/{cfg.stage_prefix}/{safe_batch}.parquet"

        def execute_export() -> int:
            """Execute PUT and COPY INTO commands to load data into Snowflake."""
            cur = self._conn.cursor()
            try:
                cur.execute(f"PUT file://{local_path} '@{cfg.stage}/{cfg.stage_prefix}/'")  # nosem
                force_clause = "FORCE = FALSE" if self.config.idempotent else "FORCE = TRUE"
                target_table = (
                    f"{_quote_identifier(cfg.database)}."
                    f"{_quote_identifier(cfg.schema)}."
                    f"{_quote_identifier(cfg.table)}"
                )
                cur.execute(
                    f"COPY INTO {target_table} "
                    f"FROM '{stage_path}' "
                    f"FILE_FORMAT = (TYPE = PARQUET) "
                    f"MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE "
                    f"{force_clause}"
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
        manifest_path = self._cfg.manifest_path
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
    from imednet.integrations.sink_base import apply_quality_gate

    records = sdk.records.list(study_key=study_key, record_data_filter=None)
    filtered_records = list(apply_quality_gate(sdk, study_key, records, config))

    total_written = 0
    with SnowflakeExportSink(config=config) as sink:
        for index, batch in enumerate(iter_batches(filtered_records, config.batch_size)):
            total_written += sink.write_batch(batch, batch_id=f"{study_key}/records/{index}")
    return total_written


__all__ = ["SnowflakeExportSink", "SnowflakeSinkConfig", "export_to_snowflake"]
