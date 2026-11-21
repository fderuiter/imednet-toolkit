"""Airflow operators for interacting with iMedNet."""

from __future__ import annotations

import sys

from .._airflow_compat import AirflowException
from ..hooks import ImednetHook
from .export import ImednetExportOperator

if "airflow.providers.imednet.operators" in sys.modules:
    sys.modules["airflow.providers.imednet.operators"] = sys.modules[__name__]
    _alias_parent = sys.modules.get("airflow.providers.imednet")
    if _alias_parent is not None:
        _alias_parent.operators = sys.modules[__name__]  # type: ignore[attr-defined]

__all__ = [
    "AirflowException",
    "ImednetExportOperator",
    "ImednetHook",
]
