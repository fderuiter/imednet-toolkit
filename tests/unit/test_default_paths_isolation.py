"""Unit tests for decoupled workflow _default_paths and tests.utils.isolation helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.utils.isolation import (
    apply_track_marker_skips,
    isolate_home_directory,
    register_track_options,
    reset_workflow_store_caches,
)

from imednet_workflows._default_paths import (
    expand_user_path,
    is_default_ledger_sentinel,
    resolve_default_imednet_path,
    resolve_default_ledger_path,
    resolve_home_dir,
)
from imednet_workflows.config_version_control import (
    _RAW_DB_PATH_CACHE,
    _get_raw_db_path,
    clear_config_version_store_caches,
)


def test_default_paths_honors_patched_path_home_env_and_monkeypatched_constants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify _default_paths precedence across monkeypatched constants, env vars, and Path.home."""
    patched_home = tmp_path / "patched_home"
    patched_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: patched_home))

    assert resolve_home_dir() == patched_home
    assert expand_user_path("~") == patched_home
    assert expand_user_path("~/nested/db.sqlite3") == patched_home / "nested" / "db.sqlite3"
    assert expand_user_path("plain/relative") == Path("plain/relative")

    initial_default = tmp_path / "initial" / ".imednet" / "cache"
    monkeypatched_default = tmp_path / "custom_constant_cache"
    assert (
        resolve_default_imednet_path(
            "cache",
            current_default=monkeypatched_default,
            initial_default=initial_default,
        )
        == monkeypatched_default
    )

    monkeypatch.setenv("IMEDNET_TEST_CUSTOM_PATH", "~/override_store.sqlite3")
    assert (
        resolve_default_imednet_path(
            "store.sqlite3",
            env_var="IMEDNET_TEST_CUSTOM_PATH",
            current_default=initial_default,
            initial_default=initial_default,
        )
        == patched_home / "override_store.sqlite3"
    )

    monkeypatch.delenv("IMEDNET_TEST_CUSTOM_PATH", raising=False)
    assert (
        resolve_default_imednet_path(
            "store.sqlite3",
            env_var="IMEDNET_TEST_CUSTOM_PATH",
            current_default=initial_default,
            initial_default=initial_default,
        )
        == patched_home / ".imednet" / "store.sqlite3"
    )


def test_default_ledger_sentinel_and_clear_config_version_store_caches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify ledger sentinel detection and ConfigVersionStore cache clearing."""
    env_home = tmp_path / "env_home"
    env_home.mkdir()
    monkeypatch.setenv("HOME", str(env_home))
    monkeypatch.delenv("IMEDNET_WORKFLOW_LEDGER_PATH", raising=False)
    monkeypatch.delenv("IMEDNET_CONFIG_DB_PATH", raising=False)

    init_sentinel = "/old/home/.imednet/pipeline_ledger.json"
    cur_sentinel = str(env_home / ".imednet" / "pipeline_ledger.json")
    assert is_default_ledger_sentinel(
        init_sentinel, current_default=cur_sentinel, initial_default=init_sentinel
    )
    assert is_default_ledger_sentinel(
        cur_sentinel, current_default=cur_sentinel, initial_default=init_sentinel
    )
    assert not is_default_ledger_sentinel(
        None, current_default=cur_sentinel, initial_default=init_sentinel
    )
    assert not is_default_ledger_sentinel(
        tmp_path / "explicit.json",
        current_default=cur_sentinel,
        initial_default=init_sentinel,
    )
    assert (
        resolve_default_ledger_path(current_default=cur_sentinel, initial_default=init_sentinel)
        == cur_sentinel
    )

    resolved_cfg_db = _get_raw_db_path()
    assert resolved_cfg_db == env_home / ".imednet" / "config_versions.sqlite3"
    assert len(_RAW_DB_PATH_CACHE) > 0

    clear_config_version_store_caches()
    assert len(_RAW_DB_PATH_CACHE) == 0


def test_isolation_helpers_idempotent_options_and_marker_skipping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify register_track_options is idempotent and apply_track_marker_skips gates markers."""
    seen_flags: set[str] = set()

    class _FakeParser:
        def addoption(self, flag: str, **_kwargs: object) -> None:
            if flag in seen_flags:
                raise ValueError(f"duplicate {flag}")
            seen_flags.add(flag)

    parser = _FakeParser()
    register_track_options(parser)  # type: ignore[arg-type]
    register_track_options(parser)  # type: ignore[arg-type]
    assert seen_flags == {"--run-fuzzing", "--run-performance", "--update-perf-baseline"}

    perf_item = SimpleNamespace(keywords={"performance": True}, add_marker=MagicMock())
    fuzz_item = SimpleNamespace(keywords={"fuzzing": True}, add_marker=MagicMock())
    unit_item = SimpleNamespace(keywords={"unit": True}, add_marker=MagicMock())

    opts = {
        "-m": "",
        "--run-fuzzing": False,
        "--run-performance": False,
        "--update-perf-baseline": False,
    }
    fake_cfg = SimpleNamespace(getoption=lambda key: opts[key])
    apply_track_marker_skips(fake_cfg, [perf_item, fuzz_item, unit_item])  # type: ignore[arg-type,list-item]
    assert perf_item.add_marker.call_count == 1
    assert fuzz_item.add_marker.call_count == 1
    assert unit_item.add_marker.call_count == 0

    isolated = isolate_home_directory(monkeypatch, tmp_path)
    assert Path.home() == isolated
    reset_workflow_store_caches()
