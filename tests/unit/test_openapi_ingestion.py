"""Tests for OpenAPI ingestion."""

import json

from imednet.models.engine import ModelEngine


def test_openapi_ingestion(tmp_path, monkeypatch):
    """Test openapi ingestion."""
    # Setup OpenAPI file
    openapi_content = {
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
            },
            "/api/v1/subjects/{id}": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Subject"},
                                    }
                                }
                            }
                        }
                    }
                }
            },
        },
        "components": {
            "schemas": {
                "Study": {
                    "type": "object",
                    "properties": {
                        "studyKey": {"type": "string"},
                        "isDisabled": {"type": "boolean"},
                        "studyId": {"type": "integer"},
                    },
                },
                "Subject": {
                    "type": "object",
                    "properties": {
                        "subjectId": {"type": "integer"},
                        "subjectKey": {"type": "string"},
                        "isEnrolled": {"type": "boolean"},
                    },
                },
            }
        },
    }

    openapi_file = tmp_path / "openapi.json"
    with open(openapi_file, 'w') as f:
        json.dump(openapi_content, f)

    monkeypatch.setenv("IMEDNET_OPENAPI_PATH", str(openapi_file))

    # We need to clear the engine cache
    from imednet.models import engine

    monkeypatch.setattr(engine, "_CONTRACT_CACHE", None)
    monkeypatch.setattr(ModelEngine, "_model_cache", {})

    study_model = ModelEngine.get_model("Study")
    subject_model = ModelEngine.get_model("Subject")

    assert "study_key" in study_model.model_fields
    assert "is_disabled" in study_model.model_fields
    assert "study_id" in study_model.model_fields

    assert "subject_id" in subject_model.model_fields
    assert "subject_key" in subject_model.model_fields
    assert "is_enrolled" in subject_model.model_fields

    # Verify that properties map correctly
    assert str(study_model.model_fields["study_key"].annotation).find("str") != -1
    assert str(study_model.model_fields["is_disabled"].annotation).find("bool") != -1
    assert str(study_model.model_fields["study_id"].annotation).find("int") != -1

    contract = engine.get_contract()
    assert contract.paths.get("studies") == "Study"
    assert contract.paths.get("subjects") == "Subject"


def test_openapi_ingestion_containers_literals_and_none_defaults(tmp_path, monkeypatch):
    """Verify OpenAPI ingestion maps object/array/literal examples to dict/list/scalar with None defaults."""
    from imednet.models import engine

    openapi_content = {
        "openapi": "3.0.0",
        "components": {
            "schemas": {
                "Record": {
                    "type": "object",
                    "properties": {
                        "recordId": {"type": "integer"},
                        "deleted": {"type": "boolean"},
                        "recordData": {"type": "object"},
                        "keywords": {"type": "array", "items": {"type": "object"}},
                        "literalFlag": {"example": False},
                        "literalCount": {"default": 0},
                        "literalNote": {"example": ""},
                    },
                }
            }
        },
    }
    openapi_file = tmp_path / "openapi_containers.json"
    openapi_file.write_text(json.dumps(openapi_content))

    monkeypatch.setenv("IMEDNET_OPENAPI_PATH", str(openapi_file))
    monkeypatch.setattr(engine, "_CONTRACT_CACHE", None)
    monkeypatch.setattr(ModelEngine, "_model_cache", {})

    contract = engine.get_contract()
    rec_fields = contract.models["Record"].fields
    assert rec_fields["record_id"].type_name == "integer"
    assert rec_fields["deleted"].type_name == "boolean"
    assert rec_fields["record_data"].type_name == "dict"
    assert rec_fields["keywords"].type_name == "list"
    assert rec_fields["literal_flag"].type_name == "boolean"
    assert rec_fields["literal_count"].type_name == "integer"
    assert rec_fields["literal_note"].type_name == "string"
    for fdef in rec_fields.values():
        assert fdef.default_value is None

    dyn_record = ModelEngine.get_model("Record")
    inst = dyn_record.from_json({})
    assert inst.record_id is None
    assert inst.deleted is None
    assert inst.record_data is None
    assert inst.keywords is None
    assert inst.literal_flag is None
    assert inst.literal_count is None
    assert inst.literal_note is None
