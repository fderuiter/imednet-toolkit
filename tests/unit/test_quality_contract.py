"""Guard shared verification tasks and public package metadata against drift."""

import copy
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SHA_SETUP_UV_RE = re.compile(r"^astral-sh/setup-uv@[0-9a-f]{40}$")
EXPECTED_RUFF_SELECT = {
    "PL",
    "PIE",
    "FURB",
    "E",
    "F",
    "I",
    "N",
    "SLF",
    "D",
    "S",
    "B",
    "C4",
    "SIM",
    "UP",
    "PT",
    "RET",
    "RUF",
}
EXPECTED_TEST_IGNORES = ["SLF001", "S", "D", "PT", "SIM", "RUF", "B", "RET", "UP", "C", "F821"]


def _workflow_triggers(pipeline: dict) -> dict:
    return pipeline.get("on") or pipeline.get(True) or {}


def _load_guardrail_manifests() -> tuple[dict, dict, dict, dict]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    hooks = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    typos_cfg = tomllib.loads((ROOT / "typos.toml").read_text(encoding="utf-8"))
    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))
    return project, hooks, typos_cfg, pipeline


def _assert_guardrail_contract(
    project: dict,
    hooks: dict,
    typos_cfg: dict,
    pipeline: dict,
) -> None:
    dev_deps = project["dependency-groups"]["dev"]
    lint_deps = project["tool"]["hatch"]["envs"]["lint"]["dependencies"]

    ruff_pin = next((d for d in dev_deps if d.startswith("ruff==")), None)
    assert ruff_pin is not None
    ruff_version = ruff_pin.split("==", 1)[1]
    assert ruff_pin in lint_deps

    typos_pin = next((d for d in dev_deps if d.startswith("typos==")), None)
    assert typos_pin is not None
    typos_version = typos_pin.split("==", 1)[1]
    assert typos_pin in lint_deps
    assert "typos" not in lint_deps

    detect_secrets_pin = next((d for d in lint_deps if d.startswith("detect-secrets==")), None)
    assert detect_secrets_pin == "detect-secrets==1.5.0"  # pragma: allowlist secret
    detect_secrets_version = detect_secrets_pin.split("==", 1)[1]
    assert "detect-secrets" not in lint_deps

    extend_words = typos_cfg.get("default", {}).get("extend-words", {})
    assert extend_words.get("VAS") == "VAS"
    assert extend_words.get("SOID") == "SOID"

    ruff_repo = next(repo for repo in hooks["repos"] if "ruff-pre-commit" in repo["repo"])
    assert ruff_repo["rev"] == f"v{ruff_version}"
    assert [h["id"] for h in ruff_repo["hooks"]] == ["ruff-check", "ruff-format"]
    ruff_check_hook = ruff_repo["hooks"][0]
    assert ruff_check_hook.get("args") == ["--fix", "--exit-non-zero-on-fix"]
    assert not any("UP038" in str(arg) for arg in ruff_check_hook.get("args", []))

    typos_repo = next(repo for repo in hooks["repos"] if "crate-ci/typos" in repo["repo"])
    assert typos_repo["rev"] == f"v{typos_version}"
    assert [h["id"] for h in typos_repo["hooks"]] == ["typos"]
    typos_hook_args = typos_repo["hooks"][0].get("args")
    assert typos_hook_args == ["--force-exclude"]
    assert "--write-changes" not in typos_hook_args
    assert "--diff" not in typos_hook_args

    secrets_repo = next(repo for repo in hooks["repos"] if "Yelp/detect-secrets" in repo["repo"])
    assert secrets_repo["rev"] == f"v{detect_secrets_version}"
    assert [h["id"] for h in secrets_repo["hooks"]] == ["detect-secrets"]
    assert secrets_repo["hooks"][0].get("args") == ["--baseline", ".secrets.baseline"]

    ruff_lint = project["tool"]["ruff"]["lint"]
    assert set(ruff_lint["select"]) == EXPECTED_RUFF_SELECT
    per_file_ignores = ruff_lint["per-file-ignores"]
    testpaths = project["tool"]["pytest"]["ini_options"]["testpaths"]
    assert set(testpaths) >= {"tests", "packages/plugins-sinks/tests", "packages/plugins-tui/tests"}
    for testpath in testpaths:
        assert per_file_ignores.get(f"{testpath}/**/*.py") == EXPECTED_TEST_IGNORES

    default_scripts = project["tool"]["hatch"]["envs"]["default"]["scripts"]
    assert "ruff format --check ." in default_scripts["verify-static"]
    assert "ruff check ." in default_scripts["verify-static"]
    assert "typos --force-exclude ." in default_scripts["verify-static"]
    assert not any("typos" in cmd and "--diff" in cmd for cmd in default_scripts["verify-static"])

    lint_scripts = project["tool"]["hatch"]["envs"]["lint"]["scripts"]
    assert lint_scripts["fmt"] == "ruff format --check {args:.}"
    assert lint_scripts["ruff-check"] == "ruff check {args:.}"
    assert lint_scripts["typos-check"] == "typos --force-exclude {args:.}"
    assert "--diff" not in lint_scripts["typos-check"]
    assert lint_scripts["secrets"] == (
        "python scripts/run_secret_scan.py {args:.}"  # pragma: allowlist secret
    )
    assert "detect-secrets-hook" not in lint_scripts["secrets"]
    assert "secrets" in lint_scripts["all"]
    assert lint_scripts["mypy-check"] == "mypy {args:packages}"
    for pkg_scoped_task in ("semgrep-check", "vulture-check", "pylint-sim", "symilar-check"):
        assert "{args:packages}" in lint_scripts[pkg_scoped_task]

    dup_scripts = project["tool"]["hatch"]["envs"]["duplication"]["scripts"]
    assert dup_scripts["check"] == "pylint --disable=all --enable=duplicate-code {args:packages}"

    quality_scripts = project["tool"]["hatch"]["envs"]["quality"]["scripts"]
    assert quality_scripts["all"] == [
        "hatch run lint:all",
        "hatch run duplication:check {args:packages}",
    ]

    jobs = pipeline["jobs"]
    assert "global-lint" not in jobs
    assert "ci-gate" in jobs
    assert jobs["pre-commit"]["name"] == "Pre-commit Quality Checks"
    assert jobs["quality"]["name"] == "Workspace Quality & Security"
    assert jobs["ci-gate"]["name"] == "CI Gate"
    assert jobs["deploy-docs"]["name"] == "Deploy to GH Pages"

    for job_key in ("pre-commit", "quality"):
        job_steps = jobs[job_key]["steps"]
        assert any(SHA_SETUP_UV_RE.match(str(s.get("uses", ""))) for s in job_steps)
        job_runs = [s.get("run", "") for s in job_steps if "run" in s]
        assert not any("pip install" in r for r in job_runs)

    pre_commit_runs = "\n".join(s.get("run", "") for s in jobs["pre-commit"]["steps"] if "run" in s)
    assert re.search(
        r'uvx --from "pre-commit==\d+\.\d+\.\d+" pre-commit run --all-files --show-diff-on-failure',
        pre_commit_runs,
    )

    def _job_needs(job_key: str) -> list[str]:
        raw = jobs[job_key].get("needs", [])
        return [raw] if isinstance(raw, str) else list(raw)

    for parallel_job in ("quality", "build", "docs", "fuzzing", "integration-sandbox"):
        assert _job_needs(parallel_job) == ["plan"], f"{parallel_job} must depend only on plan"

    assert jobs["quality"].get("if") == "needs.plan.outputs.run_quality == 'true'"
    assert jobs["build"].get("if") == "needs.plan.outputs.build_matrix != '[]'"
    assert (
        jobs["fuzzing"].get("if")
        == "needs.plan.outputs.build_matrix != '[]' || needs.plan.outputs.run_fuzzing == 'true'"
    )
    assert (
        jobs["validate-package-consumption"].get("if") == "needs.plan.outputs.build_matrix != '[]'"
    )

    test_if = " ".join(str(jobs["test"].get("if", "")).split())
    assert test_if == (
        "always() && !cancelled() && needs.plan.result == 'success' && "
        "(needs.build.result == 'success' || needs.build.result == 'skipped') && "
        "needs.plan.outputs.matrix != '[]'"
    )

    quality_runs = [s.get("run", "") for s in jobs["quality"]["steps"] if "run" in s]
    quality_joined = "\n".join(quality_runs)
    assert "ruff " not in quality_joined
    assert "typos" not in quality_joined
    assert "detect-secrets" not in quality_joined
    assert "run_secret_scan.py" not in quality_joined
    assert "pytest" not in quality_joined
    assert "uv run mypy packages examples tests/typing/endpoint_contracts.py" in quality_joined

    for job_id, job in jobs.items():
        if job_id == "pre-commit":
            continue
        other_runs = "\n".join(s.get("run", "") for s in job.get("steps", []) if "run" in s)
        assert "ruff format" not in other_runs
        assert "ruff check" not in other_runs
        assert "typos --force-exclude" not in other_runs
        assert "detect-secrets" not in other_runs
        assert "run_secret_scan.py" not in other_runs

    plan_outputs = jobs["plan"]["outputs"]
    assert {"matrix", "build_matrix", "run_quality", "run_integration", "run_fuzzing"} <= set(
        plan_outputs
    )
    filter_step = next(s for s in jobs["plan"]["steps"] if s.get("id") == "filter")
    parsed_filters = yaml.safe_load(filter_step["with"]["filters"])
    export_patterns = parsed_filters["export_logic"]
    assert "packages/plugins-sinks/**" in export_patterns
    fuzz_patterns = parsed_filters["fuzzing"]
    assert "tests/core/test_fuzz.py" in fuzz_patterns
    assert "tests/core/corpora/**" in fuzz_patterns
    assert "tests/utils/verification_reporter.py" in fuzz_patterns

    ci_gate = jobs["ci-gate"]
    ci_gate_if = " ".join(str(ci_gate.get("if", "")).split())
    assert ci_gate_if == "always() && !cancelled()"
    assert {
        "plan",
        "pre-commit",
        "quality",
        "build",
        "test",
        "integration-sandbox",
        "fuzzing",
        "validate-package-consumption",
        "docs",
    } <= set(_job_needs("ci-gate"))
    ci_gate_runs = "\n".join(s.get("run", "") for s in ci_gate["steps"] if "run" in s)
    assert "failure" in ci_gate_runs and "cancelled" in ci_gate_runs

    build_steps = jobs["build"]["steps"]
    build_step = next(s for s in build_steps if s.get("name") == "Build Workspace Packages")
    assert "Skipping" not in build_step["run"]
    upload_steps = [
        s for s in build_steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert {s["with"]["name"] for s in upload_steps} == {
        "dist-core",
        "dist-workflows",
        "dist-providers",
        "dist-sinks",
        "dist-tui",
    }
    for up in upload_steps:
        assert up.get("if") == "needs.plan.outputs.build_matrix != '[]'"

    run_tests_step = next(s for s in jobs["test"]["steps"] if s.get("name") == "Run Tests")
    assert ".venv/bin/pytest" in run_tests_step["run"]
    assert "IMEDNET_TEST_WHEELS=1" in run_tests_step["run"]
    assert "--ignore-glob=*sinks*" not in run_tests_step["run"]
    assert "hatch run" not in run_tests_step["run"]
    assert "Passing gracefully" not in run_tests_step["run"]
    assert "-eq 5" not in run_tests_step["run"]


def test_ci_uses_canonical_full_verification():
    project, hooks, typos_cfg, pipeline = _load_guardrail_manifests()
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
    assert "typos --force-exclude ." in scripts["verify-static"]
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

    _assert_guardrail_contract(project, hooks, typos_cfg, pipeline)

    steps = pipeline["jobs"]["quality"]["steps"]
    run_blocks = [s.get("run", "") for s in steps if "run" in s]
    assert any(s.get("uses", "").startswith("astral-sh/setup-uv@") for s in steps)
    assert any("uv sync --locked --all-packages --all-extras" in r for r in run_blocks)
    assert any("pip-audit==" in r for r in run_blocks)
    assert any(
        "uv run mypy packages examples tests/typing/endpoint_contracts.py" in r for r in run_blocks
    )
    assert any(
        "semgrep scan" in r
        and "vulture" in r
        and "--enable=similarities" in r
        and "--enable=duplicate-code" in r
        for r in run_blocks
    )
    assert not any("pytest" in r for r in run_blocks)
    assert not any("ruff " in r for r in run_blocks)
    assert not any("typos" in r for r in run_blocks)
    assert not any("run_secret_scan.py" in r for r in run_blocks)
    assert not any("pip install" in r for r in run_blocks)
    assert not any("hatch run" in r for r in run_blocks)
    assert not any("build_docs.py" in r for r in run_blocks)

    test_steps = pipeline["jobs"]["test"]["steps"]
    download_step = next(s for s in test_steps if s.get("name") == "Download Built Artifacts")
    assert download_step.get("if") == "needs.plan.outputs.build_matrix != '[]'"
    run_tests_step = next(s for s in test_steps if s.get("name") == "Run Tests")
    assert "tests/integration/test_airflow*.py" in run_tests_step["run"]
    assert "tests/integration/test_workflows*.py" in run_tests_step["run"]
    assert "--cov=imednet_sinks" in run_tests_step["run"]
    assert "--cov=imednet_tui" in run_tests_step["run"]
    assert "--cov-fail-under=90" in run_tests_step["run"]
    assert "--ignore-glob=*sinks*" not in run_tests_step["run"]
    assert ".venv/bin/pytest" in run_tests_step["run"]
    assert "IMEDNET_TEST_WHEELS=1" in run_tests_step["run"]
    assert "hatch run" not in run_tests_step["run"]
    assert "Passing gracefully" not in run_tests_step["run"]
    assert "-eq 5" not in run_tests_step["run"]


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
    assert "global-lint" not in needs
    assert {"plan", "pre-commit", "test", "docs", "quality"} <= needs
    cond = " ".join(str(deploy["if"]).split())
    assert "needs.docs.result == 'success'" in cond
    assert "needs.pre-commit.result == 'success'" in cond
    assert "!contains(needs.*.result, 'failure')" in cond
    assert "!contains(needs.*.result, 'cancelled')" in cond
    assert "github.ref == 'refs/heads/main'" in cond
    assert "github.event_name == 'push'" in cond
    assert "github.event_name == 'workflow_dispatch'" in cond


def test_ruff_versions_and_metadata_are_consistent():
    project, hooks, typos_cfg, pipeline = _load_guardrail_manifests()
    _assert_guardrail_contract(project, hooks, typos_cfg, pipeline)

    pinned_ruff = next(d for d in project["dependency-groups"]["dev"] if d.startswith("ruff=="))
    ruff_version = pinned_ruff.split("==")[1]
    ruff_repo = next(repo for repo in hooks["repos"] if "ruff-pre-commit" in repo["repo"])
    assert ruff_repo["rev"] == "v" + ruff_version
    assert pinned_ruff in project["tool"]["hatch"]["envs"]["lint"]["dependencies"]

    pinned_typos = next(d for d in project["dependency-groups"]["dev"] if d.startswith("typos=="))
    typos_version = pinned_typos.split("==")[1]
    typos_repo = next(repo for repo in hooks["repos"] if "crate-ci/typos" in repo["repo"])
    assert typos_repo["rev"] == "v" + typos_version
    assert pinned_typos in project["tool"]["hatch"]["envs"]["lint"]["dependencies"]

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
    assert "validate-extras" not in pipeline["jobs"]

    consumption_job = pipeline["jobs"]["validate-package-consumption"]
    assert set(consumption_job["needs"]) == {"plan", "build"}
    assert consumption_job.get("if") == "needs.plan.outputs.build_matrix != '[]'"

    consumption_steps = consumption_job["steps"]
    download_step = next(
        s for s in consumption_steps if s.get("name") == "Download Built Artifacts"
    )
    assert download_step.get("if") == "needs.plan.outputs.build_matrix != '[]'"
    assert download_step.get("uses", "").startswith("actions/download-artifact@")

    consumption_runs = [s.get("run", "") for s in consumption_steps if "run" in s]
    assert any(s.get("uses", "").startswith("astral-sh/setup-uv@") for s in consumption_steps)
    assert any("uv sync --locked --all-packages --all-extras" in r for r in consumption_runs)
    assert any(
        "scripts/validate_package_consumption.py --wheel-dir dist" in r for r in consumption_runs
    )
    assert not any("pip install" in r for r in consumption_runs)


def test_ci_export_filter_includes_sinks_and_schedules_integration_sandbox():
    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))
    plan_job = pipeline["jobs"]["plan"]
    assert plan_job["outputs"]["run_integration"] == "${{ steps.filter.outputs.export_logic }}"

    filter_step = next(s for s in plan_job["steps"] if s.get("id") == "filter")
    filters = yaml.safe_load(filter_step["with"]["filters"])
    export_patterns = filters["export_logic"]
    assert "packages/plugins-sinks/**" in export_patterns

    sink_file = PurePosixPath("packages/plugins-sinks/src/imednet_sinks/warehouse.py")
    assert any(
        sink_file.match(pattern) or str(sink_file).startswith("packages/plugins-sinks/")
        for pattern in export_patterns
        if pattern == "packages/plugins-sinks/**"
    )

    sandbox_job = pipeline["jobs"]["integration-sandbox"]
    assert sandbox_job["needs"] == ["plan"]
    assert "needs.plan.outputs.run_integration == 'true'" in sandbox_job["if"]

    fuzzing_job = pipeline["jobs"]["fuzzing"]
    assert fuzzing_job["needs"] == ["plan"]
    assert (
        fuzzing_job["if"]
        == "needs.plan.outputs.build_matrix != '[]' || needs.plan.outputs.run_fuzzing == 'true'"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash is needed for workflow scripts")
def test_ci_build_builds_all_five_wheels_and_run_tests_propagates_exit_code_5(tmp_path: Path):
    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))

    build_steps = pipeline["jobs"]["build"]["steps"]
    build_step = next(s for s in build_steps if s.get("name") == "Build Workspace Packages")
    assert "Skipping" not in build_step["run"]

    upload_steps = [
        s for s in build_steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert {s["with"]["name"] for s in upload_steps} == {
        "dist-core",
        "dist-workflows",
        "dist-providers",
        "dist-sinks",
        "dist-tui",
    }
    for up in upload_steps:
        assert up.get("if") == "needs.plan.outputs.build_matrix != '[]'"

    run_tests_step = next(
        s for s in pipeline["jobs"]["test"]["steps"] if s.get("name") == "Run Tests"
    )
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "imednet-1.2.0-py3-none-any.whl").write_bytes(b"wheel")
    marker_file = tmp_path / "wheel_mode.txt"

    stub_pytest = venv_bin / "pytest"
    stub_pytest.write_text(
        f'#!/bin/sh\nprintf "%s" "${{IMEDNET_TEST_WHEELS:-unset}}" > "{marker_file}"\nexit 5\n',
        encoding="utf-8",
    )
    stub_pytest.chmod(0o755)

    res = subprocess.run(
        [shutil.which("bash"), "-e", "-c", run_tests_step["run"]],
        cwd=tmp_path,
        env={**os.environ, "PACKAGE": "core"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 5
    assert marker_file.read_text(encoding="utf-8") == "1"


def _mutate_ruff_rev(project: dict, hooks: dict, typos_cfg: dict, pipeline: dict) -> None:
    ruff_repo = next(repo for repo in hooks["repos"] if "ruff-pre-commit" in repo["repo"])
    ruff_repo["rev"] = "v0.14.0"


def _mutate_typos_rev(project: dict, hooks: dict, typos_cfg: dict, pipeline: dict) -> None:
    typos_repo = next(repo for repo in hooks["repos"] if "crate-ci/typos" in repo["repo"])
    typos_repo["rev"] = "v1.29.0"


def _mutate_unpinned_lint_typos(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    deps = project["tool"]["hatch"]["envs"]["lint"]["dependencies"]
    project["tool"]["hatch"]["envs"]["lint"]["dependencies"] = [
        "typos" if d.startswith("typos==") else d for d in deps
    ]


def _mutate_legacy_ruff_hook_id(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    ruff_repo = next(repo for repo in hooks["repos"] if "ruff-pre-commit" in repo["repo"])
    ruff_repo["hooks"][0]["id"] = "ruff"


def _mutate_up038_ignore(project: dict, hooks: dict, typos_cfg: dict, pipeline: dict) -> None:
    ruff_repo = next(repo for repo in hooks["repos"] if "ruff-pre-commit" in repo["repo"])
    ruff_repo["hooks"][0]["args"].append("--ignore=UP038")


def _mutate_mutating_typos_hook(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    typos_repo = next(repo for repo in hooks["repos"] if "crate-ci/typos" in repo["repo"])
    typos_repo["hooks"][0]["args"] = ["--write-changes"]


def _mutate_typos_diff_flag(project: dict, hooks: dict, typos_cfg: dict, pipeline: dict) -> None:
    project["tool"]["hatch"]["envs"]["lint"]["scripts"]["typos-check"] = "typos --diff {args:.}"


def _mutate_missing_soid(project: dict, hooks: dict, typos_cfg: dict, pipeline: dict) -> None:
    typos_cfg["default"]["extend-words"].pop("SOID", None)


def _mutate_missing_vas(project: dict, hooks: dict, typos_cfg: dict, pipeline: dict) -> None:
    typos_cfg["default"]["extend-words"].pop("VAS", None)


def _mutate_missing_tui_ignores(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    project["tool"]["ruff"]["lint"]["per-file-ignores"].pop(
        "packages/plugins-tui/tests/**/*.py", None
    )


def _mutate_weakened_ruff_select(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    project["tool"]["ruff"]["lint"]["select"].remove("SLF")


def _mutate_package_scoped_fmt(project: dict, hooks: dict, typos_cfg: dict, pipeline: dict) -> None:
    project["tool"]["hatch"]["envs"]["lint"]["scripts"]["fmt"] = (
        "ruff format --check {args:packages}"
    )


def _mutate_quality_all_packages_arg(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    project["tool"]["hatch"]["envs"]["quality"]["scripts"]["all"][0] = (
        "hatch run lint:all {args:packages}"
    )


def _mutate_widened_mypy_scope(project: dict, hooks: dict, typos_cfg: dict, pipeline: dict) -> None:
    project["tool"]["hatch"]["envs"]["lint"]["scripts"]["mypy-check"] = "mypy {args:.}"


def _mutate_reintroduced_global_lint(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["global-lint"] = {"name": "Global Code Quality Check", "steps": []}


def _mutate_pre_commit_pip_install(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["pre-commit"]["steps"].append({"run": "pip install ruff"})


def _mutate_pre_commit_unpinned_uvx(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    for step in pipeline["jobs"]["pre-commit"]["steps"]:
        if "run" in step:
            step["run"] = "uvx pre-commit run --all-files --show-diff-on-failure"


def _mutate_quality_waits_for_pre_commit(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["quality"]["needs"] = ["pre-commit"]


def _mutate_quality_missing_run_quality_if(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["quality"].pop("if", None)


def _mutate_duplicate_ruff_in_quality(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["quality"]["steps"].append({"run": "uv run ruff check ."})


def _mutate_duplicate_typos_in_quality(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["quality"]["steps"].append({"run": "uv run typos --force-exclude ."})


def _mutate_duplicate_secret_scan_in_quality(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["quality"]["steps"].append(
        {"run": 'uvx --from "detect-secrets==1.5.0" python scripts/run_secret_scan.py .'}
    )


def _mutate_duplicate_pytest_in_quality(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["quality"]["steps"].append({"run": "uv run pytest -q"})


def _mutate_build_waits_for_quality(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["build"]["needs"] = ["plan", "quality"]


def _mutate_build_gated_on_test_matrix(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["build"]["if"] = "needs.plan.outputs.matrix != '[]'"


def _mutate_consumption_gated_on_test_matrix(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["validate-package-consumption"]["if"] = "needs.plan.outputs.matrix != '[]'"


def _mutate_fuzzing_missing_run_fuzzing_gate(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["fuzzing"]["if"] = "needs.plan.outputs.matrix != '[]'"


def _mutate_test_missing_skipped_build_allowance(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["test"]["if"] = "needs.plan.outputs.matrix != '[]'"


def _mutate_core_sinks_ignore_glob(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    run_step = next(s for s in pipeline["jobs"]["test"]["steps"] if s.get("name") == "Run Tests")
    run_step["run"] = run_step["run"].replace(
        "--ignore-glob=*workflows*",
        "--ignore-glob=*workflows* --ignore-glob=*sinks*",
    )


def _mutate_missing_ci_gate(project: dict, hooks: dict, typos_cfg: dict, pipeline: dict) -> None:
    pipeline["jobs"].pop("ci-gate", None)


def _mutate_ci_gate_bare_always(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["ci-gate"]["if"] = "always()"


def _mutate_missing_sinks_export_filter(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    filter_step = next(s for s in pipeline["jobs"]["plan"]["steps"] if s.get("id") == "filter")
    filter_step["with"]["filters"] = filter_step["with"]["filters"].replace(
        "  - 'packages/plugins-sinks/**'\n", ""
    )


def _mutate_build_selective_upload(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    upload_step = next(
        s for s in pipeline["jobs"]["build"]["steps"] if s.get("name") == "Upload Core Artifact"
    )
    upload_step["if"] = "contains(fromJSON(needs.plan.outputs.build_matrix), 'core')"


def _mutate_test_hatch_run(project: dict, hooks: dict, typos_cfg: dict, pipeline: dict) -> None:
    run_step = next(s for s in pipeline["jobs"]["test"]["steps"] if s.get("name") == "Run Tests")
    run_step["run"] = run_step["run"].replace(".venv/bin/pytest", "hatch run test")


def _mutate_test_exit_code_5_bypass(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    run_step = next(s for s in pipeline["jobs"]["test"]["steps"] if s.get("name") == "Run Tests")
    run_step["run"] += '\n|| { RET=$?; if [ $RET -eq 5 ]; then exit 0; fi; exit $RET; }'


def _mutate_detect_secrets_rev(project: dict, hooks: dict, typos_cfg: dict, pipeline: dict) -> None:
    secrets_repo = next(repo for repo in hooks["repos"] if "Yelp/detect-secrets" in repo["repo"])
    secrets_repo["rev"] = "v1.4.0"


def _mutate_unpinned_lint_detect_secrets(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    deps = project["tool"]["hatch"]["envs"]["lint"]["dependencies"]
    project["tool"]["hatch"]["envs"]["lint"]["dependencies"] = [
        "detect-secrets" if d.startswith("detect-secrets==") else d for d in deps
    ]


def _mutate_lint_detect_secrets_version_drift(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    deps = project["tool"]["hatch"]["envs"]["lint"]["dependencies"]
    project["tool"]["hatch"]["envs"]["lint"]["dependencies"] = [
        "detect-secrets==1.4.0" if d.startswith("detect-secrets==") else d for d in deps
    ]


def _mutate_lint_secrets_raw_hook_directory(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    project["tool"]["hatch"]["envs"]["lint"]["scripts"]["secrets"] = (
        "detect-secrets-hook --baseline .secrets.baseline {args:.}"
    )


def _mutate_lint_secrets_package_scoped(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    project["tool"]["hatch"]["envs"]["lint"]["scripts"]["secrets"] = (
        "python scripts/run_secret_scan.py {args:packages}"
    )


def _mutate_lint_all_missing_secrets(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    project["tool"]["hatch"]["envs"]["lint"]["scripts"]["all"].remove("secrets")


@pytest.mark.parametrize(
    "mutator",
    [
        _mutate_ruff_rev,
        _mutate_typos_rev,
        _mutate_unpinned_lint_typos,
        _mutate_detect_secrets_rev,
        _mutate_unpinned_lint_detect_secrets,
        _mutate_lint_detect_secrets_version_drift,
        _mutate_lint_secrets_raw_hook_directory,
        _mutate_lint_secrets_package_scoped,
        _mutate_lint_all_missing_secrets,
        _mutate_legacy_ruff_hook_id,
        _mutate_up038_ignore,
        _mutate_mutating_typos_hook,
        _mutate_typos_diff_flag,
        _mutate_missing_soid,
        _mutate_missing_vas,
        _mutate_missing_tui_ignores,
        _mutate_weakened_ruff_select,
        _mutate_package_scoped_fmt,
        _mutate_quality_all_packages_arg,
        _mutate_widened_mypy_scope,
        _mutate_reintroduced_global_lint,
        _mutate_pre_commit_pip_install,
        _mutate_pre_commit_unpinned_uvx,
        _mutate_quality_waits_for_pre_commit,
        _mutate_quality_missing_run_quality_if,
        _mutate_duplicate_ruff_in_quality,
        _mutate_duplicate_typos_in_quality,
        _mutate_duplicate_secret_scan_in_quality,
        _mutate_duplicate_pytest_in_quality,
        _mutate_build_waits_for_quality,
        _mutate_build_gated_on_test_matrix,
        _mutate_consumption_gated_on_test_matrix,
        _mutate_fuzzing_missing_run_fuzzing_gate,
        _mutate_test_missing_skipped_build_allowance,
        _mutate_core_sinks_ignore_glob,
        _mutate_missing_ci_gate,
        _mutate_ci_gate_bare_always,
        _mutate_missing_sinks_export_filter,
        _mutate_build_selective_upload,
        _mutate_test_hatch_run,
        _mutate_test_exit_code_5_bypass,
    ],
)
def test_guardrail_manifest_contract_rejects_simulated_drift(mutator) -> None:
    project, hooks, typos_cfg, pipeline = (copy.deepcopy(m) for m in _load_guardrail_manifests())
    mutator(project, hooks, typos_cfg, pipeline)
    with pytest.raises(AssertionError):
        _assert_guardrail_contract(project, hooks, typos_cfg, pipeline)


def test_verification_docs_and_skills_specify_workspace_quality_all() -> None:
    checked_paths = [
        ROOT / "docs/how-to/verification.rst",
        ROOT / ".agents/skills/README.md",
        ROOT / ".agents/skills/verification-before-completion/SKILL.md",
        ROOT / ".agents/skills/workflow-guide/SKILL.md",
    ]
    for path in checked_paths:
        text = path.read_text(encoding="utf-8")
        assert "uvx hatch run quality:all" in text, f"Missing workspace quality:all in {path}"
        assert "quality:all packages" not in text, f"Stale package-scoped quality:all in {path}"

    for skill_doc in sorted((ROOT / ".agents/skills").rglob("*.md")):
        text = skill_doc.read_text(encoding="utf-8")
        assert "quality:all packages" not in text, (
            f"Stale package-scoped quality:all in {skill_doc}"
        )


def test_all_workflows_enforce_sha_pinning_least_privilege_concurrency_and_safe_caching() -> None:
    sha_action_re = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
    workflow_paths = sorted((ROOT / ".github/workflows").glob("*.yml"))
    assert len(workflow_paths) >= 7

    for wf_path in workflow_paths:
        wf_name = wf_path.name
        wf = yaml.safe_load(wf_path.read_text(encoding="utf-8"))

        assert "concurrency" in wf, f"{wf_name} missing workflow concurrency"
        assert "group" in wf["concurrency"], f"{wf_name} missing concurrency.group"
        cancel_cfg = wf["concurrency"].get("cancel-in-progress")
        assert isinstance(cancel_cfg, bool) or (
            cancel_cfg == "${{ github.event_name == 'pull_request' }}"
        ), f"{wf_name} invalid concurrency.cancel-in-progress: {cancel_cfg!r}"

        assert "permissions" in wf, f"{wf_name} missing top-level permissions"
        expected_top_perms = {"contents": "read"} if wf_name == "live-tests.yml" else {}
        assert wf["permissions"] == expected_top_perms, (
            f"{wf_name} unexpected top-level permissions: {wf['permissions']}"
        )

        triggers = wf.get("on") or wf.get(True) or {}

        for job_id, job in wf["jobs"].items():
            ctx = f"{wf_name}:{job_id}"
            assert isinstance(job.get("name"), str) and job["name"].strip(), (
                f"{ctx} missing explicit job name"
            )
            assert isinstance(job.get("permissions"), dict), f"{ctx} missing job permissions"
            if wf_name == "main.yml" and job_id == "scorecard":
                assert job.get("if") == "github.event_name != 'pull_request'"
            job_if = " ".join(str(job.get("if", "")).split())
            if "always()" in job_if:
                assert "!cancelled()" in job_if, (
                    f"{ctx} uses job-level always() without !cancelled(), "
                    "which overrides concurrency cancel-in-progress"
                )

            steps = job.get("steps", [])
            for step in steps:
                step_label = f"{ctx}:{step.get('name', step.get('uses', 'unnamed'))}"
                if "uses" in step:
                    uses = str(step["uses"])
                    assert sha_action_re.match(uses), f"{step_label} not SHA-pinned: {uses}"
                    assert not uses.startswith("actions/cache@"), (
                        f"{step_label} uses combined actions/cache instead of explicit restore/save"
                    )
                    if uses.startswith("actions/checkout@"):
                        assert step.get("with", {}).get("persist-credentials") is False, (
                            f"{step_label} must set persist-credentials: false"
                        )
                    if uses.startswith("actions/cache/restore@") and ".venv" in str(
                        step.get("with", {}).get("path", "")
                    ):
                        key = str(step["with"]["key"])
                        assert '"' not in key, f"{step_label} cache key contains quotes: {key}"
                        assert key.startswith("uv-venv-${{ runner.os }}-py"), (
                            f"{step_label} unexpected venv cache key prefix: {key}"
                        )
                        assert key.endswith("-${{ hashFiles('uv.lock') }}"), (
                            f"{step_label} unexpected venv cache key suffix: {key}"
                        )
                    if uses.startswith("actions/cache/save@"):
                        cond = str(step.get("if", ""))
                        if wf_name == "fuzzing-deep.yml":
                            assert "pull_request" not in triggers, (
                                f"{step_label} workflow must not run on pull_request"
                            )
                        else:
                            assert (
                                "github.event_name == 'push'" in cond
                                and "github.ref == 'refs/heads/main'" in cond
                            ), f"{step_label} must restrict cache saves to push on refs/heads/main"

                if "run" in step:
                    script = str(step["run"])
                    assert "${{" not in script, (
                        f"{step_label} contains unsafe template interpolation in run script"
                    )
                    assert not re.search(r"(?<!uv )pip install", script), (
                        f"{step_label} uses unpinned pip install instead of uv/uvx"
                    )
                    if "GITHUB_PATH" in script:
                        assert '"$GITHUB_PATH"' in script, f"{step_label} must quote $GITHUB_PATH"

    main_wf = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))
    test_step_names = [s.get("name", "") for s in main_wf["jobs"]["test"]["steps"]]
    assert test_step_names.index("Save Python 3.13 Workspace Dependencies") < test_step_names.index(
        "Install Built Wheels"
    )

    docs_runs = "\n".join(s.get("run", "") for s in main_wf["jobs"]["docs"]["steps"] if "run" in s)
    assert docs_runs.count('"playwright==') >= 2
    assert "--with playwright " not in docs_runs

    quality_runs = "\n".join(
        s.get("run", "") for s in main_wf["jobs"]["quality"]["steps"] if "run" in s
    )
    for pinned_tool in (
        '"pip-audit==',
        '"semgrep==',
        '"vulture==',
        '"pylint==',
    ):
        assert pinned_tool in quality_runs, f"Missing pinned {pinned_tool} in quality job"


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="Bash and jq are needed to verify plan and ci-gate scripts",
)
def test_ci_plan_outputs_and_ci_gate_evaluate_upstream_results(tmp_path: Path) -> None:
    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))
    set_matrix_step = next(
        s for s in pipeline["jobs"]["plan"]["steps"] if s.get("id") == "set-matrix"
    )
    for changed_csv, expected_quality, expected_build, expected_test in (
        ("docs/index.rst,AGENTS.md", "false", "[]", "[]"),
        ("examples/demo.py", "true", "[]", "[]"),
        ("tests/unit/test_export_sinks_and_schema_caches.py", "true", "[]", '["core"]'),
    ):
        out_file = tmp_path / "github_output.txt"
        out_file.write_text("", encoding="utf-8")
        subprocess.run(
            [shutil.which("bash"), "-euo", "pipefail", "-c", set_matrix_step["run"]],
            cwd=ROOT,
            env={
                **os.environ,
                "ALL_CHANGED_FILES": changed_csv,
                "GITHUB_OUTPUT": str(out_file),
            },
            check=True,
            capture_output=True,
            text=True,
        )
        outputs = dict(
            line.split("=", 1) for line in out_file.read_text(encoding="utf-8").splitlines()
        )
        assert outputs["run_quality"] == expected_quality
        assert outputs["build_matrix"] == expected_build
        assert outputs["matrix"] == expected_test

    ci_gate_if = " ".join(str(pipeline["jobs"]["ci-gate"].get("if", "")).split())

    def _eval_ci_gate_if(expr: str, *, workflow_cancelled: bool) -> bool:
        tokens = {part.strip() for part in expr.split("&&")}
        if "always()" not in tokens:
            return False
        if "!cancelled()" in tokens and workflow_cancelled:
            return False
        return True

    assert _eval_ci_gate_if(ci_gate_if, workflow_cancelled=True) is False
    assert _eval_ci_gate_if(ci_gate_if, workflow_cancelled=False) is True

    gate_script = pipeline["jobs"]["ci-gate"]["steps"][0]["run"]
    for needs_payload, expected_rc in (
        (
            '{"plan":{"result":"success"},"pre-commit":{"result":"success"},'
            '"quality":{"result":"skipped"},"build":{"result":"skipped"},'
            '"test":{"result":"skipped"},"docs":{"result":"success"}}',
            0,
        ),
        (
            '{"plan":{"result":"success"},"build":{"result":"failure"},"test":{"result":"skipped"}}',
            1,
        ),
        (
            '{"plan":{"result":"success"},"quality":{"result":"cancelled"}}',
            1,
        ),
    ):
        res = subprocess.run(
            [shutil.which("bash"), "-euo", "pipefail", "-c", gate_script],
            env={**os.environ, "NEEDS_JSON": needs_payload},
            check=False,
            capture_output=True,
            text=True,
        )
        assert res.returncode == expected_rc
