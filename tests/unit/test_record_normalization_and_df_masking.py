"""Unit tests for unified Record normalization and DataFrame PHI masking pipelines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pydantic
import pytest
from cryptography.fernet import Fernet

from imednet import AsyncImednetSDK, ImednetSDK
from imednet.core.context import (
    reset_base_url_context,
    reset_security_key_context,
    reset_study_context,
    set_base_url_context,
    set_security_key_context,
    set_study_context,
)
from imednet.errors import ValidationError
from imednet.models.forms import Form
from imednet.models.intervals import Interval
from imednet.models.queries import Query
from imednet.models.records import (
    BaseRecordRequest,
    CreateNewRecordRequest,
    Record,
    RegisterSubjectRequest,
    UpdateScheduledRecordRequest,
)
from imednet.models.sites import Site
from imednet.models.study_config import StudyConfiguration
from imednet.models.subjects import Subject
from imednet.models.users import User
from imednet.models.variables import Variable
from imednet.models.visits import Visit
from imednet.simulation import SimulationState, create_simulation_transport
from imednet.utils.enrollment import normalize_subject_registration_inputs
from imednet.utils.pandas import (
    export_records_csv,
    forms_to_dataframe,
    intervals_to_dataframe,
    queries_to_dataframe,
    records_to_dataframe,
    sites_to_dataframe,
    subjects_to_dataframe,
    users_to_dataframe,
    variables_to_dataframe,
    visits_to_dataframe,
)
from imednet.utils.tabular import (
    MASKED_SENTINEL,
    ensure_no_masked_sentinel,
    normalize_records_input,
)
from imednet_workflows.config_version_control import ConfigVersionStore
from imednet_workflows.record_update import AsyncRecordUpdateWorkflow, RecordUpdateWorkflow


def _build_seeded_state(study_key: str = "STUDY_NORM") -> SimulationState:
    """Create a deterministic SimulationState with forms, sites, intervals, users, and records."""
    state = SimulationState()
    state.studies = [
        {
            "studyKey": study_key,
            "studyName": f"Study {study_key}",
            "studyDescription": "Test study",
            "studyType": "CLINICAL",
        }
    ]
    state.sites = [
        {
            "studyKey": study_key,
            "siteId": 10,
            "siteName": "SITE-01",
            "siteEnrollmentStatus": "Open",
        }
    ]
    state.forms = [
        {
            "studyKey": study_key,
            "formId": 101,
            "formKey": "ENROLL",
            "formName": "Enrollment",
            "formType": "SUBJECT",
            "subjectRecordReport": True,
            "unscheduledVisit": False,
            "otherForms": False,
            "eproForm": False,
            "allowCopy": False,
            "disabled": False,
        },
        {
            "studyKey": study_key,
            "formId": 102,
            "formKey": "VITALS",
            "formName": "Vitals",
            "formType": "VISIT",
            "subjectRecordReport": False,
            "unscheduledVisit": True,
            "otherForms": False,
            "eproForm": False,
            "allowCopy": False,
            "disabled": False,
        },
    ]
    state.intervals = [
        {
            "studyKey": study_key,
            "intervalId": 201,
            "intervalName": "Baseline",
            "intervalSequence": 1,
            "disabled": False,
            "forms": [{"formId": 102, "formKey": "VITALS", "formName": "Vitals"}],
        }
    ]
    state.variables = [
        {
            "studyKey": study_key,
            "variableId": 1001,
            "variableName": "V_AGE",
            "variableType": "INTEGER",
            "label": "Age",
            "formId": 101,
            "formKey": "ENROLL",
            "formName": "Enrollment",
            "deleted": False,
            "disabled": False,
        },
        {
            "studyKey": study_key,
            "variableId": 1002,
            "variableName": "SYS_BP",
            "variableType": "INTEGER",
            "label": "Systolic BP",
            "formId": 102,
            "formKey": "VITALS",
            "formName": "Vitals",
            "deleted": False,
            "disabled": False,
        },
    ]
    state.subjects = [
        {
            "studyKey": study_key,
            "subjectId": 501,
            "subjectOid": "OID-SUBJ-501",
            "subjectKey": "SUBJ-001",
            "subjectStatus": "Enrolled",
            "siteId": 10,
            "siteName": "SITE-01",
            "deleted": False,
            "keywords": [],
        }
    ]
    state.visits = [
        {
            "studyKey": study_key,
            "visitId": 301,
            "intervalId": 201,
            "intervalName": "Baseline",
            "subjectId": 501,
            "subjectKey": "SUBJ-001",
            "deleted": False,
        }
    ]
    state.records = [
        {
            "studyKey": study_key,
            "recordId": 9001,
            "recordOid": "OID-REC-9001",
            "formId": 102,
            "formKey": "VITALS",
            "siteId": 10,
            "siteName": "SITE-01",
            "subjectId": 501,
            "subjectOid": "OID-SUBJ-501",
            "subjectKey": "SUBJ-001",
            "visitId": 301,
            "intervalId": 201,
            "intervalName": "Baseline",
            "recordStatus": "Record Incomplete",
            "deleted": False,
            "recordData": {"SYS_BP": 120, "patientName": "Alice Secret"},
        }
    ]
    state.users = [
        {
            "userId": "user-1",
            "login": "investigator1",
            "firstName": "Jane",
            "lastName": "Doe",
            "email": "jane.doe@clinic.example",
            "userActiveInStudy": True,
            "roles": [],
        }
    ]
    state.queries = [
        {
            "studyKey": study_key,
            "subjectId": 501,
            "subjectOid": "OID-SUBJ-501",
            "subjectKey": "SUBJ-001",
            "recordId": 9001,
            "annotationId": 701,
            "annotationType": "Monitor Question",
            "description": "Verify contact email=patient.secret@hospital.org and patientName=Alice",
            "queryComments": [
                {
                    "sequence": 1,
                    "annotationStatus": "Open",
                    "user": "monitor",
                    "comment": '{"ssn": "123-45-6789"}',
                    "closed": False,
                }
            ],
        }
    ]
    state.record_revisions = [
        {
            "studyKey": study_key,
            "recordRevisionId": 801,
            "recordId": 9001,
            "recordOid": "OID-REC-9001",
            "recordRevision": 1,
            "dataRevision": 1,
            "recordStatus": "Record Complete",
            "subjectId": 501,
            "subjectOid": "OID-SUBJ-501",
            "subjectKey": "SUBJ-001",
            "siteId": 10,
            "formKey": "VITALS",
            "intervalId": 201,
            "role": "Investigator",
            "user": "investigator1",
            "reasonForChange": "Updated email=leak@clinic.org for patient",
            "deleted": False,
        }
    ]
    state.codings = [
        {
            "studyKey": study_key,
            "siteName": "SITE-01",
            "subjectId": 501,
            "subjectKey": "SUBJ-001",
            "formId": 102,
            "formName": "Vitals",
            "formKey": "VITALS",
            "revision": 1,
            "recordId": 9001,
            "variable": "SYS_BP",
            "value": "email=inline@clinic.org",
            "codingId": 601,
            "code": "C001",
            "codedBy": "coder1",
            "reason": "Routine coding",
            "dictionaryName": "MedDRA",
            "dictionaryVersion": "26.0",
        }
    ]
    return state


def test_base_record_request_all_9_write_attributes_and_extra_forbid() -> None:
    """Verify all 9 added write attributes round-trip in camelCase and extra='forbid' holds."""
    req = BaseRecordRequest(
        formKey="VITALS",
        formId=102,
        siteName="SITE-01",
        siteId=10,
        subjectKey="SUBJ-001",
        subjectId=501,
        subjectOid="OID-SUBJ-501",
        intervalName="Baseline",
        intervalId=201,
        visitId=301,
        recordId=9001,
        recordOid="OID-REC-9001",
        recordStatus="Record Complete",
        reasonForChange="Verified source chart",
        data={"SYS_BP": 118},
    )
    dumped = req.model_dump()
    assert dumped == {
        "formKey": "VITALS",
        "formId": 102,
        "siteName": "SITE-01",
        "siteId": 10,
        "subjectKey": "SUBJ-001",
        "subjectId": 501,
        "subjectOid": "OID-SUBJ-501",
        "intervalName": "Baseline",
        "intervalId": 201,
        "visitId": 301,
        "recordId": 9001,
        "recordOid": "OID-REC-9001",
        "recordStatus": "Record Complete",
        "reasonForChange": "Verified source chart",
        "data": {"SYS_BP": 118},
    }

    # Numeric-ID / OID subclass constructors omit empty string fallbacks
    reg_req = RegisterSubjectRequest(
        form_id=101,
        site_id=10,
        subject_oid="OID-NEW-1",
        reason_for_change="Initial enrollment",
        data={"V_AGE": 44},
    )
    assert reg_req.model_dump() == {
        "formId": 101,
        "siteId": 10,
        "subjectOid": "OID-NEW-1",
        "reasonForChange": "Initial enrollment",
        "data": {"V_AGE": 44},
    }

    sched_req = UpdateScheduledRecordRequest(
        form_id=102,
        subject_id=501,
        interval_id=201,
        visit_id=301,
        record_id=9001,
        record_oid="OID-REC-9001",
        record_status="Record Complete",
        reason_for_change="Scheduled update",
        data={"SYS_BP": 122},
    )
    assert sched_req.model_dump() == {
        "formId": 102,
        "subjectId": 501,
        "intervalId": 201,
        "visitId": 301,
        "recordId": 9001,
        "recordOid": "OID-REC-9001",
        "recordStatus": "Record Complete",
        "reasonForChange": "Scheduled update",
        "data": {"SYS_BP": 122},
    }

    new_req = CreateNewRecordRequest(
        form_id=102,
        subject_oid="OID-SUBJ-501",
        record_status="Record Incomplete",
        reason_for_change="Unscheduled visit",
        data={"SYS_BP": 130},
    )
    assert new_req.model_dump() == {
        "formId": 102,
        "subjectOid": "OID-SUBJ-501",
        "recordStatus": "Record Incomplete",
        "reasonForChange": "Unscheduled visit",
        "data": {"SYS_BP": 130},
    }

    # Unknown attributes on BaseRecordRequest and subclasses raise pydantic.ValidationError
    with pytest.raises(pydantic.ValidationError):
        BaseRecordRequest(formKey="VITALS", unknownField="bad")  # type: ignore[call-arg]

    with pytest.raises(pydantic.ValidationError):
        CreateNewRecordRequest(
            form_key="VITALS",
            subject_key="SUBJ-001",
            unexpected_attr=123,  # type: ignore[call-arg]
        )

    # Read models inheriting ImednetBaseModel preserve extra='ignore'
    read_rec = Record.model_validate({"recordId": 1, "futureUpstreamField": "tolerated"})
    assert read_rec.record_id == 1
    assert not hasattr(read_rec, "futureUpstreamField")


def test_masked_sentinel_blocked_on_models_and_mappings() -> None:
    """Verify ensure_no_masked_sentinel and normalize_records_input reject ***MASKED***."""
    masked_model = CreateNewRecordRequest(
        form_key="VITALS",
        subject_key="SUBJ-001",
        data={"SYS_BP": MASKED_SENTINEL},
    )
    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        ensure_no_masked_sentinel([masked_model])

    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        normalize_records_input([masked_model])

    masked_reason_model = UpdateScheduledRecordRequest(
        form_id=102,
        subject_id=501,
        interval_id=201,
        reason_for_change=MASKED_SENTINEL,
        data={"SYS_BP": 120},
    )
    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        normalize_records_input([masked_reason_model])

    masked_mapping = {
        "form_key": "VITALS",
        "subject_key": "SUBJ-001",
        "data": {"X": MASKED_SENTINEL},
    }
    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        ensure_no_masked_sentinel([masked_mapping])

    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        normalize_records_input([masked_mapping])


def test_normalize_records_input_converts_models_and_snake_case_mappings() -> None:
    """Verify normalize_records_input normalizes BaseRecordRequest and snake_case dicts."""
    model_item = UpdateScheduledRecordRequest(
        form_id=102,
        subject_id=501,
        interval_id=201,
        visit_id=301,
        record_id=9001,
        record_status="Record Complete",
        reason_for_change="Audit review",
        data={"SYS_BP": 119},
    )
    snake_item = {
        "form_key": "VITALS",
        "subject_key": "SUBJ-001",
        "site_name": "SITE-01",
        "interval_name": "Baseline",
        "record_id": 9001,
        "reason_for_change": "Snake case input",
        "record_data": {"SYS_BP": 121},
        "study_key": "STUDY_NORM",
        "date_created": "2026-01-01T00:00:00Z",
    }
    normalized = normalize_records_input((model_item, snake_item))
    assert normalized == [
        {
            "formId": 102,
            "subjectId": 501,
            "intervalId": 201,
            "visitId": 301,
            "recordId": 9001,
            "recordStatus": "Record Complete",
            "reasonForChange": "Audit review",
            "data": {"SYS_BP": 119},
        },
        {
            "formKey": "VITALS",
            "subjectKey": "SUBJ-001",
            "siteName": "SITE-01",
            "intervalName": "Baseline",
            "recordId": 9001,
            "reasonForChange": "Snake case input",
            "data": {"SYS_BP": 121},
        },
    ]


def test_enrollment_dict_coercion_strips_readonly_keys_and_preserves_forbid() -> None:
    """Verify normalize_subject_registration_inputs strips read-only keys for dict callers."""
    coerced = normalize_subject_registration_inputs(
        [
            {
                "form_key": "ENROLL",
                "site_name": "SITE-01",
                "subject_key": "SUBJ-NEW-01",
                "study_key": "STUDY_NORM",
                "subject_status": "Enrolled",
                "deleted": False,
                "date_created": "2026-01-01T00:00:00Z",
                "record_data": {"V_AGE": 37},
                "reason_for_change": "Imported from subject export",
            }
        ]
    )
    assert len(coerced) == 1
    assert coerced[0][1].model_dump() == {
        "formKey": "ENROLL",
        "siteName": "SITE-01",
        "subjectKey": "SUBJ-NEW-01",
        "reasonForChange": "Imported from subject export",
        "data": {"V_AGE": 37},
    }

    # Unknown non-readonly attributes still fail validation under extra='forbid'
    with pytest.raises((ValidationError, pydantic.ValidationError)):
        normalize_subject_registration_inputs(
            [
                {
                    "form_key": "ENROLL",
                    "site_name": "SITE-01",
                    "subject_key": "SUBJ-NEW-02",
                    "completelyBogusField": "nope",
                }
            ]
        )


@pytest.mark.asyncio
async def test_sync_and_async_sdk_and_workflows_accept_record_request_models() -> None:
    """Verify sync/async SDK and RecordUpdateWorkflow accept all BaseRecordRequest subclasses."""
    study_key = "STUDY_NORM"
    state = _build_seeded_state(study_key)
    captured_wire_payloads: list[Any] = []
    inner_transport = create_simulation_transport(state)

    def _capturing_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/records"):
            captured_wire_payloads.append(json.loads(request.content.decode("utf-8")))
        return inner_transport.handle_request(request)

    async def _async_capturing_handler(request: httpx.Request) -> httpx.Response:
        return _capturing_handler(request)

    sdk = ImednetSDK(
        api_key="test-key",  # pragma: allowlist secret
        security_key="test-sec",  # pragma: allowlist secret
        transport=httpx.MockTransport(_capturing_handler),
        simulation_mode=True,
    )
    async_sdk = AsyncImednetSDK(
        api_key="test-key",  # pragma: allowlist secret
        security_key="test-sec",  # pragma: allowlist secret
        transport=httpx.MockTransport(_async_capturing_handler),
        simulation_mode=True,
    )

    reg_model = RegisterSubjectRequest(
        form_key="ENROLL",
        site_name="SITE-01",
        subject_key="SUBJ-002",
        reason_for_change="Register subject",
        data={"V_AGE": 41},
    )
    sched_model = UpdateScheduledRecordRequest(
        form_id=102,
        subject_id=501,
        interval_id=201,
        visit_id=301,
        record_id=9001,
        record_status="Record Complete",
        reason_for_change="Scheduled vitals update",
        data={"SYS_BP": 116},
    )
    new_model = CreateNewRecordRequest(
        form_key="VITALS",
        subject_oid="OID-SUBJ-501",
        reason_for_change="Unscheduled vitals",
        data={"SYS_BP": 128},
    )
    base_model = BaseRecordRequest(
        formId=102,
        subjectKey="SUBJ-001",
        data={"SYS_BP": 124},
    )

    # Sync SDK methods (including dict with recordData alias)
    job_1 = sdk.records.create(
        study_key,
        [
            reg_model,
            sched_model,
            {"formKey": "VITALS", "subjectKey": "SUBJ-001", "recordData": {"SYS_BP": 117}},
        ],
    )
    assert job_1.batch_id
    assert "data" in captured_wire_payloads[0][2]
    assert "recordData" not in captured_wire_payloads[0][2]
    job_2 = sdk.create_record(study_key, [new_model, base_model])
    assert job_2.batch_id
    batch_1 = sdk.records.create_batch(study_key, [sched_model], chunk_size=1)
    assert len(batch_1.jobs) == 1

    # Sync RecordUpdateWorkflow
    wf = RecordUpdateWorkflow(sdk)
    wf_job = wf.create_or_update_records(study_key, [sched_model, new_model])
    assert wf_job.batch_id
    wf_batch = wf.create_or_update_records_batch(study_key, [base_model], chunk_size=1)
    assert len(wf_batch.jobs) == 1

    # Async SDK & AsyncRecordUpdateWorkflow methods
    ajob_1 = await async_sdk.records.create(
        study_key,
        [
            RegisterSubjectRequest(
                form_key="ENROLL",
                site_name="SITE-01",
                subject_key="SUBJ-003",
                data={"V_AGE": 52},
            )
        ],
    )
    assert ajob_1.batch_id
    ajob_2 = await async_sdk.create_record(study_key, [sched_model])
    assert ajob_2.batch_id
    abatch_1 = await async_sdk.records.create_batch(study_key, [new_model], chunk_size=1)
    assert len(abatch_1.jobs) == 1

    awf = AsyncRecordUpdateWorkflow(async_sdk)
    awf_job = await awf.create_or_update_records(study_key, [sched_model])
    assert awf_job.batch_id
    awf_batch = await awf.create_or_update_records_batch(study_key, [new_model], chunk_size=1)
    assert len(awf_batch.jobs) == 1

    # Verify every wire payload sent to POST /records uses strictly camelCase envelope keys
    assert len(captured_wire_payloads) == 10
    for batch_payload in captured_wire_payloads:
        for wire_item in batch_payload:
            for key in wire_item:
                assert "_" not in key, f"Unexpected snake_case wire key: {key}"


def test_simulation_transport_rejects_raw_snake_case_keys_with_400() -> None:
    """Verify raw HTTP POST /records with snake_case wire keys returns 400 BAD_REQUEST."""
    study_key = "STUDY_NORM"
    state = _build_seeded_state(study_key)
    transport = create_simulation_transport(state)
    client = httpx.Client(
        base_url="https://edc.prod.imednetapi.com",
        transport=transport,
        headers={"x-api-key": "sim-key", "x-imn-security-key": "sim-sec"},
    )

    bad_payloads = [
        [{"form_key": "VITALS", "subjectKey": "SUBJ-001", "data": {"SYS_BP": 120}}],
        [{"formKey": "VITALS", "subject_key": "SUBJ-001", "data": {"SYS_BP": 120}}],
        [{"formKey": "VITALS", "subjectKey": "SUBJ-001", "record_data": {"SYS_BP": 120}}],
        [
            {
                "formKey": "VITALS",
                "subjectKey": "SUBJ-001",
                "reason_for_change": "bad snake key",
                "data": {"SYS_BP": 120},
            }
        ],
    ]
    for payload in bad_payloads:
        resp = client.post(f"/api/v1/edc/studies/{study_key}/records", json=payload)
        assert resp.status_code == 400
        body = resp.json()
        assert body["metadata"]["status"] == "BAD_REQUEST"

    # Canonical camelCase wire payload (with UPPER_SNAKE_CASE clinical variable in data) succeeds
    ok_resp = client.post(
        f"/api/v1/edc/studies/{study_key}/records",
        json=[{"formKey": "VITALS", "subjectKey": "SUBJ-001", "data": {"SYS_BP": 125}}],
    )
    assert ok_resp.status_code in (200, 202)


@pytest.mark.asyncio
async def test_all_12_sync_and_async_endpoints_and_9_converters_mask_phi() -> None:
    """Verify all 12 sync/async .to_dataframe(mask_phi=True) endpoints and 9 pandas converters."""
    study_key = "STUDY_NORM"
    state = _build_seeded_state(study_key)
    transport = create_simulation_transport(state)

    async def _async_handler(request: httpx.Request) -> httpx.Response:
        return transport.handle_request(request)

    sdk = ImednetSDK(
        api_key="test-key",  # pragma: allowlist secret
        security_key="test-sec",  # pragma: allowlist secret
        transport=transport,
        simulation_mode=True,
    )
    async_sdk = AsyncImednetSDK(
        api_key="test-key",  # pragma: allowlist secret
        security_key="test-sec",  # pragma: allowlist secret
        transport=httpx.MockTransport(_async_handler),
        simulation_mode=True,
    )

    study_scoped_endpoints = (
        "codings",
        "forms",
        "intervals",
        "queries",
        "record_revisions",
        "records",
        "sites",
        "subjects",
        "users",
        "variables",
        "visits",
    )

    # 1. All 12 sync endpoints accept mask_phi=True without ConfigurationError
    df_studies = sdk.studies.to_dataframe(mask_phi=True)
    assert len(df_studies) >= 1
    for ep_name in study_scoped_endpoints:
        ep = getattr(sdk, ep_name)
        df_masked = ep.to_dataframe(study_key, mask_phi=True)
        assert not df_masked.empty, f"{ep_name}.to_dataframe() returned empty DataFrame"
        if "study_key" in df_masked.columns:
            assert df_masked.loc[0, "study_key"] == study_key
        if "subject_key" in df_masked.columns:
            assert df_masked.loc[0, "subject_key"] == "SUBJ-001"
        if "record_id" in df_masked.columns:
            assert df_masked.loc[0, "record_id"] == 9001

    # 2. All 12 async endpoints accept mask_phi=True without ConfigurationError
    adf_studies = await async_sdk.studies.to_dataframe(mask_phi=True)
    assert len(adf_studies) >= 1
    for ep_name in study_scoped_endpoints:
        aep = getattr(async_sdk, ep_name)
        adf_masked = await aep.to_dataframe(study_key, mask_phi=True)
        assert not adf_masked.empty, f"async {ep_name}.to_dataframe() returned empty DataFrame"

    # 3. Verify default PHI masking vs mask_phi=False on users, queries, and record_revisions
    users_default = sdk.users.to_dataframe(study_key)
    assert users_default.loc[0, "email"] == MASKED_SENTINEL
    users_raw = sdk.users.to_dataframe(study_key, mask_phi=False)
    assert users_raw.loc[0, "email"] == "jane.doe@clinic.example"

    queries_default = sdk.queries.to_dataframe(study_key)
    assert "patient.secret@hospital.org" not in queries_default.loc[0, "description"]
    assert MASKED_SENTINEL in queries_default.loc[0, "description"]
    assert "123-45-6789" not in json.dumps(queries_default.loc[0, "query_comments"])
    queries_raw = sdk.queries.to_dataframe(study_key, mask_phi=False)
    assert "patient.secret@hospital.org" in queries_raw.loc[0, "description"]
    assert "123-45-6789" in json.dumps(queries_raw.loc[0, "query_comments"])

    revs_default = sdk.record_revisions.to_dataframe(study_key)
    assert "leak@clinic.org" not in revs_default.loc[0, "reason_for_change"]
    assert MASKED_SENTINEL in revs_default.loc[0, "reason_for_change"]
    revs_raw = sdk.record_revisions.to_dataframe(study_key, mask_phi=False)
    assert "leak@clinic.org" in revs_raw.loc[0, "reason_for_change"]

    # 4. Verify all 9 standalone converters in imednet.utils.pandas mask PHI by default
    q_list = [Query(study_key=study_key, description="email=q@x.org")]
    assert MASKED_SENTINEL in queries_to_dataframe(q_list).loc[0, "description"]
    assert "q@x.org" in queries_to_dataframe(q_list, mask_phi=False).loc[0, "description"]

    v_list = [Variable(study_key=study_key, label="Contact email=v@x.org")]
    assert MASKED_SENTINEL in variables_to_dataframe(v_list).loc[0, "label"]
    assert "v@x.org" in variables_to_dataframe(v_list, mask_phi=False).loc[0, "label"]

    sub_list = [Subject(study_key=study_key, subject_key="SUBJ-001", site_name="email=s@x.org")]
    sub_df = subjects_to_dataframe(sub_list)
    assert sub_df.loc[0, "subject_key"] == "SUBJ-001"
    assert MASKED_SENTINEL in sub_df.loc[0, "site_name"]
    assert "s@x.org" in subjects_to_dataframe(sub_list, mask_phi=False).loc[0, "site_name"]

    f_list = [Form(study_key=study_key, form_name="Form email=f@x.org")]
    assert MASKED_SENTINEL in forms_to_dataframe(f_list).loc[0, "form_name"]
    assert "f@x.org" in forms_to_dataframe(f_list, mask_phi=False).loc[0, "form_name"]

    vis_list = [Visit(study_key=study_key, subject_key="SUBJ-001", interval_name="email=vis@x.org")]
    assert MASKED_SENTINEL in visits_to_dataframe(vis_list).loc[0, "interval_name"]
    assert "vis@x.org" in visits_to_dataframe(vis_list, mask_phi=False).loc[0, "interval_name"]

    int_list = [Interval(study_key=study_key, interval_name="email=int@x.org")]
    assert MASKED_SENTINEL in intervals_to_dataframe(int_list).loc[0, "interval_name"]
    assert "int@x.org" in intervals_to_dataframe(int_list, mask_phi=False).loc[0, "interval_name"]

    site_list = [Site(study_key=study_key, site_name="email=site@x.org")]
    assert MASKED_SENTINEL in sites_to_dataframe(site_list).loc[0, "site_name"]
    assert "site@x.org" in sites_to_dataframe(site_list, mask_phi=False).loc[0, "site_name"]

    user_list = [User(user_id="u1", email="u1@x.org")]
    assert users_to_dataframe(user_list).loc[0, "email"] == MASKED_SENTINEL
    assert users_to_dataframe(user_list, mask_phi=False).loc[0, "email"] == "u1@x.org"

    rec_list = [
        Record(
            study_key=study_key,
            record_id=9001,
            subject_key="SUBJ-001",
            record_data={"patientName": "Alice", "SYS_BP": 120},
        )
    ]
    rec_df = records_to_dataframe(rec_list, flatten=True)
    assert rec_df.loc[0, "patientName"] == MASKED_SENTINEL
    assert rec_df.loc[0, "SYS_BP"] == 120


@pytest.mark.asyncio
async def test_multi_origin_study_configuration_phi_isolation_without_record_study_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify unscoped records.to_dataframe and export_records_csv isolate PHI rules per origin."""
    db_path = tmp_path / "cfg_store" / "config_versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "IMEDNET_CONFIG_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)

    study_key = "SHARED_STUDY"

    # Upstream payload intentionally omits studyKey on Record items!
    records_payload = {
        "metadata": {"status": "OK"},
        "pagination": {"currentPage": 1, "size": 1, "totalPages": 1, "totalElements": 1},
        "data": [
            {
                "recordId": 77,
                "subjectKey": "SUBJ-77",
                "formKey": "VITALS",
                "recordData": {
                    "originAMrn": "ALPHA-PHI-VALUE",
                    "originBChartId": "BETA-PHI-VALUE",
                    "patientName": "Default Phi Name",
                },
            }
        ],
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=records_payload)

    async def _async_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=records_payload)

    sdk_a = ImednetSDK(
        api_key="key-a",  # pragma: allowlist secret
        security_key="sec-origin-a",  # pragma: allowlist secret
        base_url="https://origin-a.imednet.example",
        transport=httpx.MockTransport(_handler),
    )
    sdk_b = ImednetSDK(
        api_key="key-b",  # pragma: allowlist secret
        security_key="sec-origin-b",  # pragma: allowlist secret
        base_url="https://origin-b.imednet.example",
        transport=httpx.MockTransport(_handler),
    )
    async_sdk_a = AsyncImednetSDK(
        api_key="key-a",  # pragma: allowlist secret
        security_key="sec-origin-a",  # pragma: allowlist secret
        base_url="https://origin-a.imednet.example",
        transport=httpx.MockTransport(_async_handler),
    )
    async_sdk_b = AsyncImednetSDK(
        api_key="key-b",  # pragma: allowlist secret
        security_key="sec-origin-b",  # pragma: allowlist secret
        base_url="https://origin-b.imednet.example",
        transport=httpx.MockTransport(_async_handler),
    )

    ConfigVersionStore().commit_config(
        study_key,
        StudyConfiguration(studyKey=study_key, phiFields=["originAMrn"]),
        "Origin A PHI rules",
        sdk=sdk_a,
    )
    ConfigVersionStore().commit_config(
        study_key,
        StudyConfiguration(studyKey=study_key, phiFields=["originBChartId"]),
        "Origin B PHI rules",
        sdk=sdk_b,
    )

    # Clear ambient contexts so unscoped calls must bind origin governance themselves
    b_tok = set_base_url_context(None)
    s_tok = set_security_key_context(None)
    st_tok = set_study_context(None)
    try:
        df_a = sdk_a.records.to_dataframe(study_key, flatten=True)
        assert df_a.loc[0, "originAMrn"] == MASKED_SENTINEL
        assert df_a.loc[0, "originBChartId"] == "BETA-PHI-VALUE"
        assert df_a.loc[0, "patientName"] == MASKED_SENTINEL

        df_b = sdk_b.records.to_dataframe(study_key, flatten=True)
        assert df_b.loc[0, "originAMrn"] == "ALPHA-PHI-VALUE"
        assert df_b.loc[0, "originBChartId"] == MASKED_SENTINEL
        assert df_b.loc[0, "patientName"] == MASKED_SENTINEL

        adf_a = await async_sdk_a.records.to_dataframe(study_key, flatten=True)
        assert adf_a.loc[0, "originAMrn"] == MASKED_SENTINEL
        assert adf_a.loc[0, "originBChartId"] == "BETA-PHI-VALUE"

        adf_b = await async_sdk_b.records.to_dataframe(study_key, flatten=True)
        assert adf_a is not adf_b
        assert adf_b.loc[0, "originAMrn"] == "ALPHA-PHI-VALUE"
        assert adf_b.loc[0, "originBChartId"] == MASKED_SENTINEL

        csv_a = tmp_path / "origin_a.csv"
        csv_b = tmp_path / "origin_b.csv"
        export_records_csv(sdk_a, study_key, csv_a)
        export_records_csv(sdk_b, study_key, csv_b)

        csv_a_text = csv_a.read_text(encoding="utf-8")
        assert "ALPHA-PHI-VALUE" not in csv_a_text
        assert "BETA-PHI-VALUE" in csv_a_text
        assert "Default Phi Name" not in csv_a_text

        csv_b_text = csv_b.read_text(encoding="utf-8")
        assert "BETA-PHI-VALUE" not in csv_b_text
        assert "ALPHA-PHI-VALUE" in csv_b_text
        assert "Default Phi Name" not in csv_b_text

        # Unconfigured study fails closed to global_sensitivity_registry
        df_unconfigured = sdk_a.records.to_dataframe("UNCONFIGURED_STUDY", flatten=True)
        assert df_unconfigured.loc[0, "patientName"] == MASKED_SENTINEL
        assert df_unconfigured.loc[0, "originAMrn"] == "ALPHA-PHI-VALUE"
    finally:
        reset_study_context(st_tok)
        reset_security_key_context(s_tok)
        reset_base_url_context(b_tok)
