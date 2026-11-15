"""Service Provider Interface (SPI) for Imednet Extensions.

This module provides a stable public interface for advanced platform developers,
plugins, and providers, decoupling them from internal core implementation details.
"""

from importlib import import_module as _import_module

from imednet.integrations import export, sink_base

# Keep the legacy binding while providing an unambiguous procedural namespace.
export_helpers = _import_module("imednet.integrations.export")

from . import cli, constants, endpoints, errors, facade, models, utils, validation

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
    "utils",
    "validation",
]
