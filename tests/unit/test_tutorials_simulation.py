"""Offline SimulationTransport verification suite for basic tutorials, custom retry, and showcase.js snippets."""

from __future__ import annotations

import runpy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from scripts.validate_showcases import (
    SHOWCASE_EXPECTED_SDK_SCENARIOS,
    extract_showcase_sdk_snippets,
)

from imednet import ImednetSDK
from imednet.cli import app
from imednet.errors import ConfigurationError
from imednet.models.forms import Form
from imednet.models.queries import Query
from imednet.models.records import Record
from imednet.models.studies import Study
from imednet.simulation import SimulationTransport
from imednet.simulation.state import get_default_simulation_state

REPO_ROOT = Path(__file__).resolve().parents[2]
BASIC_EXAMPLES_DIR = REPO_ROOT / "examples" / "basic"
BASIC_EXAMPLE_SCRIPTS = sorted(BASIC_EXAMPLES_DIR.glob("*.py"))


@pytest.fixture(autouse=True)
def _reset_simulation_state() -> Iterator[None]:
    """Reset shared default SimulationState before and after each test."""
    get_default_simulation_state().reset()
    yield
    get_default_simulation_state().reset()


def test_basic_examples_inventory() -> None:
    """Ensure all nine basic tutorial scripts are discovered."""
    assert len(BASIC_EXAMPLE_SCRIPTS) == 9


@pytest.mark.parametrize(
    "script_path",
    BASIC_EXAMPLE_SCRIPTS,
    ids=lambda p: p.name,
)
def test_basic_tutorial_scripts_execute_under_simulation_transport(
    script_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run each examples/basic/*.py script offline via SimulationTransport without TypeError or ConfigurationError."""
    monkeypatch.chdir(tmp_path)
    for env_key in (
        "IMEDNET_API_KEY",
        "IMEDNET_SECURITY_KEY",
        "IMEDNET_BASE_URL",
        "IMEDNET_STUDY_KEY",
    ):
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv("IMEDNET_SIMULATION_MODE", "true")

    transport = SimulationTransport(state=get_default_simulation_state())
    original_init = ImednetSDK.__init__

    def _patched_init(self: ImednetSDK, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("transport", transport)
        kwargs.setdefault("simulation_mode", True)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(ImednetSDK, "__init__", _patched_init)

    capsys.readouterr()
    try:
        runpy.run_path(str(script_path), run_name="__main__")
    except (TypeError, ConfigurationError, SystemExit) as exc:
        pytest.fail(
            f"{script_path.name} failed under SimulationTransport: {type(exc).__name__}: {exc}"
        )

    assert capsys.readouterr().out.strip(), f"Expected non-empty stdout from {script_path.name}"


def test_custom_retry_script_executes_under_simulation_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify examples/custom_retry.py passes RetryConfig and succeeds offline across repeated runs."""
    custom_retry_path = REPO_ROOT / "examples" / "custom_retry.py"
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.setenv("IMEDNET_SIMULATION_MODE", "true")

    for _ in range(2):
        capsys.readouterr()
        try:
            runpy.run_path(str(custom_retry_path), run_name="__main__")
        except (TypeError, ConfigurationError, SystemExit) as exc:
            pytest.fail(f"custom_retry.py failed offline: {type(exc).__name__}: {exc}")

        assert "Request succeeded" in capsys.readouterr().out


@pytest.mark.parametrize("scenario", SHOWCASE_EXPECTED_SDK_SCENARIOS)
def test_showcase_js_sdk_snippets_execute_under_simulation_transport(
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Execute each SNIPPETS.sdk template from docs/_static/showcase.js with SimulationTransport."""
    for env_key in ("IMEDNET_API_KEY", "IMEDNET_SECURITY_KEY", "IMEDNET_BASE_URL"):
        monkeypatch.delenv(env_key, raising=False)

    showcase_js = REPO_ROOT / "docs" / "_static" / "showcase.js"
    snippets, errs = extract_showcase_sdk_snippets(showcase_js)
    assert not errs, f"Unexpected showcase.js extraction errors: {errs}"

    transport = SimulationTransport(state=get_default_simulation_state())
    original_init = ImednetSDK.__init__

    def _patched_init(self: ImednetSDK, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("transport", transport)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(ImednetSDK, "__init__", _patched_init)

    namespace: dict[str, Any] = {"__name__": "__showcase_test__"}
    capsys.readouterr()
    exec(compile(snippets[scenario], f"showcase.js:{scenario}", "exec"), namespace)
    capsys.readouterr()

    expected_models: dict[str, type[object]] = {
        "records": Record,
        "studies": Study,
        "forms": Form,
        "queries": Query,
    }
    items = namespace.get(scenario)
    assert isinstance(items, list) and len(items) > 0
    assert all(isinstance(item, expected_models[scenario]) for item in items)
    if scenario == "records":
        df = namespace.get("df")
        assert df is not None and len(df) > 0


def test_documented_cli_query_scope_filter_succeeds_in_simulation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify the documented Query target-scope CLI filter in docs/how-to/cli.rst executes cleanly."""
    monkeypatch.setenv("IMEDNET_SIMULATION_MODE", "true")
    monkeypatch.setenv("IMEDNET_NON_INTERACTIVE", "1")
    capsys.readouterr()
    app(
        [
            "queries",
            "list",
            "STUDY",
            "--filter",
            "type==record,type==question",
            "--sort",
            "annotationId,desc",
        ]
    )
    capsys.readouterr()
