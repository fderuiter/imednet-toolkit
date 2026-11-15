"""Dynamic dispatch descriptors for unified sync/async endpoint execution."""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from typing import Any, Concatenate, Generic, TypeVar, overload

from typing_extensions import ParamSpec

from imednet.core.endpoint.operations.filter_get import FilterGetOperation
from imednet.core.endpoint.operations.list import ListOperation

T = TypeVar("T")
P = ParamSpec("P")


class SyncEndpointContext:
    """Marker class for synchronous endpoints."""


class AsyncEndpointContext:
    """Marker class for asynchronous endpoints."""


class Operation(Generic[T]):
    """Protocol for an Operation that can be executed both ways."""

    def execute_sync(self, client: Any, parse_func: Any = None) -> T:
        """Execute sync."""
        raise NotImplementedError

    async def execute_async(self, client: Any, parse_func: Any = None) -> T:
        """Execute async."""
        raise NotImplementedError


class execute_operation(Generic[P, T]):  # noqa: N801
    """Descriptor that dispatches an operation-returning method to sync/async execution."""

    def __init__(self, func: Callable[Concatenate[Any, P], Operation[T]]):
        """Initialize."""
        self.func = func

    @overload
    def __get__(self, instance: SyncEndpointContext, owner: Any) -> Callable[P, T]: ...

    @overload
    def __get__(
        self, instance: AsyncEndpointContext, owner: Any
    ) -> Callable[P, Coroutine[Any, Any, T]]: ...

    @overload
    def __get__(self, instance: None, owner: Any) -> execute_operation[P, T]: ...

    def __get__(self, instance: Any, owner: Any) -> Any:
        """Get."""
        if instance is None:
            return self

        is_async = isinstance(instance, AsyncEndpointContext)
        func = self.func

        if is_async:

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                op = func(instance, *args, **kwargs)
                return await op.execute_async(instance._require_async_client())

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            op = func(instance, *args, **kwargs)
            return op.execute_sync(instance._require_sync_client())

        return sync_wrapper


class execute_list(Generic[P, T]):  # noqa: N801
    """Descriptor that dispatches a list operation."""

    def __init__(self, func: Callable[Concatenate[Any, P], ListOperation[T]]):
        """Initialize."""
        self.func = func

    @overload
    def __get__(self, instance: SyncEndpointContext, owner: Any) -> Callable[P, Iterator[T]]: ...

    @overload
    def __get__(
        self, instance: AsyncEndpointContext, owner: Any
    ) -> Callable[P, AsyncIterator[T]]: ...

    @overload
    def __get__(self, instance: None, owner: Any) -> execute_list[P, T]: ...

    def __get__(self, instance: Any, owner: Any) -> Any:
        """Get."""
        if instance is None:
            return self

        is_async = isinstance(instance, AsyncEndpointContext)
        func = self.func

        if is_async:

            @functools.wraps(func)
            def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> AsyncIterator[T]:
                op = func(instance, *args, **kwargs)
                return op.execute_async(
                    instance._require_async_client(), instance.ASYNC_PAGINATOR_CLS
                )

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> Iterator[T]:
            op = func(instance, *args, **kwargs)
            return op.execute_sync(instance._require_sync_client(), instance.PAGINATOR_CLS)

        return sync_wrapper


class execute_get(Generic[P, T]):  # noqa: N801
    """Descriptor that dispatches a get operation."""

    def __init__(self, func: Callable[Concatenate[Any, P], FilterGetOperation[T]]):
        """Initialize."""
        self.func = func

    @overload
    def __get__(self, instance: SyncEndpointContext, owner: Any) -> Callable[P, T]: ...

    @overload
    def __get__(
        self, instance: AsyncEndpointContext, owner: Any
    ) -> Callable[P, Coroutine[Any, Any, T]]: ...

    @overload
    def __get__(self, instance: None, owner: Any) -> execute_get[P, T]: ...

    def __get__(self, instance: Any, owner: Any) -> Any:
        """Get."""
        if instance is None:
            return self

        is_async = isinstance(instance, AsyncEndpointContext)
        func = self.func

        if is_async:

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                op = func(instance, *args, **kwargs)
                return await op.execute_async(
                    instance._require_async_client(), instance.ASYNC_PAGINATOR_CLS
                )

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            op = func(instance, *args, **kwargs)
            return op.execute_sync(instance._require_sync_client(), instance.PAGINATOR_CLS)

        return sync_wrapper
