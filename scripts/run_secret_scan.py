#!/usr/bin/env python3
"""Expand file and directory targets into Git-resolved files for detect-secrets."""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import patch

DEFAULT_BASELINE_NAME = ".secrets.baseline"
HookRunner = Callable[[list[str]], int]


class SecretScanTargetError(RuntimeError):
    """Raised when a secret scan target or repository root cannot be resolved."""


def resolve_repo_root(cwd: Path, explicit_root: Path | None = None) -> Path:
    """Resolve the Git repository root directory for target expansion.

    Args:
        cwd: Working directory from which to invoke Git when no root is provided.
        explicit_root: Optional caller-supplied repository root override.

    Returns:
        Canonical resolved path to the Git repository root.

    Raises:
        SecretScanTargetError: If Git is unavailable or ``cwd`` is outside a Git worktree.
    """
    if explicit_root is not None:
        resolved = explicit_root.resolve()
        if not resolved.is_dir():
            raise SecretScanTargetError(f"Repository root does not exist: {explicit_root}")
        return resolved

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise SecretScanTargetError(f"Failed executing git rev-parse: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or "not a git repository"
        raise SecretScanTargetError(f"Unable to determine Git repository root: {detail}")

    top_level = completed.stdout.strip()
    if not top_level:
        raise SecretScanTargetError("Git returned an empty repository root path.")
    return Path(top_level).resolve()


def _git_ls_files(repo_root: Path, rel_target: str) -> list[str]:
    """Return tracked and non-ignored untracked files under ``rel_target``.

    Args:
        repo_root: Resolved Git repository root directory.
        rel_target: Repository-relative POSIX target path (or ``"."`` for root).

    Returns:
        Ordered list of existing repository-relative POSIX file paths.

    Raises:
        SecretScanTargetError: If ``git ls-files`` fails.
    """
    cmd = [
        "git",
        "--literal-pathspecs",
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        rel_target,
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise SecretScanTargetError(f"Failed executing git ls-files: {exc}") from exc

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SecretScanTargetError(
            f"git ls-files failed for target '{rel_target}': {detail or completed.returncode}"
        )

    raw_entries = completed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    resolved_files: list[str] = []
    for entry in raw_entries:
        if not entry:
            continue
        if (repo_root / entry).is_file():
            resolved_files.append(entry)
    return resolved_files


def expand_targets(
    targets: Sequence[str],
    *,
    repo_root: Path,
    cwd: Path | None = None,
) -> list[str]:
    """Expand file and directory arguments into deduplicated Git-resolved file paths.

    Args:
        targets: Raw CLI file or directory arguments (defaults to ``["."]`` when empty).
        repo_root: Resolved Git repository root directory.
        cwd: Directory used to resolve relative CLI arguments (defaults to ``Path.cwd()``).

    Returns:
        Deduplicated list of repository-relative POSIX file paths ready for scanning.

    Raises:
        SecretScanTargetError: If any target is missing, outside ``repo_root``, or resolves
            to zero scannable files.
    """
    base_cwd = (cwd or Path.cwd()).resolve()
    resolved_root = repo_root.resolve()
    effective_targets = list(targets) if targets else ["."]

    ordered_files: dict[str, None] = {}
    for raw_target in effective_targets:
        candidate = Path(raw_target)
        if not candidate.is_absolute():
            candidate = base_cwd / candidate

        if not candidate.exists():
            raise SecretScanTargetError(f"Secret scan target does not exist: {raw_target}")

        resolved_target = candidate.resolve()
        try:
            rel_path = resolved_target.relative_to(resolved_root)
        except ValueError as exc:
            raise SecretScanTargetError(
                f"Secret scan target is outside repository root ({resolved_root}): {raw_target}"
            ) from exc

        rel_posix = "." if rel_path == Path(".") else rel_path.as_posix()
        target_files = _git_ls_files(resolved_root, rel_posix)
        if not target_files:
            raise SecretScanTargetError(
                f"Secret scan target resolved to zero scannable files: {raw_target}"
            )

        for file_path in target_files:
            ordered_files.setdefault(file_path, None)

    if not ordered_files:
        raise SecretScanTargetError("No scannable files resolved for secret scan.")

    return sorted(ordered_files)


def _invoke_detect_secrets_hook(argv: list[str]) -> int:
    """Invoke ``detect_secrets.pre_commit_hook.main`` in-process with isolated settings.

    Args:
        argv: Command-line arguments forwarded to ``detect_secrets.pre_commit_hook.main``.

    Returns:
        Integer return code from ``detect_secrets.pre_commit_hook.main``.

    Raises:
        SecretScanTargetError: If ``detect-secrets`` is not installed in the active Python
            environment.
    """
    try:
        pre_commit_hook = importlib.import_module("detect_secrets.pre_commit_hook")
        ds_settings = importlib.import_module("detect_secrets.settings")
        importlib.import_module("detect_secrets.core.baseline")
    except ModuleNotFoundError as exc:
        raise SecretScanTargetError(
            "detect-secrets is not installed in the current Python environment."
        ) from exc

    original_configure = ds_settings.configure_settings_from_baseline

    def _configure_with_basename(baseline_dict: dict[str, object], filename: str = "") -> object:
        normalized_name = os.path.basename(filename) if filename else ""
        return original_configure(baseline_dict, filename=normalized_name)

    ds_settings.cache_bust()
    try:
        with patch(
            "detect_secrets.core.baseline.configure_settings_from_baseline",
            side_effect=_configure_with_basename,
        ):
            try:
                return int(pre_commit_hook.main(argv))
            except SystemExit as exc:
                if exc.code in (0, None):
                    return 0
                return 1
    finally:
        ds_settings.cache_bust()


def run_scan_with_temp_baseline(
    files: Sequence[str],
    *,
    repo_root: Path,
    baseline_path: Path,
    hook_runner: HookRunner | None = None,
) -> int:
    """Run ``detect-secrets-hook`` against a temporary copy of ``.secrets.baseline``.

    Args:
        files: Repository-relative POSIX file paths to scan.
        repo_root: Resolved Git repository root directory.
        baseline_path: Path to the committed ``.secrets.baseline`` file.
        hook_runner: Optional hook callable override for unit tests.

    Returns:
        ``0`` when zero new unbaselined secrets are detected, or ``1`` otherwise.

    Raises:
        SecretScanTargetError: If ``baseline_path`` is missing or empty ``files`` are given.
    """
    if not files:
        raise SecretScanTargetError("No scannable files provided to secret scanner.")

    resolved_baseline = baseline_path.resolve()
    if not resolved_baseline.is_file():
        raise SecretScanTargetError(f"Secrets baseline file not found: {baseline_path}")

    resolved_root = repo_root.resolve()
    runner = hook_runner or _invoke_detect_secrets_hook
    previous_cwd = Path.cwd()

    with tempfile.TemporaryDirectory(prefix="imednet-secrets-") as temp_dir:
        temp_baseline = Path(temp_dir) / resolved_baseline.name
        shutil.copyfile(resolved_baseline, temp_baseline)

        os.chdir(resolved_root)
        try:
            rc = runner(["--baseline", str(temp_baseline), *files])
        finally:
            os.chdir(previous_cwd)

    # Return code 3 means zero new secrets were found and detect-secrets only updated
    # line numbers or trimmed entries in the discarded temporary baseline copy.
    if rc in (0, 3):
        return 0
    return 1


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for ``scripts/run_secret_scan.py``."""
    parser = argparse.ArgumentParser(
        description=(
            "Expand file and directory targets via git ls-files and scan them with "
            "detect-secrets against a temporary baseline copy."
        )
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Path to .secrets.baseline (defaults to <repo-root>/.secrets.baseline).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Optional Git repository root override.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        default=["."],
        help="Files or directories to scan (defaults to repository root '.').",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    hook_runner: HookRunner | None = None,
) -> int:
    """CLI entrypoint for read-only workspace and package secret scanning.

    Args:
        argv: Optional CLI argument sequence (defaults to ``sys.argv[1:]``).
        hook_runner: Optional callable override for ``detect_secrets.pre_commit_hook.main``.

    Returns:
        Process exit status (``0`` on clean scan, ``1`` on leaked secrets or target errors).
    """
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    invocation_cwd = Path.cwd()

    try:
        repo_root = resolve_repo_root(invocation_cwd, explicit_root=args.repo_root)
        if args.baseline is None:
            baseline_path = repo_root / DEFAULT_BASELINE_NAME
        elif args.baseline.is_absolute():
            baseline_path = args.baseline
        else:
            cwd_candidate = invocation_cwd / args.baseline
            baseline_path = cwd_candidate if cwd_candidate.is_file() else repo_root / args.baseline

        files = expand_targets(args.targets, repo_root=repo_root, cwd=invocation_cwd)
        return run_scan_with_temp_baseline(
            files,
            repo_root=repo_root,
            baseline_path=baseline_path,
            hook_runner=hook_runner,
        )
    except SecretScanTargetError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
