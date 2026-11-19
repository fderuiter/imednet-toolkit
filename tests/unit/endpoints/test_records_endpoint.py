"""Unit tests for records endpoint."""

from unittest.mock import MagicMock

import pytest

from imednet.core.context import set_study_context
from imednet.endpoints import records
from imednet.errors import ClientError, NotFoundError, ValidationError
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet.validation.cache import SchemaCache


def test_list_builds_path_filters_and_data_filter(
    dummy_client, paginator_factory, patch_build_filter
):
    """Test that list builds path filters and data filter."""
    ep = records.RecordsEndpoint(dummy_client)
    captured = paginator_factory(records, [{"recordId": 1}])
    filter_capture = patch_build_filter(records)

    result = ep.list(
        study_key="S1",
        record_data_filter="age>10",
        status="open",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/records"
    assert captured["params"] == {"filter": "FILTERED", "recordDataFilter": "age>10"}
    assert filter_capture["filters"] == {"status": "open", "studyKey": "S1"}
    assert isinstance(result[0], Record)


def test_get_success(monkeypatch, dummy_client):
    """Test that get success."""
    ep = records.RecordsEndpoint(dummy_client)
    called = {}

    def fake_impl(self, client, paginator, *, study_key=None, **filters):
        """Helper function to fake impl."""
        called["study_key"] = study_key
        called["filters"] = filters
        return [Record(record_id=1)]

    monkeypatch.setattr(records.RecordsEndpoint, "_list_sync", fake_impl)

    res = ep.get("S1", 1)

    assert called == {"study_key": "S1", "filters": {"recordId": 1}}
    assert isinstance(res, Record)


def test_get_rejects_unknown_keyword(monkeypatch, dummy_client):
    """Test that get rejects unknown keyword."""
    set_study_context("S1")
    ep = records.RecordsEndpoint(dummy_client)
    monkeypatch.setattr(
        records.RecordsEndpoint, "_list_sync", lambda *args, **kwargs: [Record(record_id=1)]
    )

    with pytest.raises(TypeError, match="unexpected keyword argument 'record_id'"):
        ep.get(record_id=1)


def test_get_not_found(monkeypatch, dummy_client):
    """Test that get not found."""
    ep = records.RecordsEndpoint(dummy_client)

    def fake_impl(self, client, paginator, *, study_key=None, refresh=False, **filters):
        """Helper function to fake impl."""
        return []

    monkeypatch.setattr(records.RecordsEndpoint, "_list_sync", fake_impl)

    with pytest.raises(NotFoundError):
        ep.get("S1", 1)


def test_create_sends_headers_and_parses_job(dummy_client, response_factory, monkeypatch):
    """Test that create sends headers and parses job."""
    ep = records.RecordsEndpoint(dummy_client)
    dummy_client.post.return_value = response_factory({"jobId": "1"})
    called = {}

    def fake_from_json(data):
        """Helper function to fake from json."""
        called["data"] = data
        return "JOB"

    monkeypatch.setattr(records.Job, "from_json", staticmethod(fake_from_json))

    res = ep.create("S1", [{"foo": "bar"}], email_notify=True)

    dummy_client.post.assert_called_once_with(
        "/api/v1/edc/studies/S1/records",
        json=[{"foo": "bar"}],
        headers={"x-email-notify": "true"},
    )
    assert called["data"] == {"jobId": "1"}
    assert res == "JOB"


def test_create_validates_data(dummy_client, response_factory):
    """Test that create validates data."""
    ep = records.RecordsEndpoint(dummy_client)
    schema = SchemaCache()
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    schema._form_variables = {"F1": {"age": var}}
    schema._form_id_to_key = {1: "F1"}

    dummy_client.post.return_value = response_factory({"jobId": "1"})
    # unknown form key
    with pytest.raises(ValidationError, match="Unknown form BAD"):
        ep.create("S1", [{"formKey": "BAD", "data": {}}], schema=schema)
    dummy_client.post.assert_not_called()

    # invalid variable key
    with pytest.raises(ValidationError):
        ep.create("S1", [{"formKey": "F1", "data": {"bad": 1}}], schema=schema)
    dummy_client.post.assert_not_called()

    # valid
    ep.create("S1", [{"formKey": "F1", "data": {"age": 5}}], schema=schema)
    dummy_client.post.assert_called_once()


def test_create_validates_data_with_snake_case_keys(dummy_client, response_factory):
    """Test that create validates data with snake case keys."""
    ep = records.RecordsEndpoint(dummy_client)
    schema = SchemaCache()
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    schema._form_variables = {"F1": {"age": var}}
    schema._form_id_to_key = {1: "F1"}

    dummy_client.post.return_value = response_factory({"jobId": "1"})

    # Should raise validation error because "bad" is not in schema
    # Even if we use snake_case "form_key"
    with pytest.raises(ValidationError):
        ep.create("S1", [{"form_key": "F1", "data": {"bad": 1}}], schema=schema)


def test_create_raises_on_header_injection(dummy_client):
    """Test that create raises on header injection."""
    ep = records.RecordsEndpoint(dummy_client)
    with pytest.raises(ClientError, match="Header value must not contain newlines"):
        ep.create("S1", [{"data": {}}], email_notify="test\n@example.com")


def test_to_dataframe_success(dummy_client, paginator_factory):
    """Test that to_dataframe exports queried records to a pandas DataFrame."""
    ep = records.RecordsEndpoint(dummy_client)
    paginator_factory(records, [{"recordId": 1, "recordData": {"age": 25}}])

    df = ep.to_dataframe("S1")

    assert len(df) == 1
    assert df.loc[0, "record_id"] == 1
    assert df.loc[0, "record_data"] == {"age": 25}


def test_to_dataframe_parameter_forwarding(dummy_client, paginator_factory, patch_build_filter):
    """Test that to_dataframe forwards filters and study key to list."""
    ep = records.RecordsEndpoint(dummy_client)
    captured = paginator_factory(records, [{"recordId": 1}])
    filter_capture = patch_build_filter(records)

    df = ep.to_dataframe(
        study_key="S1",
        record_data_filter="age>10",
        status="open",
    )

    assert captured["path"] == "/api/v1/edc/studies/S1/records"
    assert captured["params"] == {"filter": "FILTERED", "recordDataFilter": "age>10"}
    assert filter_capture["filters"] == {"status": "open", "studyKey": "S1"}
    assert len(df) == 1


def test_to_dataframe_flatten_option(dummy_client, paginator_factory):
    """Test that to_dataframe supports flatten option."""
    ep = records.RecordsEndpoint(dummy_client)
    paginator_factory(records, [{"recordId": 1, "recordData": {"age": 25}}])

    df = ep.to_dataframe("S1", flatten=True)

    assert len(df) == 1
    assert "record_data" not in df.columns
    assert df.loc[0, "age"] == 25


def test_to_dataframe_empty_results(dummy_client, paginator_factory):
    """Test that to_dataframe handles empty query results."""
    ep = records.RecordsEndpoint(dummy_client)
    paginator_factory(records, [])

    df = ep.to_dataframe("S1")

    assert df.empty


def test_to_dataframe_missing_dependency(monkeypatch, dummy_client, paginator_factory):
    """Test that to_dataframe raises ImportError when pandas is not installed."""
    ep = records.RecordsEndpoint(dummy_client)
    captured = paginator_factory(records, [{"recordId": 1}])
    monkeypatch.setattr("imednet.utils.pandas.pd", None)
    mock_list = MagicMock()
    monkeypatch.setattr(ep, "list", mock_list)

    with pytest.raises(ImportError, match="pandas is required for records_to_dataframe"):
        ep.to_dataframe("S1")

    mock_list.assert_not_called()
    assert captured["count"] == 0


def test_to_dataframe_masks_phi_by_default_and_honors_opt_out(
    dummy_client, paginator_factory, patch_build_filter
):
    """Test that RecordsEndpoint.to_dataframe masks PHI by default and honors mask_phi=False."""
    ep = records.RecordsEndpoint(dummy_client)
    raw_items = [
        {
            "recordId": 1,
            "subjectKey": "SUBJ-001",
            "studyKey": "STUDY1",
            "recordData": {
                "patientName": "Jane Doe",
                "DOB": "1980-01-01",
                "SSN": "123-45-6789",
                "email": "jane@example.org",
                "systolicBp": 120,
            },
        }
    ]
    paginator_factory(records, raw_items)
    filter_capture = patch_build_filter(records)

    masked_df = ep.to_dataframe("STUDY1", flatten=True, status="open")
    assert filter_capture["filters"] == {"status": "open", "studyKey": "STUDY1"}
    assert masked_df.loc[0, "patientName"] == "***MASKED***"
    assert masked_df.loc[0, "DOB"] == "***MASKED***"
    assert masked_df.loc[0, "SSN"] == "***MASKED***"
    assert masked_df.loc[0, "email"] == "***MASKED***"
    assert masked_df.loc[0, "systolicBp"] == 120
    assert masked_df.loc[0, "subject_key"] == "SUBJ-001"

    paginator_factory(records, raw_items)
    unmasked_df = ep.to_dataframe("STUDY1", flatten=True, mask_phi=False)
    assert unmasked_df.loc[0, "patientName"] == "Jane Doe"
    assert unmasked_df.loc[0, "SSN"] == "123-45-6789"
