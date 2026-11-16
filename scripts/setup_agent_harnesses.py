"""Configure local gitignored harness bridges (Claude Code -> .agents/skills & AGENTS.md)."""

from __future__ import annotations

import argparse
import contextlib
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_CLAUDE_SHIM_CONTENT = "@AGENTS.md\n"


class BridgeConflictError(RuntimeError):
    """Raised when an existing unmanaged file or directory blocks bridge creation."""


@dataclass(frozen=True)
class SetupResult:
    """Outcome summary returned by :func:`ensure_claude_bridge`."""

    claude_skills_path: Path
    claude_md_path: Path
    status: str
    actions: tuple[str, ...]


def _is_reparse_or_symlink(path: Path) -> bool:
    """Return True if *path* is a POSIX symlink or Windows reparse point/junction."""
    if path.is_symlink():
        return True
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _points_to_target(link_path: Path, target_path: Path) -> bool:
    """Return True when *link_path* is a link/junction resolving to *target_path*."""
    if not _is_reparse_or_symlink(link_path):
        return False
    try:
        return os.path.realpath(link_path) == os.path.realpath(target_path)
    except OSError:
        return False


def _unlink_bridge(link_path: Path) -> None:
    """Remove a symlink or Windows NTFS junction without touching target contents."""
    if link_path.is_symlink():
        link_path.unlink()
        return
    if os.name == "nt" and _is_reparse_or_symlink(link_path):
        os.rmdir(link_path)
        return
    raise BridgeConflictError(
        f"Refusing to remove non-link directory at {link_path}; move or remove it manually."
    )


def link_claude_skills(repo_root: Path, *, dry_run: bool = False) -> str:
    """Create or validate the local ``.claude/skills`` bridge to ``.agents/skills``."""
    canonical_skills = repo_root / ".agents" / "skills"
    if not canonical_skills.is_dir():
        raise FileNotFoundError(f"Canonical skills directory not found: {canonical_skills}")

    claude_dir = repo_root / ".claude"
    link_path = claude_dir / "skills"

    if link_path.exists() or link_path.is_symlink():
        if _points_to_target(link_path, canonical_skills):
            return "unchanged:.claude/skills"
        if _is_reparse_or_symlink(link_path):
            raise BridgeConflictError(
                f"Existing link/junction at {link_path} does not point to {canonical_skills}."
            )
        raise BridgeConflictError(f"Refusing to replace existing regular directory at {link_path}.")

    if dry_run:
        return "planned:.claude/skills"

    claude_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        winapi = getattr(__import__("_winapi"), "CreateJunction", None)
        if callable(winapi):
            winapi(str(canonical_skills), str(link_path))
        else:
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            completed = subprocess.run(
                [comspec, "/c", "mklink", "/J", str(link_path), str(canonical_skills)],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise OSError(f"Failed creating junction {link_path}: {detail}")
    else:
        rel_target = os.path.relpath(canonical_skills, claude_dir)
        os.symlink(rel_target, link_path, target_is_directory=True)

    return "created:.claude/skills"


def ensure_claude_md(repo_root: Path, *, dry_run: bool = False) -> str:
    """Ensure local gitignored ``CLAUDE.md`` imports ``@AGENTS.md``."""
    agents_md = repo_root / "AGENTS.md"
    if not agents_md.is_file():
        raise FileNotFoundError(f"AGENTS.md not found in {repo_root}")

    claude_md = repo_root / "CLAUDE.md"
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if existing == _CLAUDE_SHIM_CONTENT:
            return "unchanged:CLAUDE.md"
        raise BridgeConflictError(
            f"Refusing to overwrite existing unmanaged CLAUDE.md at {claude_md}."
        )

    if dry_run:
        return "planned:CLAUDE.md"

    claude_md.write_text(_CLAUDE_SHIM_CONTENT, encoding="utf-8")
    return "created:CLAUDE.md"


def check_claude_bridge(repo_root: Path, *, check_claude_md: bool = True) -> bool:
    """Return True when the local Claude bridge matches repository truth."""
    canonical_skills = repo_root / ".agents" / "skills"
    link_path = repo_root / ".claude" / "skills"
    if not _points_to_target(link_path, canonical_skills):
        return False
    if check_claude_md:
        claude_md = repo_root / "CLAUDE.md"
        if not claude_md.is_file():
            return False
        if claude_md.read_text(encoding="utf-8") != _CLAUDE_SHIM_CONTENT:
            return False
    return True


def ensure_claude_bridge(
    repo_root: Path,
    *,
    include_claude_md: bool = True,
    dry_run: bool = False,
) -> SetupResult:
    """Create or verify all local Claude Code adapter bridges for *repo_root*."""
    resolved_root = repo_root.resolve()
    actions = [link_claude_skills(resolved_root, dry_run=dry_run)]
    if include_claude_md:
        actions.append(ensure_claude_md(resolved_root, dry_run=dry_run))

    if dry_run and any(action.startswith("planned:") for action in actions):
        status = "planned"
    elif any(action.startswith("created:") for action in actions):
        status = "configured"
    else:
        status = "unchanged"

    return SetupResult(
        claude_skills_path=resolved_root / ".claude" / "skills",
        claude_md_path=resolved_root / "CLAUDE.md",
        status=status,
        actions=tuple(actions),
    )


def remove_claude_bridge(repo_root: Path, *, dry_run: bool = False) -> tuple[str, ...]:
    """Remove locally managed Claude Code adapter links without altering ``.agents/``."""
    resolved_root = repo_root.resolve()
    canonical_skills = resolved_root / ".agents" / "skills"
    claude_dir = resolved_root / ".claude"
    link_path = claude_dir / "skills"
    claude_md = resolved_root / "CLAUDE.md"
    actions: list[str] = []

    if link_path.exists() or link_path.is_symlink():
        if not _points_to_target(link_path, canonical_skills):
            raise BridgeConflictError(
                f"Refusing to remove {link_path}; it does not target {canonical_skills}."
            )
        if dry_run:
            actions.append("planned-remove:.claude/skills")
        else:
            _unlink_bridge(link_path)
            actions.append("removed:.claude/skills")
            with contextlib.suppress(OSError):
                claude_dir.rmdir()

    if claude_md.exists():
        if claude_md.read_text(encoding="utf-8") != _CLAUDE_SHIM_CONTENT:
            raise BridgeConflictError(f"Refusing to remove unmanaged CLAUDE.md at {claude_md}.")
        if dry_run:
            actions.append("planned-remove:CLAUDE.md")
        else:
            claude_md.unlink()
            actions.append("removed:CLAUDE.md")

    return tuple(actions) if actions else ("unchanged:none",)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure local gitignored Claude Code bridges (.claude/skills, CLAUDE.md)."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Target repository root (defaults to parent of scripts/).",
    )
    parser.add_argument("--check", action="store_true", help="Verify bridge and exit 0/1.")
    parser.add_argument("--dry-run", action="store_true", help="Report actions without writing.")
    parser.add_argument("--remove", action="store_true", help="Remove managed local bridges.")
    parser.add_argument(
        "--no-claude-md",
        action="store_true",
        help="Skip creating/checking CLAUDE.md (create .claude/skills link only).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for setting up or verifying local Claude harness bridges."""
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    repo_root = args.repo_root.resolve()

    try:
        if args.check:
            ok = check_claude_bridge(repo_root, check_claude_md=not args.no_claude_md)
            print("ok" if ok else "missing-or-stale")
            return 0 if ok else 1

        if args.remove:
            actions = remove_claude_bridge(repo_root, dry_run=args.dry_run)
            print("\n".join(actions))
            return 0

        result = ensure_claude_bridge(
            repo_root,
            include_claude_md=not args.no_claude_md,
            dry_run=args.dry_run,
        )
        print(f"{result.status}: {', '.join(result.actions)}")
        return 0
    except (BridgeConflictError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
