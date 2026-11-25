"""Unit tests for record revisions endpoint."""

import pytest

from imednet.core.context import set_study_context
from imednet.endpoints import record_revisions
from imednet.errors import NotFoundError
from imednet.models.record_revisions import RecordRevision


def test_list_uses_filters(dummy_client, paginator_factory, patch_build_filter):
    """Test that list uses documented filters and omits studyKey from filter."""
    set_study_context("S1")
    ep = record_revisions.RecordRevisionsEndpoint(dummy_client)
    capture = paginator_factory(record_revisions, [{"recordRevisionId": 1}])
    patch = patch_build_filter(record_revisions)

    result = ep.list(record_status="Closed")

    assert capture["path"] == "/api/v1/edc/studies/S1/recordRevisions"
    assert capture["params"] == {"filter": "FILTERED"}
    assert patch["filters"] == {"record_status": "Closed"}
    assert isinstance(result[0], RecordRevision)


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get not found."""
    ep = record_revisions.RecordRevisionsEndpoint(dummy_client)

    def fake_impl(self, client, paginator, *, study_key=None, refresh=False, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(record_revisions.RecordRevisionsEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", 1)


def _sample_revision_payloads() -> list[dict[str, object]]:
    return [
        {
            "studyKey": "S1",
            "recordRevisionId": 101,
            "recordId": 10,
            "recordOid": "REC-10",
            "recordRevision": 1,
            "dataRevision": 1,
            "recordStatus": "Record Incomplete",
            "user": "alice",
            "deleted": False,
            "dateCreated": "2026-01-05T10:00:00Z",
            "lastUpdated": "2026-01-05T10:00:00Z",
        },
        {
            "studyKey": "S1",
            "recordRevisionId": 102,
            "recordId": 10,
            "recordOid": "REC-10",
            "recordRevision": 2,
            "dataRevision": 2,
            "recordStatus": "Record Complete",
            "user": "alice",
            "deleted": False,
            "dateCreated": "2026-01-15T18:00:00-06:00",  # 2026-01-16T00:00:00Z
            "lastUpdated": "2026-01-20T12:00:00Z",
        },
        {
            "studyKey": "S1",
            "recordRevisionId": 103,
            "recordId": 10,
            "recordOid": "REC-10",
            "recordRevision": 3,
            "dataRevision": 2,
            "recordStatus": "Verified",
            "user": "alice",
            "deleted": False,
            "dateCreated": "2026-02-02T09:00:00Z",
            "lastUpdated": "2026-02-02T09:30:00Z",
        },
    ]


def test_sync_list_and_to_dataframe_bounded_dates_single_upstream_clause(dummy_client, monkeypatch):
    """Sync list and to_dataframe send <=1 dateCreated and <=1 lastUpdated upstream and filter secondary bounds locally."""
    pytest.importorskip("pandas")
    ep = record_revisions.RecordRevisionsEndpoint(dummy_client)
    captured_params: list[dict[str, object]] = []
    payloads = _sample_revision_payloads()

    class FakeSyncPaginator:
        has_more = False

        def __init__(self, client, path, params=None, **kwargs):
            captured_params.append(dict(params or {}))

        def __iter__(self):
            return iter(payloads)

    monkeypatch.setattr(ep, "PAGINATOR_CLS", FakeSyncPaginator)

    # 1. start_date + end_date + lastUpdated tuple bounds + post-filter limit
    items = list(
        ep.list(
            "S1",
            start_date="2026-01-10",
            end_date="2026-01-31",
            last_updated=[(">=", "2026-01-18T00:00:00Z"), ("<=", "2026-01-25T23:59:59Z")],
            limit=1,
        )
    )
    assert [rev.record_revision_id for rev in items] == [102]
    upstream_filter = str(captured_params[-1]["filter"])
    assert upstream_filter.count("dateCreated") == 1
    assert upstream_filter.count("lastUpdated") == 1

    # 2. Semicolon-joined raw filter string with dual dateCreated bounds in to_dataframe
    df = ep.to_dataframe(
        "S1",
        filter=("dateCreated>=2026-01-10T00:00:00Z;dateCreated<=2026-01-31T23:59:59Z;user==alice"),
    )
    assert list(df["record_revision_id"]) == [102]
    raw_upstream_filter = str(captured_params[-1]["filter"])
    assert raw_upstream_filter.count("dateCreated") == 1
    assert "user==alice" in raw_upstream_filter

    # 3. Non-date duplicate AND attribute still raises ValueError
    with pytest.raises(ValueError, match="Duplicate filter attribute 'recordId' in AND clause"):
        list(ep.list("S1", filter="recordId==10;recordId==20"))


@pytest.mark.asyncio
async def test_async_list_and_to_dataframe_bounded_dates_single_upstream_clause(
    dummy_client, monkeypatch
):
    """Async list and to_dataframe send <=1 dateCreated and <=1 lastUpdated upstream and filter secondary bounds locally."""
    pytest.importorskip("pandas")
    ep = record_revisions.AsyncRecordRevisionsEndpoint(dummy_client)
    captured_params: list[dict[str, object]] = []
    payloads = _sample_revision_payloads()

    class FakeAsyncPaginator:
        def __init__(self, client, path, params=None, **kwargs):
            captured_params.append(dict(params or {}))

        def __aiter__(self):
            async def _gen():
                for row in payloads:
                    yield row

            return _gen()

    monkeypatch.setattr(ep, "ASYNC_PAGINATOR_CLS", FakeAsyncPaginator)

    items = [
        rev
        async for rev in ep.list(
            "S1",
            dateCreated=[(">=", "2026-01-01T00:00:00Z"), ("<=", "2026-01-31T23:59:59Z")],
            lastUpdated=[(">=", "2026-01-15T00:00:00Z"), ("<=", "2026-01-25T23:59:59Z")],
        )
    ]
    assert [rev.record_revision_id for rev in items] == [102]
    upstream_filter = str(captured_params[-1]["filter"])
    assert upstream_filter.count("dateCreated") == 1
    assert upstream_filter.count("lastUpdated") == 1

    df = await ep.to_dataframe(
        "S1",
        start_date="2026-01-10",
        end_date="2026-01-31",
    )
    assert list(df["record_revision_id"]) == [102]
    df_upstream_filter = str(captured_params[-1]["filter"])
    assert df_upstream_filter.count("dateCreated") == 1


@pytest.mark.asyncio
async def test_record_revisions_empty_list_rejection_fractional_end_date_and_or_lists(
    dummy_client, monkeypatch
):
    """Verify user=[] rejection, _resolve_params syntax validation, fractional end_date, and OR lists."""
    sync_ep = record_revisions.RecordRevisionsEndpoint(dummy_client)
    async_ep = record_revisions.AsyncRecordRevisionsEndpoint(dummy_client)

    # 1. Empty list filters fail consistently whether raw filter is absent or present
    for kwargs in (
        {"user": []},
        {"user": [], "filter": "role==CRC"},
        {"dateCreated": []},
        {"date_created": []},
    ):
        with pytest.raises(ValueError, match="cannot be empty"):
            list(sync_ep.list("S1", **kwargs))
        with pytest.raises(ValueError, match="cannot be empty"):
            [item async for item in async_ep.list("S1", **kwargs)]

    # 2. _resolve_params validates built_filter syntax even when raw filter is absent
    with monkeypatch.context() as mp:
        mp.setattr(
            "imednet.core.endpoint.base.build_filter_string",
            lambda _f: "recordId==1;recordId==2",
        )
        with pytest.raises(ValueError, match="Duplicate filter attribute 'recordId'"):
            sync_ep._resolve_params("S1", None, {"record_id": 1})

    with monkeypatch.context() as mp:
        mp.setattr(
            "imednet.core.endpoint.base.build_filter_string",
            lambda _f: "   ",
        )
        with pytest.raises(ValueError, match="Filter expression contains an empty clause"):
            sync_ep._resolve_params("S1", None, {"record_id": 1})

    # 3. Single date-only end_date emits <next-dayT00:00:00Z and retains 23:59:59.750000Z
    captured: list[dict[str, object]] = []
    subsecond_payloads = [
        {
            "studyKey": "S1",
            "recordRevisionId": 201,
            "recordId": 20,
            "subjectId": 701,
            "subjectOid": "SUBJ-OID-701",
            "subjectKey": "SUBJ-701",
            "role": "CRC",
            "dateCreated": "2026-01-31T23:59:59.750000Z",
        },
        {
            "studyKey": "S1",
            "recordRevisionId": 202,
            "recordId": 20,
            "role": "CRC",
            "dateCreated": "2026-01-10T00:00:00Z",
        },
    ]

    class FakeSyncPaginator:
        has_more = False

        def __init__(self, client, path, params=None, **kwargs):
            captured.append(dict(params or {}))

        def __iter__(self):
            return iter(subsecond_payloads)

    monkeypatch.setattr(sync_ep, "PAGINATOR_CLS", FakeSyncPaginator)

    res_end = list(sync_ep.list("S1", end_date="2026-01-31"))
    assert [r.record_revision_id for r in res_end] == [201, 202]
    assert captured[-1]["filter"] == "dateCreated<2026-02-01T00:00:00Z"

    # 4. Pure scalar OR list on dateCreated emits valid comma-joined OR upstream; combined with role uses >= floor + local OR
    res_pure_or = list(
        sync_ep.list(
            "S1",
            dateCreated=["2026-01-10", "2026-03-01T00:00:00Z"],
        )
    )
    assert len(res_pure_or) == 2  # FakeSyncPaginator does not filter upstream pure OR
    assert (
        captured[-1]["filter"]
        == "dateCreated==2026-01-10T00:00:00Z,dateCreated==2026-03-01T00:00:00Z"
    )

    res_and_or = list(
        sync_ep.list(
            "S1",
            role="CRC",
            dateCreated=["2026-01-10T00:00:00Z", "2026-03-01T00:00:00Z"],
        )
    )
    assert [r.record_revision_id for r in res_and_or] == [202]
    assert captured[-1]["filter"] == "role==CRC;dateCreated>=2026-01-10T00:00:00Z"
