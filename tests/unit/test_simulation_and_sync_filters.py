"""Tests for simulation filter evaluation, incremental SyncWorker sync, and workflow OR batching."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx

from imednet.models.queries import Query
from imednet.models.records import Record
from imednet.models.subjects import Subject
from imednet.sdk import ImednetSDK
from imednet.simulation.state import SimulationState
from imednet.simulation.transport import SimulationTransport
from imednet_workflows.cached_loader import CachedRecordsLoader
from imednet_workflows.data_extraction import DataExtractionWorkflow
from imednet_workflows.query_management import QueryManagementWorkflow
from imednet_workflows.sync_worker import SyncWorker, SyncWorkerConfig


class _RecordingSimulationTransport(SimulationTransport):
    """SimulationTransport subclass that records dispatched requests for assertions."""

    def __init__(self, state: SimulationState | None = None) -> None:
        super().__init__(state=state)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return super().handle_request(request)


def _make_simulated_sdk() -> tuple[ImednetSDK, _RecordingSimulationTransport]:
    state = SimulationState()
    transport = _RecordingSimulationTransport(state=state)
    sdk = ImednetSDK(
        api_key="sim-key",
        security_key="sim-sec",
        base_url="https://simulation.local",
        transport=transport,
    )
    return sdk, transport


def test_sync_worker_run_once_updates_cache_with_created_and_modified_simulation_records(
    tmp_path: Path,
) -> None:
    """SyncWorker.run_once against SimulationTransport captures newly created and modified records."""
    sdk, transport = _make_simulated_sdk()
    study_key = "STUDY"
    loader = CachedRecordsLoader(sdk, cache_dir=tmp_path / "cache")
    initial_worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key=study_key, reconcile=True),
    )

    # 1. Initial sync populates default STUDY records (601, 602)
    assert initial_worker.run_once() == 2
    initial_cached = {
        rec.record_id: rec
        for rec in loader.iter_cached_records(study_key)
        if rec.record_id is not None
    }
    assert set(initial_cached) == {601, 602}
    assert initial_cached[601].record_data == {"V_AGE": 45, "V_GENDER": "Female"}

    transport.requests.clear()

    # 2. Create a new record upstream AND modify an existing record upstream
    sdk.records.create(
        study_key,
        [
            {
                "formKey": "F_DM",
                "subjectKey": "SUB1",
                "data": {"V_AGE": 49, "V_GENDER": "Male"},
            }
        ],
    )
    for raw_rec in transport.state.records:
        if raw_rec["studyKey"] == study_key and raw_rec["recordId"] == 601:
            raw_rec["recordData"] = {"V_AGE": 46, "V_GENDER": "Female"}
            raw_rec["dateModified"] = "2026-11-16T00:00:00Z"
            break

    transport.requests.clear()

    # 3. Incremental delta sync updates local SQLite cache with both new and modified rows
    delta_worker = SyncWorker(
        loader,
        config=SyncWorkerConfig(study_key=study_key, reconcile=False),
    )
    assert delta_worker.run_once() == 3

    assert len(transport.requests) == 1
    delta_params = dict(transport.requests[0].url.params)
    assert "dateModified>=" in delta_params["filter"]
    assert "filter==" not in delta_params["filter"]

    updated_cached = {
        rec.record_id: rec
        for rec in loader.iter_cached_records(study_key)
        if rec.record_id is not None
    }
    assert set(updated_cached) == {601, 602, 1001}
    assert updated_cached[601].record_data == {"V_AGE": 46, "V_GENDER": "Female"}
    assert updated_cached[1001].record_data == {"V_AGE": 49, "V_GENDER": "Male"}


def test_simulation_transport_evaluates_operators_quotes_datetimes_or_and_record_data_filter() -> (
    None
):
    """SimulationTransport._filter_and_paginate supports comparison ops, quotes, UTC dates, OR, and sort."""
    sdk, transport = _make_simulated_sdk()
    study_key = "STUDY"

    # Seed a third record and second subject so multi-item ranges and OR clauses are exercised
    transport.state.subjects.append(
        {
            "subjectId": 203,
            "subjectKey": "SUB2",
            "studyKey": study_key,
            "siteId": 101,
            "siteName": "Memorial Oncology Center",
            "subjectStatus": "Enrolled",
            "deleted": False,
            "keywords": [],
            "dateCreated": "2026-01-06T08:00:00Z",
            "dateModified": "2026-01-06T08:00:00Z",
            "lastUpdated": "2026-01-06T08:00:00Z",
        }
    )
    transport.state.records.append(
        {
            "recordId": 603,
            "studyKey": study_key,
            "subjectKey": "SUB2",
            "subjectId": 203,
            "formId": 301,
            "formKey": "F_DM",
            "visitId": 402,
            "intervalId": 502,
            "siteId": 101,
            "recordType": "Standard",
            "recordStatus": "Complete",
            "recordData": {"V_AGE": 62, "V_GENDER": "Male"},
            "deleted": False,
            "dateCreated": "2026-01-15T09:30:00Z",
            "dateModified": "2026-01-15T09:30:00Z",
            "lastUpdated": "2026-01-15T09:30:00Z",
        }
    )

    # Comparison operators (<, <=, >, >=, !=, ==, =~)
    gt_records = list(sdk.records.list(study_key, filter="recordId>601;recordId<=603"))
    assert [r.record_id for r in gt_records] == [602, 603]

    neq_records = list(sdk.records.list(study_key, filter="formKey!=F_VS"))
    assert [r.record_id for r in neq_records] == [601, 603]

    regex_subjects = list(sdk.subjects.list(study_key, filter='siteName=~"Memorial"'))
    assert [s.subject_key for s in regex_subjects] == ["SUB2"]

    # Quoted strings containing spaces
    quoted_sites = list(sdk.sites.list("DEMO-STUDY", filter='siteName=="North Site"'))
    assert [s.site_id for s in quoted_sites] == [102]

    # UTC ISO datetimes across +00:00 and Z representations
    dt_records = list(
        sdk.records.list(study_key, filter='dateModified>="2026-01-10T09:35:00+00:00"')
    )
    assert [r.record_id for r in dt_records] == [602, 603]

    # Multi-value OR filter
    or_subjects = list(sdk.subjects.list(study_key, subject_key=["SUB1", "SUB2"]))
    assert [s.subject_key for s in or_subjects] == ["SUB1", "SUB2"]

    # recordDataFilter evaluation and "none" projection
    rdf_records = list(sdk.records.list(study_key, record_data_filter="V_AGE>=60"))
    assert [r.record_id for r in rdf_records] == [603]

    stripped_records = list(sdk.records.list(study_key, record_data_filter="none"))
    assert len(stripped_records) == 3
    assert all(r.record_data in (None, {}) for r in stripped_records)

    # Sort descending
    sorted_records = list(sdk.records.list(study_key, sort="recordId,desc"))
    assert [r.record_id for r in sorted_records] == [603, 602, 601]


def test_get_queries_by_site_multi_subject_simulation_with_and_without_additional_filter() -> None:
    """QueryManagementWorkflow.get_queries_by_site works on >=2 subject sites with and without additional_filter."""
    sdk, transport = _make_simulated_sdk()
    study_key = "STUDY"
    transport.state.subjects.extend(
        [
            {
                "subjectId": 202,
                "subjectKey": "SUB2",
                "studyKey": study_key,
                "siteId": 101,
                "siteName": "SITE",
                "subjectStatus": "Enrolled",
                "deleted": False,
                "keywords": [],
                "dateCreated": "2026-01-05T08:00:00Z",
                "dateModified": "2026-01-05T08:00:00Z",
                "lastUpdated": "2026-01-05T08:00:00Z",
            },
            {
                "subjectId": 203,
                "subjectKey": "SUB_OTHER_SITE",
                "studyKey": study_key,
                "siteId": 199,
                "siteName": "OTHER_SITE",
                "subjectStatus": "Enrolled",
                "deleted": False,
                "keywords": [],
                "dateCreated": "2026-01-05T08:00:00Z",
                "dateModified": "2026-01-05T08:00:00Z",
                "lastUpdated": "2026-01-05T08:00:00Z",
            },
        ]
    )
    transport.state.queries.extend(
        [
            {
                "annotationId": 703,
                "studyKey": study_key,
                "subjectKey": "SUB2",
                "subjectId": 202,
                "recordId": 602,
                "description": "Follow-up query on SUB2.",
                "variable": "V_WEIGHT",
                "status": "OPEN",
                "queryComments": [{"sequence": 1, "closed": False, "comment": "Open"}],
            },
            {
                "annotationId": 704,
                "studyKey": study_key,
                "subjectKey": "SUB_OTHER_SITE",
                "subjectId": 203,
                "recordId": 602,
                "description": "Other site query.",
                "variable": "V_WEIGHT",
                "status": "OPEN",
                "queryComments": [{"sequence": 1, "closed": False, "comment": "Open"}],
            },
        ]
    )

    wf = QueryManagementWorkflow(sdk)

    # SITE has 2 subjects: SUB1 and SUB2
    transport.requests.clear()
    site_queries = wf.get_queries_by_site(study_key, "SITE")
    assert [q.annotation_id for q in site_queries] == [701, 702, 703]
    query_req = [r for r in transport.requests if r.url.path.endswith("/queries")][0]
    assert query_req.url.params["filter"] == "subjectKey==SUB1,subjectKey==SUB2"

    # With additional_filter: avoids mixing ';' and ',' and still excludes SUB_OTHER_SITE
    transport.requests.clear()
    filtered_site_queries = wf.get_queries_by_site(
        study_key,
        "SITE",
        additional_filter={"record_id": 602},
    )
    assert [q.annotation_id for q in filtered_site_queries] == [703]
    filtered_req = [r for r in transport.requests if r.url.path.endswith("/queries")][0]
    assert filtered_req.url.params["filter"] == "recordId==602"
    assert "," not in filtered_req.url.params["filter"]


def test_get_queries_by_site_batches_large_or_cohorts_and_handles_single_subject_or_filter() -> (
    None
):
    """get_queries_by_site chunks >50 subjects into pure-OR batches and avoids mixing on 1-subject OR filters."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key=f"S{i}") for i in range(65)]
    sdk.get_queries.side_effect = [
        [Query(annotation_id=1, subject_key="S0"), Query(annotation_id=2, subject_key="S1")],
        [Query(annotation_id=2, subject_key="S1"), Query(annotation_id=3, subject_key="S60")],
    ]
    wf = QueryManagementWorkflow(sdk)

    res = wf.get_queries_by_site("STUDY", "BIG_SITE")
    assert [q.annotation_id for q in res] == [1, 2, 3]
    assert sdk.get_queries.call_count == 2
    assert len(sdk.get_queries.call_args_list[0].kwargs["subject_key"]) == 50
    assert len(sdk.get_queries.call_args_list[1].kwargs["subject_key"]) == 15

    # Single subject with OR additional_filter falls back to client-side subject filtering
    sdk_single = MagicMock()
    sdk_single.get_subjects.return_value = [Subject(subject_key="S1")]
    sdk_single.get_queries.return_value = [
        Query(annotation_id=10, subject_key="S1"),
        Query(annotation_id=11, subject_key="S2"),
    ]
    wf_single = QueryManagementWorkflow(sdk_single)
    single_res = wf_single.get_queries_by_site(
        "STUDY", "ONE_SUBJ_SITE", additional_filter={"record_id": [100, 200]}
    )
    sdk_single.get_queries.assert_called_once_with("STUDY", record_id=[100, 200])
    assert [q.annotation_id for q in single_res] == [10]


def test_extract_records_by_criteria_pushes_or_filters_and_supports_and_criteria_in_simulation() -> (
    None
):
    """extract_records_by_criteria issues bounded OR filters when record_filter is empty and handles AND criteria."""
    sdk, transport = _make_simulated_sdk()
    study_key = "STUDY"
    transport.state.subjects.append(
        {
            "subjectId": 202,
            "subjectKey": "SUB2",
            "studyKey": study_key,
            "siteId": 101,
            "siteName": "SITE",
            "subjectStatus": "Enrolled",
            "deleted": False,
            "keywords": [],
            "dateCreated": "2026-01-05T08:00:00Z",
            "dateModified": "2026-01-05T08:00:00Z",
            "lastUpdated": "2026-01-05T08:00:00Z",
        }
    )
    transport.state.visits.append(
        {
            "visitId": 403,
            "studyKey": study_key,
            "intervalId": 501,
            "intervalName": "Baseline",
            "subjectId": 202,
            "subjectKey": "SUB2",
            "startDate": "2026-01-11T09:00:00Z",
            "visitDate": "2026-01-11T09:00:00Z",
            "deleted": False,
            "dateCreated": "2026-01-11T09:00:00Z",
            "dateModified": "2026-01-11T09:00:00Z",
            "lastUpdated": "2026-01-11T09:00:00Z",
        }
    )
    transport.state.records.append(
        {
            "recordId": 603,
            "studyKey": study_key,
            "subjectKey": "SUB_OTHER",
            "subjectId": 299,
            "formId": 301,
            "formKey": "F_DM",
            "visitId": 499,
            "intervalId": 501,
            "siteId": 199,
            "recordType": "Standard",
            "recordStatus": "Complete",
            "recordData": {"V_AGE": 31},
            "deleted": False,
            "dateCreated": "2026-01-12T09:30:00Z",
            "dateModified": "2026-01-12T09:30:00Z",
            "lastUpdated": "2026-01-12T09:30:00Z",
        }
    )

    wf = DataExtractionWorkflow(sdk)

    # 1. Selective subject_filter without record_filter -> server-side OR filter on subjectKey
    transport.requests.clear()
    by_site = wf.extract_records_by_criteria(
        study_key,
        subject_filter={"site_name": "SITE"},
    )
    assert [r.record_id for r in by_site] == [601, 602]
    rec_reqs = [r for r in transport.requests if r.url.path.endswith("/records")]
    assert len(rec_reqs) == 1
    assert rec_reqs[0].url.params["filter"] == "subjectKey==SUB1,subjectKey==SUB2"

    # 2. Selective visit_filter without record_filter -> server-side OR filter on visitId
    transport.requests.clear()
    by_visit = wf.extract_records_by_criteria(
        study_key,
        visit_filter={"interval_name": "Baseline"},
    )
    assert [r.record_id for r in by_visit] == [601, 602]
    visit_rec_reqs = [r for r in transport.requests if r.url.path.endswith("/records")]
    assert len(visit_rec_reqs) == 1
    assert visit_rec_reqs[0].url.params["filter"] == "visitId==401,visitId==403"

    # 3. Combined subject_filter + AND record_filter -> accurate intersection without connector errors
    transport.requests.clear()
    combined = wf.extract_records_by_criteria(
        study_key,
        subject_filter={"site_name": "SITE"},
        record_filter={"form_key": "F_DM"},
    )
    assert [r.record_id for r in combined] == [601]
    comb_reqs = [r for r in transport.requests if r.url.path.endswith("/records")]
    assert len(comb_reqs) == 1
    assert comb_reqs[0].url.params["filter"] == "formKey==F_DM"


def test_extract_records_by_criteria_batches_over_50_subjects_with_dedup() -> None:
    """extract_records_by_criteria batches >50 subjects into 50-item OR chunks and deduplicates."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key=f"S{i}") for i in range(60)]
    sdk.get_records.side_effect = [
        [Record(record_id=1, subject_key="S0"), Record(record_id=2, subject_key="S1")],
        [Record(record_id=2, subject_key="S1"), Record(record_id=3, subject_key="S55")],
    ]
    wf = DataExtractionWorkflow(sdk)

    res = wf.extract_records_by_criteria("STUDY", subject_filter={"status": "Active"})
    assert [r.record_id for r in res] == [1, 2, 3]
    assert sdk.get_records.call_count == 2
    assert len(sdk.get_records.call_args_list[0].kwargs["subject_key"]) == 50
    assert len(sdk.get_records.call_args_list[1].kwargs["subject_key"]) == 10
