"""Unit tests for the setup wizard environment script."""

import re
import subprocess
from pathlib import Path


def _get_setup_wizard_path() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "scripts" / "wizards" / "setup-imednet-env.sh"


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
