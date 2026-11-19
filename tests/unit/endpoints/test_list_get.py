"""Unit tests for list get."""

import pytest

from imednet.endpoints import (
    codings,
    forms,
    intervals,
    queries,
    record_revisions,
    records,
    sites,
    studies,
    subjects,
    users,
    variables,
    visits,
)
from imednet.errors.validation import ConfigurationError
from imednet.models.codings import Coding
from imednet.models.forms import Form
from imednet.models.intervals import Interval
from imednet.models.queries import Query
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.studies import Study
from imednet.models.subjects import Subject
from imednet.models.users import User
from imednet.models.variables import Variable
from imednet.models.visits import Visit

CASES = [
    (forms.FormsEndpoint, forms, Form, 1),
    (intervals.IntervalsEndpoint, intervals, Interval, 1),
    (record_revisions.RecordRevisionsEndpoint, record_revisions, RecordRevision, 1),
    (records.RecordsEndpoint, records, Record, 1),
    (sites.SitesEndpoint, sites, Site, 1),
    (studies.StudiesEndpoint, studies, Study, "S1"),
    (subjects.SubjectsEndpoint, subjects, Subject, 100),
    (variables.VariablesEndpoint, variables, Variable, 1),
]

ASYNC_CASES = [
    (forms.AsyncFormsEndpoint, forms, Form, 1),
    (intervals.AsyncIntervalsEndpoint, intervals, Interval, 1),
    (record_revisions.AsyncRecordRevisionsEndpoint, record_revisions, RecordRevision, 1),
    (records.AsyncRecordsEndpoint, records, Record, 1),
    (sites.AsyncSitesEndpoint, sites, Site, 1),
    (studies.AsyncStudiesEndpoint, studies, Study, "S1"),
    (subjects.AsyncSubjectsEndpoint, subjects, Subject, 100),
    (variables.AsyncVariablesEndpoint, variables, Variable, 1),
]

LIST_ONLY_CASES = [
    (codings.CodingsEndpoint, codings, Coding, {"codingId": "C1"}),
    (queries.QueriesEndpoint, queries, Query, {"annotationId": 1}),
    (users.UsersEndpoint, users, User, {"userId": 1}),
    (visits.VisitsEndpoint, visits, Visit, {"visitId": 1}),
]

ASYNC_LIST_ONLY_CASES = [
    (codings.AsyncCodingsEndpoint, codings, Coding, {"codingId": "C1"}),
    (queries.AsyncQueriesEndpoint, queries, Query, {"annotationId": 1}),
    (users.AsyncUsersEndpoint, users, User, {"userId": 1}),
    (visits.AsyncVisitsEndpoint, visits, Visit, {"visitId": 1}),
]

ALL_COLLECTION_SYNC = [
    (studies.StudiesEndpoint, studies, False, "studyName"),
    (sites.SitesEndpoint, sites, True, "siteName"),
    (forms.FormsEndpoint, forms, True, "formKey"),
    (intervals.IntervalsEndpoint, intervals, True, "intervalName"),
    (variables.VariablesEndpoint, variables, True, "variableName"),
    (subjects.SubjectsEndpoint, subjects, True, "subjectKey"),
    (records.RecordsEndpoint, records, True, "formKey"),
    (record_revisions.RecordRevisionsEndpoint, record_revisions, True, "formKey"),
    (visits.VisitsEndpoint, visits, True, "subjectKey"),
    (codings.CodingsEndpoint, codings, True, "variable"),
    (queries.QueriesEndpoint, queries, True, "variable"),
    (users.UsersEndpoint, users, True, None),
]


@pytest.mark.parametrize(("cls", "module", "model", "item_id"), CASES)
def test_list_and_get(dummy_client, paginator_factory, cls, module, model, item_id):
    """Test that list and get."""
    ep = cls(dummy_client)
    capture = paginator_factory(module, [{cls._id_param: item_id}])

    list_kwargs = {"study_key": "S1"} if getattr(ep, "requires_study_key", True) else {}
    result = list(ep.list(**list_kwargs))

    expected_path = "/api/v1/edc/studies"
    if getattr(ep, "requires_study_key", True):
        expected_path += f"/S1/{cls.PATH}"
    elif cls.PATH:
        expected_path += f"/{cls.PATH}"
    assert capture["path"] == expected_path
    assert isinstance(result[0], model)

    get_args = ("S1", item_id) if getattr(ep, "requires_study_key", True) else (None, item_id)
    got = ep.get(*get_args)
    assert isinstance(got, model)


@pytest.mark.asyncio
@pytest.mark.parametrize(("cls", "module", "model", "item_id"), ASYNC_CASES)
async def test_async_list_and_get(
    dummy_client,
    async_paginator_factory,
    cls,
    module,
    model,
    item_id,
):
    """Test that async list and get asynchronously."""
    ep = cls(dummy_client)
    capture = async_paginator_factory(module, [{cls._id_param: item_id}])

    list_kwargs = {"study_key": "S1"} if getattr(ep, "requires_study_key", True) else {}
    result = [item async for item in ep.list(**list_kwargs)]

    expected_path = "/api/v1/edc/studies"
    if getattr(ep, "requires_study_key", True):
        expected_path += f"/S1/{cls.PATH}"
    elif cls.PATH:
        expected_path += f"/{cls.PATH}"
    assert capture["path"] == expected_path
    assert isinstance(result[0], model)

    get_args = ("S1", item_id) if getattr(ep, "requires_study_key", True) else (None, item_id)
    got = await ep.get(*get_args)
    assert isinstance(got, model)


@pytest.mark.parametrize(("cls", "module", "model", "payload"), LIST_ONLY_CASES)
def test_list_only_endpoints(dummy_client, paginator_factory, cls, module, model, payload):
    """Test list-only endpoints list items and do not expose get."""
    ep = cls(dummy_client)
    assert not hasattr(ep, "get")
    capture = paginator_factory(module, [payload])
    result = list(ep.list(study_key="S1"))
    assert capture["path"] == f"/api/v1/edc/studies/S1/{cls.PATH}"
    assert isinstance(result[0], model)


@pytest.mark.asyncio
@pytest.mark.parametrize(("cls", "module", "model", "payload"), ASYNC_LIST_ONLY_CASES)
async def test_async_list_only_endpoints(
    dummy_client, async_paginator_factory, cls, module, model, payload
):
    """Test async list-only endpoints list items and do not expose get."""
    ep = cls(dummy_client)
    assert not hasattr(ep, "get")
    capture = async_paginator_factory(module, [payload])
    result = [item async for item in ep.list(study_key="S1")]
    assert capture["path"] == f"/api/v1/edc/studies/S1/{cls.PATH}"
    assert isinstance(result[0], model)


@pytest.mark.parametrize(("cls", "module", "needs_study", "valid_attr"), ALL_COLLECTION_SYNC)
def test_all_12_endpoints_extract_top_level_query_params_and_enforce_allowlists(
    dummy_client, paginator_factory, cls, module, needs_study, valid_attr
):
    """Test all 12 collection endpoints extract page/size/sort/filter and enforce allowlists."""
    ep = cls(dummy_client)
    capture = paginator_factory(module, [{}])
    base_kwargs = {"study_key": "S1"} if needs_study else {}

    if valid_attr is None:
        list(ep.list(**base_kwargs, page=2, size=25, sort="lastName,asc"))
        assert capture["params"]["page"] == 2
        assert capture["params"]["size"] == 25
        assert capture["params"]["sort"] == "lastName,asc"
        with pytest.raises(ConfigurationError, match="filter"):
            list(ep.list(**base_kwargs, filter="login==admin"))
    else:
        list(
            ep.list(
                **base_kwargs,
                page=1,
                size=50,
                sort=f"{valid_attr},asc",
                filter=f"{valid_attr}==val",
            )
        )
        assert capture["params"]["page"] == 1
        assert capture["params"]["size"] == 50
        assert capture["params"]["sort"] == f"{valid_attr},asc"
        assert capture["params"]["filter"] == f"{valid_attr}==val"

        with pytest.raises(ConfigurationError, match="undocumentedAttr"):
            list(ep.list(**base_kwargs, filter="undocumentedAttr==val"))

        if needs_study:
            with pytest.raises(ConfigurationError, match="studyKey"):
                list(ep.list(**base_kwargs, studyKey="S1"))
            with pytest.raises(ConfigurationError, match="studyKey"):
                list(ep.list(**base_kwargs, filter="studyKey==S1"))

    with pytest.raises(ConfigurationError, match="undocumented_attr"):
        list(ep.list(**base_kwargs, undocumented_attr="val"))


ALL_COLLECTION_ASYNC = [
    (studies.AsyncStudiesEndpoint, studies, False, "studyName"),
    (sites.AsyncSitesEndpoint, sites, True, "siteName"),
    (forms.AsyncFormsEndpoint, forms, True, "formKey"),
    (intervals.AsyncIntervalsEndpoint, intervals, True, "intervalName"),
    (variables.AsyncVariablesEndpoint, variables, True, "variableName"),
    (subjects.AsyncSubjectsEndpoint, subjects, True, "subjectKey"),
    (records.AsyncRecordsEndpoint, records, True, "formKey"),
    (record_revisions.AsyncRecordRevisionsEndpoint, record_revisions, True, "formKey"),
    (visits.AsyncVisitsEndpoint, visits, True, "subjectKey"),
    (codings.AsyncCodingsEndpoint, codings, True, "variable"),
    (queries.AsyncQueriesEndpoint, queries, True, "variable"),
    (users.AsyncUsersEndpoint, users, True, None),
]


class _CountingSyncRequester:
    """Minimal sync requester recording HTTP GET calls and returning multi-page metadata."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get(self, path: str, params: dict[str, object] | None = None) -> object:
        self.calls.append({"path": path, "params": dict(params or {})})
        payload = {"data": [{}], "pagination": {"totalPages": 5}}
        return type("Resp", (), {"json": lambda _self: payload})()


class _CountingAsyncRequester:
    """Minimal async requester recording HTTP GET calls and returning multi-page metadata."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def get(self, path: str, params: dict[str, object] | None = None) -> object:
        self.calls.append({"path": path, "params": dict(params or {})})
        payload = {"data": [{}], "pagination": {"totalPages": 5}}
        return type("Resp", (), {"json": lambda _self: payload})()


@pytest.mark.parametrize(("cls", "_module", "needs_study", "_valid_attr"), ALL_COLLECTION_SYNC)
def test_all_12_sync_endpoints_single_page_and_limit_http_execution(
    cls, _module, needs_study, _valid_attr
):
    """Test sync collection endpoints send 1 HTTP call for page/size/sort and limit=10."""
    base_kwargs = {"study_key": "S1"} if needs_study else {}

    page_client = _CountingSyncRequester()
    ep = cls(page_client)
    items = list(ep.list(**base_kwargs, page=1, size=25, sort="dateCreated,desc"))
    assert len(items) == 1
    assert len(page_client.calls) == 1
    sent_params = page_client.calls[0]["params"]
    assert isinstance(sent_params, dict)
    assert sent_params["page"] == 1
    assert sent_params["size"] == 25
    assert sent_params["sort"] == "dateCreated,desc"
    assert "filter" not in sent_params

    limit_client = _CountingSyncRequester()
    ep_limit = cls(limit_client)
    limit_items = list(ep_limit.list(**base_kwargs, limit=10))
    assert len(limit_items) == 1
    assert len(limit_client.calls) == 1
    limit_params = limit_client.calls[0]["params"]
    assert isinstance(limit_params, dict)
    assert limit_params["page"] == 0
    assert limit_params["size"] == 10
    assert "limit" not in limit_params
    assert "filter" not in limit_params


@pytest.mark.asyncio
@pytest.mark.parametrize(("cls", "_module", "needs_study", "_valid_attr"), ALL_COLLECTION_ASYNC)
async def test_all_12_async_endpoints_single_page_and_limit_http_execution(
    cls, _module, needs_study, _valid_attr
):
    """Test async collection endpoints send 1 HTTP call for page/size/sort and limit=10."""
    base_kwargs = {"study_key": "S1"} if needs_study else {}

    page_client = _CountingAsyncRequester()
    ep = cls(page_client)
    items = [
        item async for item in ep.list(**base_kwargs, page=1, size=25, sort="dateCreated,desc")
    ]
    assert len(items) == 1
    assert len(page_client.calls) == 1
    sent_params = page_client.calls[0]["params"]
    assert isinstance(sent_params, dict)
    assert sent_params["page"] == 1
    assert sent_params["size"] == 25
    assert sent_params["sort"] == "dateCreated,desc"
    assert "filter" not in sent_params

    limit_client = _CountingAsyncRequester()
    ep_limit = cls(limit_client)
    limit_items = [item async for item in ep_limit.list(**base_kwargs, limit=10)]
    assert len(limit_items) == 1
    assert len(limit_client.calls) == 1
    limit_params = limit_client.calls[0]["params"]
    assert isinstance(limit_params, dict)
    assert limit_params["page"] == 0
    assert limit_params["size"] == 10
    assert "limit" not in limit_params
    assert "filter" not in limit_params


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sync_cls", "async_cls", "needs_study", "attr"),
    [
        (s[0], a[0], s[2], s[3])
        for s, a in zip(ALL_COLLECTION_SYNC, ALL_COLLECTION_ASYNC, strict=True)
        if s[3] is not None
    ],
)
async def test_all_filterable_endpoints_or_lists_sync_async_and_dataframe(
    sync_cls, async_cls, needs_study, attr
):
    """Test multi-value OR filters across sync, async, and to_dataframe workflows."""
    base_kwargs = {"study_key": "S1"} if needs_study else {}
    expected_filter = f"{attr}==A,{attr}==B"

    sync_client = _CountingSyncRequester()
    sync_ep = sync_cls(sync_client)
    list(sync_ep.list(**base_kwargs, **{attr: ["A", "B"]}))
    assert len(sync_client.calls) == 5
    assert sync_client.calls[0]["params"]["filter"] == expected_filter  # type: ignore[index]

    df_sync = sync_ep.to_dataframe(**base_kwargs, **{attr: ["A", "B"]})
    assert len(df_sync) == 5
    assert len(sync_client.calls) == 10
    assert sync_client.calls[5]["params"]["filter"] == expected_filter  # type: ignore[index]

    async_client = _CountingAsyncRequester()
    async_ep = async_cls(async_client)
    _ = [item async for item in async_ep.list(**base_kwargs, **{attr: ["A", "B"]})]
    assert len(async_client.calls) == 5
    assert async_client.calls[0]["params"]["filter"] == expected_filter  # type: ignore[index]

    df_async = await async_ep.to_dataframe(**base_kwargs, **{attr: ["A", "B"]})
    assert len(df_async) == 5
    assert len(async_client.calls) == 10
    assert async_client.calls[5]["params"]["filter"] == expected_filter  # type: ignore[index]


@pytest.mark.parametrize(
    ("cls", "module", "attr"),
    [
        (subjects.SubjectsEndpoint, subjects, "subjectKey"),
        (records.RecordsEndpoint, records, "subjectKey"),
        (record_revisions.RecordRevisionsEndpoint, record_revisions, "subjectKey"),
        (queries.QueriesEndpoint, queries, "subjectKey"),
        (visits.VisitsEndpoint, visits, "subjectKey"),
    ],
)
def test_study_subresource_or_list_filters_omit_study_key(
    dummy_client, paginator_factory, cls, module, attr
):
    """Test OR-list filters on sub-resource endpoints succeed and omit studyKey from filter."""
    ep = cls(dummy_client)
    capture = paginator_factory(module, [{}])
    list(ep.list(study_key="S1", **{attr: ["SUB1", "SUB2"]}))
    assert capture["params"] == {"filter": f"{attr}==SUB1,{attr}==SUB2"}
