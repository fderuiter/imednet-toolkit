"""Exception hierarchy."""

from .api import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    ServerError,
    UnauthorizedError,
)
from .base import ImednetError
from .client import ClientError, PaginationError
from .export import ExportBatchError, ExportConfigurationError, ExportError
from .network import RequestError
from .orchestration import FilterConflictError, OrchestratorError
from .plugin import PluginLoadError
from .registry import get_error_class
from .validation import (
    BadRequestError,
    ConfigurationError,
    EvaluationError,
    PathTraversalValidationError,
    RestrictedEvaluationError,
    UnknownVariableTypeError,
    ValidationError,
)

__all__ = [
    "ApiError",
    "AuthenticationError",
    "AuthorizationError",
    "BadRequestError",
    "ClientError",
    "ConfigurationError",
    "ConflictError",
    "EvaluationError",
    "ExportBatchError",
    "ExportConfigurationError",
    "ExportError",
    "FilterConflictError",
    "ForbiddenError",
    "ImednetError",
    "NotFoundError",
    "OrchestratorError",
    "PaginationError",
    "PathTraversalValidationError",
    "PluginLoadError",
    "RateLimitError",
    "RequestError",
    "RestrictedEvaluationError",
    "ServerError",
    "UnauthorizedError",
    "UnknownVariableTypeError",
    "ValidationError",
    "get_error_class",
]
