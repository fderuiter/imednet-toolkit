"""Unit tests for endpoints async."""

from unittest.mock import AsyncMock

import pytest

from imednet.core.context import set_study_context
from imednet.endpoints import (
    codings,
    forms,
    intervals,
    jobs,
    queries,
    record_revisions,
    records,
    sites,
    subjects,
    users,
    variables,
    visits,
)
from imednet.errors import NotFoundError
from imednet.models.codings import Coding
from imednet.models.forms import Form
from imednet.models.intervals import Interval
from imednet.models.jobs import JobStatus
from imednet.models.queries import Query
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.subjects import Subject
from imednet.models.users import User
from imednet.models.variables import Variable
from imednet.models.visits import Visit


@pytest.mark.asyncio
async def test_async_list_records(dummy_client, async_paginator_factory, patch_build_filter):
    """Test that async list records omits studyKey from filter and maps recordDataFilter."""
    ep = records.AsyncRecordsEndpoint(dummy_client)
    captured = async_paginator_factory(records, [{"recordId": 1}])
    filter_capture = patch_build_filter(records)
    result = await ep.list(study_key="S1", record_data_filter="x", record_status="Open")
    assert captured["path"] == "/api/v1/edc/studies/S1/records"
    assert captured["params"] == {"filter": "FILTERED", "recordDataFilter": "x"}
    assert filter_capture["filters"] == {"record_status": "Open"}
    assert isinstance(result[0], Record)


@pytest.mark.asyncio
async def test_async_list_codings(dummy_client, async_paginator_factory):
    """Test that async list codings asynchronously."""
    ep = codings.AsyncCodingsEndpoint(dummy_client)
    captured = async_paginator_factory(codings, [{"codingId": 1}])
    result = await ep.list(study_key="S1")
    assert captured["path"] == "/api/v1/edc/studies/S1/codings"
    assert isinstance(result[0], Coding)


@pytest.mark.asyncio
async def test_async_list_forms(dummy_client, async_paginator_factory):
    """Test that async list forms asynchronously."""
    set_study_context("S1")
    ep = forms.AsyncFormsEndpoint(dummy_client)
    captured = async_paginator_factory(forms, [{"formId": 1}])
    result = await ep.list()
    assert captured["path"] == "/api/v1/edc/studies/S1/forms"
    assert isinstance(result[0], Form)


@pytest.mark.asyncio
async def test_async_list_intervals(dummy_client, async_paginator_factory):
    """Test that async list intervals asynchronously."""
    set_study_context("S1")
    ep = intervals.AsyncIntervalsEndpoint(dummy_client)
    captured = async_paginator_factory(intervals, [{"intervalId": 1}])
    result = await ep.list()
    assert captured["path"] == "/api/v1/edc/studies/S1/intervals"
    assert isinstance(result[0], Interval)


@pytest.mark.asyncio
async def test_async_list_queries(dummy_client, async_paginator_factory):
    """Test that async list queries asynchronously."""
    set_study_context("S1")
    ep = queries.AsyncQueriesEndpoint(dummy_client)
    captured = async_paginator_factory(queries, [{"annotationId": 1}])
    result = await ep.list(variable="AETERM")
    assert captured["path"] == "/api/v1/edc/studies/S1/queries"
    assert isinstance(result[0], Query)


@pytest.mark.asyncio
async def test_async_list_record_revisions(dummy_client, async_paginator_factory):
    """Test that async list record revisions asynchronously."""
    set_study_context("S1")
    ep = record_revisions.AsyncRecordRevisionsEndpoint(dummy_client)
    captured = async_paginator_factory(record_revisions, [{"recordRevisionId": 1}])
    result = await ep.list(record_status="Open")
    assert captured["path"] == "/api/v1/edc/studies/S1/recordRevisions"
    assert isinstance(result[0], RecordRevision)


@pytest.mark.asyncio
async def test_async_list_sites(dummy_client, async_paginator_factory):
    """Test that async list sites asynchronously."""
    ep = sites.AsyncSitesEndpoint(dummy_client)
    captured = async_paginator_factory(sites, [{"siteId": 1}])
    result = await ep.list(study_key="S1")
    assert captured["path"] == "/api/v1/edc/studies/S1/sites"
    assert isinstance(result[0], Site)


@pytest.mark.asyncio
async def test_async_list_subjects(dummy_client, async_paginator_factory):
    """Test that async list subjects asynchronously."""
    set_study_context("S1")
    ep = subjects.AsyncSubjectsEndpoint(dummy_client)
    captured = async_paginator_factory(subjects, [{"subjectKey": "x"}])
    result = await ep.list()
    assert captured["path"] == "/api/v1/edc/studies/S1/subjects"
    assert isinstance(result[0], Subject)


@pytest.mark.asyncio
async def test_async_list_users(dummy_client, async_paginator_factory):
    """Test that async list users asynchronously."""
    ep = users.AsyncUsersEndpoint(dummy_client)
    captured = async_paginator_factory(users, [{"userId": 1}])
    result = await ep.list(study_key="S1", include_inactive=True)
    assert captured["path"] == "/api/v1/edc/studies/S1/users"
    assert captured["params"] == {"includeInactive": "true"}
    assert isinstance(result[0], User)


@pytest.mark.asyncio
async def test_async_list_variables(dummy_client, async_paginator_factory):
    """Test that async list variables asynchronously."""
    ep = variables.AsyncVariablesEndpoint(dummy_client)
    captured = async_paginator_factory(variables, [{"variableId": 1}])
    result = await ep.list(study_key="S1")
    assert captured["path"] == "/api/v1/edc/studies/S1/variables"
    assert isinstance(result[0], Variable)


@pytest.mark.asyncio
async def test_async_list_visits(dummy_client, async_paginator_factory):
    """Test that async list visits asynchronously."""
    set_study_context("S1")
    ep = visits.AsyncVisitsEndpoint(dummy_client)
    captured = async_paginator_factory(visits, [{"visitId": 1}])
    result = await ep.list(interval_name="Baseline")
    assert captured["path"] == "/api/v1/edc/studies/S1/visits"
    assert isinstance(result[0], Visit)


@pytest.mark.asyncio
async def test_async_get_job(dummy_client, response_factory):
    """Test that async get job asynchronously."""
    ep = jobs.AsyncJobsEndpoint(dummy_client)

    async def fake_get(path):
        """Helper function to fake get."""
        assert path == "/api/v1/edc/studies/S1/jobs/B1"
        return response_factory({"jobId": "1"})

    dummy_client.get = fake_get
    result = await ep.get("S1", "B1")
    assert isinstance(result, JobStatus)


@pytest.mark.asyncio
async def test_async_get_record(monkeypatch, dummy_client, response_factory):
    """Test that async get record asynchronously."""
    ep = records.AsyncRecordsEndpoint(dummy_client)
    called = {}

    async def fake_impl(self, client, paginator, *, study_key=None, **filters):
        """Helper function to fake impl."""
        called["study_key"] = study_key
        called["filters"] = filters
        return [Record(record_id=1)]

    monkeypatch.setattr(records.AsyncRecordsEndpoint, "_list_async", fake_impl)

    rec = await ep.get("S1", 1)

    assert called == {"study_key": "S1", "filters": {"recordId": 1}}
    assert isinstance(rec, Record)


@pytest.mark.asyncio
async def test_async_get_record_not_found(monkeypatch, dummy_client, response_factory):
    """Test that async get record not found asynchronously."""
    ep = records.AsyncRecordsEndpoint(dummy_client)

    async def fake_impl(self, client, paginator, *, study_key=None, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(records.AsyncRecordsEndpoint, "_list_async", fake_impl)

    with pytest.raises(NotFoundError):
        await ep.get("S1", 1)


@pytest.mark.asyncio
async def test_async_create_record(dummy_client, response_factory):
    """Test that AsyncRecordsEndpoint.create parses state='created' into JobState.CREATED."""
    from imednet.errors import ValidationError
    from imednet.models.jobs import JobState

    ep = records.AsyncRecordsEndpoint(dummy_client)
    dummy_client.post = AsyncMock(
        return_value=response_factory({"jobId": "1", "batchId": "b1", "state": "created"})
    )

    job = await ep.create(
        "S1",
        [{"formKey": "F1", "data": {"foo": "bar"}}],
        email_notify="user@test.com",
    )

    dummy_client.post.assert_awaited_once_with(
        "/api/v1/edc/studies/S1/records",
        json=[{"formKey": "F1", "data": {"foo": "bar"}}],
        headers={"x-email-notify": "user@test.com"},
    )
    assert isinstance(job, records.Job)
    assert job.job_id == "1"
    assert job.batch_id == "b1"
    assert job.state == JobState.CREATED

    dummy_client.post.reset_mock()
    with pytest.raises(ValidationError):
        await ep.create("S1", [{"formKey": "F1", "data": {}}], email_notify=False)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await ep.create("S1", [{"formKey": "F1", "recordData": {"foo": "bar"}}])
    dummy_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_create_typed_and_raw_strict_ids(dummy_client, response_factory, monkeypatch):
    """Verify both typed request models and raw dicts enforce strict IDs through async create and async_create_record."""
    from pydantic import ValidationError as PydanticValidationError

    from imednet.errors import ValidationError
    from imednet.models.jobs import JobState
    from imednet.models.records import (
        CreateNewRecordRequest,
        RecordData,
        RecordRequest,
        RegisterSubjectRequest,
        UpdateScheduledRecordRequest,
    )
    from imednet.sdk import AsyncImednetSDK

    ep = records.AsyncRecordsEndpoint(dummy_client)
    dummy_client.post = AsyncMock(
        return_value=response_factory({"jobId": "20", "batchId": "b20", "state": "created"})
    )

    typed_batch = [
        RegisterSubjectRequest(formId=1, siteId=2, data=RecordData({"enroll": "yes"})),
        CreateNewRecordRequest(
            formKey="AE", subjectId=3, recordId=4, data=RecordData({"term": "nausea"})
        ),
        UpdateScheduledRecordRequest(
            formId=5, subjectKey="S-1", intervalId=6, data=RecordData({"dia": 80})
        ),
        RecordRequest(formKey="DEMOG", siteId=7, data=RecordData({"age": 41})),
    ]
    res = await ep.create("S1", typed_batch)
    assert res.state == JobState.CREATED
    dummy_client.post.assert_awaited_once_with(
        "/api/v1/edc/studies/S1/records",
        json=[
            {"formId": 1, "siteId": 2, "data": {"enroll": "yes"}},
            {"formKey": "AE", "subjectId": 3, "recordId": 4, "data": {"term": "nausea"}},
            {"formId": 5, "subjectKey": "S-1", "intervalId": 6, "data": {"dia": 80}},
            {"formKey": "DEMOG", "siteId": 7, "data": {"age": 41}},
        ],
        headers={},
    )

    dummy_client.post.reset_mock()
    async_sdk = AsyncImednetSDK(api_key="k", security_key="s", base_url="https://example.com")
    monkeypatch.setattr(async_sdk.records, "_async_client", dummy_client)

    sdk_job = await async_sdk.async_create_record(
        "S1",
        [
            RecordRequest(formId=9, siteId=2, data=RecordData({"ok": 1})),
            {"formId": 10, "siteId": 3, "data": {"ok": 2}},
        ],
    )
    assert sdk_job.state == JobState.CREATED
    dummy_client.post.assert_awaited_once_with(
        "/api/v1/edc/studies/S1/records",
        json=[
            {"formId": 9, "siteId": 2, "data": {"ok": 1}},
            {"formId": 10, "siteId": 3, "data": {"ok": 2}},
        ],
        headers={},
    )

    dummy_client.post.reset_mock()
    for bad_id in ["10", 10.0, True, False, 0, -3]:
        for builder in (
            lambda v=bad_id: RecordRequest(formId=v),  # type: ignore[arg-type]
            lambda v=bad_id: RegisterSubjectRequest(formKey="F1", siteId=v),  # type: ignore[arg-type]
            lambda v=bad_id: CreateNewRecordRequest(formKey="F1", subjectId=v),  # type: ignore[arg-type]
            lambda v=bad_id: CreateNewRecordRequest(formKey="F1", recordId=v),  # type: ignore[arg-type]
            lambda v=bad_id: UpdateScheduledRecordRequest(formKey="F1", intervalId=v),  # type: ignore[arg-type]
        ):
            with pytest.raises(PydanticValidationError):
                builder()

        for bad_payload in (
            [{"formId": bad_id, "data": {}}],
            [{"formKey": "F1", "siteId": bad_id, "data": {}}],
            [{"formKey": "F1", "subjectId": bad_id, "data": {}}],
            [{"formKey": "F1", "intervalId": bad_id, "data": {}}],
            [{"formKey": "F1", "recordId": bad_id, "data": {}}],
            [RecordRequest.model_construct(form_id=bad_id, data=RecordData({}))],
            [
                RegisterSubjectRequest.model_construct(
                    form_key="F1", site_id=bad_id, data=RecordData({})
                )
            ],
        ):
            with pytest.raises(ValidationError):
                await ep.create("S1", bad_payload)  # type: ignore[arg-type]
            with pytest.raises(ValidationError):
                await async_sdk.async_create_record("S1", bad_payload)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        await ep.create("S1", [RegisterSubjectRequest(siteName="Site 1")])
    with pytest.raises(ValidationError):
        await async_sdk.async_create_record("S1", [RegisterSubjectRequest(siteName="Site 1")])
    dummy_client.post.assert_not_awaited()
