"""Unit tests for codings endpoint."""

import pytest

from imednet.endpoints import codings
from imednet.errors.validation import ConfigurationError
from imednet.models.codings import Coding


def test_list_requires_study_key(dummy_client, paginator_factory, patch_build_filter):
    """Test that list requires study key and uses documented filter attributes."""
    ep = codings.CodingsEndpoint(dummy_client)
    capture = paginator_factory(codings, [{"codingId": 1}])
    patch = patch_build_filter(codings)

    with pytest.raises(ConfigurationError):
        ep.list()

    result = ep.list(study_key="S1", dictionaryName="MedDRA")

    assert capture["path"] == "/api/v1/edc/studies/S1/codings"
    assert capture["params"] == {"filter": "FILTERED"}
    assert patch["filters"] == {"dictionaryName": "MedDRA"}
    assert isinstance(result[0], Coding)


def test_list_repeatable_coding_filters_transport(dummy_client, paginator_factory):
    """Test repeatable coding filters at parameter-building and transport boundary."""
    ep = codings.CodingsEndpoint(dummy_client)
    capture = paginator_factory(codings, [{"codingId": "C1", "dictionaryName": "MedDRA"}])

    result = ep.list(study_key="S1", dictionaryName="MedDRA", variable="AETERM")

    assert capture["path"] == "/api/v1/edc/studies/S1/codings"
    assert "dictionaryName==MedDRA" in capture["params"]["filter"]
    assert "variable==AETERM" in capture["params"]["filter"]
    assert "studyKey" not in capture["params"]["filter"]
    assert len(result) == 1
    assert result[0].coding_id == "C1"
    assert result[0].dictionary_name == "MedDRA"


def test_codings_endpoint_is_list_only_and_rejects_undocumented_filters(dummy_client):
    """Test that CodingsEndpoint exposes no get method and rejects codingId/status filters."""
    ep = codings.CodingsEndpoint(dummy_client)
    async_ep = codings.AsyncCodingsEndpoint(dummy_client)

    assert not hasattr(ep, "get")
    assert not hasattr(async_ep, "get")
    assert not hasattr(codings.CodingsOperationDef, "_id_param")

    with pytest.raises(ConfigurationError, match="codingId"):
        ep.list(study_key="S1", codingId=1)

    with pytest.raises(ConfigurationError, match="status"):
        ep.list(study_key="S1", status="open")
