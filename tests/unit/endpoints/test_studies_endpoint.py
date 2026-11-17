"""Unit tests for studies endpoint."""

import pytest

from imednet.endpoints import studies
from imednet.errors import NotFoundError
from imednet.models.studies import Study


def test_list_builds_path_and_filters(
    monkeypatch, dummy_client, paginator_factory, patch_build_filter
):
    """Test that list builds path and filters."""
    ep = studies.StudiesEndpoint(dummy_client)
    captured = paginator_factory(studies, [{"studyKey": "S1"}])
    filter_capture = patch_build_filter(studies)

    result = ep.list(status="active")

    assert captured["path"] == "/api/v1/edc/studies"
    assert captured["params"] == {"filter": "FILTERED"}
    assert filter_capture["filters"] == {"status": "active"}
    assert isinstance(result[0], Study)


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
