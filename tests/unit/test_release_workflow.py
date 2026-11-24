"""Unit tests for release workflow trigger matching and package dispatch."""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

sys.path.append(str(Path(__file__).resolve().parents[2] / "scripts"))
import tighten_dependencies as td

ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release.yml"
RELEASE_PLEASE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release-please.yml"
MAIN_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "main.yml"
MANIFEST_PATH = ROOT / ".release-please-manifest.json"
RELEASE_PLEASE_CONFIG_PATH = ROOT / "release-please-config.json"
RELEASE_APP_WIZARD_PATH = ROOT / "scripts" / "wizards" / "setup-release-please-app.sh"
PYPI_WIZARD_PATH = ROOT / "scripts" / "wizards" / "setup-pypi-oidc.sh"
CONTRIBUTING_PATH = ROOT / "CONTRIBUTING.md"
SHA_PINNED_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


@pytest.fixture
def release_workflow() -> dict:
    source = RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")
    return yaml.load(source, Loader=yaml.BaseLoader)


@pytest.fixture
def release_please_workflow() -> dict:
    source = RELEASE_PLEASE_WORKFLOW_PATH.read_text(encoding="utf-8")
    return yaml.load(source, Loader=yaml.BaseLoader)


@pytest.fixture
def determine_packages_script(release_workflow: dict) -> str:
    steps = release_workflow["jobs"]["plan"]["steps"]
    step = next(s for s in steps if s.get("id") == "determine-packages")
    return step["run"]


def test_release_workflow_tag_triggers(release_workflow: dict) -> None:
    """Verify that push tag triggers include imednet-tui-v*.*.* and all packages."""
    tags = release_workflow["on"]["push"]["tags"]
    assert "imednet-tui-v*.*.*" in tags
    assert "imednet-v*.*.*" in tags
    assert "imednet-workflows-v*.*.*" in tags
    assert "apache-airflow-providers-imednet-v*.*.*" in tags
    assert "imednet-plugins-sinks-v*.*.*" in tags
    assert "v*.*.*" in tags


def test_release_workflow_permissions(release_workflow: dict) -> None:
    """Verify top-level permissions are empty and job-level permissions are explicitly scoped."""
    assert release_workflow.get("permissions") == {}
    jobs = release_workflow["jobs"]
    assert jobs["plan"]["permissions"] == {"contents": "read"}
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["publish"]["permissions"] == {
        "id-token": "write",
        "attestations": "write",
        "contents": "read",
    }
    assert jobs["publish"]["environment"] == "pypi"


def _run_determine_packages(
    script: str,
    github_ref: str,
    tmp_path: Path,
    cwd: Path | None = None,
    diff_files: str | None = None,
    mock_plan: str | None = None,
) -> list[str]:
    output_file = tmp_path / "github_output"
    output_file.touch()

    env = {
        **os.environ,
        "GITHUB_REF": github_ref,
        "GITHUB_OUTPUT": str(output_file),
    }

    if diff_files is not None or mock_plan is not None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        real_git = shutil.which("git") or "/usr/bin/git"
        stub_git = bin_dir / "git"
        stub_diff_file = tmp_path / "stub_diff_files.txt"
        stub_diff_file.write_text(diff_files or "", encoding="utf-8")
        stub_git.write_text(
            f'#!/bin/sh\nif [ "$1" = "diff-tree" ]; then\n  cat "{stub_diff_file}"\n  exit 0\nfi\nexec "{real_git}" "$@"\n',
            encoding="utf-8",
        )
        stub_git.chmod(0o755)

        if mock_plan is not None:
            real_py = shutil.which("python3") or "/usr/bin/python3"
            stub_py = bin_dir / "python3"
            stub_plan_file = tmp_path / "stub_plan.json"
            stub_plan_file.write_text(mock_plan, encoding="utf-8")
            stub_py.write_text(
                f'#!/bin/sh\nfor arg in "$@"; do\n  if [ "$arg" = "scripts/ci_plan.py" ]; then\n    cat "{stub_plan_file}"\n    exit 0\n  fi\ndone\nexec "{real_py}" "$@"\n',
                encoding="utf-8",
            )
            stub_py.chmod(0o755)

        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=cwd or tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    content = output_file.read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("packages="):
            return json.loads(line[len("packages=") :])
    raise ValueError(f"packages output not found in {output_file}: {content}")


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash is required for script execution")
@pytest.mark.parametrize(
    "github_ref,expected_packages",
    [
        ("refs/tags/imednet-tui-v1.0.0", ["plugins-tui"]),
        ("refs/tags/imednet-tui-v2.3.4", ["plugins-tui"]),
        ("refs/tags/imednet-v1.1.0", ["core"]),
        ("refs/tags/imednet-workflows-v1.1.0", ["plugins-workflows"]),
        ("refs/tags/apache-airflow-providers-imednet-v1.1.0", ["providers-airflow"]),
        ("refs/tags/imednet-plugins-sinks-v1.0.1", ["plugins-sinks"]),
        (
            "refs/tags/v1.0.0",
            ["core", "plugins-workflows", "providers-airflow", "plugins-sinks", "plugins-tui"],
        ),
        (
            "refs/tags/custom-tag",
            ["core", "plugins-workflows", "providers-airflow", "plugins-sinks", "plugins-tui"],
        ),
        (
            "refs/heads/other-branch",
            ["core", "plugins-workflows", "providers-airflow", "plugins-sinks", "plugins-tui"],
        ),
    ],
)
def test_tag_and_fallback_package_resolution(
    determine_packages_script: str, tmp_path: Path, github_ref: str, expected_packages: list[str]
) -> None:
    """Verify that tag resolution and fallback resolution output expected packages."""
    packages = _run_determine_packages(determine_packages_script, github_ref, tmp_path)
    assert packages == expected_packages


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash is required for script execution")
@pytest.mark.parametrize(
    "diff_files,mock_plan",
    [
        ("docs/index.md\n", None),
        ("README.md\n", None),
        (None, '{"build": []}'),
        (None, ""),
    ],
)
def test_main_branch_empty_plan_fallback(
    determine_packages_script: str, tmp_path: Path, diff_files: str | None, mock_plan: str | None
) -> None:
    """Verify that refs/heads/main falls back to all 5 packages when ci_plan build is empty."""
    packages = _run_determine_packages(
        determine_packages_script,
        "refs/heads/main",
        tmp_path,
        cwd=ROOT,
        diff_files=diff_files,
        mock_plan=mock_plan,
    )
    assert "plugins-tui" in packages
    assert packages == [
        "core",
        "plugins-workflows",
        "providers-airflow",
        "plugins-sinks",
        "plugins-tui",
    ]


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash is required for script execution")
@pytest.mark.parametrize(
    "diff_files,mock_plan,expected_packages",
    [
        ("packages/core/src/imednet/client.py\n", None, ["core"]),
        ("packages/plugins-tui/src/imednet_tui/app.py\n", None, ["plugins-tui"]),
        (None, '{"build": ["core"]}', ["core"]),
        (None, '{"build": ["plugins-tui"]}', ["plugins-tui"]),
        (
            None,
            '{"build": ["plugins-workflows", "plugins-sinks"]}',
            ["plugins-workflows", "plugins-sinks"],
        ),
    ],
)
def test_main_branch_nonempty_plan_preservation(
    determine_packages_script: str,
    tmp_path: Path,
    diff_files: str | None,
    mock_plan: str | None,
    expected_packages: list[str],
) -> None:
    """Verify that refs/heads/main preserves nonempty planner build output and does not fall back."""
    packages = _run_determine_packages(
        determine_packages_script,
        "refs/heads/main",
        tmp_path,
        cwd=ROOT,
        diff_files=diff_files,
        mock_plan=mock_plan,
    )
    assert packages == expected_packages


def test_fallback_package_list_matches_workspace() -> None:
    """Verify that the 5-package fallback set matches all workspace package directories."""
    packages_dir = ROOT / "packages"
    workspace_dirs = {
        p.name for p in packages_dir.iterdir() if p.is_dir() and (p / "pyproject.toml").exists()
    }
    expected_fallback = {
        "core",
        "plugins-workflows",
        "providers-airflow",
        "plugins-sinks",
        "plugins-tui",
    }
    assert workspace_dirs == expected_fallback


def test_release_please_concurrency(release_please_workflow: dict) -> None:
    """Verify release-please.yml serializes main runs with cancel-in-progress: false."""
    concurrency = release_please_workflow.get("concurrency")
    assert isinstance(concurrency, dict)
    assert concurrency.get("group") == "${{ github.workflow }}-${{ github.ref }}"
    assert concurrency.get("cancel-in-progress") == "false"


def test_release_please_permissions_and_app_token_scoping(
    release_please_workflow: dict,
) -> None:
    """Verify read-only GITHUB_TOKEN permissions and scoped GitHub App token minting."""
    raw_source = RELEASE_PLEASE_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "secrets.GITHUB_TOKEN" not in raw_source
    assert "# Keep default GITHUB_TOKEN read-only" in raw_source

    assert release_please_workflow.get("permissions") == {}
    job = release_please_workflow["jobs"]["release-please"]
    assert job["permissions"] == {"contents": "read"}

    steps = job["steps"]
    for step in steps:
        assert "continue-on-error" not in step, (
            f"Step '{step.get('name')}' must not set continue-on-error"
        )
        if "uses" in step:
            assert SHA_PINNED_ACTION_RE.match(step["uses"]), (
                f"Action '{step['uses']}' must be pinned to a 40-char commit SHA"
            )

    token_step = next(s for s in steps if s.get("id") == "app-token")
    assert token_step["uses"].startswith("actions/create-github-app-token@")
    token_with = token_step["with"]
    assert token_with["app-id"] == "${{ secrets.RELEASE_PLEASE_APP_ID }}"
    assert token_with["private-key"] == "${{ secrets.RELEASE_PLEASE_PRIVATE_KEY }}"
    assert token_with["repositories"] == "${{ github.event.repository.name }}"

    permission_inputs = {k: v for k, v in token_with.items() if k.startswith("permission-")}
    assert permission_inputs == {
        "permission-contents": "write",
        "permission-issues": "write",
        "permission-pull-requests": "write",
    }

    expected_token_expr = "${{ steps.app-token.outputs.token }}"
    release_step = next(s for s in steps if s.get("id") == "release")
    assert release_step["uses"].startswith("googleapis/release-please-action@")
    assert release_step["with"]["token"] == expected_token_expr

    checkout_step = next(s for s in steps if s.get("id") == "checkout-release-pr")
    assert checkout_step["uses"].startswith("actions/checkout@")
    assert checkout_step["with"]["token"] == expected_token_expr
    assert checkout_step["with"]["persist-credentials"] == "false"
    assert checkout_step["with"]["fetch-depth"] == "0"
    assert checkout_step["with"]["ref"] == "release-please--branches--main"


def test_release_please_setup_uv_matches_main_workflow(
    release_please_workflow: dict,
) -> None:
    """Verify release-please.yml uses SHA-pinned astral-sh/setup-uv matching main.yml without pip."""
    main_workflow = yaml.load(
        MAIN_WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    main_uv_steps = [
        step
        for job in main_workflow["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    ]
    assert main_uv_steps, "Expected astral-sh/setup-uv in main.yml"
    expected_setup_uv = main_uv_steps[0]["uses"]

    steps = release_please_workflow["jobs"]["release-please"]["steps"]
    uv_steps = [s for s in steps if str(s.get("uses", "")).startswith("astral-sh/setup-uv@")]
    assert len(uv_steps) == 1
    assert uv_steps[0]["uses"] == expected_setup_uv
    assert uv_steps[0]["with"]["enable-cache"] == "false"

    for step in steps:
        run_cmd = step.get("run", "")
        assert "pip install" not in run_cmd


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash is required for script execution")
def test_release_please_lockfile_push_credential_redaction(
    release_please_workflow: dict, tmp_path: Path
) -> None:
    """Verify Update uv lockfile pushes via transient GIT_CONFIG_COUNT headers without leaking tokens."""
    steps = release_please_workflow["jobs"]["release-please"]["steps"]
    lock_step = next(s for s in steps if s.get("name") == "Update uv lockfile")
    script = lock_step["run"]

    assert lock_step["env"] == {"APP_TOKEN": "${{ steps.app-token.outputs.token }}"}
    assert "GH_TOKEN" not in script
    assert "https://" not in script
    assert "@github.com" not in script
    assert " -c " not in script
    assert "GIT_CONFIG_COUNT" in script
    assert "--force-with-lease" in script

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    real_git = shutil.which("git") or "/usr/bin/git"
    subprocess.run([real_git, "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        [real_git, "config", "user.email", "init@example.com"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [real_git, "config", "user.name", "init"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [real_git, "remote", "add", "origin", "https://github.com/fderuiter/imednet-toolkit.git"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    (repo_dir / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    subprocess.run([real_git, "add", "uv.lock"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        [real_git, "commit", "-m", "initial"], cwd=repo_dir, check=True, capture_output=True
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    push_capture_file = tmp_path / "push_capture.json"

    stub_uv = bin_dir / "uv"
    stub_uv.write_text(
        '#!/bin/sh\nif [ "$1" = "lock" ]; then\n  printf "version = 2\\n" > uv.lock\n  exit 0\nfi\nexit 1\n',
        encoding="utf-8",
    )
    stub_uv.chmod(0o755)

    stub_git = bin_dir / "git"
    stub_git.write_text(
        f"""#!/usr/bin/env python3
import json, os, subprocess, sys
if len(sys.argv) > 1 and sys.argv[1] == "push":
    with open({str(push_capture_file)!r}, "w", encoding="utf-8") as fh:
        json.dump(
            {{
                "argv": sys.argv[1:],
                "git_config_count": os.environ.get("GIT_CONFIG_COUNT"),
                "git_config_key_0": os.environ.get("GIT_CONFIG_KEY_0"),
                "git_config_value_0": os.environ.get("GIT_CONFIG_VALUE_0"),
            }},
            fh,
        )
    sys.exit(0)
sys.exit(subprocess.call([{real_git!r}, *sys.argv[1:]]))
""",
        encoding="utf-8",
    )
    stub_git.chmod(0o755)

    placeholder_credential = "ghs_unit_test_ephemeral_install_tok"  # pragma: allowlist secret
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "APP_TOKEN": placeholder_credential,
        "GITHUB_SERVER_URL": "https://github.com",
    }
    res = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=repo_dir,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert placeholder_credential not in res.stdout
    assert placeholder_credential not in res.stderr

    git_config_text = (repo_dir / ".git" / "config").read_text(encoding="utf-8")
    assert placeholder_credential not in git_config_text
    assert "extraheader" not in git_config_text.lower()

    captured = json.loads(push_capture_file.read_text(encoding="utf-8"))
    assert captured["argv"] == [
        "push",
        "--force-with-lease",
        "origin",
        "release-please--branches--main",
    ]
    for arg in captured["argv"]:
        assert placeholder_credential not in arg

    expected_b64 = base64.b64encode(
        f"x-access-token:{placeholder_credential}".encode("utf-8")
    ).decode("ascii")
    assert captured["git_config_count"] == "1"
    assert captured["git_config_key_0"] == "http.https://github.com/.extraheader"
    assert captured["git_config_value_0"] == f"AUTHORIZATION: basic {expected_b64}"


def _assert_manifest_matches_git_tags(
    manifest: dict[str, str], config: dict, git_tags: set[str]
) -> None:
    packages_cfg = config["packages"]
    for pkg_path, version in manifest.items():
        pkg_name = packages_cfg[pkg_path]["package-name"]
        expected_tag = f"{pkg_name}-v{version}"
        assert expected_tag in git_tags, (
            f"Manifest package {pkg_path}={version} missing expected Git tag {expected_tag}"
        )

    unreleased_paths = set(packages_cfg) - set(manifest)
    for pkg_path in unreleased_paths:
        pkg_name = packages_cfg[pkg_path]["package-name"]
        matching_tags = [t for t in git_tags if t.startswith(f"{pkg_name}-v")]
        assert not matching_tags, (
            f"Unreleased package {pkg_path} omitted from manifest has existing tags: {matching_tags}"
        )


def test_release_please_manifest_and_config_parity(tmp_path: Path) -> None:
    """Verify manifest/config/pyproject parity after release bumps and wheel tightening behavior."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    config = json.loads(RELEASE_PLEASE_CONFIG_PATH.read_text(encoding="utf-8"))
    packages_cfg = config["packages"]

    expected_all_packages = {
        "packages/core",
        "packages/providers-airflow",
        "packages/plugins-workflows",
        "packages/plugins-sinks",
        "packages/plugins-tui",
    }
    assert set(packages_cfg) == expected_all_packages
    assert set(manifest) == expected_all_packages
    assert packages_cfg["packages/plugins-tui"].get("initial-version") == "1.0.0"

    for pkg_path, manifest_ver in manifest.items():
        pkg_pyproject = tomllib.loads(
            (ROOT / pkg_path / "pyproject.toml").read_text(encoding="utf-8")
        )
        assert pkg_pyproject["project"]["version"] == manifest_ver

    internal_pkgs = td.get_internal_packages(MANIFEST_PATH)
    assert set(internal_pkgs) == {
        "imednet",
        "apache-airflow-providers-imednet",
        "imednet-workflows",
        "imednet-plugins-sinks",
        "imednet-tui",
    }
    tui_ver = manifest["packages/plugins-tui"]
    assert (
        td.process_dependency("imednet-tui", internal_pkgs)
        == f"imednet-tui>={tui_ver},{td.compute_upper_bound(tui_ver)}"
    )

    # Verify unreleased packages omitted from a manifest remain unconstrained during wheel tightening
    partial_manifest_dir = tmp_path / "repo"
    for pkg_rel in ("packages/core", "packages/plugins-tui"):
        target_toml = partial_manifest_dir / pkg_rel / "pyproject.toml"
        target_toml.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / pkg_rel / "pyproject.toml", target_toml)
    partial_manifest_path = partial_manifest_dir / ".release-please-manifest.json"
    partial_manifest = {"packages/core": manifest["packages/core"]}
    partial_manifest_path.write_text(json.dumps(partial_manifest), encoding="utf-8")
    partial_internal = td.get_internal_packages(partial_manifest_path)
    assert "imednet-tui" not in partial_internal
    assert td.process_dependency("imednet-tui", partial_internal) == "imednet-tui"

    sample_tags = {
        f"{packages_cfg[pkg_path]['package-name']}-v{ver}" for pkg_path, ver in manifest.items()
    }
    _assert_manifest_matches_git_tags(manifest, config, sample_tags)

    unreleased_sub_cfg = {
        "packages": {
            "packages/core": packages_cfg["packages/core"],
            "packages/plugins-tui": packages_cfg["packages/plugins-tui"],
        }
    }
    _assert_manifest_matches_git_tags(
        partial_manifest,
        unreleased_sub_cfg,
        {f"imednet-v{manifest['packages/core']}"},
    )

    git_bin = shutil.which("git")
    if git_bin and (ROOT / ".git").exists():
        tag_proc = subprocess.run(
            [git_bin, "tag", "-l"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        live_tags = {line.strip() for line in tag_proc.stdout.splitlines() if line.strip()}
        if live_tags and sample_tags.issubset(live_tags):
            _assert_manifest_matches_git_tags(manifest, config, live_tags)


def test_release_please_app_setup_wizard_and_docs() -> None:
    """Verify setup-release-please-app.sh wizard structure, secret names, and CONTRIBUTING docs."""
    assert RELEASE_APP_WIZARD_PATH.is_file()
    wizard_text = RELEASE_APP_WIZARD_PATH.read_text(encoding="utf-8")
    pypi_wizard_text = PYPI_WIZARD_PATH.read_text(encoding="utf-8")

    stage_vars = re.findall(r"^TOTAL_STAGES=(\d+)", wizard_text, flags=re.MULTILINE)
    stage_calls = re.findall(r'^\s*stage\s+"([^"]+)"', wizard_text, flags=re.MULTILINE)
    assert stage_vars and int(stage_vars[-1]) == len(stage_calls) == 4

    app_open_url = re.search(r"(open_url\(\)\s*\{[\s\S]*?\n\})", wizard_text)
    pypi_open_url = re.search(r"(open_url\(\)\s*\{[\s\S]*?\n\})", pypi_wizard_text)
    assert app_open_url is not None and pypi_open_url is not None
    assert app_open_url.group(1) == pypi_open_url.group(1)

    stages_section = wizard_text.split("# STAGES:", maxsplit=1)[1]
    assert "set_secret RELEASE_PLEASE_APP_ID" in stages_section
    assert "set_secret RELEASE_PLEASE_PRIVATE_KEY" in stages_section
    assert "write_env " not in stages_section

    if shutil.which("bash") is not None:
        syntax = subprocess.run(
            ["bash", "-n", str(RELEASE_APP_WIZARD_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert syntax.returncode == 0, syntax.stderr

    contributing_text = CONTRIBUTING_PATH.read_text(encoding="utf-8")
    assert "RELEASE_PLEASE_APP_ID" in contributing_text
    assert "RELEASE_PLEASE_PRIVATE_KEY" in contributing_text
    assert "scripts/wizards/setup-release-please-app.sh" in contributing_text
