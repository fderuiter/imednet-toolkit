# Transitive Dependency Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the seven requested Dependabot exposures and prevent vulnerable workspace resolutions.
**Architecture:** Workspace-only uv constraints, targeted lock refresh, existing dependency-security regression suite and explicit scanner exception boundary.
**Tech Stack:** Python, uv, Hatch, pytest, pip-audit, Sphinx.
**Spec:** `docs/superpowers/specs/2026-11-16-dependency-security.md`.

## Global Constraints

Keep upstream dependency bounds, published package metadata and release versions.
No new audit exceptions, live EDC requests, alert dismissals or merge to main.
The user's request authorizes implementation and creation of a new security PR;
execute inline in the existing isolated worktree without another approval menu.

## Review Focus

Verify every locked vulnerable package is patched, OpenTelemetry dependencies
remain mutually compatible, the Python patch minimum is workspace-only, and
scanner exceptions cannot conceal results of the independent lockfile audit.

### Task 1: Exclude vulnerable workspace resolutions

**Files:** `tests/unit/test_dependency_security.py`, `pyproject.toml`, `uv.lock`.
**Interfaces:** Consumes maintainer advisories; produces patched universal lock and resolution floors.

- [ ] Add parameterized regressions using advisory-derived patched floors;
  require all locked copies and uv constraints to exclude affected releases.
  Separate the workspace's Python patch minimum from published package support.
- [ ] Run `uv run pytest -q tests/unit/test_dependency_security.py` and observe
  failures for vulnerable versions before updating configuration.
- [ ] Add workspace constraints and Python >=3.11.5; refresh only affected
  packages and the OpenTelemetry family. Synchronize dev/docs extras and run
  the focused suite and `uv pip check` successfully.
- [ ] Export all workspace/extras external requirements and audit without any
  ignored advisories, independently of the detached Semgrep environment.

### Task 2: Document compatibility and verify the complete change

**Files:** `docs/reference/security_exceptions.rst`, `docs/reference/compatibility.rst`,
`README.md`, `scripts/wizards/setup-imednet-env.sh`, `.github/workflows/main.yml`,
`pyproject.toml` (detached scanner installer configuration).
**Interfaces:** Consumes patched lock and scanner metadata; produces accurate support guidance and verified PR.

- [ ] Record all seven alerts, patched floors, dependency paths and uv audit
  recipe. Keep the scanner-only exceptions explicit and dated. Align the setup
  wizard's interpreter check and README with the workspace patch minimum.
- [ ] Keep the detached scanner resolution separate from workspace constraints
  and add an unignored exported-lock audit to the Pipeline quality job.
- [ ] Run pre-commit first, then canonical static checks, full offline tests
  with >=90% coverage, strict documentation, and the full quality/security gate.
- [ ] Request a fresh read-only review against this plan and spec, fix material
  findings, commit and create the new PR with exact verification evidence.
