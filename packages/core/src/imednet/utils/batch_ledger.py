"""SQLite-backed resumable batch execution ledger storing zero PHI or clinical data."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from imednet.errors import ValidationError
from imednet.models.jobs import Job, JobStatus
from imednet.utils.db import sqlite_connection

DEFAULT_LEDGER_ENV_VAR = "IMEDNET_BATCH_LEDGER_PATH"
DEFAULT_RELATIVE_LEDGER_PATH = Path(".imednet") / "batch_ledger.sqlite3"

_LEDGER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS batch_runs (
    study_key TEXT PRIMARY KEY,
    chunk_size INTEGER NOT NULL,
    total_chunks INTEGER NOT NULL,
    sequence_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_chunks (
    study_key TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_sha256 TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    job_id TEXT NOT NULL DEFAULT '',
    job_state TEXT NOT NULL,
    failed_only INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (study_key, chunk_index)
);
"""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _plain_state_name(state: Any) -> str:
    if state is None:
        return "PROCESSING"
    raw = getattr(state, "value", state)
    text = str(raw).strip()
    return text or "PROCESSING"


def _is_retryable_failed_job(status: Job | JobStatus) -> bool:
    """Return True if a Job failed/cancelled completely with zero successful records."""
    state_str = _plain_state_name(getattr(status, "state", None)).upper()
    if state_str not in {"FAILED", "CANCELLED", "CANCELED"}:
        return False
    successful = getattr(status, "successful_records", None) or 0
    return int(successful) == 0


@dataclass(frozen=True)
class LedgerChunkRecord:
    """PHI-free persisted execution state for a single batch chunk."""

    study_key: str
    chunk_index: int
    chunk_sha256: str
    batch_id: str
    job_id: str
    job_state: str
    failed_only: bool = False

    def to_job(self) -> Job:
        """Reconstruct a :class:`Job` reference from persisted non-PHI metadata."""
        return Job(
            jobId=self.job_id,
            batchId=self.batch_id,
            state=self.job_state,
        )


def resolve_opt_in_ledger(
    *,
    ledger: BatchExecutionLedger | None = None,
    ledger_path: str | os.PathLike[str] | None = None,
    resume: bool = False,
) -> BatchExecutionLedger | None:
    """Return an active :class:`BatchExecutionLedger` only when explicitly opted in."""
    if ledger is not None:
        return ledger
    if ledger_path is not None:
        return BatchExecutionLedger(ledger_path)
    if resume:
        env_override = os.environ.get(DEFAULT_LEDGER_ENV_VAR, "").strip()
        target = Path(env_override) if env_override else (Path.cwd() / DEFAULT_RELATIVE_LEDGER_PATH)
        return BatchExecutionLedger(target)
    return None


class BatchExecutionLedger:
    """Persistent WAL SQLite ledger tracking accepted ``batchId`` chunks without clinical data.

    Tables store strictly ``study_key``, chunk indexes, SHA-256 digests, ``batchId`` / ``jobId``
    identifiers, and ``Job`` state strings. Zero clinical ``Variable`` names/values or Subject
    identifiers are ever written to SQLite.
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        """Initialize the ledger bound to *db_path* (lazily created on first write/prepare)."""
        self.db_path = Path(db_path)

    def _ensure_initialized(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite_connection(self.db_path) as conn:
            conn.executescript(_LEDGER_SCHEMA_SQL)
            conn.commit()

    def prepare_run(
        self,
        study_key: str,
        *,
        chunk_size: int,
        chunk_digests: Sequence[str],
        sequence_sha256: str,
        resume: bool = False,
    ) -> dict[int, LedgerChunkRecord]:
        """Initialize or validate a batch run and return reusable accepted chunks by index.

        When ``resume=True`` and a recorded run exists for ``study_key``, validates that
        ``chunk_size``, ``total_chunks``, ``sequence_sha256``, and per-chunk SHA-256 hashes
        match the recorded run, raising :class:`ValidationError` on any divergence.
        """
        self._ensure_initialized()
        now = _utc_now_iso()
        total_chunks = len(chunk_digests)

        with sqlite_connection(self.db_path) as conn:
            conn.executescript(_LEDGER_SCHEMA_SQL)
            if not resume:
                conn.execute("DELETE FROM batch_chunks WHERE study_key = ?", (study_key,))
                conn.execute(
                    """
                    INSERT INTO batch_runs (
                        study_key, chunk_size, total_chunks, sequence_sha256, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(study_key) DO UPDATE SET
                        chunk_size = excluded.chunk_size,
                        total_chunks = excluded.total_chunks,
                        sequence_sha256 = excluded.sequence_sha256,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at
                    """,
                    (study_key, chunk_size, total_chunks, sequence_sha256, now, now),
                )
                conn.commit()
                return {}

            row = conn.execute(
                """
                SELECT chunk_size, total_chunks, sequence_sha256
                FROM batch_runs
                WHERE study_key = ?
                """,
                (study_key,),
            ).fetchone()

            if row is None:
                conn.execute(
                    """
                    INSERT INTO batch_runs (
                        study_key, chunk_size, total_chunks, sequence_sha256, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (study_key, chunk_size, total_chunks, sequence_sha256, now, now),
                )
                conn.commit()
                return {}

            rec_chunk_size = int(row["chunk_size"])
            rec_total_chunks = int(row["total_chunks"])
            rec_seq_sha = str(row["sequence_sha256"])

            if rec_chunk_size != chunk_size:
                raise ValidationError(
                    f"Cannot resume batch for study '{study_key}': chunk_size changed "
                    f"(ledger recorded {rec_chunk_size}, got {chunk_size})."
                )
            if rec_total_chunks != total_chunks or rec_seq_sha != sequence_sha256:
                raise ValidationError(
                    f"Cannot resume batch for study '{study_key}': input payload SHA-256 digest "
                    "sequence changed from the recorded ledger run."
                )

            chunk_rows = conn.execute(
                """
                SELECT chunk_index, chunk_sha256, batch_id, job_id, job_state, failed_only
                FROM batch_chunks
                WHERE study_key = ?
                ORDER BY chunk_index ASC
                """,
                (study_key,),
            ).fetchall()

            reusable: dict[int, LedgerChunkRecord] = {}
            for crow in chunk_rows:
                idx = int(crow["chunk_index"])
                c_sha = str(crow["chunk_sha256"])
                if idx < 0 or idx >= total_chunks or c_sha != chunk_digests[idx]:
                    raise ValidationError(
                        f"Cannot resume batch for study '{study_key}': chunk {idx} SHA-256 digest "
                        "does not match the recorded ledger run."
                    )
                batch_id = str(crow["batch_id"])
                failed_only = bool(crow["failed_only"])
                if batch_id and not failed_only:
                    reusable[idx] = LedgerChunkRecord(
                        study_key=study_key,
                        chunk_index=idx,
                        chunk_sha256=c_sha,
                        batch_id=batch_id,
                        job_id=str(crow["job_id"]),
                        job_state=str(crow["job_state"]),
                        failed_only=False,
                    )

            conn.execute(
                "UPDATE batch_runs SET updated_at = ? WHERE study_key = ?",
                (now, study_key),
            )
            conn.commit()
            return reusable

    def record_chunk_accepted(
        self,
        study_key: str,
        *,
        chunk_index: int,
        chunk_sha256: str,
        job: Job,
    ) -> None:
        """Persist an accepted chunk's ``batchId`` and initial ``Job`` state immediately."""
        batch_id = str(getattr(job, "batch_id", "") or "").strip()
        if not batch_id:
            return
        self._ensure_initialized()
        now = _utc_now_iso()
        job_id = str(getattr(job, "job_id", "") or "").strip()
        job_state = _plain_state_name(getattr(job, "state", None))
        failed_only = int(_is_retryable_failed_job(job))

        with sqlite_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO batch_chunks (
                    study_key, chunk_index, chunk_sha256, batch_id, job_id, job_state,
                    failed_only, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(study_key, chunk_index) DO UPDATE SET
                    chunk_sha256 = excluded.chunk_sha256,
                    batch_id = excluded.batch_id,
                    job_id = excluded.job_id,
                    job_state = excluded.job_state,
                    failed_only = excluded.failed_only,
                    updated_at = excluded.updated_at
                """,
                (
                    study_key,
                    chunk_index,
                    chunk_sha256,
                    batch_id,
                    job_id,
                    job_state,
                    failed_only,
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE batch_runs SET updated_at = ? WHERE study_key = ?",
                (now, study_key),
            )
            conn.commit()

    def update_job_states(
        self,
        study_key: str,
        statuses_by_batch_id: Mapping[str, Job | JobStatus] | Sequence[Job | JobStatus],
    ) -> None:
        """Update persisted ``job_state`` values for settled ``batchId`` entries."""
        if not statuses_by_batch_id or not self.db_path.exists():
            return
        if isinstance(statuses_by_batch_id, Mapping):
            items = list(statuses_by_batch_id.items())
        else:
            items = [
                (str(getattr(st, "batch_id", "") or "").strip(), st) for st in statuses_by_batch_id
            ]
        now = _utc_now_iso()
        with sqlite_connection(self.db_path) as conn:
            for batch_id, status in items:
                if not batch_id:
                    continue
                state_str = _plain_state_name(getattr(status, "state", None))
                job_id = str(getattr(status, "job_id", "") or "").strip()
                failed_only = int(_is_retryable_failed_job(status))
                if job_id:
                    conn.execute(
                        """
                        UPDATE batch_chunks
                        SET job_state = ?, job_id = ?, failed_only = ?, updated_at = ?
                        WHERE study_key = ? AND batch_id = ?
                        """,
                        (state_str, job_id, failed_only, now, study_key, batch_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE batch_chunks
                        SET job_state = ?, failed_only = ?, updated_at = ?
                        WHERE study_key = ? AND batch_id = ?
                        """,
                        (state_str, failed_only, now, study_key, batch_id),
                    )
            conn.execute(
                "UPDATE batch_runs SET updated_at = ? WHERE study_key = ?",
                (now, study_key),
            )
            conn.commit()

    def has_run(self, study_key: str) -> bool:
        """Return True if a batch run header is recorded for ``study_key``."""
        if not self.db_path.exists():
            return False
        with sqlite_connection(self.db_path) as conn:
            conn.executescript(_LEDGER_SCHEMA_SQL)
            row = conn.execute(
                "SELECT 1 FROM batch_runs WHERE study_key = ?",
                (study_key,),
            ).fetchone()
            return row is not None

    def get_chunks(self, study_key: str) -> list[LedgerChunkRecord]:
        """Return all persisted chunk metadata rows for ``study_key`` ordered by chunk index."""
        if not self.db_path.exists():
            return []
        with sqlite_connection(self.db_path) as conn:
            conn.executescript(_LEDGER_SCHEMA_SQL)
            rows = conn.execute(
                """
                SELECT chunk_index, chunk_sha256, batch_id, job_id, job_state, failed_only
                FROM batch_chunks
                WHERE study_key = ?
                ORDER BY chunk_index ASC
                """,
                (study_key,),
            ).fetchall()
        return [
            LedgerChunkRecord(
                study_key=study_key,
                chunk_index=int(r["chunk_index"]),
                chunk_sha256=str(r["chunk_sha256"]),
                batch_id=str(r["batch_id"]),
                job_id=str(r["job_id"]),
                job_state=str(r["job_state"]),
                failed_only=bool(r["failed_only"]),
            )
            for r in rows
        ]


__all__ = [
    "DEFAULT_LEDGER_ENV_VAR",
    "DEFAULT_RELATIVE_LEDGER_PATH",
    "BatchExecutionLedger",
    "LedgerChunkRecord",
    "resolve_opt_in_ledger",
]
