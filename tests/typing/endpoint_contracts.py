from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, Literal, assert_type

import pandas as pd

from imednet import (
    ApiError,
    AsyncImednetSDK,
    AsyncSinksNamespaceProtocol,
    AsyncWorkflowsNamespaceProtocol,
    ImednetError,
    ImednetSDK,
    NotFoundError,
    SimulationTransport,
    SinksNamespaceProtocol,
    SyncWorkflowsNamespaceProtocol,
)
from imednet.auth.strategy import AuthStrategy
from imednet.core.endpoint import EdcAsyncListEndpoint, EdcSyncListEndpoint
from imednet.core.endpoint.dispatch import AsyncEndpointContext, SyncEndpointContext
from imednet.endpoints.studies import AsyncStudiesEndpoint, StudiesEndpoint, StudiesOperationDef
from imednet.models.base import Pagination
from imednet.models.codings import Coding
from imednet.models.forms import Form
from imednet.models.intervals import FormSummary, Interval
from imednet.models.jobs import Job, JobState, JobStatus
from imednet.models.queries import Query, QueryComment, QueryStatus
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import (
    BaseRecordRequest,
    CreateNewRecordRequest,
    Keyword,
    Record,
    RecordData,
    RecordJobResponse,
    RegisterSubjectRequest,
    UpdateRecordRequest,
    UpdateScheduledRecordRequest,
)
from imednet.models.reporting import (
    AdverseEvent,
    AnalysisAdverseEvent,
    AnalysisLabResult,
    Demographics,
    DeviceDeficiency,
    ProtocolDeviation,
    SubjectLevelAnalysis,
    VitalSign,
    VitalSigns,
)
from imednet.models.sites import Site
from imednet.models.studies import Study
from imednet.models.study_config import MappingRule, StudyConfiguration, WidgetConfig
from imednet.models.study_structure import FormStructure, IntervalStructure, StudyStructure
from imednet.models.subjects import Subject, SubjectKeyword
from imednet.models.triage import TriageItem
from imednet.models.users import Role, User
from imednet.models.variables import Variable
from imednet.models.visits import Visit
from imednet.plugins import (
    AsyncRecordUpdateWorkflowProtocol,
    AsyncRegisterSubjectsWorkflowProtocol,
    AsyncStudySchemaInspectorProtocol,
    MongoDbSinkConfigProtocol,
    Neo4jSinkConfigProtocol,
    PluginProtocol,
    SinksPluginProtocol,
    SnowflakeSinkConfigProtocol,
    StudySnapshotProtocol,
    SubjectComprehensiveDataProtocol,
    SyncRecordUpdateWorkflowProtocol,
    SyncRegisterSubjectsWorkflowProtocol,
    SyncStudySchemaInspectorProtocol,
    UATRunResultProtocol,
)
from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
from imednet.spi.sinks import ExportSink
from imednet.utils.batch_coordinator import BatchSubmissionSummary
from imednet.utils.job_poller import JobPollSummary
from imednet_sinks.plugin import SinksNamespace, create_sinks
from imednet_workflows import Workflows


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
    assert_type(sdk.jobs.poll('S', 'B', resolve_results=True), JobStatus)
    assert_type(sdk.jobs.poll_many('S', ['B1', 'B2']), JobPollSummary)
    assert_type(sdk.jobs.poll_many('S', ['B1', 'B2'], resolve_results=True), JobPollSummary)
    assert_type(sdk.queries.list('S'), Iterator[Query])
    assert_type(sdk.record_revisions.list('S'), Iterator[RecordRevision])
    assert_type(sdk.record_revisions.get('S', 1), RecordRevision)
    assert_type(sdk.records.list('S'), Iterator[Record])
    assert_type(sdk.records.get('S', 1), Record)
    assert_type(sdk.records.create('S', [{'formKey': 'DEMOG'}]), Job)
    assert_type(
        sdk.records.create('S', (UpdateRecordRequest(record_id=1, data={'AGE': 40}),)),
        Job,
    )
    assert_type(sdk.records.create_batch('S', [{'a': 1}]), BatchSubmissionSummary)
    batch_summary = sdk.records.create_batch(
        'S',
        [RegisterSubjectRequest(form_id=10, site_id=1)],
        resolve_results=True,
    )
    assert_type(batch_summary.created_record_ids, list[int])
    assert_type(batch_summary.enrolled_subject_keys, list[str])
    assert_type(batch_summary.successful_records, list[dict[str, Any]])
    assert_type(batch_summary.record_errors, list[dict[str, Any]])
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
    assert_type(sdk.subjects.list_by_site('S', 1), list[Subject])
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
    assert sdk.jobs.fetch_result('https://example.com/res.json') is not None or True
    assert_type(sdk.create_record('S', [{'formKey': 'DEMOG'}]), Job)
    assert_type(sdk.create_records_batch('S', [{'a': 1}]), BatchSubmissionSummary)
    assert_type(sdk.poll_job('S', 'B', interval=1.0, timeout=30.0), JobStatus)
    assert_type(sdk.poll_job('S', 'B', resolve_results=True), JobStatus)
    assert_type(facade.poll_job('S', 'B', resolve_results=True), JobStatus)
    assert_type(sdk.poll_jobs('S', ['B1', 'B2']), JobPollSummary)
    assert_type(sdk.poll_jobs('S', ['B1', 'B2'], resolve_results=True), JobPollSummary)
    assert_type(facade.poll_jobs('S', ['B1', 'B2'], resolve_results=True), JobPollSummary)

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

    # Partial constructors across all 14 contract models (snake_case & camelCase, zero [call-arg])
    assert_type(Coding(study_key='S'), Coding)
    assert_type(Coding(studyKey='S'), Coding)
    assert_type(Form(form_key='F_DM'), Form)
    assert_type(Form(formKey='F_DM'), Form)
    assert_type(Interval(interval_name='Baseline'), Interval)
    assert_type(Interval(intervalName='Baseline', forms=None), Interval)
    assert_type(Job(batch_id='B1'), Job)
    assert_type(Job(batchId='B1'), Job)
    assert_type(
        JobStatus(
            batch_id='B1',
            progress=50,
            record_total_count=10,
            record_success_count=9,
            record_failure_count=1,
        ),
        JobStatus,
    )
    assert_type(
        JobStatus(
            batch_id='B1',
            total_records=10,
            successful_records=9,
            failed_records=1,
        ),
        JobStatus,
    )
    assert_type(
        JobStatus(
            batchId='B1',
            totalRecords=10,
            successfulRecords=9,
            failedRecords=1,
        ),
        JobStatus,
    )
    assert_type(job_status.total_records, int | None)
    assert_type(job_status.successful_records, int | None)
    assert_type(job_status.failed_records, int | None)
    assert_type(Query(study_key='S', description='Open query'), Query)
    assert_type(Query(studyKey='S', description='Open query'), Query)
    assert_type(Record(record_id=1), Record)
    assert_type(Record(recordId=1), Record)
    assert_type(RecordRevision(record_revision_id=1), RecordRevision)
    assert_type(RecordRevision(recordRevisionId=1), RecordRevision)
    assert_type(Site(site_id=1), Site)
    assert_type(Site(siteId=1), Site)
    assert_type(Study(study_key='S'), Study)
    assert_type(Study(studyKey='S'), Study)
    assert_type(Subject(subject_id=1), Subject)
    assert_type(Subject(subjectId=1), Subject)
    assert_type(User(user_id='u-1', login='clinician'), User)
    assert_type(User(userId='u-1', login='clinician'), User)
    assert_type(Variable(variable_id=1), Variable)
    assert_type(Variable(variableId=1), Variable)
    assert_type(Visit(visit_id=1), Visit)
    assert_type(Visit(visitId=1), Visit)

    # Top-level exception exports and offline transport initialization
    err_base: ImednetError = NotFoundError('missing')
    err_api: ApiError = NotFoundError('missing')
    assert err_base and err_api
    assert_type(ImednetSDK(transport=SimulationTransport()), ImednetSDK)
    assert_type(AsyncImednetSDK(transport=SimulationTransport()), AsyncImednetSDK)

    # Narrowed auth, study_context Self preservation, and sync plugin protocols
    assert_type(sdk.auth, AuthStrategy | None)
    assert_type(facade.auth, AuthStrategy | None)
    _sync_cm: AbstractContextManager[ImednetSDK] = sdk.study_context('S')
    assert _sync_cm
    with sdk.study_context('S') as scoped_sdk:
        assert_type(scoped_sdk, ImednetSDK)
    _facade_sync_cm: AbstractContextManager[ImednetFacade] = facade.study_context('S')
    assert _facade_sync_cm
    with facade.study_context('S') as scoped_facade:
        assert_type(scoped_facade, ImednetFacade)
        assert_type(scoped_facade.get_codings(), list[Coding])
        assert_type(scoped_facade.get_forms(), list[Form])
        assert_type(scoped_facade.get_intervals(), list[Interval])
        assert_type(scoped_facade.get_queries(), list[Query])
        assert_type(scoped_facade.get_record_revisions(), list[RecordRevision])
        assert_type(scoped_facade.get_records(), list[Record])
        assert_type(scoped_facade.get_sites(), list[Site])
        assert_type(scoped_facade.get_subjects(), list[Subject])
        assert_type(scoped_facade.get_users(), list[User])
        assert_type(scoped_facade.get_variables(), list[Variable])
        assert_type(scoped_facade.get_visits(), list[Visit])

    assert_type(sdk.workflows, SyncWorkflowsNamespaceProtocol)
    assert_type(sdk.workflows.data_extraction.extract_records_by_criteria('S'), list[Record])
    assert_type(sdk.workflows.duckdb_centralizer.ingest_revisions('S'), int)
    sdk.workflows.duckdb_centralizer.build_silver_view('S', include_deleted=True)
    sdk.workflows.duckdb_centralizer.close()
    with sdk.workflows.duckdb_centralizer as centralizer_ctx:
        assert_type(centralizer_ctx.ingest_revisions('S'), int)
    assert_type(sdk.workflows.query_management.get_open_queries('S'), list[Query])
    assert_type(sdk.workflows.query_management.sync_queries_to_triage('S'), list[TriageItem])
    assert_type(sdk.workflows.record_mapper.dataframe('S'), pd.DataFrame)
    assert_type(sdk.workflows.record_update, SyncRecordUpdateWorkflowProtocol)
    assert_type(sdk.workflows.record_update.create_or_update_records('S', []), Job)
    assert_type(
        sdk.workflows.record_update.create_or_update_records_batch('S', []),
        BatchSubmissionSummary,
    )
    assert_type(sdk.workflows.record_update.register_subject('S', 'F_DM', 'SITE', {}), Job)
    assert_type(
        sdk.workflows.record_update.update_scheduled_record('S', 'F_VS', 'SUBJ', 'V1', {}),
        Job,
    )
    assert_type(sdk.workflows.record_update.create_new_record('S', 'F_VS', 'SUBJ', {}), Job)
    assert_type(
        sdk.workflows.record_update.update_record(
            'S',
            {},
            record_id=10,
            record_status='Record Complete',
            reason_for_change='Audit',
            resolve_results=True,
        ),
        Job,
    )
    assert_type(sdk.workflows.register_subjects, SyncRegisterSubjectsWorkflowProtocol)
    assert_type(sdk.workflows.register_subjects.register_subjects('S', []), Job)
    assert_type(
        sdk.workflows.register_subjects.register_subjects_batch('S', []),
        BatchSubmissionSummary,
    )
    assert_type(
        sdk.workflows.subject_data.get_all_subject_data('S', 'SUBJ'),
        SubjectComprehensiveDataProtocol,
    )
    assert_type(sdk.workflows.uat_inspector, SyncStudySchemaInspectorProtocol)
    assert_type(sdk.workflows.uat_inspector.inspect('S'), StudySnapshotProtocol)
    assert_type(sdk.workflows.uat.run('S'), UATRunResultProtocol)

    assert_type(sdk.sinks, SinksNamespaceProtocol | None)
    if sdk.sinks is not None:
        mongo_cfg = sdk.sinks.MongoDbSinkConfig(
            study_key='S',
            uri='mongodb://localhost:27017',
            database='db',
            collection='records',
        )
        assert_type(mongo_cfg, MongoDbSinkConfigProtocol)
        assert_type(sdk.sinks.MongoDbExportSink(mongo_cfg), ExportSink)
        assert_type(sdk.sinks.export_to_mongodb('S', config=mongo_cfg), int)

        neo_cfg = sdk.sinks.Neo4jSinkConfig(
            study_key='S',
            uri='bolt://localhost:7687',
            auth=('neo4j', 'pass'),
        )
        assert_type(neo_cfg, Neo4jSinkConfigProtocol)
        assert_type(sdk.sinks.Neo4jExportSink(neo_cfg), ExportSink)
        assert_type(sdk.sinks.export_to_neo4j('S', config=neo_cfg), int)

        sf_cfg = sdk.sinks.SnowflakeSinkConfig(
            study_key='S',
            account='acct',
            user='u',
            password='p',
            database='DB',
            schema='PUBLIC',
            warehouse='WH',
            stage='STG',
            table='TBL',
        )
        assert_type(sf_cfg, SnowflakeSinkConfigProtocol)
        assert_type(sdk.sinks.SnowflakeExportSink(sf_cfg), ExportSink)
        assert_type(sdk.sinks.export_to_snowflake('S', config=sf_cfg), int)


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
    assert_type(await sdk.jobs.poll('S', 'B', resolve_results=True), JobStatus)
    assert_type(await sdk.jobs.poll_many('S', ['B1', 'B2']), JobPollSummary)
    assert_type(await sdk.jobs.poll_many('S', ['B1', 'B2'], resolve_results=True), JobPollSummary)
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
    assert_type(await sdk.subjects.list_by_site('S', 1), list[Subject])
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

    # Canonical unprefixed convenience methods on AsyncImednetSDK and AsyncImednetFacade
    assert_type(await sdk.get_codings('S'), list[Coding])
    assert_type(await sdk.get_forms('S'), list[Form])
    assert_type(await sdk.get_intervals('S'), list[Interval])
    assert_type(await sdk.get_queries('S'), list[Query])
    assert_type(await sdk.get_record_revisions('S'), list[RecordRevision])
    assert_type(await sdk.get_records('S'), list[Record])
    assert_type(await sdk.get_sites('S'), list[Site])
    assert_type(await sdk.get_studies(), list[Study])
    assert_type(await sdk.get_subjects('S'), list[Subject])
    assert_type(await sdk.get_users('S'), list[User])
    assert_type(await sdk.get_variables('S'), list[Variable])
    assert_type(await sdk.get_visits('S'), list[Visit])
    assert_type(await sdk.get_job('S', 'B'), JobStatus)
    assert_type(await sdk.create_record('S', [{'formKey': 'DEMOG'}]), Job)
    assert_type(await sdk.create_records_batch('S', [{'a': 1}]), BatchSubmissionSummary)
    assert_type(await sdk.poll_job('S', 'B', interval=1.0, timeout=30.0), JobStatus)
    assert_type(await sdk.poll_jobs('S', ['B1', 'B2'], resolve_results=True), JobPollSummary)

    # Deprecated async_* compatibility aliases on AsyncImednetSDK
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
    assert (await sdk.jobs.fetch_result('https://example.com/res.json')) is not None or True
    assert_type(await sdk.async_create_record('S', [{'formKey': 'DEMOG'}]), Job)
    assert_type(await sdk.async_create_records_batch('S', [{'a': 1}]), BatchSubmissionSummary)
    assert_type(await sdk.async_poll_job('S', 'B', interval=1.0, timeout=30.0), JobStatus)
    assert_type(await sdk.async_poll_job('S', 'B', resolve_results=True), JobStatus)
    assert_type(await facade.poll_job('S', 'B', resolve_results=True), JobStatus)
    assert_type(await sdk.async_poll_jobs('S', ['B1', 'B2']), JobPollSummary)
    assert_type(await sdk.async_poll_jobs('S', ['B1', 'B2'], resolve_results=True), JobPollSummary)
    assert_type(await facade.poll_jobs('S', ['B1', 'B2'], resolve_results=True), JobPollSummary)

    # Narrowed auth, study_context Self preservation, and async plugin protocols
    assert_type(sdk.auth, AuthStrategy | None)
    assert_type(facade.auth, AuthStrategy | None)
    _async_cm: AbstractContextManager[AsyncImednetSDK] = sdk.study_context('S')
    assert _async_cm
    with sdk.study_context('S') as scoped_async_sdk:
        assert_type(scoped_async_sdk, AsyncImednetSDK)
    _facade_async_cm: AbstractContextManager[AsyncImednetFacade] = facade.study_context('S')
    assert _facade_async_cm
    with facade.study_context('S') as scoped_async_facade:
        assert_type(scoped_async_facade, AsyncImednetFacade)
        assert_type(await scoped_async_facade.get_codings(), list[Coding])
        assert_type(await scoped_async_facade.get_forms(), list[Form])
        assert_type(await scoped_async_facade.get_intervals(), list[Interval])
        assert_type(await scoped_async_facade.get_queries(), list[Query])
        assert_type(await scoped_async_facade.get_record_revisions(), list[RecordRevision])
        assert_type(await scoped_async_facade.get_records(), list[Record])
        assert_type(await scoped_async_facade.get_sites(), list[Site])
        assert_type(await scoped_async_facade.get_subjects(), list[Subject])
        assert_type(await scoped_async_facade.get_users(), list[User])
        assert_type(await scoped_async_facade.get_variables(), list[Variable])
        assert_type(await scoped_async_facade.get_visits(), list[Visit])

    assert_type(sdk.workflows, AsyncWorkflowsNamespaceProtocol)
    assert_type(await sdk.workflows.data_extraction.extract_records_by_criteria('S'), list[Record])
    assert_type(await sdk.workflows.duckdb_centralizer.ingest_revisions('S'), int)
    assert_type(await sdk.workflows.query_management.get_open_queries('S'), list[Query])
    assert_type(await sdk.workflows.record_mapper.dataframe('S'), pd.DataFrame)
    assert_type(sdk.workflows.record_update, AsyncRecordUpdateWorkflowProtocol)
    assert_type(
        await sdk.workflows.record_update.create_or_update_records('S', []),
        Job,
    )
    assert_type(
        await sdk.workflows.record_update.create_or_update_records_batch('S', []),
        BatchSubmissionSummary,
    )
    assert_type(
        await sdk.workflows.record_update.async_create_or_update_records('S', []),
        Job,
    )
    assert_type(
        await sdk.workflows.record_update.async_create_or_update_records_batch('S', []),
        BatchSubmissionSummary,
    )
    assert_type(
        await sdk.workflows.record_update.async_register_subject('S', 'F_DM', 'SITE', {}),
        Job,
    )
    assert_type(
        await sdk.workflows.record_update.async_update_scheduled_record(
            'S', 'F_VS', 'SUBJ', 'V1', {}
        ),
        Job,
    )
    assert_type(
        await sdk.workflows.record_update.async_create_new_record('S', 'F_VS', 'SUBJ', {}),
        Job,
    )
    assert_type(
        await sdk.workflows.record_update.update_record(
            'S',
            {},
            record_id=10,
            record_status='Record Complete',
            reason_for_change='Audit',
            resolve_results=True,
        ),
        Job,
    )
    assert_type(
        await sdk.workflows.record_update.async_update_record('S', {}, record_id=10),
        Job,
    )
    assert_type(sdk.workflows.register_subjects, AsyncRegisterSubjectsWorkflowProtocol)
    assert_type(await sdk.workflows.register_subjects.register_subjects('S', []), Job)
    assert_type(await sdk.workflows.register_subjects.async_register_subjects('S', []), Job)
    assert_type(
        await sdk.workflows.register_subjects.async_register_subjects_batch('S', []),
        BatchSubmissionSummary,
    )
    assert_type(
        await sdk.workflows.subject_data.get_all_subject_data('S', 'SUBJ'),
        SubjectComprehensiveDataProtocol,
    )
    assert_type(await sdk.workflows.uat.run('S'), UATRunResultProtocol)
    assert_type(sdk.workflows.uat_inspector, AsyncStudySchemaInspectorProtocol)
    assert_type(await sdk.workflows.uat_inspector.inspect('S'), StudySnapshotProtocol)
    assert_type(await sdk.workflows.uat_inspector.async_inspect('S'), StudySnapshotProtocol)

    assert_type(sdk.sinks, AsyncSinksNamespaceProtocol | None)
    if sdk.sinks is not None:
        mongo_cfg_async = sdk.sinks.MongoDbSinkConfig(
            study_key='S',
            uri='mongodb://localhost:27017',
            database='db',
            collection='records',
        )
        assert_type(mongo_cfg_async, MongoDbSinkConfigProtocol)
        assert_type(await sdk.sinks.export_to_mongodb('S', config=mongo_cfg_async), int)
        assert_type(await sdk.sinks.export_to_neo4j('S'), int)
        sf_cfg_async = sdk.sinks.SnowflakeSinkConfig(study_key='S')
        assert_type(await sdk.sinks.export_to_snowflake('S', config=sf_cfg_async), int)


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


class _CustomSyncSDK(ImednetSDK):
    pass


class _CustomAsyncSDK(AsyncImednetSDK):
    pass


def plugin_implementation_contracts(
    sync_sdk: _CustomSyncSDK,
    async_sdk: _CustomAsyncSDK,
) -> None:
    with sync_sdk.study_context('S') as scoped_sync:
        assert_type(scoped_sync, _CustomSyncSDK)
    with async_sdk.study_context('S') as scoped_async:
        assert_type(scoped_async, _CustomAsyncSDK)

    workflows_ns: SyncWorkflowsNamespaceProtocol = Workflows(sync_sdk)
    sinks_ns: SinksNamespaceProtocol = SinksNamespace(sync_sdk)
    sinks_factory: SinksPluginProtocol = create_sinks
    workflows_factory: PluginProtocol = Workflows
    mongo_cfg = sinks_ns.MongoDbSinkConfig(
        'S',
        uri='mongodb://localhost:27017',
        database='db',
        collection='col',
        max_buffered_records=1000,
        as_sdtm=True,
        sdtm_domain='AE',
        standards_profile='drug',
        min_standards_score=90.0,
        naming_convention='sdtm',
    )
    neo4j_cfg = sinks_ns.Neo4jSinkConfig(
        'S',
        uri='bolt://localhost:7687',
        auth=('neo4j', 'pw'),
        max_buffered_records=1000,
        as_sdtm=True,
        sdtm_domain='AE',
        standards_profile='drug',
        min_standards_score=90.0,
        naming_convention='sdtm',
    )
    sf_cfg = sinks_ns.SnowflakeSinkConfig(
        'S',
        max_buffered_records=1000,
        as_sdtm=True,
        sdtm_domain='AE',
        standards_profile='drug',
        min_standards_score=90.0,
        naming_convention='sdtm',
    )
    assert_type(mongo_cfg.max_buffered_records, int | None)
    assert_type(mongo_cfg.as_sdtm, bool)
    assert_type(mongo_cfg.sdtm_domain, str | None)
    assert_type(neo4j_cfg.max_buffered_records, int | None)
    assert_type(neo4j_cfg.min_standards_score, float | None)
    assert_type(sf_cfg.max_buffered_records, int | None)
    assert_type(sf_cfg.naming_convention, str | None)
    assert_type(workflows_ns.record_mapper.sdtm_dataframe('S', 'AE', mask_phi=True), pd.DataFrame)
    assert workflows_ns and sinks_ns and sinks_factory and workflows_factory


def aliased_model_constructor_contracts() -> None:
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)

    # Pagination (base.py)
    assert_type(Pagination(current_page=1, total_pages=3, total_elements=60), Pagination)
    assert_type(Pagination(currentPage=1, totalPages=3, totalElements=60), Pagination)

    # Nested aliased models across intervals, queries, records, subjects, and users
    assert_type(FormSummary(form_id=1, form_key='DM', form_name='Demographics'), FormSummary)
    assert_type(FormSummary(formId=1, formKey='DM', formName='Demographics'), FormSummary)
    assert_type(QueryComment(annotation_status='Monitor Open', sequence=1), QueryComment)
    assert_type(QueryComment(annotationStatus='Monitor Open', sequence=1), QueryComment)
    assert_type(Keyword(keyword_name='Safety', keyword_key='SAFE', keyword_id=1), Keyword)
    assert_type(Keyword(keywordName='Safety', keywordKey='SAFE', keywordId=1), Keyword)
    assert_type(RecordJobResponse(job_id='J1', batch_id='B1', state='OPEN'), RecordJobResponse)
    assert_type(RecordJobResponse(jobId='J1', batchId='B1', state='OPEN'), RecordJobResponse)
    assert_type(
        SubjectKeyword(keyword_name='Enrolled', keyword_key='ENR', keyword_id=2),
        SubjectKeyword,
    )
    assert_type(
        SubjectKeyword(keywordName='Enrolled', keywordKey='ENR', keywordId=2),
        SubjectKeyword,
    )
    assert_type(Role(role_id='R1', community_id=10), Role)
    assert_type(Role(roleId='R1', communityId=10), Role)

    # Record request hierarchy with inherited BaseRecordRequest fields (records.py)
    payload = RecordData({'AGE': 42})
    assert_type(BaseRecordRequest(form_key='DM', data=payload), BaseRecordRequest)
    assert_type(BaseRecordRequest(formKey='DM', data=payload), BaseRecordRequest)
    assert_type(
        RegisterSubjectRequest(
            form_key='DM',
            site_name='SITE-01',
            subject_key='SUBJ-01',
            subject_oid='OID-01',
            data=payload,
        ),
        RegisterSubjectRequest,
    )
    assert_type(
        RegisterSubjectRequest(
            formKey='DM',
            siteName='SITE-01',
            subjectKey='SUBJ-01',
            subjectOid='OID-01',
            data=payload,
        ),
        RegisterSubjectRequest,
    )
    assert_type(
        UpdateScheduledRecordRequest(
            form_key='VS',
            subject_key='SUBJ-01',
            interval_name='Visit 1',
            data=payload,
        ),
        UpdateScheduledRecordRequest,
    )
    assert_type(
        UpdateScheduledRecordRequest(
            formKey='VS',
            subjectKey='SUBJ-01',
            intervalName='Visit 1',
            data=payload,
        ),
        UpdateScheduledRecordRequest,
    )
    assert_type(
        CreateNewRecordRequest(form_key='AE', subject_key='SUBJ-01', data=payload),
        CreateNewRecordRequest,
    )
    assert_type(
        CreateNewRecordRequest(formKey='AE', subjectKey='SUBJ-01', data=payload),
        CreateNewRecordRequest,
    )

    # Study configuration models (study_config.py)
    rule_snake = MappingRule(
        domain='AE',
        target_field='aeTerm',
        source_form_key='AE_FORM',
        source_variable_name='AETERM',
        is_baseline=True,
    )
    rule_camel = MappingRule(
        domain='AE',
        targetField='aeTerm',
        sourceFormKey='AE_FORM',
        sourceVariableName='AETERM',
        isBaseline=True,
    )
    assert_type(rule_snake, MappingRule)
    assert_type(rule_camel, MappingRule)

    widget_snake = WidgetConfig(
        widget_id='w-1',
        type='bar',
        title='AE Count',
        domain='AE',
        x_axis='aeSeverity',
        layout_cols=6,
    )
    widget_camel = WidgetConfig(
        widgetId='w-1',
        type='bar',
        title='AE Count',
        domain='AE',
        xAxis='aeSeverity',
        layoutCols=6,
    )
    assert_type(widget_snake, WidgetConfig)
    assert_type(widget_camel, WidgetConfig)

    assert_type(
        StudyConfiguration(
            study_key='STUDY-1',
            reporting_profile='drug',
            mappings=[rule_snake],
            widgets=[widget_snake],
            phi_fields=['BRTHDTC'],
        ),
        StudyConfiguration,
    )
    assert_type(
        StudyConfiguration(
            studyKey='STUDY-1',
            reportingProfile='drug',
            mappings=[rule_camel],
            widgets=[widget_camel],
            phiFields=['BRTHDTC'],
        ),
        StudyConfiguration,
    )

    # Study structure models (study_structure.py)
    form_struct_snake = FormStructure(
        form_id=1,
        form_key='DM',
        form_name='Demographics',
        form_type='CRF',
        revision=1,
        epro_form=False,
        allow_copy=False,
        date_created=now,
        date_modified=now,
    )
    form_struct_camel = FormStructure(
        formId=1,
        formKey='DM',
        formName='Demographics',
        formType='CRF',
        revision=1,
        eproForm=False,
        allowCopy=False,
        dateCreated=now,
        dateModified=now,
    )
    assert_type(form_struct_snake, FormStructure)
    assert_type(form_struct_camel, FormStructure)

    interval_struct_snake = IntervalStructure(
        interval_id=10,
        interval_name='Baseline',
        interval_sequence=1,
        interval_description='Baseline Visit',
        interval_group_name='Core',
        date_created=now,
        date_modified=now,
        forms=[form_struct_snake],
    )
    interval_struct_camel = IntervalStructure(
        intervalId=10,
        intervalName='Baseline',
        intervalSequence=1,
        intervalDescription='Baseline Visit',
        intervalGroupName='Core',
        dateCreated=now,
        dateModified=now,
        forms=[form_struct_camel],
    )
    assert_type(interval_struct_snake, IntervalStructure)
    assert_type(interval_struct_camel, IntervalStructure)

    assert_type(
        StudyStructure(
            study_key='STUDY-1',
            intervals=[interval_struct_snake],
            forms=[form_struct_snake],
        ),
        StudyStructure,
    )
    assert_type(
        StudyStructure(
            studyKey='STUDY-1',
            intervals=[interval_struct_camel],
            forms=[form_struct_camel],
        ),
        StudyStructure,
    )

    # Reporting & CDISC analysis models (reporting.py)
    assert_type(
        AdverseEvent(
            subject_key='SUBJ-01',
            ae_term='Headache',
            ae_severity='MILD',
            ae_serious=False,
        ),
        AdverseEvent,
    )
    assert_type(
        AdverseEvent(
            subjectKey='SUBJ-01',
            aeTerm='Headache',
            aeSeverity='MILD',
            aeSerious=False,
        ),
        AdverseEvent,
    )
    assert_type(
        ProtocolDeviation(
            subject_key='SUBJ-01',
            dv_term='Late visit',
            dv_category='VISIT_WINDOW',
            dv_severity='MINOR',
            dv_date=now,
        ),
        ProtocolDeviation,
    )
    assert_type(
        ProtocolDeviation(
            subjectKey='SUBJ-01',
            dvTerm='Late visit',
            dvCategory='VISIT_WINDOW',
            dvSeverity='MINOR',
            dvDate=now,
        ),
        ProtocolDeviation,
    )
    assert_type(
        DeviceDeficiency(
            subject_key='SUBJ-01',
            dd_term='Sensor calibration drift',
            dd_category='MALFUNCTION',
            dd_date=now,
        ),
        DeviceDeficiency,
    )
    assert_type(
        DeviceDeficiency(
            subjectKey='SUBJ-01',
            ddTerm='Sensor calibration drift',
            ddCategory='MALFUNCTION',
            ddDate=now,
        ),
        DeviceDeficiency,
    )
    assert_type(
        SubjectLevelAnalysis(subject_key='SUBJ-01', arm='Treatment A'),
        SubjectLevelAnalysis,
    )
    assert_type(
        SubjectLevelAnalysis(subjectKey='SUBJ-01', arm='Treatment A'),
        SubjectLevelAnalysis,
    )
    assert_type(
        AnalysisAdverseEvent(subject_key='SUBJ-01', trtemfl='Y'),
        AnalysisAdverseEvent,
    )
    assert_type(
        AnalysisAdverseEvent(subjectKey='SUBJ-01', trtemfl='Y'),
        AnalysisAdverseEvent,
    )
    assert_type(
        AnalysisLabResult(subject_key='SUBJ-01', paramcd='ALT'),
        AnalysisLabResult,
    )
    assert_type(
        AnalysisLabResult(subjectKey='SUBJ-01', paramcd='ALT'),
        AnalysisLabResult,
    )
    assert_type(
        Demographics(subject_key='SUBJ-01', sex='F', birth_date=now, age=34),
        Demographics,
    )
    assert_type(
        Demographics(subjectKey='SUBJ-01', sex='F', birthDate=now, age=34),
        Demographics,
    )
    assert_type(
        VitalSigns(
            subject_key='SUBJ-01',
            vs_test='Systolic Blood Pressure',
            vs_test_cd='SYSBP',
            vs_result=120.0,
            vs_unit='mmHg',
            vs_date=now,
        ),
        VitalSigns,
    )
    assert_type(
        VitalSign(
            subjectKey='SUBJ-01',
            vsTest='Systolic Blood Pressure',
            vsTestCd='SYSBP',
            vsResult=120.0,
            vsUnit='mmHg',
            vsDate=now,
        ),
        VitalSigns,
    )


def mixed_alias_and_narrowed_type_contracts() -> None:
    now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)

    # User.name property resolves to str, Role accepts 7-int timestamp arrays,
    # and BaseRecordRequest hierarchy accepts plain dict[str, Any] on data
    user_inst = User(user_id='u-1', firstName='Ada', last_name='Lovelace')
    assert_type(user_inst, User)
    assert_type(user_inst.name, str)
    assert_type(
        Role(
            role_id='R1',
            communityId=10,
            dateCreated=[2026, 5, 13, 14, 6, 50, 612000000],
            date_modified='2026-05-13T14:06:50Z',
        ),
        Role,
    )
    assert_type(BaseRecordRequest(form_key='DM', data={'SUBJID': '001'}), BaseRecordRequest)
    assert_type(
        RegisterSubjectRequest(form_key='DM', siteName='SITE-01', data={'SUBJID': '001'}),
        RegisterSubjectRequest,
    )
    assert_type(
        UpdateScheduledRecordRequest(
            form_key='VS',
            subjectKey='SUBJ-01',
            interval_name='Visit 1',
            data={'SYSBP': 120},
        ),
        UpdateScheduledRecordRequest,
    )
    assert_type(
        CreateNewRecordRequest(form_key='AE', subjectKey='SUBJ-01', data={'AETERM': 'Headache'}),
        CreateNewRecordRequest,
    )
    assert_type(
        UpdateRecordRequest(
            form_id=10,
            subjectId=20,
            record_id=30,
            recordOid='REC-OID-1',
            visit_id=40,
            recordStatus='Record Complete',
            reason_for_change='Correction',
            data={'AGE': 55},
        ),
        UpdateRecordRequest,
    )

    # Mixed snake_case + camelCase constructors across all 33 multi-alias models
    assert_type(Pagination(current_page=1, totalPages=3, total_elements=60), Pagination)
    assert_type(Coding(study_key='S', siteName='SITE-01'), Coding)
    assert_type(Form(subject_record_report=True, unscheduledVisit=False), Form)
    assert_type(FormSummary(form_id=1, formKey='DM', form_name='Demographics'), FormSummary)
    assert_type(Interval(study_key='S', intervalId=10), Interval)
    assert_type(Job(job_id='J1', batchId='B1'), Job)
    assert_type(
        JobStatus(job_id='J1', batchId='B1', total_records=5, successfulRecords=5),
        JobStatus,
    )
    assert_type(
        Query(study_key='S', queryComments=[QueryComment(annotationStatus='Open')]),
        Query,
    )
    assert_type(RecordRevision(record_revision=1, dataRevision=2), RecordRevision)
    assert_type(Keyword(keyword_name='Safety', keywordKey='SAFE', keyword_id=1), Keyword)
    assert_type(Record(record_data={'AGE': 42}, recordStatus='Record Complete'), Record)
    assert_type(RecordJobResponse(job_id='J1', batchId='B1', state='OPEN'), RecordJobResponse)
    assert_type(
        AdverseEvent(
            subject_key='SUBJ-01',
            aeTerm='Headache',
            ae_severity='MILD',
            aeSerious=False,
        ),
        AdverseEvent,
    )
    assert_type(
        ProtocolDeviation(
            subject_key='SUBJ-01',
            dvTerm='Late visit',
            dv_category='VISIT_WINDOW',
            dvSeverity='MINOR',
        ),
        ProtocolDeviation,
    )
    assert_type(
        DeviceDeficiency(
            subject_key='SUBJ-01',
            ddTerm='Sensor drift',
            dd_category='MALFUNCTION',
        ),
        DeviceDeficiency,
    )
    assert_type(Demographics(subject_key='SUBJ-01', sex='F', birthDate=now), Demographics)
    assert_type(
        VitalSigns(
            subject_key='SUBJ-01',
            vsTest='Systolic BP',
            vs_test_cd='SYSBP',
            vsResult=120.0,
        ),
        VitalSigns,
    )
    assert_type(Site(study_key='S', siteEnrollmentStatus='Enrollment Open'), Site)
    assert_type(Study(sponsor_key='SP', studyKey='S'), Study)
    assert_type(
        MappingRule(
            domain='AE',
            target_field='aeTerm',
            sourceFormKey='AE_FORM',
            source_variable_name='AETERM',
        ),
        MappingRule,
    )
    assert_type(
        WidgetConfig(
            widget_id='w-1',
            type='bar',
            title='AE Count',
            domain='AE',
            xAxis='aeSeverity',
        ),
        WidgetConfig,
    )
    assert_type(
        StudyConfiguration(study_key='STUDY-1', reportingProfile='drug'), StudyConfiguration
    )
    assert_type(
        FormStructure(
            form_id=1,
            formKey='DM',
            form_name='Demographics',
            formType='CRF',
            revision=1,
            date_created=now,
            dateModified=now,
        ),
        FormStructure,
    )
    assert_type(
        IntervalStructure(
            interval_id=10,
            intervalName='Baseline',
            interval_sequence=1,
            intervalDescription='Visit 1',
            interval_group_name='Core',
            dateCreated=now,
            date_modified=now,
        ),
        IntervalStructure,
    )
    assert_type(
        SubjectKeyword(keyword_name='Enrolled', keywordKey='ENR', keyword_id=2), SubjectKeyword
    )
    assert_type(Subject(study_key='S', subjectStatus='Enrolled'), Subject)
    assert_type(Variable(study_key='S', variableOid='VAR_AGE'), Variable)
    assert_type(Visit(study_key='S', visitId=1), Visit)
