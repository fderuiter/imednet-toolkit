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
* :mod:`imednet.integrations` exports tabular helpers and shared sink contracts
  (``SinkConfig`` and ``ExportSink``).
* Document, graph, and warehouse sinks (``MongoDbExportSink``,
  ``Neo4jExportSink``, ``SnowflakeExportSink``) and their configuration classes
  and ``export_to_*`` helpers live in :mod:`imednet_sinks`
  (:mod:`imednet_sinks.document`, :mod:`imednet_sinks.graph`,
  :mod:`imednet_sinks.warehouse`) and are exposed dynamically via ``sdk.sinks``.
* Airflow helpers in :mod:`apache_airflow_providers_imednet.export` wrap only
  the tabular path, while ``ImednetExportOperator`` resolves ``mongodb``,
  ``neo4j``, and ``snowflake`` destinations dynamically via ``sdk.sinks``.
"""  # noqa: RUF002

from __future__ import annotations

import contextlib
import copy
import functools
import itertools
import logging
import math
import re
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, cast, runtime_checkable
from unittest.mock import NonCallableMock

from tenacity import wait_exponential

from imednet.core.operations.executor import OperationRetryPolicy
from imednet.errors import ExportConfigurationError
from imednet.utils.dates import (
    format_iso_datetime,
    normalize_utc_timestamp_bound,
    parse_iso_datetime,
)
from imednet.utils.filters import parse_filter_expressions, validate_connector_exclusivity
from imednet.utils.security import global_sensitivity_registry, mask_clinical_phi

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sink configuration & checkpoint provider SPI
# ---------------------------------------------------------------------------

_DEFAULT_BATCH_SIZE = 500
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_BACKOFF = 1.0
STATE_PROVIDER_ENTRY_POINT_GROUP = "imednet.state_providers"


@runtime_checkable
class StateProviderProtocol(Protocol):
    """Runtime-discoverable protocol for incremental export checkpoint ledgers."""

    def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
        """Retrieve high-water mark timestamp for a study stream."""
        ...

    def set_last_timestamp(
        self,
        study_key: str,
        stream_name: str,
        timestamp: datetime,
        records_processed: int = 0,
        metadata: dict[str, Any] | None = None,
        status: Any = "success",
        error_message: str | None = None,
    ) -> None:
        """Persist high-water mark timestamp and execution metadata."""
        ...

    def transaction(
        self,
        study_key: str,
        stream_name: str,
        fallback_timestamp: datetime | None = None,
    ) -> AbstractContextManager[dict[str, Any]]:
        """Execute a checkpoint transaction committing only on clean exit."""
        ...

    def read_state(self) -> Any:
        """Read full ledger state."""
        ...

    def delete_entry(self, study_key: str, stream_name: str | None = None) -> bool:
        """Delete ledger state entry."""
        ...


def normalize_watermark_datetime(value: datetime | str | None) -> datetime | None:
    """Normalize a datetime or ISO-8601 date/timestamp string into UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = parse_iso_datetime(stripped)
        except ValueError:
            normalized_iso = normalize_utc_timestamp_bound(stripped, is_end=False)
            parsed = parse_iso_datetime(normalized_iso)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise ValueError(f"Watermark must be a datetime or ISO-8601 string, got {type(value).__name__}")


def get_state_provider(
    ledger_path: str | Path | None = None,
    *,
    provider_name: str | None = None,
) -> StateProviderProtocol:
    """Discover and instantiate a pluggable checkpoint state provider at runtime."""
    from importlib.metadata import entry_points

    target_name = provider_name or "default"
    eps = list(entry_points(group=STATE_PROVIDER_ENTRY_POINT_GROUP))
    matched = [ep for ep in eps if ep.name == target_name]
    if not matched and provider_name is None and eps:
        matched = [eps[0]]
    if not matched:
        raise ImportError(
            f"Checkpoint state provider '{target_name}' requires an installed workflows plugin. "
            "Install 'imednet-workflows' or 'imednet[workflows]'."
        )

    factory = matched[0].load()
    if target_name == "airflow":
        provider = factory()
    elif ledger_path is not None:
        provider = (
            factory(ledger_path=ledger_path) if target_name == "default" else factory(ledger_path)
        )
    else:
        provider = factory()
    return cast(StateProviderProtocol, provider)


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
    modified_since:
        Optional lower-bound modification timestamp watermark (``dateModified >= ...``).
    record_filter:
        Optional record attribute filter dictionary or filter string.
    record_data_filter:
        Optional clinical variable payload filter (``recordDataFilter``).
    include_deleted:
        When ``True``, voided (``deleted=True``) records are included in exports.
    incremental:
        When ``True``, reads and commits checkpoints via a state ledger provider.
    stream_name:
        Optional stream identifier used in the checkpoint state ledger.
    ledger_path:
        Optional file path for file-backed checkpoint state providers.
    state_provider:
        Optional pre-configured checkpoint state provider instance.
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
    modified_since: datetime | str | None = None
    record_filter: dict[str, Any] | str | None = None
    record_data_filter: str | None = None
    include_deleted: bool = False
    incremental: bool = False
    stream_name: str | None = None
    ledger_path: str | Path | None = None
    state_provider: Any | None = field(default=None, repr=False)

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
        if self.modified_since is not None:
            normalize_watermark_datetime(self.modified_since)
        if isinstance(self.record_filter, str) and self.record_filter.strip():
            validate_connector_exclusivity(self.record_filter)
        if isinstance(self.record_data_filter, str) and self.record_data_filter.strip():
            validate_connector_exclusivity(self.record_data_filter)


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


def _redact_dataframe_batch(df: Any, pd_mod: Any) -> Any:
    """Return a PHI-redacted copy of a pandas DataFrame without mutating caller state."""
    masked = df.copy()
    sensitive_cols = list(
        dict.fromkeys(
            col for col in masked.columns if global_sensitivity_registry.is_sensitive(str(col))
        )
    )
    for col in sensitive_cols:
        if len(masked) > 0:
            masked[col] = "***MASKED***"

    if len(masked) == 0:
        return masked

    remaining = masked.drop(columns=sensitive_cols)
    try:
        object_cols = list(dict.fromkeys(remaining.select_dtypes(include=[object, "str"]).columns))
    except TypeError:
        object_cols = list(dict.fromkeys(remaining.select_dtypes(include=[object]).columns))

    _ = pd_mod
    for col in object_cols:
        target = masked[col]
        if hasattr(target, "columns"):
            masked[col] = target.apply(lambda s: s.apply(mask_clinical_phi))
        else:
            masked[col] = target.apply(mask_clinical_phi)
    return masked


def _redact_single_record(item: Any) -> Any:
    """Redact PHI on a single Mapping, Pydantic model, or record-like object."""
    if isinstance(item, Mapping):
        return mask_clinical_phi(dict(item))
    if isinstance(item, NonCallableMock):
        return item

    model_copy = getattr(type(item), "model_copy", None)
    model_fields = getattr(type(item), "model_fields", None)
    if callable(model_copy) and isinstance(model_fields, Mapping):
        updates: dict[str, Any] = {}
        record_data = getattr(item, "record_data", None)
        if isinstance(record_data, dict):
            masked_rd = mask_clinical_phi(record_data)
            if masked_rd != record_data:
                updates["record_data"] = masked_rd
        for field_name in model_fields:
            if field_name == "record_data" or global_sensitivity_registry.is_exempt(field_name):
                continue
            if global_sensitivity_registry.is_sensitive(field_name):
                val = getattr(item, field_name, None)
                if val is not None and val != "***MASKED***":
                    updates[field_name] = "***MASKED***"
        return item.model_copy(update=updates) if updates else item

    record_data = getattr(item, "record_data", None)
    if isinstance(record_data, dict):
        masked_rd = mask_clinical_phi(record_data)
        if masked_rd != record_data:
            copied = copy.copy(item)
            copied.record_data = masked_rd
            return copied
    return item


def redact_batch_records(records: Any, *, mask_phi: bool = True) -> Any:
    """Apply default SensitivityRegistry PHI redaction across a batch of records or DataFrame."""
    if not mask_phi or records is None:
        return records

    pd_mod = sys.modules.get("pandas")
    if pd_mod is not None and isinstance(records, pd_mod.DataFrame):
        return _redact_dataframe_batch(records, pd_mod)

    if isinstance(records, Mapping):
        return mask_clinical_phi(dict(records))
    if isinstance(records, (str, bytes)):
        return records
    if isinstance(records, tuple):
        return tuple(_redact_single_record(item) for item in records)
    if isinstance(records, Iterable):
        return [_redact_single_record(item) for item in records]
    return records


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

    _skip_auto_redact_batch: bool = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Automatically wrap concrete ``write_batch`` implementations with PHI redaction."""
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("_skip_auto_redact_batch", False):
            return
        raw_write_batch = cls.__dict__.get("write_batch")
        if raw_write_batch is None or getattr(raw_write_batch, "__isabstractmethod__", False):
            return
        if getattr(raw_write_batch, "_phi_redaction_wrapped", False):
            return

        @functools.wraps(raw_write_batch)
        def _wrapped_write_batch(
            self: ExportSink,
            records: Sequence[Any],
            *,
            batch_id: str,
        ) -> int:
            redacted = self.redact_batch(records)
            return cast(int, raw_write_batch(self, redacted, batch_id=batch_id))

        _wrapped_write_batch._phi_redaction_wrapped = True  # type: ignore[attr-defined]
        cls.write_batch = _wrapped_write_batch  # type: ignore[method-assign]

    def __init__(self, config: SinkConfig) -> None:
        """Initialize the export sink with a configuration."""
        self.config: SinkConfig = config

    def redact_batch(self, records: Sequence[Any]) -> Any:
        """Redact sensitive clinical PHI across ``records`` when ``config.mask_phi`` is enabled."""
        should_mask = getattr(self.config, "mask_phi", True) is not False
        return redact_batch_records(records, mask_phi=should_mask)

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

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Reconcile newly voided records in idempotent downstream targets.

        Parameters
        ----------
        records:
            Sequence of newly voided (``deleted=True``) records observed during
            an incremental or watermarked export.
        batch_id:
            Idempotency key for error reporting and telemetry.

        Returns:
        --------
        int
            Number of voided records reconciled in the destination.
        """
        _ = (records, batch_id)
        return 0

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

    from imednet.core.context import reset_study_context, set_study_context
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

        ctx_token = set_study_context(study_key)
        try:
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
        finally:
            reset_study_context(ctx_token)

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


def iter_batches(records: Iterable[Any], batch_size: int) -> Iterator[list[Any]]:
    """Yield ``records`` lazily in fixed-size batches of ``batch_size``."""
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0")

    def _generate() -> Iterator[list[Any]]:
        iterator = iter(records)
        while True:
            batch = list(itertools.islice(iterator, batch_size))
            if not batch:
                break
            yield batch

    return _generate()


def is_record_deleted(record: Any) -> bool:
    """Return True strictly when a record carries an explicit ``deleted=True`` flag."""
    if isinstance(record, Mapping):
        return record.get("deleted") is True
    return getattr(record, "deleted", False) is True


def extract_record_id(record: Any) -> Any | None:
    """Extract ``record_id`` / ``recordId`` from a record object or mapping."""
    if isinstance(record, Mapping):
        rec_id = record.get("record_id")
        return rec_id if rec_id is not None else record.get("recordId")
    rec_id = getattr(record, "record_id", None)
    if rec_id is None:
        rec_id = getattr(record, "recordId", None)
    return rec_id


def extract_record_timestamp(record: Any) -> datetime | None:
    """Extract a normalized UTC modification timestamp from a record."""
    for attr in ("date_modified", "dateModified", "last_updated", "lastUpdated"):
        raw = record.get(attr) if isinstance(record, Mapping) else getattr(record, attr, None)
        if isinstance(raw, (datetime, str)):
            try:
                ts = normalize_watermark_datetime(raw)
            except ValueError:
                continue
            if ts is not None:
                return ts
    return None


def _split_top_level(expr: str, sep: str) -> list[str]:
    """Split a filter expression by ``sep`` outside double quotes."""
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    escape = False
    for ch in expr:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if ch == "\\" and in_quotes:
            buf.append(ch)
            escape = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            continue
        if ch == sep and not in_quotes:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _merge_and_condition(target: dict[str, Any], key: str, condition: Any) -> None:
    """Merge an AND filter condition onto ``target`` without losing prior bounds."""
    from imednet.utils.filters import _snake_to_camel

    camel_key = _snake_to_camel(key)
    existing_key = key if key in target else (camel_key if camel_key in target else key)
    if existing_key not in target:
        target[existing_key] = condition
        return

    prev = target[existing_key]
    if isinstance(prev, list):
        combined = list(prev)
    elif isinstance(prev, tuple):
        combined = [prev]
    else:
        combined = [("==", prev)]

    combined.append(condition if isinstance(condition, tuple) else ("==", condition))
    target[existing_key] = combined


def _expand_scalar_list_branches(base: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand scalar list/set values into separate AND-safe filter dicts."""
    list_keys: list[str] = []
    list_values: list[list[Any]] = []
    fixed: dict[str, Any] = {}

    for k, v in base.items():
        if isinstance(v, set):
            vals = list(v)
            if not vals:
                return []
            list_keys.append(k)
            list_values.append(vals)
        elif isinstance(v, list) and (not v or not any(isinstance(item, tuple) for item in v)):
            if not v:
                return []
            list_keys.append(k)
            list_values.append(list(dict.fromkeys(v)))
        else:
            fixed[k] = v

    if not list_keys:
        return [fixed]

    branches: list[dict[str, Any]] = []
    for combo in itertools.product(*list_values):
        branch = dict(fixed)
        for k, scalar in zip(list_keys, combo, strict=True):
            branch[k] = scalar
        branches.append(branch)
    return branches


def build_record_query_branches(
    *,
    record_filter: dict[str, Any] | str | Sequence[str] | None = None,
    watermark: datetime | str | None = None,
    exclude_deleted_upstream: bool = True,
) -> list[dict[str, Any]]:
    """Build AND-only filter dictionaries for ``sdk.records.list``.

    Ensures OR filter clauses (comma-separated strings or scalar lists) are
    split into separate query branches so ``;`` (AND) and ``,`` (OR) connectors
    are never mixed in a single upstream ``filter`` parameter.
    """
    base_branches: list[dict[str, Any]] = [{}]

    if isinstance(record_filter, str):
        stripped = record_filter.strip()
        if stripped and stripped.lower() != "none":
            validate_connector_exclusivity(stripped)
            or_parts = _split_top_level(stripped, ",")
            if len(or_parts) > 1:
                base_branches = [parse_filter_expressions([part]) for part in or_parts]
            else:
                and_parts = _split_top_level(stripped, ";")
                base_branches = [parse_filter_expressions(and_parts)]
    elif isinstance(record_filter, Mapping):
        raw_dict = dict(record_filter)
        inline_filter = raw_dict.pop("filter", None)
        expanded = _expand_scalar_list_branches(raw_dict)
        if not expanded:
            return []
        if isinstance(inline_filter, str) and inline_filter.strip():
            inline_branches = build_record_query_branches(
                record_filter=inline_filter,
                watermark=None,
                exclude_deleted_upstream=False,
            )
            merged_branches: list[dict[str, Any]] = []
            for left in expanded:
                for right in inline_branches:
                    combined = dict(left)
                    for rk, rv in right.items():
                        _merge_and_condition(combined, rk, rv)
                    merged_branches.append(combined)
            base_branches = merged_branches
        else:
            base_branches = expanded
    elif isinstance(record_filter, Sequence):
        exprs = [str(item).strip() for item in record_filter if str(item).strip()]
        if exprs:
            for expr in exprs:
                validate_connector_exclusivity(expr)
            if len(exprs) == 1 and len(_split_top_level(exprs[0], ",")) > 1:
                base_branches = [
                    parse_filter_expressions([p]) for p in _split_top_level(exprs[0], ",")
                ]
            else:
                flat_and: list[str] = []
                for expr in exprs:
                    if len(_split_top_level(expr, ",")) > 1:
                        raise ValueError(
                            "Filter expression cannot combine both ';' (AND) and ',' (OR) connectors"
                        )
                    flat_and.extend(_split_top_level(expr, ";"))
                base_branches = [parse_filter_expressions(flat_and)]

    norm_wm = normalize_watermark_datetime(watermark)
    wm_iso = format_iso_datetime(norm_wm) if norm_wm is not None else None

    final_branches: list[dict[str, Any]] = []
    for branch in base_branches:
        b = dict(branch)
        if wm_iso is not None:
            _merge_and_condition(b, "dateModified", (">=", wm_iso))
        if exclude_deleted_upstream and "deleted" not in b:
            b["deleted"] = False
        final_branches.append(b)
    return final_branches


def iter_filtered_sdk_records(
    sdk: Any,
    study_key: str,
    *,
    watermark: datetime | str | None = None,
    record_filter: dict[str, Any] | str | Sequence[str] | None = None,
    record_data_filter: str | None = None,
    exclude_deleted_upstream: bool = True,
) -> Iterator[Any]:
    """Lazily yield records from ``sdk.records.list`` across one or more AND-safe branches."""
    if isinstance(record_data_filter, str) and record_data_filter.strip():
        validate_connector_exclusivity(record_data_filter)

    branches = build_record_query_branches(
        record_filter=record_filter,
        watermark=watermark,
        exclude_deleted_upstream=exclude_deleted_upstream,
    )
    if not branches:
        return

    deduplicate = len(branches) > 1
    seen_ids: set[Any] = set()

    for branch in branches:
        page_iter = sdk.records.list(
            study_key=study_key,
            record_data_filter=record_data_filter,
            **branch,
        )
        for record in page_iter:
            if deduplicate:
                rec_id = extract_record_id(record)
                if rec_id is not None:
                    if rec_id in seen_ids:
                        continue
                    seen_ids.add(rec_id)
            yield record


def _extract_safe_config_options(config: Any) -> dict[str, Any]:
    """Extract streaming and filter options safely from SinkConfig or mock objects."""
    is_real_cfg = isinstance(config, SinkConfig)
    incremental = getattr(config, "incremental", False) is True
    include_deleted = getattr(config, "include_deleted", False) is True
    idempotent = getattr(config, "idempotent", True) is not False

    raw_batch_size = getattr(config, "batch_size", _DEFAULT_BATCH_SIZE)
    batch_size = (
        raw_batch_size
        if isinstance(raw_batch_size, int) and not isinstance(raw_batch_size, bool)
        else _DEFAULT_BATCH_SIZE
    )

    raw_since = config.modified_since if is_real_cfg else getattr(config, "modified_since", None)
    modified_since = (
        normalize_watermark_datetime(raw_since) if isinstance(raw_since, (datetime, str)) else None
    )

    raw_rec_filter = config.record_filter if is_real_cfg else getattr(config, "record_filter", None)
    record_filter = (
        raw_rec_filter if isinstance(raw_rec_filter, (Mapping, str, list, tuple)) else None
    )

    raw_data_filter = (
        config.record_data_filter if is_real_cfg else getattr(config, "record_data_filter", None)
    )
    record_data_filter = raw_data_filter if isinstance(raw_data_filter, str) else None

    raw_stream = config.stream_name if is_real_cfg else getattr(config, "stream_name", None)
    cfg_stream_name = raw_stream if isinstance(raw_stream, str) and raw_stream.strip() else None

    raw_ledger = config.ledger_path if is_real_cfg else getattr(config, "ledger_path", None)
    ledger_path = raw_ledger if isinstance(raw_ledger, (str, Path)) else None

    explicit_provider = (
        config.state_provider
        if is_real_cfg
        else (
            getattr(config, "state_provider", None)
            if incremental and hasattr(getattr(config, "state_provider", None), "transaction")
            else None
        )
    )

    return {
        "incremental": incremental,
        "include_deleted": include_deleted,
        "idempotent": idempotent,
        "batch_size": batch_size,
        "modified_since": modified_since,
        "record_filter": record_filter,
        "record_data_filter": record_data_filter,
        "stream_name": cfg_stream_name,
        "ledger_path": ledger_path,
        "state_provider": explicit_provider,
    }


def run_streaming_export(
    sdk: Any,
    study_key: str,
    sink: ExportSink | Callable[[], ExportSink],
    config: SinkConfig,
    *,
    stream_name: str | None = None,
    batch_id_prefix: str | None = None,
    default_provider_name: str | None = None,
    records_source: Callable[[datetime | None, bool], Iterable[Any]] | None = None,
    quality_gate_fn: Callable[[Any, str, Iterable[Any], SinkConfig], Iterable[Any]] | None = None,
    iter_batches_fn: Callable[[Iterable[Any], int], Iterable[Sequence[Any]]] | None = None,
) -> int:
    """Stream paginated records into ``sink`` in fixed-size batches with checkpointing.

    Commits ledger checkpoints only after all batches and ``sink.flush()`` succeed.
    """
    opts = _extract_safe_config_options(config)
    use_checkpoint = opts["incremental"] or opts["state_provider"] is not None
    gate_fn = quality_gate_fn or apply_quality_gate
    batches_fn = iter_batches_fn or iter_batches
    prefix = batch_id_prefix or f"{study_key}/records"
    resolved_stream = opts["stream_name"] or stream_name or "records"

    provider: StateProviderProtocol | None = None
    if use_checkpoint:
        provider = (
            cast(StateProviderProtocol, opts["state_provider"])
            if opts["state_provider"] is not None
            else get_state_provider(
                ledger_path=opts["ledger_path"],
                provider_name=default_provider_name,
            )
        )

    total_written = 0
    total_reconciled = 0
    max_ts: datetime | None = None

    with contextlib.ExitStack() as stack:
        tx: dict[str, Any] | None = None
        if provider is not None:
            tx = stack.enter_context(
                provider.transaction(
                    study_key,
                    resolved_stream,
                    fallback_timestamp=opts["modified_since"],
                )
            )
            watermark = (
                normalize_watermark_datetime(tx.get("last_timestamp")) or opts["modified_since"]
            )
        else:
            watermark = opts["modified_since"]

        if isinstance(sink, ExportSink) or hasattr(sink, "write_batch"):
            sink_obj = cast(ExportSink, sink)
        else:
            sink_obj = sink()
        stack.enter_context(sink_obj)

        reconcile_voided = (
            (not opts["include_deleted"])
            and opts["idempotent"]
            and (use_checkpoint or watermark is not None)
        )
        exclude_deleted_upstream = (not opts["include_deleted"]) and (not reconcile_voided)

        if records_source is not None:
            raw_records = records_source(watermark, exclude_deleted_upstream)
        else:
            raw_records = iter_filtered_sdk_records(
                sdk,
                study_key,
                watermark=watermark,
                record_filter=opts["record_filter"],
                record_data_filter=opts["record_data_filter"],
                exclude_deleted_upstream=exclude_deleted_upstream,
            )

        pending_voided: list[Any] = []
        voided_batch_idx = 0

        def _observe_and_route() -> Iterator[Any]:
            nonlocal max_ts, total_reconciled, voided_batch_idx
            for rec in raw_records:
                rec_ts = extract_record_timestamp(rec)
                if rec_ts is not None and (max_ts is None or rec_ts > max_ts):
                    max_ts = rec_ts

                if not opts["include_deleted"] and is_record_deleted(rec):
                    if reconcile_voided:
                        pending_voided.append(rec)
                        if len(pending_voided) >= opts["batch_size"]:
                            total_reconciled += sink_obj.reconcile_deleted(
                                list(pending_voided),
                                batch_id=f"{prefix}/voided/{voided_batch_idx}",
                            )
                            pending_voided.clear()
                            voided_batch_idx += 1
                    continue

                yield rec

        filtered_records = gate_fn(sdk, study_key, _observe_and_route(), config)
        for index, batch in enumerate(batches_fn(filtered_records, opts["batch_size"])):
            total_written += sink_obj.write_batch(batch, batch_id=f"{prefix}/{index}")

        if pending_voided:
            total_reconciled += sink_obj.reconcile_deleted(
                list(pending_voided),
                batch_id=f"{prefix}/voided/{voided_batch_idx}",
            )
            pending_voided.clear()

        if tx is not None:
            tx["records_processed"] = total_written + total_reconciled
            if max_ts is not None:
                tx["new_timestamp"] = max_ts

    return total_written


__all__ = [
    "STATE_PROVIDER_ENTRY_POINT_GROUP",
    "ExportSink",
    "SinkConfig",
    "StateProviderProtocol",
    "_redact_uri",
    "_require_optional_dep",
    "apply_quality_gate",
    "build_record_query_branches",
    "extract_record_id",
    "extract_record_timestamp",
    "get_state_provider",
    "is_record_deleted",
    "iter_batches",
    "iter_filtered_sdk_records",
    "normalize_watermark_datetime",
    "redact_batch_records",
    "run_streaming_export",
]
