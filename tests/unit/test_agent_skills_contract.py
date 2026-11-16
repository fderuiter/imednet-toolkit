"""Contract tests enforcing structure, cross-harness parity, and hygiene of .agents/skills/."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"
SKILLS_CONFIG = REPO_ROOT / ".agents" / "skills.json"

EXPECTED_SKILLS = frozenset(
    {
        "brainstorming",
        "codebase-design",
        "diagnosing-superpowers",
        "dispatching-parallel-agents",
        "domain-modeling",
        "executing-plans",
        "finishing-a-development-branch",
        "grilling",
        "handoff",
        "improve-codebase-architecture",
        "prototype",
        "receiving-code-review",
        "requesting-code-review",
        "research",
        "resolving-merge-conflicts",
        "subagent-driven-development",
        "systematic-debugging",
        "test-driven-development",
        "to-questionnaire",
        "to-spec",
        "to-tickets",
        "triage",
        "using-git-worktrees",
        "using-superpowers",
        "verification-before-completion",
        "wait-what",
        "wayfinder",
        "wizard",
        "workflow-guide",
        "writing-plans",
        "writing-skills",
    }
)

DISALLOWED_SKILL_BODY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Skill tool leak", re.compile(r"\bCall the Skill tool\b|\bUse `Skill` tool\b")),
    ("TodoWrite leak", re.compile(r"\bTodoWrite\b|\bTaskCreate\b")),
    ("obsolete Antigravity write tool", re.compile(r"\bwrite_to_file\b|\breplace_file_content\b")),
    ("legacy Poetry install rule", re.compile(r"\bpoetry install\b")),
    ("legacy needs-triage label", re.compile(r"\bneeds-triage\b")),
    ("legacy needs-info label", re.compile(r"\bneeds-info\b")),
    ("legacy ready-for-human label", re.compile(r"\bready-for-human\b")),
    ("retired setup wizard", re.compile(r"/setup-matt-pocock-skills\b")),
    ("retired out-of-scope doc", re.compile(r"\bOUT-OF-SCOPE\.md\b|\.out-of-scope/")),
    ("hardcoded /tmp/review path", re.compile(r"/tmp/review-")),
)


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    match = re.match(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$", text)
    assert match is not None, f"Missing YAML frontmatter in {path}"
    loaded = yaml.safe_load(match.group(1))
    assert isinstance(loaded, dict), f"Frontmatter in {path} must decode to a mapping"
    return loaded, match.group(2)


def test_skills_json_registers_canonical_directory() -> None:
    """Ensure .agents/skills.json registers .agents/skills."""
    data = json.loads(SKILLS_CONFIG.read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    assert ".agents/skills" in paths


def test_expected_unified_skill_set_present() -> None:
    """Verify exact 31-skill portfolio with no leftover deleted or colliding twins."""
    actual = {
        item.name
        for item in SKILLS_ROOT.iterdir()
        if item.is_dir() and not item.name.startswith(".")
    }
    assert actual == EXPECTED_SKILLS, (
        f"extra={sorted(actual - EXPECTED_SKILLS)}, missing={sorted(EXPECTED_SKILLS - actual)}"
    )


def test_skill_frontmatter_and_openai_yaml_parity() -> None:
    """Validate agentskills.io metadata and 1:1 manual-invocation parity for Codex."""
    for skill_name in sorted(EXPECTED_SKILLS):
        skill_dir = SKILLS_ROOT / skill_name
        skill_md = skill_dir / "SKILL.md"
        openai_yaml = skill_dir / "agents" / "openai.yaml"

        assert skill_md.is_file(), f"Missing SKILL.md in {skill_dir}"
        assert openai_yaml.is_file(), f"Missing agents/openai.yaml in {skill_dir}"

        frontmatter, _ = _split_frontmatter(skill_md.read_text(encoding="utf-8"), skill_md)
        assert frontmatter.get("name") == skill_name
        assert re.fullmatch(r"[a-z0-9-]+", skill_name)
        assert len(skill_name) <= 64

        description = frontmatter.get("description")
        assert isinstance(description, str) and description.strip(), (
            f"Empty description in {skill_md}"
        )

        openai_cfg = yaml.safe_load(openai_yaml.read_text(encoding="utf-8"))
        assert isinstance(openai_cfg, dict), f"Invalid YAML mapping in {openai_yaml}"
        interface = openai_cfg.get("interface", {})
        assert isinstance(interface, dict)
        assert str(interface.get("display_name", "")).strip(), (
            f"Missing display_name in {openai_yaml}"
        )
        assert str(interface.get("short_description", "")).strip(), (
            f"Missing short_description in {openai_yaml}"
        )

        dmi = frontmatter.get("disable-model-invocation") is True
        policy = openai_cfg.get("policy", {})
        allow_implicit = (
            policy.get("allow_implicit_invocation") if isinstance(policy, dict) else None
        )
        if dmi:
            assert allow_implicit is False, (
                f"{skill_name}: disable-model-invocation: true requires policy.allow_implicit_invocation: false"
            )
        else:
            assert allow_implicit is not False, (
                f"{skill_name}: policy.allow_implicit_invocation: false requires disable-model-invocation: true"
            )


def test_skill_bodies_have_no_leaked_vendor_or_governance_tokens() -> None:
    """Enforce harness-agnostic vocabulary and updated governance tokens across SKILL.md."""
    violations: list[str] = []
    for skill_name in sorted(EXPECTED_SKILLS):
        skill_md = SKILLS_ROOT / skill_name / "SKILL.md"
        if not skill_md.is_file():
            continue
        _, body = _split_frontmatter(skill_md.read_text(encoding="utf-8"), skill_md)
        for label, pattern in DISALLOWED_SKILL_BODY_PATTERNS:
            if pattern.search(body):
                violations.append(f"{skill_name}: {label}")
    assert not violations, "Leaked tokens in SKILL.md:\n" + "\n".join(violations)


def test_executing_plans_scripts_use_portable_bash_invocation() -> None:
    """Verify sibling helper calls use "${BASH:-bash}" so Windows checkouts succeed."""
    scripts_dir = SKILLS_ROOT / "executing-plans" / "scripts"
    for name in ("task-start", "task-done"):
        lines = (scripts_dir / name).read_text(encoding="utf-8").splitlines()
        sdd_lines = [
            line for line in lines if "$sdd/" in line and not line.lstrip().startswith("#")
        ]
        assert sdd_lines, f"Expected sibling $sdd/ helper calls in {name}"
        for line in sdd_lines:
            assert '"${BASH:-bash}" "$sdd/' in line, (
                f"{name} invokes sibling helper without ${{BASH:-bash}}: {line}"
            )
