"""Airflow-facing export helpers."""

from __future__ import annotations

from typing import Any

from imednet.spi import export_helpers as _base_export


def export_to_csv(*args: Any, **kwargs: Any) -> None:
    """Wrap :func:`imednet.spi.export.export_to_csv` for Airflow compatibility."""
    return _base_export.export_to_csv(*args, **kwargs)  # type: ignore


def export_to_duckdb(*args: Any, **kwargs: Any) -> None:
    """Wrap :func:`imednet.spi.export.export_to_duckdb` for Airflow compatibility."""
    return _base_export.export_to_duckdb(*args, **kwargs)  # type: ignore


def export_to_parquet(*args: Any, **kwargs: Any) -> None:
    """Wrap :func:`imednet.spi.export.export_to_parquet` for Airflow compatibility."""
    return _base_export.export_to_parquet(*args, **kwargs)  # type: ignore


def export_to_excel(*args: Any, **kwargs: Any) -> None:
    """Wrap :func:`imednet.spi.export.export_to_excel` for Airflow compatibility."""
    return _base_export.export_to_excel(*args, **kwargs)  # type: ignore


def export_to_json(*args: Any, **kwargs: Any) -> None:
    """Wrap :func:`imednet.spi.export.export_to_json` for Airflow compatibility."""
    return _base_export.export_to_json(*args, **kwargs)  # type: ignore


def export_to_sql(*args: Any, **kwargs: Any) -> None:
    """Wrap :func:`imednet.spi.export.export_to_sql` for Airflow compatibility."""
    return _base_export.export_to_sql(*args, **kwargs)  # type: ignore


def export_to_sql_by_form(*args: Any, **kwargs: Any) -> None:
    """Wrap :func:`imednet.spi.export.export_to_sql_by_form` for Airflow compatibility."""
    return _base_export.export_to_sql_by_form(*args, **kwargs)  # type: ignore


__all__ = [
    "export_to_csv",
    "export_to_duckdb",
    "export_to_excel",
    "export_to_json",
    "export_to_parquet",
    "export_to_sql",
    "export_to_sql_by_form",
]
