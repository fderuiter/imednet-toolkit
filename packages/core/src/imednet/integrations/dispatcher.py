"""Unified functional facade for data export.

This module provides a single entry point `export()` for all data persistence tasks,
supporting both tabular procedural functions and object-oriented sink classes.
"""

import dataclasses
import threading
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any, cast

from imednet.integrations.sink_base import ExportSink, SinkConfig, apply_quality_gate, iter_batches
from imednet.sdk import ImednetSDK


class ExportRegistry:
    """Central registry mapping target types to their implementations."""

    def __init__(self) -> None:
        """Initialize an empty ExportRegistry."""
        self._tabular_targets: dict[str, Callable[..., Any]] = {}
        self._sink_targets: dict[str, type[ExportSink]] = {}
        self._config_targets: dict[str, type[SinkConfig]] = {}
        self._lock = threading.RLock()

    def register_tabular(self, target_type: str, func: Callable[..., Any]) -> None:
        """Register a tabular procedural function for a target type."""
        with self._lock:
            self._tabular_targets[target_type] = func

    def register_sink(self, target_type: str, sink_class: type[ExportSink]) -> None:
        """Register an object-oriented sink class for a target type."""
        with self._lock:
            self._sink_targets[target_type] = sink_class

    def register_config(self, target_type: str, config_class: type[SinkConfig]) -> None:
        """Register a configuration class for a target type."""
        with self._lock:
            self._config_targets[target_type] = config_class

    def get_tabular(self, target_type: str) -> Callable[..., Any] | None:
        """Retrieve a registered tabular function, or None if not found."""
        with self._lock:
            return self._tabular_targets.get(target_type)

    def get_sink(self, target_type: str) -> type[ExportSink] | None:
        """Retrieve a registered sink class, or None if not found."""
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


def register_sink_target(target_type: str, sink_class: type[ExportSink]) -> None:
    """Register an object-oriented sink class for a target type."""
    _registry.register_sink(target_type, sink_class)


def register_config_target(target_type: str, config_class: type[SinkConfig]) -> None:
    """Register a configuration class for a target type."""
    _registry.register_config(target_type, config_class)


def export(
    target: str,
    sdk: ImednetSDK,
    study_key: str,
    *,
    config: SinkConfig | None = None,
    **kwargs: Any,
) -> Any:
    """Unified entry point for data export.

    Routes the export request to either a tabular function or a sink class
    based on the target identifier.

    Args:
        target: The target destination type (e.g., 'csv', 'snowflake', 'mongodb').
        sdk: Authenticated SDK instance used to fetch study records.
        study_key: Study identifier to export.
        config: Optional sink configuration (for sink-based targets).
        **kwargs: Target-specific configuration parameters.

    Returns:
        For tabular targets: Typically None.
        For sink targets: The total number of records successfully written.

    Raises:
        ValueError: If the target type is not registered or unsupported.
    """
    # 1. Try tabular path
    tabular_func = _registry.get_tabular(target)
    if tabular_func is not None:
        return tabular_func(sdk, study_key, **kwargs)

    # 2. Try sink path
    sink_class = _registry.get_sink(target)
    if sink_class is not None:
        if config is not None:
            cfg = config
        else:
            config_class = _registry.get_config_class(target)
            valid_fields = {f.name for f in dataclasses.fields(config_class)}
            config_kwargs = {k: v for k, v in kwargs.items() if k in valid_fields}
            cfg = config_class(study_key=study_key, **config_kwargs)

        records = sdk.records.list(study_key=study_key, record_data_filter=None)
        filtered_records = list(apply_quality_gate(sdk, study_key, records, cfg))

        total_written = 0

        # Instantiate sink class using ONLY the configured config object
        with sink_class(config=cfg) as sink:
            for index, batch in enumerate(iter_batches(filtered_records, cfg.batch_size)):
                total_written += sink.write_batch(batch, batch_id=f"{study_key}/records/{index}")

        return total_written

    raise ValueError(f"Unsupported export target: {target!r}")
