"""Workflow for managing a triage queue of records using a local SQLite database."""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from imednet.spi.models import (
    TriageAnnotation,
    TriageHistoryEntry,
    TriageItem,
    TriageStatus,
    validate_transition,
)
from imednet.spi.utils import redact_sensitive_payload, redact_sensitive_text, sqlite_connection

_SAFE_SQL_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]+$")
_LATEST_SCHEMA_VERSION = 1
_SQLITE_BUSY_TIMEOUT_MS = 30_000
_RETRY_BASE_DELAY_SECONDS = 0.05


class TriageStore:
    """Thread-safe SQLite-backed triage queue and decision store."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        timeout: float = 30.0,
        retry_attempts: int = 3,
    ) -> None:
        """Initialize the triage store.

        Args:
            db_path: Path to the SQLite database file.
            timeout: Maximum time to wait for a database connection.
            retry_attempts: Number of write retry attempts.
        """
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._timeout = timeout
        self._retry_attempts = retry_attempts
        self._lock = RLock()
        self._initialize_schema()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager for a configured SQLite connection."""
        try:
            with sqlite_connection(
                self.db_path,
                timeout=self._timeout,
                busy_timeout_ms=_SQLITE_BUSY_TIMEOUT_MS,
                foreign_keys=True,
            ) as conn:
                yield conn
        except sqlite3.Error as exc:
            raise sqlite3.OperationalError(
                f"Unable to open triage store at {self._redact_sqlite_target(str(self.db_path))}: "
                f"{self._redact_error_text(str(exc))}"
            ) from exc

    def _initialize_schema(self) -> None:
        """Create database tables and indexes if they do not exist."""
        with self._connection() as conn:
            current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            conn.execute("""
                CREATE TABLE IF NOT EXISTS triage_items (
                    item_id TEXT PRIMARY KEY,
                    study_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    assignee TEXT,
                    severity TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS triage_annotations (
                    annotation_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES triage_items(item_id) ON DELETE CASCADE
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS triage_history (
                    transition_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    comment TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES triage_items(item_id) ON DELETE CASCADE
                )
                """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_triage_items_study_status
                ON triage_items(study_key, status)
                """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_triage_annotations_item
                ON triage_annotations(item_id, timestamp)
                """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_triage_history_item
                ON triage_history(item_id, timestamp)
                """)
            if current_version < _LATEST_SCHEMA_VERSION:
                self._migrate_schema(conn)
                conn.execute(f"PRAGMA user_version = {_LATEST_SCHEMA_VERSION}")  # nosem
            conn.commit()

    def get_journal_mode(self) -> str:
        """Return the current SQLite journal mode."""
        with self._connection() as conn:
            value = conn.execute("PRAGMA journal_mode").fetchone()[0]
        return str(value)

    def _execute_write(self, callback: Callable[[sqlite3.Connection], None]) -> None:
        """Execute a write callback within a transaction with retries."""
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
            raise sqlite3.OperationalError(
                f"SQLite write failed for triage store at "
                f"{self._redact_sqlite_target(str(self.db_path))}: "
                f"{self._redact_error_text(str(last_error))}"
            ) from last_error

    @staticmethod
    def _validate_item_id(raw_item_id: str) -> str:
        """Normalize and validate a triage item identifier."""
        cleaned = raw_item_id.strip()
        if not cleaned:
            raise ValueError("Triage item_id must not be empty.")
        if not _SAFE_SQL_IDENTIFIER.fullmatch(cleaned):
            raise ValueError(
                f"Invalid triage item identifier: {cleaned}. "
                "Only alphanumeric characters and ._:- are allowed."
            )
        return cleaned

    @staticmethod
    def _insert_item_details(
        conn: sqlite3.Connection,
        item: TriageItem,
        *,
        extra_annotation: tuple[str, str, str, str, str] | None = None,
        extra_history: tuple[str, str, str, str, str, str | None, str] | None = None,
    ) -> None:
        """Insert or upsert annotations and transition history rows for a triage item."""
        annotation_rows = [
            (
                annotation.annotation_id,
                item.item_id,
                annotation.user_id,
                annotation.comment,
                annotation.timestamp.isoformat(),
            )
            for annotation in item.annotations
        ]
        if extra_annotation is not None:
            annotation_rows.append(extra_annotation)
        if annotation_rows:
            conn.executemany(
                """
                INSERT INTO triage_annotations (annotation_id, item_id, user_id, comment, timestamp)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(annotation_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    comment = excluded.comment,
                    timestamp = excluded.timestamp
                """,
                annotation_rows,
            )

        history_rows = [
            (
                entry.transition_id,
                item.item_id,
                entry.from_status.value,
                entry.to_status.value,
                entry.user_id,
                entry.comment,
                entry.timestamp.isoformat(),
            )
            for entry in item.history
        ]
        if extra_history is not None:
            history_rows.append(extra_history)
        if history_rows:
            conn.executemany(
                """
                INSERT INTO triage_history (
                    transition_id, item_id, from_status, to_status, user_id, comment, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transition_id) DO UPDATE SET
                    from_status = excluded.from_status,
                    to_status = excluded.to_status,
                    user_id = excluded.user_id,
                    comment = excluded.comment,
                    timestamp = excluded.timestamp
                """,
                history_rows,
            )

    def create_item(
        self,
        item: TriageItem,
        *,
        initial_comment: str | None = None,
        user_id: str = "cli-user",
    ) -> None:
        """Atomically insert a new triage item, rejecting duplicate item IDs."""
        now = datetime.now(UTC).isoformat()
        cleaned_id = self._validate_item_id(item.item_id)
        cleaned_study = item.study_key.strip()
        if not cleaned_study:
            raise ValueError("Triage study_key must not be empty.")
        normalized_item = item.model_copy(
            update={"item_id": cleaned_id, "study_key": cleaned_study}
        )
        cleaned_comment = initial_comment.strip() if initial_comment else ""
        cleaned_user = user_id.strip() or "cli-user"

        def _write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                INSERT INTO triage_items (
                    item_id, study_key, status, assignee, severity, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO NOTHING
                """,
                (
                    normalized_item.item_id,
                    normalized_item.study_key,
                    normalized_item.status.value,
                    normalized_item.assignee,
                    normalized_item.severity,
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Triage item '{normalized_item.item_id}' already exists.")

            extra = (
                (str(uuid.uuid4()), normalized_item.item_id, cleaned_user, cleaned_comment, now)
                if cleaned_comment
                else None
            )
            self._insert_item_details(conn, normalized_item, extra_annotation=extra)

        self._execute_write(_write)

    def upsert_item(self, item: TriageItem) -> None:
        """Insert or update a triage item while preserving existing annotations and audit history."""
        now = datetime.now(UTC).isoformat()
        cleaned_id = self._validate_item_id(item.item_id)
        cleaned_study = item.study_key.strip()
        if not cleaned_study:
            raise ValueError("Triage study_key must not be empty.")
        normalized_item = item.model_copy(
            update={"item_id": cleaned_id, "study_key": cleaned_study}
        )

        def _write(conn: sqlite3.Connection) -> None:
            """Internal helper to write triage item data."""
            existing = conn.execute(
                "SELECT status FROM triage_items WHERE item_id = ?",
                (normalized_item.item_id,),
            ).fetchone()
            audited_transition: tuple[str, str, str, str, str, str | None, str] | None = None
            target_status = (
                normalized_item.status
                if isinstance(normalized_item.status, TriageStatus)
                else TriageStatus(str(normalized_item.status))
            )
            if existing is not None:
                current_status = TriageStatus(str(existing["status"]))
                if current_status != target_status:
                    validate_transition(current_status, target_status)
                    audited_transition = (
                        str(uuid.uuid4()),
                        normalized_item.item_id,
                        current_status.value,
                        target_status.value,
                        (normalized_item.assignee or "system").strip() or "system",
                        "Updated via upsert_item",
                        now,
                    )

            conn.execute(
                """
                INSERT INTO triage_items (
                    item_id, study_key, status, assignee, severity, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    study_key=excluded.study_key,
                    status=excluded.status,
                    assignee=excluded.assignee,
                    severity=excluded.severity,
                    updated_at=excluded.updated_at
                """,
                (
                    normalized_item.item_id,
                    normalized_item.study_key,
                    target_status.value,
                    normalized_item.assignee,
                    normalized_item.severity,
                    now,
                    now,
                ),
            )

            self._insert_item_details(
                conn,
                normalized_item,
                extra_history=audited_transition,
            )

        self._execute_write(_write)

    def get_triage_item(self, item_id: str) -> TriageItem | None:
        """Fetch a single triage item by ID."""
        cleaned_id = item_id.strip()
        if not cleaned_id:
            return None
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT item_id, study_key, status, assignee, severity
                FROM triage_items
                WHERE item_id = ?
                """,
                (cleaned_id,),
            ).fetchone()
            if row is None:
                return None

            annotations = self._get_annotations(conn, cleaned_id)
            history = self._get_history(conn, cleaned_id)
            return TriageItem(
                item_id=str(row["item_id"]),
                study_key=str(row["study_key"]),
                status=TriageStatus(str(row["status"])),
                assignee=str(row["assignee"]).strip() if row["assignee"] else None,
                severity=str(row["severity"]),
                annotations=annotations,
                history=history,
            )

    def get_queue(self, study_key: str, status: TriageStatus | None = None) -> list[TriageItem]:
        """Fetch the triage queue for a study, optionally filtered by status."""
        cleaned_study = study_key.strip()
        if not cleaned_study:
            return []
        with self._connection() as conn:
            if status is None:
                rows = conn.execute(
                    """
                    SELECT item_id, study_key, status, assignee, severity
                    FROM triage_items
                    WHERE study_key = ?
                    ORDER BY updated_at ASC
                    """,
                    (cleaned_study,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT item_id, study_key, status, assignee, severity
                    FROM triage_items
                    WHERE study_key = ? AND status = ?
                    ORDER BY updated_at ASC
                    """,
                    (cleaned_study, status.value),
                ).fetchall()

            item_ids = [str(row["item_id"]) for row in rows]
            if not item_ids:
                return []
            invalid_item_id = next(
                (item_id for item_id in item_ids if not _SAFE_SQL_IDENTIFIER.fullmatch(item_id)),
                None,
            )
            if invalid_item_id is not None:
                raise ValueError(
                    f"Invalid triage item identifier: {invalid_item_id}. "
                    "Only alphanumeric characters and ._:- are allowed."
                )

            conn.execute("DROP TABLE IF EXISTS temp_item_ids")
            conn.execute("CREATE TEMP TABLE temp_item_ids (item_id TEXT PRIMARY KEY)")
            conn.executemany(
                "INSERT INTO temp_item_ids (item_id) VALUES (?)",
                [(item_id,) for item_id in item_ids],
            )

            annotation_rows = conn.execute(
                """
                SELECT item_id, annotation_id, user_id, comment, timestamp
                FROM triage_annotations
                WHERE item_id IN (SELECT item_id FROM temp_item_ids)
                ORDER BY timestamp ASC
                """,
            ).fetchall()
            history_rows = conn.execute(
                """
                SELECT item_id, transition_id, from_status, to_status, user_id, comment, timestamp
                FROM triage_history
                WHERE item_id IN (SELECT item_id FROM temp_item_ids)
                ORDER BY timestamp ASC
                """,
            ).fetchall()

            annotations_by_item: dict[str, list[TriageAnnotation]] = {
                item_id: [] for item_id in item_ids
            }
            for annotation_row in annotation_rows:
                item_id = str(annotation_row["item_id"])
                annotations_by_item[item_id].append(
                    TriageAnnotation(
                        annotation_id=str(annotation_row["annotation_id"]),
                        user_id=str(annotation_row["user_id"]),
                        comment=str(annotation_row["comment"]),
                        timestamp=datetime.fromisoformat(str(annotation_row["timestamp"])),
                    )
                )

            history_by_item: dict[str, list[TriageHistoryEntry]] = {
                item_id: [] for item_id in item_ids
            }
            for history_row in history_rows:
                item_id = str(history_row["item_id"])
                history_by_item[item_id].append(
                    TriageHistoryEntry(
                        transition_id=str(history_row["transition_id"]),
                        from_status=TriageStatus(str(history_row["from_status"])),
                        to_status=TriageStatus(str(history_row["to_status"])),
                        user_id=str(history_row["user_id"]),
                        comment=(
                            str(history_row["comment"]).strip() if history_row["comment"] else None
                        ),
                        timestamp=datetime.fromisoformat(str(history_row["timestamp"])),
                    )
                )

            return [
                TriageItem(
                    item_id=str(row["item_id"]),
                    study_key=str(row["study_key"]),
                    status=TriageStatus(str(row["status"])),
                    assignee=str(row["assignee"]).strip() if row["assignee"] else None,
                    severity=str(row["severity"]),
                    annotations=annotations_by_item.get(str(row["item_id"]), []),
                    history=history_by_item.get(str(row["item_id"]), []),
                )
                for row in rows
            ]

    def assign_item(self, item_id: str, assignee: str) -> None:
        """Assign a triage item to a specific user."""
        cleaned_id = item_id.strip()

        def _write(conn: sqlite3.Connection) -> None:
            """Internal helper to update item assignee."""
            cursor = conn.execute(
                """
                UPDATE triage_items
                SET assignee = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (assignee.strip(), datetime.now(UTC).isoformat(), cleaned_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Unknown triage item: {cleaned_id}")

        self._execute_write(_write)

    def update_status(
        self,
        item_id: str,
        to_status: TriageStatus,
        user_id: str,
        comment: str | None,
    ) -> None:
        """Update the status of a triage item and record the transition in history."""
        cleaned_id = item_id.strip()
        normalized_comment = comment.strip() if comment else None
        timestamp = datetime.now(UTC).isoformat()

        def _write(conn: sqlite3.Connection) -> None:
            """Internal helper to update item status."""
            row = conn.execute(
                "SELECT status FROM triage_items WHERE item_id = ?",
                (cleaned_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown triage item: {cleaned_id}")

            from_status = TriageStatus(str(row["status"]))
            target_status = (
                to_status if isinstance(to_status, TriageStatus) else TriageStatus(str(to_status))
            )

            if from_status == target_status:
                return

            validate_transition(from_status, target_status)

            conn.execute(
                """
                UPDATE triage_items
                SET status = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (target_status.value, timestamp, cleaned_id),
            )
            conn.execute(
                """
                INSERT INTO triage_history (
                    transition_id, item_id, from_status, to_status, user_id, comment, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    cleaned_id,
                    from_status.value,
                    target_status.value,
                    user_id,
                    normalized_comment,
                    timestamp,
                ),
            )

        self._execute_write(_write)

    def add_annotation(self, item_id: str, user_id: str, comment: str) -> None:
        """Add a comment/annotation to a triage item."""
        cleaned_id = item_id.strip()
        cleaned_comment = comment.strip()
        if not cleaned_comment:
            raise ValueError("Annotation comment must not be empty")

        timestamp = datetime.now(UTC).isoformat()

        def _write(conn: sqlite3.Connection) -> None:
            """Internal helper to add item annotation."""
            row = conn.execute(
                "SELECT item_id FROM triage_items WHERE item_id = ?",
                (cleaned_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown triage item: {cleaned_id}")

            conn.execute(
                """
                INSERT INTO triage_annotations (annotation_id, item_id, user_id, comment, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), cleaned_id, user_id, cleaned_comment, timestamp),
            )
            conn.execute(
                "UPDATE triage_items SET updated_at = ? WHERE item_id = ?",
                (timestamp, cleaned_id),
            )

        self._execute_write(_write)

    def get_item_last_updated(self, item_id: str) -> datetime | None:
        """Get the last updated timestamp for a triage item."""
        cleaned_id = item_id.strip()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT updated_at FROM triage_items WHERE item_id = ?",
                (cleaned_id,),
            ).fetchone()
        if row is None or row["updated_at"] is None:
            return None
        return datetime.fromisoformat(str(row["updated_at"]))

    def _get_annotations(self, conn: sqlite3.Connection, item_id: str) -> list[TriageAnnotation]:
        """Internal helper to fetch annotations for an item."""
        rows = conn.execute(
            """
            SELECT annotation_id, user_id, comment, timestamp
            FROM triage_annotations
            WHERE item_id = ?
            ORDER BY timestamp ASC
            """,
            (item_id,),
        ).fetchall()
        return [
            TriageAnnotation(
                annotation_id=str(row["annotation_id"]),
                user_id=str(row["user_id"]),
                comment=str(row["comment"]),
                timestamp=datetime.fromisoformat(str(row["timestamp"])),
            )
            for row in rows
        ]

    def _get_history(self, conn: sqlite3.Connection, item_id: str) -> list[TriageHistoryEntry]:
        """Internal helper to fetch history entries for an item."""
        rows = conn.execute(
            """
            SELECT transition_id, from_status, to_status, user_id, comment, timestamp
            FROM triage_history
            WHERE item_id = ?
            ORDER BY timestamp ASC
            """,
            (item_id,),
        ).fetchall()
        return [
            TriageHistoryEntry(
                transition_id=str(row["transition_id"]),
                from_status=TriageStatus(str(row["from_status"])),
                to_status=TriageStatus(str(row["to_status"])),
                user_id=str(row["user_id"]),
                comment=str(row["comment"]).strip() if row["comment"] else None,
                timestamp=datetime.fromisoformat(str(row["timestamp"])),
            )
            for row in rows
        ]

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Perform schema migrations to reach the latest version."""
        columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(triage_items)").fetchall()
        }
        now = datetime.now(UTC).isoformat()
        if "created_at" not in columns:
            conn.execute("ALTER TABLE triage_items ADD COLUMN created_at TEXT")
            conn.execute(
                """
                UPDATE triage_items
                SET created_at = ?
                WHERE created_at IS NULL
                """,
                (now,),
            )
        if "updated_at" not in columns:
            conn.execute("ALTER TABLE triage_items ADD COLUMN updated_at TEXT")
            conn.execute(
                """
                UPDATE triage_items
                SET updated_at = ?
                WHERE updated_at IS NULL
                """,
                (now,),
            )

    def _redact_sqlite_target(self, target: str) -> str:
        """Redact sensitive information from a SQLite connection string."""
        if "://" not in target and not target.startswith("file:"):
            return target
        return redact_sensitive_text(target)

    def _redact_error_text(self, message: str) -> str:
        """Redact sensitive information from an error message."""
        return str(redact_sensitive_payload(message))
