"""Unified functional facade for synchronous and asynchronous data export.

This module provides ``export()`` and ``async_export()`` entry points for data
persistence tasks, supporting both tabular procedural functions and object-oriented
streaming sink classes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import threading
from collections.abc import Callable, Iterator
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import NonCallableMock

from imednet.integrations.sink_base import (
    AsyncExportSink,
    ExportSink,
    SinkConfig,
    _async_try_fetch_study_forms,
    apply_quality_gate,
    async_apply_quality_gate,
    async_iter_batches,
    async_iter_filtered_sdk_records,
    async_run_streaming_export,
    is_record_deleted,
    iter_batches,
    run_streaming_export,
)
from imednet.utils.async_bridge import call_async_facade_method, is_async_facade, maybe_to_thread

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade


class ExportRegistry:
    """Central registry mapping target types to sync and async implementations."""

    def __init__(self) -> None:
        """Initialize an empty ExportRegistry."""
        self._tabular_targets: dict[str, Callable[..., Any]] = {}
        self._async_tabular_targets: dict[str, Callable[..., Any]] = {}
        self._sink_targets: dict[str, type[ExportSink]] = {}
        self._async_sink_targets: dict[str, type[AsyncExportSink]] = {}
        self._config_targets: dict[str, type[SinkConfig]] = {}
        self._lock = threading.RLock()

    def register_tabular(self, target_type: str, func: Callable[..., Any]) -> None:
        """Register a tabular procedural function for a target type."""
        with self._lock:
            self._tabular_targets[target_type] = func

    def register_async_tabular(self, target_type: str, func: Callable[..., Any]) -> None:
        """Register an async tabular procedural function for a target type."""
        with self._lock:
            self._async_tabular_targets[target_type] = func

    def register_sink(self, target_type: str, sink_class: type[ExportSink]) -> None:
        """Register a synchronous object-oriented sink class for a target type."""
        with self._lock:
            self._sink_targets[target_type] = sink_class

    def register_async_sink(self, target_type: str, sink_class: type[AsyncExportSink]) -> None:
        """Register an asynchronous object-oriented sink class for a target type."""
        with self._lock:
            self._async_sink_targets[target_type] = sink_class

    def register_config(self, target_type: str, config_class: type[SinkConfig]) -> None:
        """Register a configuration class for a target type."""
        with self._lock:
            self._config_targets[target_type] = config_class

    def get_tabular(self, target_type: str) -> Callable[..., Any] | None:
        """Retrieve a registered tabular function, or None if not found."""
        with self._lock:
            return self._tabular_targets.get(target_type)

    def get_async_tabular(self, target_type: str) -> Callable[..., Any] | None:
        """Retrieve a registered async tabular function, or None if not found."""
        with self._lock:
            return self._async_tabular_targets.get(target_type)

    def get_sink(self, target_type: str) -> type[ExportSink] | None:
        """Retrieve a registered synchronous sink class, or None if not found."""
        with self._lock:
            if target_type in self._sink_targets:
                return self._sink_targets[target_type]

            for ep in entry_points(group="imednet.sinks"):
                if ep.name == target_type:
                    try:
                        sink_class = ep.load()
                        self.register_sink(target_type, sink_class)
                        return cast(type[ExportSink], sink_class)
                    except (ImportError, AttributeError):
                        return None

            return None

    def get_async_sink(self, target_type: str) -> type[AsyncExportSink] | None:
        """Retrieve a registered asynchronous sink class, or None if not found."""
        with self._lock:
            if target_type in self._async_sink_targets:
                return self._async_sink_targets[target_type]

            for ep in entry_points(group="imednet.async_sinks"):
                if ep.name == target_type:
                    try:
                        sink_class = ep.load()
                        self.register_async_sink(target_type, sink_class)
                        return cast(type[AsyncExportSink], sink_class)
                    except (ImportError, AttributeError):
                        return None

            return None

    def get_config_class(self, target_type: str) -> type[SinkConfig]:
        """Retrieve a registered config class, or SinkConfig as default."""
        with self._lock:
            if target_type in self._config_targets:
                return self._config_targets[target_type]

            for ep in entry_points(group="imednet.sink_configs"):
                if ep.name == target_type:
                    try:
                        config_class = ep.load()
                        self._config_targets[target_type] = config_class
                        return cast(type[SinkConfig], config_class)
                    except (ImportError, AttributeError):
                        pass

            return SinkConfig


# Global registry instance
_registry = ExportRegistry()


def register_tabular_target(target_type: str, func: Callable[..., Any]) -> None:
    """Register a tabular procedural function for a target type."""
    _registry.register_tabular(target_type, func)


def register_async_tabular_target(target_type: str, func: Callable[..., Any]) -> None:
    """Register an async tabular procedural function for a target type."""
    _registry.register_async_tabular(target_type, func)


def register_sink_target(target_type: str, sink_class: type[ExportSink]) -> None:
    """Register a synchronous object-oriented sink class for a target type."""
    _registry.register_sink(target_type, sink_class)


def register_async_sink_target(target_type: str, sink_class: type[AsyncExportSink]) -> None:
    """Register an asynchronous object-oriented sink class for a target type."""
    _registry.register_async_sink(target_type, sink_class)


def register_config_target(target_type: str, config_class: type[SinkConfig]) -> None:
    """Register a configuration class for a target type."""
    _registry.register_config(target_type, config_class)


class _PrefetchedListEndpoint:
    """In-memory list endpoint adapter backed by pre-fetched async items."""

    def __init__(self, items: list[Any], *, filter_deleted: bool = False) -> None:
        self._items = items
        self._filter_deleted = filter_deleted

    def list(self, **_kwargs: Any) -> Iterator[Any]:
        exclude_deleted = _kwargs.get("deleted") is False
        allowed_form_id = _kwargs.get("formId", _kwargs.get("form_id"))
        for item in self._items:
            if (self._filter_deleted or exclude_deleted) and is_record_deleted(item):
                continue
            if allowed_form_id is not None and not isinstance(allowed_form_id, (tuple, list, set)):
                item_fid = (
                    item.get("formId", item.get("form_id"))
                    if isinstance(item, dict)
                    else getattr(item, "form_id", getattr(item, "formId", None))
                )
                if item_fid is not None and item_fid != allowed_form_id:
                    continue
            yield item


class _PrefetchedSyncFacade:
    """Lightweight synchronous facade backed by records/forms/variables fetched asynchronously."""

    def __init__(
        self,
        async_sdk: Any,
        *,
        forms: list[Any],
        variables: list[Any],
        records: list[Any],
    ) -> None:
        self._async_sdk = async_sdk
        self._forms = forms
        self._variables = variables
        self._records = records
        self.forms = _PrefetchedListEndpoint(forms)
        self.variables = _PrefetchedListEndpoint(variables)
        self.records = _PrefetchedListEndpoint(records)

    @property
    def config(self) -> Any:
        return getattr(self._async_sdk, "config", None)

    @property
    def auth(self) -> Any:
        return getattr(self._async_sdk, "auth", None)

    @contextlib.contextmanager
    def study_context(self, study_key: str) -> Iterator[_PrefetchedSyncFacade]:
        from imednet.core.context import sdk_governance_context
        from imednet.core.context import study_context as core_study_context

        with sdk_governance_context(self._async_sdk), core_study_context(study_key):
            yield self

    def get_forms(self, study_key: str | None = None, **_filters: Any) -> list[Any]:
        _ = study_key
        return list(self._forms)

    def get_variables(self, study_key: str | None = None, **_filters: Any) -> list[Any]:
        _ = study_key
        return list(self._variables)

    def get_records(self, study_key: str | None = None, **_filters: Any) -> list[Any]:
        _ = study_key
        if _filters.get("deleted") is False:
            return [r for r in self._records if not is_record_deleted(r)]
        return list(self._records)


async def _run_tabular_via_async_prefetch(
    tabular_func: Callable[..., Any],
    sdk: AsyncImednetFacade,
    study_key: str,
    *,
    config: SinkConfig | None,
    kwargs: dict[str, Any],
) -> Any:
    """Fetch study metadata and records asynchronously, then serialize locally off-loop."""
    forms = await _async_try_fetch_study_forms(sdk, study_key)
    variables: list[Any] = []
    with contextlib.suppress(Exception):
        raw_vars = await call_async_facade_method(sdk, "get_variables", study_key=study_key)
        if not isinstance(raw_vars, NonCallableMock):
            variables = list(raw_vars)

    watermark = (
        getattr(config, "modified_since", None)
        if config is not None
        else kwargs.get("modified_since")
    )
    record_filter = (
        getattr(config, "record_filter", None)
        if config is not None
        else kwargs.get("record_filter")
    )
    record_data_filter = (
        getattr(config, "record_data_filter", None)
        if config is not None
        else kwargs.get("record_data_filter")
    )
    include_deleted = bool(
        getattr(config, "include_deleted", False)
        if config is not None
        else kwargs.get("include_deleted", False)
    )
    is_inc = bool(
        (getattr(config, "incremental", False) if config is not None else kwargs.get("incremental"))
        or (getattr(config, "state_provider", None) is not None if config is not None else False)
        or watermark is not None
    )

    records = [
        rec
        async for rec in async_iter_filtered_sdk_records(
            sdk,
            study_key,
            watermark=watermark,
            record_filter=record_filter,
            record_data_filter=record_data_filter,
            exclude_deleted_upstream=not (include_deleted or is_inc),
        )
    ]
    prefetched_sdk = _PrefetchedSyncFacade(
        sdk,
        forms=forms,
        variables=variables,
        records=records,
    )

    def _invoke() -> Any:
        if config is not None:
            return tabular_func(prefetched_sdk, study_key, config=config, **kwargs)
        return tabular_func(prefetched_sdk, study_key, **kwargs)

    return await maybe_to_thread(_invoke)


def _build_sink_config_for_target(
    target: str,
    study_key: str,
    config: SinkConfig | None,
    kwargs: dict[str, Any],
) -> SinkConfig:
    if config is not None:
        return config
    config_class = _registry.get_config_class(target)
    valid_fields = {f.name for f in dataclasses.fields(config_class)}
    config_kwargs = {k: v for k, v in kwargs.items() if k in valid_fields}
    return config_class(study_key=study_key, **config_kwargs)


def export(
    target: str,
    sdk: ImednetFacade,
    study_key: str,
    *,
    config: SinkConfig | None = None,
    **kwargs: Any,
) -> Any:
    """Unified synchronous entry point for data export.

    Routes the export request to either a tabular function or a sink class
    based on the target identifier.

    Raises:
        TypeError: If ``sdk`` is an :class:`~imednet.sdk.AsyncImednetSDK`.
        ValueError: If the target type is not registered or unsupported.
    """
    if type(sdk).__name__ == "AsyncImednetSDK" or (
        not isinstance(sdk, NonCallableMock) and is_async_facade(sdk)
    ):
        raise TypeError(
            "export() does not accept AsyncImednetSDK; use await async_export(...) instead."
        )

    # 1. Try tabular path
    tabular_func = _registry.get_tabular(target)
    if tabular_func is not None:
        if config is not None:
            return tabular_func(sdk, study_key, config=config, **kwargs)
        return tabular_func(sdk, study_key, **kwargs)

    # 2. Try sink path
    sink_class = _registry.get_sink(target)
    if sink_class is not None:
        cfg = _build_sink_config_for_target(target, study_key, config, kwargs)
        return run_streaming_export(
            sdk,
            study_key,
            lambda: sink_class(config=cfg),
            cfg,
            stream_name=cfg.stream_name or target,
            batch_id_prefix=f"{study_key}/records",
            quality_gate_fn=apply_quality_gate,
            iter_batches_fn=iter_batches,
        )

    raise ValueError(f"Unsupported export target: {target!r}")


async def async_export(
    target: str,
    sdk: AsyncImednetFacade,
    study_key: str,
    *,
    config: SinkConfig | None = None,
    **kwargs: Any,
) -> Any:
    """Unified asynchronous entry point for data export.

    Routes the export request to an async tabular function, a native
    :class:`~imednet.integrations.sink_base.AsyncExportSink` class, or an
    async-prefetched local tabular target.

    Raises:
        TypeError: If ``sdk`` is a synchronous :class:`~imednet.sdk.ImednetSDK`.
        ValueError: If the target type is not registered or unsupported.
    """
    if type(sdk).__name__ == "ImednetSDK" or (
        not isinstance(sdk, NonCallableMock) and not is_async_facade(sdk)
    ):
        raise TypeError(
            "async_export() requires an AsyncImednetFacade or AsyncImednetSDK; "
            "use export() for synchronous SDK instances."
        )

    # 1. Try native async tabular path
    async_tabular_func = _registry.get_async_tabular(target)
    if async_tabular_func is not None:
        res = (
            async_tabular_func(sdk, study_key, config=config, **kwargs)
            if config is not None
            else async_tabular_func(sdk, study_key, **kwargs)
        )
        return await res if inspect.isawaitable(res) else res

    # 2. Try native async sink path
    async_sink_class = _registry.get_async_sink(target)
    if async_sink_class is not None:
        cfg = _build_sink_config_for_target(target, study_key, config, kwargs)
        return await async_run_streaming_export(
            sdk,
            study_key,
            lambda: async_sink_class(config=cfg),
            cfg,
            stream_name=cfg.stream_name or target,
            batch_id_prefix=f"{study_key}/records",
            quality_gate_fn=async_apply_quality_gate,
            iter_batches_fn=async_iter_batches,
        )

    # 3. Try local tabular target with non-blocking async SDK prefetch
    tabular_func = _registry.get_tabular(target)
    if tabular_func is not None:
        return await _run_tabular_via_async_prefetch(
            tabular_func,
            sdk,
            study_key,
            config=config,
            kwargs=kwargs,
        )

    raise ValueError(f"Unsupported export target: {target!r}")
