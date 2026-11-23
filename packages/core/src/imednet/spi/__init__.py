# pylint: disable=duplicate-code
"""Service Provider Interface (SPI) for Imednet Extensions.

This module provides a stable public interface for advanced platform developers,
plugins, and providers, decoupling them from internal core implementation details.
"""

from __future__ import annotations

from importlib import import_module as _import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import imednet.integrations.export as export_helpers
    from imednet.integrations.dispatcher import export as export

    from . import (
        cli,
        constants,
        endpoints,
        errors,
        facade,
        models,
        sink_base,
        sinks,
        utils,
        validation,
    )

_LAZY_SUBMODULES: dict[str, str] = {
    "cli": "imednet.spi.cli",
    "constants": "imednet.spi.constants",
    "endpoints": "imednet.spi.endpoints",
    "errors": "imednet.spi.errors",
    "export_helpers": "imednet.integrations.export",
    "facade": "imednet.spi.facade",
    "models": "imednet.spi.models",
    "sink_base": "imednet.spi.sink_base",
    "sinks": "imednet.spi.sinks",
    "utils": "imednet.spi.utils",
    "validation": "imednet.spi.validation",
}

for _lazy_name in (*_LAZY_SUBMODULES, "export"):
    globals().pop(_lazy_name, None)


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily load SPI submodules and export helpers on demand."""
        if name == "export":
            integrations_mod = _import_module("imednet.integrations")  # nosem
            val = integrations_mod.export
            globals()[name] = val
            return val
        try:
            module_path = _LAZY_SUBMODULES[name]
        except KeyError:
            raise AttributeError(name) from None
        mod = _import_module(module_path)  # nosem
        globals()[name] = mod
        return mod


def __dir__() -> list[str]:
    """Return module attributes including lazy SPI namespaces."""
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "cli",
    "constants",
    "endpoints",
    "errors",
    "export",
    "export_helpers",
    "facade",
    "models",
    "sink_base",
    "sinks",
    "utils",
    "validation",
]
