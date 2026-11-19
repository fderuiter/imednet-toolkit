"""Unit tests for the setup wizard environment script and workspace TUI parity."""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from scripts.validate_docs import check_packages_registered


def _get_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _get_setup_wizard_path() -> Path:
    return _get_repo_root() / "scripts" / "wizards" / "setup-imednet-env.sh"


def _extract_stage_content(script_content: str, stage_number: int) -> str:
    pattern = rf"# ── Stage {stage_number}:.*?(?=# ── Stage {stage_number + 1}:|\Z)"
    match = re.search(pattern, script_content, flags=re.DOTALL)
    assert match is not None, f"Stage {stage_number} not found in script content"
    return match.group(0)


def test_setup_wizard_contains_all_packages() -> None:
    """Verify scripts/wizards/setup-imednet-env.sh contains the --all-packages flag."""
    wizard_path = _get_setup_wizard_path()
    assert wizard_path.is_file(), f"Wizard script not found at {wizard_path}"

    content = wizard_path.read_text(encoding="utf-8")
    assert "--all-packages" in content, "setup-imednet-env.sh does not contain '--all-packages'"


def test_setup_wizard_stage3_uv_sync_invocations_use_all_packages() -> None:
    """Assert that every uv sync invocation and prompt in Stage 3 includes the --all-packages option."""
    wizard_path = _get_setup_wizard_path()
    content = wizard_path.read_text(encoding="utf-8")
    stage3_content = _extract_stage_content(content, 3)

    # Find all lines mentioning 'uv sync' in Stage 3
    uv_sync_lines = [line.strip() for line in stage3_content.splitlines() if "uv sync" in line]
    assert len(uv_sync_lines) >= 4, (
        f"Expected at least 4 'uv sync' references in Stage 3, found {len(uv_sync_lines)}"
    )

    for line in uv_sync_lines:
        assert "--all-packages" in line, (
            f"Stage 3 line mentions 'uv sync' without '--all-packages': {line}"
        )


def test_setup_wizard_stage3_commands_and_messages() -> None:
    """Verify that Stage 3 prompt, execution, warnings, and skip message use the full flag set."""
    wizard_path = _get_setup_wizard_path()
    content = wizard_path.read_text(encoding="utf-8")
    stage3_content = _extract_stage_content(content, 3)

    expected_cmd = "uv sync --all-packages --extra dev --extra docs"

    assert f'confirm "Run \'{expected_cmd}\' now?"' in stage3_content
    assert f'say "Running \'{expected_cmd}\'..."' in stage3_content
    assert f"if {expected_cmd}; then" in stage3_content
    assert (
        f"warn \"uv sync encountered issues. You can re-run '{expected_cmd}' later.\""
        in stage3_content
    )
    assert (
        f"warn \"uv is not available. Please install uv and run '{expected_cmd}'.\""
        in stage3_content
    )
    assert f"say \"Skipping dependency sync. Run '{expected_cmd}' when ready.\"" in stage3_content
    assert "all 5 packages" in stage3_content
    assert "all 4 packages" not in stage3_content


def test_setup_wizard_stage3_package_count_matches_monorepo() -> None:
    """Verify Stage 3 package count matches the actual number of workspace packages."""
    repo_root = _get_repo_root()
    package_manifests = list((repo_root / "packages").glob("*/pyproject.toml"))
    wizard_content = _get_setup_wizard_path().read_text(encoding="utf-8")
    stage3_content = _extract_stage_content(wizard_content, 3)

    assert len(package_manifests) == 5
    assert f"all {len(package_manifests)} packages" in stage3_content


def test_workspace_manifest_and_readme_include_tui() -> None:
    """Verify root pyproject.toml, uv.lock, and README.md synchronize imednet-tui."""
    repo_root = _get_repo_root()
    pyproject_data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    root_deps = pyproject_data["project"]["dependencies"]
    uv_sources = pyproject_data["tool"]["uv"]["sources"]
    assert "imednet-tui" in root_deps
    assert "imednet-tui" in uv_sources

    uv_lock_data = tomllib.loads((repo_root / "uv.lock").read_text(encoding="utf-8"))
    workspace_pkgs = [
        pkg for pkg in uv_lock_data.get("package", []) if pkg.get("name") == "imednet-workspace"
    ]
    assert len(workspace_pkgs) == 1
    locked_dep_names = {dep["name"] for dep in workspace_pkgs[0].get("dependencies", [])}
    assert "imednet-tui" in locked_dep_names

    readme_content = (repo_root / "README.md").read_text(encoding="utf-8")
    assert 'pip install "imednet[tui]"' in readme_content
    assert readme_content.count("uv sync --all-packages --extra dev --extra docs") >= 2
    assert "uv sync --extra dev --extra docs" not in readme_content


def test_validate_docs_packages_registered_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify scripts/validate_docs.py check_packages_registered validates workspace TUI parity."""
    monkeypatch.chdir(_get_repo_root())
    assert check_packages_registered() is False


def test_adr_0005_and_0007_reflect_shipped_tui_and_grid() -> None:
    """Verify ADR 0005 and ADR 0007 match shipped imednet-tui and InteractiveDataGrid behavior."""
    repo_root = _get_repo_root()
    adr_0005 = (
        repo_root / "docs" / "adr" / "0005-authentication-and-credential-capabilities.md"
    ).read_text(encoding="utf-8")
    assert "imednet-tui" in adr_0005
    assert "load_config" in adr_0005
    assert "@with_sdk" in adr_0005
    assert "no MCP client" in adr_0005
    assert "No TUI/MCP is currently shipped" not in adr_0005

    adr_0007 = (
        repo_root / "docs" / "adr" / "0007-cli-framework-and-output-contracts.md"
    ).read_text(encoding="utf-8")
    assert "imednet.cli_plugins" in adr_0007
    assert "InteractiveDataGrid" in adr_0007
    assert "IMEDNET_NON_INTERACTIVE=1" in adr_0007


def test_build_form_payload_no_args_shows_help_and_tui_guidance() -> None:
    """Verify examples/build_form_payload.py prints Form Builder usage and directs browsing to imednet tui."""
    repo_root = _get_repo_root()
    script_path = repo_root / "examples" / "build_form_payload.py"
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    combined_output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "TUI mode has been removed" not in combined_output
    assert "Form Builder" in combined_output
    assert "imednet tui" in combined_output
    assert "--preset" in combined_output
    assert "--form-id" in combined_output


def test_setup_wizard_stage6_recovery_guidance_mentions_all_packages() -> None:
    """Verify Stage 6 SDK verification recovery guidance mentions --all-packages."""
    wizard_path = _get_setup_wizard_path()
    content = wizard_path.read_text(encoding="utf-8")
    stage6_content = _extract_stage_content(content, 6)

    # In Stage 6, when python check via uv fails, uv sync recovery instruction must include --all-packages
    assert "uv sync --all-packages" in stage6_content


def test_setup_wizard_bash_syntax() -> None:
    """Verify that the setup wizard script passes bash syntax validation."""
    wizard_path = _get_setup_wizard_path()
    result = subprocess.run(
        ["bash", "-n", str(wizard_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Bash syntax check failed: {result.stderr}"
