"""Signature parity, runtime delegation, and static Mypy tests for Airflow exports."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, get_type_hints
from unittest.mock import MagicMock

import pytest
from mypy import api as mypy_api

import imednet
from apache_airflow_providers_imednet import export as airflow_export
from imednet.integrations import export as core_export
from imednet.integrations import parquet as core_parquet
from imednet.spi import export_helpers as spi_export_helpers

ROOT = Path(__file__).resolve().parents[2]

EXPECTED_EXPORTS = (
    "export_to_csv",
    "export_to_duckdb",
    "export_to_duckdb_by_form",
    "export_to_excel",
    "export_to_hive_parquet",
    "export_to_json",
    "export_to_long_sql",
    "export_to_parquet",
    "export_to_sql",
    "export_to_sql_by_form",
)


def _canonical_core_callable(name: str) -> Any:
    if name == "export_to_hive_parquet":
        return core_parquet.export_to_hive_parquet
    return getattr(core_export, name)


@pytest.mark.parametrize("helper_name", EXPECTED_EXPORTS)
def test_airflow_export_signatures_and_type_hints_match_core(helper_name: str) -> None:
    """Ensure every Airflow export helper matches the canonical core signature and type hints."""
    airflow_fn = getattr(airflow_export, helper_name)
    core_fn = _canonical_core_callable(helper_name)

    airflow_sig = inspect.signature(airflow_fn)
    core_sig = inspect.signature(core_fn)
    assert list(airflow_sig.parameters) == list(core_sig.parameters)
    for param_name, airflow_param in airflow_sig.parameters.items():
        core_param = core_sig.parameters[param_name]
        assert airflow_param.kind == core_param.kind
        assert airflow_param.default == core_param.default

    eval_ns = {
        "ImednetSDK": imednet.ImednetSDK,
        "ImednetClient": imednet.ImednetClient,
    }
    airflow_hints = get_type_hints(airflow_fn, localns=eval_ns)
    core_hints = get_type_hints(core_fn, localns=eval_ns)
    assert airflow_hints == core_hints


@pytest.mark.parametrize("helper_name", EXPECTED_EXPORTS)
def test_airflow_export_delegates_runtime_calls_verbatim(
    helper_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify each Airflow export helper forwards positional and keyword args to SPI export_helpers."""
    spy = MagicMock(return_value=None)
    monkeypatch.setattr(spi_export_helpers, helper_name, spy)

    sdk = MagicMock()
    airflow_fn = getattr(airflow_export, helper_name)
    airflow_fn(sdk, "STUDY-1", "/tmp/target", extra_flag=True)

    spy.assert_called_once_with(sdk, "STUDY-1", "/tmp/target", extra_flag=True)


def test_airflow_export_mypy_contracts_and_invalid_call_rejection(tmp_path: Path) -> None:
    """Verify Mypy accepts valid Airflow export contracts and rejects invalid argument types."""
    contract_file = ROOT / "tests" / "typing" / "airflow_export_contracts.py"
    stdout, stderr, exit_code = mypy_api.run(
        [
            "--config-file",
            str(ROOT / "pyproject.toml"),
            str(contract_file),
        ]
    )
    assert exit_code == 0, f"Airflow export contract failed mypy:\n{stdout}\n{stderr}"

    invalid_snippet = tmp_path / "invalid_airflow_calls.py"
    invalid_snippet.write_text(
        "from apache_airflow_providers_imednet import export\n"
        "from imednet import ImednetClient, ImednetSDK\n\n"
        "def bad_calls(sdk: ImednetSDK, client: ImednetClient) -> None:\n"
        '    export.export_to_csv(sdk, "STUDY")\n'
        '    export.export_to_long_sql(client, "STUDY", "tbl", "sqlite://", chunk_size="bad")\n'
        '    export.export_to_sql(sdk, "STUDY", "tbl", "sqlite://", "invalid_mode")\n',
        encoding="utf-8",
    )
    bad_stdout, bad_stderr, bad_code = mypy_api.run(
        [
            "--config-file",
            str(ROOT / "pyproject.toml"),
            str(invalid_snippet),
        ]
    )
    assert bad_code != 0, (
        f"Expected mypy failure on invalid Airflow calls:\n{bad_stdout}\n{bad_stderr}"
    )
    assert "[call-arg]" in bad_stdout
    assert "[arg-type]" in bad_stdout
