"""DuckDB ingestion workflow for incremental form centralization.

Provides an incremental record centralization pipeline using DuckDB.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from contextlib import suppress
from datetime import UTC, date, datetime
from enum import Enum
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import uuid4

from imednet.spi.utils import parse_iso_datetime

from .data_extraction import DataExtractionWorkflow

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade
    from imednet.spi.models import RecordRevision


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
        self._closed = False

    def close(self) -> None:
        """Close the underlying DuckDB connection idempotently."""
        if self._closed:
            return
        self._closed = True
        if self._connection is not None:
            self._connection.close()

    def __enter__(self) -> Self:
        """Enter the context manager and return the workflow instance."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the context manager and close the DuckDB connection."""
        self.close()

    @staticmethod
    def _validate_study_key(study_key: Any) -> str:
        """Validate that a study key matches the safe identifier pattern."""
        if not isinstance(study_key, str) or not _SAFE_STUDY_KEY_PATTERN.fullmatch(study_key):
            raise ValueError(
                f"Invalid study key: {study_key!r}. "
                "Only alphanumeric characters and ._:- are allowed."
            )
        return study_key

    @staticmethod
    def _deduplicate_rows(rows: Iterable[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        """Deduplicate staged rows on non-null (study_key, record_revision_id) keeping last."""
        materialized = list(rows)
        last_index_by_key: dict[tuple[str, int], int] = {}
        for idx, row in enumerate(materialized):
            rev_id = row[1]
            if rev_id is not None:
                last_index_by_key[(row[0], rev_id)] = idx
        return [
            row
            for idx, row in enumerate(materialized)
            if row[1] is None or last_index_by_key[(row[0], row[1])] == idx
        ]

    def ingest_revisions(
        self,
        study_key: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        mode: Literal["append", "replace"] = "append",
    ) -> int:
        """Fetch RecordRevisions and write to the bronze_revisions table."""
        self._validate_study_key(study_key)
        if mode not in {"append", "replace"}:
            raise ValueError("mode must be one of: 'append', 'replace'")

        revisions = self._extractor.extract_audit_trail(
            study_key,
            start_date=start_date,
            end_date=end_date,
        )
        rows = self._deduplicate_rows(self._to_rows(study_key, revisions))
        staging_relation = self._build_staging_relation(rows) if rows else None

        self._ensure_bronze_table()
        if not rows:
            if mode == "replace":
                self._connection.execute("BEGIN TRANSACTION")
                try:
                    self._connection.execute(
                        "DELETE FROM bronze_revisions WHERE study_key = ?",
                        [study_key],
                    )
                    self._connection.execute("COMMIT")
                except BaseException:
                    with suppress(Exception):
                        self._connection.execute("ROLLBACK")
                    raise
            return 0

        existing_overlap_count = 0
        view_name = f"_staging_bronze_revisions_{uuid4().hex}"
        self._connection.register(view_name, staging_relation)
        try:
            self._connection.execute("BEGIN TRANSACTION")
            try:
                if mode == "replace":
                    self._connection.execute(
                        "DELETE FROM bronze_revisions WHERE study_key = ?",
                        [study_key],
                    )
                has_keyed_rows = any(row[1] is not None for row in rows)
                if has_keyed_rows and (
                    mode == "append" or any(row[0] != study_key for row in rows)
                ):
                    overlap_query = f"""
                        SELECT COUNT(*)
                        FROM "{view_name}" AS staged
                        WHERE staged.record_revision_id IS NOT NULL
                          AND EXISTS (
                              SELECT 1
                              FROM bronze_revisions AS existing
                              WHERE existing.study_key = CAST(staged.study_key AS VARCHAR)
                                AND existing.record_revision_id = CAST(
                                    staged.record_revision_id AS BIGINT
                                )
                          )
                        """
                    overlap_row = self._connection.execute(overlap_query).fetchone()  # nosem
                    existing_overlap_count = int(overlap_row[0]) if overlap_row is not None else 0

                    delete_query = f"""
                        DELETE FROM bronze_revisions
                        WHERE record_revision_id IS NOT NULL
                          AND EXISTS (
                              SELECT 1
                              FROM "{view_name}" AS staged
                              WHERE staged.record_revision_id IS NOT NULL
                                AND bronze_revisions.study_key = CAST(staged.study_key AS VARCHAR)
                                AND bronze_revisions.record_revision_id = CAST(
                                    staged.record_revision_id AS BIGINT
                                )
                          )
                        """
                    self._connection.execute(delete_query)  # nosem

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
                self._connection.execute("COMMIT")
            except BaseException:
                with suppress(Exception):
                    self._connection.execute("ROLLBACK")
                raise
        finally:
            self._connection.unregister(view_name)

        return max(0, len(rows) - existing_overlap_count)

    def _refresh_combined_silver_view(
        self, prefix: str, study_key: str, combined_view: str
    ) -> None:
        """Union all per-Study views matching prefix into the aggregate silver view."""
        catalog_rows = self._connection.execute(
            """
            SELECT view_name
            FROM duckdb_views()
            WHERE database_name = current_database()
              AND schema_name = current_schema()
              AND NOT internal
              AND NOT temporary
              AND starts_with(view_name, ?)
            ORDER BY view_name
            """,
            [prefix],
        ).fetchall()
        built_study_keys = sorted(
            {
                study_key,
                *(
                    row[0].removeprefix(prefix)
                    for row in catalog_rows
                    if isinstance(row[0], str)
                    and row[0].startswith(prefix)
                    and _SAFE_STUDY_KEY_PATTERN.fullmatch(row[0].removeprefix(prefix))
                ),
            }
        )
        union_selects = "\n            UNION ALL\n            ".join(
            f'SELECT * FROM "{prefix}{key}"' for key in built_study_keys
        )
        combined_query = f"""
            CREATE OR REPLACE VIEW "{combined_view}" AS
            {union_selects}
            """
        self._connection.execute(combined_query)  # nosem

    def build_silver_view(self, study_key: str, *, include_deleted: bool = False) -> None:
        """Create or replace per-Study and combined silver current-state and history views.

        Args:
            study_key: Study key identifier to materialize.
            include_deleted: When True, retain records whose latest revision has deleted=True.
        """
        self._validate_study_key(study_key)
        self._ensure_bronze_table()
        escaped_study_key = study_key.replace("'", "''")
        record_partition_expr = (
            "study_key, COALESCE(CAST(record_id AS VARCHAR), "
            "NULLIF(record_oid, ''), CAST(record_revision_id AS VARCHAR))"
        )
        per_study_view = f"silver_current_state_{study_key}"
        deleted_clause = (
            "" if include_deleted else "\n            WHERE NOT COALESCE(deleted, FALSE)"
        )
        per_study_query = f"""
            CREATE OR REPLACE VIEW "{per_study_view}" AS
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
            FROM (
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
                    PARTITION BY {record_partition_expr}
                    ORDER BY
                        record_revision DESC NULLS LAST,
                        data_revision DESC NULLS LAST,
                        date_created DESC NULLS LAST,
                        record_revision_id DESC NULLS LAST
                ) = 1
            ) AS latest_revisions{deleted_clause}
            """
        self._connection.execute(per_study_query)  # nosem
        self._refresh_combined_silver_view(
            "silver_current_state_", study_key, "silver_current_state"
        )

        per_study_history_view = f"silver_records_history_{study_key}"
        per_study_history_query = f"""
            CREATE OR REPLACE VIEW "{per_study_history_view}" AS
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
                last_updated,
                COALESCE(date_created, last_updated) AS valid_from,
                LEAD(COALESCE(date_created, last_updated)) OVER (
                    PARTITION BY {record_partition_expr}
                    ORDER BY
                        record_revision ASC NULLS LAST,
                        data_revision ASC NULLS LAST,
                        date_created ASC NULLS LAST,
                        record_revision_id ASC NULLS LAST
                ) AS valid_to,
                LEAD(1) OVER (
                    PARTITION BY {record_partition_expr}
                    ORDER BY
                        record_revision ASC NULLS LAST,
                        data_revision ASC NULLS LAST,
                        date_created ASC NULLS LAST,
                        record_revision_id ASC NULLS LAST
                ) IS NULL AS is_current
            FROM bronze_revisions
            WHERE study_key = '{escaped_study_key}'
            """
        self._connection.execute(per_study_history_query)  # nosem
        self._refresh_combined_silver_view(
            "silver_records_history_", study_key, "silver_records_history"
        )

    def _ensure_bronze_table(self) -> None:
        """Ensure the bronze_revisions table and record_revisions view exist in DuckDB."""
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
                last_updated TIMESTAMP,
                UNIQUE (study_key, record_revision_id)
            )
            """)
        self._connection.execute("""
            CREATE OR REPLACE VIEW record_revisions AS
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
        elif isinstance(value, date):
            return value.isoformat()
        elif isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            dt = parse_iso_datetime(stripped)
        else:
            return str(value)
        if dt.tzinfo is not None and dt.utcoffset() is not None:
            dt = dt.astimezone(UTC).replace(tzinfo=None)
        return dt.isoformat()

    def _to_rows(
        self, study_key: str, revisions: Iterable[RecordRevision]
    ) -> Iterable[tuple[Any, ...]]:
        """Internal helper to convert RecordRevision models into database rows."""
        self._validate_study_key(study_key)
        for revision in revisions:
            raw_study_key = getattr(revision, "study_key", None)
            rev_study_key = self._validate_study_key(raw_study_key) if raw_study_key else study_key
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
