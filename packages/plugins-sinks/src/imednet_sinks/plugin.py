"""Entry point for the iMednet sinks plugin.

This module provides the :class:`SinksNamespace` which aggregates all available
data export sinks (MongoDB, Neo4j, Snowflake) under a single namespace on the
SDK instance.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any, cast, overload

from typing_extensions import deprecated

from imednet import AsyncImednetSDK, ImednetSDK

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

    from .document import MongoDbExportSink, MongoDbSinkConfig
    from .graph import Neo4jExportSink, Neo4jSinkConfig
    from .warehouse import SnowflakeExportSink, SnowflakeSinkConfig


def _is_async_sdk(sdk: Any) -> bool:
    """Return True when ``sdk`` is an asynchronous SDK or facade instance."""
    if isinstance(sdk, AsyncImednetSDK):
        return True
    return hasattr(sdk, "async_get_studies") and not callable(getattr(sdk, "get_studies", None))


def _ensure_sync_sdk(sdk: Any, method_name: str) -> None:
    """Raise TypeError when a synchronous sink export helper is called with AsyncImednetSDK."""
    if _is_async_sdk(sdk):
        raise TypeError(
            f"SinksNamespace.{method_name}() is synchronous-only and cannot be called on "
            "AsyncImednetSDK. Instantiate synchronous ImednetSDK(...) to run sink exports."
        )


class SinksNamespace:
    """Namespace for accessing data export sinks.

    This class is typically instantiated by the SDK and exposed via
    ``sdk.sinks``. It provides bound export methods and properties to access
    sink configuration and export sink classes.
    """

    def __init__(self, sdk: ImednetFacade | AsyncImednetFacade) -> None:
        """Initialize the sinks namespace.

        Args:
            sdk: An instance of the iMednet SDK (synchronous or asynchronous).
        """
        self._sdk = sdk

    def _resolve_export_call(
        self,
        method_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> tuple[ImednetSDK, str, tuple[Any, ...], dict[str, Any]]:
        """Resolve bound vs deprecated ``(sdk, study_key, ...)`` call arguments."""
        _ensure_sync_sdk(self._sdk, method_name)
        resolved_kwargs = dict(kwargs)

        if not args:
            if "sdk" in resolved_kwargs:
                explicit_sdk = resolved_kwargs.pop("sdk")
                _ensure_sync_sdk(explicit_sdk, method_name)
                warnings.warn(
                    f"Passing 'sdk' to sdk.sinks.{method_name}() is deprecated; "
                    "pass 'study_key' as the first argument.",
                    DeprecationWarning,
                    stacklevel=3,
                )
                target_sdk = explicit_sdk
            else:
                target_sdk = self._sdk

            if "study_key" not in resolved_kwargs:
                raise TypeError(
                    f"SinksNamespace.{method_name}() missing 1 required positional argument: "
                    "'study_key'"
                )
            study_key = resolved_kwargs.pop("study_key")
            if not isinstance(study_key, str):
                raise TypeError(
                    f"SinksNamespace.{method_name}() expected 'study_key' to be a str, "
                    f"got {type(study_key).__name__}."
                )
            return cast(ImednetSDK, target_sdk), study_key, (), resolved_kwargs

        first = args[0]
        if isinstance(first, str):
            target_sdk = self._sdk
            if "sdk" in resolved_kwargs:
                explicit_sdk = resolved_kwargs.pop("sdk")
                _ensure_sync_sdk(explicit_sdk, method_name)
                warnings.warn(
                    f"Passing 'sdk' to sdk.sinks.{method_name}() is deprecated; "
                    "pass 'study_key' as the first argument.",
                    DeprecationWarning,
                    stacklevel=3,
                )
                target_sdk = explicit_sdk
            return cast(ImednetSDK, target_sdk), first, args[1:], resolved_kwargs

        _ensure_sync_sdk(first, method_name)
        if len(args) >= 2:
            study_key = args[1]
            remaining_args = args[2:]
        elif "study_key" in resolved_kwargs:
            study_key = resolved_kwargs.pop("study_key")
            remaining_args = ()
        else:
            raise TypeError(
                f"SinksNamespace.{method_name}() expected 'study_key' (str) as the first "
                f"argument, got {type(first).__name__}."
            )

        if not isinstance(study_key, str):
            raise TypeError(
                f"SinksNamespace.{method_name}() expected 'study_key' to be a str, "
                f"got {type(study_key).__name__}."
            )

        warnings.warn(
            f"Passing 'sdk' to sdk.sinks.{method_name}() is deprecated; "
            "pass 'study_key' as the first argument.",
            DeprecationWarning,
            stacklevel=3,
        )
        return cast(ImednetSDK, first), study_key, remaining_args, resolved_kwargs

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
        """Export study records to MongoDB using the bound SDK client."""
        target_sdk, study_key, rest_args, rest_kwargs = self._resolve_export_call(
            "export_to_mongodb", args, kwargs
        )
        from .document import export_to_mongodb

        return export_to_mongodb(target_sdk, study_key, *rest_args, **rest_kwargs)

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
        """Export study records to Neo4j using the bound SDK client."""
        target_sdk, study_key, rest_args, rest_kwargs = self._resolve_export_call(
            "export_to_neo4j", args, kwargs
        )
        from .graph import export_to_neo4j

        return export_to_neo4j(target_sdk, study_key, *rest_args, **rest_kwargs)

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
        """Export study records to Snowflake using the bound SDK client."""
        target_sdk, study_key, rest_args, rest_kwargs = self._resolve_export_call(
            "export_to_snowflake", args, kwargs
        )
        from .warehouse import export_to_snowflake

        return export_to_snowflake(target_sdk, study_key, *rest_args, **rest_kwargs)

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
