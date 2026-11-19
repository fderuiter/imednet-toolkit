"""DuckDB ingestion workflow for incremental form centralization.

Provides an incremental record centralization pipeline using DuckDB.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from ._guards import ensure_async_sdk, ensure_sync_sdk
from .data_extraction import DataExtractionWorkflow

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
    from imednet.spi.models import RecordRevision


_SAFE_STUDY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
_OWNER = "DuckDBIngestionWorkflow"


def _validate_ingest_mode(mode: str) -> None:
    """Validate the ingestion mode parameter."""
    if mode not in {"append", "replace"}:
        raise ValueError("mode must be one of: 'append', 'replace'")


class DuckDBIngestionWorkflow:
    """Incremental record centralization pipeline using a bronze/silver DuckDB layout."""

    def __init__(self, sdk: ImednetFacade | AsyncImednetFacade, db_path: str = ":memory:") -> None:
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

        self._sdk = sdk
        self._connection: Any = duckdb.connect(db_path)
        self._extractor = DataExtractionWorkflow(sdk)

    def _write_revisions(
        self,
        study_key: str,
        revisions: Iterable[RecordRevision],
        *,
        mode: Literal["append", "replace"],
    ) -> int:
        """Write fetched ``RecordRevision`` rows into ``bronze_revisions``."""
        if mode == "replace":
            self._connection.execute("DROP TABLE IF EXISTS bronze_revisions")

        self._ensure_bronze_table()
        rows = list(self._to_rows(study_key, revisions))
        if rows:
            self._connection.executemany(
                """
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def ingest_revisions(
        self,
        study_key: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        mode: Literal["append", "replace"] = "append",
    ) -> int:
        """Fetch RecordRevisions and write to the bronze_revisions table."""
        ensure_sync_sdk(self._sdk, _OWNER, "ingest_revisions", "async_ingest_revisions")
        _validate_ingest_mode(mode)
        revisions = self._extractor.extract_audit_trail(
            study_key,
            start_date=start_date,
            end_date=end_date,
        )
        return self._write_revisions(study_key, revisions, mode=mode)

    async def async_ingest_revisions(
        self,
        study_key: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        mode: Literal["append", "replace"] = "append",
    ) -> int:
        """Asynchronously fetch RecordRevisions and write to the bronze_revisions table."""
        ensure_async_sdk(self._sdk, _OWNER, "async_ingest_revisions", "ingest_revisions")
        _validate_ingest_mode(mode)
        revisions = await self._extractor.async_extract_audit_trail(
            study_key,
            start_date=start_date,
            end_date=end_date,
        )
        return self._write_revisions(study_key, revisions, mode=mode)

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
    def _optional_timestamp(value: Any) -> datetime | str | None:
        """Normalize empty timestamp strings to None for DuckDB TIMESTAMP columns."""
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, (datetime, str)):
            return value
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
                getattr(revision, "record_oid", None),
                getattr(revision, "form_key", None),
                getattr(revision, "subject_key", None),
                self._optional_int(getattr(revision, "site_id", None)),
                self._optional_int(getattr(revision, "interval_id", None)),
                getattr(revision, "record_status", None),
                self._optional_int(getattr(revision, "record_revision", None)),
                self._optional_int(getattr(revision, "data_revision", None)),
                getattr(revision, "user", None),
                getattr(revision, "role", None),
                getattr(revision, "reason_for_change", None),
                False if deleted_val is None else bool(deleted_val),
                self._optional_timestamp(getattr(revision, "date_created", None)),
                self._optional_timestamp(getattr(revision, "last_updated", None)),
            )
