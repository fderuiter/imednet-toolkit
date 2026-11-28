"""Unit tests for triage store."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from imednet.models.triage import TriageAnnotation, TriageHistoryEntry, TriageItem, TriageStatus
from imednet_workflows.triage_store import TriageStore


def _seed_item(item_id: str = "AE-1") -> TriageItem:
    """Helper function to  seed item."""
    return TriageItem(
        item_id=item_id,
        study_key="STUDY-A",
        status=TriageStatus.NEW,
        assignee="triager",
        severity="critical",
        annotations=[
            TriageAnnotation(
                annotation_id=f"note-{item_id}",
                user_id="triager",
                comment="initial note",
                timestamp=datetime.now(timezone.utc) - timedelta(hours=96),
            )
        ],
        history=[
            TriageHistoryEntry(
                transition_id=f"hist-{item_id}",
                from_status=TriageStatus.NEW,
                to_status=TriageStatus.NEW,
                user_id="triager",
                comment="created",
                timestamp=datetime.now(timezone.utc) - timedelta(hours=95),
            )
        ],
    )


def test_triage_store_enables_wal_mode(tmp_path: Path) -> None:
    """Test that triage store enables wal mode."""
    store = TriageStore(tmp_path / "triage.sqlite3")
    assert store.get_journal_mode().lower() == "wal"


def test_triage_store_crud_and_queue_filters(tmp_path: Path) -> None:
    """Test that triage store crud and queue filters."""
    store = TriageStore(tmp_path / "triage.sqlite3")
    store.upsert_item(_seed_item("AE-1"))
    store.upsert_item(_seed_item("PD-2"))

    store.assign_item("AE-1", "lead-reviewer")
    store.add_annotation("AE-1", "lead-reviewer", "  investigate severity  ")
    store.update_status("AE-1", TriageStatus.UNDER_REVIEW, "lead-reviewer", "started")

    item = store.get_triage_item("AE-1")
    assert item is not None
    assert item.assignee == "lead-reviewer"
    assert item.status == TriageStatus.UNDER_REVIEW
    assert item.annotations[-1].comment == "investigate severity"
    assert item.history[-1].to_status == TriageStatus.UNDER_REVIEW

    full_queue = store.get_queue("STUDY-A")
    under_review_queue = store.get_queue("STUDY-A", status=TriageStatus.UNDER_REVIEW)

    assert {queued_item.item_id for queued_item in full_queue} == {"AE-1", "PD-2"}
    assert [queued_item.item_id for queued_item in under_review_queue] == ["AE-1"]


def test_triage_store_handles_parallel_reads_and_writes(tmp_path: Path) -> None:
    """Test that triage store handles parallel reads and writes."""
    store = TriageStore(tmp_path / "triage.sqlite3")
    store.upsert_item(_seed_item("AE-99"))

    def _writer(idx: int) -> None:
        """Helper function to  writer."""
        store.add_annotation("AE-99", f"user-{idx}", f"comment {idx}")

    def _reader() -> int:
        """Helper function to  reader."""
        item = store.get_triage_item("AE-99")
        assert item is not None
        return len(item.annotations)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_writer, idx) for idx in range(10)] + [
            pool.submit(_reader) for _ in range(10)
        ]

    failures = [future.exception() for future in futures if future.exception() is not None]
    assert failures == []

    latest = store.get_triage_item("AE-99")
    assert latest is not None
    assert len(latest.annotations) >= 11


def test_triage_store_rejects_empty_annotation_comment(tmp_path: Path) -> None:
    """Test that triage store rejects empty annotation comment."""
    store = TriageStore(tmp_path / "triage.sqlite3")
    store.upsert_item(_seed_item("DD-4"))

    with pytest.raises(ValueError, match="must not be empty"):
        store.add_annotation("DD-4", "triager", "   ")


def test_triage_store_migrates_legacy_schema(tmp_path: Path) -> None:
    """Test that triage store migrates legacy schema."""
    db_path = tmp_path / "triage.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE triage_items (
                item_id TEXT PRIMARY KEY,
                study_key TEXT NOT NULL,
                status TEXT NOT NULL,
                assignee TEXT,
                severity TEXT NOT NULL
            )
            """)
        conn.execute(
            """
            INSERT INTO triage_items (item_id, study_key, status, assignee, severity)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("AE-legacy", "STUDY-A", TriageStatus.NEW.value, "triager", "critical"),
        )
        conn.commit()

    store = TriageStore(db_path)
    migrated_item = store.get_triage_item("AE-legacy")
    assert migrated_item is not None
    assert migrated_item.item_id == "AE-legacy"

    with sqlite3.connect(db_path) as conn:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(triage_items)").fetchall()
        }
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])

    assert {"created_at", "updated_at"}.issubset(columns)
    assert version == 2


def test_triage_store_masks_sensitive_operational_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that triage store masks sensitive operational errors."""
    store = TriageStore(tmp_path / "triage.sqlite3", retry_attempts=1)

    @contextmanager
    def _memory_connection() -> Iterator[sqlite3.Connection]:
        """Helper function to  memory connection."""
        conn = sqlite3.connect(":memory:")
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(store, "_connection", _memory_connection)

    def _failing_write(_conn: sqlite3.Connection) -> None:
        """Helper function to  failing write."""
        raise sqlite3.OperationalError("unable to open database file: token=supersecret")

    with pytest.raises(sqlite3.OperationalError) as exc_info:
        store._execute_write(_failing_write)

    assert "supersecret" not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_triage_store_update_status_transitions_and_edge_cases(tmp_path: Path) -> None:
    """Test update_status validates transitions, supports no-op, and preserves state on error."""
    store = TriageStore(tmp_path / "triage.sqlite3")
    store.upsert_item(_seed_item("AE-TRANS-1"))

    # 1. Non-existent item raises ValueError before transition check
    with pytest.raises(ValueError, match="Unknown triage item: NONEXISTENT"):
        store.update_status("NONEXISTENT", TriageStatus.UNDER_REVIEW, "triager", "note")

    # 2. Same status update is a no-op: does not write duplicate history
    item_before = store.get_triage_item("AE-TRANS-1")
    assert item_before is not None
    initial_history_len = len(item_before.history)
    store.update_status("AE-TRANS-1", TriageStatus.NEW, "triager", "no-op note")
    item_after_noop = store.get_triage_item("AE-TRANS-1")
    assert item_after_noop is not None
    assert item_after_noop.status == TriageStatus.NEW
    assert len(item_after_noop.history) == initial_history_len

    # 3. Permitted transition sequence: NEW -> UNDER_REVIEW -> RESOLVED -> UNDER_REVIEW -> NEW
    store.update_status("AE-TRANS-1", TriageStatus.UNDER_REVIEW, "triager", "to under review")
    item = store.get_triage_item("AE-TRANS-1")
    assert item is not None and item.status == TriageStatus.UNDER_REVIEW

    store.update_status("AE-TRANS-1", TriageStatus.RESOLVED, "triager", "resolved")
    item = store.get_triage_item("AE-TRANS-1")
    assert item is not None and item.status == TriageStatus.RESOLVED
    resolved_history_len = len(item.history)

    # 4. Forbidden transition: RESOLVED -> NEW raises ValueError and preserves database state
    with pytest.raises(ValueError, match="Cannot transition triage status from RESOLVED to NEW"):
        store.update_status("AE-TRANS-1", TriageStatus.NEW, "triager", "illegal rollback")

    item_after_fail = store.get_triage_item("AE-TRANS-1")
    assert item_after_fail is not None
    assert item_after_fail.status == TriageStatus.RESOLVED
    assert len(item_after_fail.history) == resolved_history_len

    # 5. Permitted reopen: RESOLVED -> UNDER_REVIEW
    store.update_status("AE-TRANS-1", TriageStatus.UNDER_REVIEW, "triager", "reopen to review")
    item = store.get_triage_item("AE-TRANS-1")
    assert item is not None and item.status == TriageStatus.UNDER_REVIEW

    # 6. Permitted return: UNDER_REVIEW -> NEW
    store.update_status("AE-TRANS-1", TriageStatus.NEW, "triager", "back to new")
    item = store.get_triage_item("AE-TRANS-1")
    assert item is not None and item.status == TriageStatus.NEW


def test_triage_store_upsert_item_validates_transitions(tmp_path: Path) -> None:
    """Test upsert_item enforces allowed transitions and preserves state on illegal ones."""
    store = TriageStore(tmp_path / "triage.sqlite3")
    store.upsert_item(_seed_item("AE-UPSERT-1"))

    # Update item attributes while keeping the same status NEW
    item = store.get_triage_item("AE-UPSERT-1")
    assert item is not None
    item.assignee = "new-assignee"
    store.upsert_item(item)
    updated = store.get_triage_item("AE-UPSERT-1")
    assert updated is not None
    assert updated.assignee == "new-assignee"
    assert updated.status == TriageStatus.NEW

    # Valid transition via upsert: NEW -> RESOLVED
    item.status = TriageStatus.RESOLVED
    store.upsert_item(item)
    resolved = store.get_triage_item("AE-UPSERT-1")
    assert resolved is not None
    assert resolved.status == TriageStatus.RESOLVED

    # Illegal transition via upsert: RESOLVED -> NEW raises ValueError
    item.status = TriageStatus.NEW
    with pytest.raises(ValueError, match="Cannot transition triage status from RESOLVED to NEW"):
        store.upsert_item(item)

    # Verify state was preserved in SQLite
    preserved = store.get_triage_item("AE-UPSERT-1")
    assert preserved is not None
    assert preserved.status == TriageStatus.RESOLVED


def test_triage_store_migrates_v1_schema_preserving_data_and_composite_pks(
    tmp_path: Path,
) -> None:
    """Verify v1 databases migrate to schema v2 composite primary keys without data loss."""
    db_path = tmp_path / "triage_v1.sqlite3"
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE triage_items (
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
            CREATE TABLE triage_annotations (
                annotation_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                comment TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY(item_id) REFERENCES triage_items(item_id) ON DELETE CASCADE
            )
            """)
        conn.execute("""
            CREATE TABLE triage_history (
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
        conn.execute(
            "INSERT INTO triage_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ITEM-1", "STUDY-A", TriageStatus.NEW.value, "alice", "high", now, now),
        )
        conn.execute(
            "INSERT INTO triage_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ITEM-2", "STUDY-A", TriageStatus.NEW.value, "bob", "medium", now, now),
        )
        conn.execute(
            "INSERT INTO triage_annotations VALUES (?, ?, ?, ?, ?)",
            ("comment-1", "ITEM-1", "alice", "v1 note", now),
        )
        conn.execute(
            "INSERT INTO triage_history VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "trans-1",
                "ITEM-1",
                TriageStatus.NEW.value,
                TriageStatus.NEW.value,
                "alice",
                "init",
                now,
            ),
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    store = TriageStore(db_path)
    with sqlite3.connect(db_path) as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        ann_pk_cols = [
            row[1]
            for row in sorted(
                (
                    r
                    for r in conn.execute("PRAGMA table_info(triage_annotations)").fetchall()
                    if r[5] > 0
                ),
                key=lambda r: r[5],
            )
        ]
        hist_pk_cols = [
            row[1]
            for row in sorted(
                (
                    r
                    for r in conn.execute("PRAGMA table_info(triage_history)").fetchall()
                    if r[5] > 0
                ),
                key=lambda r: r[5],
            )
        ]

    assert version == 2
    assert ann_pk_cols == ["item_id", "annotation_id"]
    assert hist_pk_cols == ["item_id", "transition_id"]

    migrated = store.get_triage_item("ITEM-1")
    assert migrated is not None
    assert [a.comment for a in migrated.annotations] == ["v1 note"]
    assert [h.transition_id for h in migrated.history] == ["trans-1"]

    # ITEM-2 can now reuse annotation_id="comment-1" and transition_id="trans-1" without collision
    store.upsert_item(
        TriageItem(
            item_id="ITEM-2",
            study_key="STUDY-A",
            status=TriageStatus.NEW,
            assignee="bob",
            severity="medium",
            annotations=[
                TriageAnnotation(
                    annotation_id="comment-1",
                    user_id="bob",
                    comment="item-2 note",
                    timestamp=datetime.now(timezone.utc),
                )
            ],
            history=[
                TriageHistoryEntry(
                    transition_id="trans-1",
                    from_status=TriageStatus.NEW,
                    to_status=TriageStatus.NEW,
                    user_id="bob",
                    comment="item-2 init",
                    timestamp=datetime.now(timezone.utc),
                )
            ],
        )
    )
    item1_after = store.get_triage_item("ITEM-1")
    item2_after = store.get_triage_item("ITEM-2")
    assert item1_after is not None and item1_after.annotations[0].comment == "v1 note"
    assert item2_after is not None and item2_after.annotations[0].comment == "item-2 note"


def test_triage_store_upsert_preserves_annotations_history_and_logs_transition(
    tmp_path: Path,
) -> None:
    """Verify upsert_item preserves existing annotations/history and logs status transitions."""
    store = TriageStore(tmp_path / "triage.sqlite3")
    store.upsert_item(_seed_item("Q-1"))
    store.add_annotation("Q-1", "reviewer", "manual triage note")

    resynced = TriageItem(
        item_id="Q-1",
        study_key="STUDY-A",
        status=TriageStatus.UNDER_REVIEW,
        assignee="reviewer",
        severity="critical",
        annotations=[
            TriageAnnotation(
                annotation_id="note-Q-1",
                user_id="triager",
                comment="duplicate should be ignored",
                timestamp=datetime.now(timezone.utc),
            ),
            TriageAnnotation(
                annotation_id="comment-2",
                user_id="site-user",
                comment="site answered query",
                timestamp=datetime.now(timezone.utc),
            ),
        ],
    )
    store.upsert_item(resynced)

    stored = store.get_triage_item("Q-1")
    assert stored is not None
    assert stored.status == TriageStatus.UNDER_REVIEW
    comments_by_id = {a.annotation_id: a.comment for a in stored.annotations}
    assert comments_by_id["note-Q-1"] == "initial note"
    assert comments_by_id["comment-2"] == "site answered query"
    assert "manual triage note" in {a.comment for a in stored.annotations}
    assert any(
        h.from_status == TriageStatus.NEW and h.to_status == TriageStatus.UNDER_REVIEW
        for h in stored.history
    )


def test_triage_store_get_queue_supports_arbitrary_item_ids_and_delete_item(
    tmp_path: Path,
) -> None:
    """Verify get_queue handles item_id values with spaces/slashes and delete_item cleans up."""
    store = TriageStore(tmp_path / "triage.sqlite3")
    complex_id = "STUDY A/SUBJ 01/601/V_AGE"
    colon_id = "STUDY-A:701"
    store.upsert_item(_seed_item(complex_id))
    store.upsert_item(_seed_item(colon_id))

    queue = store.get_queue("STUDY-A")
    assert {item.item_id for item in queue} == {complex_id, colon_id}

    assert store.delete_item(complex_id) is True
    assert store.delete_item(complex_id) is False
    assert store.get_triage_item(complex_id) is None
    assert [item.item_id for item in store.get_queue("STUDY-A")] == [colon_id]


def test_triage_store_propagates_integrity_error_without_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify sqlite3.IntegrityError propagates directly without operational retries."""
    store = TriageStore(tmp_path / "triage.sqlite3", retry_attempts=3)
    attempts = 0

    def _integrity_violation(conn: sqlite3.Connection) -> None:
        nonlocal attempts
        attempts += 1
        conn.execute(
            "INSERT INTO triage_annotations (annotation_id, item_id, user_id, comment, timestamp) "
            "VALUES ('a1', 'MISSING-ITEM', 'u1', 'orphan', '2026-01-01T00:00:00+00:00')"
        )

    with pytest.raises(sqlite3.IntegrityError):
        store._execute_write(_integrity_violation)

    assert attempts == 1


def test_triage_store_default_path_and_entry_point_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify TriageStore is registered under imednet.stores and scopes IMEDNET_TRIAGE_DB_PATH by origin."""
    from importlib.metadata import entry_points

    env_db = tmp_path / "default_env" / "triage.sqlite3"
    monkeypatch.setenv("IMEDNET_TRIAGE_DB_PATH", str(env_db))
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)

    eps = list(entry_points(group="imednet.stores", name="TriageStore"))
    assert len(eps) == 1
    loaded_cls = eps[0].load()
    assert loaded_cls is TriageStore

    default_store = loaded_cls()
    expected_db = env_db.parent / "edc.prod.imednetapi.com" / env_db.name
    assert default_store.db_path == expected_db
    assert expected_db.exists()


def test_triage_store_dynamic_home_and_origin_scoping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify dynamic HOME/Path.home resolution and origin scoping vs explicit db_path compatibility."""
    from types import SimpleNamespace

    from imednet.core.context import reset_base_url_context, set_base_url_context
    from imednet_workflows.triage_store import DEFAULT_TRIAGE_DB_PATH

    assert str(DEFAULT_TRIAGE_DB_PATH).startswith("~")
    monkeypatch.delenv("IMEDNET_TRIAGE_DB_PATH", raising=False)
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)

    # 1. Dynamic HOME env resolution across consecutive store initializations
    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"
    monkeypatch.setenv("HOME", str(home_a))
    store_home_a = TriageStore()
    assert (
        store_home_a.db_path == home_a / ".imednet" / "edc.prod.imednetapi.com" / "triage.sqlite3"
    )

    monkeypatch.setenv("HOME", str(home_b))
    store_home_b = TriageStore()
    assert (
        store_home_b.db_path == home_b / ".imednet" / "edc.prod.imednetapi.com" / "triage.sqlite3"
    )

    # 2. Monkeypatched Path.home() resolution and tilde expansion
    patched_home = tmp_path / "patched_home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: patched_home))
    store_patched = TriageStore()
    assert (
        store_patched.db_path
        == patched_home / ".imednet" / "edc.prod.imednetapi.com" / "triage.sqlite3"
    )
    tilde_explicit = TriageStore("~/explicit_tilde.sqlite3")
    assert tilde_explicit.db_path == patched_home / "explicit_tilde.sqlite3"

    # 3. Normalized origin scoping via base_url kwarg, sdk kwarg, and contextvars
    store_norm = TriageStore(
        base_url="https://user:secretpass@TENANT-A.EDC.EXAMPLE.TEST:443/api/v1/edc/"  # pragma: allowlist secret
    )
    assert (
        store_norm.db_path
        == patched_home / ".imednet" / "tenant-a.edc.example.test" / "triage.sqlite3"
    )
    assert "secretpass" not in str(store_norm.db_path)

    stub_sdk = SimpleNamespace(base_url="https://tenant-b.edc.example.test:8443/api/v1")
    store_sdk = TriageStore(sdk=stub_sdk)
    assert (
        store_sdk.db_path
        == patched_home / ".imednet" / "tenant-b.edc.example.test_8443" / "triage.sqlite3"
    )

    tok = set_base_url_context("")
    try:
        store_empty_ctx = TriageStore(tmp_path / "ctx_store" / "triage.sqlite3")
        assert store_empty_ctx.db_path == tmp_path / "ctx_store" / "localhost" / "triage.sqlite3"
    finally:
        reset_base_url_context(tok)


@pytest.mark.parametrize(
    "crafted_url",
    [
        "../escaped",
        "../../escaped",
        "https://..",
        "https://.",
        "https://%2e%2e",
        "https://edc.example.test/../../escaped",
        "https://edc.example.test/%2e%2e/escaped",
        "/etc/passwd",
        "file:///etc/passwd",
        "C:\\Windows\\System32",
        "https://bad\x00host.example.test",
        "https://bad\nhost.example.test",
    ],
)
def test_triage_store_rejects_crafted_base_url_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crafted_url: str
) -> None:
    """Verify crafted base_url inputs raise PathTraversalValidationError and never escape .imednet."""
    from imednet.errors import PathTraversalValidationError

    safe_home = tmp_path / "isolated_home"
    monkeypatch.setenv("HOME", str(safe_home))
    monkeypatch.delenv("IMEDNET_TRIAGE_DB_PATH", raising=False)

    with pytest.raises(PathTraversalValidationError):
        TriageStore(base_url=crafted_url)

    assert not (tmp_path / "escaped").exists()


def test_triage_store_rejects_invalid_study_context(tmp_path: Path) -> None:
    """Verify invalid active study_context keys raise PathTraversalValidationError."""
    from imednet.core.context import reset_study_context, set_study_context
    from imednet.errors import PathTraversalValidationError

    tok = set_study_context("../../escape_study")
    try:
        with pytest.raises(PathTraversalValidationError):
            TriageStore(tmp_path / "triage.sqlite3")
    finally:
        reset_study_context(tok)


def test_triage_store_posix_permissions_and_standalone_without_encryption_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify 0700/0600 POSIX permissions and standalone operation with no encryption keys set."""
    import os
    import stat

    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_CONFIG_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "standalone_home"))
    monkeypatch.delenv("IMEDNET_TRIAGE_DB_PATH", raising=False)

    # Pre-create an insecure 0644 SQLite file as well to verify permission remediation
    insecure_db = tmp_path / "legacy_dir" / "insecure.sqlite3"
    insecure_db.parent.mkdir(parents=True, exist_ok=True)
    insecure_db.write_bytes(b"")
    if os.name == "posix":
        os.chmod(insecure_db, 0o644)

    standalone_store = TriageStore(base_url="https://standalone.edc.example.test")
    standalone_store.upsert_item(_seed_item("STANDALONE-1"))
    standalone_store.add_annotation(
        "STANDALONE-1",
        "reviewer",
        "patient_name=CANARY_STANDALONE_NAME token=CANARY_STANDALONE_TOKEN",
    )
    standalone_store.update_status(
        "STANDALONE-1",
        TriageStatus.UNDER_REVIEW,
        "reviewer",
        "dob=1990-01-01",
    )
    stored = standalone_store.get_triage_item("STANDALONE-1")
    assert stored is not None
    assert "CANARY_STANDALONE_NAME" not in stored.annotations[-1].comment
    assert "CANARY_STANDALONE_TOKEN" not in stored.annotations[-1].comment
    assert "1990-01-01" not in (stored.history[-1].comment or "")

    remediated_store = TriageStore(insecure_db)
    remediated_store.upsert_item(_seed_item("LEGACY-PERM-1"))

    if os.name == "posix":
        imednet_dir = tmp_path / "standalone_home" / ".imednet"
        origin_dir = standalone_store.db_path.parent
        assert stat.S_IMODE(imednet_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(origin_dir.stat().st_mode) == 0o700
        for candidate in (
            standalone_store.db_path,
            Path(f"{standalone_store.db_path}-wal"),
            Path(f"{standalone_store.db_path}-shm"),
            remediated_store.db_path,
        ):
            if candidate.exists():
                assert stat.S_IMODE(candidate.stat().st_mode) == 0o600
                assert (stat.S_IMODE(candidate.stat().st_mode) & 0o077) == 0


def test_triage_store_scrubs_committed_study_phi_and_credentials_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify upsert_item, add_annotation, and update_status scrub PHI and secrets before SQLite write."""
    from imednet.models.study_config import StudyConfiguration
    from imednet.sdk import ImednetSDK
    from imednet.simulation import SimulationState, create_simulation_transport
    from imednet_workflows.config_version_control import ConfigVersionStore

    monkeypatch.setenv("HOME", str(tmp_path / "scrub_home"))
    monkeypatch.delenv("IMEDNET_CONFIG_DB_PATH", raising=False)
    monkeypatch.delenv("IMEDNET_TRIAGE_DB_PATH", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_CONFIG_ENCRYPTION_KEY", raising=False)

    transport = create_simulation_transport(SimulationState())
    sdk = ImednetSDK(
        api_key="scrub-api-key",  # pragma: allowlist secret
        security_key="scrub-sec-key",  # pragma: allowlist secret
        base_url="https://scrub.edc.example.test",
        transport=transport,
        simulation_mode=True,
    )

    with sdk.study_context("DEMO"):
        ConfigVersionStore().commit_config(
            "DEMO",
            StudyConfiguration(study_key="DEMO", phi_fields=["custom_mrn"]),
            "Register custom_mrn PHI",
            sdk=sdk,
        )

    store = TriageStore(sdk=sdk)
    store.upsert_item(
        TriageItem(
            item_id="DEMO:10",
            study_key="DEMO",
            status=TriageStatus.NEW,
            severity="high",
            annotations=[
                TriageAnnotation(
                    annotation_id="ann-1",
                    user_id="site-user",
                    comment=(
                        "patient_name=CANARY_PATIENT_NAME_UPSERT "
                        "custom_mrn=CANARY_CUSTOM_MRN_UPSERT "
                        "https://u:CANARY_URI_SECRET@edc.test/q?token=CANARY_URL_TOKEN"  # pragma: allowlist secret
                    ),
                    timestamp=datetime.now(timezone.utc),
                )
            ],
            history=[
                TriageHistoryEntry(
                    transition_id="tr-1",
                    from_status=TriageStatus.NEW,
                    to_status=TriageStatus.NEW,
                    user_id="site-user",
                    comment="dob=1982-11-23 Authorization: Bearer CANARY_HIST_BEARER",
                    timestamp=datetime.now(timezone.utc),
                )
            ],
        )
    )
    store.add_annotation(
        "DEMO:10",
        "reviewer",
        '{"custom_mrn": "CANARY_CUSTOM_MRN_ANN", "x-api-key": "CANARY_HEADER_KEY"}',
    )
    store.update_status(
        "DEMO:10",
        TriageStatus.UNDER_REVIEW,
        "reviewer",
        "Reviewed customMrn='CANARY_CUSTOM_MRN_STATUS' patient_name='CANARY_PATIENT_STATUS'",
    )

    # Checkpoint WAL and verify raw bytes across SQLite + WAL/SHM files contain zero canary values
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(FULL)")

    raw_bytes = b"".join(
        p.read_bytes()
        for p in (
            store.db_path,
            Path(f"{store.db_path}-wal"),
            Path(f"{store.db_path}-shm"),
        )
        if p.exists()
    )
    for forbidden in (
        b"CANARY_PATIENT_NAME_UPSERT",
        b"CANARY_CUSTOM_MRN_UPSERT",
        b"CANARY_URI_SECRET",
        b"CANARY_URL_TOKEN",
        b"1982-11-23",
        b"CANARY_HIST_BEARER",
        b"CANARY_CUSTOM_MRN_ANN",
        b"CANARY_HEADER_KEY",
        b"CANARY_CUSTOM_MRN_STATUS",
        b"CANARY_PATIENT_STATUS",
    ):
        assert forbidden not in raw_bytes
