"""Export-sink error types."""

from .base import ImednetError


class ExportError(ImednetError):
    """Raised when an export sink cannot write a batch or finalize a destination."""


class ExportConfigurationError(ExportError, ValueError):
    """Raised when a sink is misconfigured (missing credentials, invalid parameters)."""


class ExportBatchError(ExportError):
    """Raised when a single batch write fails after all retries are exhausted."""

    def __init__(self, message: str, *, batch_id: str) -> None:
        """Initialize an export batch error.

        Args:
            message: The error message.
            batch_id: The ID of the failed batch.
        """
        super().__init__(message)
        self.batch_id = batch_id
