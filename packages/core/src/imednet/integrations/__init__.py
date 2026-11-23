"""Integration helpers for exporting study data."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import export as _export_module
from .dispatcher import register_tabular_target
from .export import (
    export_to_csv,
    export_to_duckdb,
    export_to_duckdb_by_form,
    export_to_excel,
    export_to_json,
    export_to_long_sql,
    export_to_parquet,
    export_to_sql,
    export_to_sql_by_form,
)
from .parquet import export_to_hive_parquet, hive_parquet_query
from .parquet_engine import PartitionedStorageEngine, PyArrowDatasetPartitionedStorageEngine
from .sink_base import ExportSink, SinkConfig

if TYPE_CHECKING:
    from .dispatcher import export as export
else:
    export = _export_module


def _bind_export_attr(attr_name: str) -> Callable[..., Any]:
    """Return a tabular target delegating dynamically to ``imednet.integrations.export``."""

    def _target(sdk: Any, study_key: str, **kwargs: Any) -> Any:
        target_fn: Callable[..., Any] = getattr(_export_module, attr_name)
        return target_fn(sdk, study_key, **kwargs)

    return _target


# Register standard tabular targets
register_tabular_target("csv", _bind_export_attr("export_to_csv"))
register_tabular_target("duckdb", _bind_export_attr("export_to_duckdb"))
register_tabular_target("duckdb_by_form", _bind_export_attr("export_to_duckdb_by_form"))
register_tabular_target("excel", _bind_export_attr("export_to_excel"))
register_tabular_target("json", _bind_export_attr("export_to_json"))
register_tabular_target("long_sql", _bind_export_attr("export_to_long_sql"))
register_tabular_target("parquet", _bind_export_attr("export_to_parquet"))
register_tabular_target("sql", _bind_export_attr("export_to_sql"))
register_tabular_target("sql_by_form", _bind_export_attr("export_to_sql_by_form"))
register_tabular_target("hive_parquet", export_to_hive_parquet)

__all__ = [  # noqa: RUF022
    # Unified entry point
    "export",
    # Tabular path
    "export_to_csv",
    "export_to_duckdb",
    "export_to_duckdb_by_form",
    "export_to_excel",
    "export_to_hive_parquet",
    "export_to_json",
    "export_to_long_sql",
    "export_to_parquet",
    "export_to_sql_by_form",
    "export_to_sql",
    "hive_parquet_query",
    "PartitionedStorageEngine",
    "PyArrowDatasetPartitionedStorageEngine",
    # Shared sink base
    "SinkConfig",
    "ExportSink",
]
