"""Dynamic dispatch descriptors for unified sync/async endpoint execution."""

from __future__ import annotations

import functools
import inspect
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    Generic,
    TypeVar,
    cast,
    get_args,
    get_type_hints,
    overload,
)

from typing_extensions import ParamSpec

if TYPE_CHECKING:
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


def _resolve_hints_and_return(
    func: Callable[..., Any],
    owner: Any,
    *,
    is_list: bool = False,
    is_async: bool = False,
) -> tuple[dict[str, Any], Any]:
    """Resolve parameter type hints and concrete return annotation for an endpoint method."""
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = dict(getattr(func, "__annotations__", {}))

    model_cls = getattr(owner, "MODEL", Any) if owner is not None else Any
    raw_ret = hints.get("return", Any)
    type_args = get_args(raw_ret)

    if type_args:
        candidate = type_args[0]
        inner = model_cls if isinstance(candidate, TypeVar) else candidate
    elif isinstance(raw_ret, type) and hasattr(raw_ret, "execute_sync"):
        try:
            op_hints = get_type_hints(raw_ret.execute_sync)
            candidate = op_hints.get("return", Any)
        except Exception:
            candidate = Any
        inner = model_cls if isinstance(candidate, TypeVar) else candidate
    elif isinstance(raw_ret, TypeVar):
        inner = model_cls
    else:
        inner = model_cls if raw_ret is Any else raw_ret

    if is_list:
        iterator_cls = cast(Any, AsyncIterator if is_async else Iterator)
        resolved_ret: Any = iterator_cls[inner]
    else:
        resolved_ret = inner
    return hints, resolved_ret


def _configure_wrapper(
    wrapper: Callable[..., Any],
    func: Callable[..., Any],
    owner: Any,
    *,
    is_list: bool = False,
    is_async: bool = False,
    bound: bool = False,
) -> Callable[..., Any]:
    """Copy function metadata and attach resolved signature and annotations."""
    functools.update_wrapper(wrapper, func)
    if not bound:
        wrapper.__dict__.pop("__wrapped__", None)

    hints, resolved_ret = _resolve_hints_and_return(
        func,
        owner,
        is_list=is_list,
        is_async=is_async,
    )
    model_cls = getattr(owner, "MODEL", Any) if owner is not None else Any

    sig = inspect.signature(func)
    params = list(sig.parameters.values())

    resolved_annotations: dict[str, Any] = {}
    for key, val in hints.items():
        if key == "return":
            continue
        resolved_annotations[key] = model_cls if isinstance(val, TypeVar) else val
    resolved_annotations["return"] = resolved_ret

    updated_params = [
        param.replace(annotation=resolved_annotations[param.name])
        if param.name in resolved_annotations
        else param
        for param in params
    ]
    wrapper.__annotations__ = resolved_annotations
    wrapper.__signature__ = sig.replace(  # type: ignore[attr-defined]
        parameters=updated_params,
        return_annotation=resolved_ret,
    )
    return wrapper


def _build_operation_callable(
    func: Callable[..., Any],
    instance: Any,
    owner: Any,
    *,
    pass_paginator: bool = False,
) -> Callable[..., Any]:
    """Build bound or unbound sync/async callable for single-result endpoint operations."""
    target_cls = type(instance) if instance is not None else owner
    is_async = isinstance(target_cls, type) and issubclass(target_cls, AsyncEndpointContext)

    if instance is None:
        if is_async:

            async def unbound_async(endpoint: Any, *args: Any, **kwargs: Any) -> Any:
                op = func(endpoint, *args, **kwargs)
                if pass_paginator:
                    return await op.execute_async(
                        endpoint._require_async_client(),
                        endpoint.ASYNC_PAGINATOR_CLS,
                    )
                return await op.execute_async(endpoint._require_async_client())

            return _configure_wrapper(unbound_async, func, target_cls, is_async=True, bound=False)

        def unbound_sync(endpoint: Any, *args: Any, **kwargs: Any) -> Any:
            op = func(endpoint, *args, **kwargs)
            if pass_paginator:
                return op.execute_sync(endpoint._require_sync_client(), endpoint.PAGINATOR_CLS)
            return op.execute_sync(endpoint._require_sync_client())

        return _configure_wrapper(unbound_sync, func, target_cls, is_async=False, bound=False)

    if is_async:

        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            op = func(instance, *args, **kwargs)
            if pass_paginator:
                return await op.execute_async(
                    instance._require_async_client(),
                    instance.ASYNC_PAGINATOR_CLS,
                )
            return await op.execute_async(instance._require_async_client())

        return _configure_wrapper(async_wrapper, func, target_cls, is_async=True, bound=True)

    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        op = func(instance, *args, **kwargs)
        if pass_paginator:
            return op.execute_sync(instance._require_sync_client(), instance.PAGINATOR_CLS)
        return op.execute_sync(instance._require_sync_client())

    return _configure_wrapper(sync_wrapper, func, target_cls, is_async=False, bound=True)


class execute_operation(Generic[P, T]):  # noqa: N801
    """Descriptor that dispatches an operation-returning method to sync/async execution."""

    def __init__(self, func: Callable[Concatenate[Any, P], Operation[T]]):
        """Initialize."""
        self.func = func
        functools.update_wrapper(cast(Any, self), func)

    @overload
    def __get__(self, instance: SyncEndpointContext, owner: Any) -> Callable[P, T]: ...

    @overload
    def __get__(
        self, instance: AsyncEndpointContext, owner: Any
    ) -> Callable[P, Coroutine[Any, Any, T]]: ...

    @overload
    def __get__(
        self, instance: None, owner: type[AsyncEndpointContext]
    ) -> Callable[Concatenate[Any, P], Coroutine[Any, Any, T]]: ...

    @overload
    def __get__(
        self, instance: None, owner: type[SyncEndpointContext]
    ) -> Callable[Concatenate[Any, P], T]: ...

    @overload
    def __get__(self, instance: None, owner: Any) -> Callable[Concatenate[Any, P], Any]: ...

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        """Get."""
        if instance is None and owner is None:
            return self
        return _build_operation_callable(self.func, instance, owner, pass_paginator=False)


class execute_list(Generic[P, T]):  # noqa: N801
    """Descriptor that dispatches a list operation."""

    def __init__(self, func: Callable[Concatenate[Any, P], ListOperation[T]]):
        """Initialize."""
        self.func = func
        functools.update_wrapper(cast(Any, self), func)

    @overload
    def __get__(self, instance: SyncEndpointContext, owner: Any) -> Callable[P, Iterator[T]]: ...

    @overload
    def __get__(
        self, instance: AsyncEndpointContext, owner: Any
    ) -> Callable[P, AsyncIterator[T]]: ...

    @overload
    def __get__(
        self, instance: None, owner: type[AsyncEndpointContext]
    ) -> Callable[Concatenate[Any, P], AsyncIterator[T]]: ...

    @overload
    def __get__(
        self, instance: None, owner: type[SyncEndpointContext]
    ) -> Callable[Concatenate[Any, P], Iterator[T]]: ...

    @overload
    def __get__(self, instance: None, owner: Any) -> Callable[Concatenate[Any, P], Any]: ...

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        """Get."""
        if instance is None and owner is None:
            return self

        target_cls = type(instance) if instance is not None else owner
        is_async = isinstance(target_cls, type) and issubclass(target_cls, AsyncEndpointContext)
        func = self.func

        if instance is None:
            if is_async:

                def unbound_async(
                    endpoint: Any, *args: P.args, **kwargs: P.kwargs
                ) -> AsyncIterator[T]:
                    op = func(endpoint, *args, **kwargs)
                    return op.execute_async(
                        endpoint._require_async_client(),
                        endpoint.ASYNC_PAGINATOR_CLS,
                    )

                return _configure_wrapper(
                    unbound_async, func, target_cls, is_list=True, is_async=True, bound=False
                )

            def unbound_sync(endpoint: Any, *args: P.args, **kwargs: P.kwargs) -> Iterator[T]:
                op = func(endpoint, *args, **kwargs)
                return op.execute_sync(endpoint._require_sync_client(), endpoint.PAGINATOR_CLS)

            return _configure_wrapper(
                unbound_sync, func, target_cls, is_list=True, is_async=False, bound=False
            )

        if is_async:

            def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> AsyncIterator[T]:
                op = func(instance, *args, **kwargs)
                return op.execute_async(
                    instance._require_async_client(), instance.ASYNC_PAGINATOR_CLS
                )

            return _configure_wrapper(
                async_wrapper, func, target_cls, is_list=True, is_async=True, bound=True
            )

        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> Iterator[T]:
            op = func(instance, *args, **kwargs)
            return op.execute_sync(instance._require_sync_client(), instance.PAGINATOR_CLS)

        return _configure_wrapper(
            sync_wrapper, func, target_cls, is_list=True, is_async=False, bound=True
        )


class execute_get(Generic[P, T]):  # noqa: N801
    """Descriptor that dispatches a get operation."""

    def __init__(self, func: Callable[Concatenate[Any, P], FilterGetOperation[T]]):
        """Initialize."""
        self.func = func
        functools.update_wrapper(cast(Any, self), func)

    @overload
    def __get__(self, instance: SyncEndpointContext, owner: Any) -> Callable[P, T]: ...

    @overload
    def __get__(
        self, instance: AsyncEndpointContext, owner: Any
    ) -> Callable[P, Coroutine[Any, Any, T]]: ...

    @overload
    def __get__(
        self, instance: None, owner: type[AsyncEndpointContext]
    ) -> Callable[Concatenate[Any, P], Coroutine[Any, Any, T]]: ...

    @overload
    def __get__(
        self, instance: None, owner: type[SyncEndpointContext]
    ) -> Callable[Concatenate[Any, P], T]: ...

    @overload
    def __get__(self, instance: None, owner: Any) -> Callable[Concatenate[Any, P], Any]: ...

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        """Get."""
        if instance is None and owner is None:
            return self
        return _build_operation_callable(self.func, instance, owner, pass_paginator=True)
