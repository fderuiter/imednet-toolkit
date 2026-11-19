"""Unit tests for studies endpoint."""

import pytest

from imednet.endpoints import studies
from imednet.errors import NotFoundError
from imednet.errors.validation import ConfigurationError
from imednet.models.studies import Study


def test_list_builds_path_and_filters(
    monkeypatch, dummy_client, paginator_factory, patch_build_filter
):
    """Test that list builds path and preserves documented filters including studyKey."""
    ep = studies.StudiesEndpoint(dummy_client)
    captured = paginator_factory(studies, [{"studyKey": "S1"}])
    filter_capture = patch_build_filter(studies)

    result = ep.list(study_type="LIVE", studyKey="S1")

    assert captured["path"] == "/api/v1/edc/studies"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"study_type": "LIVE", "studyKey": "S1"}
    assert isinstance(result[0], Study)

    with pytest.raises(ConfigurationError, match="status"):
        ep.list(status="active")


def test_get_single_arg_success(dummy_client, response_factory):
    """Test single-argument study retrieval targets /api/v1/edc/studies/{studyKey}."""
    ep = studies.StudiesEndpoint(dummy_client)
    dummy_client.get.return_value = response_factory({"studyKey": "S1", "studyName": "Demo Trial"})

    res = ep.get("S1")

    dummy_client.get.assert_called_once_with("/api/v1/edc/studies/S1")
    assert isinstance(res, Study)
    assert res.study_key == "S1"
    assert res.study_name == "Demo Trial"


def test_get_legacy_two_arg_success(dummy_client, response_factory):
    """Test legacy two-argument study retrieval (None, 'S1') targets direct route."""
    ep = studies.StudiesEndpoint(dummy_client)
    dummy_client.get.return_value = response_factory({"studyKey": "S1"})

    res = ep.get(None, "S1")

    dummy_client.get.assert_called_once_with("/api/v1/edc/studies/S1")
    assert isinstance(res, Study)
    assert res.study_key == "S1"


def test_get_keyword_arguments_success(dummy_client, response_factory):
    """Test keyword argument forms for backward compatibility."""
    ep = studies.StudiesEndpoint(dummy_client)
    dummy_client.get.return_value = response_factory({"studyKey": "S1"})

    res1 = ep.get(study_key="S1")
    assert res1.study_key == "S1"

    res2 = ep.get(study_key=None, item_id="S1")
    assert res2.study_key == "S1"

    res3 = ep.get(study_key="S1", item_id="S1")
    assert res3.study_key == "S1"

    res4 = ep.get(item_id="S1")
    assert res4.study_key == "S1"

    res5 = ep.get("S1", item_id="S1")
    assert res5.study_key == "S1"


def test_get_empty_payload_raises_not_found(dummy_client, response_factory):
    """Test that an empty response payload raises NotFoundError."""
    ep = studies.StudiesEndpoint(dummy_client)
    dummy_client.get.return_value = response_factory({})

    with pytest.raises(NotFoundError):
        ep.get("missing")


def test_get_404_raises_not_found(dummy_client):
    """Test that a 404 response raises NotFoundError."""
    ep = studies.StudiesEndpoint(dummy_client)
    dummy_client.get.side_effect = NotFoundError("Study missing not found", status_code=404)

    with pytest.raises(NotFoundError):
        ep.get("missing")


def test_get_no_args_raises_type_error(dummy_client):
    """Test calling get without arguments raises TypeError."""
    ep = studies.StudiesEndpoint(dummy_client)
    with pytest.raises(TypeError):
        ep.get()


def test_get_none_raises_type_error(dummy_client):
    """Test calling get with only None raises TypeError."""
    ep = studies.StudiesEndpoint(dummy_client)
    with pytest.raises(TypeError):
        ep.get(None)

    with pytest.raises(TypeError):
        ep.get(None, None)


def test_get_too_many_args_raises_type_error(dummy_client):
    """Test calling get with more than 2 arguments raises TypeError."""
    ep = studies.StudiesEndpoint(dummy_client)
    with pytest.raises(TypeError):
        ep.get("S1", "S2", "S3")


def test_list_each_call_makes_request(dummy_client, paginator_factory):
    """Test that list each call makes request."""
    ep = studies.StudiesEndpoint(dummy_client)
    captured = paginator_factory(studies, [{"studyKey": "S1"}])

    ep.list()
    ep.list()

    assert captured["count"] == 2


def test_studies_list_or_filter_inside_study_context_ignores_ambient_context():
    """Test sdk.studies.list with OR filter inside sdk.study_context ignores ambient studyKey."""
    from imednet import ImednetSDK
    from imednet.simulation import SimulationState, SimulationTransport

    transport = SimulationTransport(state=SimulationState())
    sdk = ImednetSDK(simulation_mode=True, transport=transport)

    with sdk.study_context("STUDY"):
        matched = list(sdk.studies.list(studyType=["Interventional", "Observational"]))
        assert {s.study_key for s in matched} == {"DEMO-STUDY", "MY_STUDY", "STUDY"}

        by_keys = sdk.get_studies(studyKey=["DEMO-STUDY", "STUDY"])
        assert {s.study_key for s in by_keys} == {"DEMO-STUDY", "STUDY"}

        df = sdk.studies.to_dataframe(study_key=["MY_STUDY", "STUDY"])
        assert set(df["study_key"]) == {"MY_STUDY", "STUDY"}

    with pytest.raises(ValueError, match="Duplicate filter attribute 'studyKey'"):
        list(sdk.studies.list("S1", studyKey="S2"))
