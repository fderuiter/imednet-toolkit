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

    with pytest.raises(ValidationError, match="Nanosecond component out of range"):
        User.from_json(
            {
                "userId": "u-4",
                "roles": [
                    {
                        "roleId": "R1",
                        "dateCreated": [2026, 5, 13, 14, 6, 50, 1_000_000_000],
                    }
                ],
            }
        )


def test_bundled_and_local_fallback_contract_resolution(monkeypatch):
    """Verify bundled resource loading and local workspace fallback when env vars are unset."""
    monkeypatch.delenv("IMEDNET_OPENAPI_PATH", raising=False)
    monkeypatch.delenv("IMEDNET_POSTMAN_PATH", raising=False)

    contract = get_contract()
    assert EXPECTED_14_MODELS.issubset(set(contract.models.keys()))
    assert engine.to_snake("recordRevisionId") == "record_revision_id"
    assert "study_key" in engine.ResourceRegistry.get_fields("Study")

    # Exercise fallback from imednet.resources to imednet/resources
    monkeypatch.setattr(engine, "_CONTRACT_CACHE", None)
    orig_files = engine.importlib.resources.files

    def fallback_files(pkg: str):
        if pkg == "imednet.resources":
            raise ModuleNotFoundError(pkg)
        return orig_files(pkg)

    monkeypatch.setattr(engine.importlib.resources, "files", fallback_files)
    contract_fallback = get_contract()
    assert "Record" in contract_fallback.models

    # Exercise local dev checkout fallback when bundled resource reports is_file() == False
    monkeypatch.setattr(engine, "_CONTRACT_CACHE", None)

    class NonFileResource:
        def is_file(self) -> bool:
            return False

    class FakePkgRoot:
        def joinpath(self, *_args):
            return NonFileResource()

    monkeypatch.setattr(engine.importlib.resources, "files", lambda _pkg: FakePkgRoot())
    contract_local = get_contract()
    assert "Subject" in contract_local.models


def test_model_engine_tracer_none_and_unknown_or_any_fields(monkeypatch):
    """Verify ModelEngine handles tracer=None, unknown models, and fallback Any field definitions."""
    from typing import Any

    from imednet.models.contract import APIContract, FieldDefinition, ModelDefinition

    monkeypatch.setattr(engine, "tracer", None)
    unknown_cls = ModelEngine.get_model("UnregisteredSyntheticModel")
    assert issubclass(unknown_cls, BaseModel)
    assert unknown_cls.model_fields == {}

    synthetic_contract = APIContract(
        models={
            "SyntheticAny": ModelDefinition(
                name="SyntheticAny",
                fields={
                    "mystery_payload": FieldDefinition(
                        name="mystery_payload",
                        type_name="any",
                        default_value=None,
                        alias="mysteryPayload",
                    )
                },
            )
        }
    )
    monkeypatch.setattr(engine, "_CONTRACT_CACHE", synthetic_contract)
    monkeypatch.setattr(ModelEngine, "_model_cache", {})
    any_cls = ModelEngine.get_model("SyntheticAny")
    assert any_cls.model_fields["mystery_payload"].annotation is Any


def test_generate_stubs_matches_committed_model_pyi_files(monkeypatch):
    """Verify ModelEngine.generate_stubs reproduces committed .pyi stubs byte-for-byte."""
    import builtins

    models_dir = ROOT_DIR / "packages" / "core" / "src" / "imednet" / "models"
    written_stubs: dict[Path, str] = {}
    real_open = builtins.open

    class _MemoryWriter:
        def __init__(self, target: Path) -> None:
            self._target = target
            self._chunks: list[str] = []

        def write(self, data: str) -> int:
            self._chunks.append(data)
            return len(data)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            written_stubs[self._target] = "".join(self._chunks)

    def intercept_open(file, mode="r", *args, **kwargs):
        if "w" in mode and str(file).endswith(".pyi"):
            return _MemoryWriter(Path(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", intercept_open)
    ModelEngine.generate_stubs(str(models_dir))

    assert len(written_stubs) >= 12
    for pyi_path, generated_text in written_stubs.items():
        assert pyi_path.exists(), f"Expected committed stub {pyi_path} to exist"
        assert generated_text == pyi_path.read_text(encoding="utf-8"), (
            f"Generated stub for {pyi_path.name} drifted from committed {pyi_path}"
        )


def test_generate_stubs_synthetic_module_constructs(tmp_path, monkeypatch):
    """Verify ModelEngine.generate_stubs handles synthetic AST constructs and formatting branches."""
    assert (
        ModelEngine._format_stub_annotation("typing.Union[int, str, NoneType]")
        == "int | str | None"
    )

    # Ignored and non-matching files
    (tmp_path / "__init__.py").write_text('"""Init."""\n', encoding="utf-8")
    (tmp_path / "plain_helper.py").write_text("X = 1\n", encoding="utf-8")

    synthetic_src = '''# pylint: disable=duplicate-code

# synthetic header comment
"""Synthetic model module."""

from __future__ import annotations

import dataclasses
import os
from typing import Dict as DictAlias

from pydantic import Field as PydanticField

from .base import ImednetBaseModel as BaseAlias
from imednet.models.engine import ModelEngine

MODULE_CONSTANT: str = "v1"


def _private_helper() -> None:
    return None


def first_public_fn(value: os.PathLike[str]) -> str:
    return str(value)


def second_public_fn(flag: bool) -> bool:
    return flag


async def long_signature_function(
    first_parameter_name: str,
    second_parameter_name: DictAlias[str, str],
    third_parameter_name: int | None = None,
) -> bool:
    return bool(first_parameter_name and second_parameter_name and third_parameter_name)


@dataclasses.dataclass
class EmptyMarker:
    pass


class SyntheticRecord(BaseAlias):
    CLASS_TAG = "RECORD"
    custom_note: str | None = PydanticField(
        default=None,
        alias="customNote",
    )

    async def fetch_extra(self) -> str:
        return ""


SyntheticRecord = ModelEngine.get_model("Record", SyntheticRecord)
RecordAlias = SyntheticRecord
'''
    (tmp_path / "synthetic_records.py").write_text(synthetic_src, encoding="utf-8")

    monkeypatch.setattr(engine, "tracer", None)
    ModelEngine.generate_stubs(str(tmp_path))

    assert not (tmp_path / "__init__.pyi").exists()
    assert not (tmp_path / "plain_helper.pyi").exists()

    stub_path = tmp_path / "synthetic_records.pyi"
    assert stub_path.exists()
    stub_text = stub_path.read_text(encoding="utf-8")
    assert stub_text.startswith("# pylint: disable=duplicate-code\n# synthetic header comment\n")
    assert "from __future__ import annotations" in stub_text
    assert "import dataclasses" in stub_text
    assert "import os" in stub_text
    assert "from typing import Any, Dict as DictAlias" in stub_text
    assert "from pydantic import Field as PydanticField" in stub_text
    assert "from .base import ImednetBaseModel as BaseAlias" in stub_text
    assert "ModelEngine" not in stub_text
    assert "MODULE_CONSTANT: str" in stub_text
    assert "_private_helper" not in stub_text
    assert (
        "def first_public_fn(value: os.PathLike[str]) -> str: ...\n"
        "def second_public_fn(flag: bool) -> bool: ..."
    ) in stub_text
    assert "async def long_signature_function(\n" in stub_text
    assert "@dataclasses.dataclass\nclass EmptyMarker: ..." in stub_text
    assert "class SyntheticRecord(BaseAlias):" in stub_text
    assert "    record_data: dict[str, Any] | None" in stub_text
    assert "RecordAlias = SyntheticRecord" in stub_text
