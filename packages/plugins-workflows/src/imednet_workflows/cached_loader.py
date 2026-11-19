"""Workflow for loading study records with local SQLite caching for performance."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from filelock import FileLock
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from imednet.spi.models import Record
from imednet.spi.utils import build_filter_string

from .chunked_pipeline import DEFAULT_CHUNK_SIZE

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

DEFAULT_CACHE_DIR = Path.home() / ".imednet" / "cache"

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
            high_water_mark = self._get_high_water_mark(conn, study_key)
            delta_records = self._fetch_delta_records(study_key, high_water_mark)
            self._upsert_records(conn, delta_records)
            if reconcile:
                active_record_ids = self._fetch_active_record_ids(study_key)
                self.reconcile_cache(conn, study_key, active_record_ids)
        finally:
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
        self, conn: sqlite3.Connection, study_key: str, active_record_ids: set[int]
    ) -> None:
        """Prune records removed from the upstream EDC backend."""
        local_rows = conn.execute(
            "SELECT record_id FROM record_cache WHERE study_key = ?",
            (study_key,),
        ).fetchall()
        local_ids = {cast(int, row["record_id"]) for row in local_rows}
        orphaned_ids = local_ids - active_record_ids
        if orphaned_ids:
            with self._lock, conn:
                conn.executemany(
                    "DELETE FROM record_cache WHERE study_key = ? AND record_id = ?",
                    [(study_key, orphaned_id) for orphaned_id in orphaned_ids],
                )

    def _initialise_cache(self) -> None:
        """Ensure the cache database and tables are created."""
        resolved = Path(self.db_path).expanduser().resolve()
        with _get_db_init_lock(resolved):
            conn = get_sqlite_connection(self.db_path)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS record_cache (
                        study_key TEXT NOT NULL,
                        record_id INTEGER NOT NULL,
                        form_key TEXT NOT NULL,
                        date_modified TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY (study_key, record_id)
                    )
                    """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_record_cache_study_modified
                    ON record_cache (study_key, date_modified)
                    """)
                conn.commit()
            finally:
                conn.close()

    def _get_high_water_mark(self, conn: sqlite3.Connection, study_key: str) -> str | None:
        """Get the latest modification timestamp from the local cache for a study."""
        row = conn.execute(
            "SELECT MAX(date_modified) AS max_date_modified FROM record_cache WHERE study_key = ?",
            (study_key,),
        ).fetchone()
        if row is None:
            return None
        return cast(str | None, row["max_date_modified"])

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
        """List records using an explicit top-level ``filter`` query parameter."""
        return self._list_records(
            study_key=study_key,
            filter=filter_string,
            record_data_filter=None,
        )

    def _upsert_records(self, conn: sqlite3.Connection, records: Iterable[Record]) -> None:
        """Insert or update records in the local SQLite cache."""
        payloads = [
            (
                record.study_key,
                record.record_id,
                record.form_key,
                (
                    record.date_modified.isoformat()
                    if record.date_modified is not None
                    and hasattr(record.date_modified, "isoformat")
                    else (str(record.date_modified) if record.date_modified is not None else "")
                ),
                json.dumps(record.model_dump(mode="json", by_alias=True), sort_keys=True),
            )
            for record in records
        ]
        if not payloads:
            return

        with self._lock, conn:
            conn.executemany(
                """
                INSERT INTO record_cache (study_key, record_id, form_key, date_modified, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(study_key, record_id) DO UPDATE SET
                    form_key = excluded.form_key,
                    date_modified = excluded.date_modified,
                    payload = excluded.payload
                """,
                payloads,
            )
