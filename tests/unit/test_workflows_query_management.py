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
    """Test that get queries by site branches multi-subject OR with state AND filter."""
    from unittest.mock import call

    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1"), Subject(subject_key="S2")]
    wf = QueryManagementWorkflow(sdk)

    wf.get_queries_by_site("STUDY", "SITE", additional_filter={"state": "open"})

    sdk.get_subjects.assert_called_once_with("STUDY", site_name="SITE")
    assert sdk.get_queries.call_args_list == [
        call("STUDY", subject_key="S1", state="open"),
        call("STUDY", subject_key="S2", state="open"),
    ]


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
    """Test that get queries by site with space in name."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1")]
    wf = QueryManagementWorkflow(sdk)

    wf.get_queries_by_site("STUDY", "Mock Site")

    sdk.get_subjects.assert_called_once_with("STUDY", site_name="Mock Site")
    sdk.get_queries.assert_called_once_with("STUDY", subject_key=["S1"])
    assert sdk.get_queries.call_args.kwargs == {"subject_key": ["S1"]}


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
    """Verify multi-Subject Site query lookup emits OR filter without studyKey predicate."""
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
                        "queryComments": [{"sequence": 1, "closed": False}],
                    },
                    {
                        "annotationId": 11,
                        "subjectKey": "SUB2",
                        "queryComments": [{"sequence": 1, "closed": False}],
                    },
                ]
            }
        )

        wf = QueryManagementWorkflow(sdk)
        queries = wf.get_queries_by_site("STUDY", "General Hospital")

        assert len(queries) == 2
        assert (
            subjects_route.calls.last.request.url.params["filter"] == 'siteName=="General Hospital"'
        )
        assert (
            queries_route.calls.last.request.url.params["filter"]
            == "subjectKey==SUB1,subjectKey==SUB2"
        )


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
    """Verify soft-deleted and unkeyed/blank subjects are excluded before querying."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [
        Subject(subject_key="ACTIVE-1"),
        Subject(subject_key="DELETED-FLAG", deleted=True),
        Subject(subject_key="DELETED-STATUS", subject_status="Deleted"),
        Subject(subject_key=None),
        Subject(subject_key="   "),
        Subject(subject_key=" ACTIVE-2 "),
    ]
    wf = QueryManagementWorkflow(sdk)
    wf.get_queries_by_site("STUDY", "Site 1")

    sdk.get_queries.assert_called_once_with("STUDY", subject_key=["ACTIVE-1", "ACTIVE-2"])


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

    store = TriageStore(db_path)
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


def test_query_management_methods_branch_or_and_deduplicate_over_http(tmp_path: Any) -> None:
    """Verify all 6 QueryManagementWorkflow methods branch OR+AND filters without mixed connectors."""
    import httpx
    import respx

    from imednet.models.triage import TriageStatus
    from imednet.sdk import ImednetSDK
    from imednet.utils.filters import validate_filter_syntax
    from imednet_workflows.triage_store import TriageStore

    base_url = "https://api.example.com"
    seen_subject_filters: list[str] = []
    seen_query_filters: list[str] = []

    def _subjects_handler(request: httpx.Request) -> httpx.Response:
        flt = request.url.params.get("filter", "")
        validate_filter_syntax(flt)
        seen_subject_filters.append(flt)
        rows = []
        if "Site A" in flt:
            rows.extend(
                [
                    {
                        "studyKey": "STUDY",
                        "subjectKey": "SUB1",
                        "siteName": "Site A",
                        "status": "Active",
                    },
                    {
                        "studyKey": "STUDY",
                        "subjectKey": "SUB2",
                        "siteName": "Site A",
                        "status": "Active",
                    },
                ]
            )
        if "Site B" in flt:
            rows.extend(
                [
                    {
                        "studyKey": "STUDY",
                        "subjectKey": "SUB2",
                        "siteName": "Site B",
                        "status": "Active",
                    },
                    {
                        "studyKey": "STUDY",
                        "subjectKey": "SUB3",
                        "siteName": "Site B",
                        "status": "Active",
                    },
                ]
            )
        return httpx.Response(
            200,
            json={
                "metadata": {"status": "OK"},
                "pagination": {
                    "currentPage": 0,
                    "size": 25,
                    "totalPages": 1,
                    "totalElements": len(rows),
                },
                "data": rows,
            },
        )

    def _queries_handler(request: httpx.Request) -> httpx.Response:
        flt = request.url.params.get("filter", "")
        validate_filter_syntax(flt)
        assert not (";" in flt and "," in flt)
        seen_query_filters.append(flt)
        # Return overlapping annotationId=501 across branches plus branch-specific and unkeyed items
        data = [
            {
                "studyKey": "STUDY",
                "annotationId": 501,
                "subjectKey": "SUB1",
                "variable": "AGE",
                "queryComments": [
                    {
                        "sequence": 1,
                        "closed": False,
                        "annotationStatus": "Monitor Open",
                        "comment": "Check AGE",
                    }
                ],
            },
            {
                "studyKey": "STUDY",
                "annotationId": 502 if "WT" in flt or "SUB2" in flt else 503,
                "subjectKey": "SUB2",
                "variable": "WT",
                "queryComments": [
                    {
                        "sequence": 1,
                        "closed": False,
                        "annotationStatus": "Monitor Open",
                        "comment": "Check WT",
                    },
                    {
                        "sequence": 2,
                        "closed": False,
                        "annotationStatus": "Subject Answered",
                        "comment": "Updated",
                    },
                ],
            },
            {
                "studyKey": "STUDY",
                "annotationId": None,
                "subjectKey": "SUB1",
                "recordId": 88,
                "variable": "UNKEYED",
                "queryComments": [
                    {
                        "sequence": 1,
                        "closed": True,
                        "annotationStatus": "Closed",
                        "comment": "Closed",
                    }
                ],
            },
        ]
        return httpx.Response(
            200,
            json={
                "metadata": {"status": "OK"},
                "pagination": {
                    "currentPage": 0,
                    "size": 25,
                    "totalPages": 1,
                    "totalElements": len(data),
                },
                "data": data,
            },
        )

    with respx.mock(base_url=base_url, assert_all_called=False) as router:
        subj_route = router.get("/api/v1/edc/studies/STUDY/subjects").mock(
            side_effect=_subjects_handler
        )
        q_route = router.get("/api/v1/edc/studies/STUDY/queries").mock(side_effect=_queries_handler)

        with ImednetSDK(api_key="k", security_key="s", base_url=base_url, retries=0) as sdk:
            wf = sdk.workflows.query_management

            # 1. Empty OR list & disjoint subject_key intersections short-circuit with zero HTTP calls
            assert wf.get_open_queries("STUDY", additional_filter={"variable": []}) == []
            assert wf.get_unresolved_queries("STUDY", additional_filter={"variable": set()}) == []
            assert (
                wf.get_queries_for_subject("STUDY", "SUB1", additional_filter={"variable": []})
                == []
            )
            assert (
                wf.get_queries_for_subject(
                    "STUDY", "SUB1", additional_filter={"subject_key": ["SUB2", "SUB3"]}
                )
                == []
            )
            assert wf.get_queries_for_subject("STUDY", "") == []
            assert wf.get_queries_by_site("STUDY", [], additional_filter={"variable": "AGE"}) == []
            assert (
                wf.get_queries_by_site("STUDY", "Site A", additional_filter={"variable": []}) == []
            )
            assert wf.get_query_state_counts("STUDY", variable=[]) == {
                s.value: 0 for s in QueryStatus
            }
            empty_store = TriageStore(tmp_path / "empty.sqlite3")
            assert (
                wf.sync_queries_to_triage(
                    "STUDY", store=empty_store, additional_filter={"variable": []}
                )
                == []
            )
            assert subj_route.call_count == 0
            assert q_route.call_count == 0

            # Disjoint site subject intersection calls get_subjects once, then skips get_queries
            assert (
                wf.get_queries_by_site(
                    "STUDY",
                    "Site A",
                    additional_filter={"subject_key": ["SUB99"], "variable": "AGE"},
                )
                == []
            )
            assert subj_route.call_count == 1
            assert q_route.call_count == 0

            # 2. get_queries_by_site with multi-site OR, subject_filter, subject_key intersection, and OR+AND query filters
            seen_subject_filters.clear()
            seen_query_filters.clear()
            site_queries = wf.get_queries_by_site(
                "STUDY",
                ["Site A", "Site B"],
                subject_filter={"subject_status": "Active"},
                additional_filter={"subject_key": ["SUB2", "SUB99"], "variable": ["AGE", "WT"]},
            )
            assert len(seen_subject_filters) == 2
            assert seen_query_filters == [
                "subjectKey==SUB2;variable==AGE",
                "subjectKey==SUB2;variable==WT",
            ]
            # annotationId 501 and 502 deduplicated across the 2 branches while unkeyed items remain
            assert [q.annotation_id for q in site_queries] == [501, 502, None, None]

            # 3. get_queries_for_subject with comma-separated OR string + AND kwarg and != subject intersection
            seen_query_filters.clear()
            subj_queries = wf.get_queries_for_subject(
                "STUDY",
                ["SUB1", "SUB2"],
                additional_filter="variable==AGE,variable==WT",
                subjectKey=("!=", "SUB2"),
            )
            assert seen_query_filters == [
                "subjectKey==SUB1;variable==AGE",
                "subjectKey==SUB1;variable==WT",
            ]
            assert [q.annotation_id for q in subj_queries] == [501, 503, None, 502, None]

            # 4. get_open_queries & get_unresolved_queries deduplicate by annotation_id
            open_qs = wf.get_open_queries(
                "STUDY",
                additional_filter={"variable": ["AGE", "WT"], "subject_key": "SUB1"},
            )
            assert [q.annotation_id for q in open_qs] == [501]

            unresolved_qs = wf.get_unresolved_queries(
                "STUDY",
                additional_filter={"variable": ["AGE", "WT"], "subject_key": "SUB1"},
            )
            assert [q.annotation_id for q in unresolved_qs] == [501, 503, 502]

            # 5. get_query_state_counts deduplicates annotation_id across branches before counting
            counts = wf.get_query_state_counts(
                "STUDY",
                additional_filter={"variable": ["AGE", "WT"], "subject_key": "SUB1"},
            )
            assert counts[QueryStatus.OPEN] == 1
            assert counts[QueryStatus.ANSWERED] == 2
            assert counts[QueryStatus.CLOSED] == 2  # 2 unkeyed items preserved

            # 6. sync_queries_to_triage deduplicates branched queries into TriageStore
            triage_store = TriageStore(tmp_path / "branched.sqlite3")
            synced = wf.sync_queries_to_triage(
                "STUDY",
                store=triage_store,
                additional_filter={"variable": ["AGE", "WT"], "subject_key": "SUB1"},
                unresolved_only=True,
            )
            assert [item.item_id for item in synced] == ["STUDY:501", "STUDY:503", "STUDY:502"]
            assert all(
                item.status in (TriageStatus.NEW, TriageStatus.UNDER_REVIEW) for item in synced
            )
