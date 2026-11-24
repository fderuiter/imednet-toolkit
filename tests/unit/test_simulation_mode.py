"""Tests for built-in simulation transport and simulation mode."""

import os
from unittest.mock import patch

import httpx
import pytest

from imednet import AsyncImednetSDK, ImednetSDK
from imednet.config import load_config
from imednet.errors import BadRequestError, NotFoundError, ValidationError
from imednet.models.base import ApiResponse
from imednet.models.jobs import JobState, JobStatus
from imednet.models.record_revisions import validate_record_revision_chain
from imednet.models.records import (
    CreateNewRecordRequest,
    Record,
    RecordData,
    RegisterSubjectRequest,
    UpdateScheduledRecordRequest,
)
from imednet.models.subjects import SubjectStatus
from imednet.simulation import (
    SimulationState,
    create_simulation_transport,
    reset_default_simulation_state,
)
from imednet.utils.job_poller import AsyncJobPoller, JobFailedError, JobPoller


@pytest.fixture(autouse=True)
def _clean_simulation_state():
    """Ensure each test starts with clean default simulation state."""
    reset_default_simulation_state()
    yield
    reset_default_simulation_state()


def test_simulation_mode_initialization_without_credentials():
    """Verify SDK initializes with simulation_mode=True without any credentials or env vars."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)
        assert sdk.config.simulation_mode is True
        assert sdk.config.api_key == "simulated_api_key"
        assert sdk.config.security_key == "simulated_security_key"


def test_load_config_simulation_mode_env_var():
    """Verify load_config picks up IMEDNET_SIMULATION_MODE environment variable."""
    with patch.dict(os.environ, {"IMEDNET_SIMULATION_MODE": "true"}, clear=True):
        cfg = load_config()
        assert cfg.simulation_mode is True
        assert cfg.api_key == "simulated_api_key"
        assert cfg.security_key == "simulated_security_key"

    with patch.dict(os.environ, {"IMEDNET_SIMULATION_MODE": "1"}, clear=True):
        cfg = load_config()
        assert cfg.simulation_mode is True


def test_load_config_simulation_mode_false_requires_credentials():
    """Verify load_config fails without credentials when simulation_mode is False."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(
            ValueError, match="Either OIDC token or both API key and security key are required"
        ):
            load_config()


def test_simulation_state_fixtures():
    """Verify SimulationState contains pre-populated contract-compliant fixtures."""
    state = SimulationState()
    assert len(state.studies) >= 2
    assert len(state.sites) >= 2
    assert len(state.subjects) >= 2
    assert len(state.forms) >= 2
    assert len(state.visits) >= 2
    assert len(state.records) >= 2
    assert len(state.codings) >= 2
    assert len(state.record_revisions) >= 2
    assert len(state.queries) >= 2
    assert len(state.jobs) >= 2
    assert len(state.intervals) >= 1
    assert len(state.variables) >= 2
    assert len(state.users) >= 2

    for user in state.users:
        assert "userName" not in user
        assert {"login", "firstName", "lastName", "userActiveInStudy", "roles"} <= set(user.keys())
    assert any(u["userActiveInStudy"] is True for u in state.users)
    assert any(u["userActiveInStudy"] is False for u in state.users)


def test_simulation_studies_list_and_get():
    """Verify sdk.studies.list and get work in simulation mode."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)
        studies = list(sdk.studies.list())
        assert len(studies) > 0
        first_study = studies[0]
        assert first_study.study_key in ["DEMO-STUDY", "MY_STUDY", "STUDY"]

        retrieved = sdk.studies.get(study_key="STUDY", item_id="STUDY")
        assert retrieved.study_key == "STUDY"
        assert retrieved.study_name == "Benchmark Validation Study"

        direct_retrieved = sdk.studies.get("STUDY")
        assert direct_retrieved.study_key == "STUDY"
        assert direct_retrieved.study_name == "Benchmark Validation Study"

        with pytest.raises(NotFoundError):
            sdk.studies.get("NONEXISTENT")


def test_simulation_sites_list_and_get():
    """Verify sdk.sites operations in simulation mode."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)
        sites = list(sdk.sites.list(study_key="STUDY"))
        assert len(sites) >= 1
        site = sdk.sites.get(study_key="STUDY", item_id=101)
        assert site.site_id == 101
        assert site.site_name == "SITE"


def test_simulation_subjects_list_and_get():
    """Verify sdk.subjects operations in simulation mode."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)
        subjects = list(sdk.subjects.list(study_key="STUDY"))
        assert len(subjects) >= 1
        assert subjects[0].subject_id is not None
        subject = sdk.subjects.get(study_key="STUDY", item_id=subjects[0].subject_id)
        assert subject.subject_id == subjects[0].subject_id
        assert subject.site_id == 101

        or_filtered = list(sdk.subjects.list(study_key="STUDY", subjectKey=["SUB1", "SUB2"]))
        assert len(or_filtered) >= 1


def test_simulation_forms_and_visits_list():
    """Verify sdk.forms and sdk.visits operations in simulation mode."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)
        forms = list(sdk.forms.list(study_key="STUDY"))
        assert len(forms) >= 2
        visits = list(sdk.visits.list(study_key="STUDY"))
        assert len(visits) >= 2


def test_simulation_codings_and_record_revisions_routes():
    """Verify codings list and recordRevisions list/get routes work in simulation mode."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)

        codings = list(sdk.codings.list(study_key="STUDY"))
        assert len(codings) == 2
        meddra_list = list(sdk.codings.list(study_key="STUDY", code="C16576"))
        assert len(meddra_list) == 1
        meddra = meddra_list[0]
        assert meddra.coding_id == 1101
        assert meddra.dictionary_name == "MedDRA"
        assert meddra.code == "C16576"

        fallback_codings = list(sdk.codings.list(study_key="DEMO-STUDY"))
        assert len(fallback_codings) == 2
        assert all(c.study_key == "DEMO-STUDY" for c in fallback_codings)

        revisions = list(sdk.record_revisions.list(study_key="STUDY"))
        assert len(revisions) == 3
        rev = sdk.record_revisions.get(study_key="STUDY", item_id=1201)
        assert rev.record_revision_id == 1201
        assert rev.record_id == 601
        assert rev.record_revision == 1


def test_simulation_records_create_returns_202_and_appends_revisions():
    """Verify POST /records returns HTTP 202 and appends audit RecordRevisions."""
    state = SimulationState()
    transport = create_simulation_transport(state)
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(transport=transport, simulation_mode=True)
        initial_records = list(sdk.records.list(study_key="STUDY"))
        initial_revisions = list(sdk.record_revisions.list(study_key="STUDY"))

        new_record_data = [
            {
                "subjectKey": "SUB1",
                "formKey": "F_DM",
                "recordData": {"V_AGE": 50, "V_GENDER": "Female"},
            },
            {
                "subjectKey": "SUB1",
                "formKey": "F_VS",
                "data": {"V_WEIGHT": 72.0},
            },
        ]

        raw_resp = httpx.Client(transport=transport, base_url="https://sim.local").post(
            "/api/v1/edc/studies/STUDY/records",
            json=[{"subjectKey": "SUB1", "formKey": "F_DM", "data": {}}],
        )
        assert raw_resp.status_code == 202
        assert raw_resp.json()["jobId"].startswith("JOB-")

        response = sdk.records.create(study_key="STUDY", records_data=new_record_data)
        assert response is not None
        assert response.batch_id is not None
        assert response.state is not None

        after_records = list(sdk.records.list(study_key="STUDY"))
        after_revisions = list(sdk.record_revisions.list(study_key="STUDY"))
        assert len(after_records) == len(initial_records) + 3
        assert len(after_revisions) == len(initial_revisions) + 3

        created_ids = {r.record_id for r in after_records} - {r.record_id for r in initial_records}
        revision_rec_ids = {rev.record_id for rev in after_revisions}
        assert created_ids <= revision_rec_ids


def test_simulation_users_contract_fields_and_include_inactive():
    """Verify users endpoint emits contract fields and filters inactive users by default."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)

        active_users = list(sdk.users.list(study_key="STUDY"))
        assert len(active_users) == 2
        assert all(u.user_active_in_study is True for u in active_users)
        logins = {u.login for u in active_users}
        assert logins == {"sim_user", "sim_admin"}
        sim_user = next(u for u in active_users if u.login == "sim_user")
        assert sim_user.first_name == "Simulated"
        assert sim_user.last_name == "User"
        assert sim_user.name == "Simulated User"
        assert isinstance(sim_user.roles, list) and len(sim_user.roles) == 1

        all_users = list(sdk.users.list(study_key="STUDY", include_inactive=True))
        assert len(all_users) == 3
        assert {u.login for u in all_users} == {"sim_user", "sim_admin", "sim_inactive"}
        inactive_user = next(u for u in all_users if u.login == "sim_inactive")
        assert inactive_user.user_active_in_study is False


def test_simulation_strict_mode_zero_additive_drift():
    """Verify updated simulation contract routes pass IMEDNET_STRICT_MODE=true without drift."""
    with patch.dict(os.environ, {"IMEDNET_STRICT_MODE": "true"}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)
        assert len(list(sdk.studies.list())) == 3
        assert len(list(sdk.sites.list(study_key="STUDY"))) >= 1
        assert len(list(sdk.subjects.list(study_key="STUDY"))) >= 1
        assert len(list(sdk.forms.list(study_key="STUDY"))) == 3
        assert len(list(sdk.visits.list(study_key="STUDY"))) == 2
        assert len(list(sdk.records.list(study_key="STUDY"))) == 3
        assert len(list(sdk.record_revisions.list(study_key="STUDY"))) == 3
        assert len(list(sdk.codings.list(study_key="STUDY"))) == 2
        assert len(list(sdk.queries.list(study_key="STUDY"))) == 2
        assert len(list(sdk.intervals.list(study_key="STUDY"))) == 1
        assert len(list(sdk.users.list(study_key="STUDY", include_inactive=True))) == 3


def test_simulation_filter_operators_quotes_and_connectors():
    """Verify <, <=, >, >=, ==, !=, =~, quoted strings, AND (;), and OR (,) filters."""
    state = SimulationState()
    transport = create_simulation_transport(state)
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(transport=transport, simulation_mode=True)
        client = httpx.Client(transport=transport, base_url="https://sim.local")

        # Quoted string containing whitespace via SDK filter builder
        north_sites = list(sdk.sites.list(study_key="DEMO-STUDY", site_name="North Site"))
        assert len(north_sites) == 1
        assert north_sites[0].site_id == 102

        # Comparison operators (<, <=, >, >=, !=) on numeric formId
        gt_forms = list(sdk.forms.list(study_key="STUDY", form_id=(">", 301)))
        assert [f.form_key for f in gt_forms] == ["F_VS", "F_AE"]

        ge_lt_forms = list(sdk.forms.list(study_key="STUDY", form_id=[(">=", 301), ("<", 302)]))
        assert [f.form_key for f in ge_lt_forms] == ["F_DM"]

        le_forms = list(sdk.forms.list(study_key="STUDY", form_id=("<=", 301)))
        assert [f.form_key for f in le_forms] == ["F_DM"]

        ne_forms = list(sdk.forms.list(study_key="STUDY", form_id=("!=", 301)))
        assert [f.form_key for f in ne_forms] == ["F_VS", "F_AE"]

        # Regex / contains operator (=~) via HTTP transport filter
        regex_resp = client.get(
            "/api/v1/edc/studies/STUDY/codings",
            params={"filter": "dictionaryName=~^Med"},
        )
        assert regex_resp.status_code == 200
        assert [c["codingId"] for c in regex_resp.json()["data"]] == [1101]

        # OR connector (,) via list of values on PopStudyKeyStrategy endpoint
        or_codings = list(sdk.codings.list(study_key="STUDY", code=["C16576", "W001"]))
        assert {c.coding_id for c in or_codings} == {1101, 1102}

        # ISO timestamp and YYYY-MM-DD comparisons without exceptions
        later_records = list(
            sdk.records.list(study_key="STUDY", date_created=(">", "2026-01-10T09:32:00Z"))
        )
        assert [r.record_id for r in later_records] == [602]

        jan_visits = list(sdk.visits.list(study_key="STUDY", visit_date=("<", "2026-02-01")))
        assert [v.visit_id for v in jan_visits] == [401]

        # Quoted strings containing commas/semicolons and escaped quotes via HTTP query
        state.queries[0]["description"] = 'Verify "age", urgent; check'
        q_resp = client.get(
            "/api/v1/edc/studies/STUDY/queries",
            params={"filter": r'description=="Verify \"age\", urgent; check";variable==V_AGE'},
        )
        assert q_resp.status_code == 200
        assert [q["annotationId"] for q in q_resp.json()["data"]] == [701]
        assert "status" not in q_resp.json()["data"][0]

        # Keyword 'or' connector via raw HTTP
        or_resp = client.get(
            "/api/v1/edc/studies/STUDY/forms",
            params={"filter": "formKey==F_DM or formKey==F_VS"},
        )
        assert or_resp.status_code == 200
        assert len(or_resp.json()["data"]) == 2

        # Dotted recordData.<key> filter lookup on pinned SimulationState via SimulationTransport
        dotted_age_resp = client.get(
            "/api/v1/edc/studies/STUDY/records",
            params={"filter": "recordData.V_AGE>40"},
        )
        assert dotted_age_resp.status_code == 200
        dotted_age_body = dotted_age_resp.json()
        assert dotted_age_body["pagination"]["totalElements"] == 1
        assert [r["recordId"] for r in dotted_age_body["data"]] == [601]
        assert dotted_age_body["data"][0]["recordData"]["V_AGE"] == 45

        # Dotted snake_case record_data.<key>, != missing-key skip, OR across dotted keys, and SDK kwargs
        dotted_snake_resp = client.get(
            "/api/v1/edc/studies/STUDY/records",
            params={"filter": "record_data.V_WEIGHT>=68.5"},
        )
        assert dotted_snake_resp.status_code == 200
        assert [r["recordId"] for r in dotted_snake_resp.json()["data"]] == [602]

        dotted_ne_resp = client.get(
            "/api/v1/edc/studies/STUDY/records",
            params={"filter": "recordData.V_AGE!=50"},
        )
        assert dotted_ne_resp.status_code == 200
        assert [r["recordId"] for r in dotted_ne_resp.json()["data"]] == [601]

        dotted_or_resp = client.get(
            "/api/v1/edc/studies/STUDY/records",
            params={"filter": "recordData.V_AGE>40,recordData.V_WEIGHT>60"},
        )
        assert dotted_or_resp.status_code == 200
        assert [r["recordId"] for r in dotted_or_resp.json()["data"]] == [601, 602]


def test_simulation_record_data_filter_and_missing_keys():
    """Verify recordDataFilter operators, missing-key skipping, and 'none' projection."""
    state = SimulationState()
    state.records.append(
        {
            "recordId": 603,
            "studyKey": "STUDY",
            "subjectKey": "SUB1",
            "subjectId": 201,
            "formId": 302,
            "formKey": "F_VS",
            "visitId": 402,
            "intervalId": 502,
            "siteId": 101,
            "recordType": "Standard",
            "recordStatus": "Complete",
            "recordData": {"V_WEIGHT": 82.0, "V_NOTE": "Follow-up, fasting", "V_EMPTY": None},
            "deleted": False,
            "dateCreated": "2026-02-10T09:35:00Z",
            "dateModified": "2026-02-10T09:35:00Z",
            "lastUpdated": "2026-02-10T09:35:00Z",
        }
    )
    transport = create_simulation_transport(state)
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(transport=transport, simulation_mode=True)

        # Numeric > and <= on V_WEIGHT skips record 601 (which only has V_AGE/V_GENDER)
        heavy = list(sdk.records.list(study_key="STUDY", record_data_filter="V_WEIGHT>70"))
        assert [r.record_id for r in heavy] == [603]

        mid_ge = list(sdk.records.list(study_key="STUDY", record_data_filter="V_WEIGHT>=68.5"))
        assert [r.record_id for r in mid_ge] == [602, 603]
        mid_lt = list(sdk.records.list(study_key="STUDY", record_data_filter="V_WEIGHT<80"))
        assert [r.record_id for r in mid_lt] == [602]

        # != skips records missing V_WEIGHT rather than falsely matching them
        not_heavy = list(sdk.records.list(study_key="STUDY", record_data_filter="V_WEIGHT!=82"))
        assert [r.record_id for r in not_heavy] == [602]

        # Quoted value with comma and =~ regex on recordDataFilter
        quoted_match = list(
            sdk.records.list(
                study_key="STUDY", record_data_filter='V_NOTE=="Follow-up, fasting";V_WEIGHT>80'
            )
        )
        assert [r.record_id for r in quoted_match] == [603]

        regex_match = list(sdk.records.list(study_key="STUDY", record_data_filter="V_GENDER=~^Fem"))
        assert [r.record_id for r in regex_match] == [601]

        # OR connector on recordDataFilter
        or_records = list(
            sdk.records.list(study_key="STUDY", record_data_filter="V_AGE==45,V_WEIGHT>80")
        )
        assert [r.record_id for r in or_records] == [601, 603]

        # None value inside recordData does not raise on ordering comparison
        none_cmp = list(sdk.records.list(study_key="STUDY", record_data_filter="V_EMPTY>0"))
        assert none_cmp == []

        # record_data_filter="none" projects records with wire recordData=None (normalized to {} on Record)
        projected = list(sdk.records.list(study_key="STUDY", record_data_filter="none"))
        assert len(projected) == 4
        assert all(r.record_data == {} for r in projected)

        client = httpx.Client(transport=transport, base_url="https://sim.local")
        sorted_proj_resp = client.get(
            "/api/v1/edc/studies/STUDY/records",
            params={"recordDataFilter": "none", "sort": "recordData.V_WEIGHT,desc"},
        )
        assert sorted_proj_resp.status_code == 200
        assert [r["recordId"] for r in sorted_proj_resp.json()["data"]] == [603, 602, 601, 600]
        assert all(r["recordData"] is None for r in sorted_proj_resp.json()["data"])


def test_simulation_sort_and_pagination_metadata_envelopes():
    """Verify sort application and contract metadata/pagination keys."""
    state = SimulationState()
    transport = create_simulation_transport(state)
    client = httpx.Client(transport=transport, base_url="https://sim.local")

    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(transport=transport, simulation_mode=True)

        # Users sort via SDK
        users_asc = list(sdk.users.list(study_key="STUDY", sort="lastName,asc"))
        assert [u.last_name for u in users_asc] == ["Admin", "User"]
        users_desc = list(sdk.users.list(study_key="STUDY", sort="lastName,desc"))
        assert [u.last_name for u in users_desc] == ["User", "Admin"]

    # Direct HTTP check of envelope keys and multi-page pagination + sort
    resp_p0 = client.get(
        "/api/v1/edc/studies/STUDY/records",
        params={"page": "0", "size": "1", "sort": "recordId,desc"},
    )
    assert resp_p0.status_code == 200
    body_p0 = resp_p0.json()

    assert set(body_p0.keys()) == {"metadata", "pagination", "data"}
    assert body_p0["metadata"]["status"] == "OK"
    assert body_p0["metadata"]["method"] == "GET"
    assert body_p0["metadata"]["path"] == "/api/v1/edc/studies/STUDY/records"
    assert "timestamp" in body_p0["metadata"]

    pagination_p0 = body_p0["pagination"]
    assert "page" not in pagination_p0
    assert "totalItems" not in pagination_p0
    assert pagination_p0["currentPage"] == 0
    assert pagination_p0["size"] == 1
    assert pagination_p0["totalPages"] == 3
    assert pagination_p0["totalElements"] == 3
    assert pagination_p0["sort"] == [{"property": "recordId", "direction": "DESC"}]
    assert [r["recordId"] for r in body_p0["data"]] == [602]

    # Validate full envelope against ApiResponse[list[Record]]
    parsed_env = ApiResponse[list[Record]].model_validate(body_p0)
    assert parsed_env.pagination is not None
    assert parsed_env.pagination.current_page == 0
    assert parsed_env.pagination.total_elements == 3
    assert parsed_env.pagination.total_pages == 3
    assert parsed_env.pagination.sort[0].property == "recordId"
    assert parsed_env.pagination.sort[0].direction == "DESC"

    # Page 1
    resp_p1 = client.get(
        "/api/v1/edc/studies/STUDY/records",
        params={"page": "1", "size": "1", "sort": "recordId,desc"},
    )
    body_p1 = resp_p1.json()
    assert body_p1["pagination"]["currentPage"] == 1
    assert [r["recordId"] for r in body_p1["data"]] == [601]


def test_simulation_invalid_filters_and_regex_return_400():
    """Verify mixed AND/OR connectors, unclosed quotes, and invalid regex return HTTP 400."""
    transport = create_simulation_transport(SimulationState())
    client = httpx.Client(transport=transport, base_url="https://sim.local")

    # Mixed ';' and ',' connectors
    mixed_resp = client.get(
        "/api/v1/edc/studies/STUDY/records",
        params={"filter": "formKey==F_DM;recordId==601,recordId==602"},
    )
    assert mixed_resp.status_code == 400
    assert mixed_resp.json()["metadata"]["status"] == "BAD_REQUEST"

    # Invalid regex syntax in filter and recordDataFilter
    bad_regex_resp = client.get(
        "/api/v1/edc/studies/STUDY/codings",
        params={"filter": "dictionaryName=~[unclosed"},
    )
    assert bad_regex_resp.status_code == 400

    # SDK surfaces ValidationError / BadRequestError on invalid regex or invalid sort
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(transport=transport, simulation_mode=True)
        with pytest.raises((BadRequestError, ValidationError)):
            list(sdk.records.list(study_key="STUDY", record_data_filter="V_GENDER=~[unclosed"))
        with pytest.raises((BadRequestError, ValidationError)):
            list(sdk.users.list(study_key="STUDY", sort="lastName,sideways"))


def test_simulation_job_polling_progression():
    """Verify job status progresses from running to complete when polled."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)
        job1 = sdk.jobs.get(study_key="STUDY", item_id="JOB-1002")
        assert job1.state in ("RUNNING", "RUNNING")
        job2 = sdk.jobs.get(study_key="STUDY", item_id="JOB-1002")
        assert job2.state in ("COMPLETED", "COMPLETE", "SUCCESS")
        assert job2.progress == 100


def test_simulation_queries_list():
    """Verify sdk.queries operations in simulation mode."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)
        queries = list(sdk.queries.list(study_key="STUDY"))
        assert len(queries) >= 2


def test_custom_simulation_transport_sharing():
    """Verify sharing simulation transport across clients shares simulation state."""
    transport = create_simulation_transport(SimulationState())
    sdk1 = ImednetSDK(transport=transport, simulation_mode=True)
    sdk2 = ImednetSDK(transport=transport, simulation_mode=True)

    sdk1.records.create(
        study_key="STUDY",
        records_data=[{"subjectKey": "SUB1", "formKey": "F_VS", "recordData": {"V_WEIGHT": 65}}],
    )

    records2 = list(sdk2.records.list(study_key="STUDY"))
    assert any(r.form_key == "F_VS" for r in records2)


@pytest.mark.asyncio
async def test_async_sdk_simulation_mode():
    """Verify AsyncImednetSDK works identically across new routes, filters, sort, and 400s."""
    state = SimulationState()
    transport = create_simulation_transport(state)
    with patch.dict(os.environ, {}, clear=True):
        async with AsyncImednetSDK(transport=transport, simulation_mode=True) as sdk:
            studies = [s async for s in sdk.studies.list()]
            assert len(studies) > 0
            assert studies[0].study_key in ["DEMO-STUDY", "MY_STUDY", "STUDY"]

            retrieved = await sdk.studies.get("STUDY")
            assert retrieved.study_key == "STUDY"

            with pytest.raises(NotFoundError):
                await sdk.studies.get("NONEXISTENT")

            # Codings & Record Revisions async list
            codings = [c async for c in sdk.codings.list(study_key="STUDY")]
            assert len(codings) == 2
            whodrug = [c async for c in sdk.codings.list(study_key="STUDY", code="W001")]
            assert len(whodrug) == 1
            assert whodrug[0].coding_id == 1102
            assert whodrug[0].dictionary_name == "WHODrug"

            initial_revisions = [r async for r in sdk.record_revisions.list(study_key="STUDY")]
            assert len(initial_revisions) == 3

            # Record creation appends record and audit revision
            records = [r async for r in sdk.records.list(study_key="STUDY")]
            initial_count = len(records)

            created = await sdk.records.create(
                study_key="STUDY",
                records_data=[
                    {"subjectKey": "SUB1", "formKey": "F_VS", "recordData": {"V_WEIGHT": 75.5}}
                ],
            )
            assert created.batch_id is not None or created.state is not None

            after_records = [r async for r in sdk.records.list(study_key="STUDY")]
            after_revisions = [r async for r in sdk.record_revisions.list(study_key="STUDY")]
            assert len(after_records) == initial_count + 1
            assert len(after_revisions) == len(initial_revisions) + 1

            # Async filter operators & record_data_filter
            filtered_records = [
                r
                async for r in sdk.records.list(
                    study_key="STUDY", record_data_filter="V_WEIGHT>=70"
                )
            ]
            assert len(filtered_records) == 1
            assert filtered_records[0].record_data["V_WEIGHT"] == 75.5

            # Async users active filter & sort
            active_users = [
                u async for u in sdk.users.list(study_key="STUDY", sort="lastName,desc")
            ]
            assert [u.last_name for u in active_users] == ["User", "Admin"]
            all_users = [u async for u in sdk.users.list(study_key="STUDY", include_inactive=True)]
            assert len(all_users) == 3

            # Async invalid regex in record_data_filter surfaces error from simulation transport
            with pytest.raises((BadRequestError, ValidationError)):
                _ = [
                    r
                    async for r in sdk.records.list(
                        study_key="STUDY", record_data_filter="V_WEIGHT=~[invalid"
                    )
                ]

    async with httpx.AsyncClient(transport=transport, base_url="https://sim.local") as aclient:
        post_resp = await aclient.post(
            "/api/v1/edc/studies/STUDY/records",
            json=[{"subjectKey": "SUB1", "formKey": "F_DM", "data": {"V_AGE": 60}}],
        )
        assert post_resp.status_code == 202

        env_resp = await aclient.get(
            "/api/v1/edc/studies/STUDY/codings",
            params={"sort": "codingId,desc", "page": "0", "size": "10"},
        )
        assert env_resp.status_code == 200
        env_json = env_resp.json()
        assert env_json["metadata"]["status"] == "OK"
        assert env_json["pagination"]["currentPage"] == 0
        assert env_json["pagination"]["totalElements"] == 2
        assert env_json["pagination"]["sort"] == [{"property": "codingId", "direction": "DESC"}]
        assert [c["codingId"] for c in env_json["data"]] == [1102, 1101]

        bad_resp = await aclient.get(
            "/api/v1/edc/studies/STUDY/records",
            params={"filter": "formKey==F_DM;siteId==101,siteId==102"},
        )
        assert bad_resp.status_code == 400

        async_dotted_resp = await aclient.get(
            "/api/v1/edc/studies/STUDY/records",
            params={"filter": "recordData.V_AGE>40", "sort": "recordData.V_AGE,desc"},
        )
        assert async_dotted_resp.status_code == 200
        assert [r["recordData"]["V_AGE"] for r in async_dotted_resp.json()["data"]] == [60, 45]


def test_simulation_filter_sort_and_state_edge_cases():
    """Verify state getter fallbacks, malformed syntax 400s, and type/sort edge cases."""
    state = SimulationState()
    assert len(state.get_sites(None)) >= 3
    assert state.get_sites("UNKNOWN-STUDY")[0]["studyKey"] == "UNKNOWN-STUDY"
    assert len(state.get_subjects(None)) >= 2
    assert state.get_subjects("UNKNOWN-STUDY")[0]["studyKey"] == "UNKNOWN-STUDY"
    assert len(state.get_forms(None)) >= 2
    assert state.get_forms("UNKNOWN-STUDY")[0]["studyKey"] == "UNKNOWN-STUDY"
    assert len(state.get_visits(None)) >= 2
    assert state.get_visits("UNKNOWN-STUDY")[0]["studyKey"] == "UNKNOWN-STUDY"
    assert len(state.get_records(None)) >= 2
    assert len(state.get_queries(None)) >= 2
    assert state.get_queries("UNKNOWN-STUDY")[0]["studyKey"] == "UNKNOWN-STUDY"
    assert len(state.get_intervals(None)) >= 1
    assert len(state.get_variables(None)) >= 3
    assert len(state.get_codings(None)) >= 2
    assert len(state.get_record_revisions(None)) >= 2
    assert len(state.get_users(None)) >= 3
    assert state.get_job("STUDY", "AUTO-BATCH") is None
    assert state.get_job_results("STUDY", "AUTO-BATCH") is None

    transport = create_simulation_transport(state)
    client = httpx.Client(transport=transport, base_url="https://sim.local")
    assert client.get("/api/v1/edc/studies/STUDY/jobs/AUTO-BATCH").status_code == 404
    assert client.get("/api/v1/edc/studies/STUDY/jobs/AUTO-BATCH/results").status_code == 404
    assert client.get("/api/v1/edc/jobs/AUTO-BATCH").status_code == 404
    assert client.get("/api/v1/edc/jobs/AUTO-BATCH/results").status_code == 404

    # Boolean filter, numeric string ordering, timezone offset ISO datetime, and 'and' connector
    assert (
        client.get(
            "/api/v1/edc/studies/STUDY/records",
            params={"filter": "deleted==false and dateCreated>=2026-01-10T04:30:00-05:00"},
        ).status_code
        == 200
    )
    num_str_resp = client.get(
        "/api/v1/edc/studies/STUDY/codings",
        params={"filter": "value>60"},
    )
    assert [c["codingId"] for c in num_str_resp.json()["data"]] == [1102]

    # Filter 'none' bypass, list/dict field handling, non-bool/non-float inequality
    assert (
        client.get("/api/v1/edc/studies/STUDY/records", params={"filter": "none"}).status_code
        == 200
    )
    assert (
        len(
            client.get(
                "/api/v1/edc/studies/STUDY/subjects", params={"filter": "deleted!=invalid"}
            ).json()["data"]
        )
        >= 1
    )
    assert (
        len(
            client.get(
                "/api/v1/edc/studies/STUDY/subjects", params={"filter": "keywords!=empty"}
            ).json()["data"]
        )
        >= 1
    )
    assert (
        client.get(
            "/api/v1/edc/studies/STUDY/subjects", params={"filter": "keywords=~test"}
        ).json()["data"]
        == []
    )
    assert (
        len(
            client.get(
                "/api/v1/edc/studies/STUDY/forms", params={"filter": "formId!=notanumber"}
            ).json()["data"]
        )
        == 3
    )

    # Multi-field sort (;), colon direction (:), default ASC, ISO sort, and missing recordData sort
    sort_resp = client.get(
        "/api/v1/edc/studies/STUDY/records",
        params={"sort": "deleted;dateCreated:desc;V_AGE,asc", "page": "invalid", "size": "-5"},
    )
    assert sort_resp.status_code == 200
    assert [r["recordId"] for r in sort_resp.json()["data"]] == [602, 601, 600]

    # Empty result pagination, variables/jobs/direct-jobs/fallback routes, single-dict POST
    empty_resp = client.get(
        "/api/v1/edc/studies/STUDY/records", params={"filter": "recordId==999999"}
    )
    assert empty_resp.json()["pagination"]["totalPages"] == 0
    assert empty_resp.json()["data"] == []

    assert client.get("/api/v1/edc/studies/STUDY/variables").status_code == 200
    assert isinstance(client.get("/api/v1/edc/studies/STUDY/jobs").json(), list)
    assert client.get("/api/v1/edc/jobs/BATCH").json()["batchId"] == "BATCH"
    assert client.get("/api/v1/edc/unknownResource").json()["data"] == []
    post_job_resp = client.post(
        "/api/v1/edc/studies/STUDY/records", json={"subjectKey": "SUB1", "V_AGE": 40}
    )
    assert post_job_resp.status_code == 202
    batch_id = post_job_resp.json()["batchId"]
    assert len(client.get(f"/api/v1/edc/studies/STUDY/jobs/{batch_id}/results").json()) == 1
    assert len(client.get(f"/api/v1/edc/jobs/{batch_id}/results").json()) == 1
    assert client.post("/api/v1/edc/studies/STUDY/records", content=b"{invalid").status_code == 202

    # Malformed filter & sort syntax -> HTTP 400
    bad_params = [
        {"filter": 'siteName=="unclosed'},
        {"filter": 'siteName=="trailing\\"'},
        {"filter": 'siteName=="mid"quote"'},
        {"filter": 'siteName==unquoted"quote'},
        {"filter": "siteName=="},
        {"filter": "formKey==F_DM;"},
        {"filter": "noOperatorHere"},
        {"filter": "formKey===F_DM"},
        {"filter": "formId<>301"},
        {"filter": "bad key==1"},
        {"filter": "recordData..V_AGE>40"},
        {"filter": "recordData.>40"},
        {"filter": ".V_AGE>40"},
        {"sort": ",asc"},
    ]
    for params in bad_params:
        resp = client.get("/api/v1/edc/studies/STUDY/forms", params=params)
        assert resp.status_code == 400, f"Expected 400 for {params}, got {resp.status_code}"

    # Non-dict intermediate segment and nested dotted dict lookup
    non_dict_dotted = client.get(
        "/api/v1/edc/studies/STUDY/records",
        params={"filter": "recordId.nested==601"},
    )
    assert non_dict_dotted.status_code == 200
    assert non_dict_dotted.json()["data"] == []

    state.records[0]["recordData"]["nested"] = {"severity": "MILD", "literal.key": "OK"}
    nested_dotted = client.get(
        "/api/v1/edc/studies/STUDY/records",
        params={"filter": "recordData.nested.severity==MILD;recordData.nested.literal.key==OK"},
    )
    assert nested_dotted.status_code == 200
    assert [r["recordId"] for r in nested_dotted.json()["data"]] == [601]


def test_simulation_filters_sorting_and_subresources():
    """Verify simulation transport handles operators, recordDataFilter, sorting, and limit."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(
            simulation_mode=True,
            transport=create_simulation_transport(SimulationState()),
        )

        codings_list = list(sdk.codings.list(study_key="STUDY"))
        assert len(codings_list) >= 1

        revisions_list = list(sdk.record_revisions.list(study_key="STUDY"))
        assert len(revisions_list) >= 1

        sorted_desc = list(sdk.records.list(study_key="STUDY", sort="recordId,desc"))
        assert len(sorted_desc) >= 2
        assert sorted_desc[0].record_id >= sorted_desc[-1].record_id

        sorted_str = list(sdk.records.list(study_key="STUDY", sort="formKey,asc"))
        assert len(sorted_str) >= 2

        no_data_records = list(
            sdk.records.list(study_key="STUDY", record_data_filter="none", limit=1)
        )
        assert len(no_data_records) == 1
        assert no_data_records[0].record_data == {}

        age_filtered = list(
            sdk.records.list(study_key="STUDY", recordDataFilter="V_AGE>=30;V_GENDER==Female")
        )
        assert len(age_filtered) == 1
        assert age_filtered[0].record_id == 601

        regex_filtered = list(
            sdk.records.list(study_key="STUDY", recordDataFilter='V_GENDER=~"^Fem"')
        )
        assert len(regex_filtered) == 1

        bool_filtered = list(sdk.records.list(study_key="STUDY", deleted=False))
        assert len(bool_filtered) >= 2


@pytest.mark.asyncio
async def test_offline_transport_initializes_without_credentials_and_matches_case_insensitive_regex():
    """ImednetSDK and AsyncImednetSDK accept transport without credentials and match =~ case-insensitively."""
    from imednet import AsyncImednetSDK, SimulationTransport

    with patch.dict(os.environ, {}, clear=True):
        with ImednetSDK(transport=SimulationTransport()) as sdk:
            matched = list(sdk.records.list("STUDY", record_data_filter="V_GENDER=~fem"))
            assert len(matched) >= 1
            assert any(r.record_data.get("V_GENDER") == "Female" for r in matched)

        async with AsyncImednetSDK(transport=SimulationTransport()) as async_sdk:
            async_matched = [
                r async for r in async_sdk.records.list("STUDY", record_data_filter="V_GENDER=~fem")
            ]
            assert len(async_matched) >= 1
            assert any(r.record_data.get("V_GENDER") == "Female" for r in async_matched)


@pytest.mark.asyncio
async def test_simulation_job_lifecycles_open_terminal_and_404():
    """Verify OPEN/active job transitions via JobPoller, terminal immutability, and 404 lookups."""
    state = SimulationState()
    state.jobs["JOB-OPEN"] = {
        "jobId": "JOB-OPEN",
        "batchId": "JOB-OPEN",
        "state": "OPEN",
        "progress": 0,
        "totalRecords": 3,
        "successfulRecords": 0,
        "failedRecords": 0,
        "results": {"status": "Open", "total": 3, "success": 0, "failed": 0},
        "dateCreated": "2026-02-01T10:00:00Z",
        "dateStarted": None,
        "dateFinished": None,
        "_internal_marker": "strip-me",
    }
    state.jobs["JOB-ASYNC-OPEN"] = {
        "jobId": "JOB-ASYNC-OPEN",
        "batchId": "JOB-ASYNC-OPEN",
        "state": JobState.OPEN,
        "progress": 10,
        "results": [
            {"recordId": 601, "status": "SUCCESS"},
            {"recordId": 602, "status": "SUCCESS"},
        ],
        "totalRecords": 2,
        "successfulRecords": 0,
        "failedRecords": 0,
        "dateCreated": "2026-02-01T11:00:00",
        "dateStarted": None,
        "dateFinished": None,
    }
    state.jobs["JOB-LEGACY-POLL"] = {
        "jobId": "JOB-LEGACY-POLL",
        "batchId": "JOB-LEGACY-POLL",
        "state": "QUEUED",
        "progress": 25,
        "dateCreated": "not-an-iso-timestamp",
        "_poll_count": 1,
    }
    for terminal in ("FAILED", "CANCELLED", "TIMEOUT"):
        batch_key = f"JOB-{terminal}"
        state.jobs[batch_key] = {
            "jobId": batch_key,
            "batchId": batch_key,
            "state": terminal,
            "progress": 100,
            "totalRecords": 1,
            "successfulRecords": 0,
            "failedRecords": 1,
            "resultError": f"Simulated {terminal}",
            "dateCreated": "2026-02-01T12:00:00Z",
            "dateStarted": "2026-02-01T12:00:01Z",
            "dateFinished": "2026-02-01T12:00:05Z",
        }

    transport = create_simulation_transport(state)
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(transport=transport, simulation_mode=True)

        # Poll JOB-OPEN through JobPoller: poll 1 -> OPEN, poll 2 -> COMPLETED
        observed_states: list[JobState | str | None] = []
        poller = JobPoller(sdk.jobs.get)
        completed_open = poller.run(
            "STUDY",
            "JOB-OPEN",
            interval=0.001,
            timeout=5.0,
            on_progress=lambda event: observed_states.append(event.status.state),
        )
        assert observed_states == [JobState.OPEN, JobState.COMPLETED]
        assert completed_open.state == JobState.COMPLETED
        assert completed_open.is_successful is True
        assert completed_open.progress == 100
        assert completed_open.date_started == "2026-02-01T10:00:00Z"
        assert completed_open.date_finished == "2026-02-01T10:00:05Z"
        assert completed_open.successful_records == 3
        assert completed_open.results == {
            "status": "Completed",
            "total": 3,
            "success": 3,
            "failed": 0,
        }
        assert state._job_poll_counts["JOB-OPEN"] == 2
        assert all(not k.startswith("_") for j in state.get_jobs("STUDY") for k in j)

        # Legacy _poll_count=1 migrates to _job_poll_counts and completes on next poll
        completed_legacy = sdk.poll_job("STUDY", "JOB-LEGACY-POLL", interval=0.001, timeout=5.0)
        assert completed_legacy.state == JobState.COMPLETED
        assert completed_legacy.date_finished == "2026-01-02T00:00:05Z"
        assert "_poll_count" not in state.jobs["JOB-LEGACY-POLL"]

        # Terminal FAILED, CANCELLED, and TIMEOUT jobs stay unchanged and raise JobFailedError
        for terminal in (JobState.FAILED, JobState.CANCELLED, JobState.TIMEOUT):
            batch_key = f"JOB-{terminal.value}"
            first_check = sdk.jobs.get("STUDY", batch_key)
            second_check = sdk.jobs.get("STUDY", batch_key)
            assert first_check.state == terminal
            assert second_check.state == terminal
            assert batch_key not in state._job_poll_counts
            with pytest.raises(JobFailedError):
                sdk.jobs.poll("STUDY", batch_key, interval=0.001, timeout=5.0)

        # Missing batch_id raises NotFoundError on sync lookups and pollers
        with pytest.raises(NotFoundError):
            sdk.jobs.get("STUDY", "UNKNOWN-BATCH-ID")
        with pytest.raises(NotFoundError):
            sdk.get_job("STUDY", "UNKNOWN-BATCH-ID")
        with pytest.raises(NotFoundError):
            sdk.poll_job("STUDY", "UNKNOWN-BATCH-ID", interval=0.001, timeout=5.0)

        # Async JobPoller + AsyncImednetSDK parity for OPEN progression and 404
        async with AsyncImednetSDK(transport=transport, simulation_mode=True) as async_sdk:
            async_observed: list[JobState | str | None] = []
            async_poller = AsyncJobPoller(async_sdk.jobs.get)
            async_done = await async_poller.run(
                "STUDY",
                "JOB-ASYNC-OPEN",
                interval=0.001,
                timeout=5.0,
                on_progress=lambda event: async_observed.append(event.status.state),
            )
            assert async_observed == [JobState.OPEN, JobState.COMPLETED]
            assert async_done.state == JobState.COMPLETED
            assert async_done.is_successful is True
            assert async_done.successful_records == 2
            assert async_done.failed_records == 0
            assert async_done.date_started == "2026-02-01T11:00:00"
            assert async_done.date_finished == "2026-02-01T11:00:05Z"

            with pytest.raises(NotFoundError):
                await async_sdk.jobs.get("STUDY", "MISSING-ASYNC-BATCH")
            with pytest.raises(NotFoundError):
                await async_sdk.async_poll_job(
                    "STUDY", "MISSING-ASYNC-BATCH", interval=0.001, timeout=5.0
                )


@pytest.mark.asyncio
async def test_simulation_strict_mode_validation():
    """Verify IMEDNET_STRICT_MODE=true passes across jobs, subjects, records, and revisions."""
    state = SimulationState()
    state.jobs["JOB-STRICT-OPEN"] = {
        "jobId": "JOB-STRICT-OPEN",
        "batchId": "JOB-STRICT-OPEN",
        "state": "OPEN",
        "progress": 0,
        "totalRecords": 1,
        "successfulRecords": 0,
        "failedRecords": 0,
        "results": {"status": "Open", "total": 1, "success": 0, "failed": 0},
        "dateCreated": "2026-03-01T00:00:00Z",
        "dateStarted": None,
        "dateFinished": None,
        "_private_note": "must-not-leak",
    }
    transport = create_simulation_transport(state)

    with patch.dict(os.environ, {"IMEDNET_STRICT_MODE": "true"}, clear=True):
        sdk = ImednetSDK(transport=transport, simulation_mode=True)

        # Seeded RUNNING -> COMPLETED and OPEN -> COMPLETED validate cleanly in strict mode
        polled_seeded = sdk.jobs.poll("STUDY", "JOB-1002", interval=0.001, timeout=5.0)
        assert polled_seeded.state == JobState.COMPLETED
        assert polled_seeded.date_finished == "2026-01-02T00:00:05Z"

        polled_open = sdk.jobs.poll("STUDY", "JOB-STRICT-OPEN", interval=0.001, timeout=5.0)
        assert polled_open.state == JobState.COMPLETED

        # Validate all get_jobs() entries against JobStatus in strict mode
        raw_jobs = state.get_jobs("STUDY")
        assert len(raw_jobs) >= 3
        for raw_job in raw_jobs:
            validated_job = JobStatus.model_validate(raw_job)
            assert validated_job.batch_id is not None

        # Create/register/update records under strict mode (validates Job + resultUrl polling)
        job = sdk.records.create(
            study_key="STUDY",
            records_data=[
                RegisterSubjectRequest(
                    formKey="F_DM",
                    siteName="SITE",
                    data=RecordData({"V_AGE": 41, "V_GENDER": "Female"}),
                ).model_dump(by_alias=True),
                UpdateScheduledRecordRequest(
                    subjectKey="SUB1",
                    formKey="F_VS",
                    intervalName="Baseline",
                    data=RecordData({"V_WEIGHT": 70.1}),
                ).model_dump(by_alias=True),
            ],
        )
        polled_batch = sdk.jobs.poll("STUDY", job.batch_id or "", interval=0.001, timeout=5.0)
        assert polled_batch.is_successful is True
        assert polled_batch.successful_records == 2

        subjects = list(sdk.subjects.list(study_key="STUDY"))
        records = list(sdk.records.list(study_key="STUDY"))
        revisions = list(sdk.record_revisions.list(study_key="STUDY"))
        assert len(subjects) == 2
        assert len(records) == 4
        assert len(revisions) == 5

        async with AsyncImednetSDK(transport=transport, simulation_mode=True) as async_sdk:
            async_subjects = [s async for s in async_sdk.subjects.list(study_key="STUDY")]
            async_records = [r async for r in async_sdk.records.list(study_key="STUDY")]
            async_revisions = [
                rev async for rev in async_sdk.record_revisions.list(study_key="STUDY")
            ]
            assert len(async_subjects) == 2
            assert len(async_records) == 4
            assert len(async_revisions) == 5


@pytest.mark.asyncio
async def test_simulation_subject_enrollment_scheduled_upsert_and_revisions():
    """Verify RegisterSubjectRequest, UpdateScheduledRecordRequest, CreateNewRecordRequest, and reset."""
    state = SimulationState()
    transport = create_simulation_transport(state)

    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(transport=transport, simulation_mode=True)

        # 1. RegisterSubjectRequest enrolls a new Subject and resolves siteId from self.sites
        reg_req = RegisterSubjectRequest(
            formKey="F_DM",
            siteName="North Site",
            data=RecordData({"V_AGE": 29, "V_GENDER": "Male"}),
        )
        sdk.records.create(
            study_key="DEMO-STUDY",
            records_data=[reg_req.model_dump(by_alias=True)],
        )
        demo_subjects = list(sdk.subjects.list(study_key="DEMO-STUDY"))
        assert len(demo_subjects) == 2
        enrolled = next(s for s in demo_subjects if s.subject_key != "SUBJ-001")
        assert enrolled.subject_id == 203
        assert enrolled.site_id == 102
        assert enrolled.site_name == "North Site"
        assert enrolled.subject_status == SubjectStatus.ENROLLED

        # 2. UpdateScheduledRecordRequest updates existing scheduled record (602, Baseline) in place
        before_study_records = list(sdk.records.list(study_key="STUDY"))
        assert len(before_study_records) == 3

        sched_update = UpdateScheduledRecordRequest(
            subjectKey="SUB1",
            formKey="F_VS",
            intervalName="Baseline",
            data=RecordData({"V_WEIGHT": 71.5}),
        )
        sdk.records.create(
            study_key="STUDY",
            records_data=[sched_update.model_dump(by_alias=True)],
        )
        after_sched_update = list(sdk.records.list(study_key="STUDY"))
        assert len(after_sched_update) == 3
        updated_602 = sdk.records.get(study_key="STUDY", item_id=602)
        assert updated_602.record_data["V_WEIGHT"] == 71.5
        assert updated_602.interval_id == 501
        assert updated_602.visit_id == 401

        # 3. UpdateScheduledRecordRequest targeting a valid Visit/Interval with no existing Record
        #    inserts a new scheduled Record + initial RecordRevision, then updates it in place
        followup_req = UpdateScheduledRecordRequest(
            subjectKey="SUB1",
            formKey="F_VS",
            intervalName="Follow-Up Month 1",
            data=RecordData({"V_WEIGHT": 69.2}),
        )
        sdk.records.create(
            study_key="STUDY",
            records_data=[followup_req.model_dump(by_alias=True)],
        )
        records_with_followup = list(sdk.records.list(study_key="STUDY"))
        assert len(records_with_followup) == 4
        followup_rec = next(r for r in records_with_followup if r.interval_id == 502)
        assert followup_rec.visit_id == 402
        assert followup_rec.subject_id == 201
        assert followup_rec.record_data["V_WEIGHT"] == 69.2

        # 4. Async client updates Follow-Up Month 1 in place and appends unscheduled records
        async with AsyncImednetSDK(transport=transport, simulation_mode=True) as async_sdk:
            followup_update_2 = UpdateScheduledRecordRequest(
                subjectKey="SUB1",
                formKey="F_VS",
                intervalName="Follow-Up Month 1",
                data=RecordData({"V_WEIGHT": 70.0}),
            )
            unsched_1 = CreateNewRecordRequest(
                subjectKey="SUB1",
                formKey="F_VS",
                data=RecordData({"V_WEIGHT": 73.1}),
            )
            unsched_2 = CreateNewRecordRequest(
                subjectKey="SUB1",
                formKey="F_VS",
                data=RecordData({"V_WEIGHT": 74.8}),
            )
            await async_sdk.records.create(
                study_key="STUDY",
                records_data=[
                    followup_update_2.model_dump(by_alias=True),
                    unsched_1.model_dump(by_alias=True),
                    unsched_2.model_dump(by_alias=True),
                ],
            )

            final_records = [r async for r in async_sdk.records.list(study_key="STUDY")]
            assert len(final_records) == 6
            unscheduled_records = [r for r in final_records if r.interval_id is None]
            assert len(unscheduled_records) == 2
            assert all(r.visit_id is None for r in unscheduled_records)
            assert [r.record_data["V_WEIGHT"] for r in unscheduled_records] == [73.1, 74.8]

            refreshed_followup = await async_sdk.records.get(
                study_key="STUDY", item_id=followup_rec.record_id
            )
            assert refreshed_followup.record_data["V_WEIGHT"] == 70.0

            # 5. RecordRevision history is study-scoped and monotonically chained per record
            study_revisions = [
                rev async for rev in async_sdk.record_revisions.list(study_key="STUDY")
            ]
            demo_revisions = [
                rev async for rev in async_sdk.record_revisions.list(study_key="DEMO-STUDY")
            ]
            assert len(demo_revisions) == 1
            assert demo_revisions[0].subject_id == 203
            validate_record_revision_chain(study_revisions)

            rev_602 = [r for r in study_revisions if r.record_id == 602]
            assert [r.record_revision for r in rev_602] == [1, 2]
            assert [r.data_revision for r in rev_602] == [1, 2]

            rev_followup = [r for r in study_revisions if r.record_id == followup_rec.record_id]
            assert [r.record_revision for r in rev_followup] == [1, 2]

        # 6. Record status transitions & soft-deleted terminal guard
        sdk.records.create(
            study_key="STUDY",
            records_data=[{"recordId": 602, "recordStatus": "Verified"}],
        )
        sdk.records.create(
            study_key="STUDY",
            records_data=[{"recordId": 602, "recordStatus": "DELETED"}],
        )
        deleted_602 = sdk.records.get(study_key="STUDY", item_id=602)
        assert deleted_602.is_deleted is True
        rev_602_after_status = [
            r for r in sdk.record_revisions.list(study_key="STUDY") if r.record_id == 602
        ]
        assert [r.record_revision for r in rev_602_after_status] == [1, 2, 3, 4]
        assert [r.data_revision for r in rev_602_after_status] == [1, 2, 2, 2]

        with pytest.raises(ValueError, match="Cannot transition soft-deleted Record"):
            state.create_records("STUDY", [{"recordId": 602, "recordStatus": "Complete"}])

        # 7. reset() restores clean counters, poll counts, and seeded record_revisions
        _ = state.get_job("STUDY", "JOB-1002")
        assert state._job_poll_counts
        state.reset()
        assert state._job_poll_counts == {}
        assert state._next_subject_id == 202
        assert state._next_revision_id == 1203
        assert len(state.record_revisions) == 3
