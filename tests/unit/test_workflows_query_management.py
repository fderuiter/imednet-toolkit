"""Unit tests for workflows query management."""

from typing import Any
from unittest.mock import MagicMock

from imednet.models.queries import Query, QueryComment, QueryStatus
from imednet.models.subjects import Subject
from imednet_workflows.query_management import QueryManagementWorkflow


def make_query(
    sequence_closed: list[tuple[int, bool]],
    status: QueryStatus | str | None = None,
) -> Query:
    """Helper function to make query."""
    comments = [QueryComment(sequence=seq, closed=closed) for seq, closed in sequence_closed]
    kwargs: dict[str, Any] = {"query_comments": comments}
    if status is not None:
        kwargs["status"] = status
    return Query(**kwargs)


def test_get_open_queries_filters_latest_comment() -> None:
    """Test that get open queries filters latest comment."""
    sdk = MagicMock()
    query_closed = make_query([(1, False), (2, True)])
    query_open = make_query([(1, False)])
    query_cancelled = Query(status=QueryStatus.CANCELLED)
    sdk.get_queries.return_value = [query_closed, query_open, query_cancelled]

    wf = QueryManagementWorkflow(sdk)
    result = wf.get_open_queries("STUDY", additional_filter={"state": "new"})

    sdk.get_queries.assert_called_once_with("STUDY", state="new")
    assert sdk.get_queries.call_args.kwargs == {"state": "new"}
    assert result == [query_open]


def test_get_queries_for_subject_builds_combined_filter() -> None:
    """Test that get queries for subject builds combined filter."""
    sdk = MagicMock()
    wf = QueryManagementWorkflow(sdk)
    wf.get_queries_for_subject("STUDY", "SUBJ1", additional_filter={"type": "x"})

    sdk.get_queries.assert_called_once_with("STUDY", subject_key="SUBJ1", type="x")
    assert sdk.get_queries.call_args.kwargs == {"subject_key": "SUBJ1", "type": "x"}


def test_get_query_state_counts_aggregates_states() -> None:
    """Test that get query state counts aggregates states."""
    sdk = MagicMock()
    open_query = make_query([(1, False)])
    closed_query = make_query([(1, True)])
    answered_query = Query(status=QueryStatus.ANSWERED)
    cancelled_query = Query(status=QueryStatus.CANCELLED)
    sdk.get_queries.return_value = [open_query, closed_query, answered_query, cancelled_query]

    wf = QueryManagementWorkflow(sdk)
    counts = wf.get_query_state_counts("STUDY")

    sdk.get_queries.assert_called_once_with("STUDY")
    assert sdk.get_queries.call_args.kwargs == {}
    assert counts == {
        QueryStatus.OPEN.value: 1,
        QueryStatus.ANSWERED.value: 1,
        QueryStatus.CLOSED.value: 1,
        QueryStatus.CANCELLED.value: 1,
    }


def test_get_queries_by_site_filters_using_subjects() -> None:
    """Multi-Subject Sites send extra filter once and filter subject keys locally."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1"), Subject(subject_key="S2")]
    q1 = Query(annotation_id=1, subject_key="S1", variable="AE_TERM")
    q2 = Query(annotation_id=2, subject_key="S2", variable="AE_TERM")
    q_other = Query(annotation_id=3, subject_key="S3", variable="AE_TERM")
    sdk.get_queries.return_value = [q1, q_other, q2]
    wf = QueryManagementWorkflow(sdk)

    result = wf.get_queries_by_site("STUDY", "SITE", additional_filter={"variable": "AE_TERM"})

    sdk.get_subjects.assert_called_once_with("STUDY", site_name="SITE")
    sdk.get_queries.assert_called_once_with("STUDY", variable="AE_TERM")
    assert sdk.get_queries.call_args.kwargs == {"variable": "AE_TERM"}
    assert result == [q1, q2]


def test_get_queries_by_site_returns_empty_if_no_subjects() -> None:
    """Test that get queries by site returns empty if no subjects."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = []
    wf = QueryManagementWorkflow(sdk)

    result = wf.get_queries_by_site("STUDY", "SITE")

    sdk.get_subjects.assert_called_once_with("STUDY", site_name="SITE")
    sdk.get_queries.assert_not_called()
    assert result == []


def test_get_queries_by_site_with_space_in_name() -> None:
    """Single-Subject Sites pass scalar subject_key upstream (including deduplicated keys)."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1"), Subject(subject_key=" S1 ")]
    q1 = Query(annotation_id=1, subject_key="S1")
    sdk.get_queries.return_value = [q1]
    wf = QueryManagementWorkflow(sdk)

    result = wf.get_queries_by_site("STUDY", "Mock Site", additional_filter={"variable": "VSORRES"})

    sdk.get_subjects.assert_called_once_with("STUDY", site_name="Mock Site")
    sdk.get_queries.assert_called_once_with("STUDY", variable="VSORRES", subject_key="S1")
    assert sdk.get_queries.call_args.kwargs == {"variable": "VSORRES", "subject_key": "S1"}
    assert result == [q1]


def test_query_state_counts_dict_lookups() -> None:
    """Test that QueryStateCounts supports enum, case-insensitive, and missing keys."""
    from imednet_workflows.query_management import QueryStateCounts

    counts = QueryStateCounts({"OPEN": 5, "CLOSED": 2})
    assert counts[QueryStatus.OPEN] == 5
    assert counts["open"] == 5
    assert counts["OPEN"] == 5
    assert counts["closed"] == 2
    assert counts.get("cancelled", 0) == 0
    assert counts.get("NONEXISTENT", 99) == 99


def test_query_management_duck_typed_objects_fallback() -> None:
    """Test that legacy or duck-typed objects lacking status attribute fall back correctly."""
    sdk = MagicMock()

    class LegacyQuery:
        def __init__(self, closed: bool | None) -> None:
            self.query_comments = (
                [QueryComment(sequence=1, closed=closed)] if closed is not None else []
            )

    legacy_open = LegacyQuery(closed=False)
    legacy_closed = LegacyQuery(closed=True)
    sdk.get_queries.return_value = [legacy_open, legacy_closed]

    wf = QueryManagementWorkflow(sdk)
    open_queries = wf.get_open_queries("STUDY")
    assert open_queries == [legacy_open]

    counts = wf.get_query_state_counts("STUDY")
    assert counts["OPEN"] == 1
    assert counts["CLOSED"] == 1


def test_get_queries_by_site_multi_subject_http_contract() -> None:
    """Verify multi-Subject Site query lookup sends one request with additional_filter and filters locally."""
    import respx

    from imednet.sdk import ImednetSDK

    with respx.mock:
        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
        subjects_route = respx.get("https://api.test/api/v1/edc/studies/STUDY/subjects").respond(
            json={
                "data": [
                    {"subjectId": 1, "subjectKey": "SUB1", "siteName": "General Hospital"},
                    {"subjectId": 2, "subjectKey": "SUB2", "siteName": "General Hospital"},
                ]
            }
        )
        queries_route = respx.get("https://api.test/api/v1/edc/studies/STUDY/queries").respond(
            json={
                "data": [
                    {
                        "annotationId": 10,
                        "subjectKey": "SUB1",
                        "variable": "AETERM",
                        "queryComments": [{"sequence": 1, "closed": False}],
                    },
                    {
                        "annotationId": 11,
                        "subjectKey": "SUB2",
                        "variable": "AETERM",
                        "queryComments": [{"sequence": 1, "closed": False}],
                    },
                    {
                        "annotationId": 12,
                        "subjectKey": "OTHER_SITE_SUB",
                        "variable": "AETERM",
                        "queryComments": [{"sequence": 1, "closed": False}],
                    },
                ]
            }
        )

        wf = QueryManagementWorkflow(sdk)
        queries = wf.get_queries_by_site(
            "STUDY",
            "General Hospital",
            additional_filter={"variable": "AETERM", "record_id": (">=", 100)},
        )

        assert [q.annotation_id for q in queries] == [10, 11]
        assert (
            subjects_route.calls.last.request.url.params["filter"] == 'siteName=="General Hospital"'
        )
        assert queries_route.call_count == 1
        sent_query_filter = queries_route.calls.last.request.url.params["filter"]
        assert "subjectKey" not in sent_query_filter
        assert sent_query_filter == "variable==AETERM;recordId>=100"


def test_get_open_queries_include_answered_and_get_unresolved_queries() -> None:
    """Verify include_answered=True and get_unresolved_queries return OPEN and ANSWERED queries."""
    sdk = MagicMock()
    q_open = Query(annotation_id=1, status=QueryStatus.OPEN)
    q_answered = Query(annotation_id=2, status=QueryStatus.ANSWERED)
    q_closed = Query(annotation_id=3, status=QueryStatus.CLOSED)
    q_cancelled = Query(annotation_id=4, status=QueryStatus.CANCELLED)
    sdk.get_queries.return_value = [q_open, q_answered, q_closed, q_cancelled]

    wf = QueryManagementWorkflow(sdk)
    assert wf.get_open_queries("STUDY") == [q_open]
    assert wf.get_open_queries("STUDY", include_answered=True) == [q_open, q_answered]
    assert wf.get_unresolved_queries("STUDY", additional_filter={"variable": "V_AGE"}) == [
        q_open,
        q_answered,
    ]
    assert sdk.get_queries.call_args.kwargs == {"variable": "V_AGE"}


def test_get_queries_by_site_excludes_deleted_and_unkeyed_subjects() -> None:
    """Verify soft-deleted and unkeyed/blank subjects are excluded before local filtering."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [
        Subject(subject_key="ACTIVE-1"),
        Subject(subject_key="DELETED-FLAG", deleted=True),
        Subject(subject_key="DELETED-STATUS", subject_status="Deleted"),
        Subject(subject_key=None),
        Subject(subject_key="   "),
        Subject(subject_key=" ACTIVE-2 "),
    ]
    q_a1 = Query(annotation_id=1, subject_key="ACTIVE-1")
    q_del = Query(annotation_id=2, subject_key="DELETED-FLAG")
    q_a2 = Query(annotation_id=3, subject_key="ACTIVE-2")
    sdk.get_queries.return_value = [q_a1, q_del, q_a2]

    wf = QueryManagementWorkflow(sdk)
    result = wf.get_queries_by_site("STUDY", "Site 1")

    sdk.get_queries.assert_called_once_with("STUDY")
    assert result == [q_a1, q_a2]


def test_get_queries_by_site_returns_empty_when_only_deleted_or_unkeyed_subjects() -> None:
    """Verify get_queries is not called if all subjects at the site are deleted or unkeyed."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [
        Subject(subject_key="DEL-1", deleted=True),
        Subject(subject_key=None),
        Subject(subject_key=""),
    ]
    wf = QueryManagementWorkflow(sdk)

    assert wf.get_queries_by_site("STUDY", "Site 1") == []
    sdk.get_queries.assert_not_called()


def test_sync_queries_to_triage_lifecycle_and_preservation(tmp_path: Any, monkeypatch: Any) -> None:
    """Verify sync_queries_to_triage preserves reviewer state and tracks status transitions."""
    from imednet.models.triage import TriageStatus
    from imednet_workflows.triage_store import TriageStore

    db_path = tmp_path / "sync_triage.sqlite3"
    monkeypatch.setenv("IMEDNET_TRIAGE_DB_PATH", str(db_path))

    sdk = MagicMock()
    q1_open = Query(
        annotation_id=701,
        subject_key="SUBJ-001",
        query_comments=[
            QueryComment(
                sequence=1,
                closed=False,
                user="dm-user",
                comment="Verify age",
                date="2026-01-11 08:00:00",
            )
        ],
    )
    q2_fallback = Query(
        subject_key="SUBJ 002",
        record_id=602,
        variable="V_WEIGHT",
        description="Missing weight check",
        date_created="2026-01-12",
    )
    sdk.get_queries.return_value = [q1_open, q2_fallback]

    wf = QueryManagementWorkflow(sdk)
    # Sync via default store resolution (imednet.stores entry point + IMEDNET_TRIAGE_DB_PATH)
    initial_queue = wf.sync_queries_to_triage("STUDY", default_severity="high")
    assert len(initial_queue) == 2

    store = TriageStore()
    assert store.db_path == db_path.parent / "edc.prod.imednetapi.com" / db_path.name
    item1 = store.get_triage_item("STUDY:701")
    item2 = store.get_triage_item("STUDY/SUBJ 002/602/V_WEIGHT")
    assert item1 is not None and item1.status == TriageStatus.NEW and item1.severity == "high"
    assert item2 is not None and item2.annotations[0].comment == "Missing weight check"

    # Reviewer assigns and annotates item1
    store.assign_item("STUDY:701", "lead-triager")
    store.add_annotation("STUDY:701", "lead-triager", "Waiting for site response")

    # Site answers q1; q2 closes
    q1_answered = Query(
        annotation_id=701,
        subject_key="SUBJ-001",
        query_comments=[
            QueryComment(
                sequence=1,
                closed=False,
                user="dm-user",
                comment="Verify age",
                date="2026-01-11 08:00:00",
            ),
            QueryComment(
                sequence=2,
                closed=False,
                user="site-coord",
                annotation_status="Site Answered",
                comment="Confirmed 45",
                date="2026-01-12 09:00:00",
            ),
        ],
    )
    q2_closed = Query(
        subject_key="SUBJ 002",
        record_id=602,
        variable="V_WEIGHT",
        status=QueryStatus.CLOSED,
    )
    sdk.get_queries.return_value = [q1_answered, q2_closed]

    wf.sync_queries_to_triage("STUDY", store=store)
    item1_ans = store.get_triage_item("STUDY:701")
    item2_res = store.get_triage_item("STUDY/SUBJ 002/602/V_WEIGHT")
    assert item1_ans is not None
    assert item1_ans.status == TriageStatus.UNDER_REVIEW
    assert item1_ans.assignee == "lead-triager"
    assert {a.comment for a in item1_ans.annotations} == {
        "Verify age",
        "Waiting for site response",
        "Confirmed 45",
    }
    assert any(
        h.from_status == TriageStatus.NEW and h.to_status == TriageStatus.UNDER_REVIEW
        for h in item1_ans.history
    )
    assert item2_res is not None and item2_res.status == TriageStatus.RESOLVED

    # Close q1, then reopen q1 (RESOLVED -> UNDER_REVIEW)
    q1_closed = Query(
        annotation_id=701,
        subject_key="SUBJ-001",
        status=QueryStatus.CLOSED,
        query_comments=q1_answered.query_comments
        + [QueryComment(sequence=3, closed=True, user="dm-user", comment="Closed")],
    )
    sdk.get_queries.return_value = [q1_closed]
    wf.sync_queries_to_triage("STUDY", store=store)
    assert store.get_triage_item("STUDY:701").status == TriageStatus.RESOLVED  # type: ignore[union-attr]

    q1_reopened = Query(
        annotation_id=701,
        subject_key="SUBJ-001",
        status=QueryStatus.OPEN,
        query_comments=q1_closed.query_comments
        + [QueryComment(sequence=4, closed=False, user="dm-user", comment="Reopened")],
    )
    sdk.get_queries.return_value = [q1_reopened, q2_closed]
    wf.sync_queries_to_triage("STUDY", store=store, unresolved_only=True)
    reopened = store.get_triage_item("STUDY:701")
    assert reopened is not None
    assert reopened.status == TriageStatus.UNDER_REVIEW
    assert any(
        h.from_status == TriageStatus.RESOLVED and h.to_status == TriageStatus.UNDER_REVIEW
        for h in reopened.history
    )


def test_sync_queries_to_triage_with_simulation_sdk(tmp_path: Any) -> None:
    """Verify end-to-end sync_queries_to_triage against SimulationMode SDK."""
    from imednet.models.triage import TriageStatus
    from imednet.sdk import ImednetSDK

    db_path = tmp_path / "sim_triage.sqlite3"
    with ImednetSDK(simulation_mode=True) as sdk:
        study_key = next(iter(sdk.studies.list())).study_key
        assert study_key is not None
        queue = sdk.workflows.query_management.sync_queries_to_triage(
            study_key,
            store=db_path,
            unresolved_only=True,
        )
        assert len(queue) >= 1
        assert all(item.status in (TriageStatus.NEW, TriageStatus.UNDER_REVIEW) for item in queue)


def test_multi_origin_sync_queries_to_triage_isolation_and_comment_redaction(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Verify multi-origin sync_queries_to_triage isolates queues and scrubs PHI/credentials on disk."""
    import os
    import sqlite3
    import stat

    import respx

    from imednet.models.study_config import StudyConfiguration
    from imednet.models.triage import TriageStatus
    from imednet.sdk import ImednetSDK
    from imednet_workflows.config_version_control import ConfigVersionStore
    from imednet_workflows.triage_store import TriageStore

    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))
    monkeypatch.delenv("IMEDNET_TRIAGE_DB_PATH", raising=False)
    monkeypatch.delenv("IMEDNET_CONFIG_DB_PATH", raising=False)
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_CONFIG_ENCRYPTION_KEY", raising=False)

    with respx.mock:
        respx.get("https://tenant-a.edc.example.test/api/v1/edc/studies/DEMO/sites").respond(
            json={
                "pagination": {"currentPage": 0, "size": 1, "totalPages": 1, "totalElements": 1},
                "data": [{"siteId": 1, "siteName": "Site A"}],
            }
        )
        respx.get("https://tenant-a.edc.example.test/api/v1/edc/studies/DEMO/queries").respond(
            json={
                "pagination": {"currentPage": 0, "size": 100, "totalPages": 1, "totalElements": 3},
                "data": [
                    {
                        "annotationId": 1,
                        "subjectKey": "SUB-01",
                        "variable": "V_NAME",
                        "queryComments": [
                            {
                                "sequence": 1,
                                "closed": False,
                                "user": "dm-a",
                                "comment": (
                                    "Verify patient_name=CANARY_PATIENT_NAME_A "
                                    "dob=1979-03-14 custom_mrn=CANARY_CUSTOM_MRN_A "
                                    "https://tenant-a.edc.example.test/export?token=CANARY_API_TOKEN_A"
                                ),
                            }
                        ],
                    },
                    {
                        "annotationId": 2,
                        "subjectKey": "SUB-02",
                        "variable": "V_DESC",
                        "description": (
                            "Fallback custom_mrn='CANARY_CUSTOM_MRN_A' "
                            "Authorization: Bearer CANARY_API_TOKEN_A"
                        ),
                    },
                    {
                        "annotationId": 3,
                        "subjectKey": "SUB-03",
                        "variable": "V_CLOSED",
                        "queryComments": [
                            {"sequence": 1, "closed": True, "user": "dm-a", "comment": "Closed A"}
                        ],
                    },
                ],
            }
        )
        respx.get("https://tenant-b.edc.example.test/api/v1/edc/studies/DEMO/queries").respond(
            json={
                "pagination": {"currentPage": 0, "size": 100, "totalPages": 1, "totalElements": 2},
                "data": [
                    {
                        "annotationId": 1,
                        "subjectKey": "SUB-01",
                        "variable": "V_NAME",
                        "queryComments": [
                            {
                                "sequence": 1,
                                "closed": False,
                                "user": "dm-b",
                                "comment": "Clean tenant B query",
                            }
                        ],
                    },
                    {
                        "annotationId": 3,
                        "subjectKey": "SUB-03",
                        "variable": "V_OPEN_B",
                        "queryComments": [
                            {
                                "sequence": 1,
                                "closed": False,
                                "user": "dm-b",
                                "comment": "Open on tenant B",
                            }
                        ],
                    },
                ],
            }
        )

        sdk_a = ImednetSDK(
            api_key="key-a",  # pragma: allowlist secret
            security_key="sec-a",  # pragma: allowlist secret
            base_url="https://tenant-a.edc.example.test",
        )
        sdk_b = ImednetSDK(
            api_key="key-b",  # pragma: allowlist secret
            security_key="sec-b",  # pragma: allowlist secret
            base_url="https://tenant-b.edc.example.test",
        )

        with sdk_a.study_context("DEMO"):
            ConfigVersionStore().commit_config(
                "DEMO",
                StudyConfiguration(study_key="DEMO", phi_fields=["custom_mrn"]),
                "Register custom_mrn PHI",
                sdk=sdk_a,
            )
            queue_a = sdk_a.workflows.query_management.sync_queries_to_triage("DEMO")
            store_a = TriageStore()
            store_a.assign_item("DEMO:1", "reviewer-tenant-a")
            store_a.add_annotation(
                "DEMO:1",
                "reviewer-tenant-a",
                "Local note custom_mrn=CANARY_CUSTOM_MRN_A x-api-key: CANARY_API_TOKEN_A",
            )

        assert {item.item_id for item in queue_a} == {"DEMO:1", "DEMO:2", "DEMO:3"}
        item_a3 = store_a.get_triage_item("DEMO:3")
        assert item_a3 is not None and item_a3.status == TriageStatus.RESOLVED

        # Sync Origin B both inside study_context and directly via SDK workflow without study_context
        with sdk_b.study_context("DEMO"):
            queue_b = sdk_b.workflows.query_management.sync_queries_to_triage("DEMO")
            store_b = TriageStore()

        queue_b_direct = sdk_b.workflows.query_management.sync_queries_to_triage("DEMO")
        assert {item.item_id for item in queue_b} == {"DEMO:1", "DEMO:3"}
        assert {item.item_id for item in queue_b_direct} == {"DEMO:1", "DEMO:3"}
        assert "DEMO:2" not in {item.item_id for item in queue_b}
        assert store_a.db_path != store_b.db_path
        assert store_a.db_path.parent.name == "tenant-a.edc.example.test"
        assert store_b.db_path.parent.name == "tenant-b.edc.example.test"

        # Origin B's open DEMO:3 must not reopen Origin A's resolved DEMO:3, and vice versa
        item_a3_after = store_a.get_triage_item("DEMO:3")
        assert item_a3_after is not None and item_a3_after.status == TriageStatus.RESOLVED
        item_b1 = store_b.get_triage_item("DEMO:1")
        item_b3 = store_b.get_triage_item("DEMO:3")
        assert item_b1 is not None
        assert item_b1.assignee is None
        assert [ann.comment for ann in item_b1.annotations] == ["Clean tenant B query"]
        assert item_b3 is not None and item_b3.status == TriageStatus.NEW

        db_file = store_a.db_path
        with sqlite3.connect(db_file) as conn:
            conn.execute("PRAGMA wal_checkpoint(FULL)")

        raw_sqlite_bytes = b"".join(
            p.read_bytes()
            for p in (db_file, db_file.with_name(f"{db_file.name}-wal"))
            if p.exists()
        )
        assert b"CANARY_PATIENT_NAME_A" not in raw_sqlite_bytes
        assert b"1979-03-14" not in raw_sqlite_bytes
        assert b"CANARY_CUSTOM_MRN_A" not in raw_sqlite_bytes
        assert b"CANARY_API_TOKEN_A" not in raw_sqlite_bytes
        if os.name == "posix":
            assert (stat.S_IMODE(db_file.stat().st_mode) & 0o077) == 0


async def test_async_multi_origin_sync_queries_to_triage_isolation(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Verify AsyncImednetSDK sync_queries_to_triage propagates governance context across worker threads."""
    import respx

    from imednet.sdk import AsyncImednetSDK

    env_db = tmp_path / "async_triage" / "triage.sqlite3"
    monkeypatch.setenv("IMEDNET_TRIAGE_DB_PATH", str(env_db))
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)

    with respx.mock:
        respx.get("https://async-a.edc.example.test/api/v1/edc/studies/DEMO/queries").respond(
            json={
                "data": [
                    {
                        "annotationId": 2,
                        "subjectKey": "SUB-A",
                        "queryComments": [
                            {
                                "sequence": 1,
                                "closed": False,
                                "comment": "patient_name=CANARY_ASYNC_PATIENT",
                            }
                        ],
                    }
                ]
            }
        )
        respx.get("https://async-b.edc.example.test/api/v1/edc/studies/DEMO/queries").respond(
            json={
                "data": [
                    {
                        "annotationId": 9,
                        "subjectKey": "SUB-B",
                        "queryComments": [{"sequence": 1, "closed": False, "comment": "Async B"}],
                    }
                ]
            }
        )

        async with (
            AsyncImednetSDK(
                api_key="a",  # pragma: allowlist secret
                security_key="sa",  # pragma: allowlist secret
                base_url="https://async-a.edc.example.test",
            ) as sdk_a,
            AsyncImednetSDK(
                api_key="b",  # pragma: allowlist secret
                security_key="sb",  # pragma: allowlist secret
                base_url="https://async-b.edc.example.test",
            ) as sdk_b,
        ):
            queue_a = await sdk_a.workflows.query_management.sync_queries_to_triage("DEMO")
            queue_b = await sdk_b.workflows.query_management.sync_queries_to_triage("DEMO")

    assert [item.item_id for item in queue_a] == ["DEMO:2"]
    assert "CANARY_ASYNC_PATIENT" not in queue_a[0].annotations[0].comment
    assert [item.item_id for item in queue_b] == ["DEMO:9"]
    assert (tmp_path / "async_triage" / "async-a.edc.example.test" / "triage.sqlite3").exists()
    assert (tmp_path / "async_triage" / "async-b.edc.example.test" / "triage.sqlite3").exists()
