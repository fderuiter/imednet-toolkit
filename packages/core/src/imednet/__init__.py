"""iMednet SDK - A Python client for the iMednet EDC REST API."""

from importlib import metadata as _metadata

from .config import Config, load_config
from .core.base_client import BaseClient
from .core.retry import DefaultRetryPolicy, RetryPolicy, RetryState
from .errors import PluginLoadError
from .errors.orchestration import FilterConflictError, OrchestratorError
from .orchestration import (
    MultiStudyOrchestrator,
    OrchestratorResult,
    StudyWorkerCallable,
)
from .plugins import PluginProtocol, WorkflowsNamespaceProtocol
from .sdk import AsyncImednetSDK, ImednetSDK
from .simulation import (
    SimulationState,
    SimulationTransport,
    create_simulation_transport,
)
from .utils.typing import FilterScalar, FilterValue, ItemId, JsonDict

# Provide a backward-compatible alias
ImednetClient = ImednetSDK

__all__ = [
    "AsyncImednetSDK",
    "BaseClient",
    "Config",
    "DefaultRetryPolicy",
    "FilterConflictError",
    "FilterScalar",
    "FilterValue",
    "ImednetClient",
    "ImednetSDK",
    "ItemId",
    "JsonDict",
    "MultiStudyOrchestrator",
    "OrchestratorError",
    "OrchestratorResult",
    "PluginLoadError",
    "PluginProtocol",
    "RetryPolicy",
    "RetryState",
    "SimulationState",
    "SimulationTransport",
    "StudyWorkerCallable",
    "WorkflowsNamespaceProtocol",
    "__version__",
    "create_simulation_transport",
    "load_config",
]

try:
    __version__: str = _metadata.version("imednet")
except _metadata.PackageNotFoundError:  # local editable install
    __version__ = "1.0.0"
