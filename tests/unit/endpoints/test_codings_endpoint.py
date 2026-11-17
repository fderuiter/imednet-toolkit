"""Unit tests for codings endpoint."""

import pytest

from imednet.endpoints import codings
from imednet.errors import NotFoundError
from imednet.errors.validation import ConfigurationError
from imednet.models.codings import Coding


def test_list_requires_study_key(dummy_client, paginator_factory, patch_build_filter):
    """Test that list requires study key."""
    ep = codings.CodingsEndpoint(dummy_client)
    capture = paginator_factory(codings, [{"codingId": 1}])
    patch = patch_build_filter(codings)

    with pytest.raises(ConfigurationError):
        ep.list()

    result = ep.list(study_key="S1", status="y")

    assert capture["path"] == "/api/v1/edc/studies/S1/codings"
    assert capture["params"] == {"filter": "FILTERED"}
    assert patch["filters"] == {"status": "y"}
    assert isinstance(result[0], Coding)


def test_list_repeatable_coding_filters_transport(dummy_client, paginator_factory):
    """Test repeatable coding filters at parameter-building and transport boundary."""
    ep = codings.CodingsEndpoint(dummy_client)
    capture = paginator_factory(codings, [{"codingId": "C1", "dictionaryName": "MedDRA"}])

    result = ep.list(study_key="S1", dictionaryName="MedDRA", variable="AETERM")

    assert capture["path"] == "/api/v1/edc/studies/S1/codings"
    assert "dictionaryName==MedDRA" in capture["params"]["filter"]
    assert "variable==AETERM" in capture["params"]["filter"]
    assert len(result) == 1
    assert result[0].coding_id == "C1"
    assert result[0].dictionary_name == "MedDRA"


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get not found."""
    ep = codings.CodingsEndpoint(dummy_client)

    def fake_impl(self, client, paginator, *, study_key=None, refresh=False, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(codings.CodingsEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", "x")
