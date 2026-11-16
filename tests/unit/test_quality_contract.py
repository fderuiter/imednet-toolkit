"""Guard shared verification tasks and public package metadata against drift."""

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_ci_uses_canonical_full_verification():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    scripts = project["tool"]["hatch"]["envs"]["default"]["scripts"]
    assert scripts["verify"] == ["verify-static", "verify-tests", "docs"]
    assert "--cov-fail-under=90" in scripts["verify-tests"]
    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text())
    steps = pipeline["jobs"]["quality"]["steps"]
    assert any(s.get("run") == "hatch run verify" for s in steps)
    assert any(s.get("run") == "hatch run quality:all packages" for s in steps)


def test_ruff_versions_and_metadata_are_consistent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pinned = next(
        d for d in project["project"]["optional-dependencies"]["dev"] if d.startswith("ruff==")
    )
    version = pinned.split("==")[1]
    hooks = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    ruff_repo = next(repo for repo in hooks["repos"] if "ruff-pre-commit" in repo["repo"])
    assert ruff_repo["rev"] == "v" + version
    assert pinned in project["tool"]["hatch"]["envs"]["lint"]["dependencies"]
    for path in (ROOT / "packages").glob("*/pyproject.toml"):
        metadata = tomllib.loads(path.read_text())["project"]
        assert metadata["requires-python"] == ">=3.11"
        assert metadata["urls"]["repository"] == "https://github.com/fderuiter/imednet-toolkit"


def test_odm_emitter_uses_only_xml_building_operations():
    import ast

    path = ROOT / "packages/core/src/imednet/datadict/emit.py"
    tree = ast.parse(path.read_text())
    uses = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ET"
    }
    assert uses <= {"Element", "SubElement", "register_namespace", "indent", "tostring"}
