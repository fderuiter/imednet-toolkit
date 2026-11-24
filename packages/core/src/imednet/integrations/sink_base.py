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
from enum import Enum
from importlib import import_module
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, cast, runtime_checkable
from unittest.mock import NonCallableMock

from tenacity import wait_exponential

from imednet.core.operations.circuit_breaker import CircuitBreaker
from imednet.core.operations.executor import OperationRetryPolicy
from imednet.errors import ExportConfigurationError, ExportError
from imednet.models.standards import (
    PROFILE_REGISTRY,
    SDTM_DOMAIN_SPECS,
    StandardsProfile,
    StandardsReadinessReport,
    StandardsReadinessValidator,
    format_iso8601_dtc,
    normalize_naming_convention,
    resolve_sdtm_domain,
    resolve_standards_profile,
    validate_cdash_variable,
)
from imednet.utils.dates import (
    format_iso_datetime,
    normalize_utc_timestamp_bound,
    parse_iso_datetime,
)
from imednet.utils.filters import (
    _parse_clause_operator,
    _snake_to_camel,
    _split_filter_clauses,
    parse_filter_expressions,
    validate_connector_exclusivity,
    validate_filter_key,
    validate_filter_operator,
    validate_record_filter,
)
from imednet.utils.security import (
    SensitivityRegistry,
    _normalize_key,
    global_sensitivity_registry,
    mask_clinical_phi,
)
from imednet.utils.tabular import MASKED_SENTINEL
from imednet.utils.url import redact_url_query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sink configuration & checkpoint provider SPI
# ---------------------------------------------------------------------------

_DEFAULT_BATCH_SIZE = 500
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_BACKOFF = 1.0
DEFAULT_MAX_BUFFERED_RECORDS = 5_000
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
        timestamp: datetime | None,
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

    def batch_transaction(
        self,
        study_key: str,
        stream_names: Sequence[str],
        fallback_timestamp: datetime | None = None,
    ) -> AbstractContextManager[dict[str, dict[str, Any]]]:
        """Execute a batched checkpoint transaction across multiple streams in one study."""
        ...

    def read_state(self) -> Any:
        """Read full ledger state."""
        ...

    def delete_entry(self, study_key: str, stream_name: str | None = None) -> bool:
        """Delete ledger state entry."""
        ...

    def get_sdtm_lineage(self, study_key: str, stream_name: str) -> dict[str, Any]:
        """Retrieve persisted SDTM sidecar lineage and monotonic sequence watermarks."""
        ...

    def record_sdtm_lineage(
        self,
        study_key: str,
        stream_name: str,
        lineage_updates: Mapping[str, dict[str, Any] | None] | None = None,
        seq_watermarks: Mapping[str, int] | None = None,
    ) -> None:
        """Persist SDTM sidecar lineage and monotonic sequence watermarks."""
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
    variable_prefix: str | None = None,
) -> StateProviderProtocol:
    """Discover and instantiate a pluggable checkpoint state provider at runtime."""
    from importlib.metadata import entry_points

    if ledger_path is not None:
        target_name = "file" if provider_name in ("airflow", "file") else "default"
    else:
        target_name = provider_name or "default"

    eps = list(entry_points(group=STATE_PROVIDER_ENTRY_POINT_GROUP))
    matched = [ep for ep in eps if ep.name == target_name]
    if not matched and ledger_path is not None:
        matched = [ep for ep in eps if ep.name in ("file", "default")]
    if not matched and provider_name is None and eps:
        matched = [eps[0]]
    if not matched:
        raise ImportError(
            f"Checkpoint state provider '{target_name}' requires an installed workflows plugin. "
            "Install 'imednet-workflows' or 'imednet[export]'."
        )

    ep = matched[0]
    factory = ep.load()
    if ledger_path is not None:
        if ep.name == "default":
            provider = factory(ledger_path=ledger_path, provider_name="file")
        else:
            provider = factory(ledger_path)
    elif ep.name == "airflow":
        provider = (
            factory(variable_prefix=variable_prefix) if variable_prefix is not None else factory()
        )
    elif ep.name == "default" and (provider_name is not None or variable_prefix is not None):
        provider = factory(
            provider_name=provider_name,
            variable_prefix=variable_prefix,
        )
    else:
        provider = factory()
    return cast(StateProviderProtocol, provider)


class _SinkRetryPolicy(OperationRetryPolicy):
    """Keep deterministic configuration and input failures out of retries."""

    def should_retry(self, exception: Exception) -> bool:
        """Retry destination failures, preserving the existing driver fallback."""
        return not isinstance(exception, (ExportConfigurationError, ValueError, TypeError))


def validate_max_buffered_records(value: int | None) -> int | None:
    """Validate an optional global buffered-record ceiling."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("max_buffered_records must be a positive integer")
    return value


def resolve_max_buffered_records(batch_size: int, max_buffered_records: int | None) -> int:
    """Resolve the effective global buffered-record ceiling across per-form buffers."""
    validated = validate_max_buffered_records(max_buffered_records)
    if validated is not None:
        return validated
    return max(batch_size, DEFAULT_MAX_BUFFERED_RECORDS)


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
    max_buffered_records:
        Optional global upper bound on records buffered across per-form streams.
    as_sdtm:
        When ``True``, transforms exported records into CDISC SDTM domain rows.
    sdtm_domain:
        Optional target 2-character CDISC SDTM domain code (e.g. ``"AE"``, ``"VS"``).
    standards_profile:
        Optional standards validation profile name (e.g. ``"drug"``, ``"device"``,
        or ``"general"``) or :class:`~imednet.models.standards.StandardsProfile`.
    min_standards_score:
        Optional minimum CDISC standards readiness score ``[0.0, 100.0]`` required
        for exports to proceed.
    naming_convention:
        Optional raw variable/metadata naming convention (``"snake_case"``,
        ``"camelCase"``, or ``"sdtm"``).
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
    max_buffered_records: int | None = None
    as_sdtm: bool = False
    sdtm_domain: str | None = None
    standards_profile: str | Any | None = None
    min_standards_score: float | None = None
    naming_convention: str | None = None

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
        if not self.idempotent and (self.incremental or self.state_provider is not None):
            raise ExportConfigurationError(
                "SinkConfig(idempotent=False) cannot be combined with incremental=True or "
                "state_provider because inclusive dateModified>= watermarks require "
                "idempotent=True."
            )
        validate_max_buffered_records(self.max_buffered_records)
        if not isinstance(self.as_sdtm, bool):
            raise ValueError("as_sdtm must be a boolean")
        if self.sdtm_domain is not None:
            if not isinstance(self.sdtm_domain, str) or not self.sdtm_domain.strip():
                raise ValueError("sdtm_domain must be a non-empty string when provided")
            self.sdtm_domain = resolve_sdtm_domain(self.sdtm_domain)
            self.as_sdtm = True
        if self.standards_profile is not None:
            try:
                resolved_prof = resolve_standards_profile(self.standards_profile)
            except KeyError as exc:
                available = ", ".join(f"'{p}'" for p in PROFILE_REGISTRY.list_profiles())
                raise ValueError(
                    f"Unknown standards_profile '{self.standards_profile}'. "
                    f"Expected {available}, or a StandardsProfile instance."
                ) from exc
            if isinstance(self.standards_profile, str):
                self.standards_profile = resolved_prof.profile_name
        if self.min_standards_score is not None:
            if (
                isinstance(self.min_standards_score, bool)
                or not isinstance(self.min_standards_score, (int, float))
                or not math.isfinite(self.min_standards_score)
                or self.min_standards_score < 0.0
                or self.min_standards_score > 100.0
            ):
                raise ValueError("min_standards_score must be a finite number between 0 and 100")
            self.min_standards_score = float(self.min_standards_score)
        if self.naming_convention is not None:
            self.naming_convention = normalize_naming_convention(self.naming_convention)
        if self.modified_since is not None:
            normalize_watermark_datetime(self.modified_since)
        if self.record_data_filter is not None:
            _validate_record_data_filter_input(self.record_data_filter)
        if self.record_filter is not None:
            if isinstance(self.record_filter, str) and not self.record_filter.strip():
                raise ValueError("record_filter cannot be empty")
            build_record_query_branches(
                record_filter=self.record_filter,
                watermark=self.modified_since,
                exclude_deleted_upstream=not self.include_deleted,
            )


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


_SDTM_BIRTH_SOURCE_KEYS: frozenset[str] = frozenset(
    {
        "brthdtc",
        "brthdat",
        "birthdate",
        "dateofbirth",
        "dob",
    }
)

_TABULAR_BATCH_STAMP_ATTR = "_imednet_tabular_batch_stamp"
_CONTAINER_CELL_TYPES = (dict, list, tuple)


@dataclass(frozen=True, slots=True)
class TabularBatchStamp:
    """Immutable preparation stamp bound to a prepared pandas DataFrame batch."""

    mask_phi: bool
    registry_snapshot: tuple[frozenset[str], frozenset[str]]
    phi_keys: frozenset[str]
    columns: tuple[Any, ...]
    sanitized: bool = False


def normalize_phi_keys_snapshot(
    phi_keys: Iterable[str] | None,
    *,
    registry: SensitivityRegistry | None = None,
) -> frozenset[str]:
    """Normalize custom StudyConfiguration PHI keys into a canonical collapsed frozenset."""
    if not phi_keys:
        return frozenset()
    active_registry = global_sensitivity_registry if registry is None else registry
    normalized: set[str] = set()
    for raw_key in phi_keys:
        if raw_key is None or active_registry.is_exempt(raw_key):
            continue
        norm = _normalize_key(raw_key)
        if norm:
            collapsed = norm.replace("_", "")
            if collapsed:
                normalized.add(collapsed)
    return frozenset(normalized)


def stamp_tabular_batch(
    df: Any,
    *,
    mask_phi: bool,
    phi_keys: Iterable[str] | None = None,
    sanitized: bool = False,
    registry: SensitivityRegistry | None = None,
) -> Any:
    """Attach a :class:`TabularBatchStamp` to ``df`` binding active PHI and header state."""
    columns = getattr(df, "columns", None)
    obj_dict = getattr(df, "__dict__", None)
    if not isinstance(obj_dict, dict) or columns is None:
        return df
    active_registry = global_sensitivity_registry if registry is None else registry
    obj_dict[_TABULAR_BATCH_STAMP_ATTR] = TabularBatchStamp(
        mask_phi=bool(mask_phi),
        registry_snapshot=active_registry.snapshot(),
        phi_keys=normalize_phi_keys_snapshot(phi_keys, registry=active_registry)
        if mask_phi
        else frozenset(),
        columns=tuple(columns),
        sanitized=bool(sanitized),
    )
    return df


def get_tabular_batch_stamp(df: Any) -> TabularBatchStamp | None:
    """Return ``df``'s :class:`TabularBatchStamp` when present and matching ``df.columns``."""
    columns = getattr(df, "columns", None)
    obj_dict = getattr(df, "__dict__", None)
    if not isinstance(obj_dict, dict) or columns is None:
        return None
    stamp = obj_dict.get(_TABULAR_BATCH_STAMP_ATTR)
    if not isinstance(stamp, TabularBatchStamp):
        return None
    if tuple(columns) != stamp.columns:
        return None
    return stamp


def has_valid_tabular_batch_stamp(
    df: Any,
    *,
    mask_phi: bool = True,
    phi_keys: Iterable[str] | None = None,
    require_sanitized: bool = False,
    registry: SensitivityRegistry | None = None,
) -> bool:
    """Verify whether ``df`` carries a valid preparation stamp for the requested requirements."""
    stamp = get_tabular_batch_stamp(df)
    if stamp is None:
        return False
    if mask_phi:
        if not stamp.mask_phi:
            return False
        active_registry = global_sensitivity_registry if registry is None else registry
        if stamp.registry_snapshot != active_registry.snapshot():
            return False
        required_phi = normalize_phi_keys_snapshot(phi_keys, registry=active_registry)
        if not required_phi.issubset(stamp.phi_keys):
            return False
    return not (require_sanitized and not stamp.sanitized)


def _mask_series_containers_default_phi(series: Any) -> Any:
    """Mask nested container cells in a pandas Series without invoking scalar mask_clinical_phi."""
    values = series.tolist()
    if not any(isinstance(v, _CONTAINER_CELL_TYPES) for v in values):
        return series
    pd_mod = sys.modules.get("pandas")
    new_vals = [mask_clinical_phi(v) if isinstance(v, _CONTAINER_CELL_TYPES) else v for v in values]
    if pd_mod is not None:
        return pd_mod.Series(new_vals, index=series.index, dtype=object)
    return series.apply(
        lambda v: mask_clinical_phi(v) if isinstance(v, _CONTAINER_CELL_TYPES) else v
    )


def _is_sdtm_birth_date_key(raw_key: str) -> bool:
    """Return True if *raw_key* represents an SDTM birth-date variable or source alias."""
    norm = _normalize_key(raw_key)
    if not norm:
        return False
    return norm in _SDTM_BIRTH_SOURCE_KEYS or norm.endswith("brthdtc")


def _redact_mapping_for_sdtm(mapping_val: Mapping[Any, Any]) -> dict[Any, Any]:
    """Redact PHI across a mapping while truncating birth-date fields to ISO year or None."""
    out: dict[Any, Any] = {}
    for k, v in mapping_val.items():
        key_str = str(k)
        if _is_sdtm_birth_date_key(key_str) and not global_sensitivity_registry.is_exempt(key_str):
            out[k] = format_iso8601_dtc(v, mask_phi=True)
        elif global_sensitivity_registry.is_sensitive(key_str):
            out[k] = MASKED_SENTINEL
        elif isinstance(v, Mapping):
            out[k] = _redact_mapping_for_sdtm(v)
        elif isinstance(v, list):
            out[k] = [
                _redact_mapping_for_sdtm(elem)
                if isinstance(elem, Mapping)
                else mask_clinical_phi(elem)
                for elem in v
            ]
        elif isinstance(v, tuple):
            out[k] = tuple(
                _redact_mapping_for_sdtm(elem)
                if isinstance(elem, Mapping)
                else mask_clinical_phi(elem)
                for elem in v
            )
        else:
            out[k] = mask_clinical_phi(v)
    return out


def _redact_dataframe_batch(df: Any, pd_mod: Any, *, as_sdtm: bool = False) -> Any:
    """Return a PHI-redacted copy of a pandas DataFrame without mutating caller state."""
    if getattr(df, "attrs", {}).get("imednet_sdtm_redacted") is True:
        return df

    masked = df.copy()
    birth_cols = (
        [
            col
            for col in masked.columns
            if _is_sdtm_birth_date_key(str(col))
            and not global_sensitivity_registry.is_exempt(str(col))
        ]
        if as_sdtm
        else []
    )
    birth_col_set = set(birth_cols)
    for col in birth_cols:
        if len(masked) > 0:
            masked[col] = masked[col].map(lambda val: format_iso8601_dtc(val, mask_phi=True))

    sensitive_cols = list(
        dict.fromkeys(
            col
            for col in masked.columns
            if col not in birth_col_set and global_sensitivity_registry.is_sensitive(str(col))
        )
    )
    for col in sensitive_cols:
        if len(masked) > 0:
            masked[col] = MASKED_SENTINEL

    if len(masked) == 0:
        if as_sdtm:
            masked.attrs["imednet_sdtm_redacted"] = True
        return masked

    remaining = masked.drop(columns=[*sensitive_cols, *birth_cols], errors="ignore")
    try:
        object_cols = list(dict.fromkeys(remaining.select_dtypes(include=[object, "str"]).columns))
    except TypeError:
        object_cols = list(dict.fromkeys(remaining.select_dtypes(include=[object]).columns))

    _ = pd_mod
    for col in object_cols:
        target = masked[col]
        if hasattr(target, "columns"):
            masked[col] = target.apply(_mask_series_containers_default_phi)
        else:
            masked[col] = _mask_series_containers_default_phi(target)
    if as_sdtm:
        masked.attrs["imednet_sdtm_redacted"] = True
    return masked


def _redact_single_record(item: Any, *, as_sdtm: bool = False) -> Any:
    """Redact PHI on a single Mapping, Pydantic model, or record-like object."""
    if isinstance(item, Mapping):
        if as_sdtm:
            return _redact_mapping_for_sdtm(item)
        return mask_clinical_phi(dict(item))
    if isinstance(item, NonCallableMock):
        return item

    model_copy = getattr(type(item), "model_copy", None)
    model_fields = getattr(type(item), "model_fields", None)
    if callable(model_copy) and isinstance(model_fields, Mapping):
        updates: dict[str, Any] = {}
        record_data = getattr(item, "record_data", None)
        if isinstance(record_data, dict):
            masked_rd = (
                _redact_mapping_for_sdtm(record_data) if as_sdtm else mask_clinical_phi(record_data)
            )
            if masked_rd != record_data:
                updates["record_data"] = masked_rd
        for field_name in model_fields:
            if field_name == "record_data" or global_sensitivity_registry.is_exempt(field_name):
                continue
            if as_sdtm and _is_sdtm_birth_date_key(field_name):
                val = getattr(item, field_name, None)
                redacted_year = format_iso8601_dtc(val, mask_phi=True)
                if redacted_year != val:
                    updates[field_name] = redacted_year
            elif global_sensitivity_registry.is_sensitive(field_name):
                val = getattr(item, field_name, None)
                if val is not None and val != MASKED_SENTINEL:
                    updates[field_name] = MASKED_SENTINEL
        return item.model_copy(update=updates) if updates else item

    record_data = getattr(item, "record_data", None)
    if isinstance(record_data, dict):
        masked_rd = (
            _redact_mapping_for_sdtm(record_data) if as_sdtm else mask_clinical_phi(record_data)
        )
        if masked_rd != record_data:
            copied = copy.copy(item)
            copied.record_data = masked_rd
            return copied
    return item


def redact_batch_records(
    records: Any,
    *,
    mask_phi: bool = True,
    as_sdtm: bool = False,
) -> Any:
    """Apply default SensitivityRegistry PHI redaction across a batch of records or DataFrame."""
    if not mask_phi or records is None:
        return records

    pd_mod = sys.modules.get("pandas")
    if pd_mod is not None and isinstance(records, pd_mod.DataFrame):
        return _redact_dataframe_batch(records, pd_mod, as_sdtm=as_sdtm)

    if isinstance(records, Mapping):
        if as_sdtm:
            return _redact_mapping_for_sdtm(records)
        return mask_clinical_phi(dict(records))
    if isinstance(records, (str, bytes)):
        return records
    if isinstance(records, tuple):
        return tuple(_redact_single_record(item, as_sdtm=as_sdtm) for item in records)
    if isinstance(records, Iterable):
        return [_redact_single_record(item, as_sdtm=as_sdtm) for item in records]
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
    _delegates_sdtm_redaction: bool = False

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
        self._circuit_breaker: CircuitBreaker = CircuitBreaker()
        self._sdtm_lineage: dict[str, dict[str, Any]] = {}
        self._sdtm_seq_watermarks: dict[str, int] = {}

    reconciles_deleted_on_full_sync: bool = False

    def bind_stream_metadata(self, metadata: dict[str, Any]) -> None:
        """Bind mutable checkpoint metadata for the active streaming run."""
        _ = metadata

    def _get_circuit_breaker(self) -> CircuitBreaker:
        """Return this sink's isolated circuit breaker, initializing lazily if needed."""
        breaker = getattr(self, "_circuit_breaker", None)
        if breaker is None:
            breaker = CircuitBreaker()
            self._circuit_breaker = breaker
        return breaker

    def redact_batch(self, records: Sequence[Any]) -> Any:
        """Redact sensitive clinical PHI across ``records`` when ``config.mask_phi`` is enabled."""
        should_mask = getattr(self.config, "mask_phi", True) is not False
        raw_domain = getattr(self.config, "sdtm_domain", None)
        is_sdtm = getattr(self.config, "as_sdtm", False) is True or (
            isinstance(raw_domain, str) and bool(raw_domain.strip())
        )
        if is_sdtm and getattr(self, "_delegates_sdtm_redaction", False):
            return records
        return redact_batch_records(records, mask_phi=should_mask, as_sdtm=is_sdtm)

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
        last_raw_exc: Exception | None = None

        def execute_batch() -> int:
            nonlocal attempts, last_raw_exc
            attempts += 1
            # Driver messages may contain credentials, URIs or payloads. Only
            # sanitized exceptions may cross the executor/telemetry boundary.
            try:
                return execute_fn()
            except ExportConfigurationError as exc:
                last_raw_exc = exc
                raise ExportConfigurationError("Invalid export configuration.") from None
            except TypeError as exc:
                last_raw_exc = exc
                raise TypeError("Invalid export batch input.") from None
            except ValueError as exc:
                last_raw_exc = exc
                raise ValueError("Invalid export batch input.") from None
            except Exception as exc:
                last_raw_exc = exc
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
            circuit_breaker=self._get_circuit_breaker(),
            batch_id=batch_id,
        )
        try:
            return executor.execute(execute_batch)
        except Exception as exec_err:
            root_exc = last_raw_exc if last_raw_exc is not None else exec_err
            sanitized_cause = _sanitize_batch_cause(root_exc)
            raise ExportBatchError(
                f"Batch {batch_id!r} failed after {attempts} attempts.",
                batch_id=batch_id,
            ) from sanitized_cause

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
_URI_USERINFO_RE = re.compile(r"(://)[^@/\s\"']+@")
_URL_IN_TEXT_RE = re.compile(r'''[a-zA-Z][a-zA-Z0-9+.-]*://[^\s"'>]*[^\s"'>.,;:!?\)\]]''')
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_KEY_VALUE_PAIR_RE = re.compile(
    r"""(?P<key>"[^"\r\n]+"|'[^'\r\n]+'|\b[A-Za-z_][A-Za-z0-9_-]*)"""
    r"""(?P<sep>\s*(?::(?!//)|=)\s*)"""
    r"""(?P<val>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?![{\[])[^\s,;}\])&'"]+)"""
)
_CREDENTIAL_KEY_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "x_api_key",
        "security_key",
        "x_imn_security_key",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "oidc_token",
        "auth_token",
        "bearer_token",
        "private_key",
        "passphrase",
        "authorization",
        "credential",
        "credentials",
    }
)
_CREDENTIAL_KEY_SUFFIXES = (
    "_password",
    "_passwd",
    "_pwd",
    "_secret",
    "_token",
    "_api_key",
    "_apikey",
    "_security_key",
    "_private_key",
    "_passphrase",
)


def _redact_uri(uri: str) -> str:
    """Replace user-info in *uri* with ``***`` to prevent credential leakage.

    Examples:
        >>> _redact_uri("mongodb://user:pass@localhost:27017/db")
        'mongodb://***@localhost:27017/db'
        >>> _redact_uri("neo4j+s://bolt.example.com")
        'neo4j+s://bolt.example.com'
    """
    return _URI_USERINFO_RE.sub(r"\1***@", uri)


def _is_credential_key(raw_key: str) -> bool:
    """Return True if *raw_key* identifies a secret, password, key, or auth token."""
    norm = _normalize_key(raw_key)
    if not norm or global_sensitivity_registry.is_exempt(norm):
        return False
    return norm in _CREDENTIAL_KEY_NAMES or norm.endswith(_CREDENTIAL_KEY_SUFFIXES)


def _scrub_key_value_match(match: re.Match[str]) -> str:
    """Replace sensitive PHI or credential scalar values in matched key-value pairs."""
    key_token = match.group("key")
    sep = match.group("sep")
    val_token = match.group("val")
    unquoted_key = key_token.strip("\"'")

    if global_sensitivity_registry.is_sensitive(unquoted_key):
        replacement = MASKED_SENTINEL
    elif _is_credential_key(unquoted_key):
        replacement = "***"
    else:
        return match.group(0)

    if len(val_token) >= 2 and val_token[0] == val_token[-1] and val_token[0] in ("'", '"'):
        quote = val_token[0]
        return f"{key_token}{sep}{quote}{replacement}{quote}"
    return f"{key_token}{sep}{replacement}"


def _sanitize_text_content(text: str) -> str:
    """Scrub connection URIs, query secrets, auth tokens, and clinical PHI from *text*."""
    if not text:
        return text
    sanitized = _redact_uri(text)
    sanitized = _URL_IN_TEXT_RE.sub(lambda m: redact_url_query(m.group(0)), sanitized)
    sanitized = _AUTH_SCHEME_RE.sub(r"\1 ***", sanitized)
    sanitized = _JWT_RE.sub("***", sanitized)
    sanitized = _KEY_VALUE_PAIR_RE.sub(_scrub_key_value_match, sanitized)
    sanitized = _SSN_RE.sub(MASKED_SENTINEL, sanitized)
    return _EMAIL_RE.sub(MASKED_SENTINEL, sanitized)


def _sanitize_exception_message(exc: BaseException) -> str:
    """Extract and scrub an exception message using URI redaction and PHI masking."""
    args = getattr(exc, "args", ())
    if isinstance(args, tuple) and any(isinstance(a, (dict, list, tuple)) for a in args):
        masked_args = mask_clinical_phi(args, registry=global_sensitivity_registry)
        raw_text = str(masked_args[0]) if len(masked_args) == 1 else str(masked_args)
    else:
        try:
            raw_text = str(exc)
        except Exception:
            raw_text = ""
    return _sanitize_text_content(raw_text)


def _clear_exception_chain(exc: Exception) -> Exception:
    """Detach inner context and cause references to prevent raw frame/driver leakage."""
    exc.__cause__ = None
    exc.__context__ = None
    exc.__suppress_context__ = True
    return exc


def _sanitize_batch_cause(exc: BaseException) -> Exception:
    """Construct a sanitized cause preserving the root exception class."""
    sanitized_msg = _sanitize_exception_message(exc)
    exc_cls: type[Exception] = type(exc) if isinstance(exc, Exception) else RuntimeError

    if isinstance(exc, OSError) and isinstance(exc.errno, int):
        strerror = (
            _sanitize_text_content(exc.strerror) if isinstance(exc.strerror, str) else sanitized_msg
        )
        with contextlib.suppress(Exception):
            return _clear_exception_chain(exc_cls(exc.errno, strerror))

    with contextlib.suppress(Exception):
        inst = exc_cls(sanitized_msg)
        _ = str(inst)
        return _clear_exception_chain(inst)

    with contextlib.suppress(Exception):
        raw_inst = exc_cls.__new__(exc_cls)
        Exception.__init__(raw_inst, sanitized_msg)
        _ = str(raw_inst)
        return _clear_exception_chain(raw_inst)

    fallback_msg = f"{exc_cls.__name__}: {sanitized_msg}" if sanitized_msg else exc_cls.__name__
    return _clear_exception_chain(RuntimeError(fallback_msg))


def normalize_sink_value(value: Any, *, by_alias: bool = False) -> Any:
    """Dump Pydantic models, ``Enum`` values, and tuples into plain containers.

    Leaves scalar ``Decimal``, ``datetime``, and ``date`` instances intact so
    each destination sink can encode them without lossy float conversion.
    """
    if value is None or value.__class__ in (str, int, float, bool):
        return value
    if isinstance(value, NonCallableMock):
        return value
    if isinstance(value, Enum):
        return normalize_sink_value(value.value, by_alias=by_alias)
    if value.__class__ is dict:
        return {
            str(normalize_sink_value(k.value, by_alias=by_alias))
            if isinstance(k, Enum)
            else (k if k.__class__ is str else str(k)): normalize_sink_value(v, by_alias=by_alias)
            for k, v in value.items()
        }
    if value.__class__ in (list, tuple):
        return [normalize_sink_value(item, by_alias=by_alias) for item in value]
    model_dump = getattr(type(value), "model_dump", None)
    if callable(model_dump):
        try:
            dumped = value.model_dump(mode="json", by_alias=by_alias)
        except TypeError:
            dumped = value.model_dump()
        if dumped is not value and isinstance(
            dumped, (Mapping, list, tuple, str, int, float, bool)
        ):
            return normalize_sink_value(dumped, by_alias=by_alias)
    if isinstance(value, Mapping):
        return {
            str(normalize_sink_value(k.value, by_alias=by_alias))
            if isinstance(k, Enum)
            else str(k): normalize_sink_value(v, by_alias=by_alias)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [normalize_sink_value(item, by_alias=by_alias) for item in value]
    return value


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


_STANDARDS_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "record_id",
        "recordId",
        "form_id",
        "formId",
        "form_key",
        "formKey",
        "study_key",
        "studyKey",
        "visit_id",
        "visitId",
        "site_id",
        "siteId",
        "deleted",
        "date_modified",
        "dateModified",
        "date_created",
        "dateCreated",
    }
)


def _extract_record_for_standards(
    record: Any,
    *,
    configured_domain: str | None = None,
    form_domain_map: Mapping[Any, str] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return ``(domain, raw_data, canonical_data)`` without mutating ``record``."""
    raw_domain: str | None = None
    form_id_val: Any = None
    subject_val: Any = None
    raw_data: dict[str, Any] = {}

    if hasattr(record, "record_data") and getattr(record, "record_data", None) is not None:
        rd = getattr(record, "record_data", None)
        raw_data = dict(rd) if isinstance(rd, Mapping) else {}
        raw_domain = getattr(record, "form_key", None)
        form_id_val = getattr(record, "form_id", None)
        subject_val = getattr(record, "subject_key", None)
    elif isinstance(record, Mapping):
        raw_domain = (
            record.get("DOMAIN")
            or record.get("domain")
            or record.get("formKey")
            or record.get("form_key")
        )
        form_id_val = record.get("formId", record.get("form_id"))
        subject_val = record.get("subjectKey") or record.get("subject_key") or record.get("USUBJID")
        nested = record.get("record_data")
        if nested is None:
            nested = record.get("recordData")
        if nested is None:
            nested = record.get("data")
        if isinstance(nested, Mapping):
            raw_data = dict(nested)
        else:
            raw_data = {
                str(k): v for k, v in record.items() if str(k) not in _STANDARDS_ENVELOPE_KEYS
            }
    elif hasattr(record, "model_dump"):
        dumped = record.model_dump(by_alias=True)
        if isinstance(dumped, Mapping):
            raw_domain = (
                dumped.get("DOMAIN")
                or dumped.get("domain")
                or dumped.get("formKey")
                or dumped.get("form_key")
            )
            form_id_val = dumped.get("formId", dumped.get("form_id"))
            subject_val = (
                dumped.get("subjectKey")
                or dumped.get("subject_key")
                or dumped.get("USUBJID")
                or getattr(record, "subject_key", None)
            )
            nested = dumped.get("record_data") or dumped.get("recordData") or dumped.get("data")
            if isinstance(nested, Mapping):
                raw_data = dict(nested)
            else:
                raw_data = {
                    str(k): v for k, v in dumped.items() if str(k) not in _STANDARDS_ENVELOPE_KEYS
                }
    elif hasattr(record, "__dict__"):
        raw_domain = getattr(record, "form_key", None) or getattr(record, "domain", None)
        form_id_val = getattr(record, "form_id", getattr(record, "formId", None))
        subject_val = getattr(record, "subject_key", None) or getattr(record, "subjectKey", None)
        rd = (
            getattr(record, "record_data", None)
            or getattr(record, "recordData", None)
            or getattr(record, "data", None)
        )
        raw_data = dict(rd) if isinstance(rd, Mapping) else {}

    mapped_domain: str | None = None
    if form_domain_map and not configured_domain:
        if raw_domain is not None and raw_domain in form_domain_map:
            mapped_domain = form_domain_map[raw_domain]
        elif form_id_val is not None and form_id_val in form_domain_map:
            mapped_domain = form_domain_map[form_id_val]
    domain = resolve_sdtm_domain(configured_domain or mapped_domain or raw_domain, fallback="AE")
    canonical: dict[str, Any] = dict(raw_data)

    resolved_subject = (
        canonical.get("subjectKey")
        or canonical.get("subject_key")
        or canonical.get("USUBJID")
        or subject_val
    )
    if resolved_subject is not None:
        canonical.setdefault("subjectKey", resolved_subject)

    spec = SDTM_DOMAIN_SPECS.get(domain)
    if spec is not None:
        lower_raw = {str(k).lower(): v for k, v in raw_data.items()}
        for sdtm_var, aliases in spec["aliases"].items():
            if not aliases:
                continue
            primary_key = aliases[0]
            found_val: Any = None
            has_val = False
            if primary_key in canonical and canonical[primary_key] is not None:
                found_val = canonical[primary_key]
                has_val = True
            else:
                for cand in [sdtm_var, *aliases]:
                    if cand in raw_data:
                        found_val = raw_data[cand]
                        has_val = True
                        break
                    if cand.lower() in lower_raw:
                        found_val = lower_raw[cand.lower()]
                        has_val = True
                        break
            if has_val:
                canonical.setdefault(primary_key, found_val)
                if domain == "DM" and sdtm_var == "BRTHDTC":
                    canonical.setdefault("birthDate", found_val)
                elif domain == "DM" and sdtm_var == "ETHNIC":
                    canonical.setdefault("ethnicity", found_val)

    return domain, raw_data, canonical


def evaluate_standards_readiness(
    records: Iterable[Any],
    *,
    study_key: str,
    config: SinkConfig | None = None,
    standards_profile: str | StandardsProfile | None = None,
    sdtm_domain: str | None = None,
    terminology_lookups: dict[str, dict[str, str]] | None = None,
    form_domain_map: Mapping[Any, str] | None = None,
) -> StandardsReadinessReport:
    """Validate and score records against CDASH naming rules and a StandardsProfile."""
    _ = study_key
    raw_prof = (
        standards_profile
        if standards_profile is not None
        else (getattr(config, "standards_profile", None) if config is not None else None)
    )
    cfg_domain = (
        sdtm_domain
        if sdtm_domain is not None
        else (getattr(config, "sdtm_domain", None) if config is not None else None)
    )
    extra = getattr(config, "extra", None) if config is not None else None
    lookups = terminology_lookups
    if lookups is None and isinstance(extra, Mapping):
        maybe_lookups = extra.get("terminology_lookups")
        if isinstance(maybe_lookups, dict):
            lookups = maybe_lookups

    profile = resolve_standards_profile(raw_prof)
    validator = StandardsReadinessValidator(profile=profile)

    records_by_domain: dict[str, list[dict[str, Any]]] = {}
    cdash_warnings: list[str] = []
    seen_cdash: set[tuple[str, str]] = set()

    for rec in records:
        domain, raw_data, canonical = _extract_record_for_standards(
            rec,
            configured_domain=cfg_domain,
            form_domain_map=form_domain_map,
        )
        records_by_domain.setdefault(domain, []).append(canonical)
        for field_key in raw_data:
            pair = (domain, str(field_key))
            if pair in seen_cdash:
                continue
            seen_cdash.add(pair)
            valid_cdash, cdash_msg = validate_cdash_variable(str(field_key), domain)
            if not valid_cdash and cdash_msg:
                cdash_warnings.append(cdash_msg)

    report = validator.score_records(
        records_by_domain=records_by_domain,
        terminology_lookups=lookups,
    )
    if cdash_warnings:
        report.warnings = [*report.warnings, *cdash_warnings]
    return report


def apply_quality_gate(
    sdk: Any,
    study_key: str,
    records: Iterable[Any],
    config: SinkConfig,
    *,
    variables: Iterable[Any] | None = None,
    forms: Iterable[Any] | None = None,
) -> Iterator[Any]:
    """Filter records based on schema readiness and optional CDASH/SDTM readiness rules."""
    cfg_profile = getattr(config, "standards_profile", None)
    cfg_min_std = getattr(config, "min_standards_score", None)
    cfg_domain = getattr(config, "sdtm_domain", None)

    if not config.quality_gate_enabled and cfg_profile is None:
        yield from records
        return

    if not config.quality_gate_enabled:
        seen_cdash: set[tuple[str, str]] = set()
        for record in records:
            domain, raw_data, _ = _extract_record_for_standards(
                record, configured_domain=cfg_domain
            )
            for field_key in raw_data:
                pair = (domain, str(field_key))
                if pair not in seen_cdash:
                    seen_cdash.add(pair)
                    valid_cdash, cdash_msg = validate_cdash_variable(str(field_key), domain)
                    if not valid_cdash and cdash_msg:
                        logger.warning("CDASH naming warning (%s): %s", domain, cdash_msg)
            yield record
        return

    from imednet.core.context import reset_study_context, set_study_context
    from imednet.validation.cache import SchemaValidator, calculate_readiness_score

    validator = SchemaValidator(sdk)
    if variables is None:
        validator.refresh(study_key)
    else:
        form_key_by_id: dict[int, str] = {}
        for form in forms or ():
            fid = getattr(form, "form_id", None)
            fk_val = getattr(form, "form_key", None)
            if (
                isinstance(fid, int)
                and not isinstance(fid, bool)
                and isinstance(fk_val, str)
                and fk_val
            ):
                form_key_by_id[fid] = fk_val

        prepared_vars: list[Any] = []
        for var in variables:
            prepared_var = var
            var_fk = getattr(var, "form_key", None)
            var_fid = getattr(var, "form_id", None)
            if not var_fk and isinstance(var_fid, int) and var_fid in form_key_by_id:
                resolved_fk = form_key_by_id[var_fid]
                model_copy = getattr(var, "model_copy", None)
                if callable(model_copy):
                    prepared_var = model_copy(update={"form_key": resolved_fk})
                else:
                    cloned = copy.copy(var)
                    cloned.form_key = resolved_fk
                    prepared_var = cloned
            prepared_vars.append(prepared_var)

        validator.schema.populate(prepared_vars, study_key=study_key)
        for fid, fk_val in form_key_by_id.items():
            validator.schema._form_id_to_key[(study_key, fid)] = fk_val

    std_validator = (
        StandardsReadinessValidator(resolve_standards_profile(cfg_profile))
        if cfg_profile is not None
        else None
    )
    seen_gate_cdash: set[tuple[str, str]] = set()

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
            if not isinstance(data, Mapping):
                data = {}
            score, reasons = calculate_readiness_score(validator.schema, fk, dict(data))
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

        if isinstance(data, Mapping):
            domain_for_cdash = resolve_sdtm_domain(cfg_domain or str(fk), fallback="AE")
            for field_key in data:
                pair = (domain_for_cdash, str(field_key))
                if pair not in seen_gate_cdash:
                    seen_gate_cdash.add(pair)
                    valid_cdash, cdash_msg = validate_cdash_variable(
                        str(field_key), domain_for_cdash
                    )
                    if not valid_cdash and cdash_msg:
                        logger.warning("CDASH naming warning (%s): %s", domain_for_cdash, cdash_msg)

        if std_validator is not None and cfg_min_std is None:
            dom, _, canonical = _extract_record_for_standards(
                record, configured_domain=cfg_domain or str(fk)
            )
            rec_report = std_validator.score_records(records_by_domain={dom: [canonical]})
            if rec_report.score < config.min_schema_readiness_score:
                std_reasons = [v.message for v in rec_report.violations]
                logger.info(
                    "Dropped record %s (Standards Score: %.1f < %.1f). Reasons: %s",
                    rec_dict.get("record_id", "Unknown"),
                    rec_report.score,
                    config.min_schema_readiness_score,
                    "; ".join(std_reasons),
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


def extract_record_form_ref(record: Any) -> tuple[int | None, str | None]:
    """Extract ``(form_id, form_key)`` from a record object or mapping."""
    if isinstance(record, Mapping):
        raw_id = record.get("form_id")
        if raw_id is None:
            raw_id = record.get("formId")
        raw_key = record.get("form_key") or record.get("formKey")
    else:
        raw_id = getattr(record, "form_id", None)
        if raw_id is None:
            raw_id = getattr(record, "formId", None)
        raw_key = getattr(record, "form_key", None) or getattr(record, "formKey", None)

    form_id: int | None = None
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        form_id = raw_id
    elif isinstance(raw_id, str):
        stripped_id = raw_id.strip()
        if stripped_id.lstrip("-").isdigit():
            form_id = int(stripped_id)

    form_key = raw_key.strip() if isinstance(raw_key, str) and raw_key.strip() else None
    return form_id, form_key


class BoundedFormBufferRouter:
    """Buffer per-form records and flush on per-form batch size or global memory ceiling."""

    def __init__(
        self,
        form_keys: Sequence[str],
        *,
        batch_size: int,
        max_buffered_records: int | None = None,
        on_flush_active: Callable[[str, list[Any], int], None],
        on_flush_voided: Callable[[str, list[Any], int], None] | None = None,
    ) -> None:
        """Initialize per-form buffers and global ceiling tracking."""
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")
        self._form_keys = list(dict.fromkeys(form_keys))
        self.batch_size = batch_size
        self.ceiling = resolve_max_buffered_records(batch_size, max_buffered_records)
        self._on_flush_active = on_flush_active
        self._on_flush_voided = on_flush_voided
        self._active: dict[str, list[Any]] = {fk: [] for fk in self._form_keys}
        self._voided: dict[str, list[Any]] = {fk: [] for fk in self._form_keys}
        self._active_batch_idx: dict[str, int] = dict.fromkeys(self._form_keys, 0)
        self._voided_batch_idx: dict[str, int] = dict.fromkeys(self._form_keys, 0)
        self.total_buffered = 0
        self.peak_buffered = 0

    def add_active(self, form_key: str, record: Any) -> None:
        """Append an active record to ``form_key`` and flush if thresholds are met."""
        buf = self._active[form_key]
        buf.append(record)
        self.total_buffered += 1
        self.peak_buffered = max(self.peak_buffered, self.total_buffered)
        if len(buf) >= self.batch_size:
            self._flush_active_form(form_key)
        elif self.total_buffered >= self.ceiling:
            self._evict_largest()

    def add_voided(self, form_key: str, record: Any) -> None:
        """Append a voided record to ``form_key`` and flush if thresholds are met."""
        if self._on_flush_voided is None:
            return
        buf = self._voided[form_key]
        buf.append(record)
        self.total_buffered += 1
        self.peak_buffered = max(self.peak_buffered, self.total_buffered)
        if len(buf) >= self.batch_size:
            self._flush_voided_form(form_key)
        elif self.total_buffered >= self.ceiling:
            self._evict_largest()

    def _flush_active_form(self, form_key: str) -> None:
        buf = self._active[form_key]
        if not buf:
            return
        batch = list(buf)
        buf.clear()
        self.total_buffered -= len(batch)
        idx = self._active_batch_idx[form_key]
        self._active_batch_idx[form_key] = idx + 1
        self._on_flush_active(form_key, batch, idx)

    def _flush_voided_form(self, form_key: str) -> None:
        if self._on_flush_voided is None:
            return
        buf = self._voided[form_key]
        if not buf:
            return
        batch = list(buf)
        buf.clear()
        self.total_buffered -= len(batch)
        idx = self._voided_batch_idx[form_key]
        self._voided_batch_idx[form_key] = idx + 1
        self._on_flush_voided(form_key, batch, idx)

    def _evict_largest(self) -> None:
        while self.total_buffered >= self.ceiling:
            best_kind: str | None = None
            best_key: str | None = None
            best_len = 0
            for fk in self._form_keys:
                a_len = len(self._active[fk])
                if a_len > best_len:
                    best_len = a_len
                    best_kind = "active"
                    best_key = fk
                if self._on_flush_voided is not None:
                    v_len = len(self._voided[fk])
                    if v_len > best_len:
                        best_len = v_len
                        best_kind = "voided"
                        best_key = fk
            if best_key is None or best_len == 0:
                break
            if best_kind == "voided":
                self._flush_voided_form(best_key)
            else:
                self._flush_active_form(best_key)

    def flush_all(self) -> None:
        """Flush all remaining buffered active and voided records in form order."""
        for fk in self._form_keys:
            if self._active[fk]:
                self._flush_active_form(fk)
            if self._on_flush_voided is not None and self._voided[fk]:
                self._flush_voided_form(fk)


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


def _validate_record_data_filter_input(record_data_filter: str) -> str:
    """Validate ``record_data_filter`` syntax, non-emptiness, operators, and unique AND keys."""
    if not isinstance(record_data_filter, str):
        raise ValueError(
            f"record_data_filter must be a string, got {type(record_data_filter).__name__}"
        )
    stripped = record_data_filter.strip()
    if not stripped:
        raise ValueError("record_data_filter cannot be empty")
    validate_record_filter(record_data_filter)
    if stripped.lower() != "none":
        clauses, _ = _split_filter_clauses(stripped)
        for clause in clauses:
            if _parse_clause_operator(clause) is None:
                raise ValueError(f"Malformed filter clause '{clause}': missing comparison operator")
    return stripped


def _split_top_level(expr: str, sep: str) -> list[str]:
    """Split a filter expression by ``sep`` outside double quotes, rejecting empty clauses."""
    raw_parts: list[str] = []
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
            raw_parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    raw_parts.append("".join(buf).strip())
    if len(raw_parts) == 1:
        return [raw_parts[0]] if raw_parts[0] else []
    if any(not part for part in raw_parts):
        raise ValueError("Filter expression contains an empty clause")
    return raw_parts


def _merge_and_condition(target: dict[str, Any], key: str, condition: Any) -> None:
    """Merge an AND filter condition onto ``target``, rejecting duplicate AND attributes."""
    camel_key = _snake_to_camel(key)
    for existing in target:
        if _snake_to_camel(existing) == camel_key:
            raise ValueError(f"Duplicate filter attribute '{camel_key}' in AND clause")
    target[key] = condition


def _coerce_date_modified_clauses(raw_cond: Any) -> list[tuple[str, datetime]]:
    """Normalize a branch's ``dateModified`` value into ``[(operator, utc_datetime), ...]``."""
    raw_items: list[tuple[str, Any]]
    if isinstance(raw_cond, list):
        raw_items = []
        for item in raw_cond:
            if isinstance(item, tuple) and len(item) == 2:
                raw_items.append((item[0], item[1]))
            else:
                raw_items.append(("==", item))
    elif isinstance(raw_cond, tuple):
        if len(raw_cond) != 2:
            raise ValueError(
                f"Filter tuple for 'dateModified' must have 2 elements, got {len(raw_cond)}"
            )
        raw_items = [(raw_cond[0], raw_cond[1])]
    else:
        raw_items = [("==", raw_cond)]

    parsed: list[tuple[str, datetime]] = []
    for op, val in raw_items:
        validate_filter_operator(op, allow_regex=False)
        dt = normalize_watermark_datetime(val)
        if dt is None:
            raise ValueError(f"Invalid dateModified filter value: {val!r}")
        parsed.append((op, dt))
    return parsed


def _combine_branch_with_watermark(
    branch: dict[str, Any],
    norm_wm: datetime | None,
) -> tuple[dict[str, Any], Callable[[Any], bool] | None] | None:
    """Combine a branch filter dict with ``norm_wm`` using at most one ``dateModified`` key.

    Returns ``None`` when the combined ``dateModified`` bounds are unsatisfiable
    so callers can skip issuing an upstream request for an empty intersection.
    """
    out = dict(branch)
    dm_keys = [k for k in out if _snake_to_camel(k) == "dateModified"]
    if len(dm_keys) > 1:
        raise ValueError("Duplicate filter attribute 'dateModified' in AND clause")

    clauses: list[tuple[str, datetime]] = []
    if dm_keys:
        raw_existing = out.pop(dm_keys[0])
        # Preserve original non-list condition directly when watermark is absent
        if norm_wm is None and not isinstance(raw_existing, list):
            _coerce_date_modified_clauses(raw_existing)
            out["dateModified"] = raw_existing
            return out, None
        clauses.extend(_coerce_date_modified_clauses(raw_existing))

    if norm_wm is not None:
        clauses.append((">=", norm_wm))

    if not clauses:
        return out, None

    lower: tuple[datetime, bool] | None = None
    upper: tuple[datetime, bool] | None = None
    exact: datetime | None = None
    excluded: set[datetime] = set()

    for op, dt in clauses:
        if op == "==":
            if exact is not None and exact != dt:
                return None
            exact = dt
        elif op in (">=", ">"):
            strict = op == ">"
            if lower is None or dt > lower[0] or (dt == lower[0] and strict and not lower[1]):
                lower = (dt, strict)
        elif op in ("<=", "<"):
            strict = op == "<"
            if upper is None or dt < upper[0] or (dt == upper[0] and strict and not upper[1]):
                upper = (dt, strict)
        elif op == "!=":
            excluded.add(dt)

    if lower is not None:
        for ex in list(excluded):
            if ex < lower[0] or (ex == lower[0] and lower[1]):
                excluded.discard(ex)
            elif ex == lower[0] and not lower[1]:
                lower = (lower[0], True)
                excluded.discard(ex)

    if upper is not None:
        for ex in list(excluded):
            if ex > upper[0] or (ex == upper[0] and upper[1]):
                excluded.discard(ex)
            elif ex == upper[0] and not upper[1]:
                upper = (upper[0], True)
                excluded.discard(ex)

    if exact is not None:
        if exact in excluded:
            return None
        if lower is not None and (exact < lower[0] or (exact == lower[0] and lower[1])):
            return None
        if upper is not None and (exact > upper[0] or (exact == upper[0] and upper[1])):
            return None
        out["dateModified"] = ("==", format_iso_datetime(exact))
        return out, None

    if lower is not None and upper is not None:
        if lower[0] > upper[0]:
            return None
        if lower[0] == upper[0]:
            if lower[1] or upper[1] or lower[0] in excluded:
                return None
            out["dateModified"] = ("==", format_iso_datetime(lower[0]))
            return out, None

    residual_upper: tuple[datetime, bool] | None = None
    if lower is not None:
        out["dateModified"] = (
            ">" if lower[1] else ">=",
            format_iso_datetime(lower[0]),
        )
        residual_upper = upper
    elif upper is not None:
        out["dateModified"] = (
            "<" if upper[1] else "<=",
            format_iso_datetime(upper[0]),
        )
    elif excluded:
        first_ex = min(excluded)
        excluded.discard(first_ex)
        out["dateModified"] = ("!=", format_iso_datetime(first_ex))

    if residual_upper is None and not excluded:
        return out, None

    frozen_excluded = frozenset(excluded)

    def _post_predicate(record: Any) -> bool:
        rec_ts = extract_record_timestamp(record)
        if rec_ts is None:
            return False
        if residual_upper is not None:
            bound_dt, is_strict = residual_upper
            if is_strict and not (rec_ts < bound_dt):
                return False
            if not is_strict and not (rec_ts <= bound_dt):
                return False
        return rec_ts not in frozen_excluded

    return out, _post_predicate


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


def _resolve_record_query_branches(
    *,
    record_filter: dict[str, Any] | str | Sequence[str] | None = None,
    watermark: datetime | str | None = None,
    exclude_deleted_upstream: bool = True,
) -> list[tuple[dict[str, Any], Callable[[Any], bool] | None]]:
    """Resolve AND-safe query branches alongside optional timestamp post-predicates."""
    from imednet.endpoints.records import RecordsEndpoint
    from imednet.models.records import Record

    allowed_attrs = RecordsEndpoint.FILTER_ATTRIBUTES
    base_branches: list[dict[str, Any]] = [{}]

    if isinstance(record_filter, str):
        stripped = record_filter.strip()
        if not stripped:
            raise ValueError("Filter expression cannot be empty")
        if stripped.lower() != "none":
            validate_connector_exclusivity(stripped)
            or_parts = _split_top_level(stripped, ",")
            if len(or_parts) > 1:
                base_branches = [
                    parse_filter_expressions(
                        [part],
                        model=Record,
                        allowed_attributes=allowed_attrs,
                    )
                    for part in or_parts
                ]
            else:
                and_parts = _split_top_level(stripped, ";")
                base_branches = [
                    parse_filter_expressions(
                        and_parts,
                        model=Record,
                        allowed_attributes=allowed_attrs,
                    )
                ]
    elif isinstance(record_filter, Mapping):
        raw_dict = dict(record_filter)
        inline_filter = raw_dict.pop("filter", None)
        seen_dict_keys: set[str] = set()
        for dict_key, dict_val in raw_dict.items():
            validate_filter_key(dict_key, model=Record, allowed_attributes=allowed_attrs)
            camel_dk = _snake_to_camel(dict_key)
            if camel_dk in seen_dict_keys:
                raise ValueError(f"Duplicate filter attribute '{camel_dk}' in AND clause")
            seen_dict_keys.add(camel_dk)
            if isinstance(dict_val, tuple):
                if len(dict_val) != 2:
                    raise ValueError(
                        f"Filter tuple for '{dict_key}' must have 2 elements, got {len(dict_val)}"
                    )
                validate_filter_operator(dict_val[0], allow_regex=False)
        expanded = _expand_scalar_list_branches(raw_dict)
        if not expanded:
            return []
        if inline_filter is not None:
            if not isinstance(inline_filter, str) or not inline_filter.strip():
                raise ValueError("Inline 'filter' expression cannot be empty")
            inline_branches = [
                b
                for b, _ in _resolve_record_query_branches(
                    record_filter=inline_filter,
                    watermark=None,
                    exclude_deleted_upstream=False,
                )
            ]
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
        if not record_filter:
            base_branches = [{}]
        else:
            exprs: list[str] = []
            for item in record_filter:
                text = str(item).strip()
                if not text:
                    raise ValueError("Filter expression cannot be empty")
                validate_connector_exclusivity(text)
                exprs.append(text)
            if len(exprs) == 1 and len(_split_top_level(exprs[0], ",")) > 1:
                base_branches = [
                    parse_filter_expressions(
                        [p],
                        model=Record,
                        allowed_attributes=allowed_attrs,
                    )
                    for p in _split_top_level(exprs[0], ",")
                ]
            else:
                flat_and: list[str] = []
                for expr in exprs:
                    if len(_split_top_level(expr, ",")) > 1:
                        raise ValueError(
                            "Filter expression cannot combine both ';' (AND) and ',' (OR) connectors"
                        )
                    flat_and.extend(_split_top_level(expr, ";"))
                base_branches = [
                    parse_filter_expressions(
                        flat_and,
                        model=Record,
                        allowed_attributes=allowed_attrs,
                    )
                ]

    norm_wm = normalize_watermark_datetime(watermark)

    final_branches: list[tuple[dict[str, Any], Callable[[Any], bool] | None]] = []
    for branch in base_branches:
        merged = _combine_branch_with_watermark(branch, norm_wm)
        if merged is None:
            continue
        b, post_pred = merged
        if exclude_deleted_upstream and not any(_snake_to_camel(k) == "deleted" for k in b):
            b["deleted"] = False
        final_branches.append((b, post_pred))
    return final_branches


def build_record_query_branches(
    *,
    record_filter: dict[str, Any] | str | Sequence[str] | None = None,
    watermark: datetime | str | None = None,
    exclude_deleted_upstream: bool = True,
) -> list[dict[str, Any]]:
    """Build AND-only filter dictionaries for ``sdk.records.list``.

    Ensures OR filter clauses (comma-separated strings or scalar lists) are
    split into separate query branches so ``;`` (AND) and ``,`` (OR) connectors
    are never mixed in a single upstream ``filter`` parameter, and merges
    ``watermark`` into ``dateModified`` without emitting duplicate
    ``dateModified`` attributes in one request.
    """
    return [
        branch
        for branch, _ in _resolve_record_query_branches(
            record_filter=record_filter,
            watermark=watermark,
            exclude_deleted_upstream=exclude_deleted_upstream,
        )
    ]


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
    validated_rdf = (
        _validate_record_data_filter_input(record_data_filter)
        if record_data_filter is not None
        else None
    )

    resolved = _resolve_record_query_branches(
        record_filter=record_filter,
        watermark=watermark,
        exclude_deleted_upstream=exclude_deleted_upstream,
    )
    if not resolved:
        return

    deduplicate = len(resolved) > 1
    seen_ids: set[Any] = set()

    for branch, post_predicate in resolved:
        page_iter = sdk.records.list(
            study_key=study_key,
            record_data_filter=validated_rdf,
            **branch,
        )
        for record in page_iter:
            if post_predicate is not None and not post_predicate(record):
                continue
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

    explicit_provider: Any = None
    if is_real_cfg:
        explicit_provider = config.state_provider
    elif not isinstance(config, NonCallableMock):
        cand_provider = getattr(config, "state_provider", None)
        if cand_provider is not None and hasattr(cand_provider, "transaction"):
            explicit_provider = cand_provider
    elif incremental and hasattr(getattr(config, "state_provider", None), "transaction"):
        explicit_provider = getattr(config, "state_provider", None)

    if not idempotent and (incremental or explicit_provider is not None):
        raise ExportConfigurationError(
            "SinkConfig(idempotent=False) cannot be combined with incremental=True or "
            "state_provider because inclusive dateModified>= watermarks require idempotent=True."
        )

    raw_max_buf = (
        config.max_buffered_records
        if is_real_cfg
        else getattr(config, "max_buffered_records", None)
    )
    max_buffered_records = (
        raw_max_buf
        if isinstance(raw_max_buf, int) and not isinstance(raw_max_buf, bool) and raw_max_buf > 0
        else None
    )

    raw_as_sdtm = config.as_sdtm if is_real_cfg else getattr(config, "as_sdtm", False)
    as_sdtm = raw_as_sdtm if isinstance(raw_as_sdtm, bool) else False

    raw_sdtm_domain = config.sdtm_domain if is_real_cfg else getattr(config, "sdtm_domain", None)
    sdtm_domain = (
        resolve_sdtm_domain(raw_sdtm_domain)
        if isinstance(raw_sdtm_domain, str) and raw_sdtm_domain.strip()
        else None
    )
    if sdtm_domain is not None:
        as_sdtm = True

    raw_std_profile = (
        config.standards_profile if is_real_cfg else getattr(config, "standards_profile", None)
    )
    standards_profile = (
        raw_std_profile
        if isinstance(raw_std_profile, StandardsProfile)
        or (isinstance(raw_std_profile, str) and raw_std_profile.strip())
        else None
    )

    raw_min_std = (
        config.min_standards_score if is_real_cfg else getattr(config, "min_standards_score", None)
    )
    min_standards_score = (
        float(raw_min_std)
        if isinstance(raw_min_std, (int, float))
        and not isinstance(raw_min_std, bool)
        and math.isfinite(raw_min_std)
        else None
    )

    raw_naming = (
        config.naming_convention if is_real_cfg else getattr(config, "naming_convention", None)
    )
    naming_convention = (
        normalize_naming_convention(raw_naming)
        if isinstance(raw_naming, str) and raw_naming.strip()
        else None
    )

    return {
        "incremental": incremental,
        "include_deleted": include_deleted,
        "idempotent": idempotent,
        "batch_size": batch_size,
        "max_buffered_records": max_buffered_records,
        "modified_since": modified_since,
        "record_filter": record_filter,
        "record_data_filter": record_data_filter,
        "stream_name": cfg_stream_name,
        "ledger_path": ledger_path,
        "state_provider": explicit_provider,
        "as_sdtm": as_sdtm,
        "sdtm_domain": sdtm_domain,
        "standards_profile": standards_profile,
        "min_standards_score": min_standards_score,
        "naming_convention": naming_convention,
    }


def _enforce_batch_standards_readiness(
    batch: Sequence[Any],
    *,
    batch_id: str,
    study_key: str,
    config: SinkConfig,
    standards_profile: Any,
    min_standards_score: float | None,
    sdtm_domain: str | None,
) -> None:
    """Evaluate batch standards readiness, log masked warnings, and enforce min_standards_score."""
    report = evaluate_standards_readiness(
        batch,
        study_key=study_key,
        config=config,
        standards_profile=standards_profile,
        sdtm_domain=sdtm_domain,
    )
    profile_obj = resolve_standards_profile(standards_profile)
    for warning_msg in report.warnings:
        logger.warning(
            "Standards readiness warning in batch %s: %s",
            batch_id,
            mask_clinical_phi(warning_msg) if config.mask_phi else warning_msg,
        )
    if min_standards_score is not None and report.score < min_standards_score:
        error_fields = sorted({v.field for v in report.violations if v.severity.upper() == "ERROR"})
        detail = f" (violating fields: {', '.join(error_fields)})" if error_fields else ""
        raise ExportError(
            f"Standards readiness check failed for batch '{batch_id}' "
            f"(profile='{profile_obj.profile_name}', score={report.score:.1f} < "
            f"min_standards_score={min_standards_score:.1f}){detail}"
        )


def resolve_form_sdtm_domain(form: Any, *, fallback: str = "AE") -> str:
    """Resolve the 2-character or canonical SDTM domain for a Study Form object or mapping."""
    if isinstance(form, Mapping):
        raw_key = form.get("formKey") or form.get("form_key")
        raw_name = form.get("formName") or form.get("form_name")
    else:
        raw_key = getattr(form, "form_key", None) or getattr(form, "formKey", None)
        raw_name = getattr(form, "form_name", None) or getattr(form, "formName", None)

    key_dom = (
        resolve_sdtm_domain(raw_key, fallback="")
        if isinstance(raw_key, str) and raw_key.strip()
        else ""
    )
    if key_dom and key_dom in SDTM_DOMAIN_SPECS:
        return key_dom

    name_dom = (
        resolve_sdtm_domain(raw_name, fallback="")
        if isinstance(raw_name, str) and raw_name.strip()
        else ""
    )
    if name_dom and name_dom in SDTM_DOMAIN_SPECS:
        return name_dom

    if key_dom:
        return key_dom
    if name_dom:
        return name_dom
    return fallback


def filter_forms_by_sdtm_domain(
    forms: Sequence[Any],
    sdtm_domain: str | None,
) -> list[Any]:
    """Filter Study Forms down to those matching ``sdtm_domain`` when specified."""
    if not sdtm_domain or not isinstance(sdtm_domain, str) or not sdtm_domain.strip():
        return list(forms)
    target = resolve_sdtm_domain(sdtm_domain)
    return [f for f in forms if resolve_form_sdtm_domain(f, fallback="") == target]


def build_form_sdtm_domain_map(forms: Iterable[Any]) -> dict[Any, str]:
    """Build a lookup map from ``form_id`` and ``form_key`` to resolved SDTM domain."""
    mapping: dict[Any, str] = {}
    for form in forms:
        if isinstance(form, NonCallableMock):
            continue
        dom = resolve_form_sdtm_domain(form, fallback="")
        if not dom:
            continue
        if isinstance(form, Mapping):
            fid = form.get("formId") if "formId" in form else form.get("form_id")
            fkey = form.get("formKey") or form.get("form_key")
        else:
            fid = getattr(form, "form_id", None)
            if fid is None:
                fid = getattr(form, "formId", None)
            fkey = getattr(form, "form_key", None) or getattr(form, "formKey", None)
        if isinstance(fid, int) and not isinstance(fid, bool):
            mapping[fid] = dom
        if isinstance(fkey, str) and fkey.strip():
            mapping[fkey] = dom
            mapping[fkey.strip()] = dom
    return mapping


def _try_fetch_study_forms(sdk: Any, study_key: str) -> list[Any]:
    """Safely fetch concrete Study Forms when ``sdk.forms.list`` is available and non-mocked."""
    if sdk is None or isinstance(sdk, NonCallableMock):
        forms_ep = getattr(sdk, "forms", None)
        list_fn = getattr(forms_ep, "list", None) if forms_ep is not None else None
        if list_fn is None:
            return []
        try:
            candidate = list_fn(study_key=study_key)
        except Exception:
            return []
        if isinstance(candidate, NonCallableMock):
            return []
        try:
            items = list(candidate)
        except Exception:
            return []
        return [
            f
            for f in items
            if not isinstance(f, NonCallableMock)
            and (
                (isinstance(f, Mapping) and (f.get("formKey") or f.get("form_key")))
                or isinstance(getattr(f, "form_key", None), str)
            )
        ]

    forms_ep = getattr(sdk, "forms", None)
    list_fn = getattr(forms_ep, "list", None) if forms_ep is not None else None
    if not callable(list_fn):
        return []
    try:
        return [f for f in list_fn(study_key=study_key) if f is not None]
    except Exception:
        return []


def extract_sdtm_composite_key(
    row: Mapping[str, Any],
) -> tuple[str, str, str, str | None, int | None] | None:
    """Return ``(STUDYID, DOMAIN, USUBJID, seq_col, seq_val)`` for a transformed SDTM row."""
    study_id = row.get("STUDYID")
    domain = row.get("DOMAIN")
    usubjid = row.get("USUBJID")
    if study_id is None or domain is None or usubjid is None:
        return None
    dom_str = str(domain).strip().upper()
    if not dom_str:
        return None
    if dom_str == "DM":
        return (str(study_id), "DM", str(usubjid), None, None)
    seq_col = f"{dom_str}SEQ"
    raw_seq = row.get(seq_col)
    if raw_seq is None:
        for cand_col, cand_val in row.items():
            cand_str = str(cand_col)
            if cand_str.endswith("SEQ") and cand_str.upper() != "DMSEQ" and cand_val is not None:
                seq_col = cand_str
                raw_seq = cand_val
                break
    if raw_seq is None or isinstance(raw_seq, bool):
        return (str(study_id), dom_str, str(usubjid), seq_col, None)
    try:
        seq_val = int(raw_seq)
    except (TypeError, ValueError):
        try:
            as_float = float(raw_seq)
        except (TypeError, ValueError):
            return (str(study_id), dom_str, str(usubjid), seq_col, None)
        if math.isnan(as_float) or math.isinf(as_float) or not as_float.is_integer():
            return (str(study_id), dom_str, str(usubjid), seq_col, None)
        seq_val = int(as_float)
    return (str(study_id), dom_str, str(usubjid), seq_col, seq_val)


def deduplicate_sdtm_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate transformed SDTM rows by composite key ``(STUDYID, DOMAIN, USUBJID[, <DOM>SEQ])``."""
    if len(rows) <= 1:
        return list(rows)
    dedup_index: dict[tuple[Any, ...], int] = {}
    ordered_rows: list[dict[str, Any] | None] = []
    for row in rows:
        comp = extract_sdtm_composite_key(row)
        if comp is None or (comp[1] != "DM" and comp[4] is None):
            ordered_rows.append(row)
            continue
        if comp in dedup_index:
            ordered_rows[dedup_index[comp]] = None
        dedup_index[comp] = len(ordered_rows)
        ordered_rows.append(row)
    return [r for r in ordered_rows if r is not None]


def _resolve_lineage_target(
    sink_or_config: Any,
    *,
    study_key: str | None = None,
    stream_name: str | None = None,
) -> tuple[Any, SinkConfig | Any, str, str, dict[str, Any] | None]:
    """Resolve ``(provider, cfg, resolved_study, resolved_stream, active_tx)`` for SDTM lineage."""
    is_sink = (
        isinstance(sink_or_config, ExportSink)
        or hasattr(sink_or_config, "write_batch")
        or hasattr(sink_or_config, "_active_tx")
        or hasattr(sink_or_config, "_sdtm_lineage")
    )
    cfg = getattr(sink_or_config, "config", sink_or_config) if is_sink else sink_or_config
    resolved_study = (
        study_key
        or (getattr(sink_or_config, "_active_study_key", None) if is_sink else None)
        or getattr(cfg, "study_key", "")
        or "UNKNOWN"
    )
    cfg_stream = getattr(cfg, "stream_name", None)
    resolved_stream = (
        stream_name
        or (getattr(sink_or_config, "_active_stream_name", None) if is_sink else None)
        or (cfg_stream if isinstance(cfg_stream, str) and cfg_stream.strip() else None)
        or "records"
    )
    active_tx = getattr(sink_or_config, "_active_tx", None) if is_sink else None
    if not isinstance(active_tx, dict):
        active_tx = None

    provider = getattr(sink_or_config, "_active_state_provider", None) if is_sink else None
    if provider is None:
        explicit_prov = getattr(cfg, "state_provider", None)
        if (explicit_prov is not None and not isinstance(explicit_prov, NonCallableMock)) or (
            explicit_prov is not None and hasattr(explicit_prov, "get_sdtm_lineage")
        ):
            provider = explicit_prov
        elif (
            getattr(cfg, "incremental", False) is True
            and getattr(cfg, "ledger_path", None) is not None
        ):
            with contextlib.suppress(Exception):
                provider = get_state_provider(ledger_path=cfg.ledger_path)
    return provider, cfg, str(resolved_study), str(resolved_stream), active_tx


def _read_provider_lineage_payload(
    provider: Any,
    study_key: str,
    stream_name: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Read ``(sdtm_lineage, sdtm_seq_watermarks)`` from ``provider``."""
    lineage: dict[str, dict[str, Any]] = {}
    watermarks: dict[str, int] = {}
    if provider is None:
        return lineage, watermarks

    raw_payload: Any = None
    get_fn = getattr(provider, "get_sdtm_lineage", None)
    if callable(get_fn) and not isinstance(get_fn, NonCallableMock):
        with contextlib.suppress(Exception):
            raw_payload = get_fn(study_key, stream_name)
    if not isinstance(raw_payload, Mapping):
        read_fn = getattr(provider, "read_state", None)
        if callable(read_fn):
            with contextlib.suppress(Exception):
                ledger_state = read_fn()
                studies = getattr(ledger_state, "studies", None)
                if isinstance(studies, Mapping) and study_key in studies:
                    streams = getattr(studies[study_key], "streams", None)
                    if isinstance(streams, Mapping) and stream_name in streams:
                        raw_payload = getattr(streams[stream_name], "metadata", None)

    if isinstance(raw_payload, Mapping):
        raw_lin = raw_payload.get("sdtm_lineage")
        if isinstance(raw_lin, Mapping):
            for rk, rv in raw_lin.items():
                if isinstance(rv, Mapping):
                    lineage[str(rk)] = dict(rv)
        raw_wm = raw_payload.get("sdtm_seq_watermarks")
        if isinstance(raw_wm, Mapping):
            for wk, wv in raw_wm.items():
                if isinstance(wv, int) and not isinstance(wv, bool):
                    watermarks[str(wk)] = wv
    return lineage, watermarks


def rehydrate_sdtm_lineage(
    sink_or_config: Any,
    *,
    study_key: str | None = None,
    stream_name: str | None = None,
) -> tuple[dict[str, dict[str, int]], dict[str, int], dict[str, dict[str, Any]]]:
    """Rehydrate ``(domain_seq_counters, seq_overrides, full_lineage)`` from ledger & sink memory."""
    provider, _cfg, resolved_study, resolved_stream, active_tx = _resolve_lineage_target(
        sink_or_config,
        study_key=study_key,
        stream_name=stream_name,
    )
    full_lineage, watermarks = _read_provider_lineage_payload(
        provider,
        resolved_study,
        resolved_stream,
    )

    if active_tx is not None:
        tx_meta = active_tx.get("metadata")
        if isinstance(tx_meta, Mapping):
            tx_lin = tx_meta.get("sdtm_lineage")
            if isinstance(tx_lin, Mapping):
                for rk, rv in tx_lin.items():
                    if isinstance(rv, Mapping):
                        full_lineage[str(rk)] = dict(rv)
            tx_wm = tx_meta.get("sdtm_seq_watermarks")
            if isinstance(tx_wm, Mapping):
                for wk, wv in tx_wm.items():
                    if isinstance(wv, int) and not isinstance(wv, bool):
                        watermarks[str(wk)] = max(watermarks.get(str(wk), 0), wv)

    mem_lin = getattr(sink_or_config, "_sdtm_lineage", None)
    if isinstance(mem_lin, Mapping):
        for rk, rv in mem_lin.items():
            if isinstance(rv, Mapping):
                full_lineage[str(rk)] = dict(rv)
    mem_wm = getattr(sink_or_config, "_sdtm_seq_watermarks", None)
    if isinstance(mem_wm, Mapping):
        for wk, wv in mem_wm.items():
            if isinstance(wv, int) and not isinstance(wv, bool):
                watermarks[str(wk)] = max(watermarks.get(str(wk), 0), wv)

    domain_seq_counters: dict[str, dict[str, int]] = {}
    seq_overrides: dict[str, int] = {}

    for wk, wv in watermarks.items():
        dom_part, sep, subj_part = wk.partition(":")
        if sep and dom_part:
            dom_map = domain_seq_counters.setdefault(dom_part, {})
            dom_map[subj_part] = max(dom_map.get(subj_part, 0), wv)

    for rec_id_str, entry in full_lineage.items():
        dom = str(entry.get("domain") or "").strip().upper()
        usubjid = str(entry.get("usubjid") or "")
        seq_val = entry.get("seq_val")
        if dom and dom != "DM" and isinstance(seq_val, int) and not isinstance(seq_val, bool):
            seq_overrides[rec_id_str] = seq_val
            dom_map = domain_seq_counters.setdefault(dom, {})
            dom_map[usubjid] = max(dom_map.get(usubjid, 0), seq_val)

    return domain_seq_counters, seq_overrides, full_lineage


def commit_sdtm_lineage(
    sink_or_config: Any,
    lineage_updates: Mapping[str, dict[str, Any]],
    *,
    study_key: str | None = None,
    stream_name: str | None = None,
) -> None:
    """Record new/updated SDTM lineage entries and monotonic sequence watermarks."""
    if not lineage_updates:
        return

    provider, _cfg, resolved_study, resolved_stream, active_tx = _resolve_lineage_target(
        sink_or_config,
        study_key=study_key,
        stream_name=stream_name,
    )

    wm_updates: dict[str, int] = {}
    clean_updates: dict[str, dict[str, Any]] = {}
    for rec_id_key, entry in lineage_updates.items():
        if not isinstance(entry, Mapping):
            continue
        copied_entry = dict(entry)
        clean_updates[str(rec_id_key)] = copied_entry
        dom = str(copied_entry.get("domain") or "").strip().upper()
        usubjid = str(copied_entry.get("usubjid") or "")
        seq_val = copied_entry.get("seq_val")
        if dom and dom != "DM" and isinstance(seq_val, int) and not isinstance(seq_val, bool):
            wm_key = f"{dom}:{usubjid}"
            wm_updates[wm_key] = max(wm_updates.get(wm_key, 0), seq_val)

    is_sink = (
        isinstance(sink_or_config, ExportSink)
        or hasattr(sink_or_config, "write_batch")
        or hasattr(sink_or_config, "_active_tx")
        or hasattr(sink_or_config, "_sdtm_lineage")
    )
    if is_sink:
        mem_lin = getattr(sink_or_config, "_sdtm_lineage", None)
        if not isinstance(mem_lin, dict):
            mem_lin = {}
            with contextlib.suppress(Exception):
                sink_or_config._sdtm_lineage = mem_lin
        mem_lin.update(clean_updates)

        mem_wm = getattr(sink_or_config, "_sdtm_seq_watermarks", None)
        if not isinstance(mem_wm, dict):
            mem_wm = {}
            with contextlib.suppress(Exception):
                sink_or_config._sdtm_seq_watermarks = mem_wm
        for wk, wv in wm_updates.items():
            mem_wm[wk] = max(mem_wm.get(wk, 0), wv)

    if active_tx is not None:
        existing_lin, existing_wm = _read_provider_lineage_payload(
            provider,
            resolved_study,
            resolved_stream,
        )
        tx_meta = active_tx.setdefault("metadata", {})
        if not isinstance(tx_meta, dict):
            tx_meta = {}
            active_tx["metadata"] = tx_meta
        merged_lin = dict(existing_lin)
        if isinstance(tx_meta.get("sdtm_lineage"), Mapping):
            merged_lin.update(tx_meta["sdtm_lineage"])
        merged_lin.update(clean_updates)
        tx_meta["sdtm_lineage"] = merged_lin

        merged_wm = dict(existing_wm)
        if isinstance(tx_meta.get("sdtm_seq_watermarks"), Mapping):
            for wk, wv in tx_meta["sdtm_seq_watermarks"].items():
                if isinstance(wv, int) and not isinstance(wv, bool):
                    merged_wm[str(wk)] = max(merged_wm.get(str(wk), 0), wv)
        for wk, wv in wm_updates.items():
            merged_wm[wk] = max(merged_wm.get(wk, 0), wv)
        tx_meta["sdtm_seq_watermarks"] = merged_wm
        return

    if provider is not None:
        record_fn = getattr(provider, "record_sdtm_lineage", None)
        if callable(record_fn) and not isinstance(record_fn, NonCallableMock):
            with contextlib.suppress(Exception):
                record_fn(
                    resolved_study,
                    resolved_stream,
                    lineage_updates=clean_updates,
                    seq_watermarks=wm_updates,
                )


def resolve_voided_sdtm_keys(
    records: Sequence[Any],
    sink_or_config: Any,
    *,
    study_key: str | None = None,
    stream_name: str | None = None,
    sdtm_domain: str | None = None,
    form_domain_map: Mapping[Any, str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve voided records to composite SDTM keys ``(study_id, domain, usubjid, seq_col, seq_val)``."""
    _dom_counters, _overrides, full_lineage = rehydrate_sdtm_lineage(
        sink_or_config,
        study_key=study_key,
        stream_name=stream_name,
    )
    _provider, _cfg, resolved_study, _resolved_stream, _tx = _resolve_lineage_target(
        sink_or_config,
        study_key=study_key,
        stream_name=stream_name,
    )
    active_form_map = form_domain_map or getattr(sink_or_config, "_active_form_domain_map", None)

    resolved_keys: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for rec in records:
        rid = extract_record_id(rec)
        rid_str = str(rid).strip() if rid is not None and not isinstance(rid, bool) else None
        if rid_str and rid_str in full_lineage:
            entry = dict(full_lineage[rid_str])
            sig = (
                entry.get("study_id"),
                entry.get("domain"),
                entry.get("usubjid"),
                entry.get("seq_col"),
                entry.get("seq_val"),
            )
            if sig not in seen:
                seen.add(sig)
                resolved_keys.append(entry)
            continue

        if isinstance(rec, Mapping):
            comp = extract_sdtm_composite_key(rec)
            if comp is not None and (comp[1] == "DM" or comp[4] is not None):
                if comp not in seen:
                    seen.add(comp)
                    resolved_keys.append(
                        {
                            "study_id": comp[0],
                            "domain": comp[1],
                            "usubjid": comp[2],
                            "seq_col": comp[3],
                            "seq_val": comp[4],
                        }
                    )
                continue

        # Fallback for one-Record-per-Subject DM records where seq_col is None
        raw_dom = sdtm_domain
        if not raw_dom:
            fref_id, fref_key = extract_record_form_ref(rec)
            if isinstance(active_form_map, Mapping):
                raw_dom = active_form_map.get(fref_id) or active_form_map.get(fref_key)
            if not raw_dom and fref_key:
                raw_dom = fref_key
        inferred_dom = resolve_sdtm_domain(raw_dom, fallback="") if raw_dom else ""
        if inferred_dom == "DM":
            if isinstance(rec, Mapping):
                subj = (
                    rec.get("USUBJID")
                    or rec.get("usubjid")
                    or rec.get("subjectKey")
                    or rec.get("subject_key")
                )
                st_id = rec.get("STUDYID") or rec.get("studyKey") or rec.get("study_key")
            else:
                subj = getattr(rec, "subject_key", None) or getattr(rec, "subjectKey", None)
                st_id = getattr(rec, "study_key", None) or getattr(rec, "studyKey", None)
            if subj:
                dm_sig = (str(st_id or resolved_study), "DM", str(subj), None, None)
                if dm_sig not in seen:
                    seen.add(dm_sig)
                    resolved_keys.append(
                        {
                            "study_id": dm_sig[0],
                            "domain": "DM",
                            "usubjid": dm_sig[2],
                            "seq_col": None,
                            "seq_val": None,
                        }
                    )
    return resolved_keys


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

    from imednet.core.context import sdk_governance_context

    with contextlib.ExitStack() as stack:
        stack.enter_context(sdk_governance_context(sdk))
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

        with contextlib.suppress(Exception):
            sink_obj._active_state_provider = provider  # type: ignore[attr-defined]
            sink_obj._active_stream_name = resolved_stream  # type: ignore[attr-defined]
            sink_obj._active_study_key = study_key  # type: ignore[attr-defined]
            sink_obj._active_tx = tx  # type: ignore[attr-defined]
        if tx is not None:
            tx_meta = tx.setdefault("metadata", {})
            bind_fn = getattr(sink_obj, "bind_stream_metadata", None)
            if callable(bind_fn) and isinstance(tx_meta, dict):
                bind_fn(tx_meta)

        reconcile_voided = (
            (not opts["include_deleted"])
            and opts["idempotent"]
            and (
                use_checkpoint
                or watermark is not None
                or bool(getattr(sink_obj, "reconciles_deleted_on_full_sync", False))
            )
        )
        exclude_deleted_upstream = (not opts["include_deleted"]) and (not reconcile_voided)

        study_forms = _try_fetch_study_forms(sdk, study_key) if opts["as_sdtm"] else []
        if study_forms:
            with contextlib.suppress(Exception):
                sink_obj._active_form_domain_map = build_form_sdtm_domain_map(study_forms)  # type: ignore[attr-defined]

        if records_source is not None:
            raw_records = records_source(watermark, exclude_deleted_upstream)
        else:
            allowed_sdtm_form_ids: set[int] | None = None
            allowed_sdtm_form_keys: set[str] | None = None
            if opts["sdtm_domain"] is not None and study_forms:
                matched_forms = filter_forms_by_sdtm_domain(study_forms, opts["sdtm_domain"])
                if not matched_forms:
                    raw_records = iter(())
                else:
                    if len(matched_forms) < len(study_forms):
                        allowed_sdtm_form_ids = {
                            fid
                            for f in matched_forms
                            if isinstance(
                                (
                                    fid := (
                                        f.get("formId") or f.get("form_id")
                                        if isinstance(f, Mapping)
                                        else getattr(f, "form_id", None)
                                    )
                                ),
                                int,
                            )
                            and not isinstance(fid, bool)
                        }
                        allowed_sdtm_form_keys = {
                            str(fkey)
                            for f in matched_forms
                            if (
                                fkey := (
                                    f.get("formKey") or f.get("form_key")
                                    if isinstance(f, Mapping)
                                    else getattr(f, "form_key", None)
                                )
                            )
                        }
                    raw_records = iter_filtered_sdk_records(
                        sdk,
                        study_key,
                        watermark=watermark,
                        record_filter=opts["record_filter"],
                        record_data_filter=opts["record_data_filter"],
                        exclude_deleted_upstream=exclude_deleted_upstream,
                    )
                    if allowed_sdtm_form_ids is not None or allowed_sdtm_form_keys is not None:
                        base_iter = raw_records

                        def _filter_by_domain_forms(it: Iterable[Any]) -> Iterator[Any]:
                            for item in it:
                                fid, fkey = extract_record_form_ref(item)
                                if (
                                    (
                                        allowed_sdtm_form_ids
                                        and fid is not None
                                        and fid in allowed_sdtm_form_ids
                                    )
                                    or (
                                        allowed_sdtm_form_keys
                                        and fkey is not None
                                        and fkey in allowed_sdtm_form_keys
                                    )
                                    or (fid is None and not fkey)
                                ):
                                    yield item

                        raw_records = _filter_by_domain_forms(base_iter)
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
        pending_gate: dict[int, list[tuple[Any, datetime | None]]] = {}
        exported_timestamps: list[datetime] = []

        def _observe_and_route() -> Iterator[Any]:
            nonlocal total_reconciled, voided_batch_idx
            for rec in raw_records:
                rec_ts = extract_record_timestamp(rec)

                if not opts["include_deleted"] and is_record_deleted(rec):
                    if reconcile_voided:
                        if rec_ts is not None:
                            exported_timestamps.append(rec_ts)
                        pending_voided.append(rec)
                        if len(pending_voided) >= opts["batch_size"]:
                            total_reconciled += sink_obj.reconcile_deleted(
                                list(pending_voided),
                                batch_id=f"{prefix}/voided/{voided_batch_idx}",
                            )
                            pending_voided.clear()
                            voided_batch_idx += 1
                    continue

                pending_gate.setdefault(id(rec), []).append((rec, rec_ts))
                yield rec

        def _pop_accepted_candidate(rec: Any) -> None:
            bucket = pending_gate.get(id(rec))
            if bucket:
                bucket.pop(0)
                if not bucket:
                    del pending_gate[id(rec)]
                return
            rec_id = extract_record_id(rec)
            if rec_id is None:
                return
            for obj_id, candidate_bucket in list(pending_gate.items()):
                for idx, (cand_rec, _) in enumerate(candidate_bucket):
                    if extract_record_id(cand_rec) == rec_id:
                        candidate_bucket.pop(idx)
                        if not candidate_bucket:
                            del pending_gate[obj_id]
                        return

        def _track_accepted(records_iter: Iterable[Any]) -> Iterator[Any]:
            for rec in records_iter:
                _pop_accepted_candidate(rec)
                rec_ts = extract_record_timestamp(rec)
                if rec_ts is not None:
                    exported_timestamps.append(rec_ts)
                yield rec

        filtered_records = _track_accepted(gate_fn(sdk, study_key, _observe_and_route(), config))
        check_standards = (
            opts["standards_profile"] is not None or opts["min_standards_score"] is not None
        )
        for index, batch in enumerate(batches_fn(filtered_records, opts["batch_size"])):
            batch_id = f"{prefix}/{index}"
            if check_standards and batch:
                _enforce_batch_standards_readiness(
                    batch,
                    batch_id=batch_id,
                    study_key=study_key,
                    config=config,
                    standards_profile=opts["standards_profile"],
                    min_standards_score=opts["min_standards_score"],
                    sdtm_domain=opts["sdtm_domain"],
                )
            total_written += sink_obj.write_batch(batch, batch_id=batch_id)

        if pending_voided:
            total_reconciled += sink_obj.reconcile_deleted(
                list(pending_voided),
                batch_id=f"{prefix}/voided/{voided_batch_idx}",
            )
            pending_voided.clear()

        rejected_timestamps = [rec_ts for bucket in pending_gate.values() for _, rec_ts in bucket]
        pending_gate.clear()

        if rejected_timestamps:
            if any(rec_ts is None for rec_ts in rejected_timestamps):
                max_ts = None
            else:
                min_rejected_ts = min(ts for ts in rejected_timestamps if ts is not None)
                eligible_ts = [ts for ts in exported_timestamps if ts <= min_rejected_ts]
                max_ts = max(eligible_ts) if eligible_ts else None
        elif exported_timestamps:
            max_ts = max(exported_timestamps)

        if tx is not None:
            tx["records_processed"] = total_written + total_reconciled
            if max_ts is not None:
                tx["new_timestamp"] = max_ts

    return total_written


def is_incremental_sink_config(config: Any) -> bool:
    """Return True when ``config`` activates incremental, watermark, or stateful sync."""
    return bool(
        getattr(config, "incremental", False) is True
        or getattr(config, "modified_since", None) is not None
        or getattr(config, "state_provider", None) is not None
    )


def bind_sdtm_stream_metadata(
    metadata: dict[str, Any],
    seq_counters: dict[str, int],
    record_seqs: dict[str, int] | None = None,
) -> None:
    """Hydrate and bind mutable SDTM sequence counters into ``StreamState.metadata``."""
    raw_counters = metadata.get("sdtm_seq_counters")
    seq_counters.clear()
    if isinstance(raw_counters, dict):
        for subj_key, val in raw_counters.items():
            if isinstance(subj_key, str) and isinstance(val, int) and not isinstance(val, bool):
                seq_counters[subj_key] = val
    metadata["sdtm_seq_counters"] = seq_counters

    if record_seqs is not None:
        raw_rec_seqs = metadata.get("sdtm_record_seqs")
        record_seqs.clear()
        if isinstance(raw_rec_seqs, dict):
            for rec_key, val in raw_rec_seqs.items():
                if isinstance(rec_key, str) and isinstance(val, int) and not isinstance(val, bool):
                    record_seqs[rec_key] = val
        metadata["sdtm_record_seqs"] = record_seqs


__all__ = [
    "DEFAULT_MAX_BUFFERED_RECORDS",
    "STATE_PROVIDER_ENTRY_POINT_GROUP",
    "BoundedFormBufferRouter",
    "ExportSink",
    "SinkConfig",
    "StateProviderProtocol",
    "TabularBatchStamp",
    "_redact_uri",
    "_require_optional_dep",
    "apply_quality_gate",
    "bind_sdtm_stream_metadata",
    "build_form_sdtm_domain_map",
    "build_record_query_branches",
    "commit_sdtm_lineage",
    "deduplicate_sdtm_rows",
    "evaluate_standards_readiness",
    "extract_record_form_ref",
    "extract_record_id",
    "extract_record_timestamp",
    "extract_sdtm_composite_key",
    "filter_forms_by_sdtm_domain",
    "get_state_provider",
    "get_tabular_batch_stamp",
    "has_valid_tabular_batch_stamp",
    "is_incremental_sink_config",
    "is_record_deleted",
    "iter_batches",
    "iter_filtered_sdk_records",
    "normalize_phi_keys_snapshot",
    "normalize_sink_value",
    "normalize_watermark_datetime",
    "redact_batch_records",
    "rehydrate_sdtm_lineage",
    "resolve_form_sdtm_domain",
    "resolve_max_buffered_records",
    "resolve_voided_sdtm_keys",
    "run_streaming_export",
    "stamp_tabular_batch",
    "validate_max_buffered_records",
]
