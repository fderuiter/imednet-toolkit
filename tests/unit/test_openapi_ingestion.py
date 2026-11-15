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
