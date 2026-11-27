"""Workflow for managing a triage queue of records using a local SQLite database."""

from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import unquote, urlsplit

from imednet.spi.models import (
    TriageAnnotation,
    TriageHistoryEntry,
    TriageItem,
    TriageStatus,
    validate_transition,
)
from imednet.spi.utils import (
    PathTraversalValidationError,
    SensitivityRegistry,
    build_study_sensitivity_registry,
    extract_sdk_security_key,
    get_base_url_context,
    get_security_key_context,
    get_study_context,
    normalize_cache_origin,
    redact_sensitive_payload,
    redact_sensitive_text,
    reset_base_url_context,
    reset_security_key_context,
    resolve_sdk_base_url,
    resolve_study_configuration,
    sanitize_diagnostic_text,
    set_base_url_context,
    set_security_key_context,
    sqlite_connection,
    validate_partition_key,
)

DEFAULT_TRIAGE_DB_PATH = Path("~/.imednet/triage.sqlite3")
_ORIG_PATH_HOME_FUNC = getattr(Path.home, "__func__", Path.home)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_SAFE_SEGMENT_SUB_RE = re.compile(r"[^a-zA-Z0-9_.-]")
_LATEST_SCHEMA_VERSION = 2
_SQLITE_BUSY_TIMEOUT_MS = 30_000
_RETRY_BASE_DELAY_SECONDS = 0.05


def _resolve_dynamic_home() -> Path:
    """Resolve user home directory dynamically honoring monkeypatched ``Path.home()`` and ``HOME``."""
    home_fn = Path.home
    if getattr(home_fn, "__func__", home_fn) is not _ORIG_PATH_HOME_FUNC:
        return Path(home_fn())
    env_home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if env_home and env_home.strip():
        return Path(env_home.strip())
    return Path.home()


def _expand_triage_path(raw_path: str | Path) -> Path:
    """Expand leading ``~`` using dynamic home resolution."""
    raw_str = os.fspath(raw_path)
    if raw_str == "~":
        return _resolve_dynamic_home()
    if raw_str.startswith(("~/", "~\\")):
        return _resolve_dynamic_home() / raw_str[2:]
    return Path(raw_str).expanduser()


def _resolve_raw_triage_db_path(db_path: str | Path | None) -> Path:
    """Resolve unpartitioned base SQLite file path from argument, env, or dynamic home."""
    if db_path is not None:
        return _expand_triage_path(db_path)
    raw_env = os.environ.get("IMEDNET_TRIAGE_DB_PATH")
    if raw_env and raw_env.strip():
        return _expand_triage_path(raw_env.strip())
    return _resolve_dynamic_home() / ".imednet" / "triage.sqlite3"


def _has_control_or_null(value: str) -> bool:
    """Return True if *value* contains NUL or ASCII control characters."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _sanitize_origin_segment(raw_url: str) -> str:
    """Normalize and validate an origin URL into a safe single directory segment."""
    candidate = raw_url.strip() if raw_url and raw_url.strip() else "http://localhost"
    decoded = unquote(candidate)
    if _has_control_or_null(candidate) or _has_control_or_null(decoded):
        raise PathTraversalValidationError("Origin URL must not contain control characters.")
    if "\\" in candidate or "\\" in decoded or _WINDOWS_DRIVE_RE.match(decoded):
        raise PathTraversalValidationError(f"Unsafe path delimiters in origin URL: {raw_url!r}")
    if candidate.startswith("/") or decoded.startswith("/"):
        raise PathTraversalValidationError(f"Origin URL must not be a rooted path: {raw_url!r}")

    probe = decoded if "://" in decoded else f"https://{decoded}"
    parsed_probe = urlsplit(probe)
    if parsed_probe.scheme.lower() not in {"http", "https"}:
        raise PathTraversalValidationError(
            f"Unsupported origin URL scheme: {parsed_probe.scheme!r}"
        )
    hostname = parsed_probe.hostname or ""
    if not hostname or hostname in {".", ".."} or ".." in hostname:
        raise PathTraversalValidationError(f"Unsafe hostname in origin URL: {raw_url!r}")
    for part in parsed_probe.path.split("/"):
        if part in {".", ".."}:
            raise PathTraversalValidationError(
                f"Relative dot-segments are not allowed in origin URL: {raw_url!r}"
            )

    normalized_origin = normalize_cache_origin(candidate)
    parsed_norm = urlsplit(normalized_origin)
    raw_segment = (
        f"{parsed_norm.netloc}{parsed_norm.path}"
        if parsed_norm.path and parsed_norm.path != "/"
        else (parsed_norm.netloc or parsed_norm.path)
    )
    sanitized = _SAFE_SEGMENT_SUB_RE.sub("_", raw_segment)
    if not sanitized or sanitized in {".", ".."}:
        raise PathTraversalValidationError(f"Invalid sanitized origin segment: {sanitized!r}")
    validate_partition_key(sanitized)
    return sanitized


def _resolve_store_origin(
    *,
    explicit_db_path: bool,
    base_url: str | None,
    sdk: Any,
) -> str | None:
    """Determine normalized origin URL for store scoping, or ``None`` for unscoped explicit paths."""
    if base_url is not None:
        return base_url if base_url.strip() else "http://localhost"
    if sdk is not None:
        return resolve_sdk_base_url(sdk)
    ctx_url = get_base_url_context()
    if ctx_url is not None:
        return ctx_url if ctx_url.strip() else "http://localhost"
    if explicit_db_path:
        return None
    env_url = os.environ.get("IMEDNET_BASE_URL")
    if env_url is not None and env_url.strip():
        return env_url.strip()
    return resolve_sdk_base_url(None)


def _compute_scoped_db_path(base_db_path: Path, origin_url: str | None) -> tuple[Path, bool]:
    """Compute validated ``(target_db_path, is_origin_scoped)`` with traversal checks."""
    ctx_study = get_study_context()
    if ctx_study:
        validate_partition_key(ctx_study)

    if origin_url is None:
        return (base_db_path, False)

    segment = _sanitize_origin_segment(origin_url)
    base_dir = base_db_path.parent
    host_dir = base_dir / segment
    resolved_base = base_dir.resolve()
    resolved_host = host_dir.resolve()
    if resolved_host == resolved_base or not resolved_host.is_relative_to(resolved_base):
        raise PathTraversalValidationError(
            f"Resolved target directory {resolved_host} escapes base directory {resolved_base}"
        )
    return (host_dir / base_db_path.name, True)


def _ensure_secure_directory(target_dir: Path, *, harden_existing_leaf: bool) -> None:
    """Create ``target_dir`` and enforce ``0700`` permissions on created/store directories on POSIX."""
    missing_ancestors: list[Path] = []
    if os.name == "posix":
        probe = target_dir
        while not probe.exists() and probe != probe.parent:
            missing_ancestors.append(probe)
            probe = probe.parent

    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "posix":
        return

    for created_dir in reversed(missing_ancestors):
        with suppress(OSError):
            os.chmod(created_dir, 0o700)
    if harden_existing_leaf or target_dir.name == ".imednet":
        with suppress(OSError):
            os.chmod(target_dir, 0o700)


def _precreate_and_harden_sqlite_files(db_path: Path, *, create_if_missing: bool) -> None:
    """Ensure SQLite DB, WAL, and SHM files use ``0600`` owner-only permissions on POSIX."""
    if os.name != "posix":
        return
    if create_if_missing and not db_path.exists():
        with suppress(OSError):
            fd = os.open(db_path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if candidate.exists():
            with suppress(OSError):
                os.chmod(candidate, 0o600)


class TriageStore:
    """Thread-safe SQLite-backed triage queue and decision store."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        base_url: str | None = None,
        sdk: Any = None,
        timeout: float = 30.0,
        retry_attempts: int = 3,
    ) -> None:
        """Initialize the triage store.

        Args:
            db_path: Path to the SQLite database file. Defaults to
                ``IMEDNET_TRIAGE_DB_PATH`` or ``~/.imednet/<origin>/triage.sqlite3``.
            base_url: Optional EDC origin URL to scope the store directory under.
            sdk: Optional SDK or facade instance supplying origin and governance context.
            timeout: Maximum time to wait for a database connection.
            retry_attempts: Number of write retry attempts.
        """
        raw_base_path = _resolve_raw_triage_db_path(db_path)
        origin_url = _resolve_store_origin(
            explicit_db_path=db_path is not None,
            base_url=base_url,
            sdk=sdk,
        )
        resolved_db_path, is_origin_scoped = _compute_scoped_db_path(raw_base_path, origin_url)
        self._pinned_base_url: str | None = (
            normalize_cache_origin(origin_url)
            if origin_url is not None and origin_url.strip()
            else origin_url
        )
        self._sdk_security_key: str | None = (
            extract_sdk_security_key(sdk) if sdk is not None else get_security_key_context()
        )
        self._is_origin_scoped = is_origin_scoped
        self.db_path = resolved_db_path
        _ensure_secure_directory(
            self.db_path.parent,
            harden_existing_leaf=is_origin_scoped or db_path is None,
        )
        self._timeout = timeout
        self._retry_attempts = retry_attempts
        self._lock = RLock()
        self._initialize_schema()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager for a configured SQLite connection."""
        _ensure_secure_directory(
            self.db_path.parent,
            harden_existing_leaf=self._is_origin_scoped,
        )
        _precreate_and_harden_sqlite_files(self.db_path, create_if_missing=True)
        try:
            with sqlite_connection(
                self.db_path,
                timeout=self._timeout,
                busy_timeout_ms=_SQLITE_BUSY_TIMEOUT_MS,
                foreign_keys=True,
            ) as conn:
                _precreate_and_harden_sqlite_files(self.db_path, create_if_missing=False)
                try:
                    yield conn
                finally:
                    _precreate_and_harden_sqlite_files(self.db_path, create_if_missing=False)
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as exc:
            raise sqlite3.OperationalError(
                f"Unable to open triage store at {self._redact_sqlite_target(str(self.db_path))}: "
                f"{self._redact_error_text(str(exc))}"
            ) from exc

    def _resolve_sensitivity_registry(self, study_key: str | None) -> SensitivityRegistry:
        """Build a study-aware ``SensitivityRegistry`` without requiring an encryption key."""
        token_base = (
            set_base_url_context(self._pinned_base_url)
            if self._pinned_base_url is not None and get_base_url_context() is None
            else None
        )
        token_sec = (
            set_security_key_context(self._sdk_security_key)
            if self._sdk_security_key is not None and get_security_key_context() is None
            else None
        )
        try:
            study_cfg = resolve_study_configuration(study_key)
        finally:
            if token_sec is not None:
                reset_security_key_context(token_sec)
            if token_base is not None:
                reset_base_url_context(token_base)
        phi_fields = study_cfg.phi_fields if study_cfg is not None else None
        return build_study_sensitivity_registry(phi_fields)

    def _scrub_comment(
        self,
        comment: str,
        *,
        study_key: str | None = None,
        registry: SensitivityRegistry | None = None,
    ) -> str:
        """Scrub PHI expressions and credentials from a triage comment string."""
        active_registry = (
            registry if registry is not None else self._resolve_sensitivity_registry(study_key)
        )
        return sanitize_diagnostic_text(comment, registry=active_registry)

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
                    annotation_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    PRIMARY KEY (item_id, annotation_id),
                    FOREIGN KEY(item_id) REFERENCES triage_items(item_id) ON DELETE CASCADE
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS triage_history (
                    transition_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    comment TEXT,
                    timestamp TEXT NOT NULL,
                    PRIMARY KEY (item_id, transition_id),
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

    def upsert_item(self, item: TriageItem) -> None:
        """Insert or update a triage item while preserving prior annotations and history."""
        now = datetime.now(UTC).isoformat()
        registry = self._resolve_sensitivity_registry(item.study_key)

        def _write(conn: sqlite3.Connection) -> None:
            """Internal helper to write triage item data."""
            existing = conn.execute(
                "SELECT status, assignee FROM triage_items WHERE item_id = ?",
                (item.item_id,),
            ).fetchone()
            target_status = (
                item.status
                if isinstance(item.status, TriageStatus)
                else TriageStatus(str(item.status))
            )
            status_changed = False
            current_status: TriageStatus | None = None
            if existing is not None:
                current_status = TriageStatus(str(existing["status"]))
                if current_status != target_status:
                    validate_transition(current_status, target_status)
                    status_changed = True

            existing_transition_ids: set[str] = set()
            if status_changed:
                existing_transition_ids = {
                    str(row["transition_id"])
                    for row in conn.execute(
                        "SELECT transition_id FROM triage_history WHERE item_id = ?",
                        (item.item_id,),
                    ).fetchall()
                }

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
                    item.item_id,
                    item.study_key,
                    target_status.value,
                    item.assignee,
                    item.severity,
                    now,
                    now,
                ),
            )

            if item.annotations:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO triage_annotations (
                        annotation_id, item_id, user_id, comment, timestamp
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            annotation.annotation_id,
                            item.item_id,
                            annotation.user_id,
                            self._scrub_comment(annotation.comment, registry=registry),
                            annotation.timestamp.isoformat(),
                        )
                        for annotation in item.annotations
                    ],
                )

            if item.history:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO triage_history (
                        transition_id, item_id, from_status, to_status, user_id, comment, timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            entry.transition_id,
                            item.item_id,
                            entry.from_status.value,
                            entry.to_status.value,
                            entry.user_id,
                            (
                                self._scrub_comment(entry.comment, registry=registry)
                                if entry.comment is not None
                                else None
                            ),
                            entry.timestamp.isoformat(),
                        )
                        for entry in item.history
                    ],
                )

            if status_changed and current_status is not None:
                has_matching_new_entry = any(
                    entry.transition_id not in existing_transition_ids
                    and entry.from_status == current_status
                    and entry.to_status == target_status
                    for entry in item.history
                )
                if not has_matching_new_entry:
                    actor = (
                        item.assignee.strip()
                        if item.assignee and item.assignee.strip()
                        else (
                            str(existing["assignee"]).strip()
                            if existing is not None
                            and existing["assignee"]
                            and str(existing["assignee"]).strip()
                            else "system"
                        )
                    )
                    transition_entry = TriageHistoryEntry(
                        transition_id=str(uuid.uuid4()),
                        from_status=current_status,
                        to_status=target_status,
                        user_id=actor,
                        comment=None,
                        timestamp=datetime.fromisoformat(now),
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO triage_history (
                            transition_id, item_id, from_status, to_status, user_id, comment, timestamp
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            transition_entry.transition_id,
                            item.item_id,
                            transition_entry.from_status.value,
                            transition_entry.to_status.value,
                            transition_entry.user_id,
                            transition_entry.comment,
                            transition_entry.timestamp.isoformat(),
                        ),
                    )

        self._execute_write(_write)

    def get_triage_item(self, item_id: str) -> TriageItem | None:
        """Fetch a single triage item by ID."""
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT item_id, study_key, status, assignee, severity
                FROM triage_items
                WHERE item_id = ?
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                return None

            annotations = self._get_annotations(conn, item_id)
            history = self._get_history(conn, item_id)
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
        with self._connection() as conn:
            if status is None:
                rows = conn.execute(
                    """
                    SELECT item_id, study_key, status, assignee, severity
                    FROM triage_items
                    WHERE study_key = ?
                    ORDER BY updated_at ASC
                    """,
                    (study_key,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT item_id, study_key, status, assignee, severity
                    FROM triage_items
                    WHERE study_key = ? AND status = ?
                    ORDER BY updated_at ASC
                    """,
                    (study_key, status.value),
                ).fetchall()

            item_ids = [str(row["item_id"]) for row in rows]
            if not item_ids:
                return []

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

        def _write(conn: sqlite3.Connection) -> None:
            """Internal helper to update item assignee."""
            cursor = conn.execute(
                """
                UPDATE triage_items
                SET assignee = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (assignee.strip(), datetime.now(UTC).isoformat(), item_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Unknown triage item: {item_id}")

        self._execute_write(_write)

    def update_status(
        self,
        item_id: str,
        to_status: TriageStatus,
        user_id: str,
        comment: str | None,
    ) -> None:
        """Update the status of a triage item and record the transition in history."""
        normalized_comment = comment.strip() if comment else None
        timestamp = datetime.now(UTC).isoformat()

        def _write(conn: sqlite3.Connection) -> None:
            """Internal helper to update item status."""
            row = conn.execute(
                "SELECT study_key, status FROM triage_items WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown triage item: {item_id}")

            from_status = TriageStatus(str(row["status"]))
            target_status = (
                to_status if isinstance(to_status, TriageStatus) else TriageStatus(str(to_status))
            )

            if from_status == target_status:
                return

            validate_transition(from_status, target_status)
            scrubbed_comment = (
                self._scrub_comment(normalized_comment, study_key=str(row["study_key"]))
                if normalized_comment is not None
                else None
            )

            conn.execute(
                """
                UPDATE triage_items
                SET status = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (target_status.value, timestamp, item_id),
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
                    item_id,
                    from_status.value,
                    target_status.value,
                    user_id,
                    scrubbed_comment,
                    timestamp,
                ),
            )

        self._execute_write(_write)

    def add_annotation(self, item_id: str, user_id: str, comment: str) -> None:
        """Add a comment/annotation to a triage item."""
        cleaned_comment = comment.strip()
        if not cleaned_comment:
            raise ValueError("Annotation comment must not be empty")

        timestamp = datetime.now(UTC).isoformat()

        def _write(conn: sqlite3.Connection) -> None:
            """Internal helper to add item annotation."""
            row = conn.execute(
                "SELECT study_key FROM triage_items WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown triage item: {item_id}")

            scrubbed_comment = self._scrub_comment(
                cleaned_comment,
                study_key=str(row["study_key"]),
            )
            conn.execute(
                """
                INSERT INTO triage_annotations (annotation_id, item_id, user_id, comment, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), item_id, user_id, scrubbed_comment, timestamp),
            )
            conn.execute(
                "UPDATE triage_items SET updated_at = ? WHERE item_id = ?",
                (timestamp, item_id),
            )

        self._execute_write(_write)

    def delete_item(self, item_id: str) -> bool:
        """Delete a triage item and its cascaded annotations and history.

        Args:
            item_id: Identifier of the triage item to delete.

        Returns:
            True if the item existed and was deleted, False otherwise.
        """
        deleted = False

        def _write(conn: sqlite3.Connection) -> None:
            """Internal helper to remove a triage item."""
            nonlocal deleted
            cursor = conn.execute(
                "DELETE FROM triage_items WHERE item_id = ?",
                (item_id,),
            )
            deleted = cursor.rowcount > 0

        self._execute_write(_write)
        return deleted

    def get_item_last_updated(self, item_id: str) -> datetime | None:
        """Get the last updated timestamp for a triage item."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT updated_at FROM triage_items WHERE item_id = ?",
                (item_id,),
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

        annotation_pk_cols = sum(
            1
            for row in conn.execute("PRAGMA table_info(triage_annotations)").fetchall()
            if int(row["pk"]) > 0
        )
        if annotation_pk_cols < 2:
            conn.execute("DROP INDEX IF EXISTS idx_triage_annotations_item")
            conn.execute("ALTER TABLE triage_annotations RENAME TO _triage_annotations_v1")
            conn.execute("""
                CREATE TABLE triage_annotations (
                    annotation_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    PRIMARY KEY (item_id, annotation_id),
                    FOREIGN KEY(item_id) REFERENCES triage_items(item_id) ON DELETE CASCADE
                )
                """)
            conn.execute("""
                INSERT OR IGNORE INTO triage_annotations (
                    annotation_id, item_id, user_id, comment, timestamp
                )
                SELECT annotation_id, item_id, user_id, comment, timestamp
                FROM _triage_annotations_v1
                WHERE item_id IN (SELECT item_id FROM triage_items)
                """)
            conn.execute("DROP TABLE _triage_annotations_v1")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_triage_annotations_item
                ON triage_annotations(item_id, timestamp)
                """)

        history_pk_cols = sum(
            1
            for row in conn.execute("PRAGMA table_info(triage_history)").fetchall()
            if int(row["pk"]) > 0
        )
        if history_pk_cols < 2:
            conn.execute("DROP INDEX IF EXISTS idx_triage_history_item")
            conn.execute("ALTER TABLE triage_history RENAME TO _triage_history_v1")
            conn.execute("""
                CREATE TABLE triage_history (
                    transition_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    comment TEXT,
                    timestamp TEXT NOT NULL,
                    PRIMARY KEY (item_id, transition_id),
                    FOREIGN KEY(item_id) REFERENCES triage_items(item_id) ON DELETE CASCADE
                )
                """)
            conn.execute("""
                INSERT OR IGNORE INTO triage_history (
                    transition_id, item_id, from_status, to_status, user_id, comment, timestamp
                )
                SELECT transition_id, item_id, from_status, to_status, user_id, comment, timestamp
                FROM _triage_history_v1
                WHERE item_id IN (SELECT item_id FROM triage_items)
                """)
            conn.execute("DROP TABLE _triage_history_v1")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_triage_history_item
                ON triage_history(item_id, timestamp)
                """)

    def _redact_sqlite_target(self, target: str) -> str:
        """Redact sensitive information from a SQLite connection string."""
        if "://" not in target and not target.startswith("file:"):
            return target
        return redact_sensitive_text(target)

    def _redact_error_text(self, message: str) -> str:
        """Redact sensitive information from an error message."""
        return str(redact_sensitive_payload(message))
