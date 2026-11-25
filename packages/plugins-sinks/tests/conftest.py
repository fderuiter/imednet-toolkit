"""Test configuration bridge for standalone imednet-plugins-sinks test runs."""

# pylint: disable=duplicate-code

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tests.utils.isolation import (  # noqa: E402
    apply_track_marker_skips,
    register_track_options,
    run_package_test_isolation,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register verification track CLI options when running sinks tests standalone."""
    register_track_options(parser)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip fuzzing/performance tests unless explicitly requested."""
    apply_track_marker_skips(config, items)


@pytest.fixture(autouse=True)
def _isolate_sinks_test_state(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Ensure hermetic HOME and ContextVar isolation for standalone sinks test runs."""
    yield from run_package_test_isolation(request, monkeypatch, tmp_path)
