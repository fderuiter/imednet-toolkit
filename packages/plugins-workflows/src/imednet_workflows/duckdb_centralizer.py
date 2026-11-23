"""DuckDB ingestion workflow for incremental form centralization.

Provides an incremental record centralization pipeline using DuckDB.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from imednet.spi.facade import ImednetFacade
from imednet.spi.models import RecordRevision

from .data_extraction import DataExtractionWorkflow

_SAFE_STUDY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
_BRONZE_COLUMN_SPECS: tuple[tuple[str, str], ...] = (
    ("study_key", "object"),
    ("record_revision_id", "Int64"),
    ("record_id", "Int64"),
    ("record_oid", "object"),
    ("form_key", "object"),
    ("subject_key", "object"),
    ("site_id", "Int64"),
    ("interval_id", "Int64"),
    ("record_status", "object"),
    ("record_revision", "Int32"),
    ("data_revision", "Int32"),
    ("user", "object"),
    ("role", "object"),
    ("reason_for_change", "object"),
    ("deleted", "bool"),
    ("date_created", "object"),
    ("last_updated", "object"),
)


class DuckDBIngestionWorkflow:
    """Incremental record centralization pipeline using a bronze/silver DuckDB layout."""

    def __init__(self, sdk: ImednetFacade, db_path: str = ":memory:") -> None:
        """Initialize the DuckDB ingestion workflow.

        Args:
            sdk: An instance of the iMednet SDK facade.
            db_path: Path to the DuckDB database file or ":memory:". Defaults to ":memory:".
        """
        try:
            import duckdb
        except ImportError as error:
            raise ImportError(
                "DuckDB ingestion requires the optional 'duckdb' dependency. "
                "Install with `pip install 'imednet[duckdb]'`."
            ) from error

        self._connection: Any = duckdb.connect(db_path)
        self._extractor = DataExtractionWorkflow(sdk)

    def ingest_revisions(
        self,
        study_key: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        mode: Literal["append", "replace"] = "append",
    ) -> int:
        """Fetch RecordRevisions and write to the bronze_revisions table."""
        if mode not in {"append", "replace"}:
            raise ValueError("mode must be one of: 'append', 'replace'")

        revisions = self._extractor.extract_audit_trail(
            study_key,
            start_date=start_date,
            end_date=end_date,
        )

        if mode == "replace":
            self._connection.execute("DROP TABLE IF EXISTS bronze_revisions")

        self._ensure_bronze_table()
        rows = list(self._to_rows(study_key, revisions))
        if not rows:
            return 0

        staging_relation = self._build_staging_relation(rows)
        view_name = f"_staging_bronze_revisions_{uuid4().hex}"
        self._connection.register(view_name, staging_relation)
        try:
            insert_query = f"""
                INSERT INTO bronze_revisions (
                    study_key,
                    record_revision_id,
                    record_id,
                    record_oid,
                    form_key,
                    subject_key,
                    site_id,
                    interval_id,
                    record_status,
                    record_revision,
                    data_revision,
                    "user",
                    role,
                    reason_for_change,
                    deleted,
                    date_created,
                    last_updated
                )
                SELECT
                    CAST(study_key AS VARCHAR),
                    CAST(record_revision_id AS BIGINT),
                    CAST(record_id AS BIGINT),
                    CAST(record_oid AS VARCHAR),
                    CAST(form_key AS VARCHAR),
                    CAST(subject_key AS VARCHAR),
                    CAST(site_id AS BIGINT),
                    CAST(interval_id AS BIGINT),
                    CAST(record_status AS VARCHAR),
                    CAST(record_revision AS INTEGER),
                    CAST(data_revision AS INTEGER),
                    CAST("user" AS VARCHAR),
                    CAST(role AS VARCHAR),
                    CAST(reason_for_change AS VARCHAR),
                    CAST(deleted AS BOOLEAN),
                    CAST(CAST(date_created AS VARCHAR) AS TIMESTAMP),
                    CAST(CAST(last_updated AS VARCHAR) AS TIMESTAMP)
                FROM "{view_name}"
                """
            self._connection.execute(insert_query)  # nosem
        finally:
            self._connection.unregister(view_name)
        return len(rows)

    def build_silver_view(self, study_key: str) -> None:
        """Create or replace the silver_current_state view."""
        if not isinstance(study_key, str) or not _SAFE_STUDY_KEY_PATTERN.fullmatch(study_key):
            raise ValueError(
                f"Invalid study key: {study_key!r}. Only alphanumeric characters and ._:- are allowed."
            )
        self._ensure_bronze_table()
        escaped_study_key = study_key.replace("'", "''")
        query = f"""
            CREATE OR REPLACE VIEW silver_current_state AS
            SELECT
                study_key,
                record_revision_id,
                record_id,
                record_oid,
                form_key,
                subject_key,
                site_id,
                interval_id,
                record_status,
                record_revision,
                data_revision,
                "user",
                role,
                reason_for_change,
                deleted,
                date_created,
                last_updated
            FROM bronze_revisions
            WHERE study_key = '{escaped_study_key}'
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY record_id
                ORDER BY
                    record_revision DESC NULLS LAST,
                    data_revision DESC NULLS LAST,
                    date_created DESC NULLS LAST,
                    record_revision_id DESC NULLS LAST
            ) = 1
            """
        self._connection.execute(query)  # nosem

    def _ensure_bronze_table(self) -> None:
        """Ensure the bronze_revisions table exists in DuckDB."""
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS bronze_revisions (
                study_key VARCHAR,
                record_revision_id BIGINT,
                record_id BIGINT,
                record_oid VARCHAR,
                form_key VARCHAR,
                subject_key VARCHAR,
                site_id BIGINT,
                interval_id BIGINT,
                record_status VARCHAR,
                record_revision INTEGER,
                data_revision INTEGER,
                "user" VARCHAR,
                role VARCHAR,
                reason_for_change VARCHAR,
                deleted BOOLEAN,
                date_created TIMESTAMP,
                last_updated TIMESTAMP
            )
            """)

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        """Convert optional integer fields without coercing None to zero."""
        return None if value is None else int(value)

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        """Convert optional string or enum fields to plain strings."""
        if value is None:
            return None
        if isinstance(value, Enum):
            return str(value.value)
        if isinstance(value, str):
            return value
        return str(value)

    @staticmethod
    def _optional_timestamp(value: Any) -> str | None:
        """Normalize timestamps to naive UTC ISO strings and blank strings to None."""
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is not None and dt.utcoffset() is not None:
                dt = dt.astimezone(UTC).replace(tzinfo=None)
            return dt.isoformat()
        if isinstance(value, str):
            return value if value.strip() else None
        return str(value)

    def _to_rows(
        self, study_key: str, revisions: Iterable[RecordRevision]
    ) -> Iterable[tuple[Any, ...]]:
        """Internal helper to convert RecordRevision models into database rows."""
        for revision in revisions:
            rev_study_key = getattr(revision, "study_key", None) or study_key
            deleted_val = getattr(revision, "deleted", False)
            yield (
                str(rev_study_key),
                self._optional_int(getattr(revision, "record_revision_id", None)),
                self._optional_int(getattr(revision, "record_id", None)),
                self._optional_str(getattr(revision, "record_oid", None)),
                self._optional_str(getattr(revision, "form_key", None)),
                self._optional_str(getattr(revision, "subject_key", None)),
                self._optional_int(getattr(revision, "site_id", None)),
                self._optional_int(getattr(revision, "interval_id", None)),
                self._optional_str(getattr(revision, "record_status", None)),
                self._optional_int(getattr(revision, "record_revision", None)),
                self._optional_int(getattr(revision, "data_revision", None)),
                self._optional_str(getattr(revision, "user", None)),
                self._optional_str(getattr(revision, "role", None)),
                self._optional_str(getattr(revision, "reason_for_change", None)),
                False if deleted_val is None else bool(deleted_val),
                self._optional_timestamp(getattr(revision, "date_created", None)),
                self._optional_timestamp(getattr(revision, "last_updated", None)),
            )

    @staticmethod
    def _build_staging_relation(rows: Sequence[tuple[Any, ...]]) -> Any:
        """Build a typed columnar staging relation using PyArrow or Pandas fallback."""
        columns: tuple[tuple[Any, ...], ...] = (
            tuple(zip(*rows, strict=True)) if rows else tuple(() for _ in _BRONZE_COLUMN_SPECS)
        )
        try:
            import pyarrow as pa
        except ImportError:
            import pandas as pd

            return pd.DataFrame(
                {
                    name: pd.Series(list(col), dtype=dtype)
                    for (name, dtype), col in zip(_BRONZE_COLUMN_SPECS, columns, strict=True)
                }
            )

        arrow_types: dict[str, Any] = {
            "object": pa.string(),
            "Int64": pa.int64(),
            "Int32": pa.int32(),
            "bool": pa.bool_(),
        }
        return pa.Table.from_arrays(
            [
                pa.array(col, type=arrow_types[dtype])
                for (_, dtype), col in zip(_BRONZE_COLUMN_SPECS, columns, strict=True)
            ],
            names=[name for name, _ in _BRONZE_COLUMN_SPECS],
        )
