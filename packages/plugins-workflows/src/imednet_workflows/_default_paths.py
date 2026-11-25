"""Dynamic default ``~/.imednet`` path resolution helpers for workflow stores."""

from __future__ import annotations

import os
from pathlib import Path

_ORIG_PATH_HOME_FUNC = getattr(Path.home, "__func__", Path.home)


def expand_user_path(raw_path: str | Path) -> Path:
    """Expand ``~`` honoring monkeypatched ``Path.home()`` as well as ``HOME``."""
    raw_str = os.fspath(raw_path)
    home_fn = Path.home
    if getattr(home_fn, "__func__", home_fn) is not _ORIG_PATH_HOME_FUNC:
        if raw_str == "~":
            return Path(home_fn())
        if raw_str.startswith(("~/", "~\\")):
            return Path(home_fn()) / raw_str[2:]
    return Path(raw_path).expanduser()


def resolve_home_dir() -> Path:
    """Resolve the active user home directory dynamically at call time."""
    home_fn = Path.home
    if getattr(home_fn, "__func__", home_fn) is not _ORIG_PATH_HOME_FUNC:
        return Path(home_fn())
    env_home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if env_home:
        return Path(env_home)
    return Path(home_fn())


def resolve_default_imednet_path(
    subpath: str | Path,
    *,
    env_var: str | None = None,
    current_default: str | Path | None = None,
    initial_default: str | Path | None = None,
) -> Path:
    """Resolve ``~/.imednet/<subpath>`` honoring monkeypatches, env vars, and dynamic ``HOME``."""
    dynamic_default = resolve_home_dir() / ".imednet" / subpath
    if current_default is not None and initial_default is not None:
        resolved_current = expand_user_path(current_default)
        resolved_initial = expand_user_path(initial_default)
        if resolved_current not in {resolved_initial, dynamic_default}:
            return resolved_current
    if env_var is not None:
        raw_env = os.environ.get(env_var)
        if raw_env:
            return expand_user_path(raw_env)
    return dynamic_default


def is_default_ledger_sentinel(
    ledger_path: str | Path | None,
    *,
    current_default: str,
    initial_default: str,
    import_default: str | None = None,
) -> bool:
    """Return ``True`` when ``ledger_path`` matches an import-time or active default sentinel."""
    if ledger_path is None:
        return False
    sentinels = {current_default, initial_default}
    if import_default is not None:
        sentinels.add(import_default)
    return os.fspath(ledger_path) in sentinels


def resolve_default_ledger_path(
    *,
    current_default: str,
    initial_default: str,
) -> str:
    """Resolve the default workflow ledger path dynamically at call time."""
    return str(
        resolve_default_imednet_path(
            "pipeline_ledger.json",
            env_var="IMEDNET_WORKFLOW_LEDGER_PATH",
            current_default=current_default,
            initial_default=initial_default,
        )
    )
