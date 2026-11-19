"""Plugin contracts for the iMednet SDK.

This module defines the :class:`PluginProtocol` that any third-party plugin
factory must satisfy in order to be discoverable via the ``imednet.plugins``
entry-point group, together with the :class:`WorkflowsNamespaceProtocol` that
the factory's return value must implement.

Minimal plugin skeleton
-----------------------

A plugin package must:

1. Expose a factory callable under the ``imednet.plugins`` entry-point group
   with the name ``"workflows"`` (or another agreed name).
2. The callable must accept a single argument — the :class:`~imednet.sdk.ImednetSDK`
   (or :class:`~imednet.sdk.AsyncImednetSDK`) instance — and return an object
   that satisfies :class:`WorkflowsNamespaceProtocol`.

Example ``pyproject.toml`` snippet::

    [project.entry-points."imednet.plugins"]
    workflows = "myplugin.namespace:create_workflows"

Example factory::

    # myplugin/namespace.py
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
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Protocol, _ProtocolMeta, runtime_checkable

if TYPE_CHECKING:
    from .spi.facade import AsyncImednetFacade, ImednetFacade


if not TYPE_CHECKING:

    def _protocol_attribute_names(protocol_cls: type[Any]) -> set[str]:
        """Collect protocol attribute names across the MRO without invoking descriptors."""
        attrs: set[str] = set()
        for base in protocol_cls.__mro__[:-1]:
            if base.__name__ in ("Protocol", "Generic"):
                continue
            annotations = getattr(base, "__annotations__", {})
            for name in list(base.__dict__.keys()) + list(annotations.keys()):
                if not name.startswith("_abc_") and name not in (
                    "__abstractmethods__",
                    "__annotations__",
                    "__weakref__",
                    "_is_protocol",
                    "_is_runtime_protocol",
                    "__doc__",
                    "__args__",
                    "__slots__",
                    "__next_in_mro__",
                    "__parameters__",
                    "__origin__",
                    "__orig_bases__",
                    "__extra__",
                    "__tree_hash__",
                    "__module__",
                    "__subclasshook__",
                    "__init__",
                    "__new__",
                    "_MutableMapping__marker",
                    "__callable_proto_members_only__",
                    "__non_callable_proto_members__",
                    "__protocol_attrs__",
                ):
                    attrs.add(name)
        return attrs

    class _SafeRuntimeProtocolMeta(_ProtocolMeta):
        def __instancecheck__(self, instance: Any) -> bool:
            if getattr(self, "_is_runtime_protocol", False):
                for attr in _protocol_attribute_names(self):
                    try:
                        inspect.getattr_static(instance, attr)
                    except AttributeError:
                        return False
                return True
            return super().__instancecheck__(instance)


@runtime_checkable
class AsyncWorkflowsNamespaceProtocol(Protocol):
    """Minimal interface that an asynchronous workflows namespace object must expose.

    Exposes workflows supporting native ``async`` operations when bound to
    :class:`~imednet.sdk.AsyncImednetSDK`.
    """

    record_update: Any
    uat_inspector: Any


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


if not TYPE_CHECKING:
    AsyncWorkflowsNamespaceProtocol.__class__ = _SafeRuntimeProtocolMeta
    WorkflowsNamespaceProtocol.__class__ = _SafeRuntimeProtocolMeta
    SinksNamespaceProtocol.__class__ = _SafeRuntimeProtocolMeta


@runtime_checkable
class PluginProtocol(Protocol):
    """Protocol that every iMednet workflows plugin factory must satisfy.

    A conforming factory is a callable that accepts an SDK instance and returns
    an object implementing :class:`WorkflowsNamespaceProtocol` for synchronous
    SDKs or :class:`AsyncWorkflowsNamespaceProtocol` for asynchronous SDKs.

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
    ) -> WorkflowsNamespaceProtocol | AsyncWorkflowsNamespaceProtocol:
        """Initialize the workflows plugin with an SDK instance."""
        ...


@runtime_checkable
class SinksPluginProtocol(Protocol):
    """Protocol for the sinks plugin factory."""

    def __call__(self, sdk_instance: ImednetFacade | AsyncImednetFacade) -> SinksNamespaceProtocol:
        """Initialize the sinks plugin with an SDK instance."""
        ...


__all__ = [
    "AsyncWorkflowsNamespaceProtocol",
    "PluginProtocol",
    "SinksNamespaceProtocol",
    "SinksPluginProtocol",
    "WorkflowsNamespaceProtocol",
]
