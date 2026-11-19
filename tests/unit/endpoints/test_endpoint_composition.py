"""Unit tests for endpoint composition."""

import imednet.endpoints.records as records_module
import imednet.endpoints.users as users_module
from imednet.endpoints.codings import AsyncCodingsEndpoint, CodingsEndpoint
from imednet.endpoints.forms import AsyncFormsEndpoint, FormsEndpoint
from imednet.endpoints.intervals import AsyncIntervalsEndpoint, IntervalsEndpoint
from imednet.endpoints.jobs import AsyncJobsEndpoint, JobsEndpoint
from imednet.endpoints.queries import AsyncQueriesEndpoint, QueriesEndpoint
from imednet.endpoints.record_revisions import AsyncRecordRevisionsEndpoint, RecordRevisionsEndpoint
from imednet.endpoints.records import AsyncRecordsEndpoint, RecordsEndpoint
from imednet.endpoints.sites import AsyncSitesEndpoint, SitesEndpoint
from imednet.endpoints.studies import AsyncStudiesEndpoint, StudiesEndpoint
from imednet.endpoints.subjects import AsyncSubjectsEndpoint, SubjectsEndpoint
from imednet.endpoints.users import AsyncUsersEndpoint, UsersEndpoint
from imednet.endpoints.variables import AsyncVariablesEndpoint, VariablesEndpoint
from imednet.endpoints.visits import AsyncVisitsEndpoint, VisitsEndpoint

ALL_ENDPOINT_CLASSES = [
    CodingsEndpoint,
    FormsEndpoint,
    IntervalsEndpoint,
    JobsEndpoint,
    QueriesEndpoint,
    RecordRevisionsEndpoint,
    RecordsEndpoint,
    SitesEndpoint,
    StudiesEndpoint,
    SubjectsEndpoint,
    UsersEndpoint,
    VariablesEndpoint,
    VisitsEndpoint,
]

ALL_ASYNC_ENDPOINT_CLASSES = [
    AsyncCodingsEndpoint,
    AsyncFormsEndpoint,
    AsyncIntervalsEndpoint,
    AsyncJobsEndpoint,
    AsyncQueriesEndpoint,
    AsyncRecordRevisionsEndpoint,
    AsyncRecordsEndpoint,
    AsyncSitesEndpoint,
    AsyncStudiesEndpoint,
    AsyncSubjectsEndpoint,
    AsyncUsersEndpoint,
    AsyncVariablesEndpoint,
    AsyncVisitsEndpoint,
]


def test_records_endpoint_keeps_public_read_api(dummy_client, paginator_factory):
    """Test that records endpoint keeps public read api."""
    endpoint = RecordsEndpoint(dummy_client)
    capture = paginator_factory(records_module, [{"recordId": 1}])

    listed = endpoint.list(study_key="S1")
    fetched = endpoint.get("S1", 1)

    assert callable(endpoint.list)
    assert callable(endpoint.get)
    assert listed[0].record_id == 1
    assert fetched.record_id == 1
    assert capture["path"] == "/api/v1/edc/studies/S1/records"


def test_jobs_endpoint_keeps_public_read_api(dummy_client, response_factory):
    """Test that jobs endpoint keeps public read api."""
    endpoint = JobsEndpoint(dummy_client)
    dummy_client.get.return_value = response_factory({"jobId": "1"})

    result = endpoint.get("S1", "B1")

    assert not hasattr(endpoint, "list")
    assert not hasattr(JobsEndpoint, "list")
    assert not hasattr(AsyncJobsEndpoint, "list")
    assert callable(endpoint.get)
    assert result.job_id == "1"
    assert "ListEndpointMixin" not in {cls.__name__ for cls in JobsEndpoint.__mro__}
    assert "PathGetEndpointMixin" not in {cls.__name__ for cls in JobsEndpoint.__mro__}


def test_users_endpoint_keeps_public_read_api(dummy_client, paginator_factory):
    """Test that users endpoint keeps public read api without get."""
    endpoint = UsersEndpoint(dummy_client)
    capture = paginator_factory(users_module, [{"userId": 1}])

    listed = endpoint.list(study_key="S1")

    assert callable(endpoint.list)
    assert not hasattr(endpoint, "get")
    assert not hasattr(UsersEndpoint, "get")
    assert not hasattr(AsyncUsersEndpoint, "get")
    assert listed[0].user_id == "1"
    assert capture["path"] == "/api/v1/edc/studies/S1/users"


LIST_ONLY_SYNC = {CodingsEndpoint, QueriesEndpoint, UsersEndpoint, VisitsEndpoint}
LIST_ONLY_ASYNC = {
    AsyncCodingsEndpoint,
    AsyncQueriesEndpoint,
    AsyncUsersEndpoint,
    AsyncVisitsEndpoint,
}


def test_list_only_endpoints_expose_no_get():
    """List-only endpoints must not expose single-item get methods."""
    for sync_cls in LIST_ONLY_SYNC:
        assert not hasattr(sync_cls, "get")
    for async_cls in LIST_ONLY_ASYNC:
        assert not hasattr(async_cls, "get")


def test_all_endpoints_inherit_from_single_edc_base():
    """Endpoints must have their respective EDC base as their sole direct parent."""
    for endpoint_cls in ALL_ENDPOINT_CLASSES:
        direct_bases = endpoint_cls.__bases__
        base_names = {b.__name__ for b in direct_bases if not b.__name__.endswith("OperationDef")}
        if endpoint_cls is JobsEndpoint:
            expected_base = "EdcSyncGetEndpoint"
        elif endpoint_cls in LIST_ONLY_SYNC:
            expected_base = "EdcSyncListEndpoint"
        else:
            expected_base = "EdcSyncListGetEndpoint"
        assert base_names == {expected_base}, (
            f"{endpoint_cls.__name__} has unexpected direct bases: {base_names}"
        )


def test_all_async_endpoints_inherit_from_single_edc_base():
    """Async endpoints must have their respective EDC base as their sole direct parent."""
    for endpoint_cls in ALL_ASYNC_ENDPOINT_CLASSES:
        base_names = {
            b.__name__ for b in endpoint_cls.__bases__ if not b.__name__.endswith("OperationDef")
        }
        if endpoint_cls is AsyncJobsEndpoint:
            expected_base = "EdcAsyncGetEndpoint"
        elif endpoint_cls in LIST_ONLY_ASYNC:
            expected_base = "EdcAsyncListEndpoint"
        else:
            expected_base = "EdcAsyncListGetEndpoint"
        assert base_names == {expected_base}, (
            f"{endpoint_cls.__name__} has unexpected direct bases: {base_names}"
        )


def test_class_level_dispatch_descriptors_expose_unbound_callables_and_metadata():
    """Class access on execute_operation, execute_list, and execute_get returns wrapped unbound callables."""
    import inspect
    from collections.abc import AsyncIterator, Iterator

    from imednet.core.endpoint.dispatch import execute_get, execute_list, execute_operation
    from imednet.models.jobs import Job, JobStatus
    from imednet.models.records import Record
    from imednet.models.subjects import Subject

    raw_create = RecordsEndpoint.__mro__[1].__dict__["create"]
    assert isinstance(raw_create, execute_operation)
    assert raw_create.__get__(None, None) is raw_create
    assert raw_create.__name__ == "create"
    assert raw_create.__doc__ and "Create one or more records" in raw_create.__doc__

    raw_list = next(c.__dict__["list"] for c in RecordsEndpoint.__mro__ if "list" in c.__dict__)
    assert isinstance(raw_list, execute_list)
    assert raw_list.__get__(None, None) is raw_list

    raw_get = next(c.__dict__["get"] for c in RecordsEndpoint.__mro__ if "get" in c.__dict__)
    assert isinstance(raw_get, execute_get)
    assert raw_get.__get__(None, None) is raw_get

    assert callable(RecordsEndpoint.list)
    assert not inspect.iscoroutinefunction(RecordsEndpoint.list)
    assert inspect.signature(RecordsEndpoint.list).return_annotation == Iterator[Record]

    assert callable(RecordsEndpoint.get)
    assert not inspect.iscoroutinefunction(RecordsEndpoint.get)
    assert inspect.signature(RecordsEndpoint.get).return_annotation is Record

    assert callable(RecordsEndpoint.create)
    assert not inspect.iscoroutinefunction(RecordsEndpoint.create)
    assert inspect.signature(RecordsEndpoint.create).return_annotation is Job

    assert callable(SubjectsEndpoint.list_by_site)
    assert not inspect.iscoroutinefunction(SubjectsEndpoint.list_by_site)
    assert inspect.signature(SubjectsEndpoint.list_by_site).return_annotation == list[Subject]

    assert callable(JobsEndpoint.get)
    assert not inspect.iscoroutinefunction(JobsEndpoint.get)
    assert inspect.signature(JobsEndpoint.get).return_annotation is JobStatus

    assert callable(AsyncRecordsEndpoint.list)
    assert not inspect.iscoroutinefunction(AsyncRecordsEndpoint.list)
    assert inspect.signature(AsyncRecordsEndpoint.list).return_annotation == AsyncIterator[Record]

    assert callable(AsyncRecordsEndpoint.get)
    assert inspect.iscoroutinefunction(AsyncRecordsEndpoint.get)
    assert inspect.signature(AsyncRecordsEndpoint.get).return_annotation is Record

    assert callable(AsyncRecordsEndpoint.create)
    assert inspect.iscoroutinefunction(AsyncRecordsEndpoint.create)
    assert inspect.signature(AsyncRecordsEndpoint.create).return_annotation is Job

    assert callable(AsyncSubjectsEndpoint.list_by_site)
    assert inspect.iscoroutinefunction(AsyncSubjectsEndpoint.list_by_site)
    assert inspect.signature(AsyncSubjectsEndpoint.list_by_site).return_annotation == list[Subject]

    assert callable(AsyncJobsEndpoint.get)
    assert inspect.iscoroutinefunction(AsyncJobsEndpoint.get)
    assert inspect.signature(AsyncJobsEndpoint.get).return_annotation is JobStatus


async def test_unbound_sync_and_async_descriptors_execute_correctly(
    dummy_client, paginator_factory, async_paginator_factory, response_factory
):
    """Unbound endpoint callables execute identically to bound endpoint methods."""
    from unittest.mock import AsyncMock

    sync_records = RecordsEndpoint(dummy_client)
    paginator_factory(records_module, [{"recordId": 7}])
    assert list(RecordsEndpoint.list(sync_records, study_key="S1"))[0].record_id == 7
    assert RecordsEndpoint.get(sync_records, "S1", 7).record_id == 7

    sync_jobs = JobsEndpoint(dummy_client)
    dummy_client.get.return_value = response_factory({"jobId": "J1"})
    assert JobsEndpoint.get(sync_jobs, "S1", "B1").job_id == "J1"

    async_client = AsyncMock()
    async_records = AsyncRecordsEndpoint(async_client=async_client)
    async_paginator_factory(records_module, [{"recordId": 9}])
    items = [r async for r in AsyncRecordsEndpoint.list(async_records, study_key="S1")]
    assert items[0].record_id == 9
    fetched = await AsyncRecordsEndpoint.get(async_records, "S1", 9)
    assert fetched.record_id == 9

    async_jobs = AsyncJobsEndpoint(async_client=async_client)
    async_client.get.return_value = response_factory({"jobId": "AJ1"})
    async_job = await AsyncJobsEndpoint.get(async_jobs, "S1", "B1")
    assert async_job.job_id == "AJ1"
