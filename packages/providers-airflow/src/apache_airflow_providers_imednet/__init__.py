"""Airflow provider for iMednet.

This package provides hooks, operators, and sensors for integrating iMednet
EDC data workflows into Apache Airflow pipelines.
"""

from __future__ import annotations

from importlib import import_module

from . import export
from .get_provider_info import get_provider_info

try:  # pragma: no cover - optional Airflow dependencies may be missing
    from .hooks import ImednetHook
except (ImportError, ModuleNotFoundError):  # pragma: no cover - hook requires Airflow extras
    ImednetHook = None  # type: ignore

try:  # pragma: no cover - optional Airflow dependencies may be missing
    from .operators import ImednetExportOperator
except (ImportError, ModuleNotFoundError):  # pragma: no cover - operator requires Airflow extras
    ImednetExportOperator = None  # type: ignore

try:  # pragma: no cover - optional Airflow dependencies may be missing
    sensors = import_module("apache_airflow_providers_imednet.sensors")
    ImednetJobSensor = sensors.ImednetJobSensor
except (ImportError, ModuleNotFoundError):  # pragma: no cover - sensor requires Airflow extras
    ImednetJobSensor = None
    sensors = None  # type: ignore

try:  # pragma: no cover - optional Airflow dependencies may be missing
    triggers = import_module("apache_airflow_providers_imednet.triggers")
    ImednetJobTrigger = triggers.ImednetJobTrigger
except (ImportError, ModuleNotFoundError):  # pragma: no cover - trigger requires Airflow extras
    ImednetJobTrigger = None
    triggers = None  # type: ignore

__all__ = [
    "ImednetExportOperator",
    "ImednetHook",
    "ImednetJobSensor",
    "ImednetJobTrigger",
    "export",
    "get_provider_info",
]
