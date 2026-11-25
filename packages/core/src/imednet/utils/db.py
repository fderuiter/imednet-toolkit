"""Centralized database connection utilities."""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_db_init_locks: dict[str, threading.RLock] = {}
_db_init_locks_guard = threading.RLock()


def _get_db_init_lock(resolved_path: Path) -> threading.RLock:
    """Return a thread-safe lock for initializing a specific database file."""
    key = str(Path(resolved_path).expanduser().resolve())
    with _db_init_locks_guard:
        return _db_init_locks.setdefault(key, threading.RLock())


def get_db_init_lock(db_path: str | Path) -> threading.RLock:
    """Return the reentrant initialization lock for ``db_path``."""
    return _get_db_init_lock(Path(db_path))


def get_sqlite_connection(
    db_path: str | Path,
    timeout: float = 30.0,
    busy_timeout_ms: int = 30000,
    foreign_keys: bool = False,
) -> sqlite3.Connection:
    """Return a SQLite connection configured for concurrent access."""
    resolved_path = Path(db_path).expanduser()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    lock = _get_db_init_lock(resolved_path)
    with lock:
        try:
            conn = sqlite3.connect(resolved_path, timeout=timeout)
        except sqlite3.Error as exc:
            raise sqlite3.OperationalError(f"Unable to open sqlite database: {exc}") from exc

        conn.row_factory = sqlite3.Row
        if busy_timeout_ms:
            conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms};")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON;")
        return conn


@contextmanager
def sqlite_connection(
    db_path: str | Path,
    timeout: float = 30.0,
    busy_timeout_ms: int = 30000,
    foreign_keys: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Context manager yielding a safely configured SQLite connection."""
    conn = get_sqlite_connection(
        db_path,
        timeout=timeout,
        busy_timeout_ms=busy_timeout_ms,
        foreign_keys=foreign_keys,
    )
    try:
        yield conn
    finally:
        conn.close()
