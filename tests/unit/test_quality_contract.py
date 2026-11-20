"""Guard shared verification tasks and public package metadata against drift."""

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _workflow_triggers(pipeline: dict) -> dict:
    return pipeline.get("on") or pipeline.get(True) or {}


def test_ci_uses_canonical_full_verification():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert set(project["project"]["dependencies"]) == {
        "imednet",
        "imednet-workflows",
        "apache-airflow-providers-imednet",
        "imednet-plugins-sinks",
        "imednet-tui",
    }
    assert "optional-dependencies" not in project["project"]
    assert {"dev", "docs"} <= set(project["dependency-groups"])
    assert project["tool"]["uv"]["default-groups"] == ["dev", "docs"]
    assert project["tool"]["hatch"]["envs"]["default"]["dependency-groups"] == ["dev", "docs"]

    scripts = project["tool"]["hatch"]["envs"]["default"]["scripts"]
    assert scripts["verify"] == ["verify-static", "verify-tests"]
    assert "docs" not in scripts["verify"]
    assert "mypy packages/plugins-sinks/src/imednet_sinks" in scripts["verify-static"]
    assert "mypy packages/plugins-tui/src/imednet_tui" in scripts["verify-static"]
    assert "mypy examples" in scripts["verify-static"]
    for cov_pkg in (
        "--cov=imednet",
        "--cov=imednet_workflows",
        "--cov=apache_airflow_providers_imednet",
        "--cov=imednet_sinks",
        "--cov=imednet_tui",
        "--cov-fail-under=90",
    ):
        assert cov_pkg in scripts["verify-tests"]

    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))
    steps = pipeline["jobs"]["quality"]["steps"]
    run_blocks = [s.get("run", "") for s in steps if "run" in s]
    assert any(s.get("uses", "").startswith("astral-sh/setup-uv@") for s in steps)
    assert any("uv sync --locked --all-packages --all-extras" in r for r in run_blocks)
    assert any(
        "uv run mypy packages examples tests/typing/endpoint_contracts.py" in r for r in run_blocks
    )
    assert any(
        "--cov=imednet_sinks" in r and "--cov=imednet_tui" in r and "--cov-fail-under=90" in r
        for r in run_blocks
    )
    assert any("semgrep scan" in r and "vulture" in r and "pylint" in r for r in run_blocks)
    assert not any("pip install" in r for r in run_blocks)
    assert not any("hatch run" in r for r in run_blocks)
    assert not any("build_docs.py" in r for r in run_blocks)

    test_steps = pipeline["jobs"]["test"]["steps"]
    download_step = next(s for s in test_steps if s.get("name") == "Download Built Artifacts")
    assert download_step.get("if") == "needs.plan.outputs.build_matrix != '[]'"
    run_tests_step = next(s for s in test_steps if s.get("name") == "Run Tests")
    assert "tests/integration/test_airflow*.py" in run_tests_step["run"]
    assert "tests/integration/test_workflows*.py" in run_tests_step["run"]


def test_ci_docs_job_enforces_clean_working_tree_and_validation():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    docs_deps = project["dependency-groups"]["docs"]
    assert not any("sphinx-thebe" in dep for dep in docs_deps)
    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))
    steps = pipeline["jobs"]["docs"]["steps"]
    run_blocks = [s.get("run", "") for s in steps if "run" in s]
    assert any(s.get("uses", "").startswith("astral-sh/setup-uv@") for s in steps)
    assert any("uv sync --locked --all-packages --all-extras" in r for r in run_blocks)
    assert sum("uv run python scripts/build_docs.py" == r.strip() for r in run_blocks) == 1
    assert any("scripts/verify_pyodide_tutorials.py" in r for r in run_blocks)
    assert not any("pip install" in r for r in run_blocks)
    assert not any("hatch run" in r for r in run_blocks)
    assert any(
        "git diff --exit-code docs/" in r and "git status --porcelain" in r for r in run_blocks
    )
    assert any(
        s.get("uses", "").startswith("actions/upload-artifact@")
        and s.get("if") == "github.event_name == 'pull_request'"
        and s.get("with", {}).get("path") == "docs/_build/html"
        for s in steps
    )
    assert any(
        s.get("uses", "").startswith("actions/upload-pages-artifact@")
        and "workflow_dispatch" in s.get("if", "")
        and s.get("with", {}).get("path") == "docs/_build/html"
        for s in steps
    )


def test_ci_deploy_docs_gates_and_manual_dispatch():
    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))
    triggers = _workflow_triggers(pipeline)
    assert "push" in triggers
    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers

    deploy = pipeline["jobs"]["deploy-docs"]
    needs = set(deploy["needs"])
    assert {"plan", "pre-commit", "test", "docs", "global-lint", "quality"} <= needs
    cond = " ".join(str(deploy["if"]).split())
    assert "needs.docs.result == 'success'" in cond
    assert "needs.pre-commit.result == 'success'" in cond
    assert "!contains(needs.*.result, 'failure')" in cond
    assert "!contains(needs.*.result, 'cancelled')" in cond
    assert "github.ref == 'refs/heads/main'" in cond
    assert "github.event_name == 'push'" in cond
    assert "github.event_name == 'workflow_dispatch'" in cond


def test_ruff_versions_and_metadata_are_consistent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pinned = next(d for d in project["dependency-groups"]["dev"] if d.startswith("ruff=="))
    version = pinned.split("==")[1]
    hooks = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    ruff_repo = next(repo for repo in hooks["repos"] if "ruff-pre-commit" in repo["repo"])
    assert ruff_repo["rev"] == "v" + version
    assert pinned in project["tool"]["hatch"]["envs"]["lint"]["dependencies"]

    manifests = sorted((ROOT / "packages").glob("*/pyproject.toml"))
    assert len(manifests) == 5
    canonical_docs_url = "https://fderuiter.github.io/imednet-toolkit/"
    for path in manifests:
        metadata = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        assert metadata["requires-python"] == ">=3.11"
        assert metadata["urls"]["repository"] == "https://github.com/fderuiter/imednet-toolkit"
        assert metadata["urls"]["documentation"] == canonical_docs_url

    core_readme = (ROOT / "packages/core/README.md").read_text(encoding="utf-8")
    assert canonical_docs_url in core_readme
    assert "imednet-python-sdk" not in core_readme


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


def test_ci_quality_job_verifies_model_type_stubs():
    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text())
    steps = pipeline["jobs"]["quality"]["steps"]
    assert any(
        "ModelEngine.generate_stubs" in s.get("run", "")
        and "git diff --exit-code packages/core/src/imednet/models/" in s.get("run", "")
        for s in steps
    )


def test_copilot_and_consumption_workflows_use_canonical_uv_sync():
    copilot = yaml.safe_load(
        (ROOT / ".github/workflows/copilot-setup-steps.yml").read_text(encoding="utf-8")
    )
    copilot_runs = [
        s.get("run", "") for s in copilot["jobs"]["copilot-setup-steps"]["steps"] if "run" in s
    ]
    assert any("uv sync --locked --all-packages --all-extras" in r for r in copilot_runs)

    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))
    consumption_steps = pipeline["jobs"]["validate-package-consumption"]["steps"]
    consumption_runs = [s.get("run", "") for s in consumption_steps if "run" in s]
    assert any(s.get("uses", "").startswith("astral-sh/setup-uv@") for s in consumption_steps)
    assert any("uv sync --locked --all-packages --all-extras" in r for r in consumption_runs)
    assert not any("pip install" in r for r in consumption_runs)
