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
    from imednet.models.base import ImednetBaseModel

    contract = get_contract()
    int_bool_count = 0
    explicit_zero_fields = {("Keyword", "keyword_id"), ("SubjectKeyword", "keyword_id")}
    for model_name, model_def in contract.models.items():
        dyn_cls = ModelEngine.get_model(model_name)
        instance = dyn_cls.from_json({})
        raw_cls = ModelEngine.get_model(model_name, ImednetBaseModel)
        raw_instance = raw_cls.from_json({})
        for fname, fdef in model_def.fields.items():
            if fdef.type_name in {"integer", "boolean"}:
                int_bool_count += 1
                assert fdef.default_value is None, f"{model_name}.{fname} default is not None"
                assert getattr(raw_instance, fname) is None, (
                    f"raw {model_name}.{fname} resolved to {getattr(raw_instance, fname)!r}"
                )
                if (model_name, fname) not in explicit_zero_fields:
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


def test_bundled_and_local_fallback_contract_resolution(monkeypatch, tmp_path):
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
    # even when cwd is outside the repository root (e.g. tmp_path in wheel mode).
    monkeypatch.setattr(engine, "_CONTRACT_CACHE", None)
    monkeypatch.chdir(tmp_path)

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
                    ),
                    "nested_map": FieldDefinition(
                        name="nested_map",
                        type_name="dict",
                        default_value=None,
                        alias="nestedMap",
                    ),
                    "nested_items": FieldDefinition(
                        name="nested_items",
                        type_name="list",
                        default_value=None,
                        alias="nestedItems",
                    ),
                },
            )
        }
    )
    monkeypatch.setattr(engine, "_CONTRACT_CACHE", synthetic_contract)
    monkeypatch.setattr(ModelEngine, "_model_cache", {})
    any_cls = ModelEngine.get_model("SyntheticAny")
    assert any_cls.model_fields["mystery_payload"].annotation is Any
    assert (
        engine._format_runtime_annotation(any_cls.model_fields["nested_map"].annotation)
        == "dict[str, Any] | None"
    )
    assert (
        engine._format_runtime_annotation(any_cls.model_fields["nested_items"].annotation)
        == "list[Any] | None"
    )


def test_generate_stubs_matches_committed_pyi_files(tmp_path):
    """Verify ModelEngine.generate_stubs reproduces all 17 committed .pyi stubs byte-for-byte."""
    import shutil

    models_dir = ROOT_DIR / "packages" / "core" / "src" / "imednet" / "models"
    committed_stubs = sorted(models_dir.glob("*.pyi"))
    assert len(committed_stubs) == 17

    for py_file in sorted(models_dir.glob("*.py")):
        shutil.copy2(py_file, tmp_path / py_file.name)

    ModelEngine.generate_stubs(str(tmp_path))

    generated_stubs = sorted(tmp_path.glob("*.pyi"))
    assert [p.name for p in generated_stubs] == [p.name for p in committed_stubs]

    for committed in committed_stubs:
        generated = tmp_path / committed.name
        assert generated.read_text(encoding="utf-8") == committed.read_text(encoding="utf-8"), (
            f"Generated stub {committed.name} differs from committed {committed}"
        )

    assert engine._run_cli(["--check", str(models_dir)]) == 0


def test_stub_cli_check_detects_drift_and_missing_stubs(tmp_path):
    """Verify engine CLI --check returns non-zero when stubs drift or are missing."""
    import shutil

    models_dir = ROOT_DIR / "packages" / "core" / "src" / "imednet" / "models"
    for f in sorted(models_dir.glob("*.py*")):
        shutil.copy2(f, tmp_path / f.name)

    assert engine._run_cli(["--check", str(tmp_path)]) == 0

    # Drift a stub
    studies_pyi = tmp_path / "studies.pyi"
    studies_pyi.write_text(
        studies_pyi.read_text(encoding="utf-8") + "    drifted_field: str | None\n",
        encoding="utf-8",
    )
    assert engine._run_cli(["--check", str(tmp_path)]) == 1

    # Regenerate via CLI without --check, then verify --check passes again
    assert engine._run_cli([str(tmp_path)]) == 0
    assert engine._run_cli(["--check", str(tmp_path)]) == 0

    # Add an unexpected stub and verify --check catches it
    extra_pyi = tmp_path / "unexpected_extra.pyi"
    extra_pyi.write_text("X: int\n", encoding="utf-8")
    assert engine._run_cli(["--check", str(tmp_path)]) == 1
    extra_pyi.unlink()

    # Delete a stub and verify --check catches missing committed stub
    studies_pyi.unlink()
    assert engine._run_cli(["--check", str(tmp_path)]) == 1


def test_ast_stub_generator_resolves_base_cls_strips_bodies_and_prunes_imports(
    tmp_path, monkeypatch
):
    """Verify AST stub generator resolves base_cls, strips method bodies, preserves Enums, and prunes imports."""
    (tmp_path / "__init__.py").write_text('"""Init."""\n', encoding="utf-8")
    (tmp_path / "plain_helper.py").write_text("X = 1\n", encoding="utf-8")

    sample_py = tmp_path / "sample_model.py"
    sample_py.write_text(
        '''# pylint: disable=duplicate-code
"""Sample dynamic model module."""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any, Optional

from pydantic import Field, computed_field

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine

MODULE_CONST: str = "v1"


@dataclasses.dataclass
class EmptyMarker:
    pass


class SampleState(str, Enum):
    """Sample state enum."""

    OPEN = "OPEN"
    COMPLETED = "COMPLETED"

    def is_done(self) -> bool:
        """Check completion."""
        return self is SampleState.COMPLETED


class BaseSample(ImednetBaseModel):
    """Custom base preserving narrow state annotation."""

    study_key: Optional[str] = Field(default=None, alias="studyKey")

    def helper(self, prefix: str = "x") -> str:
        """Runtime body that must be stripped."""
        value = f"{prefix}:{self.study_key}"
        return value


async def long_async_helper(
    first_parameter_name: str,
    second_parameter_name: str,
    *,
    third_keyword_only: int | None = None,
    **kwargs: str,
) -> bool:
    return bool(first_parameter_name and second_parameter_name and third_keyword_only and kwargs)


SampleStudy = ModelEngine.get_model("Study", base_cls=BaseSample)
StudyAlias = SampleStudy
''',
        encoding="utf-8",
    )

    monkeypatch.setattr(engine, "tracer", None)
    ModelEngine.generate_stubs(str(tmp_path))
    assert not (tmp_path / "__init__.pyi").exists()
    assert not (tmp_path / "plain_helper.pyi").exists()

    pyi_file = tmp_path / "sample_model.pyi"
    assert pyi_file.exists()
    content = pyi_file.read_text(encoding="utf-8")

    # Leading pylint comment preserved
    assert content.startswith("# pylint: disable=duplicate-code\n")

    # Unused ModelEngine, Optional, computed_field, and Any must be pruned
    assert "ModelEngine" not in content
    assert "Optional" not in content
    assert "Any" not in content
    assert "computed_field" not in content
    assert "import dataclasses" in content

    # Enum literal assignments preserved and method bodies stripped
    assert 'OPEN = "OPEN"' in content
    assert 'COMPLETED = "COMPLETED"' in content
    assert "def is_done(self) -> bool: ..." in content
    assert 'def helper(self, prefix: str = "x") -> str: ...' in content
    assert "@dataclasses.dataclass\nclass EmptyMarker: ..." in content
    assert "async def long_async_helper(" in content
    assert "MODULE_CONST: str" in content
    assert "StudyAlias = SampleStudy" in content
    assert "value =" not in content

    # Optional normalized to PEP 604 union and base_cls resolved for synthesized SampleStudy
    assert "study_key: str | None" in content
    assert "class SampleStudy(BaseSample):" in content
    assert "sponsor_key: str | None" in content


def test_job_status_runtime_fields_match_jobs_pyi():
    """Verify runtime Job and JobStatus model_fields types match committed jobs.pyi."""
    import ast

    from imednet.models.jobs import Job, JobStatus

    jobs_pyi = ROOT_DIR / "packages" / "core" / "src" / "imednet" / "models" / "jobs.pyi"
    tree = ast.parse(jobs_pyi.read_text(encoding="utf-8"))

    stub_class_fields: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in {"Job", "JobStatus"}:
            fields_map: dict[str, str] = {}
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields_map[stmt.target.id] = ast.unparse(stmt.annotation)
            stub_class_fields[node.name] = fields_map

    expected_job_status_types = {
        "state": "JobState | str | None",
        "date_created": "str | None",
        "date_started": "str | None",
        "date_finished": "str | None",
        "progress": "int | None",
        "result_url": "str | None",
    }

    for field_name, expected_type in expected_job_status_types.items():
        assert stub_class_fields["JobStatus"][field_name] == expected_type
        runtime_ann = engine._format_runtime_annotation(
            JobStatus.model_fields[field_name].annotation
        )
        assert runtime_ann == expected_type

    for field_name in ("state", "date_created", "date_started", "date_finished"):
        expected_type = expected_job_status_types[field_name]
        assert stub_class_fields["Job"][field_name] == expected_type
        assert (
            engine._format_runtime_annotation(Job.model_fields[field_name].annotation)
            == expected_type
        )


def test_dynamic_models_preserve_runtime_module_doc_all_and_field_descriptions():
    """Verify all 14 contract models and 4 nested models preserve __module__, __doc__, __all__, and Field descriptions."""
    import importlib

    contract = get_contract()
    submodules = {
        "codings": ["Coding"],
        "forms": ["Form"],
        "intervals": ["Interval"],
        "jobs": ["Job", "JobStatus"],
        "queries": ["Query", "QueryComment"],
        "record_revisions": ["RecordRevision"],
        "records": ["Keyword", "Record"],
        "sites": ["Site"],
        "studies": ["Study"],
        "subjects": ["Subject", "SubjectKeyword"],
        "users": ["Role", "User"],
        "variables": ["Variable"],
        "visits": ["Visit"],
    }

    seen_models: set[str] = set()
    described_contract_fields = 0

    for sub, expected_classes in submodules.items():
        mod_name = f"imednet.models.{sub}"
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "__all__"), f"{mod_name} is missing __all__"
        for cls_name in expected_classes:
            assert cls_name in mod.__all__, f"{cls_name} missing from {mod_name}.__all__"
            model_cls = getattr(mod, cls_name)
            assert model_cls.__module__ == mod_name, (
                f"{cls_name}.__module__ is {model_cls.__module__!r}, expected {mod_name!r}"
            )
            assert model_cls.__doc__ and model_cls.__doc__.strip(), (
                f"{cls_name}.__doc__ was dropped"
            )
            seen_models.add(cls_name)

            if cls_name in contract.models:
                contract_def = contract.models[cls_name]
                for snake_key, field_def in contract_def.fields.items():
                    finfo = model_cls.model_fields[snake_key]
                    if finfo.description:
                        described_contract_fields += 1
                    elif field_def.alias:
                        # Base-declared field overrides may supply custom Field() without description
                        assert finfo.alias == field_def.alias or snake_key in model_cls.model_fields

    assert EXPECTED_14_MODELS.issubset(seen_models)
    assert {"Keyword", "QueryComment", "Role", "SubjectKeyword"}.issubset(seen_models)
    assert described_contract_fields >= 150


def test_generated_stubs_emit_none_defaults_and_wrap_lines_within_100_chars():
    """Verify all 17 .pyi stubs emit defaults on optional fields and wrap <= 100 chars."""
    import ast

    models_dir = ROOT_DIR / "packages" / "core" / "src" / "imednet" / "models"
    pyi_files = sorted(models_dir.glob("*.pyi"))
    assert len(pyi_files) == 17
    assert engine._run_cli([str(models_dir), "--check"]) == 0

    found_classes: dict[str, ast.ClassDef] = {}
    for pyi_file in pyi_files:
        content = pyi_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), start=1):
            assert len(line) <= 100, (
                f"{pyi_file.name}:{lineno} exceeds 100 characters ({len(line)}): {line}"
            )
        tree = ast.parse(content, filename=str(pyi_file))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                found_classes[node.name] = node

    assert EXPECTED_14_MODELS.issubset(found_classes)
    for model_name in EXPECTED_14_MODELS:
        cls_def = found_classes[model_name]
        ann_assigns = [
            stmt
            for stmt in cls_def.body
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
        ]
        assert ann_assigns, f"{model_name} stub has no field annotations"
        for stmt in ann_assigns:
            assert stmt.value is not None, (
                f"{model_name}.{stmt.target.id} in .pyi stub is missing a default value"
            )

    wrapped_field = engine._wrap_field_stub(
        "    ",
        "extremely_long_dynamic_attribute_name_exceeding_one_hundred_chars",
        "dict[str, list[str]] | int | None",
        "None",
    )
    assert len(wrapped_field) > 1
    assert all(len(line) <= 100 for line in wrapped_field)

    wrapped_import = engine._format_from_import(
        "from imednet.models.base",
        [
            "VeryLongModelHelperNameOne",
            "VeryLongModelHelperNameTwo",
            "VeryLongModelHelperNameThree",
        ],
    )
    assert all(len(line) <= 100 for line in wrapped_import.splitlines())


def test_non_aliased_model_modules_do_not_generate_stubs():
    """Verify modules without aliased ImednetBaseModel classes skip .pyi generation."""
    import ast

    models_dir = ROOT_DIR / "packages" / "core" / "src" / "imednet" / "models"
    for skipped_stem in (
        "triage",
        "verification",
        "standards",
        "sdtm",
        "__init__",
        "engine",
        "contract",
    ):
        assert not (models_dir / f"{skipped_stem}.pyi").exists(), (
            f"Unexpected stub generated for non-aliased module {skipped_stem}.py"
        )

    base_tree = ast.parse((models_dir / "base.pyi").read_text(encoding="utf-8"))
    base_classes = {node.name: node for node in base_tree.body if isinstance(node, ast.ClassDef)}
    for non_aliased_cls in (
        "ImednetBaseModel",
        "SortField",
        "ErrorField",
        "Error",
        "Metadata",
        "ApiResponse",
    ):
        init_defs = [
            stmt
            for stmt in base_classes[non_aliased_cls].body
            if isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"
        ]
        assert init_defs == [], f"Unexpected __init__ stub on {non_aliased_cls}"


def test_all_39_aliased_models_emit_unified_constructors_and_field_aliases():
    """Verify all 39 aliased models emit unified snake_case + camelCase __init__ stubs."""
    import ast

    models_dir = ROOT_DIR / "packages" / "core" / "src" / "imednet" / "models"
    expected_aliased_by_module = {
        "base.pyi": {"Pagination"},
        "codings.pyi": {"Coding"},
        "forms.pyi": {"Form"},
        "intervals.pyi": {"FormSummary", "Interval"},
        "jobs.pyi": {"Job", "JobStatus"},
        "queries.pyi": {"QueryComment", "Query"},
        "record_revisions.pyi": {"RecordRevision"},
        "records.pyi": {
            "Keyword",
            "Record",
            "RecordJobResponse",
            "BaseRecordRequest",
            "RegisterSubjectRequest",
            "UpdateScheduledRecordRequest",
            "CreateNewRecordRequest",
        },
        "reporting.pyi": {
            "AdverseEvent",
            "ProtocolDeviation",
            "DeviceDeficiency",
            "SubjectLevelAnalysis",
            "AnalysisAdverseEvent",
            "AnalysisLabResult",
            "Demographics",
            "VitalSigns",
        },
        "sites.pyi": {"Site"},
        "studies.pyi": {"Study"},
        "study_config.pyi": {"MappingRule", "WidgetConfig", "StudyConfiguration"},
        "study_structure.pyi": {"FormStructure", "IntervalStructure", "StudyStructure"},
        "subjects.pyi": {"SubjectKeyword", "Subject"},
        "users.pyi": {"Role", "User"},
        "variables.pyi": {"Variable"},
        "visits.pyi": {"Visit"},
    }
    assert sum(len(v) for v in expected_aliased_by_module.values()) == 39

    class_nodes: dict[str, ast.ClassDef] = {}
    for pyi_name, expected_classes in expected_aliased_by_module.items():
        raw_text = (models_dir / pyi_name).read_text(encoding="utf-8")
        assert all(len(line) <= 100 for line in raw_text.splitlines()), (
            f"{pyi_name} exceeds 100-char line limit"
        )
        tree = ast.parse(raw_text)
        mod_classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        assert expected_classes.issubset(mod_classes.keys()), (
            f"Missing expected classes in {pyi_name}: {expected_classes - mod_classes.keys()}"
        )
        for cls_name in expected_classes:
            cls_node = mod_classes[cls_name]
            class_nodes[cls_name] = cls_node
            init_defs = [
                stmt
                for stmt in cls_node.body
                if isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"
            ]
            assert len(init_defs) == 1, (
                f"{pyi_name}:{cls_name} expected 1 unified __init__ def, got {len(init_defs)}"
            )
            fn = init_defs[0]
            assert not any(
                isinstance(d, ast.Name) and d.id == "overload" for d in fn.decorator_list
            ), f"{cls_name}.__init__ should not have @overload"
            assert [a.arg for a in fn.args.args] == ["self"]
            assert len(fn.args.kwonlyargs) >= 2

    # Dynamic contract models preserve Field(..., alias="...") declarations
    studies_raw = (models_dir / "studies.pyi").read_text(encoding="utf-8")
    assert 'alias="studyKey"' in studies_raw
    assert 'alias="sponsorKey"' in studies_raw
    study_fields = {
        stmt.target.id: ast.unparse(stmt.value)
        for stmt in class_nodes["Study"].body
        if isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and stmt.value is not None
    }
    assert "alias='studyKey'" in study_fields["study_key"]
    assert "alias='sponsorKey'" in study_fields["sponsor_key"]

    # BaseRecordRequest and subclasses accept RecordData | dict[str, Any] and both aliases in one __init__
    for req_cls_name, extra_keys in (
        ("BaseRecordRequest", {"form_key", "formKey", "data"}),
        ("RegisterSubjectRequest", {"form_key", "formKey", "site_name", "siteName", "data"}),
        (
            "UpdateScheduledRecordRequest",
            {
                "form_key",
                "formKey",
                "subject_key",
                "subjectKey",
                "interval_name",
                "intervalName",
                "data",
            },
        ),
        ("CreateNewRecordRequest", {"form_key", "formKey", "subject_key", "subjectKey", "data"}),
    ):
        init_fn = next(
            stmt
            for stmt in class_nodes[req_cls_name].body
            if isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"
        )
        kw_map = {a.arg: ast.unparse(a.annotation) for a in init_fn.args.kwonlyargs if a.annotation}
        assert extra_keys.issubset(kw_map.keys())
        assert kw_map["data"] == "RecordData | dict[str, Any]"

    # Role accepts 7-integer timestamp sequences and User.name emits @property -> str
    role_init = next(
        stmt
        for stmt in class_nodes["Role"].body
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"
    )
    role_kw = {a.arg: ast.unparse(a.annotation) for a in role_init.args.kwonlyargs if a.annotation}
    for ts_key in ("date_created", "dateCreated", "date_modified", "dateModified"):
        assert role_kw[ts_key] == "datetime | str | Sequence[int] | None"

    user_props = {
        stmt.name: ast.unparse(stmt.returns)
        for stmt in class_nodes["User"].body
        if isinstance(stmt, ast.FunctionDef)
        and stmt.returns is not None
        and any(isinstance(d, ast.Name) and d.id == "property" for d in stmt.decorator_list)
    }
    assert user_props["name"] == "str"

    # JobStatus accepts normalized record count kwargs while keeping int properties
    job_status_init = next(
        stmt
        for stmt in class_nodes["JobStatus"].body
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"
    )
    js_kw = {a.arg for a in job_status_init.args.kwonlyargs}
    assert {
        "batch_id",
        "batchId",
        "job_id",
        "jobId",
        "record_total_count",
        "record_success_count",
        "record_failure_count",
        "total_records",
        "successful_records",
        "failed_records",
        "totalRecords",
        "successfulRecords",
        "failedRecords",
    }.issubset(js_kw)

    job_status_props = {
        stmt.name: ast.unparse(stmt.returns)
        for stmt in class_nodes["JobStatus"].body
        if isinstance(stmt, ast.FunctionDef)
        and stmt.returns is not None
        and any(isinstance(d, ast.Name) and d.id == "property" for d in stmt.decorator_list)
    }
    assert job_status_props["total_records"] == "int | None"
    assert job_status_props["successful_records"] == "int | None"
    assert job_status_props["failed_records"] == "int | None"


def test_stub_formatting_helpers_cover_wrapping_and_defaults():
    """Verify dynamic field wrapping, unified init wrapping, and default formatting helpers."""
    from pydantic import Field

    from imednet.models.queries import QueryStatus

    assert engine._format_runtime_default(None) == "None"
    assert engine._format_runtime_default(True) == "True"
    assert engine._format_runtime_default(12) == "12"
    assert engine._format_runtime_default("general") == '"general"'
    assert engine._format_runtime_default(QueryStatus.OPEN) == "QueryStatus.OPEN"
    assert engine._format_runtime_default(object()) == "..."

    medium_init = engine._format_init_stub(
        ["first_medium_param: str = ''", "second_medium_param: int = 0"]
    )
    assert all(len(line) <= 100 for line in medium_init)
    empty_init = engine._format_init_stub([])
    assert empty_init == ["    def __init__(self) -> None: ..."]
    long_union_init = engine._format_init_stub(
        [
            "extremely_long_union_parameter_name: ("
            "dict[str, list[str]] | Sequence[int] | datetime | str | None"
            ") = None"
        ]
    )
    assert len(long_union_init) > 1
    wrapped_param = engine._wrap_init_param(
        "        ",
        "extremely_long_union_parameter_name: dict[str, list[str]] | Sequence[int] | datetime | str | None = None",
    )
    assert len(wrapped_param) == 3
    assert all(len(line) <= 100 for line in wrapped_param)
    multi_union_param = engine._wrap_init_param(
        "        ",
        "very_long_param: dict[str, list[tuple[str, int]]] | Sequence[datetime] | Mapping[str, Any] | str | int | None = None",
    )
    assert len(multi_union_param) > 3
    assert all(len(line) <= 100 for line in multi_union_param)

    long_finfo = Field(
        default=None,
        alias="extremelyLongDynamicAttributeAliasExceedingSingleLineThreshold",
    )
    wrapped_dyn = engine._wrap_dynamic_field_stub(
        "    ",
        "extremely_long_dynamic_attribute_name_exceeding_threshold",
        "dict[str, list[str]] | int | None",
        long_finfo,
    )
    assert len(wrapped_dyn) > 1
    assert all(len(line) <= 100 for line in wrapped_dyn)

    medium_finfo = Field(default=None, alias="mediumAttributeAliasName")
    medium_dyn = engine._wrap_dynamic_field_stub(
        "    ",
        "medium_attribute_field_name_for_two_line_wrap",
        "str | int | None",
        medium_finfo,
    )
    assert len(medium_dyn) == 3
    assert all(len(line) <= 100 for line in medium_dyn)

    unaliased_finfo = Field(default=None)
    unaliased_dyn = engine._wrap_dynamic_field_stub(
        "    ", "plain_opt", "int | None", unaliased_finfo
    )
    assert unaliased_dyn == ["    plain_opt: int | None = None"]


def test_contract_builder_extracts_study_from_list_and_all_nested_array_models():
    """Verify ContractBuilder.ingest_postman populates Study from List studies and nested models."""
    contract = get_contract()
    assert "Study" in contract.models
    assert "last_updated" in contract.models["Study"].fields
    assert len(contract.models["Study"].fields) == 9

    assert set(contract.models["FormSummary"].fields) == {"form_id", "form_key", "form_name"}
    assert {
        "sequence",
        "annotation_status",
        "user",
        "comment",
        "closed",
        "date",
    }.issubset(contract.models["QueryComment"].fields)
    assert {"keyword_name", "keyword_key", "keyword_id", "date_added"}.issubset(
        contract.models["Keyword"].fields
    )
    assert {"keyword_name", "keyword_key", "keyword_id", "date_added"}.issubset(
        contract.models["SubjectKeyword"].fields
    )
    assert {
        "role_id",
        "name",
        "community_id",
        "inactive",
        "date_created",
        "date_modified",
    }.issubset(contract.models["Role"].fields)
    assert "progress" not in contract.models["JobStatus"].fields
    assert "result_url" not in contract.models["JobStatus"].fields


def test_mypy_strict_accepts_aliased_models_and_rejects_missing_required_fields(tmp_path):
    """Verify mypy --strict with plugins = [] passes valid calls and rejects missing required args & str get IDs."""
    from mypy import api as mypy_api

    pyproject_path = str(ROOT_DIR / "pyproject.toml")
    positive_targets = [
        str(ROOT_DIR / "tests" / "unit" / "reporting" / "test_study_config.py"),
        str(ROOT_DIR / "packages" / "core" / "src" / "imednet" / "integrations" / "enrichment.py"),
    ]
    stdout, stderr, exit_code = mypy_api.run(
        ["--config-file", pyproject_path, "--strict", *positive_targets]
    )
    assert exit_code == 0, f"mypy --strict failed on positive targets:\n{stdout}\n{stderr}"

    negative_probe = tmp_path / "negative_missing_required.py"
    negative_probe.write_text(
        """from imednet import AsyncImednetSDK, ImednetSDK
from imednet.models.base import SortField
from imednet.models.reporting import Demographics
from imednet.models.study_config import MappingRule, WidgetConfig
from imednet.models.study_structure import FormStructure

MappingRule(target_field="aeTerm")
WidgetConfig(widget_id="w1")
FormStructure(form_id=1)
Demographics(subject_key="S1")
SortField(property="id")


def check_sync_str_get(sdk: ImednetSDK) -> None:
    sdk.forms.get("S", "1")
    sdk.intervals.get("S", "1")
    sdk.record_revisions.get("S", "1")
    sdk.records.get("S", "1")
    sdk.sites.get("S", "1")
    sdk.subjects.get("S", "1")
    sdk.variables.get("S", "1")


async def check_async_str_get(sdk: AsyncImednetSDK) -> None:
    await sdk.forms.get("S", "1")
    await sdk.intervals.get("S", "1")
    await sdk.record_revisions.get("S", "1")
    await sdk.records.get("S", "1")
    await sdk.sites.get("S", "1")
    await sdk.subjects.get("S", "1")
    await sdk.variables.get("S", "1")
""",
        encoding="utf-8",
    )
    neg_out, neg_err, neg_code = mypy_api.run(
        ["--config-file", pyproject_path, "--strict", str(negative_probe)]
    )
    assert neg_code != 0, f"Expected mypy --strict failure:\n{neg_out}"
    call_arg_lines = [line for line in neg_out.splitlines() if "[call-arg]" in line]
    arg_type_lines = [line for line in neg_out.splitlines() if "[arg-type]" in line]
    assert len(call_arg_lines) == 7, (
        f"Expected 7 [call-arg] errors on missing required fields, got {len(call_arg_lines)}:\n"
        f"{neg_out}\n{neg_err}"
    )
    assert len(arg_type_lines) == 14, (
        f"Expected 14 [arg-type] errors (7 sync + 7 async str item_id get calls), got {len(arg_type_lines)}:\n"
        f"{neg_out}\n{neg_err}"
    )
