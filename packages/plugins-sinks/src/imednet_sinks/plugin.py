"""Entry point for the iMednet sinks plugin.

This module provides the :class:`SinksNamespace` which aggregates all available
data export sinks (MongoDB, Neo4j, Snowflake) under a single namespace on the
SDK instance.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any, cast, overload

from typing_extensions import deprecated

if TYPE_CHECKING:
    from imednet.sdk import ImednetSDK
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

    from .document import MongoDbExportSink, MongoDbSinkConfig
    from .graph import Neo4jExportSink, Neo4jSinkConfig
    from .warehouse import SnowflakeExportSink, SnowflakeSinkConfig


class SinksNamespace:
    """Namespace for accessing data export sinks.

    This class is typically instantiated by the SDK and exposed via
    ``sdk.sinks``. It provides bound export methods and sink configuration /
    class definitions.
    """

    supports_async = True

    def __init__(self, sdk: ImednetFacade | AsyncImednetFacade) -> None:
        """Initialize the sinks namespace.

        Args:
            sdk: An instance of the iMednet SDK (synchronous or asynchronous).
        """
        self._sdk = sdk

    def _ensure_sync_sdk(self, helper_name: str, target: object) -> ImednetSDK:
        """Verify *target* is a synchronous SDK facade before running a sink export."""
        if hasattr(target, "async_get_records") and not callable(
            getattr(target, "get_records", None)
        ):
            raise TypeError(
                f"SinksNamespace.{helper_name}() requires a synchronous ImednetSDK "
                f"(ImednetFacade); AsyncImednetSDK is not supported by synchronous "
                f"sink export helpers."
            )
        return cast("ImednetSDK", target)

    def _resolve_export_call(
        self,
        helper_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> tuple[ImednetSDK, tuple[Any, ...], dict[str, Any]]:
        """Resolve bound ``(study_key, ...)`` or deprecated ``(sdk, study_key, ...)`` calls."""
        resolved_kwargs = dict(kwargs)
        depr_msg = (
            f"Passing 'sdk' to sdk.sinks.{helper_name}() is deprecated and will be "
            f"removed in a future release; call sdk.sinks.{helper_name}(study_key, ...) "
            f"directly on the bound SDK instance."
        )

        if "sdk" in resolved_kwargs:
            explicit_sdk = resolved_kwargs.pop("sdk")
            warnings.warn(depr_msg, DeprecationWarning, stacklevel=3)
            sync_sdk = self._ensure_sync_sdk(helper_name, explicit_sdk)
            rest_args = args
        elif args and not isinstance(args[0], str):
            first = args[0]
            looks_like_sdk = callable(getattr(first, "get_records", None)) or hasattr(
                first, "async_get_records"
            )
            if len(args) == 1 and "study_key" not in resolved_kwargs and not looks_like_sdk:
                self._ensure_sync_sdk(helper_name, self._sdk)
                raise TypeError(
                    f"SinksNamespace.{helper_name}() 'study_key' must be a str, "
                    f"got {type(first).__name__}."
                )
            warnings.warn(depr_msg, DeprecationWarning, stacklevel=3)
            sync_sdk = self._ensure_sync_sdk(helper_name, first)
            rest_args = args[1:]
        else:
            sync_sdk = self._ensure_sync_sdk(helper_name, self._sdk)
            rest_args = args

        if not rest_args and "study_key" not in resolved_kwargs:
            raise TypeError(
                f"SinksNamespace.{helper_name}() missing 1 required positional argument: "
                f"'study_key'"
            )
        if rest_args and "study_key" in resolved_kwargs:
            raise TypeError(
                f"SinksNamespace.{helper_name}() got multiple values for argument 'study_key'"
            )
        if rest_args and not isinstance(rest_args[0], str):
            raise TypeError(
                f"SinksNamespace.{helper_name}() 'study_key' must be a str, "
                f"got {type(rest_args[0]).__name__}."
            )
        if "study_key" in resolved_kwargs and not isinstance(resolved_kwargs["study_key"], str):
            raise TypeError(
                f"SinksNamespace.{helper_name}() 'study_key' must be a str, "
                f"got {type(resolved_kwargs['study_key']).__name__}."
            )
        return sync_sdk, rest_args, resolved_kwargs

    @overload
    def export_to_mongodb(
        self,
        study_key: str,
        uri: str = "",
        database: str = "",
        collection: str = "",
        *,
        config: MongoDbSinkConfig | None = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_mongodb() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    def export_to_mongodb(
        self,
        sdk: ImednetFacade,
        study_key: str,
        uri: str = "",
        database: str = "",
        collection: str = "",
        *,
        config: MongoDbSinkConfig | None = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    def export_to_mongodb(self, *args: Any, **kwargs: Any) -> int:
        """Export study records to MongoDB using the bound SDK instance."""
        from .document import export_to_mongodb

        sync_sdk, rest_args, resolved_kwargs = self._resolve_export_call(
            "export_to_mongodb", args, kwargs
        )
        return export_to_mongodb(sync_sdk, *rest_args, **resolved_kwargs)

    @property
    def MongoDbSinkConfig(self) -> type[MongoDbSinkConfig]:
        """Configuration class for MongoDB sinks."""
        from .document import MongoDbSinkConfig

        return MongoDbSinkConfig

    @property
    def MongoDbExportSink(self) -> type[MongoDbExportSink]:
        """Sink class for exporting records to MongoDB."""
        from .document import MongoDbExportSink

        return MongoDbExportSink

    @overload
    def export_to_neo4j(
        self,
        study_key: str,
        uri: str = "",
        auth: tuple[str, str] = ("", ""),
        *,
        config: Neo4jSinkConfig | None = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_neo4j() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    def export_to_neo4j(
        self,
        sdk: ImednetFacade,
        study_key: str,
        uri: str = "",
        auth: tuple[str, str] = ("", ""),
        *,
        config: Neo4jSinkConfig | None = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    def export_to_neo4j(self, *args: Any, **kwargs: Any) -> int:
        """Export study records to Neo4j using the bound SDK instance."""
        from .graph import export_to_neo4j

        sync_sdk, rest_args, resolved_kwargs = self._resolve_export_call(
            "export_to_neo4j", args, kwargs
        )
        return export_to_neo4j(sync_sdk, *rest_args, **resolved_kwargs)

    @property
    def Neo4jSinkConfig(self) -> type[Neo4jSinkConfig]:
        """Configuration class for Neo4j sinks."""
        from .graph import Neo4jSinkConfig

        return Neo4jSinkConfig

    @property
    def Neo4jExportSink(self) -> type[Neo4jExportSink]:
        """Sink class for exporting records to Neo4j."""
        from .graph import Neo4jExportSink

        return Neo4jExportSink

    @overload
    def export_to_snowflake(
        self,
        study_key: str,
        *,
        config: SnowflakeSinkConfig,
        mask_phi: bool | None = None,
    ) -> int: ...

    @overload
    @deprecated(
        "Passing 'sdk' to sdk.sinks.export_to_snowflake() is deprecated; "
        "pass 'study_key' as the first argument."
    )
    def export_to_snowflake(
        self,
        sdk: ImednetFacade,
        study_key: str,
        *,
        config: SnowflakeSinkConfig,
        mask_phi: bool | None = None,
    ) -> int: ...

    def export_to_snowflake(self, *args: Any, **kwargs: Any) -> int:
        """Export study records to Snowflake using the bound SDK instance."""
        from .warehouse import export_to_snowflake

        sync_sdk, rest_args, resolved_kwargs = self._resolve_export_call(
            "export_to_snowflake", args, kwargs
        )
        return export_to_snowflake(sync_sdk, *rest_args, **resolved_kwargs)

    @property
    def SnowflakeSinkConfig(self) -> type[SnowflakeSinkConfig]:
        """Configuration class for Snowflake sinks."""
        from .warehouse import SnowflakeSinkConfig

        return SnowflakeSinkConfig

    @property
    def SnowflakeExportSink(self) -> type[SnowflakeExportSink]:
        """Sink class for exporting records to Snowflake."""
        from .warehouse import SnowflakeExportSink

        return SnowflakeExportSink


def create_sinks(sdk: ImednetFacade | AsyncImednetFacade) -> SinksNamespace:
    """Factory function to create a new SinksNamespace instance."""
    return SinksNamespace(sdk)
