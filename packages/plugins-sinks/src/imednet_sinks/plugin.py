"""Entry point for the iMednet sinks plugin.

This module provides the :class:`SinksNamespace` which aggregates all available
data export sinks (MongoDB, Neo4j, Snowflake) under a single namespace on the
SDK instance.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from imednet.spi.sink_base import is_async_sdk

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade


class SinksNamespace:
    """Namespace for accessing data export sinks.

    This class is typically instantiated by the SDK and exposed via
    ``sdk.sinks``. Synchronous ``ImednetSDK`` instances receive bound
    ``export_to_*`` helpers while ``AsyncImednetSDK`` instances raise
    ``TypeError`` immediately with actionable guidance when a synchronous sink
    helper is invoked.
    """

    def __init__(self, sdk: ImednetFacade | AsyncImednetFacade) -> None:
        """Initialize the sinks namespace.

        Args:
            sdk: An instance of the iMednet SDK (synchronous or asynchronous).
        """
        self._sdk = sdk
        self._is_async = is_async_sdk(sdk)

    def _bind_export_helper(self, fn: Callable[..., Any], helper_name: str) -> Callable[..., Any]:
        """Bind ``self._sdk`` for sync SDKs or reject calls from ``AsyncImednetSDK``."""
        if self._is_async:

            @functools.wraps(fn)
            def _reject_async(*_args: Any, **_kwargs: Any) -> Any:
                raise TypeError(
                    f"Sink helper '{helper_name}' is synchronous-only and cannot be called from "
                    "AsyncImednetSDK. Instantiate synchronous ImednetSDK instead."
                )

            return _reject_async

        @functools.wraps(fn)
        def _bound(*args: Any, **kwargs: Any) -> Any:
            if "sdk" in kwargs:
                if is_async_sdk(kwargs["sdk"]):
                    raise TypeError(
                        f"Sink helper '{helper_name}' is synchronous-only and cannot be called "
                        "with an AsyncImednetSDK client. Instantiate synchronous ImednetSDK instead."
                    )
                return fn(*args, **kwargs)
            if args and not isinstance(args[0], str):
                if is_async_sdk(args[0]):
                    raise TypeError(
                        f"Sink helper '{helper_name}' is synchronous-only and cannot be called "
                        "with an AsyncImednetSDK client. Instantiate synchronous ImednetSDK instead."
                    )
                return fn(*args, **kwargs)
            return fn(self._sdk, *args, **kwargs)

        return _bound

    @property
    def export_to_mongodb(self) -> Any:
        """Helper function to export records to MongoDB."""
        from .document import export_to_mongodb

        return self._bind_export_helper(export_to_mongodb, "export_to_mongodb")

    @property
    def MongoDbSinkConfig(self) -> Any:
        """Configuration class for MongoDB sinks."""
        from .document import MongoDbSinkConfig

        return MongoDbSinkConfig

    @property
    def MongoDbExportSink(self) -> Any:
        """Sink class for exporting records to MongoDB."""
        from .document import MongoDbExportSink

        return MongoDbExportSink

    @property
    def export_to_neo4j(self) -> Any:
        """Helper function to export records to Neo4j."""
        from .graph import export_to_neo4j

        return self._bind_export_helper(export_to_neo4j, "export_to_neo4j")

    @property
    def Neo4jSinkConfig(self) -> Any:
        """Configuration class for Neo4j sinks."""
        from .graph import Neo4jSinkConfig

        return Neo4jSinkConfig

    @property
    def Neo4jExportSink(self) -> Any:
        """Sink class for exporting records to Neo4j."""
        from .graph import Neo4jExportSink

        return Neo4jExportSink

    @property
    def export_to_snowflake(self) -> Any:
        """Helper function to export records to Snowflake."""
        from .warehouse import export_to_snowflake

        return self._bind_export_helper(export_to_snowflake, "export_to_snowflake")

    @property
    def SnowflakeSinkConfig(self) -> Any:
        """Configuration class for Snowflake sinks."""
        from .warehouse import SnowflakeSinkConfig

        return SnowflakeSinkConfig

    @property
    def SnowflakeExportSink(self) -> Any:
        """Sink class for exporting records to Snowflake."""
        from .warehouse import SnowflakeExportSink

        return SnowflakeExportSink


def create_sinks(sdk: ImednetFacade | AsyncImednetFacade) -> SinksNamespace:
    """Factory function to create a new SinksNamespace instance."""
    return SinksNamespace(sdk)
