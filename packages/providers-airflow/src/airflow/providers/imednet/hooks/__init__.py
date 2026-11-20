"""Airflow hooks namespace re-export for ``ImednetHook``."""

from __future__ import annotations

from apache_airflow_providers_imednet.hooks import ImednetHook

__all__ = ["ImednetHook"]
