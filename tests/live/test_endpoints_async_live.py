"""Unit tests for endpoints async live."""

from typing import Any

import pytest

from imednet.sdk import AsyncImednetSDK
from tests.live.helpers import require_mutation


@pytest.mark.asyncio(scope="session")
async def test_async_sites(async_sdk: AsyncImednetSDK, study_key: str) -> None:
    """Test that async sites asynchronously."""
    sites = [item async for item in async_sdk.sites.list(study_key)]
    assert isinstance(sites, list)
    if sites:
        site = await async_sdk.sites.get(study_key, sites[0].site_id)
        assert site.site_id == sites[0].site_id


@pytest.mark.asyncio(scope="session")
async def test_async_subjects(async_sdk: AsyncImednetSDK, study_key: str) -> None:
    """Test that async subjects asynchronously."""
    subjects = [item async for item in async_sdk.subjects.list(study_key)]
    assert isinstance(subjects, list)
    if subjects:
        subject = await async_sdk.subjects.get(study_key, subjects[0].subject_key)
        assert subject.subject_key == subjects[0].subject_key


@pytest.mark.asyncio(scope="session")
async def test_async_records(async_sdk: AsyncImednetSDK, study_key: str) -> None:
    """Test that async records asynchronously."""
    records = [item async for item in async_sdk.records.list(study_key)]
    assert isinstance(records, list)
    if records:
        record = await async_sdk.records.get(study_key, records[0].record_id)
        assert record.record_id == records[0].record_id


@pytest.mark.asyncio(scope="session")
async def test_async_intervals(async_sdk: AsyncImednetSDK, study_key: str) -> None:
    """Test that async intervals asynchronously."""
    intervals = [item async for item in async_sdk.intervals.list(study_key)]
    assert isinstance(intervals, list)
    if intervals:
        interval = await async_sdk.intervals.get(study_key, intervals[0].interval_id)
        assert interval.interval_id == intervals[0].interval_id


@pytest.mark.asyncio(scope="session")
async def test_async_visits(async_sdk: AsyncImednetSDK, study_key: str) -> None:
    """Test that async visits asynchronously."""
    visits = [item async for item in async_sdk.visits.list(study_key)]
    assert isinstance(visits, list)
    if visits:
        visit = await async_sdk.visits.get(study_key, visits[0].visit_id)
        assert visit.visit_id == visits[0].visit_id


@pytest.mark.asyncio(scope="session")
async def test_async_variables(async_sdk: AsyncImednetSDK, study_key: str) -> None:
    """Test that async variables asynchronously."""
    variables = [item async for item in async_sdk.variables.list(study_key)]
    assert isinstance(variables, list)
    if variables:
        variable = await async_sdk.variables.get(study_key, variables[0].variable_id)
        assert variable.variable_id == variables[0].variable_id


@pytest.mark.asyncio(scope="session")
async def test_async_forms(async_sdk: AsyncImednetSDK, study_key: str) -> None:
    """Test that async forms asynchronously."""
    forms = [item async for item in async_sdk.forms.list(study_key)]
    assert isinstance(forms, list)
    if forms:
        form = await async_sdk.forms.get(study_key, forms[0].form_id)
        assert form.form_id == forms[0].form_id


@pytest.mark.asyncio(scope="session")
async def test_async_queries(async_sdk: AsyncImednetSDK, study_key: str) -> None:
    """Test that async queries asynchronously."""
    queries = [item async for item in async_sdk.queries.list(study_key)]
    assert isinstance(queries, list)
    if queries:
        query = await async_sdk.queries.get(study_key, queries[0].annotation_id)
        assert query.annotation_id == queries[0].annotation_id


@pytest.mark.asyncio(scope="session")
async def test_async_record_revisions(async_sdk: AsyncImednetSDK, study_key: str) -> None:
    """Test that async record revisions asynchronously."""
    revisions = [item async for item in async_sdk.record_revisions.list(study_key)]
    assert isinstance(revisions, list)
    if revisions:
        rev = await async_sdk.record_revisions.get(study_key, revisions[0].record_revision_id)
        assert rev.record_revision_id == revisions[0].record_revision_id


@pytest.mark.asyncio(scope="session")
async def test_async_users(async_sdk: AsyncImednetSDK, study_key: str) -> None:
    """Test that async users asynchronously."""
    assert not hasattr(async_sdk.users, "get")
    users = [item async for item in async_sdk.users.list(study_key)]
    assert isinstance(users, list)
    if users:
        assert isinstance(users[0], User)


@pytest.mark.asyncio(scope="session")
async def test_async_codings(async_sdk: AsyncImednetSDK, study_key: str) -> None:
    """Test that async codings asynchronously."""
    codings = [item async for item in async_sdk.codings.list(study_key)]
    assert isinstance(codings, list)
    if codings:
        coding = await async_sdk.codings.get(study_key, str(codings[0].coding_id))
        assert coding.coding_id == codings[0].coding_id


@pytest.mark.asyncio(scope="session")
@pytest.mark.parametrize(
    "record_payload",
    ["register", "scheduled", "new"],
    indirect=True,
)
async def test_async_create_and_poll(
    async_sdk: AsyncImednetSDK,
    study_key: str,
    record_payload: dict[str, Any],
) -> None:
    """Test that async create and poll asynchronously."""
    require_mutation()
    job = await async_sdk.records.create(study_key, [record_payload])
    if not job.batch_id:
        pytest.skip("Job completed synchronously without a batch ID")
    polled = await async_sdk.jobs.get(study_key, job.batch_id)
    assert polled.batch_id == job.batch_id
