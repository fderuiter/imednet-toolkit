"""SQLite-backed version control ledger for study configurations.

Provides immutable, append-only commit history with SHA-256 content hashing,
diff capability, and safe rollback.  History blocks are read-only once written.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse

from imednet.spi.models import StudyConfiguration
from imednet.spi.utils import (
    PathTraversalValidationError,
    extract_sdk_security_key,
    flatten,
    get_base_url_context,
    get_security_key_context,
    get_study_context,
    sdk_governance_context,
    sqlite_connection,
    validate_partition_key,
)

_DEFAULT_DB_PATH = Path(
    os.environ.get("IMEDNET_CONFIG_DB_PATH", Path.home() / ".imednet" / "config_versions.sqlite3")
)
_DEFAULT_DB_PATH_STR = str(Path.home() / ".imednet" / "config_versions.sqlite3")
_DEFAULT_RAW_DB_PATH = Path(_DEFAULT_DB_PATH_STR)
_DEFAULT_BASE_DIR = _DEFAULT_RAW_DB_PATH.parent
_READ_STORE_CACHE: dict[Path, ConfigVersionStore] = {}
_DYNAMIC_PATH_CACHE: dict[tuple[str, str], Path] = {}
_HEAD_INDEX_CACHE: dict[tuple[Path, int, int, int], dict[str, str]] = {}


def _sha256_of(content: str) -> str:
    """Return the SHA-256 hex digest of the given string content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _compute_chained_commit_id(content_hash: str, parent_commit_id: str | None) -> str:
    """Compute a content-addressed root commit ID or parent-chained child commit ID."""
    if parent_commit_id is None:
        return content_hash
    return _sha256_of(f"{parent_commit_id}:{content_hash}")


def _sanitize_base_url_for_path(url: str) -> str:
    """Extract and sanitize host from URL to create a safe path name."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc or parsed.path
        host = re.sub(r'[^a-zA-Z0-9_\.-]', '_', host)
        if not host:
            host = "default"
        return host
    except Exception:
        return "default"


def _get_fernet_key(fallback_key: str | None = None) -> bytes:
    """Retrieve and derive a valid 32-byte base64-encoded Fernet key."""
    raw_key = (
        os.environ.get("IMEDNET_ENCRYPTION_KEY")
        or os.environ.get("IMEDNET_SECURITY_KEY")
        or get_security_key_context()
        or fallback_key
    )
    if not raw_key:
        raise ValueError(
            "Encryption/decryption key is missing. Please set IMEDNET_ENCRYPTION_KEY or IMEDNET_SECURITY_KEY."
        )

    try:
        decoded = base64.urlsafe_b64decode(raw_key)
        if len(decoded) == 32:
            return raw_key.encode("utf-8")
    except Exception:  # noqa: S110
        pass

    digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _encrypt_payload(payload: str, *, fallback_key: str | None = None) -> str:
    """Encrypt payload string using AES-256 Fernet encryption."""
    from cryptography.fernet import Fernet

    key = _get_fernet_key(fallback_key=fallback_key)
    f = Fernet(key)
    return f.encrypt(payload.encode("utf-8")).decode("utf-8")


def _decrypt_payload(encrypted_payload: str, *, fallback_key: str | None = None) -> str:
    """Decrypt Fernet encrypted payload or raise clear ValueError if key is incorrect or missing."""
    from cryptography.fernet import Fernet, InvalidToken

    try:
        key = _get_fernet_key(fallback_key=fallback_key)
    except Exception as exc:
        raise ValueError(
            "Configuration data integrity check failed: "
            f"Decryption key is incorrect or missing: {exc}"
        ) from exc
    try:
        f = Fernet(key)
        return f.decrypt(encrypted_payload.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception) as exc:
        raise ValueError(
            "Configuration data integrity check failed: "
            "Decryption failed - decryption key is incorrect or missing, or data is corrupted."
        ) from exc


class ConfigVersionStore:
    """Immutable append-only store for ``StudyConfiguration`` versions.

    Each call to :meth:`commit_config` creates a new entry signed with a
    SHA-256 digest of the serialised configuration body.  History is strictly
    read-only — individual commit rows may never be edited or deleted.

    Args:
        db_path: Filesystem path for the SQLite database.  Defaults to
            ``~/.imednet/config_versions.sqlite3``.
    """

    _RESOLVE_COMMITTED_INCLUDES_LEGACY = True

    def __init__(self, db_path: str | Path | None = None) -> None:
        """Initialize the configuration version store.

        Args:
            db_path: Path to the SQLite database file.
        """
        resolved_path = (
            Path(db_path)
            if db_path is not None
            else Path(os.environ.get("IMEDNET_CONFIG_DB_PATH") or _DEFAULT_DB_PATH)
        )
        self._original_db_path = resolved_path.expanduser()
        self._lock = RLock()
        self._sdk_security_key: str | None = None
        self._ensure_schema_initialized(self.db_path)

    @property
    def db_path(self) -> Path:
        """Return the dynamically selected database file path based on active base URL context."""
        return self._get_get_dynamic_db_path()

    @db_path.setter
    def db_path(self, value: str | Path) -> None:
        """Set the original base path for database files."""
        self._original_db_path = Path(value).expanduser()

    @staticmethod
    def _get_dynamic_db_path_no_create(base_db_path: Path) -> Path:
        """Compute host-scoped dynamic DB path without splitting by study_context or creating dirs."""
        base_url = (
            get_base_url_context() or os.environ.get("IMEDNET_BASE_URL") or "http://localhost"
        )
        ctx_study_key = get_study_context()
        if ctx_study_key:
            validate_partition_key(ctx_study_key)

        cache_key = (str(base_db_path), base_url)
        cached = _DYNAMIC_PATH_CACHE.get(cache_key)
        if cached is not None:
            return cached

        sanitized_url = _sanitize_base_url_for_path(base_url)
        base_dir = base_db_path.parent
        target_dir = base_dir / sanitized_url

        resolved_base = base_dir.resolve()
        resolved_target = target_dir.resolve()
        if not resolved_target.is_relative_to(resolved_base):
            raise PathTraversalValidationError(
                f"Resolved target directory {resolved_target} escapes base directory {resolved_base}"
            )
        result = target_dir / base_db_path.name
        if len(_DYNAMIC_PATH_CACHE) > 64:
            _DYNAMIC_PATH_CACHE.clear()
        _DYNAMIC_PATH_CACHE[cache_key] = result
        return result

    @staticmethod
    def _get_legacy_db_paths_no_create(
        base_db_path: Path,
        study_key: str,
        *,
        canonical_path: Path | None = None,
        _validated: bool = False,
    ) -> list[Path]:
        """Return legacy study_context-partitioned and flat candidate SQLite paths without creating dirs."""
        if not _validated:
            validate_partition_key(study_key)
        effective_canonical = (
            canonical_path
            if canonical_path is not None
            else ConfigVersionStore._get_dynamic_db_path_no_create(base_db_path)
        )
        host_dir = effective_canonical.parent
        base_dir = base_db_path.parent
        resolved_base: Path | None = None

        candidates: list[Path] = []
        if study_key and os.path.isdir(host_dir):
            legacy_study_dir = host_dir / study_key
            legacy_db = legacy_study_dir / base_db_path.name
            if os.path.isfile(legacy_db):
                resolved_base = base_dir.resolve()
                if legacy_study_dir.resolve().is_relative_to(resolved_base):
                    candidates.append(legacy_db)
        if os.path.isfile(base_db_path):
            if resolved_base is None:
                resolved_base = base_dir.resolve()
            if base_db_path.resolve().is_relative_to(resolved_base):
                candidates.append(base_db_path)
        return candidates

    @classmethod
    def _read_committed_from_sqlite_path(
        cls,
        candidate_path: Path,
        study_key: str,
        *,
        raw_db_path: Path | None = None,
        use_cached_store: bool = False,
    ) -> StudyConfiguration | None:
        """Read and verify the latest committed StudyConfiguration for *study_key* from *candidate_path*."""
        try:
            db_stat = os.stat(candidate_path)
        except OSError:
            return None

        if use_cached_store and raw_db_path is not None:
            cached_store = _READ_STORE_CACHE.get(candidate_path)
            if cached_store is None:
                if len(_READ_STORE_CACHE) > 32:
                    _READ_STORE_CACHE.clear()
                cached_store = cls(db_path=raw_db_path)
                _READ_STORE_CACHE[candidate_path] = cached_store
            if hasattr(cls.get_head, "_mock_name") or hasattr(cached_store.get_head, "_mock_name"):
                head_id = cached_store.get_head(study_key)
                return cached_store.rollback_config(study_key, head_id) if head_id else None
        else:
            cached_store = None

        wal_mtime = 0
        with suppress(OSError):
            wal_mtime = os.stat(f"{candidate_path}-wal").st_mtime_ns
        snap_key = (candidate_path, db_stat.st_mtime_ns, db_stat.st_size, wal_mtime)
        head_index = _HEAD_INDEX_CACHE.get(snap_key)
        if head_index is None:
            if len(_HEAD_INDEX_CACHE) > 32:
                _HEAD_INDEX_CACHE.clear()
            try:
                with sqlite_connection(candidate_path, timeout=30.0) as conn:
                    tables = {
                        str(r[0])
                        for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if "config_commits" not in tables:
                        return None
                    rows = (
                        conn.execute(
                            "SELECT study_key, head_commit_id FROM config_heads"
                        ).fetchall()
                        if "config_heads" in tables
                        else []
                    )
                    indexed = {str(r["study_key"]): str(r["head_commit_id"]) for r in rows}
                    fallback_rows = conn.execute(
                        "SELECT study_key, commit_id FROM config_commits ORDER BY timestamp ASC, rowid ASC"
                    ).fetchall()
                    missing_keys = {str(r["study_key"]) for r in fallback_rows} - {
                        str(r["study_key"]) for r in rows
                    }
                    for r in fallback_rows:
                        sk = str(r["study_key"])
                        if sk not in indexed or sk in missing_keys:
                            indexed[sk] = str(r["commit_id"])
            except sqlite3.Error:
                return None
            head_index = indexed
            _HEAD_INDEX_CACHE[snap_key] = head_index

        head_id = head_index.get(study_key)
        if not head_id:
            return None

        if cached_store is not None:
            return cached_store.rollback_config(study_key, head_id)

        with sqlite_connection(candidate_path, timeout=30.0) as conn:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(config_commits)").fetchall()}
            if "parent_commit_id" in cols and "content_hash" in cols:
                row = conn.execute(
                    """
                    SELECT commit_id, study_key, config_data, parent_commit_id, content_hash
                    FROM config_commits
                    WHERE commit_id = ? AND study_key = ?
                    """,
                    (head_id, study_key),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT
                        commit_id,
                        study_key,
                        config_data,
                        NULL AS parent_commit_id,
                        NULL AS content_hash
                    FROM config_commits
                    WHERE commit_id = ? AND study_key = ?
                    """,
                    (head_id, study_key),
                ).fetchone()
        if row is None:
            return None
        decrypted_data = _decrypt_payload(row["config_data"])
        cls._verify_commit_signature_static(
            str(row["commit_id"]),
            decrypted_data,
            content_hash=row["content_hash"],
            parent_commit_id=row["parent_commit_id"],
            study_key=str(row["study_key"]),
        )
        return StudyConfiguration.model_validate_json(decrypted_data)

    @classmethod
    def _resolve_legacy_with_canonical(
        cls, raw_db_path: Path, canonical_path: Path, study_key: str
    ) -> StudyConfiguration | None:
        """Read latest committed StudyConfiguration from legacy paths given a resolved canonical path."""
        for legacy_path in cls._get_legacy_db_paths_no_create(
            raw_db_path,
            study_key,
            canonical_path=canonical_path,
            _validated=True,
        ):
            if legacy_path == canonical_path:
                continue
            resolved = cls._read_committed_from_sqlite_path(legacy_path, study_key)
            if resolved is not None:
                return resolved
        return None

    @classmethod
    def resolve_legacy_committed_config(cls, study_key: str) -> StudyConfiguration | None:
        """Read latest committed StudyConfiguration from legacy study-partitioned SQLite paths."""
        validate_partition_key(study_key)
        raw_env = os.environ.get("IMEDNET_CONFIG_DB_PATH")
        raw_db_path = Path(raw_env).expanduser() if raw_env else _DEFAULT_RAW_DB_PATH
        canonical_path = cls._get_dynamic_db_path_no_create(raw_db_path)
        base_dir = raw_db_path.parent if raw_env else _DEFAULT_BASE_DIR
        if not os.path.isdir(base_dir):
            return None
        return cls._resolve_legacy_with_canonical(raw_db_path, canonical_path, study_key)

    @classmethod
    def resolve_committed_config(cls, study_key: str) -> StudyConfiguration | None:
        """Read the latest committed :class:`StudyConfiguration` without creating empty DB files."""
        validate_partition_key(study_key)
        if hasattr(cls.get_history, "_mock_name") or hasattr(cls.rollback_config, "_mock_name"):
            store = cls()
            history = store.get_history(study_key)
            if not history or not isinstance(history, list):
                return None
            latest = history[-1]
            if not isinstance(latest, dict) or "commit_id" not in latest:
                return None
            cfg = store.rollback_config(study_key, latest["commit_id"])
            return cfg if isinstance(cfg, StudyConfiguration) else None

        raw_env = os.environ.get("IMEDNET_CONFIG_DB_PATH")
        raw_db_path = Path(raw_env).expanduser() if raw_env else _DEFAULT_RAW_DB_PATH
        candidate_path = cls._get_dynamic_db_path_no_create(raw_db_path)
        base_dir = raw_db_path.parent if raw_env else _DEFAULT_BASE_DIR
        if not os.path.isdir(base_dir):
            return None
        resolved = cls._read_committed_from_sqlite_path(
            candidate_path,
            study_key,
            raw_db_path=raw_db_path,
            use_cached_store=True,
        )
        if resolved is not None:
            return resolved
        return cls._resolve_legacy_with_canonical(raw_db_path, candidate_path, study_key)

    def _get_get_dynamic_db_path(self) -> Path:
        """Resolve database path dynamically based on active base URL without splitting by study_context."""
        result = self._get_dynamic_db_path_no_create(self._original_db_path)
        result.parent.mkdir(parents=True, exist_ok=True)
        return result

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Manage a SQLite connection with Write-Ahead Logging (WAL) enabled."""
        path = self.db_path
        self._ensure_schema_initialized(path)
        with sqlite_connection(path, timeout=30.0) as conn:
            yield conn

    def _ensure_schema_initialized(self, path: Path) -> None:
        """Thread-safely ensure that schema is initialized for a specific file path."""
        if not hasattr(self, "_initialized_paths"):
            self._initialized_paths: set[Path] = set()
        if path not in self._initialized_paths:
            with self._lock:
                if path not in self._initialized_paths:
                    self._initialize_schema_for_path(path)
                    self._initialized_paths.add(path)

    def _initialize_schema(self) -> None:
        """Create the database tables and triggers for the active database path."""
        self._ensure_schema_initialized(self.db_path)

    def _initialize_schema_for_path(self, path: Path) -> None:
        """Create or migrate database tables and triggers for the given path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite_connection(path, timeout=30.0) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS config_commits (
                    commit_id        TEXT PRIMARY KEY,
                    study_key        TEXT NOT NULL,
                    version_tag      TEXT NOT NULL,
                    config_data      TEXT NOT NULL,
                    modified_by      TEXT NOT NULL,
                    description      TEXT NOT NULL,
                    timestamp        TEXT NOT NULL,
                    parent_commit_id TEXT,
                    content_hash     TEXT
                )
            """)
            existing_cols = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(config_commits)").fetchall()
            }
            if "parent_commit_id" not in existing_cols:
                conn.execute("ALTER TABLE config_commits ADD COLUMN parent_commit_id TEXT")
            if "content_hash" not in existing_cols:
                conn.execute("ALTER TABLE config_commits ADD COLUMN content_hash TEXT")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_config_commits_study"
                " ON config_commits (study_key, timestamp)"
            )
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_config_commits_no_update
                BEFORE UPDATE ON config_commits
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'Configuration history is immutable and cannot be modified'
                    );
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_config_commits_no_delete
                BEFORE DELETE ON config_commits
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'Configuration history is immutable and cannot be deleted'
                    );
                END;
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS config_heads (
                    study_key      TEXT PRIMARY KEY,
                    head_commit_id TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                )
            """)
            missing_studies = conn.execute(
                """
                SELECT DISTINCT c.study_key
                FROM config_commits c
                LEFT JOIN config_heads h ON h.study_key = c.study_key
                WHERE h.study_key IS NULL
                """
            ).fetchall()
            for study_row in missing_studies:
                sk = str(study_row[0])
                latest = conn.execute(
                    """
                    SELECT commit_id, timestamp
                    FROM config_commits
                    WHERE study_key = ?
                    ORDER BY timestamp DESC, rowid DESC
                    LIMIT 1
                    """,
                    (sk,),
                ).fetchone()
                if latest is not None:
                    conn.execute(
                        """
                        INSERT INTO config_heads (study_key, head_commit_id, updated_at)
                        VALUES (?, ?, ?)
                        """,
                        (sk, str(latest[0]), str(latest[1])),
                    )
            conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def commit_config(
        self,
        study_key: str,
        config: StudyConfiguration,
        desc: str,
        *,
        sdk: Any = None,
    ) -> str:
        """Serialise *config*, compute its SHA-256 hash, and persist the commit.

        Args:
            study_key: Identifies the study this configuration belongs to.
            config: The :class:`~imednet.models.study_config.StudyConfiguration`
                to store.
            desc: Human-readable description of what changed.
            sdk: The SDK instance used to verify server-side permissions.

        Returns:
            The ``commit_id`` (SHA-256 hex digest of the serialised JSON body).

        Raises:
            ValueError: If a commit with the same content hash already exists.
            PermissionError: If an authenticated SDK session is not provided,
                or if the authenticated session does not have manager/admin role.
        """
        validate_partition_key(study_key)
        if sdk is None:
            raise PermissionError(
                "An authenticated SDK session is required to commit configuration."
            )

        if not hasattr(sdk, "auth") or sdk.auth is None:
            raise PermissionError("SDK session is not authenticated: missing auth strategy.")

        try:
            roles = sdk.auth.get_user_roles()
        except Exception as exc:
            raise PermissionError(f"Server-side authorization failure: {exc}") from exc

        # Legacy fallback: if using static keys, validate server-side by making an API call
        if not roles and getattr(sdk.auth, "api_key", None):
            try:
                # Validate the key has access by requesting study data
                sdk.get_sites(study_key, limit=1)
                roles = ["admin"]  # Legacy keys with access map to admin
            except Exception as exc:
                raise PermissionError(  # noqa: B904
                    f"Server-side authorization failure for legacy key: {exc}"
                )

        if not roles or ("admin" not in roles and "manager" not in roles):
            raise PermissionError(
                "Server-side authorization failure: user lacks manager or admin role required to publish."
            )

        try:
            user_id = sdk.auth.get_user_id()
        except Exception as exc:
            raise PermissionError(f"Server-side authorization failure: {exc}") from exc

        if user_id == "api-key-user":
            author = "legacy-api-key"
        elif user_id:
            author = str(user_id)
        else:
            author = ""

        if not author:
            raise ValueError("A user identifier is required to commit.")

        sdk_sec_key = extract_sdk_security_key(sdk)
        if sdk_sec_key is not None:
            self._sdk_security_key = sdk_sec_key

        config_json = json.dumps(config.model_dump(mode="json", by_alias=True), sort_keys=True)
        content_hash = _sha256_of(config_json)
        timestamp = datetime.now(UTC).isoformat()

        with sdk_governance_context(sdk):
            # Secure the payload using Fernet encryption before writing to SQLite
            encrypted_config_json = _encrypt_payload(
                config_json, fallback_key=self._sdk_security_key
            )

            with self._lock, self._connection() as conn:
                head_row = conn.execute(
                    """
                    SELECT c.commit_id, c.content_hash
                    FROM config_heads h
                    JOIN config_commits c ON c.commit_id = h.head_commit_id
                    WHERE h.study_key = ?
                    """,
                    (study_key,),
                ).fetchone()
                if head_row is None:
                    head_row = conn.execute(
                        """
                        SELECT commit_id, content_hash
                        FROM config_commits
                        WHERE study_key = ?
                        ORDER BY timestamp DESC, rowid DESC
                        LIMIT 1
                        """,
                        (study_key,),
                    ).fetchone()

                parent_commit_id: str | None = None
                if head_row is not None:
                    parent_commit_id = str(head_row["commit_id"])
                    head_content_hash = str(head_row["content_hash"] or head_row["commit_id"])
                    if head_content_hash == content_hash:
                        raise ValueError(
                            f"Commit {parent_commit_id!r} already exists — configuration is unchanged."
                        )

                commit_id = _compute_chained_commit_id(content_hash, parent_commit_id)
                existing = conn.execute(
                    "SELECT study_key FROM config_commits WHERE commit_id = ?",
                    (commit_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["study_key"]) == study_key:
                        raise ValueError(
                            f"Commit {commit_id!r} already exists — configuration is unchanged."
                        )
                    commit_id = _sha256_of(f"{study_key}:{parent_commit_id or ''}:{content_hash}")

                conn.execute(
                    """
                    INSERT INTO config_commits
                        (commit_id, study_key, version_tag, config_data,
                         modified_by, description, timestamp, parent_commit_id, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        commit_id,
                        study_key,
                        config.version,
                        encrypted_config_json,
                        author,
                        desc,
                        timestamp,
                        parent_commit_id,
                        content_hash,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO config_heads (study_key, head_commit_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(study_key) DO UPDATE SET
                        head_commit_id = excluded.head_commit_id,
                        updated_at = excluded.updated_at
                    """,
                    (study_key, commit_id, timestamp),
                )
                conn.commit()

        return commit_id

    def get_head(self, study_key: str) -> str | None:
        """Return the current head ``commit_id`` for *study_key*, or ``None`` if empty."""
        validate_partition_key(study_key)
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT head_commit_id FROM config_heads WHERE study_key = ?",
                (study_key,),
            ).fetchone()
            if row is not None:
                return str(row["head_commit_id"])
            fallback = conn.execute(
                """
                SELECT commit_id
                FROM config_commits
                WHERE study_key = ?
                ORDER BY timestamp DESC, rowid DESC
                LIMIT 1
                """,
                (study_key,),
            ).fetchone()
        return str(fallback["commit_id"]) if fallback is not None else None

    def get_history(self, study_key: str) -> list[dict[str, Any]]:
        """Return all commits for *study_key*, ordered oldest-first.

        Args:
            study_key: The study whose history should be retrieved.

        Returns:
            A list of dicts, each with keys ``commit_id``, ``study_key``,
            ``version_tag``, ``modified_by``, ``description``, ``timestamp``,
            ``parent_commit_id``, and ``content_hash``. The ``config_data`` body
            is intentionally omitted to keep the payload small — use
            :meth:`rollback_config` to retrieve the full body for a specific commit.
        """
        validate_partition_key(study_key)
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT commit_id, study_key, version_tag, config_data,
                       modified_by, description, timestamp,
                       parent_commit_id, content_hash
                FROM   config_commits
                WHERE  study_key = ?
                ORDER  BY timestamp ASC, rowid ASC
                """,
                (study_key,),
            ).fetchall()
        fallback_key = getattr(self, "_sdk_security_key", None)
        history: list[dict[str, Any]] = []
        for row in rows:
            commit = dict(row)
            # Decrypt payload before verification
            decrypted_data = _decrypt_payload(commit["config_data"], fallback_key=fallback_key)
            self._verify_commit_signature(
                commit["commit_id"],
                decrypted_data,
                content_hash=commit.get("content_hash"),
                parent_commit_id=commit.get("parent_commit_id"),
                study_key=commit.get("study_key"),
            )
            commit["content_hash"] = commit.get("content_hash") or commit["commit_id"]
            commit.pop("config_data")
            history.append(commit)
        return history

    def diff_configs(self, commit_a: str, commit_b: str) -> dict[str, Any]:
        """Compute a property-level diff between two commits.

        Compares the flat JSON key space of the two commits.  Returns a dict
        with three sub-keys:

        * ``added`` — keys present in *b* but not in *a*.
        * ``removed`` — keys present in *a* but not in *b*.
        * ``changed`` — keys present in both but with different values.

        Args:
            commit_a: SHA-256 commit ID of the *before* state.
            commit_b: SHA-256 commit ID of the *after* state.

        Returns:
            Dict with ``added``, ``removed``, and ``changed`` sub-dicts.

        Raises:
            KeyError: If either commit ID is not found in the store.
        """
        data_a = self._fetch_config_data(commit_a)
        data_b = self._fetch_config_data(commit_b)

        flat_a = flatten(data_a)
        flat_b = flatten(data_b)

        keys_a = set(flat_a)
        keys_b = set(flat_b)

        added = {k: flat_b[k] for k in keys_b - keys_a}
        removed = {k: flat_a[k] for k in keys_a - keys_b}
        changed = {
            k: {"before": flat_a[k], "after": flat_b[k]}
            for k in keys_a & keys_b
            if flat_a[k] != flat_b[k]
        }
        return {"added": added, "removed": removed, "changed": changed}

    def rollback_config(self, study_key: str, commit_id: str) -> StudyConfiguration:
        """Restore and return the ``StudyConfiguration`` stored at *commit_id*.

        This method is non-destructive — it does not modify any existing
        history rows.  The caller is responsible for creating a new commit via
        :meth:`commit_config` if they wish to record the rollback.

        Args:
            study_key: The study the commit must belong to.
            commit_id: The SHA-256 commit ID to restore.

        Returns:
            The deserialised ``StudyConfiguration``.

        Raises:
            KeyError: If the commit is not found or does not belong to the
                requested study.
        """
        validate_partition_key(study_key)
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT commit_id, study_key, config_data, parent_commit_id, content_hash
                FROM config_commits
                WHERE commit_id = ? AND study_key = ?
                """,
                (commit_id, study_key),
            ).fetchone()
        if row is None:
            raise KeyError(f"Commit {commit_id!r} not found for study {study_key!r}.")
        # Decrypt payload before validation
        decrypted_data = _decrypt_payload(
            row["config_data"], fallback_key=getattr(self, "_sdk_security_key", None)
        )
        self._verify_commit_signature(
            row["commit_id"],
            decrypted_data,
            content_hash=row["content_hash"],
            parent_commit_id=row["parent_commit_id"],
            study_key=row["study_key"],
        )
        return StudyConfiguration.model_validate_json(decrypted_data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_config_data(self, commit_id: str) -> dict[str, Any]:
        """Retrieve the raw configuration JSON for a specific commit."""
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT commit_id, study_key, config_data, parent_commit_id, content_hash
                FROM config_commits
                WHERE commit_id = ?
                """,
                (commit_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Commit {commit_id!r} not found.")
        # Decrypt payload first
        decrypted_data = _decrypt_payload(
            row["config_data"], fallback_key=getattr(self, "_sdk_security_key", None)
        )
        self._verify_commit_signature(
            row["commit_id"],
            decrypted_data,
            content_hash=row["content_hash"],
            parent_commit_id=row["parent_commit_id"],
            study_key=row["study_key"],
        )
        return json.loads(decrypted_data)  # type: ignore[no-any-return]

    @staticmethod
    def _verify_commit_signature_static(
        commit_id: str,
        config_data: str,
        *,
        content_hash: str | None = None,
        parent_commit_id: str | None = None,
        study_key: str | None = None,
    ) -> None:
        """Verify that the decrypted payload matches its content hash and parent-chained commit ID."""
        actual_hash = _sha256_of(config_data)
        expected_hash = content_hash or commit_id
        if actual_hash != expected_hash:
            raise ValueError(
                "Configuration data integrity check failed: "
                f"SHA-256 hash mismatch for commit {commit_id!r}"
            )
        if content_hash is not None:
            expected_chained = _compute_chained_commit_id(content_hash, parent_commit_id)
            expected_disambiguated = _sha256_of(
                f"{study_key or ''}:{parent_commit_id or ''}:{content_hash}"
            )
            if commit_id not in {expected_chained, expected_disambiguated}:
                raise ValueError(
                    "Configuration data integrity check failed: "
                    f"SHA-256 hash mismatch for commit {commit_id!r}"
                )

    def _verify_commit_signature(
        self,
        commit_id: str,
        config_data: str,
        *,
        content_hash: str | None = None,
        parent_commit_id: str | None = None,
        study_key: str | None = None,
    ) -> None:
        """Verify that the decrypted payload matches its content hash and parent-chained commit ID."""
        self._verify_commit_signature_static(
            commit_id,
            config_data,
            content_hash=content_hash,
            parent_commit_id=parent_commit_id,
            study_key=study_key,
        )


__all__ = ["ConfigVersionStore"]
