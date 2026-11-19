"""Integration and unit tests for centralized workflow batch coordination and UAT Subject resolution."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from imednet import AsyncImednetSDK, ImednetSDK
from imednet.models.records import RegisterSubjectRequest
from imednet.simulation import SimulationState, create_simulation_transport
from imednet.validation import cache as validation_cache_mod
from imednet.validation.cache import SchemaValidator
from imednet_workflows import (
    BatchSubmissionSummary,
    RecordUpdateWorkflow,
    RegisterSubjectsWorkflow,
)
from imednet_workflows.uat import UATWorkflow


def _build_variables_payload(form_keys: list[str] | None = None) -> list[dict[str, Any]]:
    keys = form_keys or ["F1"]
    return [
        {
            "studyKey": "STUDY",
            "formId": idx + 1,
            "formKey": fk,
            "formName": f"Form {fk}",
            "variableId": idx + 1,
            "variableName": "age",
            "variableType": "integer",
            "sequence": 1,
            "revision": 1,
            "disabled": False,
            "deleted": False,
            "blinded": False,
            "variableOid": f"OID_{fk}",
            "label": "Age",
        }
        for idx, fk in enumerate(keys)
    ]


def test_record_update_550_records_sends_six_chunks_and_validates_once() -> None:
    """Submitting 550 Records via RecordUpdateWorkflow sends 6 POSTs and validates each Record once."""
    posted_chunks: list[list[dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/variables"):
            return httpx.Response(200, json={"data": _build_variables_payload(["F1"])})
        if request.method == "POST" and path.endswith("/records"):
            body = json.loads(request.content.decode("utf-8"))
            posted_chunks.append(body)
            idx = len(posted_chunks)
            return httpx.Response(
                200,
                json={"jobId": f"JOB-{idx}", "batchId": f"BATCH-{idx}", "state": "PROCESSING"},
            )
        if request.method == "GET" and "/jobs/BATCH-" in path and not path.endswith("/results"):
            batch_id = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "jobId": f"J-{batch_id}",
                    "batchId": batch_id,
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": f"https://mock.imednet.test/api/v1/edc/studies/STUDY/jobs/{batch_id}/results",
                },
            )
        if request.method == "GET" and path.endswith("/results"):
            return httpx.Response(200, json=[{"status": "SUCCESS"}])
        return httpx.Response(404)

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://mock.imednet.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        wf = RecordUpdateWorkflow(sdk)
        records = [{"formKey": "F1", "data": {"age": i % 90}} for i in range(550)]

        with patch.object(
            validation_cache_mod,
            "validate_record_data",
            wraps=validation_cache_mod.validate_record_data,
        ) as spy_validate:
            job = wf.create_or_update_records(
                "STUDY",
                records,
                wait_for_completion=True,
                poll_interval=0,
                timeout=10,
            )

        assert len(posted_chunks) == 6
        assert [len(c) for c in posted_chunks] == [100, 100, 100, 100, 100, 50]
        assert spy_validate.call_count == 550
        assert job.state == "COMPLETED"

        # Verify batch summary helper as well
        posted_chunks.clear()
        summary = wf.create_or_update_records_batch(
            "STUDY",
            records,
            wait_for_completion=True,
            poll_interval=0,
            timeout=10,
        )
        assert isinstance(summary, BatchSubmissionSummary)
        assert summary.chunk_count == 6
        assert summary.total_records == 550
        assert summary.poll_summary is not None
        assert summary.poll_summary.all_successful is True
    finally:
        sdk.close()


@pytest.mark.asyncio
async def test_async_record_update_550_records_and_distinct_form_refresh() -> None:
    """Async RecordUpdateWorkflow chunks 550 Records into 6 POSTs, validates once, and refreshes all forms."""
    posted_chunks: list[int] = []
    available_forms = ["F1"]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/variables"):
            return httpx.Response(200, json={"data": _build_variables_payload(available_forms)})
        if request.method == "POST" and path.endswith("/records"):
            body = json.loads(request.content.decode("utf-8"))
            posted_chunks.append(len(body))
            idx = len(posted_chunks)
            return httpx.Response(
                200,
                json={"jobId": f"AJ-{idx}", "batchId": f"AB-{idx}", "state": "PROCESSING"},
            )
        if request.method == "GET" and "/jobs/AB-" in path:
            batch_id = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={"jobId": f"AJ-{batch_id}", "batchId": batch_id, "state": "COMPLETED"},
            )
        return httpx.Response(404)

    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://mock.imednet.test",
        transport=httpx.MockTransport(handler),
    ) as sdk:
        wf = RecordUpdateWorkflow(sdk)

        # Prime cache with F1 only
        await wf.async_create_or_update_records("STUDY", [{"formKey": "F1", "data": {"age": 20}}])
        assert posted_chunks == [1]
        posted_chunks.clear()

        # Add F2 on server and submit mixed F1 + F2 batch of 550 records
        available_forms.append("F2")
        records = [
            {"formKey": "F1" if i % 2 == 0 else "F2", "data": {"age": i % 80}} for i in range(550)
        ]
        with patch.object(
            validation_cache_mod,
            "validate_record_data",
            wraps=validation_cache_mod.validate_record_data,
        ) as spy_validate:
            summary = await wf.async_create_or_update_records_batch(
                "STUDY",
                records,
                wait_for_completion=True,
                poll_interval=0,
                timeout=10,
            )

        assert posted_chunks == [100, 100, 100, 100, 100, 50]
        assert spy_validate.call_count == 550
        assert summary.chunk_count == 6
        assert summary.poll_summary is not None
        assert summary.poll_summary.all_successful is True

        # Unknown second form key in mixed batch raises ValueError
        with pytest.raises(ValueError, match="Form key 'F_MISSING' not found"):
            await wf.async_create_or_update_records(
                "STUDY",
                [
                    {"formKey": "F1", "data": {"age": 20}},
                    {"formKey": "F_MISSING", "data": {"age": 25}},
                ],
            )


def test_register_subjects_550_records_six_chunks_validates_once_and_filters_enrolled() -> None:
    """Submitting 550 RegisterSubjectRequest items sends 6 POSTs, validates once, and filters enrolled."""
    posted_chunks: list[list[dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/sites"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "STUDY",
                            "siteId": 1,
                            "siteName": "SITE-1",
                            "siteEnrollmentStatus": "Active",
                        }
                    ]
                },
            )
        if request.method == "GET" and path.endswith("/variables"):
            return httpx.Response(200, json={"data": _build_variables_payload(["ENROLL"])})
        if request.method == "GET" and path.endswith("/subjects"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "STUDY",
                            "subjectId": 1,
                            "subjectKey": "EXISTING-001",
                            "siteName": "SITE-1",
                        }
                    ]
                },
            )
        if request.method == "POST" and path.endswith("/records"):
            body = json.loads(request.content.decode("utf-8"))
            posted_chunks.append(body)
            idx = len(posted_chunks)
            return httpx.Response(
                200,
                json={"jobId": f"RJ-{idx}", "batchId": f"RB-{idx}", "state": "PROCESSING"},
            )
        if request.method == "GET" and "/jobs/RB-" in path:
            batch_id = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={"jobId": f"RJ-{batch_id}", "batchId": batch_id, "state": "COMPLETED"},
            )
        return httpx.Response(404)

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://mock.imednet.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        wf = RegisterSubjectsWorkflow(sdk)
        requests = [
            RegisterSubjectRequest(formKey="ENROLL", siteName="SITE-1", data={"age": i % 70})
            for i in range(550)
        ]

        # Default path (no explicit schema passed): get_sites called once and 6 chunked POSTs sent
        default_job = wf.register_subjects(
            "STUDY",
            requests,
            wait_for_completion=True,
            poll_interval=0,
            timeout=10,
        )
        assert len(posted_chunks) == 6
        assert [len(c) for c in posted_chunks] == [100, 100, 100, 100, 100, 50]
        assert default_job.state == "COMPLETED"

        # Explicit schema path: BatchCoordinator validates 550 items once and skips duplicate endpoint validation
        posted_chunks.clear()
        validator = SchemaValidator(sdk)
        validator.schema.refresh(sdk.forms, sdk.variables, study_key="STUDY")

        with patch.object(
            validation_cache_mod,
            "validate_record_data",
            wraps=validation_cache_mod.validate_record_data,
        ) as spy_validate:
            job = wf.register_subjects(
                "STUDY",
                requests,
                schema=validator.schema,
                wait_for_completion=True,
                poll_interval=0,
                timeout=10,
            )

        assert len(posted_chunks) == 6
        assert [len(c) for c in posted_chunks] == [100, 100, 100, 100, 100, 50]
        assert spy_validate.call_count == 550
        assert job.state == "COMPLETED"

        # Optional filtering of already enrolled Subjects and duplicate candidate keys
        posted_chunks.clear()
        filtered_summary = wf.register_subjects_batch(
            "STUDY",
            [
                {
                    "formKey": "ENROLL",
                    "siteName": "SITE-1",
                    "data": {"SUBJ_ID": "EXISTING-001", "age": 30},
                },
                {
                    "formKey": "ENROLL",
                    "siteName": "SITE-1",
                    "data": {"SUBJ_ID": "NEW-002", "age": 31},
                },
                {
                    "formKey": "ENROLL",
                    "siteName": "SITE-1",
                    "data": {"SUBJ_ID": "NEW-002", "age": 31},
                },
            ],
            filter_enrolled=True,
            subject_key_field="SUBJ_ID",
        )
        assert filtered_summary.total_records == 1
        assert len(posted_chunks) == 1
        assert posted_chunks[0][0]["data"]["SUBJ_ID"] == "NEW-002"

        # When every requested Subject is already enrolled, no POST is issued
        posted_chunks.clear()
        noop_job = wf.register_subjects(
            "STUDY",
            [{"formKey": "ENROLL", "siteName": "SITE-1", "subjectKey": "EXISTING-001"}],
            skip_existing=True,
        )
        assert posted_chunks == []
        assert noop_job.state == "COMPLETED"
    finally:
        sdk.close()


def test_uat_workflow_run_simulation_enrolls_without_subject_key_and_resolves_phase2() -> None:
    """UATWorkflow.run() enrolls Subjects without subjectKey and submits Phase 2 with enrolled keys."""
    # First verify out-of-the-box default simulation study works end-to-end
    default_sdk = ImednetSDK(simulation_mode=True)
    try:
        default_res = UATWorkflow(default_sdk, poll_interval=0, poll_timeout=10).run(
            "STUDY", subject_count=2
        )
        assert default_res.overall_success is True
    finally:
        default_sdk.close()

    # Now verify against a fresh empty study with Enrollment, Scheduled, and Unscheduled forms
    sim_state = SimulationState()
    study_key = "SIM-UAT"
    sim_state.studies.append({"studyKey": study_key, "studyName": "Simulated UAT Study"})
    sim_state.sites.append(
        {
            "studyKey": study_key,
            "siteId": 1,
            "siteName": "Site Alpha",
            "siteEnrollmentStatus": "Active",
        }
    )
    sim_state.forms.extend(
        [
            {
                "studyKey": study_key,
                "formId": 1,
                "formKey": "DEMOG",
                "formName": "Demographics",
                "formType": "Enrollment",
                "subjectRecordReport": True,
            },
            {
                "studyKey": study_key,
                "formId": 2,
                "formKey": "VITALS",
                "formName": "Vitals",
                "formType": "DataEntry",
            },
            {
                "studyKey": study_key,
                "formId": 3,
                "formKey": "AE",
                "formName": "Adverse Events",
                "formType": "DataEntry",
                "unscheduledVisit": True,
            },
        ]
    )
    sim_state.variables.extend(
        [
            {
                "studyKey": study_key,
                "formId": 1,
                "formKey": "DEMOG",
                "formName": "Demographics",
                "variableId": 1,
                "variableName": "DEM_INIT",
                "variableKey": "DEM_INIT",
                "variableType": "Text",
            },
            {
                "studyKey": study_key,
                "formId": 2,
                "formKey": "VITALS",
                "formName": "Vitals",
                "variableId": 2,
                "variableName": "SYS_BP",
                "variableKey": "SYS_BP",
                "variableType": "Integer",
            },
            {
                "studyKey": study_key,
                "formId": 3,
                "formKey": "AE",
                "formName": "Adverse Events",
                "variableId": 3,
                "variableName": "AE_TERM",
                "variableKey": "AE_TERM",
                "variableType": "Text",
            },
        ]
    )
    sim_state.intervals = [
        {
            "studyKey": study_key,
            "intervalId": 1,
            "intervalName": "Baseline",
            "forms": [{"formId": 2, "formKey": "VITALS", "formName": "Vitals"}],
        }
    ]
    sim_state.subjects = []
    sim_state.records = []

    transport = create_simulation_transport(state=sim_state)
    sdk = ImednetSDK(simulation_mode=True, transport=transport)
    try:
        workflow = UATWorkflow(sdk, poll_interval=0, poll_timeout=10)
        result = workflow.run(study_key, subject_count=2)

        assert result.overall_success is True

        # Verify Phase 1 registration record set omitted subjectKey
        reg_set = next(rs for rs in result.record_sets if rs.form_key == "DEMOG")
        assert len(reg_set.payloads) == 2
        for payload in reg_set.payloads:
            assert "subjectKey" not in payload
            assert payload["siteName"] == "Site Alpha"

        # Verify SimulationState synced newly enrolled subjects
        enrolled_subjects = list(sdk.get_subjects(study_key))
        enrolled_keys = [s.subject_key for s in enrolled_subjects if s.subject_key]
        assert enrolled_keys == ["SUBJ-1001", "SUBJ-1002"]

        # Verify Phase 2 data sets resolved and submitted the server-assigned Subject keys
        vitals_set = next(rs for rs in result.record_sets if rs.form_key == "VITALS")
        ae_set = next(rs for rs in result.record_sets if rs.form_key == "AE")
        assert [p["subjectKey"] for p in vitals_set.payloads] == ["SUBJ-1001", "SUBJ-1002"]
        assert [p["subjectKey"] for p in ae_set.payloads] == ["SUBJ-1001", "SUBJ-1002"]

        # Verify all submitted records exist in SimulationState with the enrolled Subject keys
        stored_records = list(sdk.records.list(study_key))
        phase2_stored = [r for r in stored_records if r.form_key in {"VITALS", "AE"}]
        assert len(phase2_stored) == 4
        assert {r.subject_key for r in phase2_stored} == {"SUBJ-1001", "SUBJ-1002"}
    finally:
        sdk.close()


def test_uat_workflow_fetches_result_url_and_flags_partial_failures() -> None:
    """UATWorkflow resolves Subject keys from resultUrl and fails monitor on nonzero failed_records."""
    submitted_batches: list[list[dict[str, Any]]] = []
    result_url_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/forms"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "S",
                            "formId": 1,
                            "formKey": "REG",
                            "formName": "Reg",
                            "formType": "Enrollment",
                        },
                        {
                            "studyKey": "S",
                            "formId": 2,
                            "formKey": "VIS",
                            "formName": "Visit",
                            "formType": "DataEntry",
                            "unscheduledVisit": True,
                        },
                    ]
                },
            )
        if request.method == "GET" and path.endswith("/variables"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "S",
                            "formId": 1,
                            "formKey": "REG",
                            "formName": "Reg",
                            "variableId": 1,
                            "variableName": "V1",
                            "variableKey": "V1",
                            "variableType": "Text",
                        },
                        {
                            "studyKey": "S",
                            "formId": 2,
                            "formKey": "VIS",
                            "formName": "Visit",
                            "variableId": 2,
                            "variableName": "V2",
                            "variableKey": "V2",
                            "variableType": "Text",
                        },
                    ]
                },
            )
        if request.method == "GET" and path.endswith("/intervals"):
            return httpx.Response(200, json={"data": []})
        if request.method == "GET" and path.endswith("/sites"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "S",
                            "siteId": 1,
                            "siteName": "Site1",
                            "siteEnrollmentStatus": "Active",
                        }
                    ]
                },
            )
        if request.method == "GET" and path.endswith("/subjects"):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and path.endswith("/records"):
            body = json.loads(request.content.decode("utf-8"))
            submitted_batches.append(body)
            bid = f"B{len(submitted_batches)}"
            return httpx.Response(
                200, json={"jobId": f"J-{bid}", "batchId": bid, "state": "QUEUED"}
            )
        if request.method == "GET" and "/jobs/B" in path and not path.endswith("/results"):
            bid = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "jobId": f"J-{bid}",
                    "batchId": bid,
                    "state": "COMPLETED",
                    "progress": 100,
                    "resultUrl": f"https://mock.imednet.test/api/v1/edc/studies/S/jobs/{bid}/results",
                },
            )
        if request.method == "GET" and path.endswith("/results"):
            result_url_calls.append(str(request.url))
            bid = path.split("/")[-2]
            if bid == "B1":
                return httpx.Response(
                    200,
                    json=[
                        {"subjectKey": "ASSIGNED-101", "status": "SUCCESS"},
                        {"subjectKey": "ASSIGNED-102", "status": "SUCCESS"},
                    ],
                )
            # B2 has one failed record -> partial failure must mark UAT overall_success False
            return httpx.Response(
                200,
                json=[
                    {"subjectKey": "ASSIGNED-101", "status": "SUCCESS"},
                    {"subjectKey": "ASSIGNED-102", "status": "FAILED", "error": "Invalid visit"},
                ],
            )
        return httpx.Response(404)

    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://mock.imednet.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        wf = UATWorkflow(sdk, poll_interval=0, poll_timeout=10)
        res = wf.run("S", subject_count=2)

        assert len(submitted_batches) == 2
        # Phase 1 registration omitted subjectKey
        assert all("subjectKey" not in rec for rec in submitted_batches[0])
        # Phase 2 used server-assigned Subject keys from B1's resultUrl
        assert [rec["subjectKey"] for rec in submitted_batches[1]] == [
            "ASSIGNED-101",
            "ASSIGNED-102",
        ]
        assert len(result_url_calls) >= 2
        # Because B2 had 1 failed record in its resultUrl payload, overall_success is False
        assert res.overall_success is False
        assert res.poll_summary.all_successful is False
        assert "B2" in res.poll_summary.failures
    finally:
        sdk.close()
