# pylint: disable=duplicate-code
"""Airflow-facing export helpers."""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, ParamSpec, TypeVar, cast

from imednet.spi import export_helpers as _base_export

if TYPE_CHECKING:
    from imednet import ImednetClient, ImednetSDK

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _delegate_export(target_name: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Forward calls verbatim to the corresponding ``imednet.spi.export_helpers`` function."""

    def decorator(func: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            target = cast(Callable[..., _R], getattr(_base_export, target_name))
            return target(*args, **kwargs)

        return wrapper

    return decorator


@_delegate_export("export_to_csv")
def export_to_csv(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str],
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    manifest_path: str | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Wrap :func:`imednet.spi.export.export_to_csv` for Airflow compatibility."""


@_delegate_export("export_to_duckdb")
def export_to_duckdb(
    sdk: ImednetSDK,
    study_key: str,
    db_path: str | os.PathLike[str],
    table_name: str,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Wrap :func:`imednet.spi.export.export_to_duckdb` for Airflow compatibility."""


@_delegate_export("export_to_duckdb_by_form")
def export_to_duckdb_by_form(
    sdk: ImednetSDK,
    study_key: str,
    db_path: str | os.PathLike[str],
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Wrap :func:`imednet.spi.export.export_to_duckdb_by_form` for Airflow compatibility."""


@_delegate_export("export_to_parquet")
def export_to_parquet(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str],
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Wrap :func:`imednet.spi.export.export_to_parquet` for Airflow compatibility."""


@_delegate_export("export_to_hive_parquet")
def export_to_hive_parquet(
    sdk: ImednetSDK,
    study_key: str,
    base_dir: str,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    chunk_size: int = 5_000,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    max_buffered_records: int | None = None,
    **kwargs: Any,
) -> None:
    """Wrap :func:`imednet.spi.export.export_to_hive_parquet` for Airflow compatibility."""


@_delegate_export("export_to_excel")
def export_to_excel(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str],
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Wrap :func:`imednet.spi.export.export_to_excel` for Airflow compatibility."""


@_delegate_export("export_to_json")
def export_to_json(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str],
    *,
    use_labels_as_columns: bool = False,
    hierarchical: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Wrap :func:`imednet.spi.export.export_to_json` for Airflow compatibility."""


@_delegate_export("export_to_sql")
def export_to_sql(
    sdk: ImednetSDK,
    study_key: str,
    table: str,
    conn_str: str,
    if_exists: Literal["fail", "replace", "append", "delete_rows"] | None = None,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    manifest_path: str | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Wrap :func:`imednet.spi.export.export_to_sql` for Airflow compatibility."""


@_delegate_export("export_to_sql_by_form")
def export_to_sql_by_form(
    sdk: ImednetSDK,
    study_key: str,
    conn_str: str,
    if_exists: Literal["fail", "replace", "append", "delete_rows"] | None = None,
    *,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Wrap :func:`imednet.spi.export.export_to_sql_by_form` for Airflow compatibility."""


@_delegate_export("export_to_long_sql")
def export_to_long_sql(
    sdk: ImednetClient,
    study_key: str,
    table_name: str,
    conn_str: str,
    *,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    chunk_size: int = 1000,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Wrap :func:`imednet.spi.export.export_to_long_sql` for Airflow compatibility."""


__all__ = [
    "export_to_csv",
    "export_to_duckdb",
    "export_to_duckdb_by_form",
    "export_to_excel",
    "export_to_hive_parquet",
    "export_to_json",
    "export_to_long_sql",
    "export_to_parquet",
    "export_to_sql",
    "export_to_sql_by_form",
]
