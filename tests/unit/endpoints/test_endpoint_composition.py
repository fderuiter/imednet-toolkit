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
