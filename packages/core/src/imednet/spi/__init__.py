"""Service Provider Interface (SPI) for Imednet Extensions.

This module provides a stable public interface for advanced platform developers,
plugins, and providers, decoupling them from internal core implementation details.
"""

from importlib import import_module as _import_module

from imednet.integrations import export

# Keep the legacy binding while providing an unambiguous procedural namespace.
export_helpers = _import_module("imednet.integrations.export")

from . import (
    cli,
    constants,
    datadict,
    endpoints,
    errors,
    facade,
    jobs,
    models,
    sink_base,
    sinks,
    utils,
    validation,
)

__all__ = [
    "cli",
    "constants",
    "datadict",
    "endpoints",
    "errors",
    "export",
    "export_helpers",
    "facade",
    "jobs",
    "models",
    "sink_base",
    "sinks",
    "utils",
    "validation",
]
