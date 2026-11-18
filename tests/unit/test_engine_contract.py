"""Unit tests for contract engine, package resource resolution, and bundled assets."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from imednet.models import engine
from imednet.models.engine import ModelEngine, get_contract
from imednet.models.records import Record
from imednet.models.subjects import Subject

ROOT_DIR = Path(__file__).resolve().parents[2]
ROOT_COLLECTION = ROOT_DIR / "imednet.postman_collection.json"
BUNDLED_COLLECTION = (
    ROOT_DIR
    / "packages"
    / "core"
    / "src"
    / "imednet"
    / "resources"
    / "imednet.postman_collection.json"
)

EXPECTED_14_MODELS = {
    "Coding",
    "Form",
    "Interval",
    "Job",
    "JobStatus",
    "Query",
    "Record",
    "RecordRevision",
    "Site",
    "Study",
    "Subject",
    "User",
    "Variable",
    "Visit",
}


@pytest.fixture(autouse=True)
def _reset_engine_cache(monkeypatch):
    """Ensure a clean contract and model cache for each test."""
    monkeypatch.setattr(engine, "_CONTRACT_CACHE", None)
    monkeypatch.setattr(ModelEngine, "_model_cache", {})


def test_bundled_resource_resolution():
    """Verify that get_contract loads all 14 core resource models via bundled resources."""
    contract = get_contract()
    assert EXPECTED_14_MODELS.issubset(set(contract.models.keys()))


def test_subject_and_record_schema_fields():
    """Verify that Subject and Record models contain dynamic schema attributes."""
    assert "subject_id" in Subject.model_fields
    assert "subject_key" in Subject.model_fields
    assert "site_key" in Subject.model_fields or "site_id" in Subject.model_fields

    assert "record_id" in Record.model_fields
    assert "record_key" in Record.model_fields or "record_status" in Record.model_fields
    assert "study_key" in Record.model_fields


def test_all_14_core_models_have_fields():
    """Verify that all 14 core models have dynamic schema fields populated."""
    for model_name in EXPECTED_14_MODELS:
        model_cls = ModelEngine.get_model(model_name)
        assert issubclass(model_cls, BaseModel)
        assert len(model_cls.model_fields) > 0, f"Model {model_name} has no fields"


def test_custom_postman_path_override(tmp_path, monkeypatch):
    """Verify IMEDNET_POSTMAN_PATH override loads custom contract specification."""
    custom_spec = {
        "info": {"name": "Custom"},
        "item": [
            {
                "name": "Custom Subject",
                "response": [
                    {
                        "name": "Subject list",
                        "body": json.dumps({"data": [{"customField": "<string>"}]}),
                    }
                ],
            }
        ],
    }
    custom_file = tmp_path / "custom_postman.json"
    custom_file.write_text(json.dumps(custom_spec))

    monkeypatch.setenv("IMEDNET_POSTMAN_PATH", str(custom_file))
    contract = get_contract()
    assert "Subject" in contract.models
    assert "custom_field" in contract.models["Subject"].fields


def test_custom_openapi_path_override(tmp_path, monkeypatch):
    """Verify IMEDNET_OPENAPI_PATH override loads OpenAPI specification."""
    custom_openapi = {
        "openapi": "3.0.0",
        "paths": {
            "/api/v1/studies": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Study"}
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "Study": {
                    "type": "object",
                    "properties": {
                        "customStudyField": {"type": "string"},
                    },
                }
            }
        },
    }
    openapi_file = tmp_path / "custom_openapi.json"
    openapi_file.write_text(json.dumps(custom_openapi))

    monkeypatch.setenv("IMEDNET_OPENAPI_PATH", str(openapi_file))
    contract = get_contract()
    assert "Study" in contract.models
    assert "custom_study_field" in contract.models["Study"].fields


def test_missing_postman_path_raises_file_not_found(monkeypatch):
    """Verify FileNotFoundError is raised when IMEDNET_POSTMAN_PATH does not exist."""
    monkeypatch.setenv("IMEDNET_POSTMAN_PATH", "/nonexistent/postman_spec.json")
    with pytest.raises(FileNotFoundError, match="Configured Postman collection file not found"):
        get_contract()


def test_missing_openapi_path_raises_file_not_found(monkeypatch):
    """Verify FileNotFoundError is raised when IMEDNET_OPENAPI_PATH does not exist."""
    monkeypatch.setenv("IMEDNET_OPENAPI_PATH", "/nonexistent/openapi_spec.json")
    with pytest.raises(FileNotFoundError, match="Configured OpenAPI specification file not found"):
        get_contract()


def test_missing_contract_raises_file_not_found(monkeypatch):
    """Verify FileNotFoundError is raised when no contract specification can be located."""
    monkeypatch.delenv("IMEDNET_OPENAPI_PATH", raising=False)
    monkeypatch.delenv("IMEDNET_POSTMAN_PATH", raising=False)

    class FakeResource:
        def is_file(self):
            return False

    def fake_files(_package):
        class TraversableRoot:
            def joinpath(self, *args):
                return FakeResource()

        return TraversableRoot()

    monkeypatch.setattr(engine.importlib.resources, "files", fake_files)
    monkeypatch.setattr(engine.os.path, "exists", lambda _p: False)

    with pytest.raises(FileNotFoundError, match="API contract specification could not be found"):
        get_contract()


def test_empty_specification_raises_runtime_error(tmp_path, monkeypatch):
    """Verify RuntimeError is raised when contract specification yields 0 models."""
    empty_file = tmp_path / "empty_postman.json"
    empty_file.write_text(json.dumps({"info": {"name": "Empty"}}))

    monkeypatch.setenv("IMEDNET_POSTMAN_PATH", str(empty_file))
    with pytest.raises(RuntimeError, match="No valid models could be loaded"):
        get_contract()


def test_postman_bundled_asset_identical_to_root():
    """Verify that bundled Postman collection asset matches repository root copy."""
    assert ROOT_COLLECTION.exists(), "Root Postman collection file is missing"
    assert BUNDLED_COLLECTION.exists(), "Bundled Postman collection file is missing"
    assert ROOT_COLLECTION.read_bytes() == BUNDLED_COLLECTION.read_bytes(), (
        "Bundled Postman collection differs from root collection"
    )


def test_parse_postman_type_infers_literals_and_checks_bool_before_int():
    """Verify ContractBuilder.parse_postman_type infers scalar and container types with None defaults."""
    from imednet.models.contract import ContractBuilder

    builder = ContractBuilder()
    assert builder.parse_postman_type(True) == ("boolean", None)
    assert builder.parse_postman_type(False) == ("boolean", None)
    assert builder.parse_postman_type("<boolean>") == ("boolean", None)
    assert builder.parse_postman_type(0) == ("integer", None)
    assert builder.parse_postman_type(42) == ("integer", None)
    assert builder.parse_postman_type("<integer>") == ("integer", None)
    assert builder.parse_postman_type("") == ("string", None)
    assert builder.parse_postman_type("literal") == ("string", None)
    assert builder.parse_postman_type("<string>") == ("string", None)
    assert builder.parse_postman_type({}) == ("dict", None)
    assert builder.parse_postman_type([]) == ("list", None)
    assert builder.parse_postman_type(None) == ("any", None)


def test_zero_any_fields_across_collection_models_and_job():
    """Verify zero documented attributes resolve to typing.Any across all SDK contract models except JobStatus.results."""
    from typing import Any

    from imednet.models.codings import Coding
    from imednet.models.forms import Form
    from imednet.models.intervals import Interval
    from imednet.models.jobs import Job, JobStatus
    from imednet.models.queries import Query
    from imednet.models.record_revisions import RecordRevision
    from imednet.models.records import Record
    from imednet.models.sites import Site
    from imednet.models.studies import Study
    from imednet.models.subjects import Subject
    from imednet.models.users import User
    from imednet.models.variables import Variable
    from imednet.models.visits import Visit

    models_without_any = [
        Form,
        Variable,
        Interval,
        Site,
        Subject,
        Record,
        RecordRevision,
        Coding,
        Query,
        Visit,
        User,
        Study,
        Job,
    ]
    for model_cls in models_without_any:
        any_fields = [
            fname for fname, finfo in model_cls.model_fields.items() if finfo.annotation is Any
        ]
        assert any_fields == [], f"{model_cls.__name__} retains typing.Any fields: {any_fields}"

    job_status_any = [
        fname for fname, finfo in JobStatus.model_fields.items() if finfo.annotation is Any
    ]
    assert job_status_any == ["results"]


def test_all_nullable_integer_and_boolean_contract_attributes_default_to_none():
    """Verify all nullable integer and boolean contract attributes default to None when omitted."""
    contract = get_contract()
    int_bool_count = 0
    for model_name, model_def in contract.models.items():
        dyn_cls = ModelEngine.get_model(model_name)
        instance = dyn_cls.from_json({})
        for fname, fdef in model_def.fields.items():
            if fdef.type_name in {"integer", "boolean"}:
                int_bool_count += 1
                assert fdef.default_value is None, f"{model_name}.{fname} default is not None"
                assert getattr(instance, fname) is None, (
                    f"{model_name}.{fname} resolved to {getattr(instance, fname)!r} instead of None"
                )
    assert int_bool_count >= 46


def test_record_from_json_defaults_and_typed_keywords():
    """Verify Record.from_json preserves None nullable IDs/booleans and parses Keyword objects."""
    from datetime import UTC, datetime

    from imednet.models.records import Keyword, Record

    minimal = Record.from_json({"studyKey": "S1", "recordId": 1})
    assert minimal.visit_id is None
    assert minimal.parent_record_id is None
    assert minimal.deleted is None
    assert minimal.keywords == []
    assert minimal.record_data == {}

    with_keywords = Record.from_json(
        {
            "studyKey": "S1",
            "recordId": 2,
            "keywords": [
                {
                    "keywordName": "Safety Review",
                    "keywordKey": "SAFE_REV",
                    "keywordId": 77,
                    "dateAdded": "2026-05-13T14:06:50Z",
                }
            ],
            "recordData": {"AETERM": "Nausea"},
        }
    )
    assert len(with_keywords.keywords) == 1
    kw = with_keywords.keywords[0]
    assert isinstance(kw, Keyword)
    assert kw.keyword_id == 77
    assert kw.keyword_key == "SAFE_REV"
    assert kw.keyword_name == "Safety Review"
    assert kw.date_added == datetime(2026, 5, 13, 14, 6, 50, tzinfo=UTC)
    assert with_keywords.record_data == {"AETERM": "Nausea"}


def test_coding_and_form_documented_types():
    """Verify Coding numeric fields and Form.other_forms boolean field preserve documented types."""
    from imednet.models.codings import Coding
    from imednet.models.forms import Form

    coding = Coding.from_json({"codingId": 1, "revision": 2, "reason": "Fix"})
    assert coding.coding_id == 1 and type(coding.coding_id) is int
    assert coding.revision == 2 and type(coding.revision) is int
    assert coding.reason == "Fix" and type(coding.reason) is str

    form = Form.from_json({"otherForms": True})
    assert form.other_forms is True
    assert Form.from_json({}).other_forms is None


def test_user_from_json_roles_timestamp_arrays_and_strict_mode(monkeypatch):
    """Verify User.from_json validates Role items, parses 7-element timestamps, and rejects extra nested keys in strict mode."""
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from imednet.models.users import Role, User

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "true")
    user = User.from_json(
        {
            "userId": "u-1",
            "login": "auditor",
            "firstName": "Ada",
            "lastName": "Lovelace",
            "email": "ada@example.com",
            "userActiveInStudy": True,
            "roles": [
                {
                    "dateCreated": [2026, 5, 13, 14, 6, 50, 612000000],
                    "dateModified": [2026, 5, 13, 14, 6, 50, 613000000],
                    "roleId": "DATA_MGR",
                    "communityId": 12,
                    "name": "Data Manager",
                    "description": "Clinical data review",
                    "level": 2,
                    "type": "Study",
                    "inactive": False,
                }
            ],
        }
    )
    assert len(user.roles) == 1
    role = user.roles[0]
    assert isinstance(role, Role)
    assert role.role_id == "DATA_MGR"
    assert role.community_id == 12
    assert role.date_created == datetime(2026, 5, 13, 14, 6, 50, 612000, tzinfo=UTC)
    assert role.date_modified == datetime(2026, 5, 13, 14, 6, 50, 613000, tzinfo=UTC)

    with pytest.raises(ValidationError, match="undocumentedNestedKey"):
        User.from_json(
            {
                "userId": "u-2",
                "roles": [
                    {
                        "roleId": "R1",
                        "undocumentedNestedKey": "unexpected",
                    }
                ],
            }
        )

    with pytest.raises(ValidationError, match="7-element integer timestamp array"):
        User.from_json(
            {
                "userId": "u-3",
                "roles": [{"roleId": "R1", "dateCreated": [2026, 5, 13]}],
            }
        )
