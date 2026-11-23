"""Tests asserting the public interface shape.

Validates importability, ``__all__`` coverage, and type annotations across
the stable public namespace.
"""

from __future__ import annotations

import importlib
import inspect
import subprocess
import sys
from pathlib import Path
from typing import get_type_hints

import pytest

# ---------------------------------------------------------------------------
# Modules that MUST declare __all__
# ---------------------------------------------------------------------------
PUBLIC_MODULES_WITH_ALL = [
    "imednet",
    "imednet.auth",
    "imednet.core",
    "imednet.core.endpoint",
    "imednet.core.endpoint.operations",
    "imednet.core.http",
    "imednet.endpoints",
    "imednet.errors",
    "imednet.form_designer",
    "imednet.integrations",
    "imednet.models",
    "imednet.orchestration",
    "imednet.pagination",
    "imednet.utils",
    "imednet.validation",
]

# The testing module requires optional faker; checked separately below.
OPTIONAL_MODULES_WITH_ALL = [
    "imednet.testing",
]


@pytest.mark.parametrize("module_name", PUBLIC_MODULES_WITH_ALL)
def test_module_has_all(module_name: str) -> None:
    """Every public module must declare __all__."""
    mod = importlib.import_module(module_name)
    assert hasattr(mod, "__all__"), (
        f"{module_name} must define __all__ to declare its public surface"
    )
    assert isinstance(mod.__all__, (list, tuple)), f"{module_name}.__all__ must be a list or tuple"


@pytest.mark.parametrize("module_name", OPTIONAL_MODULES_WITH_ALL)
def test_optional_module_has_all(module_name: str) -> None:
    """Optional modules (extra dependencies) must also declare __all__ when available."""
    mod = pytest.importorskip(module_name)
    assert hasattr(mod, "__all__"), (
        f"{module_name} must define __all__ to declare its public surface"
    )


# ---------------------------------------------------------------------------
# Key type aliases are importable from the top-level package
# ---------------------------------------------------------------------------
def test_top_level_type_aliases() -> None:
    """FilterValue, ItemId, JsonDict and FilterScalar are re-exported from imednet."""
    import imednet

    assert hasattr(imednet, "FilterValue"), "imednet.FilterValue must be exported"
    assert hasattr(imednet, "FilterScalar"), "imednet.FilterScalar must be exported"
    assert hasattr(imednet, "ItemId"), "imednet.ItemId must be exported"
    assert hasattr(imednet, "JsonDict"), "imednet.JsonDict must be exported"

    for name in ("FilterValue", "FilterScalar", "ItemId", "JsonDict"):
        assert name in imednet.__all__, f"{name!r} must appear in imednet.__all__"


def test_type_aliases_importable_from_utils() -> None:
    """Type aliases must also be importable from imednet.utils."""
    from imednet.utils import FilterValue, ItemId, JsonDict  # noqa: F401


# ---------------------------------------------------------------------------
# Public endpoint methods must NOT use bare `Any` in their signatures
# ---------------------------------------------------------------------------


def _is_any(tp: object) -> bool:
    """Return True if *tp* is typing.Any."""
    from typing import Any

    return tp is Any


def _get_type_hints_safe(method: object) -> dict[str, object]:
    """Return resolved type hints for *method*, unwrapping partial/bound wrappers."""
    if hasattr(method, "func"):
        return get_type_hints(method.func)  # type: ignore[arg-type]
    return get_type_hints(method)  # type: ignore[arg-type]


def test_jobs_endpoint_get_no_any() -> None:
    """JobsEndpoint.get must use ItemId for item_id, not Any."""
    from imednet.endpoints.jobs import JobsEndpoint

    hints = _get_type_hints_safe(JobsEndpoint.get)
    assert not _is_any(hints.get("item_id")), "JobsEndpoint.get: item_id must not be typed as Any"


def test_async_jobs_endpoint_get_no_any() -> None:
    """AsyncJobsEndpoint.get must use ItemId for item_id, not Any."""
    from imednet.endpoints.jobs import AsyncJobsEndpoint

    hints = _get_type_hints_safe(AsyncJobsEndpoint.get)
    assert not _is_any(hints.get("item_id")), (
        "AsyncJobsEndpoint.get: item_id must not be typed as Any"
    )


def _get_kwargs_type(method: object, param_name: str = "filters") -> object:
    """Return the **kwargs type hint for *param_name*, or None if absent."""
    hints = _get_type_hints_safe(method)  # type: ignore[arg-type]
    # get_type_hints stores **kwargs under the parameter's actual name
    return hints.get(param_name)


def test_sync_list_get_endpoint_list_uses_filter_value() -> None:
    """SyncListGetEndpoint.list must use FilterValue for **filters, not Any."""
    from imednet.core.endpoint.base import SyncListGetEndpoint

    tp = _get_kwargs_type(SyncListGetEndpoint.list, "filters")
    assert tp is not None, "SyncListGetEndpoint.list must annotate **filters"
    assert not _is_any(tp), "SyncListGetEndpoint.list: **filters must not be typed as Any"


def test_sync_list_endpoint_list_uses_filter_value() -> None:
    """SyncListEndpoint.list must use FilterValue for **filters, not Any."""
    from imednet.core.endpoint.base import SyncListEndpoint

    tp = _get_kwargs_type(SyncListEndpoint.list, "filters")
    assert tp is not None, "SyncListEndpoint.list must annotate **filters"
    assert not _is_any(tp), "SyncListEndpoint.list: **filters must not be typed as Any"


def test_async_list_get_endpoint_async_list_uses_filter_value() -> None:
    """AsyncListGetEndpoint.list must use FilterValue for **filters, not Any."""
    from imednet.core.endpoint.base import AsyncListGetEndpoint

    tp = _get_kwargs_type(AsyncListGetEndpoint.list, "filters")
    assert tp is not None, "AsyncListGetEndpoint.list must annotate **filters"
    assert not _is_any(tp), "AsyncListGetEndpoint.list: **filters must not be typed as Any"


def test_async_list_endpoint_async_list_uses_filter_value() -> None:
    """AsyncListEndpoint.list must use FilterValue for **filters, not Any."""
    from imednet.core.endpoint.base import AsyncListEndpoint

    tp = _get_kwargs_type(AsyncListEndpoint.list, "filters")
    assert tp is not None, "AsyncListEndpoint.list must annotate **filters"
    assert not _is_any(tp), "AsyncListEndpoint.list: **filters must not be typed as Any"


def test_records_endpoint_create_no_bare_dict_any() -> None:
    """RecordsEndpoint.create must annotate records_data with JsonDict, not Dict[str, Any]."""
    from imednet.endpoints.records import RecordsEndpoint

    hints = _get_type_hints_safe(RecordsEndpoint.create)
    records_data_type = hints.get("records_data")
    assert records_data_type is not None, "RecordsEndpoint.create must annotate records_data"
    assert not _is_any(records_data_type), (
        "RecordsEndpoint.create: records_data must not be typed as Any"
    )


# ---------------------------------------------------------------------------
# SupportsList and SupportsGet protocols use typed parameters
# ---------------------------------------------------------------------------


def test_supports_list_protocol_uses_filter_value() -> None:
    """SupportsList.list must declare **filters as FilterValue."""
    from imednet.core.endpoint.protocols import SupportsList

    tp = _get_kwargs_type(SupportsList.list, "filters")
    assert tp is not None, "SupportsList.list must annotate **filters"
    assert not _is_any(tp), "SupportsList.list: **filters must not be typed as Any"


def test_supports_get_protocol_uses_item_id() -> None:
    """SupportsGet.get must declare item_id as ItemId."""
    from imednet.core.endpoint.protocols import SupportsGet

    hints = get_type_hints(SupportsGet.get)
    assert not _is_any(hints.get("item_id")), "SupportsGet.get: item_id must not be typed as Any"


# ---------------------------------------------------------------------------
# Convenience methods use FilterValue
# ---------------------------------------------------------------------------


def test_sdk_convenience_mixin_uses_filter_value() -> None:
    """SDKConvenienceMixin helper methods must annotate **filters as FilterValue."""
    from imednet.sdk_convenience import SDKConvenienceMixin

    class DummySDK(SDKConvenienceMixin):
        """Test suite for DummySDK."""

        def __init__(self):
            """Initialize the test object."""
            from unittest.mock import MagicMock

            self.records = MagicMock()
            self.subjects = MagicMock()

    dummy = DummySDK()

    # spot-check a few methods
    for method_name in ("get_records", "get_subjects"):
        method = getattr(dummy, method_name)
        tp = _get_kwargs_type(method, "filters")
        if tp is not None:
            assert not _is_any(tp), (
                f"SDKConvenienceMixin.{method_name}: **filters must not be typed as Any"
            )


# ---------------------------------------------------------------------------
# Lazy barrel __all__ resolution, callable export, get_type_hints, and mypy
# ---------------------------------------------------------------------------

RUNTIME_ALL_MODULES = [
    *PUBLIC_MODULES_WITH_ALL,
    "imednet.spi",
    "imednet.spi.cli",
    "imednet.cli.utils",
    "imednet_workflows",
]

RUNTIME_HINT_MODULES = [
    "imednet",
    "imednet.utils",
    "imednet.validation",
    "imednet.endpoints",
    "imednet.spi.facade",
    "imednet.plugins",
    "imednet_workflows",
]


@pytest.mark.parametrize("module_name", RUNTIME_ALL_MODULES)
def test_all_public_symbols_resolve_at_runtime(module_name: str) -> None:
    """Every symbol listed in __all__ must resolve via getattr at runtime."""
    mod = importlib.import_module(module_name)
    missing: list[str] = []
    for symbol in mod.__all__:
        try:
            getattr(mod, symbol)
        except AttributeError:
            missing.append(symbol)
    assert not missing, f"{module_name} failed to resolve __all__ attributes: {missing}"


def test_testing_module_resolves_all_helpers_lazily() -> None:
    """All 17 helpers in imednet.testing.__all__ must resolve via PEP 562 __getattr__."""
    mod = pytest.importorskip("imednet.testing")
    assert len(mod.__all__) == 17
    for symbol in mod.__all__:
        resolved = getattr(mod, symbol)
        assert resolved is not None, f"imednet.testing.{symbol} resolved to None"


def test_callable_export_fresh_import_and_submodule_compat() -> None:
    """imednet.integrations.export and imednet.spi.export must be identical callables on fresh import."""
    fresh_script = """
from types import SimpleNamespace
from unittest.mock import patch
import imednet.integrations as integrations
import imednet.spi as spi

assert callable(integrations.export)
assert callable(spi.export)
assert spi.export is integrations.export
assert hasattr(integrations.export, "export_to_csv")
assert hasattr(integrations.export, "TabularSinkConfig")

sdk_stub = SimpleNamespace()
with patch("imednet.integrations.export.export_to_csv", return_value=42) as mock_csv:
    assert spi.export("csv", sdk_stub, "STUDY-1", path="out.csv") == 42
    mock_csv.assert_called_once_with(sdk_stub, "STUDY-1", path="out.csv")
"""
    submodule_first_script = """
import imednet.integrations.export as export_mod
import imednet.integrations as integrations
import imednet.spi as spi

assert callable(export_mod)
assert integrations.export is export_mod
assert spi.export is export_mod
"""
    for script in (fresh_script, submodule_first_script):
        res = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert res.returncode == 0, res.stderr


@pytest.mark.parametrize("module_name", RUNTIME_HINT_MODULES)
def test_runtime_get_type_hints_resolves_public_surfaces(module_name: str) -> None:
    """typing.get_type_hints() must succeed without NameError across public classes and functions."""
    mod = importlib.import_module(module_name)
    export_names = getattr(mod, "__all__", [n for n in dir(mod) if not n.startswith("_")])
    failures: list[str] = []

    for name in export_names:
        obj = getattr(mod, name)
        if inspect.isclass(obj):
            try:
                get_type_hints(obj)
            except Exception as exc:  # pragma: no cover - failure reporting
                failures.append(f"{module_name}.{name}: {exc!r}")
            for member_name, member in inspect.getmembers(obj):
                if member_name.startswith("_") and member_name not in ("__init__", "__call__"):
                    continue
                target_fn = None
                if isinstance(member, property) and member.fget is not None:
                    target_fn = member.fget
                elif inspect.isfunction(member) or inspect.ismethod(member):
                    target_fn = member
                if target_fn is None:
                    continue
                fn_mod = getattr(target_fn, "__module__", "") or ""
                if not fn_mod.startswith("imednet"):
                    continue
                try:
                    get_type_hints(target_fn)
                except Exception as exc:  # pragma: no cover - failure reporting
                    failures.append(f"{module_name}.{name}.{member_name}: {exc!r}")
        elif inspect.isfunction(obj):
            try:
                get_type_hints(obj)
            except Exception as exc:  # pragma: no cover - failure reporting
                failures.append(f"{module_name}.{name}: {exc!r}")

    assert not failures, "get_type_hints() failures:\n" + "\n".join(failures)


def test_records_to_dataframe_get_type_hints_without_pandas() -> None:
    """to_dataframe type hints must fall back to Any when pandas is absent."""
    script = """
import sys
import typing

sys.modules["pandas"] = None
from imednet.endpoints import AsyncRecordsEndpoint, RecordsEndpoint
from imednet.endpoints.users import AsyncUsersEndpoint, UsersEndpoint

assert typing.get_type_hints(RecordsEndpoint.to_dataframe)["return"] is typing.Any
assert typing.get_type_hints(UsersEndpoint.to_dataframe)["return"] is typing.Any
assert "return" in typing.get_type_hints(AsyncRecordsEndpoint.to_dataframe)
assert "return" in typing.get_type_hints(AsyncUsersEndpoint.to_dataframe)
"""
    res = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr


def test_cold_start_import_and_sdk_instantiation_budgets() -> None:
    """Importing imednet and instantiating ImednetSDK must not eagerly load models or heavy deps."""
    script = """
import sys
import imednet

for heavy in ("pandas", "numpy", "pyarrow", "cryptography"):
    assert heavy not in sys.modules, f"{heavy} loaded eagerly on import imednet"
assert not any(m.startswith("imednet.models.") for m in sys.modules), (
    "imednet.models.* loaded eagerly on import imednet"
)

sdk = imednet.ImednetSDK(api_key="test-key-1234567890", security_key="test-sec-1234567890")
assert not any(m.startswith("imednet.models.") for m in sys.modules), (
    "imednet.models.* loaded eagerly on ImednetSDK instantiation"
)
eager_endpoints = [
    m for m in sys.modules
    if m.startswith("imednet.endpoints.") and m != "imednet.endpoints.registry"
]
assert not eager_endpoints, f"Concrete endpoints loaded eagerly: {eager_endpoints}"
"""
    res = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr


def test_lazy_barrels_mypy_strict_concrete_types_and_attr_defined(tmp_path: Path) -> None:
    """mypy --strict must infer concrete types on lazy barrels and flag unknown attributes."""
    mypy_api = pytest.importorskip("mypy.api")
    repo_root = Path(__file__).resolve().parents[3]
    config_file = repo_root / "pyproject.toml"

    probe = tmp_path / "strict_barrel_probe.py"
    probe.write_text(
        """from typing import Any
import imednet.cli.utils as cli_utils
import imednet.integrations as integrations
import imednet.spi as spi
import imednet.spi.cli as spi_cli
import imednet.testing as testing
import imednet.utils as utils
import imednet.validation as validation

reveal_type(utils.BatchCoordinator)
reveal_type(utils.parse_iso_datetime)
reveal_type(testing.fake_record)
reveal_type(testing.canonical_type)
reveal_type(validation.SchemaValidator)
reveal_type(spi_cli.is_interactive_terminal)
reveal_type(cli_utils.InteractiveDataGrid)
reveal_type(integrations.export)
reveal_type(spi.export)

_bad_utils: Any = utils.nonexistent_helper_xyz
_bad_spi_cli: Any = spi_cli.nonexistent_helper_xyz
_bad_cli_utils: Any = cli_utils.nonexistent_helper_xyz
_bad_testing: Any = testing.nonexistent_helper_xyz
_bad_validation: Any = validation.nonexistent_helper_xyz
""",
        encoding="utf-8",
    )

    stdout, stderr, _ = mypy_api.run(["--config-file", str(config_file), "--strict", str(probe)])
    assert stderr == ""
    revealed_lines = [line for line in stdout.splitlines() if "Revealed type is" in line]
    assert len(revealed_lines) == 9, f"Unexpected reveal_type output:\n{stdout}"
    for line in revealed_lines:
        assert 'Revealed type is "Any"' not in line, f"Lazy export degraded to Any: {line}"
        assert 'Revealed type is "types.ModuleType"' not in line, (
            f"Callable export degraded to ModuleType: {line}"
        )

    for line_no in (20, 21, 22, 23, 24):
        expected_err = (
            f':{line_no}: error: Module has no attribute "nonexistent_helper_xyz"  [attr-defined]'
        )
        assert expected_err in stdout, f"Expected {expected_err!r}, got:\n{stdout}"
