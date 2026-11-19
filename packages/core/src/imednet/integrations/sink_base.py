"""Shared base classes and helpers for all export sinks.

Architecture decision
---------------------
The SDK provides three export paths:

**Tabular path** (``RecordMapper`` + ``pandas.DataFrame``)
    Flattens record data into a wide DataFrame and writes it to CSV, Excel,
    JSON, SQL, DuckDB, or Parquet.  All functions in
    :mod:`imednet.integrations.export` follow this path.

**Structure-preserving path** (``DataExtractionWorkflow`` + typed records)
    Traverses the clinical data hierarchy
    *Study → Subject → Visit → Record* and materialises the relationships
    into a destination that can represent them natively — a property graph
    (Neo4j) or a nested document store (MongoDB).

**Warehouse path** (staged Parquet + native bulk loader)
    Writes one Parquet file per form to an intermediate staging area and then
    invokes the destination's native bulk-loading command (e.g. Snowflake
    ``COPY INTO``).

All three paths share the contracts defined in this module: ``SinkConfig``,
the ``ExportSink`` ABC, the import-guard helper :func:`_require_optional_dep`,
and the credential-redaction helper :func:`_redact_uri`.

Shared contracts
----------------
* **Batching** – callers split records into batches and call
  :meth:`ExportSink.write_batch` once per batch.  The ``batch_id`` parameter
  is a caller-supplied idempotency key (e.g. ``"<study_key>/<form_key>/<n>"``).
* **Chunk sizing** – ``SinkConfig.batch_size`` controls the number of records
  per batch (default 500).
* **Retries** – sinks must honour ``SinkConfig.max_retries`` and use
  ``SinkConfig.retry_backoff`` as the base delay between attempts.
* **Idempotent writes** – when ``SinkConfig.idempotent`` is ``True`` (default)
  sinks must use upsert semantics or ``CREATE OR REPLACE`` so that replaying a
  batch with the same ``batch_id`` produces no duplicate data.
* **Error propagation** – transient errors are retried; permanent errors raise
  :class:`~imednet.errors.ExportBatchError` (includes ``batch_id``) or
  :class:`~imednet.errors.ExportConfigurationError`.
* **Logging** – sinks use ``logging.getLogger(__name__)`` and must not log
  raw credentials or full URIs.  Pass URIs through :func:`_redact_uri` before
  logging.

Optional dependency conventions
--------------------------------
* Each sink module calls :func:`_require_optional_dep` at connection time (not
  at import time) so that importing the module never fails due to a missing
  optional library.
* Extras keys follow the pattern ``imednet[<key>]``:

  .. code-block:: console

     pip install 'imednet[neo4j]'
     pip install 'imednet[mongodb]'
     pip install 'imednet[snowflake]'

Public-API exposure rules
-------------------------
* :mod:`imednet.integrations` re-exports only the tabular helpers by default
  (backward compatibility).
* The three new sink classes (``Neo4jExportSink``, ``MongoDbExportSink``,
  ``SnowflakeExportSink``) and ``SinkConfig`` are importable from their
  respective submodules and are also re-exported from
  :mod:`imednet.integrations` via explicit names.
* Airflow helpers in :mod:`apache_airflow_providers_imednet.export` wrap only
  the tabular path; graph/document/warehouse sinks are not wrapped there.
"""  # noqa: RUF002

from __future__ import annotations

import logging
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from types import TracebackType
from typing import Any

from tenacity import wait_exponential

from imednet.core.operations.executor import OperationRetryPolicy
from imednet.errors import ExportConfigurationError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sink configuration
# ---------------------------------------------------------------------------

_DEFAULT_BATCH_SIZE = 500
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_BACKOFF = 1.0


class _SinkRetryPolicy(OperationRetryPolicy):
    """Keep deterministic configuration and input failures out of retries."""

    def should_retry(self, exception: Exception) -> bool:
        """Retry destination failures, preserving the existing driver fallback."""
        return not isinstance(exception, (ExportConfigurationError, ValueError, TypeError))


@dataclass
class SinkConfig:
    """Shared configuration for all export sinks.

    Parameters
    ----------
    study_key:
        Mandatory study key for the export.
    batch_size:
        Number of records per :meth:`ExportSink.write_batch` call.
    max_retries:
        Maximum number of retry attempts on transient errors.
    retry_backoff:
        Base delay in seconds between retry attempts.  The actual delay grows
        exponentially: ``retry_backoff * 2 ** attempt``.
    idempotent:
        When ``True``, sinks use upsert / replace semantics so that replaying
        a batch with the same ``batch_id`` produces no duplicate data.
    mask_phi:
        When ``True`` (default), sensitive clinical PHI fields in ``record_data``
        are replaced with ``***MASKED***`` before writing to the destination.
    """

    study_key: str
    batch_size: int = _DEFAULT_BATCH_SIZE
    max_retries: int = _DEFAULT_MAX_RETRIES
    retry_backoff: float = _DEFAULT_RETRY_BACKOFF
    idempotent: bool = True
    mask_phi: bool = True
    extra: dict[str, Any] = field(default_factory=dict)
    quality_gate_enabled: bool = False
    min_schema_readiness_score: float = 100.0
    tracer: Any | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Validate config properties after initialization."""
        if not self.study_key or not isinstance(self.study_key, str) or not self.study_key.strip():
            raise ValueError("study_key must be a non-empty string")
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 0
        ):
            raise ValueError("max_retries must be a non-negative integer")
        if (
            isinstance(self.retry_backoff, bool)
            or not isinstance(self.retry_backoff, (int, float))
            or not math.isfinite(self.retry_backoff)
            or self.retry_backoff < 0
        ):
            raise ValueError("retry_backoff must be a finite non-negative number")
        if not isinstance(self.mask_phi, bool):
            raise ValueError("mask_phi must be a boolean")


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class ExportSink(ABC):
    """Abstract base class for all export sinks.

    Subclasses **must** implement :meth:`write_batch`, :meth:`flush`, and
    :meth:`close`.  The context-manager protocol is provided by this class.

    Parameters
    ----------
    config:
        Shared sink configuration.  Defaults to :class:`SinkConfig` with
        all values at their defaults.
    """

    def __init__(self, config: SinkConfig) -> None:
        """Initialize the export sink with a configuration."""
        self.config: SinkConfig = config

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write one batch of records to the destination.

        Parameters
        ----------
        records:
            Sequence of records to write.  The concrete type depends on the
            export path:

            * **Tabular path** – ``pandas.DataFrame`` rows or plain dicts
              produced by :class:`~imednet_workflows.record_mapper.RecordMapper`.
            * **Structure-preserving path** – typed
              :class:`~imednet.models.Record` instances from
              :class:`~imednet_workflows.data_extraction.DataExtractionWorkflow`.
            * **Warehouse path** – ``pyarrow.RecordBatch`` or dicts destined
              for a staged Parquet file.
        batch_id:
            Caller-supplied idempotency key.  Recommended format:
            ``"<study_key>/<form_key>/<batch_number>"``.

        Returns:
        --------
        int
            Number of records successfully written.

        Raises:
        -------
        ~imednet.errors.ExportBatchError
            When the batch cannot be written after all retries.
        """  # noqa: RUF002
        ...

    @abstractmethod
    def flush(self) -> None:
        """Flush any internal buffers to the destination.

        Raises:
        -------
        ~imednet.errors.ExportError
            On flush failure.
        """
        ...

    @abstractmethod
    def close(self) -> None:
        """Release all resources held by this sink (connections, file handles).

        Implementations must be idempotent — calling ``close()`` on an already
        closed sink must not raise.
        """
        ...

    def _execute_with_retry(
        self,
        operation_name: str,
        batch_id: str,
        execute_fn: Callable[[], int],
    ) -> int:
        """Execute a batch operation with configured retries and telemetry.

        Parameters
        ----------
        operation_name:
            Telemetry span name.
        batch_id:
            Idempotency key for error reporting and spans.
        execute_fn:
            Callable that performs the export and returns the record count.
        """
        from imednet.core.operations.executor import UniversalExecutor
        from imednet.core.retry import RetryConfig
        from imednet.errors import ExportBatchError

        attempts = 0

        def execute_batch() -> int:
            nonlocal attempts
            attempts += 1
            # Driver messages may contain credentials, URIs or payloads. Only
            # sanitized exceptions may cross the executor/telemetry boundary.
            try:
                return execute_fn()
            except ExportConfigurationError:
                raise ExportConfigurationError("Invalid export configuration.") from None
            except (ValueError, TypeError):
                raise ValueError("Invalid export batch input.") from None
            except Exception:
                raise RuntimeError("Export destination operation failed.") from None

        executor = UniversalExecutor(
            retry_config=RetryConfig(
                retries=self.config.max_retries,
                backoff_factor=self.config.retry_backoff,
                retry_policy=_SinkRetryPolicy(),
            ),
            wait_strategy=wait_exponential(multiplier=self.config.retry_backoff),
            tracer=self.config.tracer,
            operation_name=operation_name,
            batch_id=batch_id,
        )
        try:
            return executor.execute(execute_batch)
        except Exception:
            raise ExportBatchError(
                f"Batch {batch_id!r} failed after {attempts} attempts.",
                batch_id=batch_id,
            ) from None

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> ExportSink:
        """Enter the context manager, returning the sink instance."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the context manager, flushing and closing the sink."""
        try:
            if exc_type is None:
                self.flush()
        finally:
            self.close()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Regex that matches the user-info component of a URI
# e.g.  mongodb://user:pass@host  ->  mongodb://***@host
_URI_USERINFO_RE = re.compile(r"(://)[^@/]+@")


def _redact_uri(uri: str) -> str:
    """Replace user-info in *uri* with ``***`` to prevent credential leakage.

    Examples:
        >>> _redact_uri("mongodb://user:pass@localhost:27017/db")
        'mongodb://***@localhost:27017/db'
        >>> _redact_uri("neo4j+s://bolt.example.com")
        'neo4j+s://bolt.example.com'
    """
    return _URI_USERINFO_RE.sub(r"\1***@", uri)


def _require_optional_dep(package: str, extras_key: str) -> Any:
    """Import *package* or raise :class:`ImportError` with a helpful message.

    Parameters
    ----------
    package:
        Top-level package name to import (e.g. ``"neo4j"``).
    extras_key:
        The ``imednet`` extras key that installs the dependency
        (e.g. ``"neo4j"``).

    Returns:
    --------
    types.ModuleType
        The imported module.

    Raises:
    -------
    ImportError
        When *package* is not installed.
    """
    try:
        return import_module(package)  # nosem
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith(package.split(".", maxsplit=1)[0]):
            raise ImportError(
                f"This export sink requires the optional '{package}' package. "
                f"Install with `pip install 'imednet[{extras_key}]'`."
            ) from error
        raise


def apply_quality_gate(
    sdk: Any, study_key: str, records: Iterable[Any], config: SinkConfig
) -> Iterator[Any]:
    """Filter records based on minimum schema readiness score if enabled."""
    if not config.quality_gate_enabled:
        yield from records
        return

    from imednet.validation.cache import SchemaValidator, calculate_readiness_score

    validator = SchemaValidator(sdk)
    validator.refresh(study_key)

    dropped_count = 0

    for record in records:
        if hasattr(record, "model_dump"):
            rec_dict = record.model_dump()
        elif hasattr(record, "__dict__"):
            rec_dict = {
                "form_key": getattr(record, "form_key", None),
                "form_id": getattr(record, "form_id", None),
                "record_data": getattr(record, "record_data", None),
                "recordData": getattr(record, "recordData", None),
                "data": getattr(record, "data", {}),
                "record_id": getattr(record, "record_id", None),
            }
        elif isinstance(record, dict):
            rec_dict = record
        else:
            rec_dict = {"data": {}}

        fk = rec_dict.get("formKey") or rec_dict.get("form_key")
        if not fk:
            fid = rec_dict.get("formId") or rec_dict.get("form_id") or 0
            fk = validator.schema.form_key_from_id(fid)

        if not fk:
            logger.info("Dropped record %s: Unknown form", rec_dict.get("record_id", "Unknown"))
            dropped_count += 1
            continue

        data = rec_dict.get("record_data")
        if data is None:
            data = rec_dict.get("recordData")
        if data is None:
            data = rec_dict.get("data", {})
        if data is None:
            data = {}
        score, reasons = calculate_readiness_score(validator.schema, fk, data)

        if score < config.min_schema_readiness_score:
            logger.info(
                "Dropped record %s (Score: %.1f < %.1f). Reasons: %s",
                rec_dict.get("record_id", "Unknown"),
                score,
                config.min_schema_readiness_score,
                "; ".join(reasons),
            )
            dropped_count += 1
            continue

        yield record

    if dropped_count > 0:
        logger.info("Quality gate dropped %d records in total.", dropped_count)


def iter_batches(records: Sequence[Any], batch_size: int) -> Iterator[Sequence[Any]]:
    """Yield ``records`` in chunks of ``batch_size``."""
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0")
    for start in range(0, len(records), batch_size):
        yield records[start : start + batch_size]


__all__ = [
    "ExportSink",
    "SinkConfig",
    "_redact_uri",
    "_require_optional_dep",
    "iter_batches",
]
