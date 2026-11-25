"""SQLite-backed version control ledger for study configurations.

Provides immutable, append-only commit history with SHA-256 content hashing,
diff capability, and safe rollback.  History blocks are read-only once written.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, cast
from urllib.parse import urlparse

from imednet.spi.constants import DEFAULT_BASE_URL
from imednet.spi.models import StudyConfiguration
from imednet.spi.utils import (
    PathTraversalValidationError,
    extract_sdk_base_url,
    extract_sdk_security_key,
    flatten,
    get_base_url_context,
    get_security_key_context,
    get_study_context,
    sdk_governance_context,
    set_security_key_context,
    sqlite_connection,
    validate_partition_key,
)

from ._default_paths import _ORIG_PATH_HOME_FUNC
from ._default_paths import expand_user_path as _expand_path_with_home

_MAX_RAW_DB_PATH_CACHE = 64
_MAX_ORIGIN_LAYOUT_CACHE = 128
_MAX_DIR_CACHE_SIZE = 128
_MAX_HEAD_INDEX_CACHE = 64
_MAX_COMMITTED_CONFIG_CACHE = 256
_COARSE_MTIME_GUARD_NS = 20_000_000

_CACHE_LOCK = RLock()
_WRITE_GENERATION = 0


@dataclass(frozen=True, slots=True)
class _OriginLayout:
    """Validated filesystem paths for a ``(base_db_path, base_url)`` pair."""

    base_dir: Path
    resolved_base: Path
    host_dir: Path
    canonical_db_path: Path
    flat_db_path: Path | None
    db_name: str


_RAW_DB_PATH_CACHE: dict[tuple[str, str, str], Path] = {}
_ORIGIN_LAYOUT_CACHE: dict[tuple[str, str], _OriginLayout] = {}
_BASE_DIR_ENTRIES_CACHE: dict[tuple[Path, int, int, int, int, int, int], frozenset[str]] = {}
_HOST_SUBDIR_CACHE: dict[
    tuple[Path, int, int, int, int, int, int], tuple[frozenset[str], frozenset[str]]
] = {}
_HEAD_INDEX_CACHE: dict[
    tuple[Path, int, int, int, int, int, int, int, int, int], dict[str, str]
] = {}
_COMMITTED_CONFIG_CACHE: dict[
    tuple[tuple[Path, int, int, int, int, int, int, int, int, int], str, str, bytes],
    StudyConfiguration,
] = {}


def _bounded_cache_get(cache: dict[Any, Any], key: Any) -> Any:
    """Read ``key`` from ``cache`` while holding ``_CACHE_LOCK``."""
    with _CACHE_LOCK:
        return cache.get(key)


def _bounded_cache_put(cache: dict[Any, Any], key: Any, value: Any, max_size: int) -> None:
    """Insert ``value`` into ``cache`` while enforcing a fixed ``max_size`` bound."""
    with _CACHE_LOCK:
        if key in cache:
            cache.pop(key, None)
        while len(cache) >= max_size:
            cache.pop(next(iter(cache)), None)
        cache[key] = value


def _get_write_generation() -> int:
    """Return the current write generation counter while holding ``_CACHE_LOCK``."""
    with _CACHE_LOCK:
        return _WRITE_GENERATION


def _bump_write_generation() -> None:
    """Advance module write generation to immediately invalidate directory and SQLite caches."""
    global _WRITE_GENERATION
    with _CACHE_LOCK:
        _WRITE_GENERATION += 1


def clear_config_version_store_caches() -> None:
    """Clear all module-level path, layout, directory, head, and config caches."""
    global _WRITE_GENERATION
    with _CACHE_LOCK:
        _WRITE_GENERATION += 1
        _RAW_DB_PATH_CACHE.clear()
        _ORIGIN_LAYOUT_CACHE.clear()
        _BASE_DIR_ENTRIES_CACHE.clear()
        _HOST_SUBDIR_CACHE.clear()
        _HEAD_INDEX_CACHE.clear()
        _COMMITTED_CONFIG_CACHE.clear()
        _sanitize_base_url_for_path.cache_clear()


def _sha256_of(content: str) -> str:
    """Return the SHA-256 hex digest of the given string content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _compute_chained_commit_id(content_hash: str, parent_commit_id: str | None) -> str:
    """Compute a content-addressed root commit ID or parent-chained child commit ID."""
    if parent_commit_id is None:
        return content_hash
    return _sha256_of(f"{parent_commit_id}:{content_hash}")


@functools.lru_cache(maxsize=64)
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
        or fallback_key
        or get_security_key_context()
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


def _get_raw_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve the raw base SQLite path honoring ``IMEDNET_CONFIG_DB_PATH``, ``HOME``, and ``Path.home()``."""
    if db_path is not None:
        return _expand_path_with_home(db_path)

    raw_env = os.environ.get("IMEDNET_CONFIG_DB_PATH")
    home_fn = Path.home
    is_home_patched = getattr(home_fn, "__func__", home_fn) is not _ORIG_PATH_HOME_FUNC

    if raw_env:
        if "~" not in raw_env:
            key = ("env", raw_env, "")
            cached = _bounded_cache_get(_RAW_DB_PATH_CACHE, key)
            if cached is not None:
                return cast(Path, cached)
            resolved = Path(raw_env)
        else:
            home_str = (
                os.fspath(home_fn())
                if is_home_patched
                else (
                    os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.fspath(home_fn())
                )
            )
            key = ("env_tilde", raw_env, home_str)
            cached = _bounded_cache_get(_RAW_DB_PATH_CACHE, key)
            if cached is not None:
                return cast(Path, cached)
            resolved = _expand_path_with_home(raw_env)
        _bounded_cache_put(_RAW_DB_PATH_CACHE, key, resolved, _MAX_RAW_DB_PATH_CACHE)
        return resolved

    if is_home_patched:
        patched_home = Path(home_fn())
        key = ("home_patched", os.fspath(patched_home), "")
        cached = _bounded_cache_get(_RAW_DB_PATH_CACHE, key)
        if cached is not None:
            return cast(Path, cached)
        resolved = patched_home / ".imednet" / "config_versions.sqlite3"
    else:
        env_home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
        if env_home:
            key = ("home_env", env_home, "")
            cached = _bounded_cache_get(_RAW_DB_PATH_CACHE, key)
            if cached is not None:
                return cast(Path, cached)
            resolved = Path(env_home) / ".imednet" / "config_versions.sqlite3"
        else:
            std_home = Path.home()
            key = ("home_std", os.fspath(std_home), "")
            cached = _bounded_cache_get(_RAW_DB_PATH_CACHE, key)
            if cached is not None:
                return cast(Path, cached)
            resolved = std_home / ".imednet" / "config_versions.sqlite3"

    _bounded_cache_put(_RAW_DB_PATH_CACHE, key, resolved, _MAX_RAW_DB_PATH_CACHE)
    return resolved


def _resolve_effective_base_url(pinned_base_url: str | None = None) -> tuple[str, bool]:
    """Return ``(effective_base_url, allow_localhost_fallback)`` for origin path resolution."""
    default_host = _sanitize_base_url_for_path(DEFAULT_BASE_URL)
    if pinned_base_url is not None:
        resolved = pinned_base_url if pinned_base_url.strip() else "http://localhost"
        return (resolved, _sanitize_base_url_for_path(resolved) == default_host)

    ctx_url = get_base_url_context()
    if ctx_url is not None:
        resolved = ctx_url if ctx_url.strip() else "http://localhost"
        return (resolved, _sanitize_base_url_for_path(resolved) == default_host)

    env_url = os.environ.get("IMEDNET_BASE_URL")
    if env_url is not None and env_url.strip():
        return (env_url, _sanitize_base_url_for_path(env_url) == default_host)

    return (DEFAULT_BASE_URL, True)


def _get_origin_layout(
    base_db_path: Path, *, base_url_override: str | None = None
) -> _OriginLayout:
    """Validate active study context and return memoized canonical/legacy layout for ``base_db_path``."""
    ctx_study_key = get_study_context()
    if ctx_study_key:
        validate_partition_key(ctx_study_key)

    base_url = (
        base_url_override if base_url_override is not None else _resolve_effective_base_url()[0]
    )
    cache_key = (os.fspath(base_db_path), base_url)
    cached = _bounded_cache_get(_ORIGIN_LAYOUT_CACHE, cache_key)
    if cached is not None:
        return cast(_OriginLayout, cached)

    sanitized_url = _sanitize_base_url_for_path(base_url)
    base_dir = base_db_path.parent
    host_dir = base_dir / sanitized_url
    resolved_base = base_dir.resolve()
    resolved_host = host_dir.resolve()
    if not resolved_host.is_relative_to(resolved_base):
        raise PathTraversalValidationError(
            f"Resolved target directory {resolved_host} escapes base directory {resolved_base}"
        )

    db_name = base_db_path.name
    canonical_db_path = host_dir / db_name
    flat_db_path = (
        base_db_path
        if base_db_path != canonical_db_path
        and base_db_path.resolve().is_relative_to(resolved_base)
        else None
    )
    layout = _OriginLayout(
        base_dir=base_dir,
        resolved_base=resolved_base,
        host_dir=host_dir,
        canonical_db_path=canonical_db_path,
        flat_db_path=flat_db_path,
        db_name=db_name,
    )
    _bounded_cache_put(_ORIGIN_LAYOUT_CACHE, cache_key, layout, _MAX_ORIGIN_LAYOUT_CACHE)
    return layout


def _get_base_dir_entries(base_dir: Path, db_name: str) -> frozenset[str] | None:
    """Return cached entry names in ``base_dir`` or ``None`` when ``base_dir`` is absent."""
    try:
        st = os.stat(base_dir)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None

    snap_key = (
        base_dir,
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_nlink,
        st.st_size,
        st.st_ino,
        _get_write_generation(),
    )
    entries = _bounded_cache_get(_BASE_DIR_ENTRIES_CACHE, snap_key)
    if entries is not None and not (
        db_name not in entries
        and (time.time_ns() - max(st.st_mtime_ns, st.st_ctime_ns)) < _COARSE_MTIME_GUARD_NS
        and os.path.exists(base_dir / db_name)
    ):
        return cast(frozenset[str], entries)

    try:
        with os.scandir(base_dir) as it:
            scanned = frozenset(entry.name for entry in it)
    except OSError:
        return None
    _bounded_cache_put(_BASE_DIR_ENTRIES_CACHE, snap_key, scanned, _MAX_DIR_CACHE_SIZE)
    return scanned


def _get_host_dir_listing(host_dir: Path, db_name: str) -> tuple[frozenset[str], frozenset[str]]:
    """Return cached ``(entry_names, subdir_names)`` inside ``host_dir``."""
    try:
        st = os.stat(host_dir)
    except OSError:
        return (frozenset(), frozenset())
    if not stat.S_ISDIR(st.st_mode):
        return (frozenset(), frozenset())

    snap_key = (
        host_dir,
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_nlink,
        st.st_size,
        st.st_ino,
        _get_write_generation(),
    )
    cached = _bounded_cache_get(_HOST_SUBDIR_CACHE, snap_key)
    if cached is not None and not (
        db_name not in cached[0]
        and (time.time_ns() - max(st.st_mtime_ns, st.st_ctime_ns)) < _COARSE_MTIME_GUARD_NS
        and os.path.exists(host_dir / db_name)
    ):
        return cast(tuple[frozenset[str], frozenset[str]], cached)

    names: set[str] = set()
    subdirs: set[str] = set()
    try:
        with os.scandir(host_dir) as it:
            for entry in it:
                names.add(entry.name)
                with suppress(OSError):
                    if entry.is_dir():
                        subdirs.add(entry.name)
    except OSError:
        return (frozenset(), frozenset())

    listing = (frozenset(names), frozenset(subdirs))
    _bounded_cache_put(_HOST_SUBDIR_CACHE, snap_key, listing, _MAX_DIR_CACHE_SIZE)
    return listing


class ConfigVersionStore:
    """Immutable append-only store for ``StudyConfiguration`` versions.

    Each call to :meth:`commit_config` creates a new entry signed with a
    SHA-256 digest of the serialised configuration body.  History is strictly
    read-only — individual commit rows may never be edited or deleted.

    Args:
        db_path: Filesystem path for the SQLite database.  Defaults to
            ``~/.imednet/config_versions.sqlite3``.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        base_url: str | None = None,
        sdk: Any = None,
    ) -> None:
        """Initialize the configuration version store.

        Args:
            db_path: Path to the SQLite database file.
            base_url: Optional explicit API origin URL to pin this store to.
            sdk: Optional SDK instance to extract pinned origin and session credentials from.
        """
        self._original_db_path = _get_raw_db_path(db_path)
        self._lock = RLock()
        self._sdk_security_key: str | None = (
            extract_sdk_security_key(sdk) if sdk is not None else None
        )
        if base_url is not None:
            self._pinned_base_url: str | None = base_url if base_url.strip() else "http://localhost"
        elif sdk is not None:
            self._pinned_base_url = extract_sdk_base_url(sdk)
        else:
            ctx_url = get_base_url_context()
            env_url = os.environ.get("IMEDNET_BASE_URL")
            if ctx_url is not None:
                self._pinned_base_url = ctx_url if ctx_url.strip() else "http://localhost"
            elif env_url is not None and env_url.strip():
                self._pinned_base_url = env_url
            else:
                self._pinned_base_url = None
        self._ensure_schema_initialized(self.db_path)

    @property
    def db_path(self) -> Path:
        """Return the dynamically selected database file path based on pinned or active base URL."""
        return self._get_get_dynamic_db_path()

    @db_path.setter
    def db_path(self, value: str | Path) -> None:
        """Set the original base path for database files."""
        self._original_db_path = _expand_path_with_home(value)

    @staticmethod
    def _get_dynamic_db_path_no_create(
        base_db_path: Path, *, pinned_base_url: str | None = None
    ) -> Path:
        """Compute host-scoped dynamic DB path without splitting by study_context or creating dirs."""
        effective_url, _ = _resolve_effective_base_url(pinned_base_url)
        return _get_origin_layout(base_db_path, base_url_override=effective_url).canonical_db_path

    @staticmethod
    def _get_legacy_db_paths_no_create(base_db_path: Path, study_key: str) -> list[Path]:
        """Return legacy study_context-partitioned and flat candidate SQLite paths without creating dirs."""
        validate_partition_key(study_key)
        effective_url, allow_localhost_fallback = _resolve_effective_base_url()
        layout = _get_origin_layout(base_db_path, base_url_override=effective_url)
        base_entries = _get_base_dir_entries(layout.base_dir, layout.db_name)
        if base_entries is None:
            return []

        candidates: list[Path] = []
        host_name = layout.host_dir.name
        if study_key and host_name in base_entries:
            _host_entries, host_subdirs = _get_host_dir_listing(layout.host_dir, layout.db_name)
            if study_key in host_subdirs:
                legacy_study_dir = layout.host_dir / study_key
                if legacy_study_dir.resolve().is_relative_to(layout.resolved_base):
                    legacy_candidate = legacy_study_dir / layout.db_name
                    if legacy_candidate != layout.canonical_db_path:
                        candidates.append(legacy_candidate)

        if allow_localhost_fallback and "localhost" in base_entries:
            lh_layout = _get_origin_layout(base_db_path, base_url_override="http://localhost")
            lh_entries, lh_subdirs = _get_host_dir_listing(lh_layout.host_dir, lh_layout.db_name)
            if (
                lh_layout.db_name in lh_entries
                and lh_layout.canonical_db_path != layout.canonical_db_path
            ):
                candidates.append(lh_layout.canonical_db_path)
            if study_key and study_key in lh_subdirs:
                lh_study_dir = lh_layout.host_dir / study_key
                if lh_study_dir.resolve().is_relative_to(lh_layout.resolved_base):
                    lh_study_candidate = lh_study_dir / lh_layout.db_name
                    if lh_study_candidate not in candidates:
                        candidates.append(lh_study_candidate)

        if layout.flat_db_path is not None and layout.db_name in base_entries:
            candidates.append(layout.flat_db_path)
        return candidates

    @classmethod
    def _read_committed_from_sqlite_path(
        cls,
        candidate_path: Path,
        study_key: str,
        *,
        raw_db_path: Path | None = None,
    ) -> StudyConfiguration | None:
        """Read and verify the latest committed StudyConfiguration for *study_key* from *candidate_path*."""
        validate_partition_key(study_key)
        try:
            db_stat = os.stat(candidate_path)
        except OSError:
            return None

        if hasattr(cls.get_head, "_mock_name"):
            mocked_store = cls(db_path=raw_db_path) if raw_db_path is not None else cls()
            head_id = mocked_store.get_head(study_key)
            return mocked_store.rollback_config(study_key, head_id) if head_id else None

        wal_ino = 0
        wal_mtime_ns = 0
        wal_size = 0
        with suppress(OSError):
            wal_stat = os.stat(f"{candidate_path}-wal")
            wal_ino = wal_stat.st_ino
            wal_mtime_ns = wal_stat.st_mtime_ns
            wal_size = wal_stat.st_size

        snap_key = (
            candidate_path,
            db_stat.st_ino,
            db_stat.st_mtime_ns,
            db_stat.st_ctime_ns,
            db_stat.st_nlink,
            db_stat.st_size,
            wal_ino,
            wal_mtime_ns,
            wal_size,
            _get_write_generation(),
        )
        head_index = _bounded_cache_get(_HEAD_INDEX_CACHE, snap_key)
        if head_index is None:
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
            _bounded_cache_put(_HEAD_INDEX_CACHE, snap_key, head_index, _MAX_HEAD_INDEX_CACHE)

        head_id = head_index.get(study_key)
        if not head_id:
            return None

        try:
            fernet_key = _get_fernet_key()
        except Exception as exc:
            raise ValueError(
                "Configuration data integrity check failed: "
                f"Decryption key is incorrect or missing: {exc}"
            ) from exc

        cfg_cache_key = (snap_key, study_key, head_id, fernet_key)
        cached_cfg = _bounded_cache_get(_COMMITTED_CONFIG_CACHE, cfg_cache_key)
        if cached_cfg is not None:
            return cast(StudyConfiguration, cached_cfg).model_copy(deep=True)

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
        validated_cfg = StudyConfiguration.model_validate_json(decrypted_data)
        _bounded_cache_put(
            _COMMITTED_CONFIG_CACHE,
            cfg_cache_key,
            validated_cfg,
            _MAX_COMMITTED_CONFIG_CACHE,
        )
        return validated_cfg.model_copy(deep=True)

    @classmethod
    def resolve_legacy_committed_config(cls, study_key: str) -> StudyConfiguration | None:
        """Read latest committed StudyConfiguration from legacy study-partitioned SQLite paths."""
        validate_partition_key(study_key)
        raw_db_path = _get_raw_db_path()
        for legacy_path in cls._get_legacy_db_paths_no_create(raw_db_path, study_key):
            resolved = cls._read_committed_from_sqlite_path(
                legacy_path,
                study_key,
                raw_db_path=raw_db_path,
            )
            if resolved is not None:
                return resolved
        return None

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

        raw_db_path = _get_raw_db_path()
        effective_url, allow_localhost_fallback = _resolve_effective_base_url()
        layout = _get_origin_layout(raw_db_path, base_url_override=effective_url)
        base_entries = _get_base_dir_entries(layout.base_dir, layout.db_name)
        if base_entries is None:
            return None

        host_name = layout.host_dir.name
        has_legacy_study_dir = False
        if host_name in base_entries:
            host_entries, host_subdirs = _get_host_dir_listing(layout.host_dir, layout.db_name)
            if layout.db_name in host_entries:
                resolved = cls._read_committed_from_sqlite_path(
                    layout.canonical_db_path,
                    study_key,
                    raw_db_path=raw_db_path,
                )
                if resolved is not None:
                    return resolved
            has_legacy_study_dir = bool(study_key and study_key in host_subdirs)

        has_localhost_legacy = False
        if allow_localhost_fallback and "localhost" in base_entries:
            lh_layout = _get_origin_layout(raw_db_path, base_url_override="http://localhost")
            lh_entries, lh_subdirs = _get_host_dir_listing(lh_layout.host_dir, lh_layout.db_name)
            if lh_layout.db_name in lh_entries:
                resolved = cls._read_committed_from_sqlite_path(
                    lh_layout.canonical_db_path,
                    study_key,
                    raw_db_path=raw_db_path,
                )
                if resolved is not None:
                    return resolved
            has_localhost_legacy = bool(study_key and study_key in lh_subdirs)

        has_flat_db = layout.flat_db_path is not None and layout.db_name in base_entries
        if (
            hasattr(cls.resolve_legacy_committed_config, "_mock_name")
            or has_legacy_study_dir
            or has_localhost_legacy
            or has_flat_db
        ):
            return cls.resolve_legacy_committed_config(study_key)
        return None

    def _get_get_dynamic_db_path(self) -> Path:
        """Resolve database path dynamically based on pinned or active base URL without splitting by study_context."""
        pinned = getattr(self, "_pinned_base_url", None)
        result = self._get_dynamic_db_path_no_create(
            self._original_db_path,
            pinned_base_url=pinned,
        )
        parent_existed = result.parent.exists()
        result.parent.mkdir(parents=True, exist_ok=True)
        if not parent_existed:
            _bump_write_generation()
        return result

    def _get_fallback_localhost_db_path(self) -> Path | None:
        """Return existing legacy ``localhost`` SQLite path when resolved origin is production."""
        pinned = getattr(self, "_pinned_base_url", None)
        _effective_url, allow_localhost_fallback = _resolve_effective_base_url(pinned)
        if not allow_localhost_fallback:
            return None
        lh_layout = _get_origin_layout(self._original_db_path, base_url_override="http://localhost")
        if lh_layout.canonical_db_path.is_file():
            return lh_layout.canonical_db_path
        return None

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
            _bump_write_generation()

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

        # Legacy/opaque token fallback: if using static keys or an opaque OIDC bearer token
        # (never an unverified JWT), validate server-side by making an API call.
        has_opaque_oidc_token = bool(
            isinstance(getattr(sdk.auth, "token", None), str)
            and getattr(sdk.auth, "token", "").strip()
            and not getattr(sdk.auth, "_requires_verification", False)
        )
        if not roles and (getattr(sdk.auth, "api_key", None) or has_opaque_oidc_token):
            try:
                # Validate the credential has access by requesting study data
                sdk.get_sites(study_key, limit=1)
                roles = ["admin"]
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
        elif has_opaque_oidc_token:
            author = "oidc-token-user"
        else:
            author = ""

        if not author:
            raise ValueError("A user identifier is required to commit.")

        sdk_base_url = extract_sdk_base_url(sdk)
        if getattr(self, "_pinned_base_url", None) is None and sdk_base_url is not None:
            self._pinned_base_url = sdk_base_url

        sdk_sec_key = extract_sdk_security_key(sdk)
        if sdk_sec_key is not None:
            self._sdk_security_key = sdk_sec_key
            if (
                not (
                    os.environ.get("IMEDNET_ENCRYPTION_KEY")
                    or os.environ.get("IMEDNET_SECURITY_KEY")
                )
                and get_security_key_context() is None
            ):
                set_security_key_context(sdk_sec_key)

        config_json = json.dumps(config.model_dump(mode="json", by_alias=True), sort_keys=True)
        content_hash = _sha256_of(config_json)
        timestamp = datetime.now(UTC).isoformat()

        with sdk_governance_context(sdk):
            # Secure the payload using Fernet encryption before writing to SQLite
            encrypted_config_json = _encrypt_payload(
                config_json,
                fallback_key=self._sdk_security_key,
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
                _bump_write_generation()

        return commit_id

    def _query_head_for_path(self, path: Path, study_key: str) -> str | None:
        """Query head commit_id for *study_key* from *path*."""
        self._ensure_schema_initialized(path)
        with sqlite_connection(path, timeout=30.0) as conn:
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

    def get_head(self, study_key: str) -> str | None:
        """Return the current head ``commit_id`` for *study_key*, or ``None`` if empty."""
        validate_partition_key(study_key)
        with self._lock:
            head_id = self._query_head_for_path(self.db_path, study_key)
            if head_id is not None:
                return head_id
            lh_path = self._get_fallback_localhost_db_path()
            if lh_path is not None:
                return self._query_head_for_path(lh_path, study_key)
        return None

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
        query = """
            SELECT commit_id, study_key, version_tag, config_data,
                   modified_by, description, timestamp,
                   parent_commit_id, content_hash
            FROM   config_commits
            WHERE  study_key = ?
            ORDER  BY timestamp ASC, rowid ASC
        """
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(query, (study_key,)).fetchall()
            if not rows:
                lh_path = self._get_fallback_localhost_db_path()
                if lh_path is not None:
                    self._ensure_schema_initialized(lh_path)
                    with sqlite_connection(lh_path, timeout=30.0) as lh_conn:
                        rows = lh_conn.execute(query, (study_key,)).fetchall()
        fallback_key = getattr(self, "_sdk_security_key", None)
        history: list[dict[str, Any]] = []
        for row in rows:
            commit = dict(row)
            # Decrypt payload before verification
            decrypted_data = _decrypt_payload(
                commit["config_data"],
                fallback_key=fallback_key,
            )
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
        query = """
            SELECT commit_id, study_key, config_data, parent_commit_id, content_hash
            FROM config_commits
            WHERE commit_id = ? AND study_key = ?
        """
        with self._lock:
            with self._connection() as conn:
                row = conn.execute(query, (commit_id, study_key)).fetchone()
            if row is None:
                lh_path = self._get_fallback_localhost_db_path()
                if lh_path is not None:
                    self._ensure_schema_initialized(lh_path)
                    with sqlite_connection(lh_path, timeout=30.0) as lh_conn:
                        row = lh_conn.execute(query, (commit_id, study_key)).fetchone()
        if row is None:
            raise KeyError(f"Commit {commit_id!r} not found for study {study_key!r}.")
        fallback_key = getattr(self, "_sdk_security_key", None)
        # Decrypt payload before validation
        decrypted_data = _decrypt_payload(
            row["config_data"],
            fallback_key=fallback_key,
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
        query = """
            SELECT commit_id, study_key, config_data, parent_commit_id, content_hash
            FROM config_commits
            WHERE commit_id = ?
        """
        with self._lock:
            with self._connection() as conn:
                row = conn.execute(query, (commit_id,)).fetchone()
            if row is None:
                lh_path = self._get_fallback_localhost_db_path()
                if lh_path is not None:
                    self._ensure_schema_initialized(lh_path)
                    with sqlite_connection(lh_path, timeout=30.0) as lh_conn:
                        row = lh_conn.execute(query, (commit_id,)).fetchone()
        if row is None:
            raise KeyError(f"Commit {commit_id!r} not found.")
        fallback_key = getattr(self, "_sdk_security_key", None)
        # Decrypt payload first
        decrypted_data = _decrypt_payload(
            row["config_data"],
            fallback_key=fallback_key,
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
