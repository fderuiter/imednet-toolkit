"""Guard shared verification tasks and public package metadata against drift."""

import copy
import json
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
    assert "merge_group" in triggers
    assert "workflow_dispatch" in triggers

    assert "semantic-pr-title" not in pipeline["jobs"]
    assert pipeline["jobs"]["scorecard"]["if"] == "github.event_name != 'merge_group'"

    ci_gate = pipeline["jobs"]["ci-gate"]
    assert ci_gate["name"] == "CI Gate"
    assert ci_gate["if"] == "always()"
    assert ci_gate["permissions"] == {}
    assert set(ci_gate["needs"]) == {
        "global-lint",
        "pre-commit",
        "plan",
        "scorecard",
        "quality",
        "build",
        "test",
        "integration-sandbox",
        "fuzzing",
        "validate-package-consumption",
        "docs",
    }

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


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash is needed for workflow scripts")
def test_ci_gate_executes_terminal_pass_skip_and_failure_semantics(tmp_path: Path) -> None:
    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))
    gate_step = pipeline["jobs"]["ci-gate"]["steps"][0]
    assert gate_step["if"] == "always()"
    script = gate_step["run"]

    def make_needs(
        *,
        matrix: str = '["core"]',
        run_integration: str = "true",
        overrides: dict[str, str] | None = None,
    ) -> dict:
        base = {
            job: {"result": "success", "outputs": {}}
            for job in pipeline["jobs"]["ci-gate"]["needs"]
        }
        base["plan"]["outputs"] = {"matrix": matrix, "run_integration": run_integration}
        if overrides:
            for job, res in overrides.items():
                base[job]["result"] = res
        return base

    def run_gate(
        needs_payload: dict, event_name: str = "pull_request"
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [shutil.which("bash") or "bash", "-e", "-c", script],
            cwd=tmp_path,
            env={
                **os.environ,
                "EVENT_NAME": event_name,
                "NEEDS_JSON": json.dumps(needs_payload),
            },
            capture_output=True,
            text=True,
            check=False,
        )

    # Full pass
    assert run_gate(make_needs()).returncode == 0

    # Docs/markdown PR where ci_plan.py returns empty matrix
    docs_only_needs = make_needs(
        matrix="[]",
        run_integration="false",
        overrides={
            "build": "skipped",
            "test": "skipped",
            "integration-sandbox": "skipped",
            "fuzzing": "skipped",
            "validate-package-consumption": "skipped",
        },
    )
    assert run_gate(docs_only_needs).returncode == 0

    # Leaf-package PR where integration-sandbox is skipped
    leaf_needs = make_needs(
        matrix='["plugins-tui"]',
        run_integration="false",
        overrides={"integration-sandbox": "skipped"},
    )
    assert run_gate(leaf_needs).returncode == 0

    # merge_group run where scorecard is guarded/skipped
    mg_needs = make_needs(overrides={"scorecard": "skipped"})
    assert run_gate(mg_needs, event_name="merge_group").returncode == 0
    assert run_gate(mg_needs, event_name="pull_request").returncode == 1

    # Upstream failure or cancellation fails CI Gate
    assert run_gate(make_needs(overrides={"test": "failure"})).returncode == 1
    assert run_gate(make_needs(overrides={"fuzzing": "cancelled"})).returncode == 1

    # Unexpected skip of impacted job fails CI Gate
    assert run_gate(make_needs(overrides={"test": "skipped"})).returncode == 1
    assert run_gate(make_needs(overrides={"integration-sandbox": "skipped"})).returncode == 1


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="Bash and jq are needed for plan workflow script tests",
)
def test_plan_job_diffs_merge_group_base_and_head_sha(tmp_path: Path) -> None:
    pipeline = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))
    plan_steps = pipeline["jobs"]["plan"]["steps"]
    filter_step = next(s for s in plan_steps if s.get("id") == "filter")
    assert "merge_group.base_sha" in filter_step["with"]["base"]
    assert "merge_group.head_sha" in filter_step["with"]["ref"]

    set_matrix_step = next(s for s in plan_steps if s.get("id") == "set-matrix")
    assert (
        set_matrix_step["env"]["MERGE_GROUP_BASE_SHA"] == "${{ github.event.merge_group.base_sha }}"
    )
    assert (
        set_matrix_step["env"]["MERGE_GROUP_HEAD_SHA"] == "${{ github.event.merge_group.head_sha }}"
    )

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "ci_plan.py", repo / "scripts" / "ci_plan.py")
    for pkg_toml in (ROOT / "packages").glob("*/pyproject.toml"):
        dest = repo / "packages" / pkg_toml.parent.name / "pyproject.toml"
        dest.parent.mkdir(parents=True)
        shutil.copy2(pkg_toml, dest)

    git_bin = shutil.which("git") or "git"
    subprocess.run([git_bin, "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run([git_bin, "config", "user.email", "ci@example.com"], cwd=repo, check=True)
    subprocess.run([git_bin, "config", "user.name", "CI"], cwd=repo, check=True)
    subprocess.run([git_bin, "add", "."], cwd=repo, check=True)
    subprocess.run(
        [git_bin, "commit", "-m", "chore: initial"], cwd=repo, check=True, capture_output=True
    )
    base_sha = subprocess.check_output([git_bin, "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    # Commit 1: docs-only queued change
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.rst").write_text("Guide\n=====\n", encoding="utf-8")
    subprocess.run([git_bin, "add", "docs/guide.rst"], cwd=repo, check=True)
    subprocess.run(
        [git_bin, "commit", "-m", "docs: add guide (#2160)"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    docs_head_sha = subprocess.check_output(
        [git_bin, "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()

    out_docs = tmp_path / "docs_output.txt"
    out_docs.touch()
    subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", set_matrix_step["run"]],
        cwd=repo,
        env={
            **os.environ,
            "EVENT_NAME": "merge_group",
            "MERGE_GROUP_BASE_SHA": base_sha,
            "MERGE_GROUP_HEAD_SHA": docs_head_sha,
            "ALL_CHANGED_FILES": "",
            "GITHUB_OUTPUT": str(out_docs),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    docs_lines = dict(
        line.split("=", 1)
        for line in out_docs.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert json.loads(docs_lines["matrix"]) == []
    assert json.loads(docs_lines["build_matrix"]) == []

    # Commit 2: second PR in merge queue modifies plugins-sinks
    sinks_src = repo / "packages" / "plugins-sinks" / "src" / "imednet_sinks"
    sinks_src.mkdir(parents=True)
    (sinks_src / "graph.py").write_text("# sink change\n", encoding="utf-8")
    subprocess.run([git_bin, "add", "."], cwd=repo, check=True)
    subprocess.run(
        [git_bin, "commit", "-m", "fix(sinks): align sdtm cypher (#2161)"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sinks_head_sha = subprocess.check_output(
        [git_bin, "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()

    out_sinks = tmp_path / "sinks_output.txt"
    out_sinks.touch()
    subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", set_matrix_step["run"]],
        cwd=repo,
        env={
            **os.environ,
            "EVENT_NAME": "merge_group",
            "MERGE_GROUP_BASE_SHA": base_sha,
            "MERGE_GROUP_HEAD_SHA": sinks_head_sha,
            "ALL_CHANGED_FILES": "",
            "GITHUB_OUTPUT": str(out_sinks),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    sinks_lines = dict(
        line.split("=", 1)
        for line in out_sinks.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert json.loads(sinks_lines["matrix"]) == ["plugins-sinks"]
    assert json.loads(sinks_lines["build_matrix"]) == ["plugins-sinks"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash is needed for workflow scripts")
def test_pr_title_workflow_enforces_edited_bots_isolated_concurrency_and_merge_group(
    tmp_path: Path,
) -> None:
    pr_title_path = ROOT / ".github" / "workflows" / "pr-title.yml"
    raw = pr_title_path.read_text(encoding="utf-8")
    assert "[bot]" not in raw

    wf = yaml.safe_load(raw)
    triggers = _workflow_triggers(wf)
    assert set(triggers["pull_request"]["types"]) >= {
        "opened",
        "edited",
        "synchronize",
        "reopened",
    }
    assert "merge_group" in triggers

    main_wf = yaml.safe_load((ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8"))
    perf_wf = yaml.safe_load(
        (ROOT / ".github/workflows/performance.yml").read_text(encoding="utf-8")
    )
    pr_group = wf["concurrency"]["group"]
    assert pr_group.startswith("pr-title-")
    assert pr_group != main_wf["concurrency"]["group"]
    assert pr_group != perf_wf["concurrency"]["group"]
    assert wf["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert wf["permissions"] == {}

    job = wf["jobs"]["semantic-pr-title"]
    assert job["name"] == "Semantic PR Title"
    steps = job["steps"]
    for step in steps:
        if "uses" in step:
            assert len(step["uses"].split("@", 1)[1]) == 40

    pr_step = next(s for s in steps if s.get("name") == "Validate PR title")
    assert pr_step["if"] == "github.event_name == 'pull_request'"
    assert pr_step["uses"].startswith("amannn/action-semantic-pull-request@")
    allowed_types = [t.strip() for t in pr_step["with"]["types"].splitlines() if t.strip()]
    assert allowed_types == [
        "feat",
        "fix",
        "chore",
        "docs",
        "ci",
        "test",
        "refactor",
        "perf",
        "revert",
    ]

    mg_step = next(s for s in steps if s.get("name") == "Validate queued commit prefixes")
    assert mg_step["if"] == "github.event_name == 'merge_group'"

    repo = tmp_path / "mg_repo"
    repo.mkdir()
    git_bin = shutil.which("git") or "git"
    subprocess.run([git_bin, "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run([git_bin, "config", "user.email", "ci@example.com"], cwd=repo, check=True)
    subprocess.run([git_bin, "config", "user.name", "CI"], cwd=repo, check=True)
    subprocess.run(
        [git_bin, "commit", "--allow-empty", "-m", "chore: base"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.check_output([git_bin, "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    subprocess.run(
        [git_bin, "commit", "--allow-empty", "-m", "fix(sdtm): unify domain routing (#2135)"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [git_bin, "commit", "--allow-empty", "-m", "feat(ci)!: add merge queue gates (#2164)"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    valid_head = subprocess.check_output(
        [git_bin, "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()

    ok = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", mg_step["run"]],
        cwd=repo,
        env={
            **os.environ,
            "BASE_SHA": base_sha,
            "HEAD_SHA": valid_head,
            "HEAD_COMMIT_MSG": "feat(ci)!: add merge queue gates (#2164)",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr

    subprocess.run(
        [git_bin, "commit", "--allow-empty", "-m", "Update README without conventional prefix"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    bad_head = subprocess.check_output([git_bin, "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    bad = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", mg_step["run"]],
        cwd=repo,
        env={
            **os.environ,
            "BASE_SHA": base_sha,
            "HEAD_SHA": bad_head,
            "HEAD_COMMIT_MSG": "Update README without conventional prefix",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode != 0
    assert "Invalid Conventional Commit prefix" in bad.stderr


def test_required_checks_documented_in_contributing_and_agents() -> None:
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for required_check in ("CI Gate", "Performance Gate", "Semantic PR Title", "merge_group"):
        assert required_check in contributing
        assert required_check in agents
    assert "copilot-setup-steps.yml" in contributing


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
    assert consumption_job.get("if") == "needs.plan.outputs.matrix != '[]'"

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
    ],
)
def test_guardrail_manifest_contract_rejects_simulated_drift(mutator) -> None:
    project, hooks, typos_cfg, pipeline = (copy.deepcopy(m) for m in _load_guardrail_manifests())
    mutator(project, hooks, typos_cfg, pipeline)
    with pytest.raises(AssertionError):
        _assert_guardrail_contract(project, hooks, typos_cfg, pipeline)
