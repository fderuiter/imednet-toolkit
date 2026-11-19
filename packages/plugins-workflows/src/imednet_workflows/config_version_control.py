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
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from imednet.spi.models import StudyConfiguration
from imednet.spi.utils import flatten, sqlite_connection

_DEFAULT_DB_PATH = Path(
    os.environ.get("IMEDNET_CONFIG_DB_PATH", Path.home() / ".imednet" / "config_versions.sqlite3")
)


def _sha256_of(content: str) -> str:
    """Return the SHA-256 hex digest of the given string content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


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


def _get_fernet_key() -> bytes:
    """Retrieve and derive a valid 32-byte base64-encoded Fernet key."""
    raw_key = os.environ.get("IMEDNET_ENCRYPTION_KEY") or os.environ.get("IMEDNET_SECURITY_KEY")
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


def _encrypt_payload(payload: str) -> str:
    """Encrypt payload string using AES-256 Fernet encryption."""
    key = _get_fernet_key()
    f = Fernet(key)
    return f.encrypt(payload.encode("utf-8")).decode("utf-8")


def _decrypt_payload(encrypted_payload: str) -> str:
    """Decrypt Fernet encrypted payload or raise clear ValueError if key is incorrect or missing."""
    try:
        key = _get_fernet_key()
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

    def __init__(self, db_path: str | Path = _DEFAULT_DB_PATH) -> None:
        """Initialize the configuration version store.

        Args:
            db_path: Path to the SQLite database file.
        """
        self._original_db_path = Path(db_path).expanduser()
        self._lock = RLock()
        self._ensure_schema_initialized(self.db_path)

    @property
    def db_path(self) -> Path:
        """Return the dynamically selected database file path based on environment and context."""
        return self._get_get_dynamic_db_path()

    @db_path.setter
    def db_path(self, value: str | Path) -> None:
        """Set the original base path for database files."""
        self._original_db_path = Path(value).expanduser()

    def _get_get_dynamic_db_path(self) -> Path:
        """Resolve database path dynamically based on active base URL and study key context."""
        from imednet.spi.utils import (
            get_base_url_context,
            get_study_context,
        )

        from ._spi_compat import PathTraversalValidationError, validate_partition_key

        base_url = (
            get_base_url_context() or os.environ.get("IMEDNET_BASE_URL") or "http://localhost"
        )
        sanitized_url = _sanitize_base_url_for_path(base_url)
        base_dir = self._original_db_path.parent

        ctx_study_key = get_study_context()
        if ctx_study_key:
            validate_partition_key(ctx_study_key)
            target_dir = base_dir / sanitized_url / ctx_study_key
        else:
            target_dir = base_dir / sanitized_url

        resolved_base = base_dir.resolve()
        resolved_target = target_dir.resolve()
        if not resolved_target.is_relative_to(resolved_base):
            raise PathTraversalValidationError(
                f"Resolved target directory {resolved_target} escapes base directory {resolved_base}"
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / self._original_db_path.name

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
        """Create the database tables and triggers if they do not exist for the given path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite_connection(path, timeout=30.0) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS config_commits (
                    commit_id   TEXT PRIMARY KEY,
                    study_key   TEXT NOT NULL,
                    version_tag TEXT NOT NULL,
                    config_data TEXT NOT NULL,
                    modified_by TEXT NOT NULL,
                    description TEXT NOT NULL,
                    timestamp   TEXT NOT NULL
                )
            """)
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
        if not roles and hasattr(sdk.auth, "api_key") and sdk.auth.api_key:
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

        config_json = json.dumps(config.model_dump(mode="json", by_alias=True), sort_keys=True)
        commit_id = _sha256_of(config_json)
        timestamp = datetime.now(UTC).isoformat()

        # Secure the payload using Fernet encryption before writing to SQLite
        encrypted_config_json = _encrypt_payload(config_json)

        with self._lock, self._connection() as conn:
            existing = conn.execute(
                "SELECT 1 FROM config_commits WHERE commit_id = ?", (commit_id,)
            ).fetchone()
            if existing:
                raise ValueError(
                    f"Commit {commit_id!r} already exists — configuration is unchanged."
                )
            conn.execute(
                """
                INSERT INTO config_commits
                    (commit_id, study_key, version_tag, config_data,
                     modified_by, description, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_id,
                    study_key,
                    config.version,
                    encrypted_config_json,
                    author,
                    desc,
                    timestamp,
                ),
            )
            conn.commit()

        return commit_id

    def get_history(self, study_key: str) -> list[dict[str, Any]]:
        """Return all commits for *study_key*, ordered oldest-first.

        Args:
            study_key: The study whose history should be retrieved.

        Returns:
            A list of dicts, each with keys ``commit_id``, ``study_key``,
            ``version_tag``, ``modified_by``, ``description``, and
            ``timestamp``.  The ``config_data`` body is intentionally omitted
            to keep the payload small — use :meth:`rollback_config` to
            retrieve the full body for a specific commit.
        """
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT commit_id, study_key, version_tag, config_data,
                       modified_by, description, timestamp
                FROM   config_commits
                WHERE  study_key = ?
                ORDER  BY timestamp ASC
                """,
                (study_key,),
            ).fetchall()
        history: list[dict[str, Any]] = []
        for row in rows:
            commit = dict(row)
            # Decrypt payload before verification
            decrypted_data = _decrypt_payload(commit["config_data"])
            self._verify_commit_signature(commit["commit_id"], decrypted_data)
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
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT commit_id, config_data
                FROM config_commits
                WHERE commit_id = ? AND study_key = ?
                """,
                (commit_id, study_key),
            ).fetchone()
        if row is None:
            raise KeyError(f"Commit {commit_id!r} not found for study {study_key!r}.")
        # Decrypt payload before validation
        decrypted_data = _decrypt_payload(row["config_data"])
        self._verify_commit_signature(row["commit_id"], decrypted_data)
        return StudyConfiguration.model_validate_json(decrypted_data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_config_data(self, commit_id: str) -> dict[str, Any]:
        """Retrieve the raw configuration JSON for a specific commit."""
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT commit_id, config_data FROM config_commits WHERE commit_id = ?",
                (commit_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Commit {commit_id!r} not found.")
        # Decrypt payload first
        decrypted_data = _decrypt_payload(row["config_data"])
        self._verify_commit_signature(row["commit_id"], decrypted_data)
        return json.loads(decrypted_data)  # type: ignore[no-any-return]

    def _verify_commit_signature(self, commit_id: str, config_data: str) -> None:
        """Verify that the content hash matches the stored commit ID."""
        if _sha256_of(config_data) != commit_id:
            raise ValueError(
                "Configuration data integrity check failed: "
                f"SHA-256 hash mismatch for commit {commit_id!r}"
            )


__all__ = ["ConfigVersionStore"]
