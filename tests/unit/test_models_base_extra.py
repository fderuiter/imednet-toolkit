"""Tests verifying that SDK models tolerate unexpected or null API fields."""

from __future__ import annotations

import datetime

import pytest

from imednet.models import ErrorField as ExportedErrorField
from imednet.models.base import (
    ApiResponse,
    Error,
    ErrorField,
    ImednetBaseModel,
    Metadata,
    Pagination,
    SortField,
)
from imednet.models.jobs import Job, JobStatus
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.studies import Study
from imednet.models.study_structure import StudyStructure
from imednet.models.subjects import Subject
from imednet.models.users import User


def test_sort_field_defaults():
    """Test that sort field defaults."""
    model = SortField.model_validate({"property": None, "direction": None})
    assert model.property == ""
    assert model.direction == ""


def test_pagination_aliases_and_defaults():
    """Test that pagination aliases and defaults."""
    data = {
        "currentPage": "2",
        "size": "5",
        "totalPages": "3",
        "totalElements": "10",
        "sort": [{"property": "p", "direction": "ASC"}],
    }
    model = Pagination.model_validate(data)
    assert model.current_page == 2
    assert model.size == 5
    assert model.total_pages == 3
    assert model.total_elements == 10
    assert model.sort[0].property == "p"


def test_error_and_metadata_parsing():
    """Test that error and metadata parsing."""
    err = Error.model_validate({"details": {"foo": "bar"}})
    assert err.code == ""
    assert err.message == ""
    assert err.details == {"foo": "bar"}

    metadata = Metadata.model_validate(
        {
            "timestamp": "2023-01-01T00:00:00Z",
        }
    )
    assert metadata.status == ""
    assert metadata.method == ""
    assert metadata.path == ""
    assert isinstance(metadata.timestamp, datetime.datetime)


def test_api_response_generic():
    """Test that api response generic."""
    resp = ApiResponse[int].model_validate(
        {
            "metadata": {"timestamp": "2023-01-01T00:00:00Z"},
            "data": 5,
        }
    )
    assert resp.data == 5


def test_nested_error_envelope_and_strict_mode(monkeypatch: pytest.MonkeyPatch):
    """Test ErrorField, nested metadata.error envelopes, empty HTTP 200 error, and strict mode."""
    assert ExportedErrorField is ErrorField

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "true")

    # Upstream HTTP 400 validation error envelope without `data`
    error_envelope = {
        "metadata": {
            "status": "BAD_REQUEST",
            "path": "/api/v1/edc/studies",
            "timestamp": "2018-10-18 05:46:29",
            "error": {
                "code": "1000",
                "description": "Field raised validation errors",
                "field": {
                    "attribute": "page",
                    "value": "XX",
                },
            },
        }
    }
    parsed_err_resp = ApiResponse[list[Study]].from_json(error_envelope)
    assert parsed_err_resp.data is None
    assert parsed_err_resp.metadata.status == "BAD_REQUEST"
    assert parsed_err_resp.metadata.path == "/api/v1/edc/studies"
    assert parsed_err_resp.metadata.error.code == "1000"
    assert parsed_err_resp.metadata.error.description == "Field raised validation errors"
    assert parsed_err_resp.metadata.error.message == ""
    assert parsed_err_resp.metadata.error.field == ErrorField(attribute="page", value="XX")
    assert parsed_err_resp.metadata.error.field.attribute == "page"
    assert parsed_err_resp.metadata.error.field.value == "XX"

    # Upstream HTTP 200 envelope with empty `metadata.error` dict
    ok_envelope = {
        "metadata": {
            "status": "OK",
            "method": "GET",
            "path": "/api/v1/edc/studies",
            "timestamp": "2023-01-01T00:00:00Z",
            "error": {},
        },
        "data": [{"studyKey": "PHARMADEMO", "studyId": 100}],
    }
    parsed_ok_resp = ApiResponse[list[Study]].from_json(ok_envelope)
    assert parsed_ok_resp.metadata.error.code == ""
    assert parsed_ok_resp.metadata.error.description == ""
    assert parsed_ok_resp.metadata.error.message == ""
    assert parsed_ok_resp.metadata.error.field is None
    assert parsed_ok_resp.metadata.error.details == {}
    assert parsed_ok_resp.data is not None
    assert len(parsed_ok_resp.data) == 1


# ---------------------------------------------------------------------------
# ImednetBaseModel existence
# ---------------------------------------------------------------------------


def test_imednet_base_model_exists():
    """ImednetBaseModel must be importable and carry extra='ignore' config."""
    assert ImednetBaseModel.model_config.get("extra") == "ignore"
    assert ImednetBaseModel.model_config.get("populate_by_name") is True
    assert ImednetBaseModel.model_config.get("str_strip_whitespace") is True


# ---------------------------------------------------------------------------
# Core resilience contract: every model must ignore unknown fields
# ---------------------------------------------------------------------------

_EXTRA_FIELDS = {
    "completely_undocumented_telemetry_tracker": "12345XYZ",
    "completely_new_feature_flag": True,
    "completely_vendor_internal_id": 9999,
    "completely_future_field": None,
    "completely_nested_unknown": {"key": "value"},
}


def test_study_ignores_undocumented_fields():
    """Injecting undocumented API keys must not raise a ValidationError."""
    payload = {"studyKey": "PHARMADEMO", "studyId": 100, **_EXTRA_FIELDS}
    study = Study.model_validate(payload)
    assert study.study_key == "PHARMADEMO"
    for field in _EXTRA_FIELDS:
        assert not hasattr(study, field)


def test_site_ignores_undocumented_fields():
    """Injecting undocumented API keys into a Site payload must not raise a ValidationError."""
    payload = {"studyKey": "PHARMADEMO", "siteId": 1, **_EXTRA_FIELDS}
    site = Site.model_validate(payload)
    assert site.study_key == "PHARMADEMO"
    for field in _EXTRA_FIELDS:
        assert not hasattr(site, field)


def test_subject_ignores_undocumented_fields():
    """Injecting undocumented API keys into a Subject payload must not raise a ValidationError."""
    payload = {"studyKey": "PHARMADEMO", "subjectId": 42, **_EXTRA_FIELDS}
    subject = Subject.model_validate(payload)
    assert subject.study_key == "PHARMADEMO"
    for field in _EXTRA_FIELDS:
        assert not hasattr(subject, field)


def test_record_ignores_undocumented_fields():
    """Injecting undocumented API keys into a Record payload must not raise a ValidationError."""
    payload = {"studyKey": "PHARMADEMO", "recordId": 7, **_EXTRA_FIELDS}
    record = Record.model_validate(payload)
    assert record.study_key == "PHARMADEMO"
    for field in _EXTRA_FIELDS:
        assert not hasattr(record, field)


def test_job_ignores_undocumented_fields():
    """Injecting undocumented API keys into a Job payload must not raise a ValidationError."""
    payload = {"jobId": "abc", "batchId": "batch1", "state": "PROCESSING", **_EXTRA_FIELDS}
    job = Job.model_validate(payload)
    assert job.job_id == "abc"
    for field in _EXTRA_FIELDS:
        assert not hasattr(job, field)


def test_user_ignores_undocumented_fields():
    """Injecting undocumented API keys into a User payload must not raise a ValidationError."""
    payload = {"userId": "u1", "login": "alice", **_EXTRA_FIELDS}
    user = User.model_validate(payload)
    assert user.user_id == "u1"
    for field in _EXTRA_FIELDS:
        assert not hasattr(user, field)


def test_study_structure_ignores_undocumented_fields():
    """Injecting undocumented API keys into a StudyStructure payload must not raise a ValidationError."""
    payload = {"studyKey": "ST1", "intervals": [], **_EXTRA_FIELDS}
    study = StudyStructure.model_validate(payload)
    assert study.study_key == "ST1"
    for field in _EXTRA_FIELDS:
        assert not hasattr(study, field)


def test_job_status_ignores_undocumented_fields():
    """Injecting undocumented API keys into a JobStatus payload must not raise a ValidationError."""
    payload = {
        "jobId": "j1",
        "batchId": "b1",
        "state": "SUCCESS",
        "progress": "75",
        **_EXTRA_FIELDS,
    }
    js = JobStatus.model_validate(payload)
    assert js.progress == 75
    for field in _EXTRA_FIELDS:
        assert not hasattr(js, field)


# ---------------------------------------------------------------------------
# Null / missing optional field resilience
# ---------------------------------------------------------------------------


def test_study_survives_null_informational_fields():
    """Fields that default to '' must handle None gracefully."""
    payload = {
        "studyKey": "PHARMADEMO",
        "studyId": 100,
        "studyDescription": None,
        "studyType": {"complex": "object_unexpected"},
    }
    study = Study.model_validate(payload)
    assert study.study_key == "PHARMADEMO"


def test_site_survives_null_site_name():
    """SiteName returning null must not crash the parser."""
    payload = {"studyKey": "PHARMADEMO", "siteId": 1, "siteName": None}
    site = Site.model_validate(payload)
    assert site.study_key == "PHARMADEMO"


def test_subject_survives_null_status():
    """SubjectStatus returning null must not crash the parser."""
    payload = {"studyKey": "PHARMADEMO", "subjectId": 42, "subjectStatus": None}
    subject = Subject.model_validate(payload)
    assert subject.study_key == "PHARMADEMO"
