"""SQLite-backed local ledger for persisting study background jobs across sessions."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import Field

from imednet.models.base import ImednetBaseModel
from imednet.models.jobs import (
    ACTIVE_JOB_STATES,
    TERMINAL_JOB_STATES,
    InvalidJobStateError,
    JobState,
    normalize_job_state,
)
from imednet.utils.db import sqlite_connection

logger = logging.getLogger(__name__)

_SQLITE_BUSY_TIMEOUT_MS = 30_000
_RETRY_BASE_DELAY_SECONDS = 0.05
_initialized_ledger_paths: set[str] = set()
_initialized_paths_lock = RLock()


def get_default_job_ledger_path() -> Path:
    """Resolve the default SQLite path for the shared Job ledger.

    Checks ``IMEDNET_JOB_LEDGER_PATH`` first, falling back to
    ``~/.imednet/job_ledger.db``.
    """
    env_path = os.environ.get("IMEDNET_JOB_LEDGER_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".imednet" / "job_ledger.db"


class JobLedgerEntry(ImednetBaseModel):
    """Persisted record of an active or recent study background job."""

    study_key: str = Field(..., alias="studyKey")
    batch_id: str = Field(..., alias="batchId")
    job_id: str | None = Field(default=None, alias="jobId")
    job_type: str = Field(default="Job", alias="jobType")
    state: str = Field(default=JobState.PENDING.value, alias="state")
    progress: int = Field(default=0, alias="progress")
    date_created: str | None = Field(default=None, alias="dateCreated")
    date_started: str | None = Field(default=None, alias="dateStarted")
    date_finished: str | None = Field(default=None, alias="dateFinished")
    source: str | None = Field(default=None, alias="source")
    recorded_at: str | None = Field(default=None, alias="recordedAt")
    updated_at: str | None = Field(default=None, alias="updatedAt")

    @property
    def is_active(self) -> bool:
        """Return True when the persisted job state is non-terminal."""
        try:
            return normalize_job_state(self.state) in ACTIVE_JOB_STATES
        except InvalidJobStateError:
            return False

    @property
    def is_terminal(self) -> bool:
        """Return True when the persisted job state is terminal."""
        try:
            return normalize_job_state(self.state) in TERMINAL_JOB_STATES
        except InvalidJobStateError:
            return True


def _coerce_state_str(raw_state: Any, default: str = JobState.PENDING.value) -> str:
    """Normalize a job state enum or string for storage."""
    if raw_state is None:
        return default
    raw_text = str(raw_state.value if hasattr(raw_state, "value") else raw_state).strip()
    if not raw_text:
        return default
    try:
        return normalize_job_state(raw_text).value
    except InvalidJobStateError:
        return raw_text.upper()


def _coerce_progress_int(raw_progress: Any, state_str: str, fallback: int = 0) -> int:
    """Normalize progress percentage into 0..100 integer range."""
    if raw_progress is not None:
        try:
            return max(0, min(100, int(raw_progress)))
        except (TypeError, ValueError):
            pass
    if state_str in {JobState.COMPLETED.value, JobState.COMPLETE.value, JobState.SUCCESS.value}:
        return max(fallback, 100)
    return max(0, min(100, fallback))


class JobLedger:
    """Thread-safe SQLite store tracking active and recent Jobs per Study."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        timeout: float = 30.0,
        retry_attempts: int = 3,
    ) -> None:
        """Initialize the SQLite Job ledger.

        Args:
            db_path: Optional explicit SQLite database path. When omitted, resolves
                dynamically via :func:`get_default_job_ledger_path`.
            timeout: Connection timeout in seconds.
            retry_attempts: Maximum write retry attempts on SQLite busy errors.
        """
        self._explicit_db_path = Path(db_path).expanduser() if db_path is not None else None
        self._timeout = timeout
        self._retry_attempts = retry_attempts
        self._lock = RLock()

    @property
    def db_path(self) -> Path:
        """Active SQLite database file path."""
        if self._explicit_db_path is not None:
            return self._explicit_db_path
        return get_default_job_ledger_path()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield an initialized WAL-enabled SQLite connection for writes or schema setup."""
        target = self.db_path
        existed_before = target.is_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_key = str(target.resolve())
        with sqlite_connection(
            target,
            timeout=self._timeout,
            busy_timeout_ms=_SQLITE_BUSY_TIMEOUT_MS,
            foreign_keys=False,
        ) as conn:
            with _initialized_paths_lock:
                needs_init = not existed_before or resolved_key not in _initialized_ledger_paths
                if needs_init:
                    self._ensure_schema(conn)
                    _initialized_ledger_paths.add(resolved_key)
            yield conn

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection | None]:
        """Yield a lightweight read connection without WAL/DDL writes when already initialized."""
        target = self.db_path
        if not target.is_file():
            yield None
            return
        resolved_key = str(target.resolve())
        with _initialized_paths_lock:
            is_initialized = resolved_key in _initialized_ledger_paths
        if not is_initialized:
            with self._connection() as conn:
                yield conn
            return
        conn = sqlite3.connect(target, timeout=self._timeout)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS};")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        """Create job_ledger table and indexes if missing."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_ledger (
                study_key TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                job_id TEXT,
                job_type TEXT NOT NULL DEFAULT 'Job',
                state TEXT NOT NULL DEFAULT 'PENDING',
                progress INTEGER NOT NULL DEFAULT 0,
                date_created TEXT,
                date_started TEXT,
                date_finished TEXT,
                source TEXT,
                recorded_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (study_key, batch_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_ledger_study_updated
            ON job_ledger (study_key, updated_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_ledger_study_state
            ON job_ledger (study_key, state)
            """
        )
        conn.commit()

    def _execute_write(self, callback: Callable[[sqlite3.Connection], None]) -> None:
        """Run a transactional write with bounded retries."""
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(self._retry_attempts):
            try:
                with self._lock, self._connection() as conn, conn:
                    callback(conn)
                return
            except sqlite3.OperationalError as exc:
                last_error = exc
                if attempt < self._retry_attempts - 1:
                    time.sleep(_RETRY_BASE_DELAY_SECONDS * (attempt + 1))
        if last_error is not None:
            raise last_error

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> JobLedgerEntry:
        """Hydrate a JobLedgerEntry from a SQLite row."""
        return JobLedgerEntry(
            studyKey=str(row["study_key"]),
            batchId=str(row["batch_id"]),
            jobId=str(row["job_id"]) if row["job_id"] else None,
            jobType=str(row["job_type"] or "Job"),
            state=str(row["state"] or JobState.PENDING.value),
            progress=int(row["progress"] or 0),
            dateCreated=str(row["date_created"]) if row["date_created"] else None,
            dateStarted=str(row["date_started"]) if row["date_started"] else None,
            dateFinished=str(row["date_finished"]) if row["date_finished"] else None,
            source=str(row["source"]) if row["source"] else None,
            recordedAt=str(row["recorded_at"]) if row["recorded_at"] else None,
            updatedAt=str(row["updated_at"]) if row["updated_at"] else None,
        )

    def record_job(
        self,
        study_key: str,
        batch_id: str,
        *,
        job_id: str | None = None,
        job_type: str | None = None,
        state: JobState | str | None = None,
        progress: int | None = None,
        date_created: str | None = None,
        date_started: str | None = None,
        date_finished: str | None = None,
        source: str | None = None,
    ) -> JobLedgerEntry:
        """Insert or update a job record in the ledger for a study."""
        cleaned_study = str(study_key).strip()
        cleaned_batch = str(batch_id).strip()
        if not cleaned_study or not cleaned_batch:
            raise ValueError("Both study_key and batch_id must be non-empty.")

        now = datetime.now(UTC).isoformat()

        def _write(conn: sqlite3.Connection) -> None:
            existing = conn.execute(
                """
                SELECT job_id, job_type, state, progress, date_created,
                       date_started, date_finished, source, recorded_at
                FROM job_ledger
                WHERE study_key = ? AND batch_id = ?
                """,
                (cleaned_study, cleaned_batch),
            ).fetchone()

            prev_state = str(existing["state"]) if existing else JobState.PENDING.value
            prev_progress = int(existing["progress"] or 0) if existing else 0
            resolved_state = _coerce_state_str(state, default=prev_state)
            resolved_progress = _coerce_progress_int(
                progress, resolved_state, fallback=prev_progress
            )
            resolved_job_id = (
                str(job_id)
                if job_id
                else (str(existing["job_id"]) if existing and existing["job_id"] else None)
            )
            resolved_type = (
                str(job_type)
                if job_type
                else (str(existing["job_type"]) if existing and existing["job_type"] else "Job")
            )
            resolved_created = (
                str(date_created)
                if date_created
                else (
                    str(existing["date_created"]) if existing and existing["date_created"] else None
                )
            )
            resolved_started = (
                str(date_started)
                if date_started
                else (
                    str(existing["date_started"]) if existing and existing["date_started"] else None
                )
            )
            resolved_finished = (
                str(date_finished)
                if date_finished
                else (
                    str(existing["date_finished"])
                    if existing and existing["date_finished"]
                    else None
                )
            )
            resolved_source = (
                str(source)
                if source
                else (str(existing["source"]) if existing and existing["source"] else None)
            )
            recorded_at = str(existing["recorded_at"]) if existing else now

            conn.execute(
                """
                INSERT INTO job_ledger (
                    study_key, batch_id, job_id, job_type, state, progress,
                    date_created, date_started, date_finished, source, recorded_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(study_key, batch_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    job_type = excluded.job_type,
                    state = excluded.state,
                    progress = excluded.progress,
                    date_created = excluded.date_created,
                    date_started = excluded.date_started,
                    date_finished = excluded.date_finished,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    cleaned_study,
                    cleaned_batch,
                    resolved_job_id,
                    resolved_type,
                    resolved_state,
                    resolved_progress,
                    resolved_created,
                    resolved_started,
                    resolved_finished,
                    resolved_source,
                    recorded_at,
                    now,
                ),
            )

        self._execute_write(_write)
        entry = self.get_job(cleaned_study, cleaned_batch)
        assert entry is not None
        return entry

    def record_from_model(
        self,
        study_key: str,
        job: Any,
        *,
        fallback_batch_id: str | None = None,
        job_type: str | None = None,
        source: str | None = None,
    ) -> JobLedgerEntry | None:
        """Extract job attributes from a Job/JobStatus model or dict and persist them."""
        if not study_key or job is None:
            return None

        if isinstance(job, dict):
            batch_id = job.get("batch_id") or job.get("batchId") or fallback_batch_id
            job_id = job.get("job_id") or job.get("jobId")
            state = job.get("state") or job.get("status") or job.get("jobStatus")
            progress = job.get("progress")
            date_created = job.get("date_created") or job.get("dateCreated")
            date_started = job.get("date_started") or job.get("dateStarted")
            date_finished = job.get("date_finished") or job.get("dateFinished")
            model_type = job.get("job_type") or job.get("jobType")
        else:
            batch_id = (
                getattr(job, "batch_id", None) or getattr(job, "batchId", None) or fallback_batch_id
            )
            job_id = getattr(job, "job_id", None) or getattr(job, "jobId", None)
            state = (
                getattr(job, "state", None)
                or getattr(job, "status", None)
                or getattr(job, "job_status", None)
                or getattr(job, "jobStatus", None)
            )
            progress = getattr(job, "progress", None)
            date_created = getattr(job, "date_created", None) or getattr(job, "dateCreated", None)
            date_started = getattr(job, "date_started", None) or getattr(job, "dateStarted", None)
            date_finished = getattr(job, "date_finished", None) or getattr(
                job, "dateFinished", None
            )
            model_type = getattr(job, "job_type", None) or getattr(job, "jobType", None)

        if not batch_id or not str(batch_id).strip():
            return None

        return self.record_job(
            study_key=study_key,
            batch_id=str(batch_id).strip(),
            job_id=str(job_id) if job_id else None,
            job_type=job_type or (str(model_type) if model_type else None),
            state=state,
            progress=progress,
            date_created=str(date_created) if date_created else None,
            date_started=str(date_started) if date_started else None,
            date_finished=str(date_finished) if date_finished else None,
            source=source,
        )

    def get_job(self, study_key: str, batch_id: str) -> JobLedgerEntry | None:
        """Retrieve a single persisted job entry for a study."""
        with self._read_connection() as conn:
            if conn is None:
                return None
            row = conn.execute(
                """
                SELECT study_key, batch_id, job_id, job_type, state, progress,
                       date_created, date_started, date_finished, source, recorded_at, updated_at
                FROM job_ledger
                WHERE study_key = ? AND batch_id = ?
                """,
                (study_key, batch_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    def list_jobs(
        self,
        study_key: str,
        *,
        active_only: bool = False,
        limit: int = 50,
    ) -> list[JobLedgerEntry]:
        """List persisted jobs for a study ordered by most recently updated."""
        if not study_key:
            return []
        active_json = json.dumps(sorted(st.value for st in ACTIVE_JOB_STATES))
        with self._read_connection() as conn:
            if conn is None:
                return []
            if active_only:
                rows = conn.execute(
                    """
                    SELECT study_key, batch_id, job_id, job_type, state, progress,
                           date_created, date_started, date_finished, source, recorded_at, updated_at
                    FROM job_ledger
                    WHERE study_key = ?
                      AND UPPER(TRIM(state)) IN (SELECT value FROM json_each(?))
                    ORDER BY updated_at DESC, recorded_at DESC
                    LIMIT ?
                    """,
                    (study_key, active_json, max(1, limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT study_key, batch_id, job_id, job_type, state, progress,
                           date_created, date_started, date_finished, source, recorded_at, updated_at
                    FROM job_ledger
                    WHERE study_key = ?
                    ORDER BY updated_at DESC, recorded_at DESC
                    LIMIT ?
                    """,
                    (study_key, max(1, limit)),
                ).fetchall()
        entries = [self._row_to_entry(row) for row in rows]
        if active_only:
            return [entry for entry in entries if entry.is_active]
        return entries

    def list_active_jobs(self, study_key: str) -> list[JobLedgerEntry]:
        """Return all currently active (non-terminal) jobs for a study."""
        return self.list_jobs(study_key, active_only=True, limit=200)

    def remove_job(self, study_key: str, batch_id: str) -> bool:
        """Delete a single job from the ledger."""
        deleted = False

        def _write(conn: sqlite3.Connection) -> None:
            nonlocal deleted
            cursor = conn.execute(
                "DELETE FROM job_ledger WHERE study_key = ? AND batch_id = ?",
                (study_key, batch_id),
            )
            deleted = cursor.rowcount > 0

        self._execute_write(_write)
        return deleted

    def clear_study(self, study_key: str) -> int:
        """Delete all ledger entries for a study and return the removed count."""
        removed = 0

        def _write(conn: sqlite3.Connection) -> None:
            nonlocal removed
            cursor = conn.execute("DELETE FROM job_ledger WHERE study_key = ?", (study_key,))
            removed = cursor.rowcount

        self._execute_write(_write)
        return removed


_default_ledger = JobLedger()


def record_study_job(
    study_key: str | None,
    job: Any,
    *,
    fallback_batch_id: str | None = None,
    job_type: str | None = None,
    source: str | None = None,
    only_if_tracked: bool = False,
) -> JobLedgerEntry | None:
    """Safely record or update a study job in the default SQLite ledger.

    Never raises exceptions into SDK callers if the local filesystem is read-only.
    """
    if not study_key or job is None:
        return None
    with contextlib.suppress(Exception):
        if only_if_tracked:
            explicit_batch = (
                (job.get("batch_id") or job.get("batchId"))
                if isinstance(job, dict)
                else (getattr(job, "batch_id", None) or getattr(job, "batchId", None))
            )
            if not explicit_batch and (
                not fallback_batch_id
                or _default_ledger.get_job(study_key, fallback_batch_id) is None
            ):
                return None
        return _default_ledger.record_from_model(
            study_key,
            job,
            fallback_batch_id=fallback_batch_id,
            job_type=job_type,
            source=source,
        )
    return None


__all__ = [
    "JobLedger",
    "JobLedgerEntry",
    "get_default_job_ledger_path",
    "record_study_job",
]
