"""Workflow for loading study records with local SQLite caching for performance."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from filelock import FileLock
from pydantic import ValidationError as PydanticValidationError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from imednet.spi.errors import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    BadRequestError,
    ClientError,
    ConflictError,
    NotFoundError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from imednet.spi.models import Record
from imednet.spi.sink_base import is_record_deleted
from imednet.spi.utils import (
    build_filter_string,
    delegates_to_sdk_retry_policy,
    normalize_utc_timestamp_bound,
    redact_sensitive_text,
    resolve_sdk_base_url,
)

from .chunked_pipeline import DEFAULT_CHUNK_SIZE

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

DEFAULT_CACHE_DIR = Path.home() / ".imednet" / "cache"
_EPOCH_FLOOR_WATERMARK = "1970-01-01T00:00:00Z"

_T = TypeVar("_T")


class StudySyncStatus(StrEnum):
    """Lifecycle status for per-study SQLite cache synchronization."""

    IDLE = "idle"
    SYNCING = "syncing"
    SYNCED = "synced"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StudySyncState:
    """Snapshot of synchronization epoch and watermark metadata for a study."""

    study_key: str
    sync_epoch: int = 0
    high_water_mark: str | None = None
    initial_sync_complete: bool = False
    status: StudySyncStatus = StudySyncStatus.IDLE
    last_synced_at: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class _LocalRecordMeta:
    """Lightweight metadata snapshot of a locally cached Record."""

    date_modified: str | None
    form_key: str
    record_status: str
    revision: int | None
    last_updated: str | None


def _serialize_date_modified(value: Any) -> str | None:
    """Serialize a record modification timestamp, storing missing values as SQL NULL."""
    if value is None:
        return None
    serialized = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return serialized if serialized.strip() else None


def _normalize_status_token(value: Any) -> str:
    """Extract normalized string representation for a Record status field."""
    if value is None:
        return ""
    raw = value.value if hasattr(value, "value") else value
    return str(raw).strip()


def _normalize_optional_int(value: Any) -> int | None:
    """Return integer value when positive/non-boolean, otherwise None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _is_projected_record_updated(projected: Record, local_meta: _LocalRecordMeta) -> bool:
    """Return True when upstream lightweight projection metadata supersedes cached metadata."""
    proj_dm = _serialize_date_modified(projected.date_modified)
    if proj_dm is not None and proj_dm != local_meta.date_modified:
        return True

    proj_status = _normalize_status_token(projected.record_status)
    if proj_status and proj_status != local_meta.record_status:
        return True

    proj_form_key = (projected.form_key or "").strip()
    if proj_form_key and proj_form_key != local_meta.form_key:
        return True

    proj_rev = _normalize_optional_int(getattr(projected, "revision", None))
    if proj_rev is not None and proj_rev != local_meta.revision:
        return True

    proj_last_updated = _serialize_date_modified(getattr(projected, "last_updated", None))
    return bool(proj_last_updated is not None and proj_last_updated != local_meta.last_updated)


def _is_retryable_loader_exception(exc: BaseException) -> bool:
    """Return True only for transient operational errors; never retry 4xx or validation errors."""
    if not isinstance(exc, Exception):
        return False
    if isinstance(
        exc,
        (
            ClientError,
            PydanticValidationError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ),
    ):
        return False
    if isinstance(
        exc,
        (
            BadRequestError,
            ValidationError,
            AuthenticationError,
            AuthorizationError,
            NotFoundError,
            ConflictError,
        ),
    ):
        return False
    if isinstance(exc, (RateLimitError, ServerError)):
        return True
    if isinstance(exc, ApiError):
        status = exc.status_code
        return status == 429 or (status is not None and 500 <= status < 600)
    return True


def _timestamp_sort_key(timestamp: str) -> tuple[int, Any]:
    """Return a chronological sort key for ISO-8601 timestamps with string fallback."""
    normalized = timestamp.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        utc_dt = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return (0, timestamp)
    return (1, utc_dt)


def _max_timestamp(candidates: Iterable[str | None]) -> str | None:
    """Return the chronologically latest non-empty timestamp string while preserving formatting."""
    valid = [ts for ts in candidates if ts is not None and ts.strip()]
    if not valid:
        return None
    return max(valid, key=_timestamp_sort_key)


# Per-DB-path locks that serialise _initialise_cache across threads within the
# same process.  Switching an SQLite database to WAL journal mode requires a
# brief exclusive lock; if multiple threads attempt the switch simultaneously
# they all race for that lock and the losers immediately raise
# ``sqlite3.OperationalError: database is locked``.  Serialising initialisation
# eliminates the race entirely.  The dict grows at most one entry per distinct
# database file, which is negligible.
_db_init_locks: dict[str, threading.Lock] = {}
_db_init_locks_guard = threading.Lock()


def _get_db_init_lock(resolved_path: Path) -> threading.Lock:
    """Return a thread-safe lock for initializing a specific database file."""
    key = str(resolved_path)
    with _db_init_locks_guard:
        return _db_init_locks.setdefault(key, threading.Lock())


def get_sqlite_connection(db_path: str | Path) -> sqlite3.Connection:
    """Return a SQLite connection configured for concurrent cache access."""
    resolved_path = Path(db_path).expanduser()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # busy_timeout instructs SQLite to retry at the C level on SQLITE_BUSY
    # (e.g. during the WAL transition); this complements Python's connect
    # timeout and is also effective in cross-process scenarios.
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


class CachedRecordsLoader:
    """Load study records through a local SQLite cache with origin-scoped incremental sync."""

    def __init__(
        self,
        sdk: ImednetFacade,
        *,
        cache_dir: str | Path | None = None,
        database_name: str = "records_cache.sqlite3",
        retry_attempts: int = 3,
        lock_timeout_seconds: int = 30,
        lock: FileLock | None = None,
    ) -> None:
        """Initialize the cached records loader.

        Args:
            sdk: An instance of the iMednet SDK facade.
            cache_dir: Directory to store the SQLite cache. Defaults to ~/.imednet/cache.
            database_name: Name of the SQLite database file.
            retry_attempts: Number of API retry attempts for facade implementations without SDK retry policy.
            lock_timeout_seconds: Number of seconds to wait when acquiring the cache file lock.
            lock: Optional FileLock instance for synchronizing write transactions.
        """
        self._sdk = sdk
        self._base_url = resolve_sdk_base_url(sdk)
        self._tx_state = threading.local()
        base_dir = DEFAULT_CACHE_DIR if cache_dir is None else Path(cache_dir).expanduser()
        self.db_path = base_dir / database_name
        self._retry_attempts = retry_attempts
        lock_path = Path(f"{self.db_path}.lock")
        self._lock = (
            lock if lock is not None else FileLock(str(lock_path), timeout=lock_timeout_seconds)
        )
        self._initialise_cache()

    @property
    def base_url(self) -> str:
        """Return the normalized SDK origin URL partitioning this loader's cache state."""
        return self._base_url

    @property
    def lock(self) -> FileLock:
        """Return the inter-process file lock protecting cache write operations."""
        return self._lock

    def load_records(self, study_key: str, *, reconcile: bool = True) -> list[Record]:
        """Synchronise the cache for ``study_key`` and return cached records."""
        self.sync_records(study_key, reconcile=reconcile)
        return self.get_cached_records(study_key)

    def sync_records(self, study_key: str, *, reconcile: bool = True) -> None:
        """Synchronise the cache for ``study_key`` in a single atomic commit after network I/O."""
        conn = get_sqlite_connection(self.db_path)
        try:
            sync_state = self.get_study_sync_state(study_key, conn=conn)
            cycle_timestamps: list[str | None] = [sync_state.high_water_mark]

            try:
                delta_records: list[Record] = []
                if not sync_state.initial_sync_complete and sync_state.high_water_mark is None:
                    delta_records = self._fetch_delta_records(study_key, None)
                elif sync_state.high_water_mark is not None:
                    delta_records = self._fetch_delta_records(study_key, sync_state.high_water_mark)
                elif not reconcile:
                    delta_records = self._fetch_delta_records(study_key, _EPOCH_FLOOR_WATERMARK)

                active_delta_records: list[Record] = []
                voided_delta_ids: set[int] = set()
                for record in delta_records:
                    cycle_timestamps.append(_serialize_date_modified(record.date_modified))
                    if is_record_deleted(record):
                        if record.record_id is not None:
                            voided_delta_ids.add(record.record_id)
                    else:
                        active_delta_records.append(record)

                pre_reconcile_epoch: int | None = None
                active_record_ids: set[int] = set()
                backfill_records: list[Record] = []
                if reconcile:
                    pre_reconcile_epoch = self._get_sync_epoch(conn, study_key)
                    projected_records = self._fetch_active_records_projection(study_key)
                    active_record_ids = {
                        record.record_id
                        for record in projected_records
                        if record.record_id is not None and not is_record_deleted(record)
                    }
                    local_meta = self._get_local_record_metadata(conn, study_key)
                    delta_active_ids = {
                        record.record_id
                        for record in active_delta_records
                        if record.record_id is not None
                    }
                    missing_ids = active_record_ids - set(local_meta.keys()) - delta_active_ids
                    stale_ids = {
                        proj.record_id
                        for proj in projected_records
                        if proj.record_id is not None
                        and not is_record_deleted(proj)
                        and proj.record_id in local_meta
                        and proj.record_id not in delta_active_ids
                        and _is_projected_record_updated(proj, local_meta[proj.record_id])
                    }
                    ids_to_fetch = missing_ids | stale_ids
                    if ids_to_fetch:
                        fetched_missing = self._fetch_missing_records(study_key, ids_to_fetch)
                        for record in fetched_missing:
                            cycle_timestamps.append(_serialize_date_modified(record.date_modified))
                            if is_record_deleted(record):
                                if record.record_id is not None:
                                    voided_delta_ids.add(record.record_id)
                            else:
                                backfill_records.append(record)

                with self._lock, conn:
                    self._tx_state.in_atomic_commit = True
                    try:
                        if active_delta_records:
                            self._upsert_records_unlocked(
                                conn,
                                active_delta_records,
                                default_study_key=study_key,
                            )
                        if voided_delta_ids:
                            self._delete_record_ids_unlocked(conn, study_key, voided_delta_ids)
                        if backfill_records:
                            self._upsert_records_unlocked(
                                conn,
                                backfill_records,
                                default_study_key=study_key,
                            )
                        if reconcile:
                            self.reconcile_cache(
                                conn,
                                study_key,
                                active_record_ids,
                                max_epoch=pre_reconcile_epoch,
                            )
                        self._complete_study_sync_unlocked(conn, study_key, cycle_timestamps)
                    finally:
                        self._tx_state.in_atomic_commit = False
            except Exception as exc:
                self._fail_study_sync(conn, study_key, exc)
                raise
        finally:
            conn.close()

    def get_study_sync_state(
        self, study_key: str, *, conn: sqlite3.Connection | None = None
    ) -> StudySyncState:
        """Return the stored sync epoch, watermark, and status metadata for ``study_key``."""
        close_conn = False
        if conn is None:
            conn = get_sqlite_connection(self.db_path)
            close_conn = True
        try:
            row = conn.execute(
                """
                SELECT
                    study_key,
                    sync_epoch,
                    high_water_mark,
                    initial_sync_complete,
                    status,
                    last_synced_at,
                    error_message
                FROM study_sync_state
                WHERE base_url = ? AND study_key = ?
                """,
                (self._base_url, study_key),
            ).fetchone()
            if row is None:
                return StudySyncState(study_key=study_key)
            raw_status = cast(str | None, row["status"]) or StudySyncStatus.IDLE.value
            try:
                parsed_status = StudySyncStatus(raw_status)
            except ValueError:
                parsed_status = StudySyncStatus.IDLE
            return StudySyncState(
                study_key=cast(str, row["study_key"]),
                sync_epoch=int(row["sync_epoch"]),
                high_water_mark=cast(str | None, row["high_water_mark"]),
                initial_sync_complete=bool(row["initial_sync_complete"]),
                status=parsed_status,
                last_synced_at=cast(str | None, row["last_synced_at"]),
                error_message=cast(str | None, row["error_message"]),
            )
        finally:
            if close_conn:
                conn.close()

    def count_cached_records(
        self, study_key: str, *, conn: sqlite3.Connection | None = None
    ) -> int:
        """Return the count of cached records for ``study_key`` without materialising rows."""
        close_conn = False
        if conn is None:
            conn = get_sqlite_connection(self.db_path)
            close_conn = True
        try:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM record_cache
                WHERE base_url = ? AND study_key = ?
                """,
                (self._base_url, study_key),
            ).fetchone()
            if row is None:
                return 0
            return int(row[0])
        finally:
            if close_conn:
                conn.close()

    def get_cached_records(
        self, study_key: str, *, conn: sqlite3.Connection | None = None
    ) -> list[Record]:
        """Return cached records for ``study_key`` without contacting the API."""
        return list(self.iter_cached_records(study_key, conn=conn))

    def iter_cached_records(
        self,
        study_key: str,
        *,
        conn: sqlite3.Connection | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        order_by_subject: bool = False,
    ) -> Iterator[Record]:
        """Yield cached records for ``study_key`` in bounded chunks."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")

        close_conn = False
        if conn is None:
            conn = get_sqlite_connection(self.db_path)
            close_conn = True
        query = (
            """
            SELECT payload
            FROM record_cache
            WHERE base_url = ? AND study_key = ?
            ORDER BY
                COALESCE(
                    NULLIF(TRIM(CAST(json_extract(payload, '$.recordData.USUBJID') AS TEXT)), ''),
                    NULLIF(TRIM(CAST(json_extract(payload, '$.recordData.usubjid') AS TEXT)), ''),
                    NULLIF(TRIM(CAST(json_extract(payload, '$.subjectKey') AS TEXT)), ''),
                    NULLIF(TRIM(CAST(json_extract(payload, '$.subject_key') AS TEXT)), ''),
                    ''
                ) ASC,
                record_id ASC
            """
            if order_by_subject
            else """
            SELECT payload
            FROM record_cache
            WHERE base_url = ? AND study_key = ?
            ORDER BY record_id
            """
        )
        try:
            cursor = conn.execute(query, (self._base_url, study_key))
            while True:
                rows = cursor.fetchmany(chunk_size)
                if not rows:
                    break
                for row in rows:
                    record = Record.from_json(json.loads(cast(str, row["payload"])))
                    if not is_record_deleted(record):
                        yield record
        finally:
            if close_conn:
                conn.close()

    def reconcile_cache(
        self,
        conn: sqlite3.Connection,
        study_key: str,
        active_record_ids: set[int],
        *,
        max_epoch: int | None = None,
    ) -> None:
        """Prune records removed from the upstream EDC backend up to ``max_epoch``."""
        if getattr(self._tx_state, "in_atomic_commit", False):
            self._reconcile_cache_unlocked(
                conn,
                study_key,
                active_record_ids,
                max_epoch=max_epoch,
            )
            return
        with self._lock, conn:
            self._reconcile_cache_unlocked(
                conn,
                study_key,
                active_record_ids,
                max_epoch=max_epoch,
            )

    def _reconcile_cache_unlocked(
        self,
        conn: sqlite3.Connection,
        study_key: str,
        active_record_ids: set[int],
        *,
        max_epoch: int | None = None,
    ) -> None:
        """Execute epoch-fenced orphan deletion inside an active lock-held transaction."""
        effective_epoch = (
            max_epoch if max_epoch is not None else self._get_sync_epoch(conn, study_key)
        )
        local_rows = conn.execute(
            """
            SELECT record_id
            FROM record_cache
            WHERE base_url = ? AND study_key = ? AND upserted_epoch <= ?
            """,
            (self._base_url, study_key, effective_epoch),
        ).fetchall()
        local_ids = {cast(int, row["record_id"]) for row in local_rows}
        orphaned_ids = local_ids - active_record_ids
        if orphaned_ids:
            conn.executemany(
                """
                DELETE FROM record_cache
                WHERE base_url = ? AND study_key = ? AND record_id = ? AND upserted_epoch <= ?
                """,
                [
                    (self._base_url, study_key, orphaned_id, effective_epoch)
                    for orphaned_id in orphaned_ids
                ],
            )

    def _delete_record_ids_unlocked(
        self,
        conn: sqlite3.Connection,
        study_key: str,
        record_ids: set[int],
    ) -> None:
        """Delete voided record IDs from ``record_cache`` inside an active transaction."""
        if not record_ids:
            return
        conn.executemany(
            """
            DELETE FROM record_cache
            WHERE base_url = ? AND study_key = ? AND record_id = ?
            """,
            [(self._base_url, study_key, rec_id) for rec_id in sorted(record_ids)],
        )

    def _initialise_cache(self) -> None:
        """Ensure the cache database and origin-partitioned tables are created and migrated."""
        resolved = Path(self.db_path).expanduser().resolve()
        with _get_db_init_lock(resolved):
            conn = get_sqlite_connection(self.db_path)
            try:
                with self._lock, conn:
                    self._migrate_or_create_record_cache(conn)
                    self._migrate_or_create_study_sync_state(conn)
                    self._backfill_legacy_study_sync_state(conn)
            finally:
                conn.close()

    def _migrate_or_create_record_cache(self, conn: sqlite3.Connection) -> None:
        """Create ``record_cache`` or migrate legacy schemas in place without data loss."""
        table_info = conn.execute("PRAGMA table_info(record_cache)").fetchall()
        if not table_info:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS record_cache (
                    base_url TEXT NOT NULL,
                    study_key TEXT NOT NULL,
                    record_id INTEGER NOT NULL,
                    form_key TEXT NOT NULL,
                    date_modified TEXT,
                    payload TEXT NOT NULL,
                    upserted_epoch INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (base_url, study_key, record_id)
                )
                """
            )
        else:
            columns = {cast(str, row["name"]): row for row in table_info}
            pk_cols = [
                cast(str, row["name"])
                for row in sorted(table_info, key=lambda r: int(r["pk"]))
                if int(row["pk"]) > 0
            ]
            date_mod_col = columns.get("date_modified")
            needs_rebuild = (
                "base_url" not in columns
                or "upserted_epoch" not in columns
                or pk_cols != ["base_url", "study_key", "record_id"]
                or (date_mod_col is not None and int(date_mod_col["notnull"]) != 0)
            )
            if needs_rebuild:
                conn.execute("DROP TABLE IF EXISTS record_cache_migrated")
                conn.execute(
                    """
                    CREATE TABLE record_cache_migrated (
                        base_url TEXT NOT NULL,
                        study_key TEXT NOT NULL,
                        record_id INTEGER NOT NULL,
                        form_key TEXT NOT NULL,
                        date_modified TEXT,
                        payload TEXT NOT NULL,
                        upserted_epoch INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (base_url, study_key, record_id)
                    )
                    """
                )
                if "base_url" in columns and "upserted_epoch" in columns:
                    conn.execute(
                        """
                        INSERT INTO record_cache_migrated (
                            base_url,
                            study_key,
                            record_id,
                            form_key,
                            date_modified,
                            payload,
                            upserted_epoch
                        )
                        SELECT
                            COALESCE(NULLIF(base_url, ''), ?),
                            study_key,
                            record_id,
                            form_key,
                            NULLIF(date_modified, ''),
                            payload,
                            COALESCE(upserted_epoch, 1)
                        FROM record_cache
                        """,
                        (self._base_url,),
                    )
                elif "upserted_epoch" in columns:
                    conn.execute(
                        """
                        INSERT INTO record_cache_migrated (
                            base_url,
                            study_key,
                            record_id,
                            form_key,
                            date_modified,
                            payload,
                            upserted_epoch
                        )
                        SELECT
                            ?,
                            study_key,
                            record_id,
                            form_key,
                            NULLIF(date_modified, ''),
                            payload,
                            COALESCE(upserted_epoch, 1)
                        FROM record_cache
                        """,
                        (self._base_url,),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO record_cache_migrated (
                            base_url,
                            study_key,
                            record_id,
                            form_key,
                            date_modified,
                            payload,
                            upserted_epoch
                        )
                        SELECT
                            ?,
                            study_key,
                            record_id,
                            form_key,
                            NULLIF(date_modified, ''),
                            payload,
                            1
                        FROM record_cache
                        """,
                        (self._base_url,),
                    )
                conn.execute("DROP TABLE record_cache")
                conn.execute("ALTER TABLE record_cache_migrated RENAME TO record_cache")

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_record_cache_origin_study_modified
            ON record_cache (base_url, study_key, date_modified)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_record_cache_origin_study_epoch
            ON record_cache (base_url, study_key, upserted_epoch)
            """
        )

    def _migrate_or_create_study_sync_state(self, conn: sqlite3.Connection) -> None:
        """Create ``study_sync_state`` or migrate legacy unpartitioned rows in place."""
        sync_info = conn.execute("PRAGMA table_info(study_sync_state)").fetchall()
        if not sync_info:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS study_sync_state (
                    base_url TEXT NOT NULL,
                    study_key TEXT NOT NULL,
                    sync_epoch INTEGER NOT NULL DEFAULT 0,
                    high_water_mark TEXT,
                    initial_sync_complete INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_synced_at TEXT,
                    error_message TEXT,
                    PRIMARY KEY (base_url, study_key)
                )
                """
            )
            return

        sync_cols = {cast(str, row["name"]): row for row in sync_info}
        sync_pk = [
            cast(str, row["name"])
            for row in sorted(sync_info, key=lambda r: int(r["pk"]))
            if int(row["pk"]) > 0
        ]
        if "base_url" in sync_cols and sync_pk == ["base_url", "study_key"]:
            return

        conn.execute("DROP TABLE IF EXISTS study_sync_state_migrated")
        conn.execute(
            """
            CREATE TABLE study_sync_state_migrated (
                base_url TEXT NOT NULL,
                study_key TEXT NOT NULL,
                sync_epoch INTEGER NOT NULL DEFAULT 0,
                high_water_mark TEXT,
                initial_sync_complete INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'idle',
                last_synced_at TEXT,
                error_message TEXT,
                PRIMARY KEY (base_url, study_key)
            )
            """
        )
        if "base_url" in sync_cols:
            conn.execute(
                """
                INSERT INTO study_sync_state_migrated (
                    base_url,
                    study_key,
                    sync_epoch,
                    high_water_mark,
                    initial_sync_complete,
                    status,
                    last_synced_at,
                    error_message
                )
                SELECT
                    COALESCE(NULLIF(base_url, ''), ?),
                    study_key,
                    COALESCE(sync_epoch, 0),
                    NULLIF(high_water_mark, ''),
                    COALESCE(initial_sync_complete, 0),
                    COALESCE(status, 'idle'),
                    last_synced_at,
                    error_message
                FROM study_sync_state
                """,
                (self._base_url,),
            )
        else:
            conn.execute(
                """
                INSERT INTO study_sync_state_migrated (
                    base_url,
                    study_key,
                    sync_epoch,
                    high_water_mark,
                    initial_sync_complete,
                    status,
                    last_synced_at,
                    error_message
                )
                SELECT
                    ?,
                    study_key,
                    COALESCE(sync_epoch, 0),
                    NULLIF(high_water_mark, ''),
                    COALESCE(initial_sync_complete, 0),
                    COALESCE(status, 'idle'),
                    last_synced_at,
                    error_message
                FROM study_sync_state
                """,
                (self._base_url,),
            )
        conn.execute("DROP TABLE study_sync_state")
        conn.execute("ALTER TABLE study_sync_state_migrated RENAME TO study_sync_state")

    def _backfill_legacy_study_sync_state(self, conn: sqlite3.Connection) -> None:
        """Synthesize missing ``study_sync_state`` rows from migrated ``record_cache`` rows."""
        legacy_studies = conn.execute(
            """
            SELECT DISTINCT r.base_url, r.study_key
            FROM record_cache AS r
            LEFT JOIN study_sync_state AS s
                ON s.base_url = r.base_url AND s.study_key = r.study_key
            WHERE s.study_key IS NULL
            """
        ).fetchall()
        for study_row in legacy_studies:
            legacy_base_url = cast(str, study_row["base_url"])
            legacy_study_key = cast(str, study_row["study_key"])
            epoch_row = conn.execute(
                """
                SELECT COALESCE(MAX(upserted_epoch), 1)
                FROM record_cache
                WHERE base_url = ? AND study_key = ?
                """,
                (legacy_base_url, legacy_study_key),
            ).fetchone()
            max_epoch = int(epoch_row[0]) if epoch_row is not None else 1
            ts_rows = conn.execute(
                """
                SELECT date_modified
                FROM record_cache
                WHERE base_url = ? AND study_key = ? AND date_modified IS NOT NULL
                """,
                (legacy_base_url, legacy_study_key),
            ).fetchall()
            hwm = _max_timestamp(cast(str | None, ts_row["date_modified"]) for ts_row in ts_rows)
            conn.execute(
                """
                INSERT OR IGNORE INTO study_sync_state (
                    base_url,
                    study_key,
                    sync_epoch,
                    high_water_mark,
                    initial_sync_complete,
                    status
                )
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (legacy_base_url, legacy_study_key, max_epoch, hwm, StudySyncStatus.SYNCED.value),
            )

    def _get_sync_epoch(self, conn: sqlite3.Connection, study_key: str) -> int:
        """Return the current monotonic sync epoch for ``study_key``."""
        row = conn.execute(
            "SELECT sync_epoch FROM study_sync_state WHERE base_url = ? AND study_key = ?",
            (self._base_url, study_key),
        ).fetchone()
        if row is None:
            return 0
        return int(row["sync_epoch"])

    def _get_local_record_metadata(
        self, conn: sqlite3.Connection, study_key: str
    ) -> dict[int, _LocalRecordMeta]:
        """Return lightweight cached metadata indexed by ``record_id`` for ``study_key``."""
        rows = conn.execute(
            """
            SELECT
                record_id,
                date_modified,
                form_key,
                COALESCE(
                    json_extract(payload, '$.recordStatus'),
                    json_extract(payload, '$.record_status')
                ) AS record_status,
                json_extract(payload, '$.revision') AS revision,
                COALESCE(
                    json_extract(payload, '$.lastUpdated'),
                    json_extract(payload, '$.last_updated')
                ) AS last_updated
            FROM record_cache
            WHERE base_url = ? AND study_key = ?
            """,
            (self._base_url, study_key),
        ).fetchall()
        metadata: dict[int, _LocalRecordMeta] = {}
        for row in rows:
            rec_id = int(row["record_id"])
            metadata[rec_id] = _LocalRecordMeta(
                date_modified=_serialize_date_modified(row["date_modified"]),
                form_key=str(row["form_key"] or "").strip(),
                record_status=_normalize_status_token(row["record_status"]),
                revision=_normalize_optional_int(row["revision"]),
                last_updated=_serialize_date_modified(row["last_updated"]),
            )
        return metadata

    def _complete_study_sync_unlocked(
        self,
        conn: sqlite3.Connection,
        study_key: str,
        candidate_timestamps: Iterable[str | None],
    ) -> None:
        """Advance ``high_water_mark`` monotonically inside an active lock-held transaction."""
        now_iso = datetime.now(UTC).isoformat()
        candidates = list(candidate_timestamps)
        current_hwm = self._get_high_water_mark(conn, study_key)
        next_hwm = _max_timestamp([current_hwm, *candidates])
        conn.execute(
            """
            INSERT INTO study_sync_state (
                base_url,
                study_key,
                sync_epoch,
                high_water_mark,
                initial_sync_complete,
                status,
                last_synced_at,
                error_message
            )
            VALUES (?, ?, 0, ?, 1, ?, ?, NULL)
            ON CONFLICT(base_url, study_key) DO UPDATE SET
                high_water_mark = excluded.high_water_mark,
                initial_sync_complete = 1,
                status = excluded.status,
                last_synced_at = excluded.last_synced_at,
                error_message = NULL
            """,
            (self._base_url, study_key, next_hwm, StudySyncStatus.SYNCED.value, now_iso),
        )

    def _fail_study_sync(self, conn: sqlite3.Connection, study_key: str, exc: Exception) -> None:
        """Record sync failure metadata while preserving the existing ``high_water_mark``."""
        safe_error = redact_sensitive_text(str(exc)) or type(exc).__name__
        with self._lock, conn:
            conn.execute(
                """
                INSERT INTO study_sync_state (base_url, study_key, sync_epoch, status, error_message)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(base_url, study_key) DO UPDATE SET
                    status = excluded.status,
                    error_message = excluded.error_message
                """,
                (self._base_url, study_key, StudySyncStatus.FAILED.value, safe_error),
            )

    def _get_high_water_mark(self, conn: sqlite3.Connection, study_key: str) -> str | None:
        """Get the stored high-water mark timestamp from ``study_sync_state``."""
        row = conn.execute(
            "SELECT high_water_mark FROM study_sync_state WHERE base_url = ? AND study_key = ?",
            (self._base_url, study_key),
        ).fetchone()
        if row is None:
            return None
        value = cast(str | None, row["high_water_mark"])
        return value if value is not None and value.strip() else None

    def _fetch_delta_records(self, study_key: str, high_water_mark: str | None) -> list[Record]:
        """Fetch records from the API that have been modified since the high water mark."""
        if not high_water_mark:
            records = self._list_records(
                study_key=study_key,
                record_data_filter=None,
                deleted=False,
            )
            return [record for record in records if not is_record_deleted(record)]

        # Use >= to avoid missing updates that share the high-water-mark timestamp.
        # _upsert_records_unlocked keeps refresh idempotent by deduplicating on (base_url, study_key, record_id).
        normalized_hwm = normalize_utc_timestamp_bound(high_water_mark, is_end=False)
        delta_filter = build_filter_string({"date_modified": (">=", normalized_hwm)})
        return self._list_records_with_filter_override(
            study_key=study_key,
            filter_string=delta_filter,
        )

    def _fetch_active_records_projection(self, study_key: str) -> list[Record]:
        """Fetch lightweight non-deleted Record projections for reconciliation and draft checks."""
        records = self._list_records(study_key=study_key, record_data_filter="none", deleted=False)
        return [record for record in records if not is_record_deleted(record)]

    def _fetch_missing_records(self, study_key: str, missing_ids: set[int]) -> list[Record]:
        """Fetch full record payloads for upstream active IDs missing or stale in local cache."""
        if not missing_ids:
            return []
        record_id_bound: int | tuple[str, int] = (
            next(iter(missing_ids)) if len(missing_ids) == 1 else (">=", min(missing_ids))
        )
        records = self._list_records(
            study_key=study_key,
            record_id=record_id_bound,
            record_data_filter=None,
        )
        return [record for record in records if record.record_id in missing_ids]

    def _execute_with_retry_ownership(
        self,
        target: Callable[..., _T],
        *args: Any,
        **kwargs: Any,
    ) -> _T:
        """Invoke *target*, delegating transient retries when backed by SDK DefaultRetryPolicy."""
        if delegates_to_sdk_retry_policy(self._sdk, target):
            return target(*args, **kwargs)

        retryer = Retrying(
            stop=stop_after_attempt(max(1, self._retry_attempts)),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception(_is_retryable_loader_exception),
            reraise=True,
        )
        return retryer(target, *args, **kwargs)

    def _list_records(self, **filters: Any) -> list[Record]:
        """Call the SDK records list endpoint honoring retry ownership and permanent 4xx rules."""
        return self._execute_with_retry_ownership(self._sdk.get_records, **filters)

    def _list_records_with_filter_override(
        self, *, study_key: str, filter_string: str
    ) -> list[Record]:
        """List records using an explicit raw ``filter`` query parameter.

        This bypasses automatic filter construction so incremental sync can
        send only the timestamp predicate (without ``studyKey``).
        """
        endpoint = getattr(self._sdk, "records")  # noqa: B009
        list_fn = endpoint.list

        def _fetch_delta() -> list[Record]:
            return list(
                list_fn(
                    study_key=study_key,
                    filter=filter_string,
                    record_data_filter=None,
                )
            )

        if delegates_to_sdk_retry_policy(self._sdk, list_fn):
            return _fetch_delta()

        return self._execute_with_retry_ownership(_fetch_delta)

    def _upsert_records_unlocked(
        self,
        conn: sqlite3.Connection,
        records: Iterable[Record],
        *,
        default_study_key: str | None = None,
    ) -> None:
        """Insert or update active records inside an active lock-held transaction."""
        prepared: list[tuple[str, int, str, str | None, str]] = []
        for record in records:
            if is_record_deleted(record):
                continue
            resolved_study_key = record.study_key or default_study_key
            if resolved_study_key is None or record.record_id is None:
                continue
            prepared.append(
                (
                    cast(str, resolved_study_key),
                    cast(int, record.record_id),
                    cast(str, record.form_key or ""),
                    _serialize_date_modified(record.date_modified),
                    json.dumps(record.model_dump(mode="json", by_alias=True), sort_keys=True),
                )
            )

        if not prepared:
            return

        distinct_studies = list(dict.fromkeys(item[0] for item in prepared))
        epochs_by_study: dict[str, int] = {}
        for study_key in distinct_studies:
            conn.execute(
                """
                INSERT INTO study_sync_state (base_url, study_key, sync_epoch, status)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(base_url, study_key) DO UPDATE SET
                    sync_epoch = study_sync_state.sync_epoch + 1
                """,
                (self._base_url, study_key, StudySyncStatus.IDLE.value),
            )
            epochs_by_study[study_key] = self._get_sync_epoch(conn, study_key)

        payloads = [
            (
                self._base_url,
                study_key,
                record_id,
                form_key,
                date_modified,
                payload,
                epochs_by_study[study_key],
            )
            for study_key, record_id, form_key, date_modified, payload in prepared
        ]
        conn.executemany(
            """
            INSERT INTO record_cache (
                base_url,
                study_key,
                record_id,
                form_key,
                date_modified,
                payload,
                upserted_epoch
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(base_url, study_key, record_id) DO UPDATE SET
                form_key = excluded.form_key,
                date_modified = excluded.date_modified,
                payload = excluded.payload,
                upserted_epoch = excluded.upserted_epoch
            """,
            payloads,
        )
