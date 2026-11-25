"""Unit tests for context-aware strict normalizers, model contracts, and endpoint typing."""

from __future__ import annotations

import datetime
import json

import pytest
from pydantic import ValidationError

from imednet import AsyncImednetSDK, ImednetSDK, SimulationTransport, constants
from imednet.core.context import strict_mode_context
from imednet.errors.validation import PathTraversalValidationError
from imednet.models import jobs as jobs_models
from imednet.models.base import ImednetBaseModel, Metadata, Pagination
from imednet.models.contract import ContractBuilder
from imednet.models.engine import ModelEngine, ResourceRegistry
from imednet.models.intervals import FormSummary, Interval
from imednet.models.jobs import JobStatus
from imednet.models.queries import Query, QueryComment
from imednet.models.records import Keyword, Record, RecordData, RegisterSubjectRequest
from imednet.models.reporting import Demographics
from imednet.models.subjects import Subject, SubjectKeyword
from imednet.models.users import Role, User
from imednet.spi import constants as spi_constants
from imednet.utils import validators


@pytest.mark.parametrize(
    ("model_cls", "payload"),
    [
        # Invalid int primitives (bool, string, float truncation, non-finite)
        (Subject, {"subjectId": True}),
        (Subject, {"subjectId": False}),
        (Subject, {"subjectId": "not-an-int"}),
        (Subject, {"subjectId": ""}),
        (Subject, {"subjectId": 5.9}),
        (Subject, {"subjectId": 5.0}),
        (Subject, {"subjectId": "5.9"}),
        (Subject, {"subjectId": "inf"}),
        (Subject, {"siteId": True}),
        (Subject, {"siteId": "bad-site"}),
        (Subject, {"siteId": 3.7}),
        (Record, {"recordId": True}),
        (Record, {"recordId": "invalid"}),
        (Record, {"recordId": 4.2}),
        (Record, {"subjectId": "4.2"}),
        (Keyword, {"keywordName": "K", "keywordKey": "KK", "keywordId": True}),
        (Keyword, {"keywordName": "K", "keywordKey": "KK", "keywordId": "bad"}),
        (Keyword, {"keywordName": "K", "keywordKey": "KK", "keywordId": 1.5}),
        (Keyword, {"keywordName": "K", "keywordKey": "KK", "keywordId": "inf"}),
        (SubjectKeyword, {"keywordName": "K", "keywordKey": "KK", "keywordId": True}),
        (SubjectKeyword, {"keywordName": "K", "keywordKey": "KK", "keywordId": ""}),
        (JobStatus, {"batchId": "B1", "progress": "invalid"}),
        (JobStatus, {"batchId": "B1", "progress": True}),
        (JobStatus, {"batchId": "B1", "progress": 12.5}),
        (JobStatus, {"batchId": "B1", "progress": ""}),
        (JobStatus, {"batchId": "B1", "progress": "inf"}),
        # Invalid bool primitives
        (Subject, {"subjectId": 1, "deleted": "maybe"}),
        (Subject, {"subjectId": 1, "deleted": 1}),
        (Subject, {"subjectId": 1, "deleted": []}),
        (Subject, {"subjectId": 1, "deleted": float("nan")}),
        (Subject, {"subjectId": 1, "deleted": "inf"}),
        (Record, {"recordId": 1, "deleted": "maybe"}),
        (Record, {"recordId": 1, "deleted": 0}),
        (Record, {"recordId": 1, "deleted": {}}),
        (Record, {"recordId": 1, "deleted": float("nan")}),
        (Record, {"recordId": 1, "deleted": "nan"}),
        # Non-dict recordData & list-to-object coercion
        (Record, {"recordId": 1, "recordData": "not-a-dict"}),
        (Record, {"recordId": 1, "recordData": 123}),
        (Record, {"recordId": 1, "recordData": [{"AGE": 42}]}),
        (Metadata, {"status": "OK", "timestamp": "2026-05-13T12:00:00Z", "error": [{"code": "E"}]}),
        (Subject, [{"subjectId": 1}]),
        (Subject, []),
        # Scalar-to-list & object-to-list wrapping
        (Record, {"recordId": 1, "keywords": "scalar"}),
        (
            Record,
            {"recordId": 1, "keywords": {"keywordName": "K", "keywordKey": "KK", "keywordId": 1}},
        ),
        (Subject, {"subjectId": 1, "keywords": "scalar"}),
        (Pagination, {"sort": "scalar"}),
        (Pagination, {"sort": {"property": "id", "direction": "ASC"}}),
        # Empty or invalid timestamps
        (Metadata, {"status": "OK", "timestamp": ""}),
        (Metadata, {"status": "OK", "timestamp": "   "}),
        (Metadata, {"status": "OK", "timestamp": None}),
        (Metadata, {"status": "OK", "timestamp": "not-a-timestamp"}),
        (Keyword, {"keywordName": "K", "keywordKey": "KK", "keywordId": 1, "dateAdded": ""}),
        (
            Keyword,
            {"keywordName": "K", "keywordKey": "KK", "keywordId": 1, "dateAdded": "invalid-ts"},
        ),
        (
            SubjectKeyword,
            {"keywordName": "K", "keywordKey": "KK", "keywordId": 1, "dateAdded": ""},
        ),
        (Role, {"roleId": "R1", "dateCreated": ""}),
        (Demographics, {"subjectKey": "S1", "sex": "F", "birthDate": ""}),
    ],
)
def test_strict_mode_from_json_rejects_invalid_primitives_containers_and_timestamps(
    monkeypatch,
    model_cls: type[ImednetBaseModel],
    payload: object,
):
    """Verify strict mode rejects invalid primitives, structural coercions, and empty timestamps."""
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "1")
    with pytest.raises(ValidationError):
        model_cls.from_json(payload)

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "0")
    with strict_mode_context(True):
        with pytest.raises(ValidationError):
            model_cls.from_json(payload)


def test_metadata_and_keyword_accept_valid_string_timestamps_in_strict_mode(monkeypatch):
    """Verify Metadata.timestamp and Keyword.date_added accept valid string timestamps per API docs."""
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "1")
    meta = Metadata.from_json(
        {
            "status": "OK",
            "method": "GET",
            "path": "/api/v1/edc/studies/S/subjects",
            "timestamp": "2026-05-13 14:06:50",
        }
    )
    assert meta.timestamp == datetime.datetime(2026, 5, 13, 14, 6, 50)

    kw = Keyword.from_json(
        {
            "keywordName": "Safety",
            "keywordKey": "SAFE",
            "keywordId": 7,
            "dateAdded": "2026-05-13T14:06:50Z",
        }
    )
    assert kw.date_added == datetime.datetime(2026, 5, 13, 14, 6, 50, tzinfo=datetime.timezone.utc)

    js = JobStatus.from_json({"batchId": "B-1", "progress": "75"})
    assert js.progress == 75


@pytest.mark.parametrize(
    "bad_deleted",
    [float("nan"), float("inf"), float("-inf"), "nan", "NaN", "inf", "-inf"],
)
def test_nan_and_infinity_do_not_lock_record_or_subject_deleted(monkeypatch, bad_deleted):
    """Verify NaN and infinity return False in permissive mode and never lock deleted=True."""
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "0")
    rec = Record.from_json({"recordId": 10, "deleted": bad_deleted, "recordStatus": "Open"})
    assert rec.deleted is False
    rec.record_status = "Record Complete"
    assert rec.record_status == "Record Complete"

    subj = Subject.from_json({"subjectId": 20, "deleted": bad_deleted, "subjectStatus": "Enrolled"})
    assert subj.deleted is False
    subj.subject_status = "Complete"
    assert subj.subject_status == "Complete"


def test_permissive_mode_rejects_bool_on_int_fields_while_preserving_other_fallbacks(monkeypatch):
    """Verify bool inputs on int fields do not fabricate 1 in permissive mode while other fallbacks remain."""
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "0")
    assert validators.parse_int_or_default(True, default=0) == 0
    assert validators.parse_int_or_default(False, default=9) == 9

    subj = Subject.from_json({"subjectId": True, "siteId": False})
    assert subj.subject_id == 0
    assert subj.site_id == 0

    kw = Keyword.from_json({"keywordName": "K", "keywordKey": "KK", "keywordId": True})
    assert kw.keyword_id == 0

    # Other permissive fallbacks remain intact when strict mode is off
    permissive_rec = Record.from_json(
        {
            "recordId": "42.9",
            "recordData": [{"AGE": 50}],
            "keywords": {"keywordName": "K", "keywordKey": "KK", "keywordId": "3"},
        }
    )
    assert permissive_rec.record_id == 42
    assert permissive_rec.record_data == {"AGE": 50}
    assert len(permissive_rec.keywords) == 1
    assert permissive_rec.keywords[0].keyword_id == 3


def test_role_timestamp_arrays_5_6_7_elements_user_name_property_and_request_dict_data():
    """Verify Role 5-, 6-, and 7-element int timestamps, User.name @property/@computed_field, and dict request data."""
    for strict in (False, True):
        with strict_mode_context(strict):
            role_5 = Role.from_json(
                {
                    "roleId": "R-5",
                    "communityId": 5,
                    "dateCreated": [2026, 5, 13, 14, 6],
                    "dateModified": [2026, 5, 14, 9, 30],
                }
            )
            assert role_5.date_created == datetime.datetime(
                2026, 5, 13, 14, 6, 0, 0, tzinfo=datetime.timezone.utc
            )
            assert role_5.date_modified == datetime.datetime(
                2026, 5, 14, 9, 30, 0, 0, tzinfo=datetime.timezone.utc
            )

            role_6 = Role.from_json(
                {
                    "roleId": "R-6",
                    "communityId": 5,
                    "dateCreated": [2026, 5, 13, 14, 6, 50],
                    "dateModified": [2026, 5, 14, 9, 30, 15],
                }
            )
            assert role_6.date_created == datetime.datetime(
                2026, 5, 13, 14, 6, 50, 0, tzinfo=datetime.timezone.utc
            )
            assert role_6.date_modified == datetime.datetime(
                2026, 5, 14, 9, 30, 15, 0, tzinfo=datetime.timezone.utc
            )

            role = Role.from_json(
                {
                    "roleId": "R-1",
                    "communityId": 5,
                    "dateCreated": [2026, 5, 13, 14, 6, 50, 612000000],
                    "dateModified": [2026, 5, 14, 9, 30, 0, 0],
                }
            )
            assert role.date_created == datetime.datetime(
                2026, 5, 13, 14, 6, 50, 612000, tzinfo=datetime.timezone.utc
            )
            assert role.date_modified == datetime.datetime(
                2026, 5, 14, 9, 30, 0, 0, tzinfo=datetime.timezone.utc
            )

    with strict_mode_context(True):
        for bad_ts in (
            [2026, 5, 13, 14],
            [2026, 5, 13, 14, 6, 50, 0, 0],
            [2026, True, 13, 14, 6],
        ):
            with pytest.raises(ValidationError):
                Role.from_json({"roleId": "R-BAD", "dateCreated": bad_ts})

    user = User(userId="U-1", firstName="Ada", last_name="Lovelace", roles=[role])
    assert isinstance(User.name, property)
    assert user.name == "Ada Lovelace"
    dumped = user.model_dump()
    assert dumped["name"] == "Ada Lovelace"

    req = RegisterSubjectRequest(form_key="DM", siteName="SITE-01", data={"SUBJID": "001"})
    assert isinstance(req.data, RecordData)
    assert req.data.root["SUBJID"] == "001"
    assert req.site_name == "SITE-01"


@pytest.mark.asyncio
async def test_filter_get_endpoints_runtime_reject_bool_negative_and_str_ids():
    """Verify _ListGetEndpointBase._validate_item_id retains runtime guards against bool, negative, and str."""
    sdk = ImednetSDK(api_key="k", security_key="s", transport=SimulationTransport())
    async_sdk = AsyncImednetSDK(api_key="k", security_key="s", transport=SimulationTransport())

    endpoint_names = (
        "forms",
        "intervals",
        "record_revisions",
        "records",
        "sites",
        "subjects",
        "variables",
    )
    for ep_name in endpoint_names:
        sync_ep = getattr(sdk, ep_name)
        async_ep = getattr(async_sdk, ep_name)
        for bad_id in (True, False, -1, "123"):
            with pytest.raises(ValueError, match="non-negative integer"):
                sync_ep.get("STUDY_A", bad_id)  # type: ignore[arg-type]
            with pytest.raises(ValueError, match="non-negative integer"):
                await async_ep.get("STUDY_A", bad_id)  # type: ignore[arg-type]
        with pytest.raises(PathTraversalValidationError):
            sync_ep.get("STUDY_A", "../bad")  # type: ignore[arg-type]


def test_model_engine_get_model_and_registry_default_to_canonical_subclasses():
    """Verify ModelEngine.get_model and ResourceRegistry.get_fields default to canonical subclasses."""
    from imednet.models import engine

    engine._CONTRACT_CACHE = None
    ModelEngine._model_cache.clear()

    expected_canonical = [
        ("Record", Record, "keywords", Keyword),
        ("Subject", Subject, "keywords", SubjectKeyword),
        ("Interval", Interval, "forms", FormSummary),
        ("Query", Query, "query_comments", QueryComment),
        ("User", User, "roles", Role),
        ("JobStatus", JobStatus, "progress", int),
    ]
    for model_name, canonical_cls, field_name, nested_cls in expected_canonical:
        dyn_cls = ModelEngine.get_model(model_name)
        assert issubclass(dyn_cls, canonical_cls), (
            f"ModelEngine.get_model({model_name!r}) did not inherit {canonical_cls.__name__}"
        )
        assert field_name in dyn_cls.model_fields
        assert str(nested_cls.__name__) in str(dyn_cls.model_fields[field_name].annotation)

        reg_fields = ResourceRegistry.get_fields(model_name)
        assert field_name in reg_fields

    # Verify canonical properties/methods remain intact on dynamic models without base_cls
    dyn_user = ModelEngine.get_model("User").from_json({"firstName": "Grace", "lastName": "Hopper"})
    assert dyn_user.name == "Grace Hopper"

    dyn_js = ModelEngine.get_model("JobStatus").from_json(
        {"batchId": "B1", "totalRecords": 8, "successfulRecords": 7, "failedRecords": 1}
    )
    assert dyn_js.total_records == 8
    assert dyn_js.successful_records == 7
    assert dyn_js.failed_records == 1


def test_contract_builder_float_nullable_ref_post_and_terminal_job_states(tmp_path, monkeypatch):
    """Verify ContractBuilder float/number, nullable arrays, $ref, POST paths, and TERMINAL_JOB_STATES."""
    from imednet.models import engine

    builder = ContractBuilder()
    assert builder.parse_postman_type(3.14) == ("float", None)
    assert builder.parse_postman_type("<float>") == ("float", None)
    assert builder.parse_postman_type("<double>") == ("float", None)
    assert builder.parse_postman_type("<number>") == ("float", None)
    assert builder.parse_postman_type(True) == ("boolean", None)
    assert builder.parse_postman_type(10) == ("integer", None)

    openapi_spec = {
        "openapi": "3.1.0",
        "paths": {
            "/api/v1/edc/studies/{studyKey}/records": {
                "post": {
                    "responses": {
                        "201": {
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Job"}}
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "ScoreScalar": {"type": "number"},
                "TagList": {"type": "array", "items": {"type": "string"}},
                "MetaObj": {"type": "object", "properties": {"k": {"type": "string"}}},
                "CycleNode": {
                    "$ref": "#/components/schemas/CycleNode",
                },
                "Job": {
                    "type": "object",
                    "properties": {
                        "batchId": {"type": ["string", "null"]},
                        "completionRatio": {"type": "number"},
                        "nullableWeight": {"type": ["null", "number"]},
                        "refScore": {"$ref": "#/components/schemas/ScoreScalar"},
                        "refTags": {"$ref": "#/components/schemas/TagList"},
                        "refMeta": {"$ref": "#/components/schemas/MetaObj"},
                        "refCycle": {"$ref": "#/components/schemas/CycleNode"},
                    },
                },
            }
        },
    }
    spec_path = tmp_path / "openapi_31.json"
    spec_path.write_text(json.dumps(openapi_spec), encoding="utf-8")

    builder = ContractBuilder()
    builder.ingest_openapi(str(spec_path))

    assert builder.contract.paths["records"] == "Job"
    job_fields = builder.contract.models["Job"].fields
    assert job_fields["batch_id"].type_name == "string"
    assert job_fields["completion_ratio"].type_name == "float"
    assert job_fields["nullable_weight"].type_name == "float"
    assert job_fields["ref_score"].type_name == "float"
    assert job_fields["ref_tags"].type_name == "list"
    assert job_fields["ref_meta"].type_name == "dict"
    assert job_fields["ref_cycle"].type_name == "any"

    monkeypatch.setenv("IMEDNET_OPENAPI_PATH", str(spec_path))
    monkeypatch.setattr(engine, "_CONTRACT_CACHE", None)
    monkeypatch.setattr(ModelEngine, "_model_cache", {})
    dyn_job = ModelEngine.get_model("Job", ImednetBaseModel)
    assert dyn_job.model_fields["completion_ratio"].annotation == float | None
    assert dyn_job.model_fields["nullable_weight"].annotation == float | None
    assert dyn_job.model_fields["ref_score"].annotation == float | None

    expected_states = {s.value for s in jobs_models.TERMINAL_JOB_STATES}
    assert set(constants.TERMINAL_JOB_STATES) == expected_states
    assert set(spi_constants.TERMINAL_JOB_STATES) == expected_states
    assert "" not in constants.TERMINAL_JOB_STATES
    assert "" not in spi_constants.TERMINAL_JOB_STATES
