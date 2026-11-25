"""Shared hermetic test-state isolation and verification track hooks.

Centralizes ``HOME`` redirection, ``ContextVar`` cleanup, workflow store cache
invalidation, simulation state resets, and ``@pytest.mark.performance`` /
``@pytest.mark.fuzzing`` collection filtering across all test roots.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

ISOLATION_ACTIVE_ATTR = "_imednet_test_isolation_active"


def register_track_options(parser: pytest.Parser) -> None:
    """Idempotently register custom CLI flags for fuzzing and performance tracks."""
    options = (
        (
            "--run-fuzzing",
            {"action": "store_true", "default": False, "help": "run fuzzing tests"},
        ),
        (
            "--run-performance",
            {"action": "store_true", "default": False, "help": "run performance tests"},
        ),
        (
            "--update-perf-baseline",
            {
                "action": "store_true",
                "default": False,
                "help": "recompute and overwrite committed performance baseline ratios",
            },
        ),
    )
    for flag, kwargs in options:
        try:
            parser.addoption(flag, **kwargs)
        except ValueError:
            # Already registered by another conftest in the same pytest session.
            pass


def apply_track_marker_skips(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip fuzzing and performance tests unless explicitly enabled via CLI flags or ``-m``."""
    skip_fuzzing = pytest.mark.skip(reason="need --run-fuzzing option or -m fuzzing to run")
    skip_performance = pytest.mark.skip(
        reason="need --run-performance option or -m performance to run"
    )

    marker_expr = config.getoption("-m") or ""
    run_fuzzing = bool(config.getoption("--run-fuzzing"))
    run_performance = bool(config.getoption("--run-performance")) or bool(
        config.getoption("--update-perf-baseline")
    )

    for item in items:
        if "fuzzing" in item.keywords and not run_fuzzing and "fuzzing" not in marker_expr:
            item.add_marker(skip_fuzzing)
        if (
            "performance" in item.keywords
            and not run_performance
            and "performance" not in marker_expr
        ):
            item.add_marker(skip_performance)


def isolate_home_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect ``HOME`` / ``USERPROFILE`` and default ledger constants into *tmp_path*."""
    isolated_home = tmp_path / ".imednet_test_home"
    isolated_home.mkdir(parents=True, exist_ok=True)

    prior_candidates: set[str] = {str(Path.home() / ".imednet" / "pipeline_ledger.json")}
    for mod_key in (
        "imednet_workflows.state_ledger",
        "imednet_workflows.cli",
        "imednet_workflows.wizard",
    ):
        candidate_mod = sys.modules.get(mod_key)
        if candidate_mod is None:
            continue
        for attr in (
            "DEFAULT_LEDGER_PATH",
            "_INITIAL_DEFAULT_LEDGER_PATH",
            "_IMPORT_DEFAULT_LEDGER_PATH",
        ):
            val = getattr(candidate_mod, attr, None)
            if isinstance(val, str):
                prior_candidates.add(val)

    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("USERPROFILE", str(isolated_home))

    if "IMEDNET_WORKFLOW_LEDGER_PATH" not in os.environ:
        isolated_ledger = str(isolated_home / ".imednet" / "pipeline_ledger.json")
        for mod_name, mod in list(sys.modules.items()):
            if mod is None or not mod_name.startswith(
                ("imednet", "apache_airflow_providers_imednet", "tests", "test_")
            ):
                continue
            mod_dict = getattr(mod, "__dict__", None)
            if not isinstance(mod_dict, dict):
                continue
            cur = mod_dict.get("DEFAULT_LEDGER_PATH")
            if isinstance(cur, str) and cur in prior_candidates:
                monkeypatch.setattr(mod, "DEFAULT_LEDGER_PATH", isolated_ledger, raising=False)
            init_cur = mod_dict.get("_INITIAL_DEFAULT_LEDGER_PATH")
            if isinstance(init_cur, str) and init_cur in prior_candidates:
                monkeypatch.setattr(
                    mod, "_INITIAL_DEFAULT_LEDGER_PATH", isolated_ledger, raising=False
                )

    return isolated_home


def reset_all_runtime_contexts() -> None:
    """Reset all core SDK ``ContextVar`` values (study, origin, security key, strict mode)."""
    from imednet.core.context import clear_all_contexts

    clear_all_contexts()


def reset_workflow_store_caches() -> None:
    """Clear module-level ``ConfigVersionStore`` caches if the workflows module is loaded."""
    cvc_mod = sys.modules.get("imednet_workflows.config_version_control")
    if cvc_mod is not None and hasattr(cvc_mod, "clear_config_version_store_caches"):
        cvc_mod.clear_config_version_store_caches()


def reset_default_simulation_state_if_loaded() -> None:
    """Reset the global default ``SimulationState`` if initialized."""
    sim_state_mod = sys.modules.get("imednet.simulation.state")
    if sim_state_mod is not None and getattr(sim_state_mod, "_default_state", None) is not None:
        sim_state_mod.reset_default_simulation_state()


def reset_global_circuit_breaker_if_loaded() -> None:
    """Reset the global circuit breaker if loaded."""
    cb_mod = sys.modules.get("imednet.core.operations.circuit_breaker")
    if cb_mod is not None and hasattr(cb_mod, "get_global_circuit_breaker"):
        cb_mod.get_global_circuit_breaker().reset()


def run_package_test_isolation(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Any:
    """Provide standalone package-test isolation when ``tests/conftest.py`` is not active."""
    if getattr(request.node, ISOLATION_ACTIVE_ATTR, False):
        yield
        return

    setattr(request.node, ISOLATION_ACTIVE_ATTR, True)
    os.environ.setdefault("IMEDNET_SECURITY_KEY", "test_sec_key")  # pragma: allowlist secret
    isolate_home_directory(monkeypatch, tmp_path)
    reset_all_runtime_contexts()
    reset_workflow_store_caches()
    reset_default_simulation_state_if_loaded()
    reset_global_circuit_breaker_if_loaded()
    try:
        yield
    finally:
        reset_all_runtime_contexts()
        reset_workflow_store_caches()
        reset_default_simulation_state_if_loaded()
        reset_global_circuit_breaker_if_loaded()
