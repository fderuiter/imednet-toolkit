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

    assert callable(raw_create)
    assert callable(raw_list)
    assert callable(raw_get)
    assert "records_data" in inspect.signature(raw_create).parameters
    assert "study_key" in inspect.signature(raw_list).parameters
    assert "item_id" in inspect.signature(raw_get).parameters

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


def test_generic_edc_endpoint_bases_preserve_typevar_annotations():
    """Generic Edc*Endpoint bases preserve TypeVar T rather than collapsing to Any."""
    import inspect
    from collections.abc import AsyncIterator, Iterator
    from typing import TypeVar, get_args, get_origin

    from imednet.core import (
        EdcAsyncGetEndpoint,
        EdcAsyncListEndpoint,
        EdcAsyncListGetEndpoint,
        EdcSyncGetEndpoint,
        EdcSyncListEndpoint,
        EdcSyncListGetEndpoint,
    )

    sync_list_ret = inspect.signature(EdcSyncListEndpoint.list).return_annotation
    assert get_origin(sync_list_ret) is Iterator
    assert isinstance(get_args(sync_list_ret)[0], TypeVar)

    sync_list_get_ret = inspect.signature(EdcSyncListGetEndpoint.list).return_annotation
    assert get_origin(sync_list_get_ret) is Iterator
    assert isinstance(get_args(sync_list_get_ret)[0], TypeVar)
    assert isinstance(inspect.signature(EdcSyncGetEndpoint.get).return_annotation, TypeVar)
    assert isinstance(inspect.signature(EdcSyncListGetEndpoint.get).return_annotation, TypeVar)

    async_list_ret = inspect.signature(EdcAsyncListEndpoint.list).return_annotation
    assert get_origin(async_list_ret) is AsyncIterator
    assert isinstance(get_args(async_list_ret)[0], TypeVar)

    async_list_get_ret = inspect.signature(EdcAsyncListGetEndpoint.list).return_annotation
    assert get_origin(async_list_get_ret) is AsyncIterator
    assert isinstance(get_args(async_list_get_ret)[0], TypeVar)
    assert inspect.iscoroutinefunction(EdcAsyncGetEndpoint.get)
    assert inspect.iscoroutinefunction(EdcAsyncListGetEndpoint.get)
    assert isinstance(inspect.signature(EdcAsyncGetEndpoint.get).return_annotation, TypeVar)
    assert isinstance(inspect.signature(EdcAsyncListGetEndpoint.get).return_annotation, TypeVar)


async def test_sdk_list_operation_descriptor_metadata_and_callable_inspection():
    """_ListOperation copies wrapper metadata and supports callable/signature inspection and unbound calls."""
    import inspect
    from unittest.mock import AsyncMock, MagicMock

    from imednet.models.records import Record
    from imednet.models.studies import Study
    from imednet.sdk import AsyncImednetSDK, ImednetSDK
    from imednet.sdk_convenience import AsyncSDKConvenienceMixin, SyncSDKConvenienceMixin

    raw_sync = SyncSDKConvenienceMixin.__dict__["get_records"]
    assert raw_sync.__get__(None, None) is raw_sync
    assert callable(raw_sync)
    assert raw_sync.__name__ == "get_records"
    assert raw_sync.__doc__ == "List records."
    assert inspect.signature(raw_sync).return_annotation == list[Record]

    unbound_sync = ImednetSDK.get_records
    assert callable(unbound_sync)
    assert not inspect.iscoroutinefunction(unbound_sync)
    sync_sig = inspect.signature(unbound_sync)
    assert list(sync_sig.parameters) == ["self", "study_key", "filters"]
    assert sync_sig.parameters["self"].annotation is inspect.Parameter.empty
    assert sync_sig.return_annotation == list[Record]
    assert "<locals>" not in unbound_sync.__qualname__

    raw_async = AsyncSDKConvenienceMixin.__dict__["async_get_studies"]
    assert raw_async.__get__(None, None) is raw_async
    assert callable(raw_async)
    assert raw_async.__name__ == "async_get_studies"
    assert raw_async.__doc__ == "Asynchronously list studies."

    unbound_async = AsyncImednetSDK.async_get_studies
    assert callable(unbound_async)
    assert inspect.iscoroutinefunction(unbound_async)
    async_sig = inspect.signature(unbound_async)
    assert list(async_sig.parameters) == ["self", "study_key", "filters"]
    assert async_sig.parameters["self"].annotation is inspect.Parameter.empty
    assert async_sig.return_annotation == list[Study]
    assert "<locals>" not in unbound_async.__qualname__

    fake_sync_sdk = MagicMock()
    fake_sync_sdk.records.list.return_value = ["r1", "r2"]
    assert raw_sync(fake_sync_sdk, "STUDY_1", page_size=10) == ["r1", "r2"]
    assert ImednetSDK.get_records(fake_sync_sdk, "STUDY_1") == ["r1", "r2"]

    async def _aiter_studies():
        yield "s1"

    fake_async_sdk = AsyncMock()
    fake_async_sdk.studies = MagicMock()
    fake_async_sdk.studies.list.return_value = _aiter_studies()
    assert await AsyncImednetSDK.async_get_studies(fake_async_sdk, "STUDY_1") == ["s1"]


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
