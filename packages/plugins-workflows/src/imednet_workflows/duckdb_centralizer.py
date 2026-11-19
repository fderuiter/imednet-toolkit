"""DuckDB ingestion workflow for incremental form centralization.

Provides an incremental record centralization pipeline using DuckDB.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from imednet.spi.utils import format_iso_datetime, normalize_utc_timestamp_bound

from .data_extraction import DataExtractionWorkflow

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade
    from imednet.spi.models import RecordRevision


_SAFE_STUDY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
_LEGACY_SILVER_STUDY_KEY_PATTERN = re.compile(
    r"WHERE\s*\(*\s*(?:bronze_revisions\.)?\"?study_key\"?\s*=\s*'([A-Za-z0-9._:-]+)'",
    re.IGNORECASE,
)

_BRONZE_SCHEMA_SPEC: tuple[tuple[str, str, str], ...] = (
    ("study_key", "study_key", "VARCHAR"),
    ("record_revision_id", "record_revision_id", "BIGINT"),
    ("record_id", "record_id", "BIGINT"),
    ("record_revision", "record_revision", "INTEGER"),
    ("data_revision", "data_revision", "INTEGER"),
    ("record_status", "record_status", "VARCHAR"),
    ("subject_id", "subject_id", "BIGINT"),
    ("subject_key", "subject_key", "VARCHAR"),
    ("site_id", "site_id", "BIGINT"),
    ("form_key", "form_key", "VARCHAR"),
    ("interval_id", "interval_id", "BIGINT"),
    ("deleted", "deleted", "BOOLEAN"),
    ("date_created", "date_created", "TIMESTAMP"),
    ("reason_for_change", "reason_for_change", "VARCHAR"),
    ("record_oid", "record_oid", "VARCHAR"),
    ("role", "role", "VARCHAR"),
    ("subject_oid", "subject_oid", "VARCHAR"),
    ("user", '"user"', "VARCHAR"),
    ("last_updated", "last_updated", "TIMESTAMP"),
)
_EXPECTED_BRONZE_COLUMNS: tuple[str, ...] = tuple(
    col_name for col_name, _, _ in _BRONZE_SCHEMA_SPEC
)
_BRONZE_PK_COLUMNS: tuple[str, str] = ("study_key", "record_revision_id")
_REVISION_SQL_COLUMNS = ",\n    ".join(sql_id for _, sql_id, _ in _BRONZE_SCHEMA_SPEC)

_BRONZE_COLUMN_DEFINITIONS = """
    study_key VARCHAR NOT NULL,
    record_revision_id BIGINT NOT NULL,
    record_id BIGINT,
    record_revision INTEGER,
    data_revision INTEGER,
    record_status VARCHAR,
    subject_id BIGINT,
    subject_key VARCHAR,
    site_id BIGINT,
    form_key VARCHAR,
    interval_id BIGINT,
    deleted BOOLEAN,
    date_created TIMESTAMP,
    reason_for_change VARCHAR,
    record_oid VARCHAR,
    role VARCHAR,
    subject_oid VARCHAR,
    "user" VARCHAR,
    last_updated TIMESTAMP,
    PRIMARY KEY (study_key, record_revision_id)
"""


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
        """Fetch RecordRevisions and write keyed rows to the bronze_revisions table.

        Pre-upgrade persistent ``bronze_revisions`` tables with valid
        ``(study_key, record_revision_id)`` keys are migrated in place while
        preserving existing data. Incompatible legacy tables can be rebuilt
        with ``mode="replace"``.
        """
        if mode not in {"append", "replace"}:
            raise ValueError("mode must be one of: 'append', 'replace'")

        revisions = self._extractor.extract_audit_trail(
            study_key,
            start_date=start_date,
            end_date=end_date,
        )
        rows = list(self._to_rows(study_key, revisions))

        if mode == "replace":
            self._connection.execute("DROP TABLE IF EXISTS bronze_revisions")
            self._ensure_bronze_table()
            self._refresh_existing_silver_view(fallback_study_key=study_key)
        else:
            self._ensure_bronze_table()

        if rows:
            insert_query = f"""
                INSERT OR REPLACE INTO bronze_revisions (
                    {_REVISION_SQL_COLUMNS}
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """  # nosec B608
            self._connection.executemany(insert_query, rows)  # nosem
        return len(rows)

    def build_silver_view(self, study_key: str) -> None:
        """Create or replace the silver_current_state view."""
        if not isinstance(study_key, str) or not _SAFE_STUDY_KEY_PATTERN.fullmatch(study_key):
            raise ValueError(
                f"Invalid study key: {study_key!r}. Only alphanumeric characters and ._:- are allowed."
            )
        self._ensure_bronze_table()
        self._write_silver_view(study_key)

    def _write_silver_view(self, study_key: str) -> None:
        """Write the 19-column silver_current_state view for a validated study key."""
        escaped_study_key = study_key.replace("'", "''")
        query = f"""
            CREATE OR REPLACE VIEW silver_current_state AS
            SELECT
                {_REVISION_SQL_COLUMNS}
            FROM bronze_revisions
            WHERE study_key = '{escaped_study_key}'
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY record_id
                ORDER BY
                    record_revision DESC NULLS LAST,
                    record_revision_id DESC NULLS LAST
            ) = 1
            """  # nosec B608
        self._connection.execute(query)  # nosem

    def _refresh_existing_silver_view(self, *, fallback_study_key: str | None = None) -> None:
        """Upgrade a pre-existing silver_current_state view if one exists in the catalog."""
        view_row = self._connection.execute(
            """
            SELECT sql
            FROM duckdb_views()
            WHERE view_name = 'silver_current_state'
              AND schema_name = current_schema()
              AND database_name = current_database()
            """
        ).fetchone()
        if view_row is None:
            return

        view_sql = str(view_row[0] or "")
        match = _LEGACY_SILVER_STUDY_KEY_PATTERN.search(view_sql)
        target_study_key = match.group(1) if match is not None else fallback_study_key
        if isinstance(target_study_key, str) and _SAFE_STUDY_KEY_PATTERN.fullmatch(
            target_study_key
        ):
            self._write_silver_view(target_study_key)

    def _ensure_bronze_table(self) -> None:
        """Ensure bronze_revisions exists on the keyed 19-column schema, migrating legacy tables."""
        exists_row = self._connection.execute(
            """
            SELECT 1
            FROM duckdb_tables()
            WHERE table_name = 'bronze_revisions'
              AND schema_name = current_schema()
              AND database_name = current_database()
            """
        ).fetchone()
        if exists_row is None:
            create_query = f"CREATE TABLE bronze_revisions ({_BRONZE_COLUMN_DEFINITIONS})"
            self._connection.execute(create_query)
            return

        table_info = self._connection.execute("PRAGMA table_info('bronze_revisions')").fetchall()

        existing_columns = tuple(str(row[1]) for row in table_info)
        pk_columns = tuple(str(row[1]) for row in table_info if row[5])
        notnull_by_col = {str(row[1]): bool(row[3]) for row in table_info}
        if (
            existing_columns == _EXPECTED_BRONZE_COLUMNS
            and pk_columns == _BRONZE_PK_COLUMNS
            and notnull_by_col.get("study_key", False)
            and notnull_by_col.get("record_revision_id", False)
        ):
            return

        self._migrate_bronze_table(set(existing_columns))

    def _migrate_bronze_table(self, existing_columns_set: set[str]) -> None:
        """Migrate a pre-upgrade bronze_revisions table to the keyed 19-column schema."""
        count_row = self._connection.execute("SELECT COUNT(*) FROM bronze_revisions").fetchone()
        row_count = int(count_row[0]) if count_row is not None else 0

        has_key_columns = {"study_key", "record_revision_id"}.issubset(existing_columns_set)
        if row_count > 0 and not has_key_columns:
            raise ValueError(
                "Existing 'bronze_revisions' table is missing required primary-key columns "
                "('study_key', 'record_revision_id') and cannot be migrated automatically; "
                "rebuild it with ingest_revisions(..., mode='replace')."
            )

        if row_count > 0:
            invalid_row = self._connection.execute(
                """
                SELECT COUNT(*)
                FROM bronze_revisions
                WHERE study_key IS NULL
                   OR TRIM(CAST(study_key AS VARCHAR)) = ''
                   OR record_revision_id IS NULL
                   OR TRY_CAST(record_revision_id AS BIGINT) IS NULL
                   OR TRY_CAST(record_revision_id AS BIGINT) <= 0
                """
            ).fetchone()
            invalid_count = int(invalid_row[0]) if invalid_row is not None else 0
            if invalid_count > 0:
                raise ValueError(
                    "Existing 'bronze_revisions' table contains rows without valid positive "
                    "('study_key', 'record_revision_id') keys and cannot be migrated automatically; "
                    "rebuild it with ingest_revisions(..., mode='replace')."
                )

        select_expressions: list[str] = []
        for col_name, sql_id, sql_type in _BRONZE_SCHEMA_SPEC:
            if col_name == "study_key":
                select_expressions.append("TRIM(CAST(study_key AS VARCHAR)) AS study_key")
            elif col_name == "record_revision_id":
                select_expressions.append(
                    "CAST(record_revision_id AS BIGINT) AS record_revision_id"
                )
            elif col_name in existing_columns_set:
                select_expressions.append(f"CAST({sql_id} AS {sql_type}) AS {sql_id}")
            else:
                select_expressions.append(f"CAST(NULL AS {sql_type}) AS {sql_id}")

        projection_sql = ",\n                    ".join(select_expressions)
        staging_create_sql = (
            f"CREATE TABLE _bronze_revisions_migrated ({_BRONZE_COLUMN_DEFINITIONS})"
        )
        copy_sql = f"""
            INSERT INTO _bronze_revisions_migrated (
                {_REVISION_SQL_COLUMNS}
            )
            SELECT
                {projection_sql}
            FROM bronze_revisions
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY
                    TRIM(CAST(study_key AS VARCHAR)),
                    CAST(record_revision_id AS BIGINT)
                ORDER BY rowid DESC
            ) = 1
            """  # nosec B608

        self._connection.execute("BEGIN TRANSACTION")
        try:
            self._connection.execute("DROP TABLE IF EXISTS _bronze_revisions_migrated")
            self._connection.execute(staging_create_sql)
            if row_count > 0:
                self._connection.execute(copy_sql)  # nosem
            self._connection.execute("DROP TABLE bronze_revisions")
            self._connection.execute(
                "ALTER TABLE _bronze_revisions_migrated RENAME TO bronze_revisions"
            )
            self._refresh_existing_silver_view()
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _require_revision_id(value: Any) -> int:
        """Validate and convert the required positive ``record_revision_id`` primary-key column."""
        if value is None or isinstance(value, bool):
            raise ValueError("RecordRevision is missing required 'record_revision_id'")
        if isinstance(value, str) and not value.strip():
            raise ValueError("RecordRevision is missing required 'record_revision_id'")
        revision_id = int(value)
        if revision_id <= 0:
            raise ValueError("RecordRevision is missing required 'record_revision_id'")
        return revision_id

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        """Convert optional integer fields without coercing None to zero."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return int(value)

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        """Convert optional string-like fields while preserving None."""
        return None if value is None else str(value)

    @staticmethod
    def _optional_timestamp(value: Any) -> str | None:
        """Normalize optional timestamps to UTC ISO strings for DuckDB TIMESTAMP columns."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return format_iso_datetime(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            return normalize_utc_timestamp_bound(stripped, is_end=False)
        return normalize_utc_timestamp_bound(str(value), is_end=False)

    def _to_rows(
        self, study_key: str, revisions: Iterable[RecordRevision]
    ) -> Iterable[tuple[Any, ...]]:
        """Internal helper to convert RecordRevision models into 19-column database rows."""
        for revision in revisions:
            raw_study_key = getattr(revision, "study_key", None)
            rev_study_key = (
                str(raw_study_key).strip()
                if raw_study_key is not None and str(raw_study_key).strip()
                else study_key
            )
            if not rev_study_key or not str(rev_study_key).strip():
                raise ValueError("RecordRevision is missing required 'study_key'")

            revision_id = self._require_revision_id(getattr(revision, "record_revision_id", None))
            deleted_val = getattr(revision, "deleted", None)

            yield (
                str(rev_study_key),
                revision_id,
                self._optional_int(getattr(revision, "record_id", None)),
                self._optional_int(getattr(revision, "record_revision", None)),
                self._optional_int(getattr(revision, "data_revision", None)),
                self._optional_str(getattr(revision, "record_status", None)),
                self._optional_int(getattr(revision, "subject_id", None)),
                self._optional_str(getattr(revision, "subject_key", None)),
                self._optional_int(getattr(revision, "site_id", None)),
                self._optional_str(getattr(revision, "form_key", None)),
                self._optional_int(getattr(revision, "interval_id", None)),
                None if deleted_val is None else bool(deleted_val),
                self._optional_timestamp(getattr(revision, "date_created", None)),
                self._optional_str(getattr(revision, "reason_for_change", None)),
                self._optional_str(getattr(revision, "record_oid", None)),
                self._optional_str(getattr(revision, "role", None)),
                self._optional_str(getattr(revision, "subject_oid", None)),
                self._optional_str(getattr(revision, "user", None)),
                self._optional_timestamp(getattr(revision, "last_updated", None)),
            )
