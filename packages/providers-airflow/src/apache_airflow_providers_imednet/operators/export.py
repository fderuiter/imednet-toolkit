"""Airflow operator for exporting study records."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote_plus

from imednet import ImednetSDK
from imednet.spi import sink_base
from imednet.spi.sinks import redact_uri
from imednet.spi.utils import validate_path_segment

from .. import export
from .._airflow_compat import AirflowException, Context
from ..hooks import ImednetHook

if TYPE_CHECKING:
    try:
        from airflow.sdk.bases.operator import BaseOperator
    except ImportError:
        try:
            from airflow.models import BaseOperator  # type: ignore[no-redef]
        except ImportError:

            class BaseOperator:  # type: ignore[no-redef]
                """Fallback BaseOperator typing stub."""

                template_fields: Sequence[str] = ()

                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    """Initialize fallback BaseOperator."""
else:  # pragma: no cover - optional Airflow dependency

    def _resolve_base_operator() -> type[Any]:
        sdk_bases = sys.modules.get("airflow.sdk.bases")
        if (
            sdk_bases is None
            or getattr(sdk_bases, "__file__", None) is not None
            or hasattr(sdk_bases, "operator")
        ):
            try:
                from airflow.sdk.bases.operator import BaseOperator as SdkBaseOperator

                return SdkBaseOperator
            except (ImportError, ModuleNotFoundError):
                pass
        try:
            from airflow.models import BaseOperator as LegacyBaseOperator

            return LegacyBaseOperator
        except (ImportError, ModuleNotFoundError):

            class _FallbackBaseOperator:
                """Fallback BaseOperator for offline tests."""

                template_fields: Sequence[str] = ()

                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    """Initialize fallback BaseOperator."""

            return _FallbackBaseOperator

    BaseOperator = _resolve_base_operator()


logger = logging.getLogger(__name__)

# Fallback mapping for tabular exports if the user explicitly needs them or backward compatibility
_TABULAR_EXPORTS = {
    "csv": export.export_to_csv,
    "parquet": export.export_to_parquet,
    "excel": export.export_to_excel,
    "json": export.export_to_json,
    "sql": export.export_to_sql,
    "sql_by_form": export.export_to_sql_by_form,
    "long_sql": export.export_to_long_sql,
    "duckdb": export.export_to_duckdb,
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
}


def _has_embedded_uri_credentials(value: str) -> bool:
    """Return True if *value* embeds plaintext URI userinfo credentials."""
    if "://" not in value or "://***@" in value:
        return False
    return redact_uri(value) != value


def _validate_no_secrets_in_value(value: Any, *, field_name: str = "export_kwargs") -> None:
    """Reject secret keys or embedded URI credentials in templated operator fields."""
    if isinstance(value, str):
        if _has_embedded_uri_credentials(value):
            raise AirflowException(
                f"Plaintext URI credentials are not allowed in '{field_name}'. "
                "Configure an Airflow Connection and pass 'destination_conn_id' instead."
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_norm = str(key).strip().lower()
            if key_norm in _SECRET_EXPORT_KEYS:
                raise AirflowException(
                    f"Secret key '{key}' is not allowed in '{field_name}'. "
                    "Configure an Airflow Connection and pass 'destination_conn_id' instead."
                )
            _validate_no_secrets_in_value(item, field_name=field_name)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _validate_no_secrets_in_value(item, field_name=field_name)


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
            frozenset(key for key in _TABULAR_OPTION_DEFAULTS if key in kwargs),
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
        self.modified_since = modified_since
        self.record_filter = record_filter
        self.record_data_filter = record_data_filter
        self.include_deleted = include_deleted
        self.incremental = incremental
        self.stream_name = stream_name
        self.ledger_path = ledger_path
        self.state_provider = state_provider

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

    @property
    def modified_since(self) -> Any:
        """Return the configured watermark filter."""
        return getattr(self, "_modified_since", None)

    @modified_since.setter
    def modified_since(self, value: Any) -> None:
        """Validate and set modified_since, rejecting embedded secrets."""
        if value is not None:
            _validate_no_secrets_in_value(value, field_name="modified_since")
        self._modified_since = value

    @property
    def record_filter(self) -> Any:
        """Return the configured record envelope filter."""
        return getattr(self, "_record_filter", None)

    @record_filter.setter
    def record_filter(self, value: Any) -> None:
        """Validate and set record_filter, rejecting secret keys or URI credentials."""
        if value is not None:
            _validate_no_secrets_in_value(value, field_name="record_filter")
        self._record_filter = value

    @property
    def record_data_filter(self) -> str | None:
        """Return the configured record_data filter expression."""
        return getattr(self, "_record_data_filter", None)

    @record_data_filter.setter
    def record_data_filter(self, value: str | None) -> None:
        """Validate and set record_data_filter, rejecting embedded URI credentials."""
        if value is not None:
            _validate_no_secrets_in_value(value, field_name="record_data_filter")
        self._record_data_filter = value

    @property
    def stream_name(self) -> str | None:
        """Return the configured incremental checkpoint stream name."""
        return getattr(self, "_stream_name", None)

    @stream_name.setter
    def stream_name(self, value: str | None) -> None:
        """Validate and set stream_name, rejecting embedded URI credentials."""
        if value is not None:
            _validate_no_secrets_in_value(value, field_name="stream_name")
        self._stream_name = value

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
            if attr_val != default_val:
                opts[key] = attr_val
            elif key in runtime_kwargs and runtime_kwargs[key] != default_val:
                opts[key] = runtime_kwargs[key]
        effective_mask_phi = (
            bool(runtime_kwargs["mask_phi"])
            if "mask_phi" in runtime_kwargs
            else bool(getattr(self, "mask_phi", True))
        )
        explicit: frozenset[str] = getattr(self, "_explicit_tabular_options", frozenset())
        if not effective_mask_phi or "mask_phi" in runtime_kwargs or "mask_phi" in explicit:
            opts["mask_phi"] = effective_mask_phi
        return opts

    def _build_tabular_kwargs(self, runtime_kwargs: dict[str, Any]) -> dict[str, Any]:
        """Merge explicitly configured operational parameters into tabular export kwargs."""
        merged = dict(runtime_kwargs)
        explicit: frozenset[str] = getattr(self, "_explicit_tabular_options", frozenset())
        for key, default_val in _TABULAR_OPTION_DEFAULTS.items():
            current_val = getattr(self, key)
            if key not in merged and (key in explicit or current_val != default_val):
                merged[key] = current_val
        for key, val in self._collect_streaming_options(runtime_kwargs).items():
            merged.setdefault(key, val)
        if (
            merged.get("incremental")
            and "state_provider" not in merged
            and "ledger_path" not in merged
        ):
            merged["state_provider"] = sink_base.get_state_provider(provider_name="airflow")
        return merged

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
                auth=conn_settings.get("auth", ("", "")),
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

    def _default_incremental_stream_name(self, dest: str, context: Context) -> str:
        """Build a task-scoped default checkpoint stream name when stream_name is omitted."""
        task_id = getattr(self, "task_id", None)
        base_name = (
            f"airflow_{dest}_{task_id}"
            if isinstance(task_id, str) and task_id
            else f"airflow_{dest}"
        )
        if isinstance(context, Mapping):
            ti = context.get("ti")
            map_index = getattr(ti, "map_index", None)
            if type(map_index) is int and map_index >= 0:
                return f"{base_name}__map_{map_index}"
        return base_name

    def execute(self, context: Context) -> str | None:
        """Execute the export task."""
        # Validate templated fields at runtime prior to any export work.
        validate_path_segment(self.study_key, name="study_key")
        for templated_field in (
            "output_path",
            "modified_since",
            "record_filter",
            "record_data_filter",
            "stream_name",
        ):
            field_val = getattr(self, templated_field, None)
            if field_val is not None:
                _validate_no_secrets_in_value(field_val, field_name=templated_field)
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

        default_stream_name = self._default_incremental_stream_name(dest, context)

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
                stream_config = config
                if (
                    dest == "snowflake"
                    and config.idempotent
                    and not config.include_deleted
                    and "include_deleted" not in runtime_kwargs
                    and not config.incremental
                    and config.modified_since is None
                    and config.state_provider is None
                ):
                    stream_config = replace(config, include_deleted=True)
                sink_base.run_streaming_export(
                    sdk,
                    self.study_key,
                    sink,
                    stream_config,
                    stream_name=default_stream_name,
                    batch_id_prefix=f"{self.study_key}/batch/{run_token}",
                    default_provider_name="airflow",
                )
            else:
                # Execution path for tabular functions (dispatch via getattr so test mocks are preserved)
                if not export_callable:
                    raise AirflowException(f"Unsupported destination or export_func '{dest}'")

                tabular_kwargs = self._build_tabular_kwargs(runtime_kwargs)
                if tabular_kwargs.get("incremental"):
                    tabular_kwargs.setdefault("stream_name", default_stream_name)
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
