"""Workflow for loading study records with local SQLite caching for performance."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from filelock import FileLock
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from imednet.spi.models import Record
from imednet.spi.utils import build_filter_string, redact_sensitive_text

from .chunked_pipeline import DEFAULT_CHUNK_SIZE

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

DEFAULT_CACHE_DIR = Path.home() / ".imednet" / "cache"
_MISSING_ID_BATCH_SIZE = 100


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


def _serialize_date_modified(value: Any) -> str | None:
    """Serialize a record modification timestamp, storing missing values as SQL NULL."""
    if value is None:
        return None
    serialized = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return serialized if serialized.strip() else None


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
    """Load study records through a local SQLite cache with incremental sync."""

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
            retry_attempts: Number of API retry attempts.
            lock_timeout_seconds: Number of seconds to wait when acquiring the cache file lock.
            lock: Optional FileLock instance for synchronizing write transactions.
        """
        self._sdk = sdk
        base_dir = DEFAULT_CACHE_DIR if cache_dir is None else Path(cache_dir).expanduser()
        self.db_path = base_dir / database_name
        self._retry_attempts = retry_attempts
        lock_path = Path(f"{self.db_path}.lock")
        self._lock = (
            lock if lock is not None else FileLock(str(lock_path), timeout=lock_timeout_seconds)
        )
        self._initialise_cache()

    @property
    def lock(self) -> FileLock:
        """Return the inter-process file lock protecting cache write operations."""
        return self._lock

    def load_records(self, study_key: str, *, reconcile: bool = True) -> list[Record]:
        """Synchronise the cache for ``study_key`` and return cached records."""
        self.sync_records(study_key, reconcile=reconcile)
        return self.get_cached_records(study_key)

    def sync_records(self, study_key: str, *, reconcile: bool = True) -> None:
        """Synchronise the cache for ``study_key`` without materialising cached rows."""
        conn = get_sqlite_connection(self.db_path)
        try:
            sync_state = self.get_study_sync_state(study_key, conn=conn)
            self._mark_study_syncing(conn, study_key)
            cycle_timestamps: list[str | None] = [sync_state.high_water_mark]

            try:
                should_fetch_delta = not (
                    sync_state.high_water_mark is None
                    and sync_state.initial_sync_complete
                    and (reconcile or self.count_cached_records(study_key, conn=conn) > 0)
                )

                delta_records: list[Record] = []
                if should_fetch_delta:
                    delta_records = self._fetch_delta_records(study_key, sync_state.high_water_mark)
                    cycle_timestamps.extend(
                        _serialize_date_modified(record.date_modified) for record in delta_records
                    )
                    self._upsert_records(conn, delta_records)

                if reconcile:
                    pre_reconcile_epoch = self._get_sync_epoch(conn, study_key)
                    active_record_ids = self._fetch_active_record_ids(study_key)
                    missing_ids = active_record_ids - self._get_local_record_ids(conn, study_key)
                    if missing_ids:
                        missing_records = self._fetch_missing_records(study_key, missing_ids)
                        cycle_timestamps.extend(
                            _serialize_date_modified(record.date_modified)
                            for record in missing_records
                        )
                        self._upsert_records(conn, missing_records)
                    self.reconcile_cache(
                        conn,
                        study_key,
                        active_record_ids,
                        max_epoch=pre_reconcile_epoch,
                    )

                self._complete_study_sync(conn, study_key, cycle_timestamps)
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
                WHERE study_key = ?
                """,
                (study_key,),
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
                WHERE study_key = ?
                """,
                (study_key,),
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
    ) -> Iterator[Record]:
        """Yield cached records for ``study_key`` in bounded chunks."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")

        close_conn = False
        if conn is None:
            conn = get_sqlite_connection(self.db_path)
            close_conn = True
        try:
            cursor = conn.execute(
                """
                SELECT payload
                FROM record_cache
                WHERE study_key = ?
                ORDER BY record_id
                """,
                (study_key,),
            )
            while True:
                rows = cursor.fetchmany(chunk_size)
                if not rows:
                    break
                for row in rows:
                    yield Record.from_json(json.loads(cast(str, row["payload"])))
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
        with self._lock, conn:
            effective_epoch = (
                max_epoch if max_epoch is not None else self._get_sync_epoch(conn, study_key)
            )
            local_rows = conn.execute(
                """
                SELECT record_id
                FROM record_cache
                WHERE study_key = ? AND upserted_epoch <= ?
                """,
                (study_key, effective_epoch),
            ).fetchall()
            local_ids = {cast(int, row["record_id"]) for row in local_rows}
            orphaned_ids = local_ids - active_record_ids
            if orphaned_ids:
                conn.executemany(
                    """
                    DELETE FROM record_cache
                    WHERE study_key = ? AND record_id = ? AND upserted_epoch <= ?
                    """,
                    [(study_key, orphaned_id, effective_epoch) for orphaned_id in orphaned_ids],
                )

    def _initialise_cache(self) -> None:
        """Ensure the cache database and tables are created and migrated in place."""
        resolved = Path(self.db_path).expanduser().resolve()
        with _get_db_init_lock(resolved):
            conn = get_sqlite_connection(self.db_path)
            try:
                with self._lock, conn:
                    table_info = conn.execute("PRAGMA table_info(record_cache)").fetchall()
                    if not table_info:
                        conn.execute(
                            """
                            CREATE TABLE IF NOT EXISTS record_cache (
                                study_key TEXT NOT NULL,
                                record_id INTEGER NOT NULL,
                                form_key TEXT NOT NULL,
                                date_modified TEXT,
                                payload TEXT NOT NULL,
                                upserted_epoch INTEGER NOT NULL DEFAULT 0,
                                PRIMARY KEY (study_key, record_id)
                            )
                            """
                        )
                    else:
                        columns = {cast(str, row["name"]): row for row in table_info}
                        date_mod_col = columns.get("date_modified")
                        needs_rebuild = "upserted_epoch" not in columns or (
                            date_mod_col is not None and int(date_mod_col["notnull"]) != 0
                        )
                        if needs_rebuild:
                            conn.execute(
                                """
                                CREATE TABLE record_cache_migrated (
                                    study_key TEXT NOT NULL,
                                    record_id INTEGER NOT NULL,
                                    form_key TEXT NOT NULL,
                                    date_modified TEXT,
                                    payload TEXT NOT NULL,
                                    upserted_epoch INTEGER NOT NULL DEFAULT 0,
                                    PRIMARY KEY (study_key, record_id)
                                )
                                """
                            )
                            if "upserted_epoch" in columns:
                                conn.execute(
                                    """
                                    INSERT INTO record_cache_migrated (
                                        study_key,
                                        record_id,
                                        form_key,
                                        date_modified,
                                        payload,
                                        upserted_epoch
                                    )
                                    SELECT
                                        study_key,
                                        record_id,
                                        form_key,
                                        NULLIF(date_modified, ''),
                                        payload,
                                        COALESCE(upserted_epoch, 1)
                                    FROM record_cache
                                    """
                                )
                            else:
                                conn.execute(
                                    """
                                    INSERT INTO record_cache_migrated (
                                        study_key,
                                        record_id,
                                        form_key,
                                        date_modified,
                                        payload,
                                        upserted_epoch
                                    )
                                    SELECT
                                        study_key,
                                        record_id,
                                        form_key,
                                        NULLIF(date_modified, ''),
                                        payload,
                                        1
                                    FROM record_cache
                                    """
                                )
                            conn.execute("DROP TABLE record_cache")
                            conn.execute("ALTER TABLE record_cache_migrated RENAME TO record_cache")

                    conn.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_record_cache_study_modified
                        ON record_cache (study_key, date_modified)
                        """
                    )
                    conn.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_record_cache_study_epoch
                        ON record_cache (study_key, upserted_epoch)
                        """
                    )
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS study_sync_state (
                            study_key TEXT PRIMARY KEY,
                            sync_epoch INTEGER NOT NULL DEFAULT 0,
                            high_water_mark TEXT,
                            initial_sync_complete INTEGER NOT NULL DEFAULT 0,
                            status TEXT NOT NULL DEFAULT 'idle',
                            last_synced_at TEXT,
                            error_message TEXT
                        )
                        """
                    )

                    legacy_studies = conn.execute(
                        """
                        SELECT DISTINCT r.study_key
                        FROM record_cache AS r
                        LEFT JOIN study_sync_state AS s ON s.study_key = r.study_key
                        WHERE s.study_key IS NULL
                        """
                    ).fetchall()
                    for study_row in legacy_studies:
                        legacy_study_key = cast(str, study_row["study_key"])
                        epoch_row = conn.execute(
                            "SELECT COALESCE(MAX(upserted_epoch), 1) FROM record_cache WHERE study_key = ?",
                            (legacy_study_key,),
                        ).fetchone()
                        max_epoch = int(epoch_row[0]) if epoch_row is not None else 1
                        ts_rows = conn.execute(
                            "SELECT date_modified FROM record_cache WHERE study_key = ? AND date_modified IS NOT NULL",
                            (legacy_study_key,),
                        ).fetchall()
                        hwm = _max_timestamp(
                            cast(str | None, ts_row["date_modified"]) for ts_row in ts_rows
                        )
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO study_sync_state (
                                study_key,
                                sync_epoch,
                                high_water_mark,
                                initial_sync_complete,
                                status
                            )
                            VALUES (?, ?, ?, 1, ?)
                            """,
                            (legacy_study_key, max_epoch, hwm, StudySyncStatus.SYNCED.value),
                        )
            finally:
                conn.close()

    def _get_sync_epoch(self, conn: sqlite3.Connection, study_key: str) -> int:
        """Return the current monotonic sync epoch for ``study_key``."""
        row = conn.execute(
            "SELECT sync_epoch FROM study_sync_state WHERE study_key = ?",
            (study_key,),
        ).fetchone()
        if row is None:
            return 0
        return int(row["sync_epoch"])

    def _get_local_record_ids(self, conn: sqlite3.Connection, study_key: str) -> set[int]:
        """Return all locally cached record IDs for ``study_key``."""
        rows = conn.execute(
            "SELECT record_id FROM record_cache WHERE study_key = ?",
            (study_key,),
        ).fetchall()
        return {cast(int, row["record_id"]) for row in rows}

    def _mark_study_syncing(self, conn: sqlite3.Connection, study_key: str) -> None:
        """Mark ``study_key`` as actively syncing without modifying its watermark."""
        with self._lock, conn:
            conn.execute(
                """
                INSERT INTO study_sync_state (study_key, sync_epoch, status)
                VALUES (?, 0, ?)
                ON CONFLICT(study_key) DO UPDATE SET
                    status = excluded.status
                """,
                (study_key, StudySyncStatus.SYNCING.value),
            )

    def _complete_study_sync(
        self,
        conn: sqlite3.Connection,
        study_key: str,
        candidate_timestamps: Iterable[str | None],
    ) -> None:
        """Advance ``high_water_mark`` monotonically and record sync completion under lock."""
        now_iso = datetime.now(UTC).isoformat()
        candidates = list(candidate_timestamps)
        with self._lock, conn:
            current_hwm = self._get_high_water_mark(conn, study_key)
            next_hwm = _max_timestamp([current_hwm, *candidates])
            conn.execute(
                """
                INSERT INTO study_sync_state (
                    study_key,
                    sync_epoch,
                    high_water_mark,
                    initial_sync_complete,
                    status,
                    last_synced_at,
                    error_message
                )
                VALUES (?, 0, ?, 1, ?, ?, NULL)
                ON CONFLICT(study_key) DO UPDATE SET
                    high_water_mark = excluded.high_water_mark,
                    initial_sync_complete = 1,
                    status = excluded.status,
                    last_synced_at = excluded.last_synced_at,
                    error_message = NULL
                """,
                (study_key, next_hwm, StudySyncStatus.SYNCED.value, now_iso),
            )

    def _fail_study_sync(self, conn: sqlite3.Connection, study_key: str, exc: Exception) -> None:
        """Record sync failure metadata while preserving the existing ``high_water_mark``."""
        safe_error = redact_sensitive_text(str(exc)) or type(exc).__name__
        with self._lock, conn:
            conn.execute(
                """
                INSERT INTO study_sync_state (study_key, sync_epoch, status, error_message)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(study_key) DO UPDATE SET
                    status = excluded.status,
                    error_message = excluded.error_message
                """,
                (study_key, StudySyncStatus.FAILED.value, safe_error),
            )

    def _get_high_water_mark(self, conn: sqlite3.Connection, study_key: str) -> str | None:
        """Get the stored high-water mark timestamp from ``study_sync_state``."""
        row = conn.execute(
            "SELECT high_water_mark FROM study_sync_state WHERE study_key = ?",
            (study_key,),
        ).fetchone()
        if row is None:
            return None
        value = cast(str | None, row["high_water_mark"])
        return value if value is not None and value.strip() else None

    def _fetch_delta_records(self, study_key: str, high_water_mark: str | None) -> list[Record]:
        """Fetch records from the API that have been modified since the high water mark."""
        if not high_water_mark:
            return self._list_records(study_key=study_key, record_data_filter=None)

        # Use >= to avoid missing updates that share the high-water-mark timestamp.
        # _upsert_records keeps refresh idempotent by deduplicating on (study_key, record_id).
        delta_filter = build_filter_string({"date_modified": (">=", high_water_mark)})
        return self._list_records_with_filter_override(
            study_key=study_key,
            filter_string=delta_filter,
        )

    def _fetch_active_record_ids(self, study_key: str) -> set[int]:
        """Fetch the set of all non-deleted record IDs for a study from the API using lightweight projection."""
        records = self._list_records(study_key=study_key, record_data_filter="none", deleted=False)
        return {record.record_id for record in records if record.record_id is not None}

    def _fetch_missing_records(self, study_key: str, missing_ids: set[int]) -> list[Record]:
        """Fetch full record payloads for upstream active IDs missing from the local cache."""
        if not missing_ids:
            return []
        sorted_ids = sorted(missing_ids)
        fetched: list[Record] = []
        for idx in range(0, len(sorted_ids), _MISSING_ID_BATCH_SIZE):
            batch_ids = sorted_ids[idx : idx + _MISSING_ID_BATCH_SIZE]
            batch_set = set(batch_ids)
            records = self._list_records(
                study_key=study_key,
                record_id=batch_ids,
                record_data_filter=None,
            )
            fetched.extend(record for record in records if record.record_id in batch_set)
        return fetched

    def _list_records(self, **filters: Any) -> list[Record]:
        """Call the SDK records list endpoint with retry logic."""
        retryer = Retrying(
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        return retryer(self._sdk.get_records, **filters)

    def _list_records_with_filter_override(
        self, *, study_key: str, filter_string: str
    ) -> list[Record]:
        """List records using an explicit raw ``filter`` query parameter.

        This bypasses automatic filter construction so incremental sync can
        send only the timestamp predicate (without ``studyKey``).
        """
        retryer = Retrying(
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        endpoint = getattr(self._sdk, "records")  # noqa: B009

        def _fetch_delta() -> list[Record]:
            return list(
                endpoint.list(
                    study_key=study_key,
                    filter=filter_string,
                    record_data_filter=None,
                )
            )

        return retryer(_fetch_delta)

    def _upsert_records(self, conn: sqlite3.Connection, records: Iterable[Record]) -> None:
        """Insert or update records in the local SQLite cache stamped with a new sync epoch."""
        prepared = [
            (
                cast(str, record.study_key),
                cast(int, record.record_id),
                cast(str, record.form_key or ""),
                _serialize_date_modified(record.date_modified),
                json.dumps(record.model_dump(mode="json", by_alias=True), sort_keys=True),
            )
            for record in records
            if record.study_key is not None and record.record_id is not None
        ]
        if not prepared:
            return

        distinct_studies = list(dict.fromkeys(item[0] for item in prepared))
        with self._lock, conn:
            epochs_by_study: dict[str, int] = {}
            for study_key in distinct_studies:
                conn.execute(
                    """
                    INSERT INTO study_sync_state (study_key, sync_epoch, status)
                    VALUES (?, 1, ?)
                    ON CONFLICT(study_key) DO UPDATE SET
                        sync_epoch = study_sync_state.sync_epoch + 1
                    """,
                    (study_key, StudySyncStatus.IDLE.value),
                )
                epochs_by_study[study_key] = self._get_sync_epoch(conn, study_key)

            payloads = [
                (
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
                    study_key,
                    record_id,
                    form_key,
                    date_modified,
                    payload,
                    upserted_epoch
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(study_key, record_id) DO UPDATE SET
                    form_key = excluded.form_key,
                    date_modified = excluded.date_modified,
                    payload = excluded.payload,
                    upserted_epoch = excluded.upserted_epoch
                """,
                payloads,
            )
