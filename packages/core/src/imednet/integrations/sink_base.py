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
import inspect
import itertools
import logging
import math
import re
import sys
from abc import ABC, abstractmethod
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from importlib import import_module
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, Self, cast, runtime_checkable
from unittest.mock import NonCallableMock

from tenacity import wait_exponential

from imednet.core.operations.circuit_breaker import CircuitBreaker
from imednet.core.operations.executor import OperationRetryPolicy
from imednet.errors import ExportConfigurationError, ExportError
from imednet.integrations.enrichment import resolve_sink_phi_keys
from imednet.models.sdtm import (
    deduplicate_sdtm_rows,
    extract_sdtm_composite_key,
    format_nested_variable_name,
    prefix_colliding_variable_name,
)
from imednet.models.standards import (
    _SDTM_RECORDS_SORT,
    PROFILE_REGISTRY,
    SDTM_DOMAIN_SPECS,
    StandardsProfile,
    StandardsReadinessReport,
    StandardsReadinessValidator,
    async_iter_subject_ordered_sdtm_records,
    build_form_sdtm_domain_map,
    extract_record_form_ref,
    filter_forms_by_sdtm_domain,
    format_iso8601_dtc,
    iter_subject_ordered_sdtm_records,
    merge_form_ids_into_record_filter,
    normalize_naming_convention,
    record_matches_sdtm_domain,
    resolve_form_sdtm_domain,
    resolve_sdtm_domain,
    resolve_standards_profile,
    validate_cdash_variable,
)
from imednet.utils.async_bridge import (
    SyncToAsyncFacadeAdapter,
    call_async_facade_method,
    is_async_facade,
    run_sync_over_async,
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
    build_study_sensitivity_registry,
    global_sensitivity_registry,
    mask_clinical_phi,
    sanitize_diagnostic_text,
)
from imednet.utils.tabular import MASKED_SENTINEL
from imednet.utils.url import redact_url_query

from ._record_timestamp_bounds import (
    RECORD_TIMESTAMP_ATTRIBUTES,
    _combine_branch_with_watermark,
    _merge_and_condition,
    normalize_watermark_datetime,
)

if TYPE_CHECKING:
    from imednet.spi.facade import AsyncImednetFacade, ImednetFacade

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


def get_state_provider(
    ledger_path: str | Path | None = None,
    *,
    provider_name: str | None = None,
) -> StateProviderProtocol:
    """Discover and instantiate a pluggable checkpoint state provider at runtime."""
    from importlib.metadata import entry_points

    target_name = provider_name or "default"
    eps = list(entry_points(group=STATE_PROVIDER_ENTRY_POINT_GROUP))
    if ledger_path is not None and target_name in ("default", "airflow"):
        file_eps = [ep for ep in eps if ep.name == "file"]
        if file_eps:
            target_name = "file"
            matched = file_eps
        else:
            default_eps = [ep for ep in eps if ep.name == "default"]
            if default_eps:
                target_name = "default"
                matched = default_eps
            else:
                matched = [ep for ep in eps if ep.name == target_name]
    else:
        matched = [ep for ep in eps if ep.name == target_name]
    if not matched and provider_name is None and eps:
        matched = [eps[0]]
    if not matched:
        raise ImportError(
            f"Checkpoint state provider '{target_name}' requires an installed workflows plugin. "
            "Install 'imednet-workflows' or 'imednet[export]'."
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
    as_sdtm: bool = field(default=cast(bool, None))
    sdtm_domain: str | None = None
    standards_profile: str | Any | None = None
    min_standards_score: float | None = None
    naming_convention: str | None = None
    preserve_record_id: bool | None = None

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
        if self.preserve_record_id is not None and not isinstance(self.preserve_record_id, bool):
            raise ValueError("preserve_record_id must be a boolean or None")
        if not self.idempotent and (self.incremental or self.state_provider is not None):
            raise ExportConfigurationError(
                "SinkConfig(idempotent=False) cannot be combined with incremental=True or "
                "state_provider because inclusive dateModified>= watermarks require "
                "idempotent=True."
            )
        validate_max_buffered_records(self.max_buffered_records)
        if self.as_sdtm is None:
            object.__setattr__(self, "_as_sdtm_explicit", False)
            self.as_sdtm = False
        else:
            if not isinstance(self.as_sdtm, bool):
                raise ValueError("as_sdtm must be a boolean")
            object.__setattr__(self, "_as_sdtm_explicit", True)
        if self.sdtm_domain is not None:
            if not isinstance(self.sdtm_domain, str) or not self.sdtm_domain.strip():
                raise ValueError("sdtm_domain must be a non-empty string when provided")
            self.sdtm_domain = resolve_sdtm_domain(self.sdtm_domain)
            if not getattr(self, "_as_sdtm_explicit", False):
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


def _mask_series_containers_default_phi(
    series: Any,
    *,
    registry: SensitivityRegistry | None = None,
) -> Any:
    """Mask nested container cells in a pandas Series without invoking scalar mask_clinical_phi."""
    values = series.tolist()
    if not any(isinstance(v, _CONTAINER_CELL_TYPES) for v in values):
        return series
    active_reg = registry if registry is not None else global_sensitivity_registry
    pd_mod = sys.modules.get("pandas")
    new_vals = [
        mask_clinical_phi(v, registry=active_reg) if isinstance(v, _CONTAINER_CELL_TYPES) else v
        for v in values
    ]
    if pd_mod is not None:
        return pd_mod.Series(new_vals, index=series.index, dtype=object)
    return series.apply(
        lambda v: (
            mask_clinical_phi(v, registry=active_reg) if isinstance(v, _CONTAINER_CELL_TYPES) else v
        )
    )


def _is_sdtm_birth_date_key(raw_key: str) -> bool:
    """Return True if *raw_key* represents an SDTM birth-date variable or source alias."""
    norm = _normalize_key(raw_key)
    if not norm:
        return False
    return norm in _SDTM_BIRTH_SOURCE_KEYS or norm.endswith("brthdtc")


def _redact_mapping_for_sdtm(
    mapping_val: Mapping[Any, Any],
    *,
    registry: SensitivityRegistry | None = None,
) -> dict[Any, Any]:
    """Redact PHI across a mapping while truncating birth-date fields to ISO year or None."""
    active_reg = registry if registry is not None else global_sensitivity_registry
    out: dict[Any, Any] = {}
    for k, v in mapping_val.items():
        key_str = str(k)
        if _is_sdtm_birth_date_key(key_str) and not active_reg.is_exempt(key_str):
            out[k] = format_iso8601_dtc(v, mask_phi=True)
        elif active_reg.is_sensitive(key_str):
            out[k] = MASKED_SENTINEL
        elif isinstance(v, Mapping):
            out[k] = _redact_mapping_for_sdtm(v, registry=active_reg)
        elif isinstance(v, list):
            out[k] = [
                _redact_mapping_for_sdtm(elem, registry=active_reg)
                if isinstance(elem, Mapping)
                else mask_clinical_phi(elem, registry=active_reg)
                for elem in v
            ]
        elif isinstance(v, tuple):
            out[k] = tuple(
                _redact_mapping_for_sdtm(elem, registry=active_reg)
                if isinstance(elem, Mapping)
                else mask_clinical_phi(elem, registry=active_reg)
                for elem in v
            )
        else:
            out[k] = mask_clinical_phi(v, registry=active_reg)
    return out


def _redact_dataframe_batch(
    df: Any,
    pd_mod: Any,
    *,
    as_sdtm: bool = False,
    registry: SensitivityRegistry | None = None,
    phi_keys: Collection[str] | None = None,
) -> Any:
    """Return a PHI-redacted copy of a pandas DataFrame without mutating caller state."""
    active_reg = registry if registry is not None else global_sensitivity_registry
    if getattr(df, "attrs", {}).get("imednet_sdtm_redacted") is True and (
        not phi_keys or has_valid_tabular_batch_stamp(df, mask_phi=True, phi_keys=phi_keys)
    ):
        return df

    masked = df.copy()
    birth_cols = (
        [
            col
            for col in masked.columns
            if _is_sdtm_birth_date_key(str(col)) and not active_reg.is_exempt(str(col))
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
            if col not in birth_col_set and active_reg.is_sensitive(str(col))
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
            masked[col] = target.apply(
                lambda s: _mask_series_containers_default_phi(s, registry=active_reg)
            )
        else:
            masked[col] = _mask_series_containers_default_phi(target, registry=active_reg)
    if as_sdtm:
        masked.attrs["imednet_sdtm_redacted"] = True
    return masked


def _redact_single_record(
    item: Any,
    *,
    as_sdtm: bool = False,
    registry: SensitivityRegistry | None = None,
) -> Any:
    """Redact PHI on a single Mapping, Pydantic model, or record-like object."""
    active_reg = registry if registry is not None else global_sensitivity_registry
    if isinstance(item, Mapping):
        if as_sdtm:
            return _redact_mapping_for_sdtm(item, registry=active_reg)
        return mask_clinical_phi(dict(item), registry=active_reg)
    if isinstance(item, NonCallableMock):
        return item

    model_copy = getattr(type(item), "model_copy", None)
    model_fields = getattr(type(item), "model_fields", None)
    if callable(model_copy) and isinstance(model_fields, Mapping):
        updates: dict[str, Any] = {}
        record_data = getattr(item, "record_data", None)
        if isinstance(record_data, dict):
            masked_rd = (
                _redact_mapping_for_sdtm(record_data, registry=active_reg)
                if as_sdtm
                else mask_clinical_phi(record_data, registry=active_reg)
            )
            if masked_rd != record_data:
                updates["record_data"] = masked_rd
        for field_name in model_fields:
            if field_name == "record_data" or active_reg.is_exempt(field_name):
                continue
            if as_sdtm and _is_sdtm_birth_date_key(field_name):
                val = getattr(item, field_name, None)
                redacted_year = format_iso8601_dtc(val, mask_phi=True)
                if redacted_year != val:
                    updates[field_name] = redacted_year
            elif active_reg.is_sensitive(field_name):
                val = getattr(item, field_name, None)
                if val is not None and val != MASKED_SENTINEL:
                    updates[field_name] = MASKED_SENTINEL
        return item.model_copy(update=updates) if updates else item

    record_data = getattr(item, "record_data", None)
    if isinstance(record_data, dict):
        masked_rd = (
            _redact_mapping_for_sdtm(record_data, registry=active_reg)
            if as_sdtm
            else mask_clinical_phi(record_data, registry=active_reg)
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
    study_key: str | None = None,
    config: SinkConfig | None = None,
    phi_keys: Collection[str] | None = None,
) -> Any:
    """Apply Study-scoped SensitivityRegistry PHI redaction across a batch of records or DataFrame."""
    if not mask_phi or records is None:
        return records
    if isinstance(records, (str, bytes)):
        return records

    pd_mod = sys.modules.get("pandas")
    is_df = pd_mod is not None and isinstance(records, pd_mod.DataFrame)
    materialized = records
    if (
        not is_df
        and not isinstance(records, (Mapping, Collection))
        and isinstance(records, Iterable)
    ):
        materialized = list(records)

    resolved_phi = resolve_sink_phi_keys(
        study_key,
        config=config,
        records=materialized,
        phi_keys=phi_keys,
    )
    active_registry = build_study_sensitivity_registry(resolved_phi)

    if is_df:
        return _redact_dataframe_batch(
            materialized,
            pd_mod,
            as_sdtm=as_sdtm,
            registry=active_registry,
            phi_keys=resolved_phi,
        )

    if isinstance(materialized, Mapping):
        if as_sdtm:
            return _redact_mapping_for_sdtm(materialized, registry=active_registry)
        return mask_clinical_phi(dict(materialized), registry=active_registry)
    if isinstance(materialized, tuple):
        return tuple(
            _redact_single_record(item, as_sdtm=as_sdtm, registry=active_registry)
            for item in materialized
        )
    if isinstance(materialized, Iterable):
        return [
            _redact_single_record(item, as_sdtm=as_sdtm, registry=active_registry)
            for item in materialized
        ]
    return materialized


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

    def redact_batch(
        self,
        records: Sequence[Any],
        *,
        study_key: str | None = None,
        config: SinkConfig | None = None,
        phi_keys: Collection[str] | None = None,
    ) -> Any:
        """Redact sensitive clinical PHI across ``records`` when ``config.mask_phi`` is enabled."""
        active_cfg = config if config is not None else getattr(self, "config", None)
        should_mask = getattr(active_cfg, "mask_phi", True) is not False
        is_sdtm = resolve_effective_as_sdtm(active_cfg)
        if is_sdtm and getattr(self, "_delegates_sdtm_redaction", False):
            return records
        active_sk = study_key if study_key is not None else getattr(active_cfg, "study_key", None)
        return redact_batch_records(
            records,
            mask_phi=should_mask,
            as_sdtm=is_sdtm,
            study_key=active_sk,
            config=active_cfg,
            phi_keys=phi_keys,
        )

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

    def __enter__(self) -> Self:
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


class AsyncExportSink(ABC):
    """Abstract base class for native asynchronous export sinks.

    Subclasses **must** implement :meth:`write_batch`, :meth:`flush`, and
    :meth:`close` as coroutines.  The async context-manager protocol and
    automatic PHI redaction wrapper are provided by this class.
    """

    _skip_auto_redact_batch: bool = False
    _delegates_sdtm_redaction: bool = False
    reconciles_deleted_on_full_sync: bool = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Automatically wrap concrete ``write_batch`` coroutines with PHI redaction."""
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("_skip_auto_redact_batch", False):
            return
        raw_write_batch = cls.__dict__.get("write_batch")
        if raw_write_batch is None or getattr(raw_write_batch, "__isabstractmethod__", False):
            return
        if getattr(raw_write_batch, "_phi_redaction_wrapped", False):
            return

        @functools.wraps(raw_write_batch)
        async def _wrapped_write_batch(
            self: AsyncExportSink,
            records: Sequence[Any],
            *,
            batch_id: str,
        ) -> int:
            redacted = self.redact_batch(records)
            return cast(int, await raw_write_batch(self, redacted, batch_id=batch_id))

        _wrapped_write_batch._phi_redaction_wrapped = True  # type: ignore[attr-defined]
        cls.write_batch = _wrapped_write_batch  # type: ignore[method-assign]

    def __init__(self, config: SinkConfig) -> None:
        """Initialize the asynchronous export sink with a configuration."""
        self.config: SinkConfig = config
        self._circuit_breaker: CircuitBreaker = CircuitBreaker()
        self._sdtm_lineage: dict[str, dict[str, Any]] = {}
        self._sdtm_seq_watermarks: dict[str, int] = {}

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

    def redact_batch(
        self,
        records: Sequence[Any],
        *,
        study_key: str | None = None,
        config: SinkConfig | None = None,
        phi_keys: Collection[str] | None = None,
    ) -> Any:
        """Redact sensitive clinical PHI across ``records`` when ``config.mask_phi`` is enabled."""
        active_cfg = config if config is not None else getattr(self, "config", None)
        should_mask = getattr(active_cfg, "mask_phi", True) is not False
        is_sdtm = resolve_effective_as_sdtm(active_cfg)
        if is_sdtm and getattr(self, "_delegates_sdtm_redaction", False):
            return records
        active_sk = study_key if study_key is not None else getattr(active_cfg, "study_key", None)
        return redact_batch_records(
            records,
            mask_phi=should_mask,
            as_sdtm=is_sdtm,
            study_key=active_sk,
            config=active_cfg,
            phi_keys=phi_keys,
        )

    @abstractmethod
    async def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Asynchronously write one batch of records to the destination."""
        ...

    @abstractmethod
    async def flush(self) -> None:
        """Asynchronously flush any internal buffers to the destination."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Asynchronously release all resources held by this sink."""
        ...

    async def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Asynchronously reconcile newly voided records in idempotent targets."""
        _ = (records, batch_id)
        return 0

    async def _execute_with_retry(
        self,
        operation_name: str,
        batch_id: str,
        execute_fn: Callable[[], Awaitable[int]],
    ) -> int:
        """Execute an async batch operation with configured retries and telemetry."""
        from imednet.core.operations.executor import UniversalExecutor
        from imednet.core.retry import RetryConfig
        from imednet.errors import ExportBatchError

        attempts = 0
        last_raw_exc: Exception | None = None

        async def execute_batch() -> int:
            nonlocal attempts, last_raw_exc
            attempts += 1
            try:
                return await execute_fn()
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
            return await executor.execute_async(execute_batch)
        except Exception as exec_err:
            root_exc = last_raw_exc if last_raw_exc is not None else exec_err
            sanitized_cause = _sanitize_batch_cause(root_exc)
            raise ExportBatchError(
                f"Batch {batch_id!r} failed after {attempts} attempts.",
                batch_id=batch_id,
            ) from sanitized_cause

    async def __aenter__(self) -> Self:
        """Enter the async context manager, returning the sink instance."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the async context manager, flushing and closing the sink."""
        try:
            if exc_type is None:
                await self.flush()
        finally:
            await self.close()


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
    study_config: Any = None,
    pipeline: Any = None,
    lookup_keys_lower: frozenset[str] = frozenset(),
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return ``(domain, raw_data, canonical_data)`` without mutating ``record``."""
    raw_domain: str | None = None
    subject_val: Any = None
    raw_data: dict[str, Any] = {}

    if hasattr(record, "record_data") and getattr(record, "record_data", None) is not None:
        rd = getattr(record, "record_data", None)
        raw_data = dict(rd) if isinstance(rd, Mapping) else {}
        raw_domain = getattr(record, "form_key", None) or getattr(record, "formKey", None)
        subject_val = getattr(record, "subject_key", None) or getattr(record, "subjectKey", None)
    elif isinstance(record, Mapping):
        raw_domain = (
            record.get("DOMAIN")
            or record.get("domain")
            or record.get("formKey")
            or record.get("form_key")
        )
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
        subject_val = getattr(record, "subject_key", None) or getattr(record, "subjectKey", None)
        rd = (
            getattr(record, "record_data", None)
            or getattr(record, "recordData", None)
            or getattr(record, "data", None)
        )
        raw_data = dict(rd) if isinstance(rd, Mapping) else {}

    mapped_domain: str | None = None
    resolved_from_form = resolve_form_sdtm_domain(
        record,
        study_config=study_config,
        form_domain_map=form_domain_map,
        fallback="",
    )
    if not resolved_from_form and raw_domain is not None:
        resolved_from_form = resolve_form_sdtm_domain(
            raw_domain,
            study_config=study_config,
            form_domain_map=form_domain_map,
            fallback="",
        )
    if resolved_from_form:
        mapped_domain = resolved_from_form

    raw_resolved = (
        resolve_sdtm_domain(str(raw_domain), fallback="")
        if isinstance(raw_domain, str) and raw_domain.strip()
        else ""
    )
    if mapped_domain:
        domain = resolve_sdtm_domain(mapped_domain, fallback="AE")
    elif raw_resolved and (raw_resolved in SDTM_DOMAIN_SPECS or form_domain_map is not None):
        domain = raw_resolved
    else:
        domain = resolve_sdtm_domain(configured_domain or raw_domain, fallback="AE")

    source_data: dict[str, Any] = dict(raw_data)
    if pipeline is not None and hasattr(pipeline, "process"):
        try:
            enriched = pipeline.process(
                dict(raw_data),
                mask_phi=False,
                domain=domain,
                form_key=str(raw_domain) if raw_domain else None,
            )
        except TypeError:
            enriched = pipeline.process(dict(raw_data), mask_phi=False)
        if isinstance(enriched, Mapping):
            source_data = dict(enriched)

    canonical: dict[str, Any] = dict(source_data)

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
        lower_raw = {str(k).lower(): v for k, v in source_data.items()}
        for sdtm_var, aliases in spec["aliases"].items():
            if not aliases:
                continue
            primary_key = aliases[0]
            found_val: Any = None
            matched_cand: str | None = None
            has_val = False
            if primary_key in canonical and canonical[primary_key] is not None:
                found_val = canonical[primary_key]
                has_val = True
            else:
                for cand in [sdtm_var, *aliases]:
                    if cand in source_data:
                        found_val = source_data[cand]
                        matched_cand = cand
                        has_val = True
                        break
                    if cand.lower() in lower_raw:
                        found_val = lower_raw[cand.lower()]
                        matched_cand = cand
                        has_val = True
                        break
            if has_val:
                cand_governed_by_lookup = (
                    matched_cand is not None
                    and matched_cand.lower() in lookup_keys_lower
                    and primary_key.lower() not in lookup_keys_lower
                )
                if not cand_governed_by_lookup:
                    canonical.setdefault(primary_key, found_val)
                if domain == "DM" and sdtm_var == "BRTHDTC":
                    canonical.setdefault("birthDate", found_val)
                elif (
                    domain == "DM"
                    and sdtm_var == "ETHNIC"
                    and not (
                        matched_cand is not None
                        and matched_cand.lower() in lookup_keys_lower
                        and "ethnicity" not in lookup_keys_lower
                    )
                ):
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
    from imednet.integrations.enrichment import EnrichmentPipeline, resolve_study_configuration

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
    study_cfg: Any = (
        extra.get("study_config")
        if isinstance(extra, Mapping) and extra.get("study_config") is not None
        else resolve_study_configuration(study_key)
    )
    pipeline: Any = extra.get("enrichment_pipeline") if isinstance(extra, Mapping) else None
    if pipeline is None and study_cfg is not None:
        pipeline = EnrichmentPipeline(study_cfg)

    merged_lookups: dict[str, dict[str, str]] = {}
    cfg_lookups = getattr(study_cfg, "terminology_lookups", None)
    pipeline_has_terminology = bool(pipeline is not None and getattr(pipeline, "terminology", None))
    if isinstance(cfg_lookups, Mapping):
        for tbl_name, tbl_vals in cfg_lookups.items():
            if isinstance(tbl_vals, Mapping):
                copied_tbl = {str(k): str(v) for k, v in tbl_vals.items()}
                if pipeline_has_terminology:
                    upper_keys = {k.strip().upper() for k in copied_tbl}
                    for mapped_val in list(copied_tbl.values()):
                        if mapped_val.strip() and mapped_val.strip().upper() not in upper_keys:
                            copied_tbl.setdefault(mapped_val, mapped_val)
                            upper_keys.add(mapped_val.strip().upper())
                merged_lookups[str(tbl_name)] = copied_tbl
    extra_lookups = (
        extra.get("terminology_lookups")
        if isinstance(extra, Mapping) and isinstance(extra.get("terminology_lookups"), Mapping)
        else None
    )
    for explicit_map in (extra_lookups, terminology_lookups):
        if isinstance(explicit_map, Mapping):
            for tbl_name, tbl_vals in explicit_map.items():
                if isinstance(tbl_vals, Mapping):
                    dest_tbl = merged_lookups.setdefault(str(tbl_name), {})
                    explicit_pairs = {str(k): str(v) for k, v in tbl_vals.items()}
                    dest_tbl.update(explicit_pairs)
                    if (
                        pipeline_has_terminology
                        and isinstance(cfg_lookups, Mapping)
                        and tbl_name in cfg_lookups
                    ):
                        upper_keys = {k.strip().upper() for k in dest_tbl}
                        for mapped_val in list(explicit_pairs.values()):
                            if mapped_val.strip() and mapped_val.strip().upper() not in upper_keys:
                                dest_tbl.setdefault(mapped_val, mapped_val)
                                upper_keys.add(mapped_val.strip().upper())
    lookups: dict[str, dict[str, str]] | None = merged_lookups or None
    lookup_keys_lower: frozenset[str] = (
        frozenset(k.lower() for k in lookups) if lookups is not None else frozenset()
    )

    effective_form_map: dict[Any, str] = build_form_sdtm_domain_map(None, study_config=study_cfg)
    if form_domain_map:
        effective_form_map.update(form_domain_map)

    profile = resolve_standards_profile(raw_prof)
    validator = StandardsReadinessValidator(profile=profile)

    target_dom = (
        resolve_sdtm_domain(cfg_domain, fallback="")
        if isinstance(cfg_domain, str) and cfg_domain.strip()
        else ""
    )
    records_by_domain: dict[str, list[dict[str, Any]]] = {}
    cdash_warnings: list[str] = []
    seen_cdash: set[tuple[str, str]] = set()

    for rec in records:
        domain, raw_data, canonical = _extract_record_for_standards(
            rec,
            configured_domain=cfg_domain,
            form_domain_map=effective_form_map or None,
            study_config=study_cfg,
            pipeline=pipeline,
            lookup_keys_lower=lookup_keys_lower,
        )
        if target_dom and domain != target_dom:
            continue
        records_by_domain.setdefault(domain, []).append(canonical)
        for field_key in raw_data:
            pair = (domain, str(field_key))
            if pair in seen_cdash:
                continue
            seen_cdash.add(pair)
            valid_cdash, cdash_msg = validate_cdash_variable(str(field_key), domain)
            if not valid_cdash and cdash_msg:
                cdash_warnings.append(cdash_msg)

    if target_dom and not records_by_domain:
        records_by_domain[target_dom] = []

    report = validator.score_records(
        records_by_domain=records_by_domain,
        terminology_lookups=lookups,
    )
    if cdash_warnings:
        report.warnings = [*report.warnings, *cdash_warnings]
    if config is not None and getattr(config, "mask_phi", True) is True and report.warnings:
        report.warnings = [sanitize_diagnostic_text(mask_clinical_phi(w)) for w in report.warnings]
    return report


def _populate_quality_gate_schema(
    schema: Any,
    study_key: str,
    variables: Iterable[Any],
    forms: Iterable[Any] | None,
) -> None:
    """Populate a SchemaCache / AsyncSchemaCache from explicit ``variables`` and ``forms``."""
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

    schema.populate(prepared_vars, study_key=study_key)
    for fid, fk_val in form_key_by_id.items():
        schema._form_id_to_key[(study_key, fid)] = fk_val


def _evaluate_quality_gate_record(
    record: Any,
    *,
    schema: Any,
    study_key: str,
    config: SinkConfig,
    cfg_domain: str | None,
    study_cfg: Any,
    gate_form_map: Mapping[Any, str],
    seen_gate_cdash: set[tuple[str, str]],
) -> bool:
    """Return True if ``record`` passes schema readiness and log optional CDASH warnings."""
    from imednet.core.context import reset_study_context, set_study_context
    from imednet.validation.cache import calculate_readiness_score

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
            fk = schema.form_key_from_id(fid)

        if not fk:
            logger.info("Dropped record %s: Unknown form", rec_dict.get("record_id", "Unknown"))
            return False

        data = rec_dict.get("record_data")
        if data is None:
            data = rec_dict.get("recordData")
        if data is None:
            data = rec_dict.get("data", {})
        if not isinstance(data, Mapping):
            data = {}
        score, reasons = calculate_readiness_score(schema, fk, dict(data))
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
        return False

    if isinstance(data, Mapping):
        domain_for_cdash = (
            resolve_sdtm_domain(cfg_domain, fallback="AE")
            if cfg_domain
            else resolve_form_sdtm_domain(
                str(fk),
                study_config=study_cfg,
                form_domain_map=gate_form_map or None,
                fallback="AE",
            )
        )
        for field_key in data:
            pair = (domain_for_cdash, str(field_key))
            if pair not in seen_gate_cdash:
                seen_gate_cdash.add(pair)
                valid_cdash, cdash_msg = validate_cdash_variable(str(field_key), domain_for_cdash)
                if not valid_cdash and cdash_msg:
                    logger.warning("CDASH naming warning (%s): %s", domain_for_cdash, cdash_msg)
    return True


def apply_quality_gate(
    sdk: Any,
    study_key: str,
    records: Iterable[Any],
    config: SinkConfig,
    *,
    variables: Iterable[Any] | None = None,
    forms: Iterable[Any] | None = None,
) -> Iterator[Any]:
    """Filter records based on schema readiness and optional CDASH naming checks."""
    cfg_profile = getattr(config, "standards_profile", None)
    cfg_domain = getattr(config, "sdtm_domain", None)

    if not config.quality_gate_enabled and cfg_profile is None:
        yield from records
        return

    from imednet.integrations.enrichment import resolve_study_configuration

    extra = getattr(config, "extra", None)
    study_cfg: Any = (
        extra.get("study_config")
        if isinstance(extra, Mapping) and extra.get("study_config") is not None
        else resolve_study_configuration(study_key)
    )
    gate_form_map = build_form_sdtm_domain_map(forms, study_config=study_cfg)

    if not config.quality_gate_enabled:
        seen_cdash: set[tuple[str, str]] = set()
        for record in records:
            domain, raw_data, _ = _extract_record_for_standards(
                record,
                configured_domain=cfg_domain,
                form_domain_map=gate_form_map or None,
                study_config=study_cfg,
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

    from imednet.validation.cache import SchemaValidator

    validator = SchemaValidator(sdk)
    if variables is None:
        validator.refresh(study_key)
    else:
        _populate_quality_gate_schema(validator.schema, study_key, variables, forms)

    seen_gate_cdash: set[tuple[str, str]] = set()
    dropped_count = 0

    for record in records:
        if _evaluate_quality_gate_record(
            record,
            schema=validator.schema,
            study_key=study_key,
            config=config,
            cfg_domain=cfg_domain,
            study_cfg=study_cfg,
            gate_form_map=gate_form_map,
            seen_gate_cdash=seen_gate_cdash,
        ):
            yield record
        else:
            dropped_count += 1

    if dropped_count > 0:
        logger.info("Quality gate dropped %d records in total.", dropped_count)


async def async_apply_quality_gate(
    sdk: AsyncImednetFacade,
    study_key: str,
    records: AsyncIterable[Any] | Iterable[Any],
    config: SinkConfig,
    *,
    variables: Iterable[Any] | None = None,
    forms: Iterable[Any] | None = None,
) -> AsyncIterator[Any]:
    """Asynchronously filter records based on schema readiness and optional CDASH checks."""

    async def _as_async(source: AsyncIterable[Any] | Iterable[Any]) -> AsyncIterator[Any]:
        if isinstance(source, AsyncIterable):
            async for item in source:
                yield item
        else:
            for item in source:
                yield item

    cfg_profile = getattr(config, "standards_profile", None)
    cfg_domain = getattr(config, "sdtm_domain", None)

    if not config.quality_gate_enabled and cfg_profile is None:
        async for record in _as_async(records):
            yield record
        return

    from imednet.integrations.enrichment import resolve_study_configuration

    extra = getattr(config, "extra", None)
    study_cfg: Any = (
        extra.get("study_config")
        if isinstance(extra, Mapping) and extra.get("study_config") is not None
        else resolve_study_configuration(study_key)
    )
    gate_form_map = build_form_sdtm_domain_map(forms, study_config=study_cfg)

    if not config.quality_gate_enabled:
        seen_cdash: set[tuple[str, str]] = set()
        async for record in _as_async(records):
            domain, raw_data, _ = _extract_record_for_standards(
                record,
                configured_domain=cfg_domain,
                form_domain_map=gate_form_map or None,
                study_config=study_cfg,
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

    from imednet.validation.cache import AsyncSchemaValidator

    validator = AsyncSchemaValidator(sdk)
    if variables is None:
        await validator.refresh(study_key)
    else:
        _populate_quality_gate_schema(validator.schema, study_key, variables, forms)

    seen_gate_cdash: set[tuple[str, str]] = set()
    dropped_count = 0

    async for record in _as_async(records):
        if _evaluate_quality_gate_record(
            record,
            schema=validator.schema,
            study_key=study_key,
            config=config,
            cfg_domain=cfg_domain,
            study_cfg=study_cfg,
            gate_form_map=gate_form_map,
            seen_gate_cdash=seen_gate_cdash,
        ):
            yield record
        else:
            dropped_count += 1

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


def async_iter_batches(
    records: AsyncIterable[Any] | Iterable[Any], batch_size: int
) -> AsyncIterator[list[Any]]:
    """Asynchronously yield ``records`` lazily in fixed-size batches of ``batch_size``."""
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0")

    async def _generate() -> AsyncIterator[list[Any]]:
        batch: list[Any] = []
        if isinstance(records, AsyncIterable):
            async for item in records:
                batch.append(item)
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
        else:
            for item in records:
                batch.append(item)
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
        if batch:
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


class AsyncBoundedFormBufferRouter:
    """Asynchronously buffer per-form records and flush on batch size or memory ceiling."""

    def __init__(
        self,
        form_keys: Sequence[str],
        *,
        batch_size: int,
        max_buffered_records: int | None = None,
        on_flush_active: Callable[[str, list[Any], int], Awaitable[None]],
        on_flush_voided: Callable[[str, list[Any], int], Awaitable[None]] | None = None,
    ) -> None:
        """Initialize per-form async buffers and global ceiling tracking."""
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

    async def add_active(self, form_key: str, record: Any) -> None:
        """Append an active record to ``form_key`` and flush if thresholds are met."""
        buf = self._active[form_key]
        buf.append(record)
        self.total_buffered += 1
        self.peak_buffered = max(self.peak_buffered, self.total_buffered)
        if len(buf) >= self.batch_size:
            await self._flush_active_form(form_key)
        elif self.total_buffered >= self.ceiling:
            await self._evict_largest()

    async def add_voided(self, form_key: str, record: Any) -> None:
        """Append a voided record to ``form_key`` and flush if thresholds are met."""
        if self._on_flush_voided is None:
            return
        buf = self._voided[form_key]
        buf.append(record)
        self.total_buffered += 1
        self.peak_buffered = max(self.peak_buffered, self.total_buffered)
        if len(buf) >= self.batch_size:
            await self._flush_voided_form(form_key)
        elif self.total_buffered >= self.ceiling:
            await self._evict_largest()

    async def _flush_active_form(self, form_key: str) -> None:
        buf = self._active[form_key]
        if not buf:
            return
        batch = list(buf)
        buf.clear()
        self.total_buffered -= len(batch)
        idx = self._active_batch_idx[form_key]
        self._active_batch_idx[form_key] = idx + 1
        await self._on_flush_active(form_key, batch, idx)

    async def _flush_voided_form(self, form_key: str) -> None:
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
        await self._on_flush_voided(form_key, batch, idx)

    async def _evict_largest(self) -> None:
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
                await self._flush_voided_form(best_key)
            else:
                await self._flush_active_form(best_key)

    async def flush_all(self) -> None:
        """Flush all remaining buffered active and voided records in form order."""
        for fk in self._form_keys:
            if self._active[fk]:
                await self._flush_active_form(fk)
            if self._on_flush_voided is not None and self._voided[fk]:
                await self._flush_voided_form(fk)


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
                        multi_bound_attributes=RECORD_TIMESTAMP_ATTRIBUTES,
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
                        multi_bound_attributes=RECORD_TIMESTAMP_ATTRIBUTES,
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
    sort: str | None = None,
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
        call_kwargs = dict(branch)
        if sort and "sort" not in call_kwargs:
            call_kwargs["sort"] = sort
        page_iter = sdk.records.list(
            study_key=study_key,
            record_data_filter=validated_rdf,
            **call_kwargs,
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


async def async_iter_filtered_sdk_records(
    sdk: Any,
    study_key: str,
    *,
    watermark: datetime | str | None = None,
    record_filter: dict[str, Any] | str | Sequence[str] | None = None,
    record_data_filter: str | None = None,
    exclude_deleted_upstream: bool = True,
    sort: str | None = None,
) -> AsyncIterator[Any]:
    """Asynchronously yield records from ``sdk.records.list`` or ``sdk.get_records``."""
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

    use_records_ep = False
    if isinstance(sdk, NonCallableMock):
        mock_children = getattr(sdk, "_mock_children", {})
        inst_vars = vars(sdk)
        has_records_ep = "records" in mock_children or "records" in inst_vars
        has_get_records = (
            "get_records" in inst_vars
            or "async_get_records" in inst_vars
            or "get_records" in mock_children
            or "async_get_records" in mock_children
        )
        use_records_ep = has_records_ep or not has_get_records
    else:
        records_ep = getattr(sdk, "records", None)
        use_records_ep = records_ep is not None and callable(getattr(records_ep, "list", None))

    for branch, post_predicate in resolved:
        call_kwargs = dict(branch)
        if sort and "sort" not in call_kwargs:
            call_kwargs["sort"] = sort
        if validated_rdf is not None:
            call_kwargs["record_data_filter"] = validated_rdf
        elif use_records_ep:
            call_kwargs["record_data_filter"] = None

        if use_records_ep:
            page_source = sdk.records.list(
                study_key=study_key,
                **call_kwargs,
            )
            if inspect.isawaitable(page_source):
                page_source = await page_source
        else:
            page_source = await call_async_facade_method(
                sdk,
                "get_records",
                study_key=study_key,
                **call_kwargs,
            )

        if isinstance(page_source, AsyncIterable):
            async for record in page_source:
                if post_predicate is not None and not post_predicate(record):
                    continue
                if deduplicate:
                    rec_id = extract_record_id(record)
                    if rec_id is not None:
                        if rec_id in seen_ids:
                            continue
                        seen_ids.add(rec_id)
                yield record
        else:
            for record in page_source:
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

    raw_sdtm_domain = config.sdtm_domain if is_real_cfg else getattr(config, "sdtm_domain", None)
    sdtm_domain = (
        resolve_sdtm_domain(raw_sdtm_domain)
        if isinstance(raw_sdtm_domain, str) and raw_sdtm_domain.strip()
        else None
    )
    as_sdtm = resolve_effective_as_sdtm(config, sdtm_domain=sdtm_domain)

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
    form_domain_map: Mapping[Any, str] | None = None,
) -> None:
    """Evaluate batch standards readiness, log masked warnings, and enforce min_standards_score."""
    report = evaluate_standards_readiness(
        batch,
        study_key=study_key,
        config=config,
        standards_profile=standards_profile,
        sdtm_domain=sdtm_domain,
        form_domain_map=form_domain_map,
    )
    profile_obj = resolve_standards_profile(standards_profile)
    for warning_msg in report.warnings:
        logger.warning(
            "Standards readiness warning in batch %s: %s",
            batch_id,
            sanitize_diagnostic_text(mask_clinical_phi(warning_msg))
            if config.mask_phi
            else warning_msg,
        )
    if min_standards_score is not None and report.score < min_standards_score:
        error_fields = sorted({v.field for v in report.violations if v.severity.upper() == "ERROR"})
        detail = f" (violating fields: {', '.join(error_fields)})" if error_fields else ""
        raise ExportError(
            f"Standards readiness check failed for batch '{batch_id}' "
            f"(profile='{profile_obj.profile_name}', score={report.score:.1f} < "
            f"min_standards_score={min_standards_score:.1f}){detail}"
        )


def _is_bound_real_records_endpoint(sdk: Any) -> bool:
    """Return True strictly when ``sdk.records.list`` is an unmocked ``RecordsEndpoint.list``."""
    from imednet.endpoints.records import RecordsEndpoint

    records_ep = getattr(sdk, "records", None)
    if not isinstance(records_ep, RecordsEndpoint):
        return False
    list_attr = getattr(records_ep, "list", None)
    return getattr(list_attr, "__func__", None) is RecordsEndpoint.list


def _is_bound_real_async_records_endpoint(sdk: Any) -> bool:
    """Return True when ``sdk`` uses an unmocked real ``AsyncRecordsEndpoint`` or sync adapter."""
    from imednet.endpoints.records import AsyncRecordsEndpoint

    if isinstance(sdk, SyncToAsyncFacadeAdapter):
        return _is_bound_real_records_endpoint(sdk.sync_sdk)
    records_ep = getattr(sdk, "records", None)
    if not isinstance(records_ep, AsyncRecordsEndpoint):
        return False
    list_attr = getattr(records_ep, "list", None)
    return getattr(list_attr, "__func__", None) is AsyncRecordsEndpoint.list


def _filter_valid_study_forms(items: Iterable[Any], *, is_mock_sdk: bool) -> list[Any]:
    """Filter fetched study forms while ignoring auto-created MagicMock placeholders."""
    if is_mock_sdk:
        return [
            f
            for f in items
            if not isinstance(f, NonCallableMock)
            and (
                (isinstance(f, Mapping) and (f.get("formKey") or f.get("form_key")))
                or isinstance(getattr(f, "form_key", None), str)
            )
        ]
    return [f for f in items if f is not None]


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
        return _filter_valid_study_forms(items, is_mock_sdk=True)

    forms_ep = getattr(sdk, "forms", None)
    list_fn = getattr(forms_ep, "list", None) if forms_ep is not None else None
    if not callable(list_fn):
        return []
    try:
        return _filter_valid_study_forms(list_fn(study_key=study_key), is_mock_sdk=False)
    except Exception:
        return []


async def _async_try_fetch_study_forms(sdk: Any, study_key: str) -> list[Any]:
    """Safely fetch Study Forms asynchronously when ``sdk.forms.list`` or ``get_forms`` exists."""
    if sdk is None:
        return []
    if isinstance(sdk, SyncToAsyncFacadeAdapter):
        return _try_fetch_study_forms(sdk.sync_sdk, study_key)

    is_mock_sdk = isinstance(sdk, NonCallableMock)
    if is_mock_sdk:
        mock_children = getattr(sdk, "_mock_children", {})
        inst_vars = vars(sdk)
        has_forms_ep = "forms" in mock_children or "forms" in inst_vars
        has_get_forms = (
            "get_forms" in inst_vars
            or "async_get_forms" in inst_vars
            or "get_forms" in mock_children
            or "async_get_forms" in mock_children
        )
        if not has_forms_ep and has_get_forms:
            try:
                fetched = await call_async_facade_method(sdk, "get_forms", study_key=study_key)
                if isinstance(fetched, NonCallableMock):
                    return []
                return _filter_valid_study_forms(list(fetched), is_mock_sdk=True)
            except Exception:
                return []
        forms_ep = getattr(sdk, "forms", None)
        list_fn = getattr(forms_ep, "list", None) if forms_ep is not None else None
        if list_fn is None:
            return []
        try:
            candidate = list_fn(study_key=study_key)
            if inspect.isawaitable(candidate):
                candidate = await candidate
            if isinstance(candidate, NonCallableMock):
                return []
            if isinstance(candidate, AsyncIterable):
                items = [item async for item in candidate]
            else:
                items = list(candidate)
        except Exception:
            return []
        return _filter_valid_study_forms(items, is_mock_sdk=True)

    forms_ep = getattr(sdk, "forms", None)
    list_fn = getattr(forms_ep, "list", None) if forms_ep is not None else None
    if callable(list_fn):
        try:
            candidate = list_fn(study_key=study_key)
            if inspect.isawaitable(candidate):
                candidate = await candidate
            if isinstance(candidate, AsyncIterable):
                items = [item async for item in candidate]
            else:
                items = list(candidate)
            return _filter_valid_study_forms(items, is_mock_sdk=False)
        except Exception:
            return []

    try:
        fetched = await call_async_facade_method(sdk, "get_forms", study_key=study_key)
        return _filter_valid_study_forms(list(fetched), is_mock_sdk=False)
    except Exception:
        return []


def resolve_effective_as_sdtm(
    config: Any,
    *,
    as_sdtm: bool | None = None,
    sdtm_domain: str | None = None,
) -> bool:
    """Resolve effective ``as_sdtm`` mode while honoring explicit ``as_sdtm=False``."""
    if isinstance(as_sdtm, bool):
        return as_sdtm
    if config is None:
        return bool(isinstance(sdtm_domain, str) and sdtm_domain.strip())
    if isinstance(config, SinkConfig):
        if (
            isinstance(sdtm_domain, str)
            and sdtm_domain.strip()
            and not getattr(config, "_as_sdtm_explicit", False)
        ):
            return True
        return bool(config.as_sdtm)
    raw_as = getattr(config, "as_sdtm", None)
    if raw_as is True:
        return True
    cfg_ns = getattr(config, "__dict__", None)
    if isinstance(cfg_ns, Mapping) and "as_sdtm" in cfg_ns and cfg_ns["as_sdtm"] is False:
        return False
    if getattr(config, "_as_sdtm_explicit", False) and raw_as is False:
        return False
    raw_dom = sdtm_domain if sdtm_domain is not None else getattr(config, "sdtm_domain", None)
    if isinstance(raw_dom, str) and raw_dom.strip():
        return True
    return bool(raw_as is True)


def _resolve_lineage_target(
    sink_or_config: Any,
    *,
    study_key: str | None = None,
    stream_name: str | None = None,
) -> tuple[Any, SinkConfig | Any, str, str, dict[str, Any] | None]:
    """Resolve ``(provider, cfg, resolved_study, resolved_stream, active_tx)`` for SDTM lineage."""
    is_sink = (
        isinstance(sink_or_config, (ExportSink, AsyncExportSink))
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


class _ScopedSeqOverrides(dict[str, int]):
    """Scoped ``domain:usubjid:record_id -> seq_val`` overrides compatible with ``dict[str, int]``.

    Dictionary iteration and ``dict.update(...)`` expose only scoped string keys so
    downstream sinks (`warehouse`, `document`, `graph`) preserve per-`(domain, usubjid, record_id)`
    isolation without modifying caller contracts, while direct indexing also accepts
    ``(domain, usubjid, record_id)`` tuples and unambiguous legacy ``record_id`` lookups.
    """

    def __init__(self) -> None:
        super().__init__()
        self._by_record_id: dict[str, int | None] = {}

    def set_scoped(self, domain: str, usubjid: str, record_id: str, seq_val: int) -> None:
        from imednet.models.sdtm import format_scoped_seq_override_key

        scoped_key = format_scoped_seq_override_key(domain, usubjid, record_id)
        super().__setitem__(scoped_key, seq_val)
        if record_id not in self._by_record_id:
            self._by_record_id[record_id] = seq_val
        elif self._by_record_id[record_id] != seq_val:
            self._by_record_id[record_id] = None

    def _resolve_lookup_key(self, key: Any) -> str | None:
        if isinstance(key, tuple) and len(key) == 3:
            from imednet.models.sdtm import format_scoped_seq_override_key

            cand = format_scoped_seq_override_key(str(key[0]), str(key[1]), str(key[2]))
            return cand if super().__contains__(cand) else None
        if isinstance(key, str) and super().__contains__(key):
            return key
        return None

    def __contains__(self, key: object) -> bool:
        if self._resolve_lookup_key(key) is not None:
            return True
        if isinstance(key, str) and ":" not in key:
            return self._by_record_id.get(key) is not None
        return False

    def __getitem__(self, key: Any) -> int:
        resolved = self._resolve_lookup_key(key)
        if resolved is not None:
            return super().__getitem__(resolved)
        if isinstance(key, str) and ":" not in key:
            legacy_val = self._by_record_id.get(key)
            if legacy_val is not None:
                return legacy_val
        raise KeyError(key)

    def get(self, key: Any, default: Any = None) -> Any:  # type: ignore[override, unused-ignore]
        try:
            return self.__getitem__(key)
        except KeyError:
            return default


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
    seq_overrides = _ScopedSeqOverrides()

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
            seq_overrides.set_scoped(dom, usubjid, rec_id_str, seq_val)
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
        isinstance(sink_or_config, (ExportSink, AsyncExportSink))
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


class _SyncExportSinkAdapter(AsyncExportSink):
    """Internal adapter delegating :class:`AsyncExportSink` lifecycle to a sync :class:`ExportSink`."""

    _skip_auto_redact_batch = True
    _sync_sink: ExportSink

    def __init__(self, sync_sink: ExportSink) -> None:
        object.__setattr__(self, "_sync_sink", sync_sink)
        super().__init__(cast(SinkConfig, getattr(sync_sink, "config", None)))

    @property
    def reconciles_deleted_on_full_sync(self) -> bool:  # type: ignore[override]
        return bool(getattr(self._sync_sink, "reconciles_deleted_on_full_sync", False))

    def bind_stream_metadata(self, metadata: dict[str, Any]) -> None:
        bind_fn = getattr(self._sync_sink, "bind_stream_metadata", None)
        if callable(bind_fn):
            bind_fn(metadata)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_active_"):
            with contextlib.suppress(Exception):
                setattr(self._sync_sink, name, value)
        object.__setattr__(self, name, value)

    async def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        return self._sync_sink.write_batch(records, batch_id=batch_id)

    async def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        return self._sync_sink.reconcile_deleted(records, batch_id=batch_id)

    async def flush(self) -> None:
        self._sync_sink.flush()

    async def close(self) -> None:
        self._sync_sink.close()

    async def __aenter__(self) -> _SyncExportSinkAdapter:
        enter_fn = getattr(self._sync_sink, "__enter__", None)
        if callable(enter_fn):
            enter_fn()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        exit_fn = getattr(self._sync_sink, "__exit__", None)
        if callable(exit_fn):
            exit_fn(exc_type, exc_val, exc_tb)
            return
        try:
            if exc_type is None:
                self._sync_sink.flush()
        finally:
            self._sync_sink.close()


async def _ensure_async_stream(source: Any) -> AsyncIterator[Any]:
    """Yield items asynchronously from an ``AsyncIterable``, ``Awaitable``, or ``Iterable``."""
    resolved = await source if inspect.isawaitable(source) else source
    if isinstance(resolved, AsyncIterable):
        async for item in resolved:
            yield item
    else:
        for item in resolved:
            yield item


async def async_run_streaming_export(
    sdk: AsyncImednetFacade,
    study_key: str,
    sink: AsyncExportSink | Callable[[], AsyncExportSink],
    config: SinkConfig,
    *,
    stream_name: str | None = None,
    batch_id_prefix: str | None = None,
    default_provider_name: str | None = None,
    records_source: (
        Callable[
            [datetime | None, bool],
            AsyncIterable[Any] | Iterable[Any] | Awaitable[Iterable[Any]],
        ]
        | None
    ) = None,
    quality_gate_fn: Callable[..., Any] | None = None,
    iter_batches_fn: Callable[..., Any] | None = None,
    _caller_sdk: Any = None,
) -> int:
    """Asynchronously stream paginated records into ``sink`` with checkpointing and audit gates.

    Commits ledger checkpoints strictly after all batches and ``await sink.flush()`` succeed.
    """
    if type(sdk).__name__ == "ImednetSDK" or (
        sdk is not None
        and not isinstance(sdk, (NonCallableMock, SyncToAsyncFacadeAdapter))
        and not is_async_facade(sdk)
    ):
        raise TypeError(
            "async_run_streaming_export() requires an AsyncImednetFacade or AsyncImednetSDK; "
            "use run_streaming_export() for synchronous SDK instances."
        )

    opts = _extract_safe_config_options(config)
    use_checkpoint = opts["incremental"] or opts["state_provider"] is not None
    gate_fn = quality_gate_fn or async_apply_quality_gate
    batches_fn = iter_batches_fn or async_iter_batches
    prefix = batch_id_prefix or f"{study_key}/records"
    resolved_stream = opts["stream_name"] or stream_name or "records"
    gov_sdk = _caller_sdk if _caller_sdk is not None else sdk

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

    raw_extra = config.extra if isinstance(config, SinkConfig) else getattr(config, "extra", None)
    study_cfg_override = (
        raw_extra.get("study_config")
        if isinstance(raw_extra, Mapping) and raw_extra.get("study_config") is not None
        else None
    )

    async with contextlib.AsyncExitStack() as stack:
        stack.enter_context(sdk_governance_context(gov_sdk))
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

        if isinstance(sink, AsyncExportSink) or hasattr(sink, "write_batch"):
            sink_obj = cast(AsyncExportSink, sink)
        else:
            sink_obj = sink()
        await stack.enter_async_context(sink_obj)

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

        check_standards = (
            opts["standards_profile"] is not None or opts["min_standards_score"] is not None
        )
        need_form_map = bool(opts["as_sdtm"] or opts["sdtm_domain"] is not None or check_standards)
        study_forms = await _async_try_fetch_study_forms(sdk, study_key) if need_form_map else []
        active_form_map: dict[Any, str] = (
            build_form_sdtm_domain_map(
                study_forms,
                study_config=study_cfg_override,
                study_key=study_key,
            )
            if need_form_map
            else {}
        )
        if active_form_map:
            with contextlib.suppress(Exception):
                sink_obj._active_form_domain_map = active_form_map  # type: ignore[attr-defined]

        sdtm_sort = _SDTM_RECORDS_SORT if opts["as_sdtm"] else None
        source_is_ordered = False

        if records_source is not None:
            raw_records: AsyncIterable[Any] = _ensure_async_stream(
                records_source(watermark, exclude_deleted_upstream)
            )
            source_is_ordered = bool(getattr(records_source, "_sdtm_source_is_ordered", False))
        else:
            source_is_ordered = bool(opts["as_sdtm"] and _is_bound_real_async_records_endpoint(sdk))
            if opts["as_sdtm"] and opts["sdtm_domain"] is not None:
                if study_forms:
                    matched_forms = filter_forms_by_sdtm_domain(
                        study_forms,
                        opts["sdtm_domain"],
                        study_config=study_cfg_override,
                        study_key=study_key,
                    )
                    if not matched_forms:
                        raw_records = _ensure_async_stream(())
                    else:
                        upstream_filter = merge_form_ids_into_record_filter(
                            opts["record_filter"],
                            matched_forms,
                        )
                        base_aiter = async_iter_filtered_sdk_records(
                            sdk,
                            study_key,
                            watermark=watermark,
                            record_filter=upstream_filter,
                            record_data_filter=opts["record_data_filter"],
                            exclude_deleted_upstream=exclude_deleted_upstream,
                            sort=sdtm_sort,
                        )
                        raw_records = (
                            item
                            async for item in base_aiter
                            if record_matches_sdtm_domain(
                                item,
                                opts["sdtm_domain"],
                                form_domain_map=active_form_map,
                                study_config=study_cfg_override,
                                study_key=study_key,
                            )
                        )
                else:
                    base_aiter = async_iter_filtered_sdk_records(
                        sdk,
                        study_key,
                        watermark=watermark,
                        record_filter=opts["record_filter"],
                        record_data_filter=opts["record_data_filter"],
                        exclude_deleted_upstream=exclude_deleted_upstream,
                        sort=sdtm_sort,
                    )
                    raw_records = (
                        item
                        async for item in base_aiter
                        if record_matches_sdtm_domain(
                            item,
                            opts["sdtm_domain"],
                            form_domain_map=active_form_map,
                            study_config=study_cfg_override,
                            study_key=study_key,
                        )
                    )
            else:
                raw_records = async_iter_filtered_sdk_records(
                    sdk,
                    study_key,
                    watermark=watermark,
                    record_filter=opts["record_filter"],
                    record_data_filter=opts["record_data_filter"],
                    exclude_deleted_upstream=exclude_deleted_upstream,
                    sort=sdtm_sort,
                )

        pending_voided: list[Any] = []
        voided_batch_idx = 0
        pending_gate: dict[int, list[tuple[Any, datetime | None]]] = {}
        exported_timestamps: list[datetime] = []

        async def _observe_and_route() -> AsyncIterator[Any]:
            nonlocal total_reconciled, voided_batch_idx
            async for rec in raw_records:
                rec_ts = extract_record_timestamp(rec)

                if not opts["include_deleted"] and is_record_deleted(rec):
                    if reconcile_voided:
                        if rec_ts is not None:
                            exported_timestamps.append(rec_ts)
                        pending_voided.append(rec)
                        if len(pending_voided) >= opts["batch_size"]:
                            total_reconciled += await sink_obj.reconcile_deleted(
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

        async def _track_accepted(records_aiter: AsyncIterable[Any]) -> AsyncIterator[Any]:
            async for rec in records_aiter:
                _pop_accepted_candidate(rec)
                rec_ts = extract_record_timestamp(rec)
                if rec_ts is not None:
                    exported_timestamps.append(rec_ts)
                yield rec

        if gate_fn in (async_apply_quality_gate, apply_quality_gate):
            gated_stream: AsyncIterable[Any] = async_apply_quality_gate(
                sdk,
                study_key,
                _observe_and_route(),
                config,
                forms=study_forms or None,
            )
        elif inspect.isasyncgenfunction(gate_fn):
            gated_stream = gate_fn(gov_sdk, study_key, _observe_and_route(), config)
        else:
            observed_list = [item async for item in _observe_and_route()]
            gated_stream = _ensure_async_stream(gate_fn(gov_sdk, study_key, observed_list, config))

        filtered_records: AsyncIterable[Any] = _track_accepted(gated_stream)
        if opts["as_sdtm"]:
            filtered_records = async_iter_subject_ordered_sdtm_records(
                filtered_records,
                study_key=study_key,
                sdtm_domain=opts["sdtm_domain"],
                form_domain_map=active_form_map or None,
                study_config=study_cfg_override,
                mask_phi=getattr(config, "mask_phi", True) is not False,
                source_is_ordered=source_is_ordered,
            )

        if batches_fn in (async_iter_batches, iter_batches):
            batch_stream: AsyncIterable[Sequence[Any]] = async_iter_batches(
                filtered_records,
                opts["batch_size"],
            )
        elif inspect.isasyncgenfunction(batches_fn):
            batch_stream = batches_fn(filtered_records, opts["batch_size"])
        else:
            materialized_filtered = [item async for item in filtered_records]
            batch_stream = _ensure_async_stream(
                batches_fn(materialized_filtered, opts["batch_size"])
            )

        index = 0
        async for batch in batch_stream:
            batch_id = f"{prefix}/{index}"
            index += 1
            if check_standards and batch:
                _enforce_batch_standards_readiness(
                    batch,
                    batch_id=batch_id,
                    study_key=study_key,
                    config=config,
                    standards_profile=opts["standards_profile"],
                    min_standards_score=opts["min_standards_score"],
                    sdtm_domain=opts["sdtm_domain"],
                    form_domain_map=active_form_map or None,
                )
            total_written += await sink_obj.write_batch(batch, batch_id=batch_id)

        if pending_voided:
            total_reconciled += await sink_obj.reconcile_deleted(
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


def run_streaming_export(
    sdk: ImednetFacade,
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
    if type(sdk).__name__ == "AsyncImednetSDK" or (
        sdk is not None
        and not isinstance(sdk, (NonCallableMock, SyncToAsyncFacadeAdapter))
        and is_async_facade(sdk)
    ):
        raise TypeError(
            "run_streaming_export() does not accept AsyncImednetSDK; "
            "use await async_run_streaming_export(...) instead."
        )

    adapted_sdk: Any = SyncToAsyncFacadeAdapter(sdk) if sdk is not None else None

    def _make_async_sink() -> AsyncExportSink:
        if isinstance(sink, ExportSink) or hasattr(sink, "write_batch"):
            return _SyncExportSinkAdapter(cast(ExportSink, sink))
        return _SyncExportSinkAdapter(sink())

    return run_sync_over_async(
        lambda: async_run_streaming_export(
            adapted_sdk,
            study_key,
            _make_async_sink,
            config,
            stream_name=stream_name,
            batch_id_prefix=batch_id_prefix,
            default_provider_name=default_provider_name,
            records_source=records_source,
            quality_gate_fn=quality_gate_fn,
            iter_batches_fn=iter_batches_fn,
            _caller_sdk=sdk,
        )
    )


def is_incremental_sink_config(config: Any) -> bool:
    """Return True when ``config`` activates incremental, watermark, or stateful sync."""
    return bool(
        getattr(config, "incremental", False) is True
        or getattr(config, "modified_since", None) is not None
        or getattr(config, "state_provider", None) is not None
    )


def should_preserve_sdtm_record_id(
    config: Any,
    preserve_record_id: bool | None = None,
) -> bool:
    """Return True when SDTM sink projections should retain ``RECORD_ID`` for lineage/upserts."""
    if preserve_record_id is not None:
        return bool(preserve_record_id)
    cfg_val = getattr(config, "preserve_record_id", None)
    if isinstance(cfg_val, bool):
        return cfg_val
    extra = getattr(config, "extra", None)
    if isinstance(extra, Mapping):
        extra_val = extra.get("preserve_record_id")
        if isinstance(extra_val, bool):
            return extra_val
    return is_incremental_sink_config(config)


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
    "_SDTM_RECORDS_SORT",
    "AsyncBoundedFormBufferRouter",
    "AsyncExportSink",
    "BoundedFormBufferRouter",
    "ExportSink",
    "SinkConfig",
    "StateProviderProtocol",
    "TabularBatchStamp",
    "_redact_uri",
    "_require_optional_dep",
    "apply_quality_gate",
    "async_apply_quality_gate",
    "async_iter_batches",
    "async_iter_filtered_sdk_records",
    "async_iter_subject_ordered_sdtm_records",
    "async_run_streaming_export",
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
    "format_nested_variable_name",
    "get_state_provider",
    "get_tabular_batch_stamp",
    "has_valid_tabular_batch_stamp",
    "is_incremental_sink_config",
    "is_record_deleted",
    "iter_batches",
    "iter_filtered_sdk_records",
    "iter_subject_ordered_sdtm_records",
    "merge_form_ids_into_record_filter",
    "normalize_phi_keys_snapshot",
    "normalize_sink_value",
    "normalize_watermark_datetime",
    "prefix_colliding_variable_name",
    "record_matches_sdtm_domain",
    "redact_batch_records",
    "rehydrate_sdtm_lineage",
    "resolve_effective_as_sdtm",
    "resolve_form_sdtm_domain",
    "resolve_max_buffered_records",
    "resolve_voided_sdtm_keys",
    "run_streaming_export",
    "should_preserve_sdtm_record_id",
    "stamp_tabular_batch",
    "validate_max_buffered_records",
]
