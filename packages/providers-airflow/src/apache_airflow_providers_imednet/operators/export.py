"""Airflow operator for exporting study records."""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime
from enum import Enum
from typing import Any, cast
from urllib.parse import quote_plus, urlsplit

from imednet import ImednetSDK
from imednet.spi import sink_base
from imednet.spi.sinks import redact_uri
from imednet.spi.utils import (
    redact_sensitive_payload,
    redact_sensitive_text,
    validate_path_segment,
)

from .. import export
from .._airflow_compat import AirflowException, BaseOperator, Context
from .._guards import has_embedded_uri_credentials, has_inline_secret_tokens
from ..hooks import ImednetHook

logger = logging.getLogger(__name__)

# Fallback mapping for tabular exports if the user explicitly needs them or backward compatibility
_TABULAR_EXPORTS = {
    "csv": export.export_to_csv,
    "parquet": export.export_to_parquet,
    "hive_parquet": export.export_to_hive_parquet,
    "excel": export.export_to_excel,
    "json": export.export_to_json,
    "sql": export.export_to_sql,
    "sql_by_form": export.export_to_sql_by_form,
    "long_sql": export.export_to_long_sql,
    "duckdb": export.export_to_duckdb,
    "duckdb_by_form": export.export_to_duckdb_by_form,
}

_SECRET_EXPORT_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "auth",
        "auth_token",
        "authorization",
        "client_secret",
        "credentials",
        "oidc_token",
        "passphrase",
        "passwd",
        "password",
        "private_key",
        "private_key_passphrase",
        "pwd",
        "refresh_token",
        "secret",
        "secret_key",
        "security_key",
        "token",
        "x-api-key",
        "x-imn-security-key",
    }
)

_TABULAR_OPTION_DEFAULTS: dict[str, Any] = {
    "batch_size": 500,
    "max_retries": 3,
    "idempotent": True,
    "mask_phi": True,
    "quality_gate_enabled": False,
    "min_schema_readiness_score": 100.0,
    "as_sdtm": False,
    "sdtm_domain": None,
    "standards_profile": None,
    "min_standards_score": None,
    "naming_convention": None,
}


def _is_allowed_auth_none(key_norm: str, item: Any) -> bool:
    """Return True when ``export_kwargs`` explicitly disables auth via ``auth='none'``."""
    return key_norm == "auth" and isinstance(item, str) and item.strip().lower() == "none"


def _validate_no_secrets_in_value(
    value: Any,
    *,
    field_name: str = "export_kwargs",
    check_secret_keys: bool | None = None,
) -> None:
    """Reject secret keys or embedded credentials in templated operator fields."""
    enforce_keys = field_name == "export_kwargs" if check_secret_keys is None else check_secret_keys
    if isinstance(value, str):
        if has_embedded_uri_credentials(value):
            raise AirflowException(
                f"Plaintext URI credentials are not allowed in '{field_name}'. "
                "Configure an Airflow Connection and pass 'destination_conn_id' instead."
            )
        if has_inline_secret_tokens(value):
            raise AirflowException(
                f"Embedded credentials or secret tokens are not allowed in '{field_name}'. "
                "Configure an Airflow Connection instead."
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_norm = str(key).strip().lower()
            if (
                enforce_keys
                and key_norm in _SECRET_EXPORT_KEYS
                and not _is_allowed_auth_none(key_norm, item)
            ):
                raise AirflowException(
                    f"Secret key '{key}' is not allowed in '{field_name}'. "
                    "Configure an Airflow Connection and pass 'destination_conn_id' instead."
                )
            _validate_no_secrets_in_value(
                item,
                field_name=field_name,
                check_secret_keys=enforce_keys,
            )
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _validate_no_secrets_in_value(
                item,
                field_name=field_name,
                check_secret_keys=enforce_keys,
            )


def _load_openlineage_types() -> tuple[Any, Any] | None:
    """Lazily import OpenLineage ``OperatorLineage`` and ``Dataset`` when available."""
    try:
        from airflow.providers.openlineage.extractors import OperatorLineage
    except (ImportError, ModuleNotFoundError):
        return None
    try:
        from airflow.providers.common.compat.openlineage.facet import Dataset
    except (ImportError, ModuleNotFoundError):
        try:
            from openlineage.client.event_v2 import Dataset  # type: ignore[no-redef]
        except (ImportError, ModuleNotFoundError):
            return None
    if OperatorLineage is None or Dataset is None:
        return None
    return OperatorLineage, Dataset


def _sanitize_lineage_text(value: str) -> str:
    """Redact credentials and sensitive query parameters from OpenLineage identifiers."""
    return redact_sensitive_text(redact_uri(value))


def _sanitize_stream_segment(value: str, *, max_len: int = 80) -> str:
    """Redact URI/inline secrets and normalize an identifier into a stream_name token."""
    raw = str(value).strip()
    if not raw:
        return ""
    redacted = redact_sensitive_text(str(redact_sensitive_payload(redact_uri(raw))))
    cleaned = redacted.replace("***", "").replace("[REDACTED]", "")
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "_", cleaned).strip("_")
    sanitized = re.sub(r"_+", "_", sanitized)
    if not sanitized:
        return ""
    if len(sanitized) <= max_len:
        return sanitized
    digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()[:8]
    half = max((max_len - 10) // 2, 8)
    return f"{sanitized[:half]}_{sanitized[-half:]}_{digest}"


def _lineage_authority(uri: str, *, normalize_sql_scheme: bool = False) -> str:
    """Extract canonical ``scheme://host[:port]`` namespace with zero userinfo or query params."""
    parsed = urlsplit(uri.strip())
    if not parsed.scheme or not parsed.hostname:
        return _sanitize_lineage_text(uri.strip())
    scheme = parsed.scheme.lower()
    if normalize_sql_scheme:
        scheme = scheme.split("+", 1)[0]
        if scheme == "postgresql":
            scheme = "postgres"
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    authority = f"{host}:{port}" if port is not None else host
    return f"{scheme}://{authority}"


def _inject_uri_credentials(uri: str, user: str | None, password: str | None) -> str:
    """Inject URL-encoded credentials into a credential-free URI when present."""
    if not uri or not user or "://" not in uri or "@" in uri:
        return uri
    scheme, remainder = uri.split("://", 1)
    userinfo = (
        f"{quote_plus(user)}:{quote_plus(password)}@"
        if password is not None
        else f"{quote_plus(user)}@"
    )
    return f"{scheme}://{userinfo}{remainder}"


class _ValidatedExportKwargs(dict[str, Any]):
    """Dictionary wrapper that rejects secret keys or embedded URI credentials on mutation."""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        """Initialize and validate export keyword arguments."""
        data = dict(initial or {})
        _validate_no_secrets_in_value(data, field_name="export_kwargs")
        super().__init__(data)

    def __setitem__(self, key: str, value: Any) -> None:
        """Validate key and value before setting an item."""
        _validate_no_secrets_in_value({key: value}, field_name="export_kwargs")
        super().__setitem__(key, value)

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Validate all items before updating the mapping."""
        candidate = dict(*args, **kwargs)
        _validate_no_secrets_in_value(candidate, field_name="export_kwargs")
        super().update(candidate)

    def setdefault(self, key: str, default: Any = None) -> Any:
        """Validate default before inserting a missing key."""
        if key not in self:
            _validate_no_secrets_in_value({key: default}, field_name="export_kwargs")
        return super().setdefault(key, default)


_STREAMING_EXPORT_KEYS = (
    "modified_since",
    "record_filter",
    "record_data_filter",
    "include_deleted",
    "incremental",
    "stream_name",
    "ledger_path",
    "state_provider",
)


class ImednetExportOperator(BaseOperator):
    """Unified Airflow operator for exporting study records to any destination."""

    # Fields intended for Airflow `.partial().expand()` runtime mapping.
    mapped_runtime_fields: Sequence[str] = ("study_key", "output_path", "export_kwargs")
    template_fields: Sequence[str] = (
        *mapped_runtime_fields,
        "modified_since",
        "record_filter",
        "record_data_filter",
        "stream_name",
    )
    template_fields_renderers = {"export_kwargs": "json"}  # noqa: RUF012
    _explicit_tabular_options: frozenset[str] = frozenset()

    def __new__(cls, *args: Any, **kwargs: Any) -> ImednetExportOperator:
        """Record explicitly supplied operational flags prior to default binding."""
        instance = cast("ImednetExportOperator", super().__new__(cls))
        object.__setattr__(
            instance,
            "_explicit_tabular_options",
            frozenset(key for key in (*_TABULAR_OPTION_DEFAULTS, "domain") if key in kwargs),
        )
        return instance

    def __init__(
        self,
        *,
        study_key: str,
        destination: str | None = None,
        output_path: str | None = None,
        export_func: str | None = None,
        export_kwargs: Mapping[str, Any] | None = None,
        imednet_conn_id: str = "imednet_default",
        destination_conn_id: str | None = None,
        batch_size: int = 500,
        max_retries: int = 3,
        idempotent: bool = True,
        mask_phi: bool = True,
        quality_gate_enabled: bool = False,
        min_schema_readiness_score: float = 100.0,
        as_sdtm: bool = False,
        domain: str | None = None,
        sdtm_domain: str | None = None,
        standards_profile: str | None = None,
        min_standards_score: float | None = None,
        naming_convention: str | None = None,
        modified_since: Any = None,
        record_filter: Any = None,
        record_data_filter: str | None = None,
        include_deleted: bool = False,
        incremental: bool = False,
        stream_name: str | None = None,
        ledger_path: Any = None,
        state_provider: Any = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the operator.

        :param study_key: The study key identifier.
        :param destination: The destination sink format or database.
        :param output_path: The filesystem path or SQL table target to export to (if applicable).
        :param export_func: The specific legacy tabular export function to use.
        :param export_kwargs: Extra non-secret keyword arguments passed to the sink/export function.
        :param imednet_conn_id: Airflow connection ID to use for iMednet API credentials.
        :param destination_conn_id: Optional Airflow connection ID to resolve destination credentials at runtime.
        :param batch_size: Number of records to read and write per batch.
        :param max_retries: Maximum number of export attempts.
        :param idempotent: Whether the export execution should be safely repeatable, automatically deduplicating or replacing existing outputs as appropriate.
        :param mask_phi: Whether to replace sensitive PHI values with ``***MASKED***`` by default.
        :param quality_gate_enabled: Whether to filter records through the schema readiness quality gate prior to export.
        :param min_schema_readiness_score: Minimum schema readiness score (0-100) required when the quality gate is enabled.
        :param kwargs: Additional Airflow BaseOperator arguments.
        """
        super().__init__(**kwargs)
        if not getattr(self, "task_id", None) and isinstance(kwargs.get("task_id"), str):
            self.task_id = kwargs["task_id"]
        self.study_key = study_key
        self.destination = destination
        self.output_path = output_path
        self.export_func = export_func
        self.export_kwargs = _ValidatedExportKwargs(export_kwargs)
        self.imednet_conn_id = imednet_conn_id
        self.destination_conn_id = destination_conn_id

        # Common operational parameters
        self.batch_size: int = batch_size
        self.max_retries = max_retries
        self.idempotent = idempotent
        self.mask_phi = mask_phi
        self.quality_gate_enabled = quality_gate_enabled
        self.min_schema_readiness_score = min_schema_readiness_score
        self.domain = domain
        self.sdtm_domain = sdtm_domain if sdtm_domain is not None else domain
        self.as_sdtm = bool(as_sdtm or self.sdtm_domain is not None)
        self.standards_profile = standards_profile
        self.min_standards_score = min_standards_score
        self.naming_convention = naming_convention
        self.modified_since = modified_since
        self.record_filter = record_filter
        self.record_data_filter = record_data_filter
        self.include_deleted = include_deleted
        self.incremental = incremental
        self.stream_name = stream_name
        self.ledger_path = ledger_path
        self.state_provider = state_provider

    @property
    def study_key(self) -> str:
        """Return the configured study key."""
        return getattr(self, "_study_key", "")

    @study_key.setter
    def study_key(self, value: str) -> None:
        """Validate and set study_key, rejecting embedded credentials."""
        if value is not None:
            _validate_no_secrets_in_value(value, field_name="study_key")
        self._study_key = value

    @property
    def output_path(self) -> str | None:
        """Return the configured output path."""
        return getattr(self, "_output_path", None)

    @output_path.setter
    def output_path(self, value: str | None) -> None:
        """Validate and set output_path, rejecting embedded URI credentials."""
        if value is not None:
            _validate_no_secrets_in_value(value, field_name="output_path")
        self._output_path = value

    @property
    def modified_since(self) -> Any:
        """Return the configured modified_since watermark."""
        return getattr(self, "_modified_since", None)

    @modified_since.setter
    def modified_since(self, value: Any) -> None:
        """Validate and set modified_since, rejecting embedded credentials."""
        if value is not None:
            _validate_no_secrets_in_value(value, field_name="modified_since")
        self._modified_since = value

    @property
    def record_filter(self) -> Any:
        """Return the configured record_filter."""
        return getattr(self, "_record_filter", None)

    @record_filter.setter
    def record_filter(self, value: Any) -> None:
        """Validate and set record_filter, rejecting embedded credentials."""
        if value is not None:
            _validate_no_secrets_in_value(value, field_name="record_filter")
        self._record_filter = value

    @property
    def record_data_filter(self) -> str | None:
        """Return the configured record_data_filter."""
        return getattr(self, "_record_data_filter", None)

    @record_data_filter.setter
    def record_data_filter(self, value: str | None) -> None:
        """Validate and set record_data_filter, rejecting embedded credentials."""
        if value is not None:
            _validate_no_secrets_in_value(value, field_name="record_data_filter")
        self._record_data_filter = value

    @property
    def stream_name(self) -> str | None:
        """Return the configured stream_name."""
        return getattr(self, "_stream_name", None)

    @stream_name.setter
    def stream_name(self, value: str | None) -> None:
        """Validate and set stream_name, rejecting embedded credentials."""
        if value is not None:
            _validate_no_secrets_in_value(value, field_name="stream_name")
        self._stream_name = value

    @property
    def export_kwargs(self) -> dict[str, Any]:
        """Return validated non-secret export keyword arguments."""
        current = getattr(self, "_export_kwargs", None)
        if current is None:
            current = _ValidatedExportKwargs()
            self._export_kwargs = current
        return current

    @export_kwargs.setter
    def export_kwargs(self, value: Mapping[str, Any] | None) -> None:
        """Validate and store export_kwargs without persisting raw credentials."""
        self._export_kwargs = _ValidatedExportKwargs(value)

    def _validate_templated_attributes(self) -> None:
        """Validate all templated operator attributes prior to execution or lineage emission."""
        for field_name in (
            "study_key",
            "output_path",
            "modified_since",
            "record_filter",
            "record_data_filter",
            "stream_name",
        ):
            val = getattr(self, field_name, None)
            if val is not None:
                _validate_no_secrets_in_value(val, field_name=field_name)
        _validate_no_secrets_in_value(self.export_kwargs, field_name="export_kwargs")

    def _effective_destination(self) -> str:
        """Return normalized destination identifier from ``destination`` or ``export_func``."""
        dest = self.destination
        if not dest and self.export_func:
            dest = self.export_func.replace("export_to_", "")
        elif not dest:
            dest = "csv"
        return dest.lower().strip()

    def _get_sdk(self) -> ImednetSDK:
        """Resolve the SDK client from the configured Airflow connection at execute time."""
        return ImednetHook(self.imednet_conn_id).get_sdk_client()

    def _resolve_destination_connection(self, destination: str) -> dict[str, Any]:
        """Resolve destination credentials at runtime from ``destination_conn_id`` if configured."""
        if not self.destination_conn_id:
            return {}
        return ImednetHook.resolve_destination_connection(self.destination_conn_id, destination)

    def _get_runtime_export_kwargs(self) -> dict[str, Any]:
        """Return a validated defensive copy of export kwargs for mapped task isolation."""
        _validate_no_secrets_in_value(self.export_kwargs, field_name="export_kwargs")
        return dict(self.export_kwargs)

    def _collect_streaming_options(self, runtime_kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """Collect non-default streaming, filtering, and checkpoint options."""
        opts: dict[str, Any] = {}
        defaults: dict[str, Any] = {
            "modified_since": None,
            "record_filter": None,
            "record_data_filter": None,
            "include_deleted": False,
            "incremental": False,
            "stream_name": None,
            "ledger_path": None,
            "state_provider": None,
        }
        for key, default_val in defaults.items():
            attr_val = getattr(self, key, default_val)
            if (
                key in ("modified_since", "stream_name")
                and isinstance(attr_val, str)
                and not attr_val.strip()
            ):
                attr_val = None
            if attr_val != default_val:
                opts[key] = attr_val
            elif key in runtime_kwargs and runtime_kwargs[key] != default_val:
                kw_val = runtime_kwargs[key]
                if (
                    key in ("modified_since", "stream_name")
                    and isinstance(kw_val, str)
                    and not kw_val.strip()
                ):
                    kw_val = None
                if kw_val != default_val:
                    opts[key] = kw_val
        effective_mask_phi = (
            bool(runtime_kwargs["mask_phi"])
            if "mask_phi" in runtime_kwargs
            else bool(getattr(self, "mask_phi", True))
        )
        explicit: frozenset[str] = getattr(self, "_explicit_tabular_options", frozenset())
        if not effective_mask_phi or "mask_phi" in runtime_kwargs or "mask_phi" in explicit:
            opts["mask_phi"] = effective_mask_phi

        resolved_domain = (
            runtime_kwargs.get("sdtm_domain")
            or runtime_kwargs.get("domain")
            or getattr(self, "sdtm_domain", None)
            or getattr(self, "domain", None)
        )
        effective_as_sdtm = bool(
            runtime_kwargs.get("as_sdtm", getattr(self, "as_sdtm", False))
            or resolved_domain is not None
        )
        if effective_as_sdtm or "as_sdtm" in runtime_kwargs or "as_sdtm" in explicit:
            opts["as_sdtm"] = effective_as_sdtm
        if resolved_domain is not None:
            opts["sdtm_domain"] = str(resolved_domain)
        resolved_profile = runtime_kwargs.get(
            "standards_profile", getattr(self, "standards_profile", None)
        )
        if resolved_profile is not None:
            opts["standards_profile"] = str(resolved_profile)
        raw_min_std = runtime_kwargs.get(
            "min_standards_score", getattr(self, "min_standards_score", None)
        )
        if raw_min_std is not None:
            opts["min_standards_score"] = float(raw_min_std)
        resolved_convention = runtime_kwargs.get(
            "naming_convention", getattr(self, "naming_convention", None)
        )
        if resolved_convention is not None:
            opts["naming_convention"] = str(resolved_convention)
        return opts

    def _resolve_dag_id(self, context: Context | Mapping[str, Any] | None = None) -> str | None:
        """Resolve the enclosing DAG identifier without touching unassigned ``self.dag``."""
        if isinstance(context, Mapping):
            for ctx_key in ("dag", "dag_run", "ti", "task_instance"):
                ctx_obj = context.get(ctx_key)
                ctx_dag_id = getattr(ctx_obj, "dag_id", None) if ctx_obj is not None else None
                if isinstance(ctx_dag_id, str) and ctx_dag_id.strip():
                    return ctx_dag_id.strip()
            raw_ctx_dag_id = context.get("dag_id")
            if isinstance(raw_ctx_dag_id, str) and raw_ctx_dag_id.strip():
                return raw_ctx_dag_id.strip()

        has_dag_fn = getattr(self, "has_dag", None)
        if callable(has_dag_fn):
            if has_dag_fn():
                raw_dag_id = getattr(self, "dag_id", None)
                if isinstance(raw_dag_id, str) and raw_dag_id.strip():
                    return raw_dag_id.strip()
        else:
            raw_dag_id = getattr(self, "dag_id", None)
            if (
                isinstance(raw_dag_id, str)
                and raw_dag_id.strip()
                and raw_dag_id.strip() != "adhoc_airflow"
            ):
                return raw_dag_id.strip()

        env_dag_id = os.environ.get("AIRFLOW_CTX_DAG_ID", "").strip()
        return env_dag_id or None

    def _resolve_export_target_identifier(
        self,
        dest: str,
        runtime_kwargs: Mapping[str, Any],
        conn_settings: Mapping[str, Any] | None = None,
    ) -> str:
        """Extract and sanitize destination-specific target segments for ``stream_name``."""
        conn = conn_settings or {}
        raw_parts: list[Any] = []
        if dest == "snowflake":
            raw_parts.extend(
                [
                    runtime_kwargs.get("database") or conn.get("database"),
                    runtime_kwargs.get("schema") or conn.get("schema"),
                    runtime_kwargs.get("table") or conn.get("table") or self.output_path,
                ]
            )
        elif dest == "mongodb":
            raw_parts.extend(
                [
                    runtime_kwargs.get("database") or conn.get("database"),
                    runtime_kwargs.get("collection") or conn.get("collection") or self.output_path,
                ]
            )
        elif dest == "neo4j":
            raw_parts.extend(
                [
                    runtime_kwargs.get("database") or conn.get("database"),
                    runtime_kwargs.get("label") or conn.get("label") or self.output_path,
                ]
            )
        elif dest == "duckdb":
            raw_parts.extend(
                [
                    self.output_path
                    or runtime_kwargs.get("db_path")
                    or runtime_kwargs.get("path")
                    or conn.get("db_path"),
                    runtime_kwargs.get("table_name")
                    or runtime_kwargs.get("table")
                    or conn.get("table_name"),
                ]
            )
        elif dest == "duckdb_by_form":
            raw_parts.append(
                self.output_path
                or runtime_kwargs.get("db_path")
                or runtime_kwargs.get("path")
                or conn.get("db_path")
            )
        elif dest in ("sql", "long_sql"):
            conn_str = (
                conn.get("conn_str")
                or runtime_kwargs.get("conn_str")
                or (
                    self.output_path
                    if isinstance(self.output_path, str) and "://" in self.output_path
                    else None
                )
            )
            table_target = (
                (
                    self.output_path
                    if isinstance(self.output_path, str) and "://" not in self.output_path
                    else None
                )
                or runtime_kwargs.get("table")
                or runtime_kwargs.get("table_name")
                or conn.get("table")
            )
            raw_parts.extend([conn_str, table_target])
        elif dest == "sql_by_form":
            raw_parts.append(
                self.output_path or runtime_kwargs.get("conn_str") or conn.get("conn_str")
            )
        elif dest == "hive_parquet":
            raw_parts.append(
                self.output_path or runtime_kwargs.get("base_dir") or runtime_kwargs.get("path")
            )
        else:
            raw_parts.append(
                self.output_path
                or runtime_kwargs.get("output_path")
                or runtime_kwargs.get("path")
                or runtime_kwargs.get("table")
                or runtime_kwargs.get("table_name")
                or runtime_kwargs.get("collection")
            )

        sanitized_parts = [
            seg
            for part in raw_parts
            if part is not None
            and isinstance(part, (str, os.PathLike))
            and str(part).strip()
            and (seg := _sanitize_stream_segment(str(part), max_len=64))
        ]
        if not sanitized_parts:
            return ""
        return _sanitize_stream_segment("_".join(sanitized_parts), max_len=80)

    def _default_airflow_stream_name(
        self,
        dest: str,
        *,
        sink_prefix: bool = False,
        context: Context | Mapping[str, Any] | None = None,
        runtime_kwargs: Mapping[str, Any] | None = None,
        conn_settings: Mapping[str, Any] | None = None,
    ) -> str:
        """Return a target-scoped default stream name invariant across mapped task indexes."""
        clean_dest = _sanitize_stream_segment(dest, max_len=32) or "records"
        base = f"airflow_{clean_dest}" if sink_prefix else clean_dest
        segments: list[str] = [base]

        dag_id = self._resolve_dag_id(context)
        if dag_id:
            dag_seg = _sanitize_stream_segment(dag_id, max_len=48)
            if dag_seg:
                segments.append(dag_seg)

        task_id = getattr(self, "task_id", None)
        if isinstance(task_id, str) and task_id.strip():
            task_seg = _sanitize_stream_segment(task_id.strip(), max_len=48)
            if task_seg:
                segments.append(task_seg)

        effective_kwargs = (
            runtime_kwargs if runtime_kwargs is not None else self._get_runtime_export_kwargs()
        )
        resolved_conn = conn_settings
        if resolved_conn is None:
            try:
                resolved_conn = self._resolve_destination_connection(dest)
            except Exception:
                resolved_conn = {}
        target_seg = self._resolve_export_target_identifier(
            dest,
            effective_kwargs,
            resolved_conn,
        )
        if target_seg:
            segments.append(target_seg)

        return "_".join(segments)

    def _pre_upgrade_candidate_streams(self, dest: str) -> list[str]:
        """Return candidate pre-upgrade stream names for single-target checkpoint migration."""
        candidates: list[str] = []
        task_id = getattr(self, "task_id", None)
        clean_task = task_id.strip() if isinstance(task_id, str) and task_id.strip() else None
        if clean_task:
            for cand in (f"{dest}_{clean_task}", f"airflow_{dest}_{clean_task}"):
                if cand not in candidates:
                    candidates.append(cand)
        if dest in ("snowflake", "neo4j", "mongodb"):
            sink_cand = f"airflow_{dest}"
            if sink_cand not in candidates:
                candidates.append(sink_cand)
        return candidates

    def _seed_pre_upgrade_watermark(
        self,
        *,
        dest: str,
        scoped_stream: str,
        state_provider: Any,
        ledger_path: Any,
        default_provider_name: str,
    ) -> datetime | None:
        """Seed ``scoped_stream`` from one matching pre-upgrade checkpoint when state is absent."""
        try:
            provider = (
                state_provider
                if state_provider is not None
                else sink_base.get_state_provider(
                    ledger_path=ledger_path,
                    provider_name=default_provider_name,
                )
            )
        except Exception:
            return None

        if provider is None:
            return None

        task_id = getattr(self, "task_id", None)
        clean_task = task_id.strip() if isinstance(task_id, str) and task_id.strip() else None
        task_seg = _sanitize_stream_segment(clean_task, max_len=48) if clean_task else ""

        try:
            study_streams: Mapping[str, Any] | None = None
            load_study_fn = getattr(provider, "_load_study_state", None)
            if callable(load_study_fn):
                loaded = load_study_fn(self.study_key)
                if (
                    isinstance(loaded, tuple)
                    and len(loaded) >= 2
                    and isinstance(loaded[1], Mapping)
                ):
                    study_streams = loaded[1]
            else:
                read_state_fn = getattr(provider, "read_state", None)
                if callable(read_state_fn):
                    ledger_state = read_state_fn()
                    studies_map = getattr(ledger_state, "studies", None)
                    if isinstance(studies_map, Mapping):
                        study_obj = studies_map.get(self.study_key)
                        streams_attr = getattr(study_obj, "streams", None)
                        if isinstance(streams_attr, Mapping):
                            study_streams = streams_attr

            if dest in ("sql_by_form", "duckdb_by_form"):
                if not clean_task or study_streams is None:
                    return None
                scoped_prefix = f"{scoped_stream}_"
                if any(str(k).startswith(scoped_prefix) for k in study_streams):
                    return None
                legacy_prefix = f"{dest}_{clean_task}_"
                seeded_any: datetime | None = None
                for s_key, s_entry in list(study_streams.items()):
                    if not str(s_key).startswith(legacy_prefix):
                        continue
                    form_suffix = str(s_key)[len(legacy_prefix) :]
                    if not form_suffix:
                        continue
                    raw_val = (
                        s_entry.get("last_timestamp")
                        if isinstance(s_entry, Mapping)
                        else getattr(s_entry, "last_timestamp", None)
                    )
                    if not isinstance(raw_val, (datetime, str)):
                        continue
                    norm_ts = sink_base.normalize_watermark_datetime(raw_val)
                    if norm_ts is not None and callable(
                        getattr(provider, "set_last_timestamp", None)
                    ):
                        provider.set_last_timestamp(
                            self.study_key,
                            f"{scoped_stream}_{form_suffix}",
                            norm_ts,
                            status="success",
                        )
                        seeded_any = norm_ts
                return seeded_any

            candidates = [
                cand for cand in self._pre_upgrade_candidate_streams(dest) if cand != scoped_stream
            ]
            if not candidates:
                return None

            if study_streams is not None:
                if scoped_stream in study_streams:
                    return None
                other_scoped_prefixes = [f"{dest}_", f"airflow_{dest}_"]
                existing_other_scoped = [
                    str(k)
                    for k in study_streams
                    if str(k) not in candidates
                    and any(str(k).startswith(p) for p in other_scoped_prefixes)
                    and (not task_seg or f"_{task_seg}_" in f"_{k}_")
                ]
                if existing_other_scoped:
                    return None

                matched_timestamps: list[tuple[str, datetime]] = []
                for cand in candidates:
                    if cand not in study_streams:
                        continue
                    entry = study_streams[cand]
                    raw_ts = (
                        entry.get("last_timestamp")
                        if isinstance(entry, Mapping)
                        else getattr(entry, "last_timestamp", None)
                    )
                    if isinstance(raw_ts, (datetime, str)):
                        norm_ts = sink_base.normalize_watermark_datetime(raw_ts)
                        if norm_ts is not None:
                            matched_timestamps.append((cand, norm_ts))
            else:
                existing_ts = provider.get_last_timestamp(self.study_key, scoped_stream)
                if (
                    isinstance(existing_ts, (datetime, str))
                    and sink_base.normalize_watermark_datetime(existing_ts) is not None
                ):
                    return None
                get_status_fn = getattr(provider, "get_last_status", None)
                if callable(get_status_fn):
                    existing_status = get_status_fn(self.study_key, scoped_stream)
                    if isinstance(existing_status, (str, Enum)):
                        return None

                matched_timestamps = []
                for cand in candidates:
                    raw_ts = provider.get_last_timestamp(self.study_key, cand)
                    if isinstance(raw_ts, (datetime, str)):
                        norm_ts = sink_base.normalize_watermark_datetime(raw_ts)
                        if norm_ts is not None:
                            matched_timestamps.append((cand, norm_ts))

            if len(matched_timestamps) != 1:
                return None

            _, seed_ts = matched_timestamps[0]
            set_ts_fn = getattr(provider, "set_last_timestamp", None)
            if callable(set_ts_fn):
                set_ts_fn(
                    self.study_key,
                    scoped_stream,
                    seed_ts,
                    status="success",
                )
            return seed_ts
        except Exception:
            return None

    def _build_tabular_kwargs(
        self,
        runtime_kwargs: dict[str, Any],
        *,
        dest: str = "records",
        context: Context | Mapping[str, Any] | None = None,
        conn_settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Merge operational parameters and deterministic quality-gate state into tabular kwargs."""
        merged = dict(runtime_kwargs)
        if "domain" in merged and "sdtm_domain" not in merged:
            merged["sdtm_domain"] = merged.pop("domain")
        else:
            merged.pop("domain", None)
        explicit: frozenset[str] = getattr(self, "_explicit_tabular_options", frozenset())
        for key, default_val in _TABULAR_OPTION_DEFAULTS.items():
            current_val = getattr(self, key)
            if key not in merged and (key in explicit or current_val != default_val):
                merged[key] = current_val
        if "quality_gate_enabled" in merged:
            merged["quality_gate_enabled"] = bool(merged["quality_gate_enabled"])
        else:
            merged["quality_gate_enabled"] = bool(getattr(self, "quality_gate_enabled", False))
        for key, val in self._collect_streaming_options(runtime_kwargs).items():
            merged.setdefault(key, val)
        use_checkpoint = bool(merged.get("incremental") or merged.get("state_provider") is not None)
        if use_checkpoint:
            if "state_provider" not in merged and "ledger_path" not in merged:
                merged["state_provider"] = sink_base.get_state_provider(provider_name="airflow")
            if "stream_name" not in merged:
                merged["stream_name"] = self._default_airflow_stream_name(
                    dest,
                    context=context,
                    runtime_kwargs=merged,
                    conn_settings=conn_settings,
                )
        return merged

    @staticmethod
    def _filter_callable_kwargs(
        export_callable: Any,
        tabular_kwargs: dict[str, Any],
        *,
        implicit_quality_gate: bool,
    ) -> dict[str, Any]:
        """Omit implicit ``quality_gate_enabled`` only when a custom callable rejects sink kwargs."""
        if not implicit_quality_gate or "quality_gate_enabled" not in tabular_kwargs:
            return tabular_kwargs
        try:
            params = inspect.signature(export_callable).parameters
        except (TypeError, ValueError):
            return tabular_kwargs
        if "quality_gate_enabled" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            return tabular_kwargs
        filtered = dict(tabular_kwargs)
        filtered.pop("quality_gate_enabled", None)
        return filtered

    def _resolve_sink(self, config: sink_base.SinkConfig, sdk: ImednetSDK | None = None) -> Any:
        """Dynamically load and configure the target sink from the SDK, handling dependencies lazily."""
        dest = self.destination
        if not dest and self.export_func:
            dest = self.export_func.replace("export_to_", "")

        if dest not in ("snowflake", "neo4j", "mongodb"):
            return None

        if sdk is None:
            with closing(self._get_sdk()) as resolved_sdk:
                return self._resolve_sink(config, sdk=resolved_sdk)

        sinks = getattr(sdk, "sinks", None)
        if sinks is None:
            raise AirflowException(
                f"The sinks plugin is required for destination '{dest}' but is not installed. "
                "Please install it using: pip install imednet-plugins-sinks"
            )

        runtime_kwargs = self._get_runtime_export_kwargs()
        conn_settings = self._resolve_destination_connection(dest)
        stream_opts = self._collect_streaming_options(runtime_kwargs)
        if not config.mask_phi and "mask_phi" not in stream_opts:
            stream_opts["mask_phi"] = False
        if config.max_buffered_records is not None:
            stream_opts["max_buffered_records"] = config.max_buffered_records

        if dest == "snowflake":
            sink_cls = getattr(sinks, "SnowflakeExportSink", None)
            config_cls = getattr(sinks, "SnowflakeSinkConfig", None)
            if sink_cls is None or config_cls is None:
                raise AirflowException(
                    "Snowflake sink is not available in the installed sinks plugin. "
                    "Please install the required sink dependencies: pip install 'imednet-plugins-sinks[snowflake]'"
                )
            hard_delete_kw: dict[str, Any] = {}
            if "hard_delete" in runtime_kwargs and runtime_kwargs["hard_delete"] is not None:
                hard_delete_kw["hard_delete"] = runtime_kwargs["hard_delete"]
            elif "hard_delete" in conn_settings and conn_settings["hard_delete"] is not None:
                hard_delete_kw["hard_delete"] = conn_settings["hard_delete"]
            snowflake_config = config_cls(
                study_key=config.study_key,
                batch_size=config.batch_size,
                max_retries=config.max_retries,
                retry_backoff=config.retry_backoff,
                idempotent=config.idempotent,
                extra=config.extra,
                quality_gate_enabled=config.quality_gate_enabled,
                min_schema_readiness_score=config.min_schema_readiness_score,
                account=runtime_kwargs.get("account") or conn_settings.get("account", ""),
                user=runtime_kwargs.get("user") or conn_settings.get("user", ""),
                password=conn_settings.get("password", ""),
                database=runtime_kwargs.get("database") or conn_settings.get("database", ""),
                schema=runtime_kwargs.get("schema") or conn_settings.get("schema", "PUBLIC"),
                warehouse=runtime_kwargs.get("warehouse") or conn_settings.get("warehouse", ""),
                stage=runtime_kwargs.get("stage") or conn_settings.get("stage", ""),
                table=runtime_kwargs.get("table") or conn_settings.get("table", ""),
                stage_prefix=(
                    runtime_kwargs.get("stage_prefix")
                    or runtime_kwargs.get("prefix")
                    or conn_settings.get("stage_prefix", "imednet")
                ),
                prefix=runtime_kwargs.get("prefix"),
                local_staging_dir=(
                    runtime_kwargs.get("local_staging_dir")
                    or conn_settings.get("local_staging_dir")
                ),
                manifest_path=(
                    runtime_kwargs.get("manifest_path") or conn_settings.get("manifest_path")
                ),
                **hard_delete_kw,
                **stream_opts,
            )
            return sink_cls(config=snowflake_config)

        if dest == "neo4j":
            sink_cls = getattr(sinks, "Neo4jExportSink", None)
            config_cls = getattr(sinks, "Neo4jSinkConfig", None)
            if sink_cls is None or config_cls is None:
                raise AirflowException(
                    "Neo4j sink is not available in the installed sinks plugin. "
                    "Please install the required sink dependencies: pip install 'imednet-plugins-sinks[neo4j]'"
                )
            neo4j_auth = (
                ("", "")
                if _is_allowed_auth_none("auth", runtime_kwargs.get("auth"))
                else conn_settings.get("auth", ("", ""))
            )
            neo4j_config = config_cls(
                study_key=config.study_key,
                batch_size=config.batch_size,
                max_retries=config.max_retries,
                retry_backoff=config.retry_backoff,
                idempotent=config.idempotent,
                extra=config.extra,
                quality_gate_enabled=config.quality_gate_enabled,
                min_schema_readiness_score=config.min_schema_readiness_score,
                uri=runtime_kwargs.get("uri") or conn_settings.get("uri", ""),
                auth=neo4j_auth,
                database=runtime_kwargs.get("database") or conn_settings.get("database", "neo4j"),
                **stream_opts,
            )
            return sink_cls(config=neo4j_config)

        if dest == "mongodb":
            sink_cls = getattr(sinks, "MongoDbExportSink", None)
            config_cls = getattr(sinks, "MongoDbSinkConfig", None)
            if sink_cls is None or config_cls is None:
                raise AirflowException(
                    "MongoDB sink is not available in the installed sinks plugin. "
                    "Please install the required sink dependencies: pip install 'imednet-plugins-sinks[mongodb]'"
                )
            kw_uri = runtime_kwargs.get("uri")
            if isinstance(kw_uri, str) and kw_uri:
                mongo_uri = _inject_uri_credentials(
                    kw_uri,
                    conn_settings.get("user"),
                    conn_settings.get("password"),
                )
            else:
                mongo_uri = conn_settings.get("uri", "")
            mongo_config = config_cls(
                study_key=config.study_key,
                batch_size=config.batch_size,
                max_retries=config.max_retries,
                retry_backoff=config.retry_backoff,
                idempotent=config.idempotent,
                extra=config.extra,
                quality_gate_enabled=config.quality_gate_enabled,
                min_schema_readiness_score=config.min_schema_readiness_score,
                uri=mongo_uri,
                database=runtime_kwargs.get("database") or conn_settings.get("database", ""),
                collection=runtime_kwargs.get("collection") or conn_settings.get("collection", ""),
                **stream_opts,
            )
            return sink_cls(config=mongo_config)

        return None

    def _build_openlineage_datasets(self, dataset_cls: Any) -> tuple[list[Any], list[Any]]:
        """Construct redacted OpenLineage source and destination datasets."""
        source_namespace = _lineage_authority(ImednetHook(self.imednet_conn_id).get_base_url())
        source_study = _sanitize_lineage_text(str(self.study_key or "unknown"))
        inputs = [dataset_cls(namespace=source_namespace, name=f"study.{source_study}.records")]

        dest = self._effective_destination()
        runtime_kwargs = dict(self.export_kwargs)
        try:
            conn_settings = self._resolve_destination_connection(dest)
        except Exception:
            conn_settings = {}

        outputs: list[Any] = []
        if dest == "snowflake":
            account = str(
                runtime_kwargs.get("account") or conn_settings.get("account") or "unknown"
            ).strip()
            raw_ns = account if "://" in account else f"snowflake://{account}"
            namespace = _lineage_authority(raw_ns)
            db = str(runtime_kwargs.get("database") or conn_settings.get("database") or "").strip()
            schema = str(
                runtime_kwargs.get("schema") or conn_settings.get("schema") or "PUBLIC"
            ).strip()
            table = str(
                runtime_kwargs.get("table")
                or conn_settings.get("table")
                or self.output_path
                or source_study
            ).strip()
            name = _sanitize_lineage_text(".".join(part for part in (db, schema, table) if part))
            outputs.append(dataset_cls(namespace=namespace, name=name))
        elif dest == "neo4j":
            raw_uri = str(
                runtime_kwargs.get("uri") or conn_settings.get("uri") or "neo4j://localhost"
            ).strip()
            namespace = _lineage_authority(raw_uri)
            database = str(
                runtime_kwargs.get("database") or conn_settings.get("database") or "neo4j"
            ).strip()
            label = str(
                runtime_kwargs.get("label") or conn_settings.get("label") or source_study
            ).strip()
            name = _sanitize_lineage_text(f"{database}.{label}")
            outputs.append(dataset_cls(namespace=namespace, name=name))
        elif dest == "mongodb":
            raw_uri = str(
                runtime_kwargs.get("uri") or conn_settings.get("uri") or "mongodb://localhost"
            ).strip()
            namespace = _lineage_authority(raw_uri)
            database = str(
                runtime_kwargs.get("database") or conn_settings.get("database") or ""
            ).strip()
            collection = str(
                runtime_kwargs.get("collection") or conn_settings.get("collection") or source_study
            ).strip()
            raw_name = f"{database}.{collection}" if database else collection
            outputs.append(dataset_cls(namespace=namespace, name=_sanitize_lineage_text(raw_name)))
        elif dest in {"duckdb", "duckdb_by_form"}:
            raw_db_path = str(
                self.output_path
                or runtime_kwargs.get("db_path")
                or runtime_kwargs.get("path")
                or conn_settings.get("db_path")
                or ":memory:"
            ).strip()
            safe_db_path = _sanitize_lineage_text(raw_db_path)
            table_target = str(
                runtime_kwargs.get("table_name")
                or runtime_kwargs.get("table")
                or conn_settings.get("table_name")
                or ("forms" if dest == "duckdb_by_form" else "records")
            ).strip()
            raw_name = f"{safe_db_path}.{table_target}" if safe_db_path else table_target
            outputs.append(dataset_cls(namespace="duckdb", name=_sanitize_lineage_text(raw_name)))
        elif dest in {"sql", "sql_by_form", "long_sql"}:
            raw_conn = str(
                conn_settings.get("conn_str")
                or runtime_kwargs.get("conn_str")
                or (
                    self.output_path
                    if isinstance(self.output_path, str) and "://" in self.output_path
                    else ""
                )
                or "sql://database"
            ).strip()
            safe_conn = _sanitize_lineage_text(raw_conn)
            table_target = str(
                (
                    self.output_path
                    if isinstance(self.output_path, str) and "://" not in self.output_path
                    else None
                )
                or runtime_kwargs.get("table")
                or runtime_kwargs.get("table_name")
                or conn_settings.get("table")
                or ("records" if dest == "sql_by_form" else source_study)
            ).strip()
            if safe_conn.startswith("sqlite:"):
                sqlite_path = safe_conn.split(":///", 1)[-1] if ":///" in safe_conn else ":memory:"
                namespace = "sqlite"
                raw_name = f"{sqlite_path}.{table_target}" if sqlite_path else table_target
            elif "://" in raw_conn:
                parsed_sql = urlsplit(raw_conn)
                namespace = _lineage_authority(raw_conn, normalize_sql_scheme=True)
                db_path = parsed_sql.path.lstrip("/")
                raw_name = (
                    f"{db_path}.{table_target}"
                    if db_path and not table_target.startswith(f"{db_path}.")
                    else table_target
                )
            else:
                namespace = "sql"
                raw_name = table_target
            outputs.append(dataset_cls(namespace=namespace, name=_sanitize_lineage_text(raw_name)))
        elif self.output_path:
            raw_target = str(self.output_path)
            if "://" in raw_target:
                parsed_out = urlsplit(raw_target)
                namespace = _lineage_authority(raw_target)
                name = parsed_out.path.lstrip("/") or _sanitize_lineage_text(raw_target)
            else:
                namespace = "file"
                name = _sanitize_lineage_text(raw_target)
            outputs.append(dataset_cls(namespace=namespace, name=_sanitize_lineage_text(name)))

        return inputs, outputs

    def get_openlineage_facets_on_start(self) -> Any:
        """Return OpenLineage source and destination datasets prior to execution."""
        lineage_types = _load_openlineage_types()
        if lineage_types is None:
            return None
        operator_lineage_cls, dataset_cls = lineage_types
        inputs, outputs = self._build_openlineage_datasets(dataset_cls)
        return operator_lineage_cls(inputs=inputs, outputs=outputs)

    def get_openlineage_facets_on_complete(self, task_instance: Any = None) -> Any:
        """Return OpenLineage source and destination datasets after execution completes."""
        _ = task_instance
        lineage_types = _load_openlineage_types()
        if lineage_types is None:
            return None
        operator_lineage_cls, dataset_cls = lineage_types
        inputs, outputs = self._build_openlineage_datasets(dataset_cls)
        return operator_lineage_cls(inputs=inputs, outputs=outputs)

    def execute(self, context: Context) -> str | None:
        """Execute the export task."""
        _ = context
        # Validate templated fields at runtime prior to any export work.
        self._validate_templated_attributes()
        validate_path_segment(self.study_key, name="study_key")
        runtime_kwargs = self._get_runtime_export_kwargs()
        # Resolve destination early to fail fast on invalid configs (like legacy _get_export_callable)
        dest = self.destination
        if not dest and self.export_func:
            dest = self.export_func.replace("export_to_", "")
        elif not dest:
            dest = "csv"

        export_callable = None
        if dest in _TABULAR_EXPORTS:
            export_callable = getattr(export, f"export_to_{dest}", _TABULAR_EXPORTS[dest])
        elif self.export_func and hasattr(export, self.export_func):
            export_callable = getattr(export, self.export_func)

        if not export_callable and dest not in ("snowflake", "neo4j", "mongodb"):
            raise AirflowException(
                f"Unsupported export_func '{self.export_func or dest}'. Expected a valid destination or tabular function."
            )

        stream_opts = self._collect_streaming_options(runtime_kwargs)
        sink_stream_opts = {k: v for k, v in stream_opts.items() if k != "mask_phi"}

        config = sink_base.SinkConfig(
            study_key=self.study_key,
            batch_size=int(runtime_kwargs.get("batch_size", self.batch_size)),
            max_buffered_records=runtime_kwargs.get("max_buffered_records"),
            max_retries=int(runtime_kwargs.get("max_retries", self.max_retries)),
            retry_backoff=float(runtime_kwargs.get("retry_backoff", 1.0)),
            idempotent=bool(runtime_kwargs.get("idempotent", self.idempotent)),
            mask_phi=bool(runtime_kwargs.get("mask_phi", self.mask_phi)),
            extra=runtime_kwargs,
            quality_gate_enabled=bool(
                runtime_kwargs.get("quality_gate_enabled", self.quality_gate_enabled)
            ),
            min_schema_readiness_score=float(
                runtime_kwargs.get(
                    "min_schema_readiness_score",
                    self.min_schema_readiness_score,
                )
            ),
            **sink_stream_opts,
        )

        with closing(self._get_sdk()) as sdk:
            sink = None
            if not export_callable:
                sink = self._resolve_sink(config, sdk=sdk)
                if not sink:
                    raise AirflowException(
                        f"Unsupported export_func '{self.export_func or dest}'. Expected a valid destination or tabular function."
                    )

            if sink:
                run_token = uuid.uuid4().hex[:12]
                if isinstance(context, Mapping):
                    raw_run_id = context.get("run_id") or context.get("ts_nodash")
                    if isinstance(raw_run_id, str) and raw_run_id.strip():
                        run_token = f"{raw_run_id.strip()}/{uuid.uuid4().hex[:8]}"
                default_sink_stream = self._default_airflow_stream_name(
                    dest,
                    sink_prefix=True,
                    context=context,
                    runtime_kwargs=runtime_kwargs,
                )
                default_provider_name = "file" if config.ledger_path is not None else "airflow"
                if config.stream_name is None and (
                    config.incremental or config.state_provider is not None
                ):
                    seeded_sink_ts = self._seed_pre_upgrade_watermark(
                        dest=dest,
                        scoped_stream=default_sink_stream,
                        state_provider=config.state_provider,
                        ledger_path=config.ledger_path,
                        default_provider_name=default_provider_name,
                    )
                    if seeded_sink_ts is not None and config.modified_since is None:
                        config.modified_since = seeded_sink_ts
                sink_base.run_streaming_export(
                    sdk,
                    self.study_key,
                    sink,
                    config,
                    stream_name=default_sink_stream,
                    batch_id_prefix=f"{self.study_key}/batch/{run_token}",
                    quality_gate_fn=sink_base.apply_quality_gate,
                    default_provider_name=default_provider_name,
                )
            else:
                # Execution path for tabular functions (dispatch via getattr so test mocks are preserved)
                if not export_callable:
                    raise AirflowException(f"Unsupported destination or export_func '{dest}'")

                explicit_opts: frozenset[str] = getattr(
                    self, "_explicit_tabular_options", frozenset()
                )
                implicit_gate = (
                    "quality_gate_enabled" not in runtime_kwargs
                    and "quality_gate_enabled" not in explicit_opts
                    and not bool(getattr(self, "quality_gate_enabled", False))
                )
                built_tabular_kwargs = self._build_tabular_kwargs(
                    runtime_kwargs,
                    dest=dest,
                    context=context,
                )
                if (
                    "stream_name" not in stream_opts
                    and "stream_name" in built_tabular_kwargs
                    and (
                        built_tabular_kwargs.get("incremental")
                        or built_tabular_kwargs.get("state_provider") is not None
                    )
                ):
                    default_tab_provider = (
                        "file" if built_tabular_kwargs.get("ledger_path") is not None else "airflow"
                    )
                    seeded_tab_ts = self._seed_pre_upgrade_watermark(
                        dest=dest,
                        scoped_stream=str(built_tabular_kwargs["stream_name"]),
                        state_provider=built_tabular_kwargs.get("state_provider"),
                        ledger_path=built_tabular_kwargs.get("ledger_path"),
                        default_provider_name=default_tab_provider,
                    )
                    if seeded_tab_ts is not None and "modified_since" not in built_tabular_kwargs:
                        built_tabular_kwargs["modified_since"] = seeded_tab_ts
                tabular_kwargs = self._filter_callable_kwargs(
                    export_callable,
                    built_tabular_kwargs,
                    implicit_quality_gate=implicit_gate,
                )
                if dest == "duckdb_by_form":
                    conn_settings = self._resolve_destination_connection(dest)
                    db_path_arg = (
                        self.output_path
                        or tabular_kwargs.pop("db_path", None)
                        or tabular_kwargs.pop("path", None)
                        or conn_settings.get("db_path")
                    )
                    if db_path_arg is not None:
                        _validate_no_secrets_in_value(db_path_arg, field_name="output_path")
                    export_callable(
                        sdk,
                        self.study_key,
                        db_path_arg,
                        **tabular_kwargs,
                    )
                    return redact_uri(str(db_path_arg)) if db_path_arg is not None else None
                if dest == "hive_parquet":
                    base_dir_arg = (
                        self.output_path
                        or tabular_kwargs.pop("base_dir", None)
                        or tabular_kwargs.pop("path", None)
                    )
                    if base_dir_arg is not None:
                        _validate_no_secrets_in_value(base_dir_arg, field_name="output_path")
                    export_callable(
                        sdk,
                        self.study_key,
                        base_dir_arg,
                        **tabular_kwargs,
                    )
                    return redact_uri(str(base_dir_arg)) if base_dir_arg is not None else None
                if dest == "duckdb":
                    conn_settings = self._resolve_destination_connection(dest)
                    db_path_arg = (
                        self.output_path
                        or tabular_kwargs.pop("db_path", None)
                        or tabular_kwargs.pop("path", None)
                        or conn_settings.get("db_path")
                    )
                    if db_path_arg is not None:
                        _validate_no_secrets_in_value(db_path_arg, field_name="output_path")
                    table_name_arg = (
                        tabular_kwargs.pop("table_name", None)
                        or tabular_kwargs.pop("table", None)
                        or conn_settings.get("table_name")
                        or "records"
                    )
                    export_callable(
                        sdk,
                        self.study_key,
                        db_path_arg,
                        table_name_arg,
                        **tabular_kwargs,
                    )
                    return redact_uri(str(db_path_arg)) if db_path_arg is not None else None
                if dest in ("sql", "sql_by_form", "long_sql"):
                    conn_settings = self._resolve_destination_connection(dest)
                    if dest == "sql_by_form":
                        kw_conn_str = tabular_kwargs.pop("conn_str", None)
                        conn_str_arg = (
                            self.output_path or kw_conn_str or conn_settings.get("conn_str")
                        )
                        export_callable(sdk, self.study_key, conn_str_arg, **tabular_kwargs)
                        return redact_uri(str(conn_str_arg)) if conn_str_arg is not None else None
                    kw_table = tabular_kwargs.pop("table", None)
                    kw_table_name = tabular_kwargs.pop("table_name", None)
                    table_arg = (
                        self.output_path or kw_table or kw_table_name or conn_settings.get("table")
                    )
                    if "conn_str" not in tabular_kwargs and conn_settings.get("conn_str"):
                        tabular_kwargs["conn_str"] = conn_settings["conn_str"]
                    export_callable(sdk, self.study_key, table_arg, **tabular_kwargs)
                    if self.output_path is not None:
                        return redact_uri(self.output_path)
                    return redact_uri(str(table_arg)) if table_arg is not None else None
                export_callable(sdk, self.study_key, self.output_path, **tabular_kwargs)
            return redact_uri(self.output_path) if self.output_path is not None else None


__all__ = ["ImednetExportOperator"]
