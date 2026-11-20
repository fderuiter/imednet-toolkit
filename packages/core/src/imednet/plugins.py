"""Plugin contracts for the iMednet SDK.

This module defines the :class:`PluginProtocol` that namespace-level plugin
factories satisfy when discovered via the ``imednet.plugins`` entry-point
group (such as ``sinks = "imednet_sinks.plugin:create_sinks"``), together with
:class:`WorkflowsNamespaceProtocol` and :class:`SinksNamespaceProtocol`.

Individual workflows exposed on ``sdk.workflows`` are discovered lazily by
:class:`~imednet.sdk.WorkflowRegistry` via the ``imednet.workflows``
entry-point group, where each entry point maps a workflow attribute name to a
callable accepting the SDK instance.

Minimal plugin skeleton
-----------------------

To register an individual workflow on ``sdk.workflows``:

1. Expose a class or factory callable under ``[project.entry-points."imednet.workflows"]``.
2. The callable must accept a single argument — the :class:`~imednet.sdk.ImednetSDK`
   (or :class:`~imednet.sdk.AsyncImednetSDK`) instance — and return the workflow helper.

Example ``pyproject.toml`` snippet::

    [project.entry-points."imednet.workflows"]
    my_workflow = "myplugin.workflows:MyWorkflow"

    [project.entry-points."imednet.plugins"]
    sinks = "imednet_sinks.plugin:create_sinks"

Example workflow class::

    # myplugin/workflows.py
    from imednet.sdk import ImednetSDK

    class MyWorkflow:
        def __init__(self, sdk: ImednetSDK) -> None:
            self._sdk = sdk
"""

from __future__ import annotations

import inspect
import sys
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .spi.facade import AsyncImednetFacade, ImednetFacade

if sys.version_info < (3, 12) and not TYPE_CHECKING:
    from typing import _get_protocol_attrs, _ProtocolMeta

    class _SafeRuntimeProtocolMeta(_ProtocolMeta):
        def __instancecheck__(self, instance: Any) -> bool:
            if getattr(self, "_is_runtime_protocol", False):
                for attr in _get_protocol_attrs(self):
                    try:
                        inspect.getattr_static(instance, attr)
                    except AttributeError:
                        return False
                return True
            return super().__instancecheck__(instance)


@runtime_checkable
class WorkflowsNamespaceProtocol(Protocol):
    """Minimal interface that a workflows namespace object must expose.

    Each attribute should be a workflow class instance wired to the SDK.
    """

    data_extraction: Any
    duckdb_centralizer: Any
    query_management: Any
    record_mapper: Any
    record_update: Any
    register_subjects: Any
    subject_data: Any
    uat: Any
    uat_inspector: Any


if sys.version_info < (3, 12) and not TYPE_CHECKING:
    WorkflowsNamespaceProtocol.__class__ = _SafeRuntimeProtocolMeta


@runtime_checkable
class SinksNamespaceProtocol(Protocol):
    """Minimal interface that a sinks namespace object must expose."""

    export_to_mongodb: Any
    MongoDbSinkConfig: Any
    MongoDbExportSink: Any
    export_to_neo4j: Any
    Neo4jSinkConfig: Any
    Neo4jExportSink: Any
    export_to_snowflake: Any
    SnowflakeSinkConfig: Any
    SnowflakeExportSink: Any


@runtime_checkable
class PluginProtocol(Protocol):
    """Protocol that every iMednet plugin factory must satisfy.

    A conforming factory is a callable that accepts an SDK instance and returns
    an object implementing :class:`WorkflowsNamespaceProtocol`.

    Example::

        from imednet.plugins import PluginProtocol
        from imednet.sdk import ImednetSDK

        class MyWorkflows:
            def __init__(self, sdk: ImednetSDK) -> None:
                self.data_extraction = ...
                self.duckdb_centralizer = ...
                self.query_management = ...
                self.record_mapper = ...
                self.record_update = ...
                self.register_subjects = ...
                self.subject_data = ...
                self.uat = ...
                self.uat_inspector = ...

        def create_workflows(sdk: ImednetSDK) -> MyWorkflows:
            return MyWorkflows(sdk)

        # Verify conformance at import time (optional, for development use):
        assert isinstance(create_workflows, PluginProtocol)
    """

    def __call__(
        self, sdk_instance: ImednetFacade | AsyncImednetFacade
    ) -> WorkflowsNamespaceProtocol:
        """Initialize the workflows plugin with an SDK instance."""
        ...


@runtime_checkable
class SinksPluginProtocol(Protocol):
    """Protocol for the sinks plugin factory."""

    def __call__(self, sdk_instance: ImednetFacade | AsyncImednetFacade) -> SinksNamespaceProtocol:
        """Initialize the sinks plugin with an SDK instance."""
        ...


__all__ = [
    "PluginProtocol",
    "SinksNamespaceProtocol",
    "SinksPluginProtocol",
    "WorkflowsNamespaceProtocol",
]
