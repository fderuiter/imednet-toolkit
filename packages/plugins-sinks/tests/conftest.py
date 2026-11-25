# pylint: disable=duplicate-code
"""Bridge shared pytest options, marker skipping, and isolation fixtures for sink plugin tests."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tests.conftest import (  # noqa: E402, F401
    isolate_logging_handlers_between_tests,
    isolate_test_home,
    pytest_addoption,
    pytest_collection_modifyitems,
    reset_circuit_breaker_between_tests,
    reset_simulation_state_between_tests,
    reset_store_caches_between_tests,
    reset_study_context_between_tests,
)
