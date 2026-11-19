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


def test_create_sends_headers_and_parses_job(dummy_client, response_factory):
    """Test that create sends headers and parses state='created' into JobState.CREATED."""
    from imednet.models.jobs import JobState

    ep = records.RecordsEndpoint(dummy_client)
    dummy_client.post.return_value = response_factory(
        {"jobId": "1", "batchId": "b1", "state": "created"}
    )

    res = ep.create(
        "S1",
        [{"formKey": "F1", "data": {"foo": "bar"}}],
        email_notify="user@example.com",
    )

    dummy_client.post.assert_called_once_with(
        "/api/v1/edc/studies/S1/records",
        json=[{"formKey": "F1", "data": {"foo": "bar"}}],
        headers={"x-email-notify": "user@example.com"},
    )
    assert isinstance(res, records.Job)
    assert res.job_id == "1"
    assert res.batch_id == "b1"
    assert res.state == JobState.CREATED


@pytest.mark.parametrize(
    "bad_email",
    [True, False, "", "   ", "true", "user@test", "not-an-email", "@example.com", "a@b@c.com"],
)
def test_create_rejects_boolean_or_malformed_email_before_dispatch(dummy_client, bad_email):
    """Passing boolean or malformed email_notify raises ValidationError before HTTP dispatch."""
    ep = records.RecordsEndpoint(dummy_client)
    with pytest.raises(ValidationError):
        ep.create("S1", [{"formKey": "F1", "data": {}}], email_notify=bad_email)
    dummy_client.post.assert_not_called()


def test_create_rejects_missing_form_identifier_and_record_data(dummy_client):
    """RecordsEndpoint.create rejects missing formKey/formId and unmapped recordData keys."""
    ep = records.RecordsEndpoint(dummy_client)
    with pytest.raises(ValidationError):
        ep.create("S1", [{"data": {"age": 30}}])
    with pytest.raises(ValidationError) as exc_info:
        ep.create(
            "S1",
            [
                {
                    "formKey": "F1",
                    "recordData": {"patient_name": "Alice Secret", "ssn": "999-00-1111"},
                }
            ],
        )
    assert "Alice Secret" not in str(exc_info.value)
    assert "999-00-1111" not in str(exc_info.value)
    dummy_client.post.assert_not_called()


def test_create_validates_data(dummy_client, response_factory):
    """Test that create validates data."""
    ep = records.RecordsEndpoint(dummy_client)
    schema = SchemaCache()
    var = Variable(variable_name="age", variable_type="integer", form_id=1, form_key="F1")
    schema._form_variables = {"F1": {"age": var}}
    schema._form_id_to_key = {1: "F1"}

    dummy_client.post.return_value = response_factory(
        {"jobId": "1", "batchId": "b1", "state": "created"}
    )
    # unknown form key
    with pytest.raises(ValidationError, match="Unknown form BAD"):
        ep.create("S1", [{"formKey": "BAD", "data": {}}], schema=schema)
    dummy_client.post.assert_not_called()

    # invalid variable key
    with pytest.raises(ValidationError):
        ep.create("S1", [{"formKey": "F1", "data": {"bad": 1}}], schema=schema)
    dummy_client.post.assert_not_called()

    # valid
    job = ep.create("S1", [{"formKey": "F1", "data": {"age": 5}}], schema=schema)
    assert job.state == "CREATED"
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


def test_create_typed_and_raw_strict_ids_sync(dummy_client, response_factory, monkeypatch):
    """Verify both typed request models and raw dicts enforce strict IDs through sync create and create_record."""
    from pydantic import ValidationError as PydanticValidationError

    from imednet.models.jobs import JobState
    from imednet.models.records import (
        CreateNewRecordRequest,
        RecordData,
        RecordRequest,
        RegisterSubjectRequest,
        UpdateScheduledRecordRequest,
    )
    from imednet.sdk import ImednetSDK

    ep = records.RecordsEndpoint(dummy_client)
    dummy_client.post.return_value = response_factory(
        {"jobId": "10", "batchId": "b10", "state": "created"}
    )

    typed_batch = [
        RegisterSubjectRequest(formId=1, siteId=2, data=RecordData({"enroll": "yes"})),
        CreateNewRecordRequest(
            formKey="AE", subjectId=3, recordId=4, data=RecordData({"term": "headache"})
        ),
        UpdateScheduledRecordRequest(
            formId=5, subjectKey="S-1", intervalId=6, data=RecordData({"sys": 120})
        ),
        RecordRequest(formKey="DEMOG", siteId=7, data=RecordData({"age": 41})),
    ]
    res = ep.create("S1", typed_batch)
    assert res.state == JobState.CREATED
    dummy_client.post.assert_called_once_with(
        "/api/v1/edc/studies/S1/records",
        json=[
            {"formId": 1, "siteId": 2, "data": {"enroll": "yes"}},
            {"formKey": "AE", "subjectId": 3, "recordId": 4, "data": {"term": "headache"}},
            {"formId": 5, "subjectKey": "S-1", "intervalId": 6, "data": {"sys": 120}},
            {"formKey": "DEMOG", "siteId": 7, "data": {"age": 41}},
        ],
        headers={},
    )

    dummy_client.post.reset_mock()
    sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://example.com")
    monkeypatch.setattr(sdk.records, "_client", dummy_client)

    sdk_job = sdk.create_record(
        "S1",
        [
            RecordRequest(formId=9, siteId=2, data=RecordData({"ok": 1})),
            {"formId": 10, "siteId": 3, "data": {"ok": 2}},
        ],
    )
    assert sdk_job.state == JobState.CREATED
    dummy_client.post.assert_called_once_with(
        "/api/v1/edc/studies/S1/records",
        json=[
            {"formId": 9, "siteId": 2, "data": {"ok": 1}},
            {"formId": 10, "siteId": 3, "data": {"ok": 2}},
        ],
        headers={},
    )

    dummy_client.post.reset_mock()
    for bad_id in ["10", 10.0, True, False, 0, -3]:
        # Public request models reject non-strict IDs before Pydantic coercion
        for builder in (
            lambda v=bad_id: RecordRequest(formId=v),  # type: ignore[arg-type]
            lambda v=bad_id: RegisterSubjectRequest(formKey="F1", siteId=v),  # type: ignore[arg-type]
            lambda v=bad_id: CreateNewRecordRequest(formKey="F1", subjectId=v),  # type: ignore[arg-type]
            lambda v=bad_id: CreateNewRecordRequest(formKey="F1", recordId=v),  # type: ignore[arg-type]
            lambda v=bad_id: UpdateScheduledRecordRequest(formKey="F1", intervalId=v),  # type: ignore[arg-type]
        ):
            with pytest.raises(PydanticValidationError):
                builder()

        # Equivalent raw dicts and unvalidated model_construct instances fail identically in create / create_record
        for bad_payload in (
            [{"formId": bad_id, "data": {}}],
            [{"formKey": "F1", "siteId": bad_id, "data": {}}],
            [{"formKey": "F1", "subjectId": bad_id, "data": {}}],
            [{"formKey": "F1", "intervalId": bad_id, "data": {}}],
            [{"formKey": "F1", "recordId": bad_id, "data": {}}],
            [RecordRequest.model_construct(form_id=bad_id, data=RecordData({}))],
            [
                RegisterSubjectRequest.model_construct(
                    form_key="F1", site_id=bad_id, data=RecordData({})
                )
            ],
        ):
            with pytest.raises(ValidationError):
                ep.create("S1", bad_payload)  # type: ignore[arg-type]
            with pytest.raises(ValidationError):
                sdk.create_record("S1", bad_payload)  # type: ignore[arg-type]

    # Typed model missing both formKey and formId is also rejected
    with pytest.raises(ValidationError):
        ep.create("S1", [RegisterSubjectRequest(siteName="Site 1")])
    with pytest.raises(ValidationError):
        sdk.create_record("S1", [RegisterSubjectRequest(siteName="Site 1")])
    dummy_client.post.assert_not_called()


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
