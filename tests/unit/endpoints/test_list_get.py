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
    (codings.CodingsEndpoint, codings, Coding, "C1"),
    (forms.FormsEndpoint, forms, Form, 1),
    (intervals.IntervalsEndpoint, intervals, Interval, 1),
    (queries.QueriesEndpoint, queries, Query, 1),
    (record_revisions.RecordRevisionsEndpoint, record_revisions, RecordRevision, 1),
    (records.RecordsEndpoint, records, Record, 1),
    (sites.SitesEndpoint, sites, Site, 1),
    (studies.StudiesEndpoint, studies, Study, "S1"),
    (subjects.SubjectsEndpoint, subjects, Subject, "SUB"),
    (users.UsersEndpoint, users, User, 1),
    (variables.VariablesEndpoint, variables, Variable, 1),
    (visits.VisitsEndpoint, visits, Visit, 1),
]

ASYNC_CASES = [
    (codings.AsyncCodingsEndpoint, codings, Coding, "C1"),
    (forms.AsyncFormsEndpoint, forms, Form, 1),
    (intervals.AsyncIntervalsEndpoint, intervals, Interval, 1),
    (queries.AsyncQueriesEndpoint, queries, Query, 1),
    (record_revisions.AsyncRecordRevisionsEndpoint, record_revisions, RecordRevision, 1),
    (records.AsyncRecordsEndpoint, records, Record, 1),
    (sites.AsyncSitesEndpoint, sites, Site, 1),
    (studies.AsyncStudiesEndpoint, studies, Study, "S1"),
    (subjects.AsyncSubjectsEndpoint, subjects, Subject, "SUB"),
    (users.AsyncUsersEndpoint, users, User, 1),
    (variables.AsyncVariablesEndpoint, variables, Variable, 1),
    (visits.AsyncVisitsEndpoint, visits, Visit, 1),
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
