"""Tests asserting the public interface shape.

Validates importability, ``__all__`` coverage, and type annotations across
the stable public namespace.
"""

from __future__ import annotations

import importlib
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

    if hasattr(method, "func"):
        return get_type_hints(method.func)  # type: ignore[arg-type]
    return get_type_hints(method)  # type: ignore[arg-type]


def _get_type_hints_safe(method: object) -> dict[str, object]:
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
