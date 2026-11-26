"""Entry point for the iMednet sinks plugin.

This module provides :class:`SinksNamespace` and :class:`AsyncSinksNamespace`,
which aggregate all available data export sinks (MongoDB, Neo4j, Snowflake)
under ``sdk.sinks`` on :class:`~imednet.sdk.ImednetSDK` and
:class:`~imednet.sdk.AsyncImednetSDK` respectively.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any, cast, overload

from typing_extensions import deprecated

if TYPE_CHECKING:
    from imednet.sdk import ImednetSDK
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

    from .document import AsyncMongoDbExportSink, MongoDbExportSink, MongoDbSinkConfig
    from .graph import AsyncNeo4jExportSink, Neo4jExportSink, Neo4jSinkConfig
    from .warehouse import AsyncSnowflakeExportSink, SnowflakeExportSink, SnowflakeSinkConfig


def _looks_like_sdk(candidate: object) -> bool:
    """Return True if *candidate* resembles a sync or async SDK facade."""
    return callable(getattr(candidate, "get_records", None)) or hasattr(
        candidate, "async_get_records"
    )


def _validate_study_key_arg(
    owner_name: str,
    helper_name: str,
    rest_args: tuple[Any, ...],
    resolved_kwargs: dict[str, Any],
) -> None:
    """Validate positional or keyword ``study_key`` argument after SDK resolution."""
    if not rest_args and "study_key" not in resolved_kwargs:
        raise TypeError(
            f"{owner_name}.{helper_name}() missing 1 required positional argument: 'study_key'"
        )
    if rest_args and "study_key" in resolved_kwargs:
        raise TypeError(
            f"{owner_name}.{helper_name}() got multiple values for argument 'study_key'"
        )
    if rest_args and not isinstance(rest_args[0], str):
        raise TypeError(
            f"{owner_name}.{helper_name}() 'study_key' must be a str, "
            f"got {type(rest_args[0]).__name__}."
        )
    if "study_key" in resolved_kwargs and not isinstance(resolved_kwargs["study_key"], str):
        raise TypeError(
            f"{owner_name}.{helper_name}() 'study_key' must be a str, "
            f"got {type(resolved_kwargs['study_key']).__name__}."
        )


class _SinkClassAccessorsMixin:
    """Lazy property accessors for sink configuration and sync/async sink classes."""

    @property
    def MongoDbSinkConfig(self) -> type[MongoDbSinkConfig]:
        """Configuration class for MongoDB sinks."""
        from .document import MongoDbSinkConfig

        return MongoDbSinkConfig

    @property
    def MongoDbExportSink(self) -> type[MongoDbExportSink]:
        """Synchronous sink class for exporting records to MongoDB."""
        from .document import MongoDbExportSink

        return MongoDbExportSink

    @property
    def AsyncMongoDbExportSink(self) -> type[AsyncMongoDbExportSink]:
        """Asynchronous sink class for exporting records to MongoDB."""
        from .document import AsyncMongoDbExportSink

        return AsyncMongoDbExportSink

    @property
    def Neo4jSinkConfig(self) -> type[Neo4jSinkConfig]:
        """Configuration class for Neo4j sinks."""
        from .graph import Neo4jSinkConfig

        return Neo4jSinkConfig

    @property
    def Neo4jExportSink(self) -> type[Neo4jExportSink]:
        """Synchronous sink class for exporting records to Neo4j."""
        from .graph import Neo4jExportSink

        return Neo4jExportSink

    @property
    def AsyncNeo4jExportSink(self) -> type[AsyncNeo4jExportSink]:
        """Asynchronous sink class for exporting records to Neo4j."""
        from .graph import AsyncNeo4jExportSink

        return AsyncNeo4jExportSink

    @property
    def SnowflakeSinkConfig(self) -> type[SnowflakeSinkConfig]:
        """Configuration class for Snowflake sinks."""
        from .warehouse import SnowflakeSinkConfig

        return SnowflakeSinkConfig

    @property
    def SnowflakeExportSink(self) -> type[SnowflakeExportSink]:
        """Synchronous sink class for exporting records to Snowflake."""
        from .warehouse import SnowflakeExportSink

        return SnowflakeExportSink

    @property
    def AsyncSnowflakeExportSink(self) -> type[AsyncSnowflakeExportSink]:
        """Asynchronous sink class for exporting records to Snowflake."""
        from .warehouse import AsyncSnowflakeExportSink

        return AsyncSnowflakeExportSink


class SinksNamespace(_SinkClassAccessorsMixin):
    """Synchronous namespace for accessing data export sinks on ``ImednetSDK.sinks``."""

    supports_async = True

    def __init__(self, sdk: ImednetFacade | AsyncImednetFacade) -> None:
        """Initialize the synchronous sinks namespace."""
        self._sdk = sdk

    def _ensure_sync_sdk(self, helper_name: str, target: object) -> ImednetSDK:
        """Verify *target* is a synchronous SDK facade before running a sink export."""
        from imednet.spi.utils import is_async_facade

        if is_async_facade(target):
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
            if len(args) == 1 and "study_key" not in resolved_kwargs and not _looks_like_sdk(first):
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

        _validate_study_key_arg("SinksNamespace", helper_name, rest_args, resolved_kwargs)
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
        """Export study records to MongoDB using the bound synchronous SDK instance."""
        from .document import export_to_mongodb

        sync_sdk, rest_args, resolved_kwargs = self._resolve_export_call(
            "export_to_mongodb", args, kwargs
        )
        return export_to_mongodb(sync_sdk, *rest_args, **resolved_kwargs)

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
        """Export study records to Neo4j using the bound synchronous SDK instance."""
        from .graph import export_to_neo4j

        sync_sdk, rest_args, resolved_kwargs = self._resolve_export_call(
            "export_to_neo4j", args, kwargs
        )
        return export_to_neo4j(sync_sdk, *rest_args, **resolved_kwargs)

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
        """Export study records to Snowflake using the bound synchronous SDK instance."""
        from .warehouse import export_to_snowflake

        sync_sdk, rest_args, resolved_kwargs = self._resolve_export_call(
            "export_to_snowflake", args, kwargs
        )
        return export_to_snowflake(sync_sdk, *rest_args, **resolved_kwargs)


class AsyncSinksNamespace(_SinkClassAccessorsMixin):
    """Asynchronous namespace for accessing native async export sinks on ``AsyncImednetSDK.sinks``."""

    supports_async = True

    def __init__(self, sdk: AsyncImednetFacade) -> None:
        """Initialize the asynchronous sinks namespace."""
        self._sdk = sdk

    def _ensure_async_sdk(self, helper_name: str, target: object) -> AsyncImednetFacade:
        """Verify *target* is an asynchronous SDK facade before running an async sink export."""
        from imednet.spi.utils import is_async_facade

        if not is_async_facade(target):
            raise TypeError(
                f"AsyncSinksNamespace.{helper_name}() requires an AsyncImednetSDK "
                f"(AsyncImednetFacade); synchronous ImednetSDK is not supported by async "
                f"sink export helpers."
            )
        return cast("AsyncImednetFacade", target)

    def _resolve_export_call(
        self,
        helper_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> tuple[AsyncImednetFacade, tuple[Any, ...], dict[str, Any]]:
        """Resolve bound ``(study_key, ...)`` or deprecated ``(sdk, study_key, ...)`` calls."""
        resolved_kwargs = dict(kwargs)
        depr_msg = (
            f"Passing 'sdk' to sdk.sinks.{helper_name}() is deprecated and will be "
            f"removed in a future release; call await sdk.sinks.{helper_name}(study_key, ...) "
            f"directly on the bound AsyncImednetSDK instance."
        )

        if "sdk" in resolved_kwargs:
            explicit_sdk = resolved_kwargs.pop("sdk")
            warnings.warn(depr_msg, DeprecationWarning, stacklevel=3)
            async_sdk = self._ensure_async_sdk(helper_name, explicit_sdk)
            rest_args = args
        elif args and not isinstance(args[0], str):
            first = args[0]
            if len(args) == 1 and "study_key" not in resolved_kwargs and not _looks_like_sdk(first):
                self._ensure_async_sdk(helper_name, self._sdk)
                raise TypeError(
                    f"AsyncSinksNamespace.{helper_name}() 'study_key' must be a str, "
                    f"got {type(first).__name__}."
                )
            warnings.warn(depr_msg, DeprecationWarning, stacklevel=3)
            async_sdk = self._ensure_async_sdk(helper_name, first)
            rest_args = args[1:]
        else:
            async_sdk = self._ensure_async_sdk(helper_name, self._sdk)
            rest_args = args

        _validate_study_key_arg("AsyncSinksNamespace", helper_name, rest_args, resolved_kwargs)
        return async_sdk, rest_args, resolved_kwargs

    @overload
    async def export_to_mongodb(
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
    async def export_to_mongodb(
        self,
        sdk: AsyncImednetFacade,
        study_key: str,
        uri: str = "",
        database: str = "",
        collection: str = "",
        *,
        config: MongoDbSinkConfig | None = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    async def export_to_mongodb(self, *args: Any, **kwargs: Any) -> int:
        """Asynchronously export study records to MongoDB using the bound ``AsyncImednetSDK``."""
        from .document import async_export_to_mongodb

        async_sdk, rest_args, resolved_kwargs = self._resolve_export_call(
            "export_to_mongodb", args, kwargs
        )
        return await async_export_to_mongodb(async_sdk, *rest_args, **resolved_kwargs)

    @overload
    async def export_to_neo4j(
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
    async def export_to_neo4j(
        self,
        sdk: AsyncImednetFacade,
        study_key: str,
        uri: str = "",
        auth: tuple[str, str] = ("", ""),
        *,
        config: Neo4jSinkConfig | None = None,
        mask_phi: bool | None = None,
        **kwargs: Any,
    ) -> int: ...

    async def export_to_neo4j(self, *args: Any, **kwargs: Any) -> int:
        """Asynchronously export study records to Neo4j using the bound ``AsyncImednetSDK``."""
        from .graph import async_export_to_neo4j

        async_sdk, rest_args, resolved_kwargs = self._resolve_export_call(
            "export_to_neo4j", args, kwargs
        )
        return await async_export_to_neo4j(async_sdk, *rest_args, **resolved_kwargs)

    @overload
    async def export_to_snowflake(
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
    async def export_to_snowflake(
        self,
        sdk: AsyncImednetFacade,
        study_key: str,
        *,
        config: SnowflakeSinkConfig,
        mask_phi: bool | None = None,
    ) -> int: ...

    async def export_to_snowflake(self, *args: Any, **kwargs: Any) -> int:
        """Asynchronously export study records to Snowflake using the bound ``AsyncImednetSDK``."""
        from .warehouse import async_export_to_snowflake

        async_sdk, rest_args, resolved_kwargs = self._resolve_export_call(
            "export_to_snowflake", args, kwargs
        )
        return await async_export_to_snowflake(async_sdk, *rest_args, **resolved_kwargs)


@overload
def create_sinks(sdk: AsyncImednetFacade) -> AsyncSinksNamespace: ...


@overload
def create_sinks(sdk: ImednetFacade) -> SinksNamespace: ...


@overload
def create_sinks(
    sdk: ImednetFacade | AsyncImednetFacade,
) -> SinksNamespace | AsyncSinksNamespace: ...


def create_sinks(
    sdk: ImednetFacade | AsyncImednetFacade,
) -> SinksNamespace | AsyncSinksNamespace:
    """Factory function returning :class:`AsyncSinksNamespace` or :class:`SinksNamespace` for *sdk*."""
    from imednet.spi.utils import is_async_facade

    if is_async_facade(sdk):
        return AsyncSinksNamespace(cast("AsyncImednetFacade", sdk))
    return SinksNamespace(sdk)
