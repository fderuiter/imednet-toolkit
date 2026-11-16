"""Unit tests for the local cross-harness Claude adapter bridge script."""

from pathlib import Path

import pytest
from scripts.setup_agent_harnesses import (
    BridgeConflictError,
    check_claude_bridge,
    ensure_claude_bridge,
    main,
    remove_claude_bridge,
)


def _init_fake_repo(tmp_path: Path) -> Path:
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    skill_dir = tmp_path / ".agents" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: Demo\n---\n")
    return tmp_path


def test_dry_run_makes_no_changes(tmp_path: Path) -> None:
    repo = _init_fake_repo(tmp_path)
    result = ensure_claude_bridge(repo, dry_run=True)
    assert result.status == "planned"
    assert not (repo / ".claude" / "skills").exists()
    assert not (repo / "CLAUDE.md").exists()


def test_ensure_check_idempotent_and_remove(tmp_path: Path) -> None:
    repo = _init_fake_repo(tmp_path)
    assert check_claude_bridge(repo) is False

    created = ensure_claude_bridge(repo)
    assert created.status == "configured"
    claude_skills = repo / ".claude" / "skills"
    assert claude_skills.exists()
    assert (claude_skills / "demo-skill" / "SKILL.md").is_file()
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    assert check_claude_bridge(repo) is True

    reapplied = ensure_claude_bridge(repo)
    assert reapplied.status == "unchanged"
    assert check_claude_bridge(repo) is True

    removed = remove_claude_bridge(repo)
    assert "removed" in " ".join(removed).lower()
    assert not claude_skills.exists()
    assert not (repo / "CLAUDE.md").exists()
    # Canonical .agents/skills must remain completely untouched.
    assert (repo / ".agents" / "skills" / "demo-skill" / "SKILL.md").is_file()


def test_refuses_to_overwrite_existing_regular_claude_skills_dir(tmp_path: Path) -> None:
    repo = _init_fake_repo(tmp_path)
    existing = repo / ".claude" / "skills" / "custom-local"
    existing.mkdir(parents=True)
    sentinel = existing / "SKILL.md"
    sentinel.write_text("custom", encoding="utf-8")

    with pytest.raises(BridgeConflictError, match="regular directory"):
        ensure_claude_bridge(repo)
    assert sentinel.read_text(encoding="utf-8") == "custom"


def test_refuses_to_overwrite_unmanaged_claude_md(tmp_path: Path) -> None:
    repo = _init_fake_repo(tmp_path)
    (repo / "CLAUDE.md").write_text("# Custom hand-written Claude rules\n", encoding="utf-8")

    with pytest.raises(BridgeConflictError, match="CLAUDE.md"):
        ensure_claude_bridge(repo)


def test_cli_exit_codes_for_check_and_setup(tmp_path: Path) -> None:
    repo = _init_fake_repo(tmp_path)
    assert main(["--repo-root", str(repo), "--check"]) == 1
    assert main(["--repo-root", str(repo)]) == 0
    assert main(["--repo-root", str(repo), "--check"]) == 0
    assert main(["--repo-root", str(repo), "--remove"]) == 0
    assert main(["--repo-root", str(repo), "--check"]) == 1
