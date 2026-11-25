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
UNQUOTED_VENV_KEY_RE = re.compile(
    r"^uv-venv-\$\{\{\s*runner\.os\s*\}\}-"
    r"(?:3\.11|3\.12|3\.13|\$\{\{\s*matrix\.python-version\s*\}\})-"
    r"\$\{\{\s*hashFiles\('uv\.lock'\)\s*\}\}$"
)
UNQUOTED_VENV_RESTORE_RE = re.compile(
    r"^uv-venv-\$\{\{\s*runner\.os\s*\}\}-"
    r"(?:3\.11|3\.12|3\.13|\$\{\{\s*matrix\.python-version\s*\}\})-$"
)
UNPINNED_PIP_INSTALL_RE = re.compile(r"(?<!uv\s)pip\s+install")
VENV_WORKFLOW_PATHS = (
    ".github/workflows/main.yml",
    ".github/workflows/fuzzing-deep.yml",
    ".github/workflows/performance.yml",
    ".github/workflows/live-tests.yml",
    ".github/workflows/copilot-setup-steps.yml",
)
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
    assert jobs["global-lint"]["name"] == "Global Code Quality Check"
    assert jobs["pre-commit"]["name"] == "Pre-commit Quality Checks"
    assert jobs["quality"]["name"] == "Workspace Quality & Security"
    assert jobs["deploy-docs"]["name"] == "Deploy to GH Pages"

    for job_key in ("global-lint", "pre-commit", "quality"):
        job_steps = jobs[job_key]["steps"]
        assert any(SHA_SETUP_UV_RE.match(str(s.get("uses", ""))) for s in job_steps)
        job_runs = [s.get("run", "") for s in job_steps if "run" in s]
        assert not any("pip install" in r for r in job_runs)

    global_lint_runs = "\n".join(
        s.get("run", "") for s in jobs["global-lint"]["steps"] if "run" in s
    )
    assert f'uvx --from "{ruff_pin}" ruff format --check .' in global_lint_runs
    assert f'uvx --from "{ruff_pin}" ruff check .' in global_lint_runs
    assert "--select" not in global_lint_runs
    assert f'uvx --from "{typos_pin}" typos --force-exclude .' in global_lint_runs
    assert "--diff" not in global_lint_runs
    assert "--write-changes" not in global_lint_runs

    pre_commit_runs = "\n".join(s.get("run", "") for s in jobs["pre-commit"]["steps"] if "run" in s)
    assert re.search(
        r'uvx --from "pre-commit==\d+\.\d+\.\d+" pre-commit run --all-files --show-diff-on-failure',
        pre_commit_runs,
    )

    quality_needs = jobs["quality"].get("needs", [])
    if isinstance(quality_needs, str):
        quality_needs = [quality_needs]
    assert "pre-commit" in set(quality_needs)

    quality_runs = [s.get("run", "") for s in jobs["quality"]["steps"] if "run" in s]
    assert any(
        "uv run ruff format --check ." in r
        and "uv run ruff check ." in r
        and "uv run typos --force-exclude ." in r
        for r in quality_runs
    )
    assert not any("typos" in r and "--diff" in r for r in quality_runs)

    filter_step = next(s for s in jobs["plan"]["steps"] if s.get("id") == "filter")
    export_patterns = yaml.safe_load(filter_step["with"]["filters"])["export_logic"]
    assert "packages/plugins-sinks/**" in export_patterns

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
    assert "hatch run" not in run_tests_step["run"]
    assert "Passing gracefully" not in run_tests_step["run"]
    assert "-eq 5" not in run_tests_step["run"]

    _assert_workflow_venv_cache_contract({".github/workflows/main.yml": pipeline})


def _cache_step_paths(step: dict) -> list[str]:
    raw_path = str(step.get("with", {}).get("path", ""))
    return [line.strip() for line in raw_path.splitlines() if line.strip()]


def _is_cache_step(step: dict) -> bool:
    uses = str(step.get("uses", ""))
    return uses.startswith(("actions/cache@", "actions/cache/restore@", "actions/cache/save@"))


def _assert_workflow_venv_cache_contract(workflows: dict[str, dict]) -> None:
    for wf_path, wf_doc in workflows.items():
        for job_key, job in wf_doc.get("jobs", {}).items():
            steps = job.get("steps", [])
            job_runs = [str(s.get("run", "")) for s in steps if "run" in s]

            assert not any(UNPINNED_PIP_INSTALL_RE.search(r) for r in job_runs), (
                f"Unpinned pip install found in {wf_path}:{job_key}"
            )
            if any(
                tok in r for r in job_runs for tok in ("uv ", "uvx ", "pytest", "validate_package")
            ):
                assert any(SHA_SETUP_UV_RE.match(str(s.get("uses", ""))) for s in steps), (
                    f"Missing SHA-pinned astral-sh/setup-uv in {wf_path}:{job_key}"
                )

            restores_venv = False
            for step in steps:
                if not _is_cache_step(step):
                    continue
                uses = str(step.get("uses", ""))
                with_cfg = step.get("with", {})
                key = str(with_cfg.get("key", "")).strip()
                restore_keys = [
                    line.strip()
                    for line in str(with_cfg.get("restore-keys", "")).splitlines()
                    if line.strip()
                ]
                assert '"' not in key, f"Quoted cache key in {wf_path}:{job_key}: {key}"
                assert all('"' not in rk for rk in restore_keys), (
                    f"Quoted restore-keys in {wf_path}:{job_key}: {restore_keys}"
                )

                paths = _cache_step_paths(step)
                if ".venv" in paths:
                    assert UNQUOTED_VENV_KEY_RE.match(key), (
                        f"Non-canonical .venv cache key in {wf_path}:{job_key}: {key}"
                    )
                    if uses.startswith(("actions/cache@", "actions/cache/restore@")):
                        restores_venv = True
                        assert restore_keys and all(
                            UNQUOTED_VENV_RESTORE_RE.match(rk) for rk in restore_keys
                        ), (
                            f"Non-canonical .venv restore-keys in {wf_path}:{job_key}: "
                            f"{restore_keys}"
                        )
                else:
                    assert not key.startswith("uv-venv-"), (
                        f"Non-.venv cache step must not use uv-venv- prefix in {wf_path}:{job_key}"
                    )

            if restores_venv:
                assert job_key not in {"build", "validate-package-consumption"}, (
                    f"Unused .venv restore in {wf_path}:{job_key}"
                )
                assert any("uv sync --locked --all-packages --all-extras" in r for r in job_runs), (
                    f"Job restoring .venv must sync locked workspace in {wf_path}:{job_key}"
                )
                if job_key != "copilot-setup-steps":
                    assert any(
                        tok in r for r in job_runs for tok in ("uv run", ".venv/bin", "pytest")
                    ), f"Unused .venv restore without consumer command in {wf_path}:{job_key}"

    main_wf = workflows.get(".github/workflows/main.yml")
    if main_wf is None:
        return

    main_jobs = main_wf["jobs"]

    build_steps = main_jobs["build"]["steps"]
    build_runs = [str(s.get("run", "")) for s in build_steps if "run" in s]
    assert not any(".venv" in _cache_step_paths(s) for s in build_steps if _is_cache_step(s))
    assert not any("uv sync" in r for r in build_runs)
    assert not any("hatch build" in r for r in build_runs)
    assert any("uv build" in r for r in build_runs)

    test_steps = main_jobs["test"]["steps"]
    assert not any(
        str(s.get("uses", "")).startswith("actions/cache@") and ".venv" in _cache_step_paths(s)
        for s in test_steps
    ), "Unified actions/cache in test would save wheel-mutated .venv at job teardown"

    restore_idx = next(
        i
        for i, s in enumerate(test_steps)
        if str(s.get("uses", "")).startswith("actions/cache/restore@")
        and ".venv" in _cache_step_paths(s)
    )
    sync_idx = next(
        i
        for i, s in enumerate(test_steps)
        if "uv sync --locked --all-packages --all-extras" in str(s.get("run", ""))
    )
    save_idx = next(
        i
        for i, s in enumerate(test_steps)
        if str(s.get("uses", "")).startswith("actions/cache/save@")
        and ".venv" in _cache_step_paths(s)
    )
    wheel_indices = [
        i
        for i, s in enumerate(test_steps)
        if "uv pip install --force-reinstall --no-deps dist/*.whl" in str(s.get("run", ""))
    ]
    run_tests_idx = next(i for i, s in enumerate(test_steps) if s.get("name") == "Run Tests")

    assert len(wheel_indices) == 1
    wheel_idx = wheel_indices[0]
    assert restore_idx < sync_idx < save_idx < wheel_idx < run_tests_idx
    save_if = str(test_steps[save_idx].get("if", ""))
    assert "cache-hit != 'true'" in save_if
    assert not any(
        "dist/*.whl" in str(s.get("run", "")) for s in test_steps[: save_idx + 1] if "run" in s
    )

    consumption_steps = main_jobs["validate-package-consumption"]["steps"]
    consumption_runs = [str(s.get("run", "")) for s in consumption_steps if "run" in s]
    consumption_caches = [s for s in consumption_steps if _is_cache_step(s)]
    assert len(consumption_caches) == 1
    assert _cache_step_paths(consumption_caches[0]) == ["~/.cache/uv"]
    assert not any("uv sync" in r for r in consumption_runs)


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
    assert any("uv run typos --force-exclude ." in r for r in run_blocks)
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
    workflows = {
        rel_path: yaml.safe_load((ROOT / rel_path).read_text(encoding="utf-8"))
        for rel_path in VENV_WORKFLOW_PATHS
    }
    _assert_workflow_venv_cache_contract(workflows)

    copilot = workflows[".github/workflows/copilot-setup-steps.yml"]
    copilot_steps = copilot["jobs"]["copilot-setup-steps"]["steps"]
    copilot_runs = [s.get("run", "") for s in copilot_steps if "run" in s]
    assert any(SHA_SETUP_UV_RE.match(str(s.get("uses", ""))) for s in copilot_steps)
    assert any("uv sync --locked --all-packages --all-extras" in r for r in copilot_runs)
    assert not any(UNPINNED_PIP_INSTALL_RE.search(r) for r in copilot_runs)

    pipeline = workflows[".github/workflows/main.yml"]
    assert "validate-extras" not in pipeline["jobs"]

    consumption_job = pipeline["jobs"]["validate-package-consumption"]
    assert set(consumption_job["needs"]) == {"plan", "build"}
    assert consumption_job.get("if") == "needs.plan.outputs.matrix != '[]'"

    consumption_steps = consumption_job["steps"]
    download_step = next(
        s for s in consumption_steps if s.get("name") == "Download Built Artifacts"
    )
    assert download_step.get("if") == "needs.plan.outputs.build_matrix != '[]'"
    assert download_step.get("uses", "").startswith("actions/download-artifact@")

    consumption_runs = [s.get("run", "") for s in consumption_steps if "run" in s]
    assert any(SHA_SETUP_UV_RE.match(str(s.get("uses", ""))) for s in consumption_steps)
    assert not any("uv sync" in r for r in consumption_runs)
    assert any(
        "scripts/validate_package_consumption.py --wheel-dir dist" in r for r in consumption_runs
    )
    assert not any("pip install" in r for r in consumption_runs)

    # Confirm quoted cache keys in non-main workflows are also rejected
    mutated_deep = copy.deepcopy(workflows)
    deep_restore = next(
        s
        for s in mutated_deep[".github/workflows/fuzzing-deep.yml"]["jobs"]["deep-fuzzing"]["steps"]
        if str(s.get("uses", "")).startswith("actions/cache/restore@")
    )
    deep_restore["with"]["key"] = 'uv-venv-${{ runner.os }}-"3.12"-${{ hashFiles(\'uv.lock\') }}'
    with pytest.raises(AssertionError, match="Quoted cache key"):
        _assert_workflow_venv_cache_contract(mutated_deep)


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
    assert "needs.plan.outputs.run_integration == 'true'" in sandbox_job["if"]


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


def _mutate_global_lint_pip_install(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["global-lint"]["steps"].append({"run": "pip install ruff"})


def _mutate_global_lint_subset_select(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    for step in pipeline["jobs"]["global-lint"]["steps"]:
        if "run" in step:
            step["run"] += "\nruff check . --select F401,F841"


def _mutate_pre_commit_unpinned_uvx(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    for step in pipeline["jobs"]["pre-commit"]["steps"]:
        if "run" in step:
            step["run"] = "uvx pre-commit run --all-files --show-diff-on-failure"


def _mutate_quality_missing_needs(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["quality"]["needs"] = []


def _mutate_quality_missing_typos(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    for step in pipeline["jobs"]["quality"]["steps"]:
        if "run" in step:
            step["run"] = step["run"].replace("uv run typos --force-exclude .\n", "")


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


def _mutate_quoted_venv_cache_key(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    cache_step = next(s for s in pipeline["jobs"]["quality"]["steps"] if _is_cache_step(s))
    cache_step["with"]["key"] = 'uv-venv-${{ runner.os }}-"3.11"-${{ hashFiles(\'uv.lock\') }}'


def _mutate_test_post_wheel_venv_save(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    test_steps = pipeline["jobs"]["test"]["steps"]
    save_idx = next(
        i
        for i, s in enumerate(test_steps)
        if str(s.get("uses", "")).startswith("actions/cache/save@")
    )
    wheel_idx = next(
        i
        for i, s in enumerate(test_steps)
        if "uv pip install --force-reinstall --no-deps dist/*.whl" in str(s.get("run", ""))
    )
    save_step = test_steps.pop(save_idx)
    test_steps.insert(wheel_idx, save_step)


def _mutate_test_unified_cache_post_wheel_save(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    test_steps = pipeline["jobs"]["test"]["steps"]
    restore_step = next(
        s for s in test_steps if str(s.get("uses", "")).startswith("actions/cache/restore@")
    )
    restore_step["uses"] = restore_step["uses"].replace("actions/cache/restore@", "actions/cache@")


def _mutate_build_unused_venv_restore(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    pipeline["jobs"]["build"]["steps"].insert(
        2,
        {
            "name": "Cache Workspace Dependencies",
            "uses": "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
            "with": {
                "path": "~/.cache/uv\n.venv",
                "key": "uv-venv-${{ runner.os }}-3.11-${{ hashFiles('uv.lock') }}",
                "restore-keys": "uv-venv-${{ runner.os }}-3.11-",
            },
        },
    )


def _mutate_consumption_unused_venv_restore(
    project: dict, hooks: dict, typos_cfg: dict, pipeline: dict
) -> None:
    cache_step = next(
        s for s in pipeline["jobs"]["validate-package-consumption"]["steps"] if _is_cache_step(s)
    )
    cache_step["with"]["path"] = "~/.cache/uv\n.venv"
    cache_step["with"]["key"] = "uv-venv-${{ runner.os }}-3.11-${{ hashFiles('uv.lock') }}"
    cache_step["with"]["restore-keys"] = "uv-venv-${{ runner.os }}-3.11-"


@pytest.mark.parametrize(
    "mutator",
    [
        _mutate_ruff_rev,
        _mutate_typos_rev,
        _mutate_unpinned_lint_typos,
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
        _mutate_global_lint_pip_install,
        _mutate_global_lint_subset_select,
        _mutate_pre_commit_unpinned_uvx,
        _mutate_quality_missing_needs,
        _mutate_quality_missing_typos,
        _mutate_missing_sinks_export_filter,
        _mutate_build_selective_upload,
        _mutate_test_hatch_run,
        _mutate_test_exit_code_5_bypass,
        _mutate_quoted_venv_cache_key,
        _mutate_test_post_wheel_venv_save,
        _mutate_test_unified_cache_post_wheel_save,
        _mutate_build_unused_venv_restore,
        _mutate_consumption_unused_venv_restore,
    ],
)
def test_guardrail_manifest_contract_rejects_simulated_drift(mutator) -> None:
    project, hooks, typos_cfg, pipeline = (copy.deepcopy(m) for m in _load_guardrail_manifests())
    mutator(project, hooks, typos_cfg, pipeline)
    with pytest.raises(AssertionError):
        _assert_guardrail_contract(project, hooks, typos_cfg, pipeline)
