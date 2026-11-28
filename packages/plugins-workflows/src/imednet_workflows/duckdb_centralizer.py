"""DuckDB ingestion workflow for incremental form centralization.

Provides an incremental record centralization pipeline using DuckDB.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable, Sequence
from contextlib import suppress
from datetime import UTC, date, datetime
from enum import Enum
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import uuid4

from imednet.spi.models import validate_record_revision_chain
from imednet.spi.utils import (
    SensitivityRegistry,
    build_study_sensitivity_registry,
    global_sensitivity_registry,
    is_async_facade,
    mask_clinical_phi,
    maybe_to_thread,
    parse_iso_datetime,
    resolve_cli_phi_fields,
    resolve_enrichment_pipeline,
    sanitize_diagnostic_text,
    sdk_governance_context,
)

from ._workflow_helpers import ensure_sync_workflow_mode
from .data_extraction import AsyncDataExtractionWorkflow, DataExtractionWorkflow

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
    from imednet.spi.models import RecordRevision


_ORIG_RESOLVE_PIPELINE = resolve_enrichment_pipeline
_SAFE_STUDY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")


def _extractor_accepts_kwarg(fn: Any, kwarg: str) -> bool:
    """Return True when *fn* accepts *kwarg* or arbitrary ``**kwargs``."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD or p.name == kwarg for p in params)


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


class _DuckDBCentralizerBase:
    """Shared bronze/silver DuckDB table, PHI masking, and view materialization engine."""

    def __init__(self, db_path: str = ":memory:", *, mask_phi: bool = True) -> None:
        try:
            import duckdb
        except ImportError as error:
            raise ImportError(
                "DuckDB ingestion requires the optional 'duckdb' dependency. "
                "Install with `pip install 'imednet[duckdb]'`."
            ) from error

        self._connection: Any = duckdb.connect(db_path)
        self._mask_phi = bool(mask_phi)
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

    def _validate_merged_bronze_chains_sync(
        self,
        study_keys: Sequence[str],
        *,
        allow_partial: bool,
    ) -> None:
        """Validate monotonic chain and tombstone invariants across merged bronze_revisions."""
        for sk in study_keys:
            merged_rows = self._connection.execute(
                """
                SELECT
                    study_key,
                    record_revision_id,
                    record_id,
                    record_oid,
                    record_revision,
                    data_revision,
                    record_status,
                    deleted,
                    date_created
                FROM bronze_revisions
                WHERE study_key = ?
                ORDER BY
                    record_id ASC NULLS LAST,
                    record_oid ASC NULLS LAST,
                    record_revision ASC NULLS FIRST,
                    date_created ASC NULLS LAST,
                    record_revision_id ASC NULLS LAST
                """,
                [sk],
            ).fetchall()
            if not merged_rows:
                continue
            merged_payloads = [
                {
                    "study_key": row[0],
                    "record_revision_id": row[1],
                    "record_id": row[2],
                    "record_oid": (
                        f"rev:{row[1]}"
                        if row[2] is None and row[3] == "***MASKED***" and row[1] is not None
                        else row[3]
                    ),
                    "record_revision": row[4],
                    "data_revision": row[5],
                    "record_status": row[6],
                    "deleted": row[7],
                    "date_created": row[8],
                }
                for row in merged_rows
            ]
            validate_record_revision_chain(merged_payloads, allow_partial=allow_partial)

    def _ingest_fetched_revisions_sync(
        self,
        study_key: str,
        revisions: Iterable[RecordRevision],
        *,
        mode: Literal["append", "replace"],
        effective_mask_phi: bool,
        validate_chain: bool = False,
        effective_allow_partial: bool = False,
    ) -> int:
        """Write extracted RecordRevision models into the DuckDB bronze_revisions table."""
        revisions_list = list(revisions)
        rows = self._deduplicate_rows(
            self._to_rows(study_key, revisions_list, mask_phi=effective_mask_phi)
        )
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
                    if validate_chain:
                        validate_record_revision_chain(
                            revisions_list,
                            allow_partial=effective_allow_partial,
                        )
                    self._connection.execute("COMMIT")
                except BaseException:
                    with suppress(Exception):
                        self._connection.execute("ROLLBACK")
                    raise
            elif validate_chain:
                validate_record_revision_chain(
                    revisions_list,
                    allow_partial=effective_allow_partial,
                )
                self._validate_merged_bronze_chains_sync(
                    [study_key],
                    allow_partial=effective_allow_partial,
                )
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
                if validate_chain:
                    validate_record_revision_chain(
                        revisions_list,
                        allow_partial=effective_allow_partial,
                    )
                    affected_keys = sorted({str(row[0]) for row in rows} | {study_key})
                    self._validate_merged_bronze_chains_sync(
                        affected_keys,
                        allow_partial=effective_allow_partial,
                    )
                self._connection.execute("COMMIT")
            except BaseException:
                with suppress(Exception):
                    self._connection.execute("ROLLBACK")
                raise
        finally:
            self._connection.unregister(view_name)

        return max(0, len(rows) - existing_overlap_count)

    def _build_combined_silver_union_sql(self, prefix: str, study_key: str) -> str:
        """Build UNION ALL SQL across all per-Study views matching prefix."""
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
        return "\n            UNION ALL\n            ".join(
            f'SELECT * FROM "{prefix}{key}"' for key in built_study_keys
        )

    def _build_silver_view_sync(self, study_key: str, *, include_deleted: bool = False) -> None:
        """Materialize per-Study and combined silver current-state and history views."""
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
        current_union_sql = self._build_combined_silver_union_sql(
            "silver_current_state_", study_key
        )
        combined_current_query = f"""
            CREATE OR REPLACE VIEW silver_current_state AS
            {current_union_sql}
            """
        self._connection.execute(combined_current_query)  # nosem

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
                prev_record_status,
                prev_data_revision,
                prev_deleted,
                data_changed,
                CASE
                    WHEN COALESCE(deleted, FALSE) AND NOT COALESCE(prev_deleted, FALSE) THEN 'deleted'
                    WHEN NOT _has_prev THEN
                        CASE
                            WHEN record_revision IS NULL OR record_revision <= 1 THEN 'created'
                            ELSE 'window_baseline'
                        END
                    WHEN COALESCE(data_changed, FALSE) THEN 'data_updated'
                    WHEN record_status IS DISTINCT FROM prev_record_status THEN 'status_changed'
                    ELSE 'metadata_updated'
                END AS change_type,
                valid_from,
                valid_to,
                is_current
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
                    last_updated,
                    LAG(1) OVER rev_window IS NOT NULL AS _has_prev,
                    LAG(record_status) OVER rev_window AS prev_record_status,
                    LAG(data_revision) OVER rev_window AS prev_data_revision,
                    LAG(deleted) OVER rev_window AS prev_deleted,
                    CASE
                        WHEN LAG(1) OVER rev_window IS NULL THEN
                            CASE
                                WHEN record_revision IS NULL OR record_revision <= 1
                                    THEN COALESCE(data_revision, 0) > 0
                                ELSE CAST(NULL AS BOOLEAN)
                            END
                        ELSE COALESCE(data_revision, 0) > COALESCE(LAG(data_revision) OVER rev_window, 0)
                    END AS data_changed,
                    COALESCE(date_created, last_updated) AS valid_from,
                    LEAD(COALESCE(date_created, last_updated)) OVER rev_window AS valid_to,
                    LEAD(1) OVER rev_window IS NULL AS is_current
                FROM bronze_revisions
                WHERE study_key = '{escaped_study_key}'
                WINDOW rev_window AS (
                    PARTITION BY {record_partition_expr}
                    ORDER BY
                        record_revision ASC NULLS LAST,
                        data_revision ASC NULLS LAST,
                        date_created ASC NULLS LAST,
                        record_revision_id ASC NULLS LAST
                )
            ) AS history_projection
            """
        self._connection.execute(per_study_history_query)  # nosem
        history_union_sql = self._build_combined_silver_union_sql(
            "silver_records_history_", study_key
        )
        combined_history_query = f"""
            CREATE OR REPLACE VIEW silver_records_history AS
            {history_union_sql}
            """
        self._connection.execute(combined_history_query)  # nosem

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
        self,
        study_key: str,
        revisions: Iterable[RecordRevision],
        *,
        mask_phi: bool | None = None,
    ) -> Iterable[tuple[Any, ...]]:
        """Internal helper to convert RecordRevision models into database rows."""
        self._validate_study_key(study_key)
        effective_mask_phi = self._mask_phi if mask_phi is None else bool(mask_phi)
        phi_context_cache: dict[str, SensitivityRegistry] = {}
        sdk_ref = getattr(self, "_sdk", None)

        def _resolve_phi_context(sk: str) -> SensitivityRegistry:
            if sk not in phi_context_cache:
                custom_keys = set(resolve_cli_phi_fields(sk, sdk=sdk_ref))
                if resolve_enrichment_pipeline is not _ORIG_RESOLVE_PIPELINE:
                    with sdk_governance_context(sdk_ref):
                        pipeline = resolve_enrichment_pipeline(sk)
                    if pipeline is not None and getattr(pipeline, "phi_keys", None):
                        custom_keys.update(
                            str(k)
                            for k in pipeline.phi_keys
                            if not global_sensitivity_registry.is_exempt(str(k))
                        )
                phi_context_cache[sk] = build_study_sensitivity_registry(custom_keys)
            return phi_context_cache[sk]

        for revision in revisions:
            raw_study_key = getattr(revision, "study_key", None)
            rev_study_key = self._validate_study_key(raw_study_key) if raw_study_key else study_key
            rev_study_str = str(rev_study_key)
            deleted_val = getattr(revision, "deleted", False)

            record_revision_id = self._optional_int(getattr(revision, "record_revision_id", None))
            record_id = self._optional_int(getattr(revision, "record_id", None))
            site_id = self._optional_int(getattr(revision, "site_id", None))
            interval_id = self._optional_int(getattr(revision, "interval_id", None))
            date_created = self._optional_timestamp(getattr(revision, "date_created", None))
            last_updated = self._optional_timestamp(getattr(revision, "last_updated", None))

            record_oid = self._optional_str(getattr(revision, "record_oid", None))
            form_key = self._optional_str(getattr(revision, "form_key", None))
            subject_key = self._optional_str(getattr(revision, "subject_key", None))
            record_status = self._optional_str(getattr(revision, "record_status", None))
            user_val = self._optional_str(getattr(revision, "user", None))
            role_val = self._optional_str(getattr(revision, "role", None))
            reason_val = self._optional_str(getattr(revision, "reason_for_change", None))

            if effective_mask_phi:
                active_registry = _resolve_phi_context(rev_study_str)
                raw_str_map: dict[str, Any] = {
                    k: v
                    for k, v in (
                        ("record_oid", record_oid),
                        ("form_key", form_key),
                        ("subject_key", subject_key),
                        ("record_status", record_status),
                        ("user", user_val),
                        ("role", role_val),
                        ("reason_for_change", reason_val),
                    )
                    if v is not None
                }
                if raw_str_map:
                    masked_map = mask_clinical_phi(raw_str_map, registry=active_registry)
                    record_oid = masked_map.get("record_oid")
                    form_key = masked_map.get("form_key")
                    subject_key = masked_map.get("subject_key")
                    record_status = masked_map.get("record_status")
                    user_val = masked_map.get("user")
                    role_val = masked_map.get("role")
                    reason_val = masked_map.get("reason_for_change")
                    if isinstance(reason_val, str) and reason_val != "***MASKED***":
                        reason_val = sanitize_diagnostic_text(reason_val, registry=active_registry)
                if active_registry.is_sensitive("record_revision_id"):
                    record_revision_id = None
                if active_registry.is_sensitive("site_id"):
                    site_id = None
                if active_registry.is_sensitive("interval_id"):
                    interval_id = None
                if active_registry.is_sensitive("date_created"):
                    date_created = None
                if active_registry.is_sensitive("last_updated"):
                    last_updated = None

            yield (
                rev_study_str,
                record_revision_id,
                record_id,
                record_oid,
                form_key,
                subject_key,
                site_id,
                interval_id,
                record_status,
                self._optional_int(getattr(revision, "record_revision", None)),
                self._optional_int(getattr(revision, "data_revision", None)),
                user_val,
                role_val,
                reason_val,
                False if deleted_val is None else bool(deleted_val),
                date_created,
                last_updated,
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


class AsyncDuckDBIngestionWorkflow(_DuckDBCentralizerBase):
    """Asynchronous incremental record centralization pipeline using DuckDB."""

    supports_async = True

    def __init__(
        self,
        sdk: AsyncImednetFacade,
        db_path: str = ":memory:",
        *,
        mask_phi: bool = True,
    ) -> None:
        """Initialize the asynchronous DuckDB ingestion workflow."""
        super().__init__(db_path=db_path, mask_phi=mask_phi)
        self._sdk = sdk
        self._extractor = AsyncDataExtractionWorkflow(sdk)

    async def aclose(self) -> None:
        """Asynchronously close the underlying DuckDB connection."""
        await maybe_to_thread(self.close)

    async def __aenter__(self) -> Self:
        """Enter the async context manager and return the workflow instance."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the async context manager and close the DuckDB connection."""
        await self.aclose()

    async def ingest_revisions(
        self,
        study_key: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        mode: Literal["append", "replace"] = "append",
        mask_phi: bool | None = None,
        validate_chain: bool = False,
        allow_partial: bool | None = None,
    ) -> int:
        """Asynchronously fetch RecordRevisions and write to the bronze_revisions table."""
        self._validate_study_key(study_key)
        if mode not in {"append", "replace"}:
            raise ValueError("mode must be one of: 'append', 'replace'")
        effective_mask_phi = self._mask_phi if mask_phi is None else bool(mask_phi)
        effective_allow_partial = (
            bool(allow_partial) if allow_partial is not None else bool(start_date or end_date)
        )
        fetch_kwargs: dict[str, Any] = {}
        if validate_chain and _extractor_accepts_kwarg(self._extractor.extract_audit_trail, "sort"):
            fetch_kwargs["sort"] = "recordRevision,asc"

        revisions = await self._extractor.extract_audit_trail(
            study_key,
            start_date=start_date,
            end_date=end_date,
            **fetch_kwargs,
        )
        return await maybe_to_thread(
            self._ingest_fetched_revisions_sync,
            study_key,
            revisions,
            mode=mode,
            effective_mask_phi=effective_mask_phi,
            validate_chain=bool(validate_chain),
            effective_allow_partial=effective_allow_partial,
        )

    async def build_silver_view(self, study_key: str, *, include_deleted: bool = False) -> None:
        """Asynchronously create or replace per-Study and combined silver views."""
        self._validate_study_key(study_key)
        await maybe_to_thread(
            self._build_silver_view_sync,
            study_key,
            include_deleted=include_deleted,
        )


class DuckDBIngestionWorkflow(_DuckDBCentralizerBase):
    """Incremental record centralization pipeline using a bronze/silver DuckDB layout."""

    def __init__(
        self,
        sdk: ImednetFacade,
        db_path: str = ":memory:",
        *,
        mask_phi: bool = True,
    ) -> None:
        """Initialize the DuckDB ingestion workflow.

        Args:
            sdk: An instance of the synchronous iMednet SDK facade.
            db_path: Path to the DuckDB database file or ":memory:". Defaults to ":memory:".
            mask_phi: Redact sensitive clinical and Study-configured PHI values by default.
        """
        super().__init__(db_path=db_path, mask_phi=mask_phi)
        self._sdk = sdk
        self._extractor = DataExtractionWorkflow(sdk)

    def ingest_revisions(
        self,
        study_key: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        mode: Literal["append", "replace"] = "append",
        mask_phi: bool | None = None,
        validate_chain: bool = False,
        allow_partial: bool | None = None,
    ) -> int:
        """Fetch RecordRevisions and write to the bronze_revisions table."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "DuckDBIngestionWorkflow",
            "ingest_revisions",
            "ingest_revisions",
        )
        self._validate_study_key(study_key)
        if mode not in {"append", "replace"}:
            raise ValueError("mode must be one of: 'append', 'replace'")
        effective_mask_phi = self._mask_phi if mask_phi is None else bool(mask_phi)
        effective_allow_partial = (
            bool(allow_partial) if allow_partial is not None else bool(start_date or end_date)
        )
        fetch_kwargs: dict[str, Any] = {}
        if validate_chain and _extractor_accepts_kwarg(self._extractor.extract_audit_trail, "sort"):
            fetch_kwargs["sort"] = "recordRevision,asc"

        revisions = self._extractor.extract_audit_trail(
            study_key,
            start_date=start_date,
            end_date=end_date,
            **fetch_kwargs,
        )
        return self._ingest_fetched_revisions_sync(
            study_key,
            revisions,
            mode=mode,
            effective_mask_phi=effective_mask_phi,
            validate_chain=bool(validate_chain),
            effective_allow_partial=effective_allow_partial,
        )

    def build_silver_view(self, study_key: str, *, include_deleted: bool = False) -> None:
        """Create or replace per-Study and combined silver current-state and history views."""
        ensure_sync_workflow_mode(
            is_async_facade(self._sdk),
            "DuckDBIngestionWorkflow",
            "build_silver_view",
            "build_silver_view",
        )
        self._build_silver_view_sync(study_key, include_deleted=include_deleted)
