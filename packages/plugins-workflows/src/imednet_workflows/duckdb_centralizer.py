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

_REVISION_SQL_COLUMNS = """
    study_key,
    record_revision_id,
    record_id,
    record_revision,
    data_revision,
    record_status,
    subject_id,
    subject_key,
    site_id,
    form_key,
    interval_id,
    deleted,
    date_created,
    reason_for_change,
    record_oid,
    role,
    subject_oid,
    "user",
    last_updated
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
        """Fetch RecordRevisions and write keyed rows to the bronze_revisions table."""
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
        if rows:
            self._connection.executemany(
                f"""
                INSERT OR REPLACE INTO bronze_revisions (
                    {_REVISION_SQL_COLUMNS}
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
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
                {_REVISION_SQL_COLUMNS}
            FROM bronze_revisions
            WHERE study_key = '{escaped_study_key}'
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY record_id
                ORDER BY
                    record_revision DESC NULLS LAST,
                    record_revision_id DESC NULLS LAST
            ) = 1
            """
        self._connection.execute(query)  # nosem

    def _ensure_bronze_table(self) -> None:
        """Ensure the keyed bronze_revisions table exists in DuckDB."""
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS bronze_revisions (
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
            )
            """)

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
