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


def test_contract_builder_defensive_branches_and_nested_openapi_refs(tmp_path):
    """Verify ContractBuilder handles missing/malformed inputs and nested OpenAPI data/recordData refs."""
    from imednet.models.contract import ContractBuilder

    builder = ContractBuilder()
    builder.ingest_postman(str(tmp_path / "missing_postman.json"))
    builder.ingest_openapi(str(tmp_path / "missing_openapi.json"))
    assert builder.contract.models == {}

    non_dict_file = tmp_path / "list_root.json"
    non_dict_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    builder.ingest_postman(str(non_dict_file))
    builder.ingest_openapi(str(non_dict_file))
    assert builder.contract.models == {}

    malformed_postman = tmp_path / "malformed_postman.json"
    malformed_postman.write_text(
        json.dumps(
            {
                "item": [
                    "not-a-dict",
                    {"item": "not-a-list"},
                    {"response": "not-a-list"},
                    {
                        "response": [
                            "not-a-dict",
                            {"name": "Study information", "body": "{invalid-json"},
                            {"name": "Study information", "body": json.dumps(["not-a-dict"])},
                        ]
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    builder.ingest_postman(str(malformed_postman))
    assert builder.contract.models == {}

    nested_openapi = {
        "openapi": "3.0.0",
        "paths": {
            "/api/v1/forms": {
                "post": {"responses": {"200": {}}},
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "data": {"$ref": "#/components/schemas/Form"}
                                        },
                                    }
                                }
                            }
                        }
                    }
                },
            },
            "/api/v1/codings": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "data": {
                                                "type": "array",
                                                "items": {"$ref": "#/components/schemas/Coding"},
                                            }
                                        },
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/api/v1/records": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "recordData": {"$ref": "#/components/schemas/Record"}
                                        },
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/api/v1/record_revisions": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "recordData": {
                                                "type": "array",
                                                "items": {
                                                    "$ref": "#/components/schemas/RecordRevision"
                                                },
                                            }
                                        },
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
                "Form": {
                    "type": "object",
                    "properties": {
                        "constVersion": {"const": 1},
                        "enumStatus": {"enum": ["ACTIVE", "INACTIVE"]},
                    },
                },
                "Coding": {"type": "object", "properties": {"codingId": {"const": True}}},
                "Record": {"type": "object", "properties": {"recordId": {"type": "integer"}}},
                "RecordRevision": {
                    "type": "object",
                    "properties": {"recordRevisionId": {"type": "integer"}},
                },
            }
        },
    }
    openapi_path = tmp_path / "nested_openapi.json"
    openapi_path.write_text(json.dumps(nested_openapi), encoding="utf-8")
    builder.ingest_openapi(str(openapi_path))

    assert builder.contract.models["Form"].fields["const_version"].type_name == "integer"
    assert builder.contract.models["Form"].fields["enum_status"].type_name == "string"
    assert builder.contract.models["Coding"].fields["coding_id"].type_name == "boolean"
    assert builder.contract.paths["forms"] == "Form"
    assert builder.contract.paths["codings"] == "Coding"
    assert builder.contract.paths["records"] == "Record"
    assert builder.contract.paths["record_revisions"] == "RecordRevision"
