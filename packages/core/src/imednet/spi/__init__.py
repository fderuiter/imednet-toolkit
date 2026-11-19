"""Service Provider Interface (SPI) for Imednet Extensions.

This module provides a stable public interface for advanced platform developers,
plugins, and providers, decoupling them from internal core implementation details.
"""

from importlib import import_module as _import_module

from . import cli, constants, endpoints, errors, facade, models, sink_base, sinks, utils, validation

export = _import_module("imednet.integrations.export")
export_helpers = export

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
