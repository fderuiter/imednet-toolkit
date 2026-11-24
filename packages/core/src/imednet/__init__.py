# pylint: disable=duplicate-code
"""iMednet SDK - A Python client for the iMednet EDC REST API."""

from __future__ import annotations

from importlib import import_module
from importlib import metadata as _metadata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config, load_config
    from .core.base_client import BaseClient
    from .core.retry import DefaultRetryPolicy, RetryPolicy, RetryState
    from .errors import ApiError, ImednetAPIError, ImednetError, NotFoundError, PluginLoadError
    from .errors.orchestration import FilterConflictError, OrchestratorError
    from .orchestration import (
        MultiStudyOrchestrator,
        OrchestratorResult,
        StudyWorkerCallable,
    )
    from .plugins import (
        AsyncSinksNamespaceProtocol,
        AsyncWorkflowsNamespaceProtocol,
        PluginProtocol,
        SinksNamespaceProtocol,
        SyncWorkflowsNamespaceProtocol,
        WorkflowsNamespaceProtocol,
    )
    from .sdk import AsyncImednetSDK, ImednetSDK
    from .simulation import (
        SimulationState,
        SimulationTransport,
        create_simulation_transport,
    )
    from .utils.typing import FilterScalar, FilterValue, ItemId, JsonDict, RecordsInput

    ImednetClient = ImednetSDK

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "ApiError": ("imednet.errors", "ApiError"),
    "AsyncImednetSDK": ("imednet.sdk", "AsyncImednetSDK"),
    "AsyncSinksNamespaceProtocol": ("imednet.plugins", "AsyncSinksNamespaceProtocol"),
    "AsyncWorkflowsNamespaceProtocol": ("imednet.plugins", "AsyncWorkflowsNamespaceProtocol"),
    "BaseClient": ("imednet.core.base_client", "BaseClient"),
    "Config": ("imednet.config", "Config"),
    "DefaultRetryPolicy": ("imednet.core.retry", "DefaultRetryPolicy"),
    "FilterConflictError": ("imednet.errors.orchestration", "FilterConflictError"),
    "FilterScalar": ("imednet.utils.typing", "FilterScalar"),
    "FilterValue": ("imednet.utils.typing", "FilterValue"),
    "ImednetAPIError": ("imednet.errors", "ImednetAPIError"),
    "ImednetClient": ("imednet.sdk", "ImednetSDK"),
    "ImednetError": ("imednet.errors", "ImednetError"),
    "ImednetSDK": ("imednet.sdk", "ImednetSDK"),
    "ItemId": ("imednet.utils.typing", "ItemId"),
    "JsonDict": ("imednet.utils.typing", "JsonDict"),
    "MultiStudyOrchestrator": ("imednet.orchestration", "MultiStudyOrchestrator"),
    "NotFoundError": ("imednet.errors", "NotFoundError"),
    "OrchestratorError": ("imednet.errors.orchestration", "OrchestratorError"),
    "OrchestratorResult": ("imednet.orchestration", "OrchestratorResult"),
    "PluginLoadError": ("imednet.errors", "PluginLoadError"),
    "PluginProtocol": ("imednet.plugins", "PluginProtocol"),
    "RecordsInput": ("imednet.utils.typing", "RecordsInput"),
    "RetryPolicy": ("imednet.core.retry", "RetryPolicy"),
    "RetryState": ("imednet.core.retry", "RetryState"),
    "SimulationState": ("imednet.simulation", "SimulationState"),
    "SimulationTransport": ("imednet.simulation", "SimulationTransport"),
    "SinksNamespaceProtocol": ("imednet.plugins", "SinksNamespaceProtocol"),
    "StudyWorkerCallable": ("imednet.orchestration", "StudyWorkerCallable"),
    "SyncWorkflowsNamespaceProtocol": ("imednet.plugins", "SyncWorkflowsNamespaceProtocol"),
    "WorkflowsNamespaceProtocol": ("imednet.plugins", "WorkflowsNamespaceProtocol"),
    "create_simulation_transport": ("imednet.simulation", "create_simulation_transport"),
    "load_config": ("imednet.config", "load_config"),
}


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Lazily resolve top-level SDK exports on demand."""
        try:
            module_path, obj_name = _LAZY_ATTRS[name]
        except KeyError:
            raise AttributeError(name) from None
        mod = import_module(module_path)  # nosem
        obj = getattr(mod, obj_name)
        globals()[name] = obj
        return obj


def __dir__() -> list[str]:
    """Return module attributes including lazy exports."""
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "ApiError",
    "AsyncImednetSDK",
    "AsyncSinksNamespaceProtocol",
    "AsyncWorkflowsNamespaceProtocol",
    "BaseClient",
    "Config",
    "DefaultRetryPolicy",
    "FilterConflictError",
    "FilterScalar",
    "FilterValue",
    "ImednetAPIError",
    "ImednetClient",
    "ImednetError",
    "ImednetSDK",
    "ItemId",
    "JsonDict",
    "MultiStudyOrchestrator",
    "NotFoundError",
    "OrchestratorError",
    "OrchestratorResult",
    "PluginLoadError",
    "PluginProtocol",
    "RecordsInput",
    "RetryPolicy",
    "RetryState",
    "SimulationState",
    "SimulationTransport",
    "SinksNamespaceProtocol",
    "StudyWorkerCallable",
    "SyncWorkflowsNamespaceProtocol",
    "WorkflowsNamespaceProtocol",
    "__version__",
    "create_simulation_transport",
    "load_config",
]

try:
    __version__: str = _metadata.version("imednet")
except _metadata.PackageNotFoundError:  # local editable install
    __version__ = "1.1.0"  # x-release-please-version
