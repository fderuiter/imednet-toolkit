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
