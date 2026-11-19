from collections.abc import AsyncIterator, Iterator
from typing import Literal, assert_type

from imednet import (
    AsyncImednetSDK,
    AsyncWorkflowsNamespaceProtocol,
    ImednetSDK,
    SinksNamespaceProtocol,
    WorkflowsNamespaceProtocol,
)
from imednet.core.endpoint import EdcAsyncListEndpoint, EdcSyncListEndpoint
from imednet.core.endpoint.dispatch import AsyncEndpointContext, SyncEndpointContext
from imednet.endpoints.studies import AsyncStudiesEndpoint, StudiesEndpoint, StudiesOperationDef
from imednet.models.codings import Coding
from imednet.models.forms import Form
from imednet.models.intervals import Interval
from imednet.models.jobs import Job, JobState, JobStatus
from imednet.models.queries import Query, QueryStatus
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.studies import Study
from imednet.models.subjects import Subject, SubjectKeyword
from imednet.models.users import User
from imednet.models.variables import Variable
from imednet.models.visits import Visit
from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
from imednet.utils.batch_coordinator import BatchSubmissionSummary
from imednet.utils.job_poller import JobPollSummary


def sync_contract(sdk: ImednetSDK) -> None:
    facade: ImednetFacade = sdk
    assert_type(facade, ImednetFacade)

    # All 13 endpoints on ImednetSDK
    assert_type(sdk.codings.list('S'), Iterator[Coding])
    assert_type(sdk.forms.list('S'), Iterator[Form])
    assert_type(sdk.forms.get('S', 1), Form)
    assert_type(sdk.intervals.list('S'), Iterator[Interval])
    assert_type(sdk.intervals.get('S', 1), Interval)
    assert_type(sdk.jobs.get('S', 'B'), JobStatus)
    assert_type(sdk.jobs.poll('S', 'B'), JobStatus)
    assert_type(sdk.jobs.poll_many('S', ['B1', 'B2']), JobPollSummary)
    assert_type(sdk.queries.list('S'), Iterator[Query])
    assert_type(sdk.record_revisions.list('S'), Iterator[RecordRevision])
    assert_type(sdk.record_revisions.get('S', 1), RecordRevision)
    assert_type(sdk.records.list('S'), Iterator[Record])
    assert_type(sdk.records.get('S', 1), Record)
    assert_type(sdk.records.create('S', [{'formKey': 'DEMOG'}]), Job)
    assert_type(sdk.records.create_batch('S', [{'a': 1}]), BatchSubmissionSummary)
    assert_type(sdk.sites.list('S'), Iterator[Site])
    assert_type(sdk.sites.get('S', 1), Site)
    assert_type(sdk.studies.list(), Iterator[Study])
    assert_type(sdk.studies.get('S'), Study)
    assert_type(sdk.studies.get('S').study_key, str | None)
    assert_type(sdk.studies.get(study_key='S'), Study)
    assert_type(sdk.studies.get(item_id='S'), Study)
    assert_type(sdk.studies.get(None, 'S'), Study)
    assert_type(sdk.studies.get('S', 'S'), Study)
    assert_type(sdk.studies.get(study_key=None, item_id='S'), Study)
    assert_type(sdk.studies.get(study_key='S', item_id='S'), Study)
    assert_type(sdk.studies.get('S', item_id='S'), Study)
    assert_type(sdk.subjects.list('S'), Iterator[Subject])
    assert_type(sdk.subjects.get('S', 1), Subject)
    assert_type(sdk.subjects.get('S', 1).keywords, list[SubjectKeyword])
    assert_type(sdk.users.list('S'), Iterator[User])
    assert_type(sdk.variables.list('S'), Iterator[Variable])
    assert_type(sdk.variables.get('S', 1), Variable)
    assert_type(sdk.visits.list('S'), Iterator[Visit])
    _visits_ep: EdcSyncListEndpoint[Visit] = sdk.visits
    _codings_ep: EdcSyncListEndpoint[Coding] = sdk.codings
    _queries_ep: EdcSyncListEndpoint[Query] = sdk.queries
    assert _visits_ep and _codings_ep and _queries_ep

    # Convenience methods on ImednetSDK
    assert_type(sdk.get_codings('S'), list[Coding])
    assert_type(sdk.get_forms('S'), list[Form])
    assert_type(sdk.get_intervals('S'), list[Interval])
    assert_type(sdk.get_queries('S'), list[Query])
    assert_type(sdk.get_record_revisions('S'), list[RecordRevision])
    assert_type(sdk.get_records('S'), list[Record])
    assert_type(sdk.get_sites('S'), list[Site])
    assert_type(sdk.get_studies(), list[Study])
    assert_type(sdk.get_subjects('S'), list[Subject])
    assert_type(sdk.get_users('S'), list[User])
    assert_type(sdk.get_variables('S'), list[Variable])
    assert_type(sdk.get_visits('S'), list[Visit])
    assert_type(sdk.get_job('S', 'B'), JobStatus)
    assert_type(sdk.create_record('S', [{'formKey': 'DEMOG'}]), Job)
    assert_type(sdk.create_records_batch('S', [{'a': 1}]), BatchSubmissionSummary)
    assert_type(sdk.poll_job('S', 'B', interval=1.0, timeout=30.0), JobStatus)
    assert_type(sdk.poll_jobs('S', ['B1', 'B2']), JobPollSummary)

    # JobStatus and Enum literal typing
    job_status = sdk.get_job('S', 'B')
    assert_type(job_status.state, JobState | str | None)
    assert_type(job_status.date_created, str | None)
    assert_type(job_status.date_started, str | None)
    assert_type(job_status.date_finished, str | None)
    assert_type(job_status.progress, int | None)
    assert_type(job_status.result_url, str | None)
    assert_type(JobState.COMPLETED, Literal[JobState.COMPLETED])
    assert_type(QueryStatus.OPEN, Literal[QueryStatus.OPEN])

    # Plugin namespaces on ImednetSDK
    assert_type(sdk.workflows, WorkflowsNamespaceProtocol | None)
    assert_type(sdk.sinks, SinksNamespaceProtocol | None)


async def async_contract(sdk: AsyncImednetSDK) -> None:
    facade: AsyncImednetFacade = sdk
    assert_type(facade, AsyncImednetFacade)

    # All 13 endpoints on AsyncImednetSDK
    assert_type(sdk.codings.list('S'), AsyncIterator[Coding])
    assert_type(sdk.forms.list('S'), AsyncIterator[Form])
    assert_type(await sdk.forms.get('S', 1), Form)
    assert_type(sdk.intervals.list('S'), AsyncIterator[Interval])
    assert_type(await sdk.intervals.get('S', 1), Interval)
    assert_type(await sdk.jobs.get('S', 'B'), JobStatus)
    assert_type(await sdk.jobs.poll('S', 'B'), JobStatus)
    assert_type(await sdk.jobs.poll_many('S', ['B1', 'B2']), JobPollSummary)
    assert_type(sdk.queries.list('S'), AsyncIterator[Query])
    assert_type(sdk.record_revisions.list('S'), AsyncIterator[RecordRevision])
    assert_type(await sdk.record_revisions.get('S', 1), RecordRevision)
    assert_type(sdk.records.list('S'), AsyncIterator[Record])
    assert_type(await sdk.records.get('S', 1), Record)
    assert_type(await sdk.records.create('S', [{'formKey': 'DEMOG'}]), Job)
    assert_type(await sdk.records.create_batch('S', [{'a': 1}]), BatchSubmissionSummary)
    assert_type(sdk.sites.list('S'), AsyncIterator[Site])
    assert_type(await sdk.sites.get('S', 1), Site)
    assert_type(sdk.studies.list(), AsyncIterator[Study])
    assert_type(await sdk.studies.get('S'), Study)
    assert_type((await sdk.studies.get('S')).study_key, str | None)
    assert_type(await sdk.studies.get(study_key='S'), Study)
    assert_type(await sdk.studies.get(item_id='S'), Study)
    assert_type(await sdk.studies.get(None, 'S'), Study)
    assert_type(await sdk.studies.get('S', 'S'), Study)
    assert_type(await sdk.studies.get(study_key=None, item_id='S'), Study)
    assert_type(await sdk.studies.get(study_key='S', item_id='S'), Study)
    assert_type(await sdk.studies.get('S', item_id='S'), Study)
    assert_type(sdk.subjects.list('S'), AsyncIterator[Subject])
    assert_type(await sdk.subjects.get('S', 1), Subject)
    assert_type((await sdk.subjects.get('S', 1)).keywords, list[SubjectKeyword])
    assert_type(sdk.users.list('S'), AsyncIterator[User])
    assert_type(sdk.variables.list('S'), AsyncIterator[Variable])
    assert_type(await sdk.variables.get('S', 1), Variable)
    assert_type(sdk.visits.list('S'), AsyncIterator[Visit])
    _visits_ep: EdcAsyncListEndpoint[Visit] = sdk.visits
    _codings_ep: EdcAsyncListEndpoint[Coding] = sdk.codings
    _queries_ep: EdcAsyncListEndpoint[Query] = sdk.queries
    assert _visits_ep and _codings_ep and _queries_ep

    # Convenience methods on AsyncImednetSDK
    assert_type(await sdk.async_get_codings('S'), list[Coding])
    assert_type(await sdk.async_get_forms('S'), list[Form])
    assert_type(await sdk.async_get_intervals('S'), list[Interval])
    assert_type(await sdk.async_get_queries('S'), list[Query])
    assert_type(await sdk.async_get_record_revisions('S'), list[RecordRevision])
    assert_type(await sdk.async_get_records('S'), list[Record])
    assert_type(await sdk.async_get_sites('S'), list[Site])
    assert_type(await sdk.async_get_studies(), list[Study])
    assert_type(await sdk.async_get_subjects('S'), list[Subject])
    assert_type(await sdk.async_get_users('S'), list[User])
    assert_type(await sdk.async_get_variables('S'), list[Variable])
    assert_type(await sdk.async_get_visits('S'), list[Visit])
    assert_type(await sdk.async_get_job('S', 'B'), JobStatus)
    assert_type(await sdk.async_create_record('S', [{'formKey': 'DEMOG'}]), Job)
    assert_type(await sdk.async_create_records_batch('S', [{'a': 1}]), BatchSubmissionSummary)
    assert_type(await sdk.async_poll_job('S', 'B', interval=1.0, timeout=30.0), JobStatus)
    assert_type(await sdk.async_poll_jobs('S', ['B1', 'B2']), JobPollSummary)

    # Plugin namespaces on AsyncImednetSDK
    assert_type(sdk.workflows, AsyncWorkflowsNamespaceProtocol | None)
    assert_type(sdk.sinks, SinksNamespaceProtocol | None)


def sync_endpoint_contract(ep: StudiesEndpoint) -> None:
    assert_type(ep.get('S'), Study)
    assert_type(ep.get('S').study_key, str | None)
    assert_type(ep.get(study_key='S'), Study)
    assert_type(ep.get(item_id='S'), Study)
    assert_type(ep.get(None, 'S'), Study)
    assert_type(ep.get(study_key=None, item_id='S'), Study)


async def async_endpoint_contract(ep: AsyncStudiesEndpoint) -> None:
    assert_type(await ep.get('S'), Study)
    assert_type((await ep.get('S')).study_key, str | None)
    assert_type(await ep.get(study_key='S'), Study)
    assert_type(await ep.get(item_id='S'), Study)
    assert_type(await ep.get(None, 'S'), Study)
    assert_type(await ep.get(study_key=None, item_id='S'), Study)


class _SyncStub(StudiesOperationDef, SyncEndpointContext):
    pass


class _AsyncStub(StudiesOperationDef, AsyncEndpointContext):
    pass


def sync_stub_contract(ep: _SyncStub) -> None:
    assert_type(ep.get('S'), Study)
    assert_type(ep.get('S').study_key, str | None)
    assert_type(ep.get(item_id='S'), Study)
    assert_type(ep.get(None, 'S'), Study)


async def async_stub_contract(ep: _AsyncStub) -> None:
    assert_type(await ep.get('S'), Study)
    assert_type((await ep.get('S')).study_key, str | None)
    assert_type(await ep.get(item_id='S'), Study)
    assert_type(await ep.get(None, 'S'), Study)
