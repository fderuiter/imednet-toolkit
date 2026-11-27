# pylint: disable=duplicate-code
"""Unified Sinks plugin for iMednet."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .document import (
        AsyncMongoDbExportSink,
        MongoDbExportSink,
        MongoDbSinkConfig,
        async_export_to_mongodb,
        export_to_mongodb,
    )
    from .graph import (
        AsyncNeo4jExportSink,
        Neo4jExportSink,
        Neo4jSinkConfig,
        async_export_to_neo4j,
        export_to_neo4j,
    )
    from .plugin import AsyncSinksNamespace, SinksNamespace
    from .warehouse import (
        AsyncSnowflakeExportSink,
        SnowflakeExportSink,
        SnowflakeSinkConfig,
        async_export_to_snowflake,
        export_to_snowflake,
    )

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "AsyncMongoDbExportSink": ("imednet_sinks.document", "AsyncMongoDbExportSink"),
    "AsyncNeo4jExportSink": ("imednet_sinks.graph", "AsyncNeo4jExportSink"),
    "AsyncSinksNamespace": ("imednet_sinks.plugin", "AsyncSinksNamespace"),
    "AsyncSnowflakeExportSink": ("imednet_sinks.warehouse", "AsyncSnowflakeExportSink"),
    "MongoDbExportSink": ("imednet_sinks.document", "MongoDbExportSink"),
    "MongoDbSinkConfig": ("imednet_sinks.document", "MongoDbSinkConfig"),
    "Neo4jExportSink": ("imednet_sinks.graph", "Neo4jExportSink"),
    "Neo4jSinkConfig": ("imednet_sinks.graph", "Neo4jSinkConfig"),
    "SinksNamespace": ("imednet_sinks.plugin", "SinksNamespace"),
    "SnowflakeExportSink": ("imednet_sinks.warehouse", "SnowflakeExportSink"),
    "SnowflakeSinkConfig": ("imednet_sinks.warehouse", "SnowflakeSinkConfig"),
    "async_export_to_mongodb": ("imednet_sinks.document", "async_export_to_mongodb"),
    "async_export_to_neo4j": ("imednet_sinks.graph", "async_export_to_neo4j"),
    "async_export_to_snowflake": ("imednet_sinks.warehouse", "async_export_to_snowflake"),
    "export_to_mongodb": ("imednet_sinks.document", "export_to_mongodb"),
    "export_to_neo4j": ("imednet_sinks.graph", "export_to_neo4j"),
    "export_to_snowflake": ("imednet_sinks.warehouse", "export_to_snowflake"),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily resolve sink backends on attribute access."""
        try:
            module_path, obj_name = _LAZY_ATTRS[name]
        except KeyError:
            raise AttributeError(name) from None
        mod = import_module(module_path)  # nosem
        obj = getattr(mod, obj_name)
        globals()[name] = obj
        return obj


def __dir__() -> list[str]:
    """Return module attributes including lazy exports."""
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "AsyncMongoDbExportSink",
    "AsyncNeo4jExportSink",
    "AsyncSinksNamespace",
    "AsyncSnowflakeExportSink",
    "MongoDbExportSink",
    "MongoDbSinkConfig",
    "Neo4jExportSink",
    "Neo4jSinkConfig",
    "SinksNamespace",
    "SnowflakeExportSink",
    "SnowflakeSinkConfig",
    "async_export_to_mongodb",
    "async_export_to_neo4j",
    "async_export_to_snowflake",
    "export_to_mongodb",
    "export_to_neo4j",
    "export_to_snowflake",
]
