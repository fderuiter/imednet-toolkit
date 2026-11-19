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


def test_generate_stubs_matches_committed_pyi_files(tmp_path):
    """Verify ModelEngine.generate_stubs reproduces all 13 committed .pyi stubs byte-for-byte."""
    import shutil

    models_dir = ROOT_DIR / "packages" / "core" / "src" / "imednet" / "models"
    committed_stubs = sorted(models_dir.glob("*.pyi"))
    assert len(committed_stubs) == 13

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

    # Delete a stub and verify --check catches missing committed stub
    studies_pyi.unlink()
    assert engine._run_cli(["--check", str(tmp_path)]) == 1


def test_ast_stub_generator_resolves_base_cls_strips_bodies_and_prunes_imports(tmp_path):
    """Verify AST stub generator resolves base_cls, strips method bodies, preserves Enums, and prunes imports."""
    sample_py = tmp_path / "sample_model.py"
    sample_py.write_text(
        '''"""Sample dynamic model module."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import Field, computed_field

from imednet.models.base import ImednetBaseModel
from imednet.models.engine import ModelEngine


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


SampleStudy = ModelEngine.get_model("Study", BaseSample)
''',
        encoding="utf-8",
    )

    ModelEngine.generate_stubs(str(tmp_path))
    pyi_file = tmp_path / "sample_model.pyi"
    assert pyi_file.exists()
    content = pyi_file.read_text(encoding="utf-8")

    # Unused ModelEngine, Optional, computed_field, and Any must be pruned
    assert "ModelEngine" not in content
    assert "Optional" not in content
    assert "Any" not in content
    assert "computed_field" not in content

    # Enum literal assignments preserved and method bodies stripped
    assert 'OPEN = "OPEN"' in content
    assert 'COMPLETED = "COMPLETED"' in content
    assert "def is_done(self) -> bool: ..." in content
    assert 'def helper(self, prefix: str = "x") -> str: ...' in content
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
