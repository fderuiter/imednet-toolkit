"""Airflow operator for exporting study records."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast
from urllib.parse import quote_plus

from imednet import ImednetSDK
from imednet.spi import sink_base
from imednet.spi.sinks import redact_uri

from .. import export
from .._airflow_compat import AirflowException, Context
from ..hooks import ImednetHook

try:  # pragma: no cover - optional Airflow dependency
    from airflow.models import BaseOperator
except (ImportError, ModuleNotFoundError):  # pragma: no cover - placeholder fallback

    class BaseOperator:  # type: ignore
        """Fallback BaseOperator for offline tests."""

        template_fields: Sequence[str] = ()

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Initialize fallback BaseOperator."""


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
    "retry_backoff": 1.0,
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


def _resolve_setting(
    runtime_kwargs: Mapping[str, Any],
    key: str,
    operator_val: Any,
    conn_settings: Mapping[str, Any],
    default: Any = None,
) -> Any:
    """Resolve an optional setting across runtime kwargs, operator attributes, and connection metadata."""
    if key in runtime_kwargs and runtime_kwargs[key] is not None:
        return runtime_kwargs[key]
    if operator_val is not None:
        return operator_val
    conn_val = conn_settings.get(key)
    return conn_val if conn_val is not None else default


def _coerce_int(value: Any) -> Any:
    """Coerce numeric strings while preserving booleans so SinkConfig rejects bools."""
    return value if isinstance(value, bool) else int(value)


def _coerce_float(value: Any) -> Any:
    """Coerce numeric strings while preserving booleans so SinkConfig rejects bools."""
    return value if isinstance(value, bool) else float(value)


class ImednetExportOperator(BaseOperator):
    """Unified Airflow operator for exporting study records to any destination."""

    # Fields intended for Airflow `.partial().expand()` runtime mapping.
    mapped_runtime_fields: Sequence[str] = ("study_key", "output_path", "export_kwargs")
    template_fields: Sequence[str] = mapped_runtime_fields
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
        retry_backoff: float = 1.0,
        idempotent: bool = True,
        mask_phi: bool = True,
        quality_gate_enabled: bool = False,
        min_schema_readiness_score: float = 100.0,
        tracer: Any = None,
        database: str | None = None,
        stage_prefix: str | None = None,
        local_staging_dir: Any = None,
        manifest_path: Any = None,
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
        :param retry_backoff: Base delay in seconds between retry attempts.
        :param idempotent: Whether the export execution should be safely repeatable, automatically deduplicating or replacing existing outputs as appropriate.
        :param mask_phi: Whether to mask sensitive clinical PHI fields prior to writing.
        :param quality_gate_enabled: Whether to filter records through the schema readiness quality gate prior to export.
        :param min_schema_readiness_score: Minimum schema readiness score (0-100) required when the quality gate is enabled.
        :param database: Optional target database override for database/graph/warehouse sinks.
        :param stage_prefix: Optional stage path prefix for warehouse sinks.
        :param local_staging_dir: Optional local staging directory for warehouse sinks.
        :param manifest_path: Optional JSON-lines manifest path for audit logging.
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

        # Common operational and destination parameters
        self.batch_size: int = batch_size
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.idempotent = idempotent
        self.mask_phi = mask_phi
        self.quality_gate_enabled = quality_gate_enabled
        self.min_schema_readiness_score = min_schema_readiness_score
        self.tracer = tracer
        self.database = database
        self.stage_prefix = stage_prefix
        self.local_staging_dir = local_staging_dir
        self.manifest_path = manifest_path
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
            "mask_phi": True,
            "tracer": None,
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
        if self.manifest_path is not None and "manifest_path" not in merged:
            merged["manifest_path"] = self.manifest_path
        if (
            merged.get("incremental")
            and "state_provider" not in merged
            and "ledger_path" not in merged
        ):
            merged["state_provider"] = sink_base.get_state_provider(provider_name="airflow")
        return merged

    def _build_sink_target(
        self, config: sink_base.SinkConfig, sdk: ImednetSDK | None = None
    ) -> tuple[Callable[[], Any], Any] | None:
        """Build a validated sink configuration and lazy sink factory for external sinks."""
        dest = self.destination
        if not dest and self.export_func:
            dest = self.export_func.replace("export_to_", "")

        if dest not in ("snowflake", "neo4j", "mongodb"):
            return None

        if sdk is None:
            sdk = self._get_sdk()

        sinks = getattr(sdk, "sinks", None)
        if sinks is None:
            raise AirflowException(
                f"The sinks plugin is required for destination '{dest}' but is not installed. "
                "Please install it using: pip install imednet-plugins-sinks"
            )

        runtime_kwargs = self._get_runtime_export_kwargs()
        conn_settings = self._resolve_destination_connection(dest)
        stream_opts = self._collect_streaming_options(runtime_kwargs)
        common_sink_kwargs: dict[str, Any] = {
            "study_key": config.study_key,
            "batch_size": config.batch_size,
            "max_retries": config.max_retries,
            "retry_backoff": config.retry_backoff,
            "idempotent": config.idempotent,
            "extra": config.extra,
            "quality_gate_enabled": config.quality_gate_enabled,
            "min_schema_readiness_score": config.min_schema_readiness_score,
            **stream_opts,
        }

        if dest == "snowflake":
            sf_sink_cls = getattr(sinks, "SnowflakeExportSink", None)
            sf_config_cls = getattr(sinks, "SnowflakeSinkConfig", None)
            if sf_sink_cls is None or sf_config_cls is None:
                raise AirflowException(
                    "Snowflake sink is not available in the installed sinks plugin. "
                    "Please install the required sink dependencies: pip install 'imednet-plugins-sinks[snowflake]'"
                )
            if "stage_prefix" in runtime_kwargs and runtime_kwargs["stage_prefix"] is not None:
                resolved_stage_prefix = runtime_kwargs["stage_prefix"]
            elif "prefix" in runtime_kwargs and runtime_kwargs["prefix"] is not None:
                resolved_stage_prefix = runtime_kwargs["prefix"]
            elif self.stage_prefix is not None:
                resolved_stage_prefix = self.stage_prefix
            else:
                resolved_stage_prefix = conn_settings.get("stage_prefix", "imednet")
            snowflake_config = sf_config_cls(
                **common_sink_kwargs,
                account=_resolve_setting(runtime_kwargs, "account", None, conn_settings, ""),
                user=_resolve_setting(runtime_kwargs, "user", None, conn_settings, ""),
                password=conn_settings.get("password", ""),
                database=_resolve_setting(
                    runtime_kwargs, "database", self.database, conn_settings, ""
                ),
                schema=_resolve_setting(runtime_kwargs, "schema", None, conn_settings, "PUBLIC"),
                warehouse=_resolve_setting(runtime_kwargs, "warehouse", None, conn_settings, ""),
                stage=_resolve_setting(runtime_kwargs, "stage", None, conn_settings, ""),
                table=_resolve_setting(runtime_kwargs, "table", None, conn_settings, ""),
                stage_prefix=resolved_stage_prefix,
                prefix=runtime_kwargs.get("prefix"),
                local_staging_dir=_resolve_setting(
                    runtime_kwargs,
                    "local_staging_dir",
                    self.local_staging_dir,
                    conn_settings,
                    None,
                ),
                manifest_path=_resolve_setting(
                    runtime_kwargs, "manifest_path", self.manifest_path, conn_settings, None
                ),
            )
            return (lambda: sf_sink_cls(config=snowflake_config), snowflake_config)

        if dest == "neo4j":
            neo_sink_cls = getattr(sinks, "Neo4jExportSink", None)
            neo_config_cls = getattr(sinks, "Neo4jSinkConfig", None)
            if neo_sink_cls is None or neo_config_cls is None:
                raise AirflowException(
                    "Neo4j sink is not available in the installed sinks plugin. "
                    "Please install the required sink dependencies: pip install 'imednet-plugins-sinks[neo4j]'"
                )
            neo4j_config = neo_config_cls(
                **common_sink_kwargs,
                uri=_resolve_setting(runtime_kwargs, "uri", None, conn_settings, ""),
                auth=conn_settings.get("auth", ("", "")),
                database=_resolve_setting(
                    runtime_kwargs, "database", self.database, conn_settings, "neo4j"
                ),
            )
            return (lambda: neo_sink_cls(config=neo4j_config), neo4j_config)

        if dest == "mongodb":
            mongo_sink_cls = getattr(sinks, "MongoDbExportSink", None)
            mongo_config_cls = getattr(sinks, "MongoDbSinkConfig", None)
            if mongo_sink_cls is None or mongo_config_cls is None:
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
            mongo_config = mongo_config_cls(
                **common_sink_kwargs,
                uri=mongo_uri,
                database=_resolve_setting(
                    runtime_kwargs, "database", self.database, conn_settings, ""
                ),
                collection=_resolve_setting(runtime_kwargs, "collection", None, conn_settings, ""),
            )
            return (lambda: mongo_sink_cls(config=mongo_config), mongo_config)

        return None

    def _resolve_sink(self, config: sink_base.SinkConfig, sdk: ImednetSDK | None = None) -> Any:
        """Dynamically load and configure the target sink from the SDK, handling dependencies lazily."""
        target = self._build_sink_target(config, sdk=sdk)
        if target is None:
            return None
        sink_factory, _ = target
        return sink_factory()

    def execute(self, context: Context) -> str | None:
        """Execute the export task."""
        _ = context
        # Validate templated fields at runtime prior to any export work.
        if self.output_path is not None:
            _validate_no_secrets_in_value(self.output_path, field_name="output_path")
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

        sdk = self._get_sdk()
        stream_opts = self._collect_streaming_options(runtime_kwargs)

        config = sink_base.SinkConfig(
            study_key=self.study_key,
            batch_size=_coerce_int(runtime_kwargs.get("batch_size", self.batch_size)),
            max_retries=_coerce_int(runtime_kwargs.get("max_retries", self.max_retries)),
            retry_backoff=_coerce_float(runtime_kwargs.get("retry_backoff", self.retry_backoff)),
            idempotent=bool(runtime_kwargs.get("idempotent", self.idempotent)),
            extra=runtime_kwargs,
            quality_gate_enabled=bool(
                runtime_kwargs.get("quality_gate_enabled", self.quality_gate_enabled)
            ),
            min_schema_readiness_score=_coerce_float(
                runtime_kwargs.get(
                    "min_schema_readiness_score",
                    self.min_schema_readiness_score,
                )
            ),
            **stream_opts,
        )

        if not export_callable:
            sink_target = self._build_sink_target(config, sdk=sdk)
            if sink_target is None:
                raise AirflowException(
                    f"Unsupported export_func '{self.export_func or dest}'. Expected a valid destination or tabular function."
                )
            sink_factory, resolved_cfg = sink_target
            runner_cfg = resolved_cfg if isinstance(resolved_cfg, sink_base.SinkConfig) else config
            sink_base.run_streaming_export(
                sdk,
                self.study_key,
                sink_factory,
                runner_cfg,
                stream_name=f"airflow_{dest}",
                batch_id_prefix=f"{self.study_key}/batch",
                default_provider_name="airflow",
            )
            return redact_uri(self.output_path) if self.output_path is not None else None

        tabular_kwargs = self._build_tabular_kwargs(runtime_kwargs)
        if dest in ("sql", "sql_by_form", "long_sql"):
            conn_settings = self._resolve_destination_connection(dest)
            if dest == "sql_by_form":
                conn_str_arg = (
                    self.output_path
                    or tabular_kwargs.pop("conn_str", None)
                    or conn_settings.get("conn_str")
                )
                export_callable(sdk, self.study_key, conn_str_arg, **tabular_kwargs)
                return redact_uri(str(conn_str_arg)) if conn_str_arg is not None else None
            if (
                self.output_path
                and "://" in self.output_path
                and ("table" in tabular_kwargs or "table_name" in tabular_kwargs)
                and "conn_str" not in tabular_kwargs
                and not conn_settings.get("conn_str")
            ):
                tabular_kwargs["conn_str"] = self.output_path
                table_arg = tabular_kwargs.pop("table", None) or tabular_kwargs.pop(
                    "table_name", None
                )
            else:
                table_arg = (
                    self.output_path
                    or tabular_kwargs.pop("table", None)
                    or tabular_kwargs.pop("table_name", None)
                    or conn_settings.get("table")
                )
                if "conn_str" not in tabular_kwargs and conn_settings.get("conn_str"):
                    tabular_kwargs["conn_str"] = conn_settings["conn_str"]
            export_callable(sdk, self.study_key, table_arg, **tabular_kwargs)
            if self.output_path is not None:
                return redact_uri(self.output_path)
            return redact_uri(str(table_arg)) if table_arg is not None else None

        if dest == "hive_parquet":
            base_dir_arg = (
                self.output_path
                if self.output_path is not None
                else tabular_kwargs.pop("base_dir", None)
            )
            export_callable(sdk, self.study_key, base_dir_arg, **tabular_kwargs)
            return redact_uri(str(base_dir_arg)) if base_dir_arg is not None else None

        if dest in ("duckdb", "duckdb_by_form"):
            db_path_arg = (
                self.output_path
                if self.output_path is not None
                else tabular_kwargs.pop("db_path", None)
            )
            export_callable(sdk, self.study_key, db_path_arg, **tabular_kwargs)
            return redact_uri(str(db_path_arg)) if db_path_arg is not None else None

        export_callable(sdk, self.study_key, self.output_path, **tabular_kwargs)
        return redact_uri(self.output_path) if self.output_path is not None else None


__all__ = ["ImednetExportOperator"]
