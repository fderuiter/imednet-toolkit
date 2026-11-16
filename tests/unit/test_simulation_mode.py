"""Tests for built-in simulation transport and simulation mode."""

import os
from unittest.mock import patch

import pytest

from imednet import AsyncImednetSDK, ImednetSDK
from imednet.config import Config, load_config
from imednet.simulation import SimulationState, SimulationTransport, create_simulation_transport


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
    """Verify SimulationState contains pre-populated fixtures."""
    state = SimulationState()
    assert len(state.studies) >= 2
    assert len(state.sites) >= 2
    assert len(state.subjects) >= 2
    assert len(state.forms) >= 2
    assert len(state.visits) >= 2
    assert len(state.records) >= 2
    assert len(state.queries) >= 2
    assert len(state.jobs) >= 2
    assert len(state.intervals) >= 1
    assert len(state.variables) >= 2
    assert len(state.users) >= 2


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
        subject = sdk.subjects.get(study_key="STUDY", item_id="SUB1")
        assert subject.subject_key == "SUB1"
        assert subject.site_id == 101


def test_simulation_forms_and_visits_list():
    """Verify sdk.forms and sdk.visits operations in simulation mode."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)
        forms = list(sdk.forms.list(study_key="STUDY"))
        assert len(forms) >= 2
        visits = list(sdk.visits.list(study_key="STUDY"))
        assert len(visits) >= 2


def test_simulation_records_create_and_retrieve():
    """Verify stateful record creation and list retrieval in simulation mode."""
    with patch.dict(os.environ, {}, clear=True):
        sdk = ImednetSDK(simulation_mode=True)
        initial_records = list(sdk.records.list(study_key="STUDY"))
        initial_count = len(initial_records)

        new_record_data = [
            {
                "subjectKey": "SUB1",
                "formKey": "F_DM",
                "recordData": {"V_AGE": 50, "V_GENDER": "Female"},
            }
        ]

        response = sdk.records.create(study_key="STUDY", records_data=new_record_data)
        assert response is not None
        assert response.batch_id is not None or response.state is not None

        after_records = list(sdk.records.list(study_key="STUDY"))
        assert len(after_records) == initial_count + 1


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
    transport = create_simulation_transport()
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
    """Verify AsyncImednetSDK works identically in simulation mode."""
    with patch.dict(os.environ, {}, clear=True):
        async with AsyncImednetSDK(simulation_mode=True) as sdk:
            studies = [s async for s in sdk.studies.list()]
            assert len(studies) > 0
            assert studies[0].study_key in ["DEMO-STUDY", "MY_STUDY", "STUDY"]

            records = [r async for r in sdk.records.list(study_key="STUDY")]
            initial_count = len(records)

            created = await sdk.records.create(
                study_key="STUDY",
                records_data=[
                    {"subjectKey": "SUB1", "formKey": "F_VS", "recordData": {"V_WEIGHT": 70}}
                ],
            )
            assert created.batch_id is not None or created.state is not None

            after_records = [r async for r in sdk.records.list(study_key="STUDY")]
            assert len(after_records) == initial_count + 1

