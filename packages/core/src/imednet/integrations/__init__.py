"""Integration helpers for exporting study data."""

import sys as _sys
from typing import TYPE_CHECKING

from .cdisc import (
    DatasetJsonExportSink,
    OdmExportSink,
    export_to_dataset_json,
    export_to_odm,
)
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
    export = _sys.modules["imednet.integrations.export"]

# Register standard tabular targets
register_tabular_target("csv", export_to_csv)
register_tabular_target("dataset_json", export_to_dataset_json)
register_tabular_target("duckdb", export_to_duckdb)
register_tabular_target("duckdb_by_form", export_to_duckdb_by_form)
register_tabular_target("excel", export_to_excel)
register_tabular_target("json", export_to_json)
register_tabular_target("long_sql", export_to_long_sql)
register_tabular_target("odm", export_to_odm)
register_tabular_target("parquet", export_to_parquet)
register_tabular_target("sql", export_to_sql)
register_tabular_target("sql_by_form", export_to_sql_by_form)
register_tabular_target("hive_parquet", export_to_hive_parquet)

__all__ = [  # noqa: RUF022
    # Unified entry point
    "export",
    # Tabular path
    "export_to_csv",
    "export_to_dataset_json",
    "export_to_duckdb",
    "export_to_duckdb_by_form",
    "export_to_excel",
    "export_to_hive_parquet",
    "export_to_json",
    "export_to_long_sql",
    "export_to_odm",
    "export_to_parquet",
    "export_to_sql_by_form",
    "export_to_sql",
    "hive_parquet_query",
    "DatasetJsonExportSink",
    "OdmExportSink",
    "PartitionedStorageEngine",
    "PyArrowDatasetPartitionedStorageEngine",
    # Shared sink base
    "SinkConfig",
    "ExportSink",
]
