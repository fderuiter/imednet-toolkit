"""Data dictionary loaders package."""

from __future__ import annotations

from .api import load_from_api
from .csv_export import load_from_csv, load_from_csv_files

__all__ = ["load_from_api", "load_from_csv", "load_from_csv_files"]
