# pylint: disable=duplicate-code
"""Core endpoint abstractions and protocols."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import (
        AsyncGetEndpoint,
        AsyncListEndpoint,
        AsyncListGetEndpoint,
        GenericEndpoint,
        GenericGetEndpoint,
        GenericListEndpoint,
        GenericListGetEndpoint,
        SyncGetEndpoint,
        SyncListEndpoint,
        SyncListGetEndpoint,
    )
    from .edc_mixin import (
        EdcAsyncGetEndpoint,
        EdcAsyncListEndpoint,
        EdcAsyncListGetEndpoint,
        EdcGenericGetEndpoint,
        EdcGenericListEndpoint,
        EdcGenericListGetEndpoint,
        EdcSyncGetEndpoint,
        EdcSyncListEndpoint,
        EdcSyncListGetEndpoint,
    )
    from .protocols import SupportsCreate, SupportsGet, SupportsList

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "AsyncGetEndpoint": ("imednet.core.endpoint.base", "AsyncGetEndpoint"),
    "AsyncListEndpoint": ("imednet.core.endpoint.base", "AsyncListEndpoint"),
    "AsyncListGetEndpoint": ("imednet.core.endpoint.base", "AsyncListGetEndpoint"),
    "EdcAsyncGetEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcAsyncGetEndpoint"),
    "EdcAsyncListEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcAsyncListEndpoint"),
    "EdcAsyncListGetEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcAsyncListGetEndpoint"),
    "EdcGenericGetEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcGenericGetEndpoint"),
    "EdcGenericListEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcGenericListEndpoint"),
    "EdcGenericListGetEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcGenericListGetEndpoint"),
    "EdcSyncGetEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcSyncGetEndpoint"),
    "EdcSyncListEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcSyncListEndpoint"),
    "EdcSyncListGetEndpoint": ("imednet.core.endpoint.edc_mixin", "EdcSyncListGetEndpoint"),
    "GenericEndpoint": ("imednet.core.endpoint.base", "GenericEndpoint"),
    "GenericGetEndpoint": ("imednet.core.endpoint.base", "GenericGetEndpoint"),
    "GenericListEndpoint": ("imednet.core.endpoint.base", "GenericListEndpoint"),
    "GenericListGetEndpoint": ("imednet.core.endpoint.base", "GenericListGetEndpoint"),
    "SupportsCreate": ("imednet.core.endpoint.protocols", "SupportsCreate"),
    "SupportsGet": ("imednet.core.endpoint.protocols", "SupportsGet"),
    "SupportsList": ("imednet.core.endpoint.protocols", "SupportsList"),
    "SyncGetEndpoint": ("imednet.core.endpoint.base", "SyncGetEndpoint"),
    "SyncListEndpoint": ("imednet.core.endpoint.base", "SyncListEndpoint"),
    "SyncListGetEndpoint": ("imednet.core.endpoint.base", "SyncListGetEndpoint"),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily resolve endpoint base classes and protocols."""
        try:
            module_path, obj_name = _LAZY_ATTRS[name]
        except KeyError:
            raise AttributeError(name) from None
        mod = import_module(module_path)  # nosem
        obj = getattr(mod, obj_name)
        globals()[name] = obj
        return obj


def __dir__() -> list[str]:
    """Return module attributes including lazy exports."""
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "AsyncGetEndpoint",
    "AsyncListEndpoint",
    "AsyncListGetEndpoint",
    "EdcAsyncGetEndpoint",
    "EdcAsyncListEndpoint",
    "EdcAsyncListGetEndpoint",
    "EdcGenericGetEndpoint",
    "EdcGenericListEndpoint",
    "EdcGenericListGetEndpoint",
    "EdcSyncGetEndpoint",
    "EdcSyncListEndpoint",
    "EdcSyncListGetEndpoint",
    "GenericEndpoint",
    "GenericGetEndpoint",
    "GenericListEndpoint",
    "GenericListGetEndpoint",
    "SupportsCreate",
    "SupportsGet",
    "SupportsList",
    "SyncGetEndpoint",
    "SyncListEndpoint",
    "SyncListGetEndpoint",
]
