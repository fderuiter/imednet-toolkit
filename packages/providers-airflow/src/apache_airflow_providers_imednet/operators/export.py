"""Airflow operator for exporting study records."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from imednet import ImednetSDK
from imednet.spi import sink_base

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
    "excel": export.export_to_excel,
    "json": export.export_to_json,
    "sql": export.export_to_sql,
    "duckdb": export.export_to_duckdb,
}


class ImednetExportOperator(BaseOperator):
    """Unified Airflow operator for exporting study records to any destination."""

    # Fields intended for Airflow `.partial().expand()` runtime mapping.
    mapped_runtime_fields: Sequence[str] = ("study_key", "output_path", "export_kwargs")
    template_fields: Sequence[str] = mapped_runtime_fields
    template_fields_renderers = {"export_kwargs": "json"}  # noqa: RUF012

    def __init__(
        self,
        *,
        study_key: str,
        destination: str | None = None,
        output_path: str | None = None,
        export_func: str | None = None,
        export_kwargs: Mapping[str, Any] | None = None,
        imednet_conn_id: str = "imednet_default",
        batch_size: int = 500,
        max_retries: int = 3,
        idempotent: bool = True,
        **kwargs: Any,
    ) -> None:
        """Initialize the operator.

        :param study_key: The study key identifier.
        :param destination: The destination sink format or database.
        :param output_path: The filesystem path to export to (if applicable).
        :param export_func: The specific legacy tabular export function to use.
        :param export_kwargs: Extra keyword arguments passed to the sink/export function.
        :param imednet_conn_id: Airflow connection ID to use for credentials.
        :param batch_size: Number of records to read and write per batch.
        :param max_retries: Maximum number of export attempts.
        :param idempotent: Whether the export execution should be safely repeatable, automatically deduplicating or replacing existing outputs as appropriate.
        :param kwargs: Additional Airflow BaseOperator arguments.
        """
        super().__init__(**kwargs)
        self.study_key = study_key
        self.destination = destination
        self.output_path = output_path
        self.export_func = export_func
        self.export_kwargs = dict(export_kwargs or {})
        self.imednet_conn_id = imednet_conn_id

        # Common operational parameters
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.idempotent = idempotent

    def _get_sdk(self) -> ImednetSDK:
        """Resolve the SDK client from the configured Airflow connection at execute time."""
        return ImednetHook(self.imednet_conn_id).get_sdk_client()

    def _get_runtime_export_kwargs(self) -> dict[str, Any]:
        """Return a defensive copy of export kwargs for mapped task isolation."""
        return dict(self.export_kwargs)

    def _resolve_sink(self, config: sink_base.SinkConfig, sdk: ImednetSDK | None = None) -> Any:
        """Dynamically load and configure the target sink from the SDK, handling dependencies lazily."""
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

        if dest == "snowflake":
            sink_cls = getattr(sinks, "SnowflakeExportSink", None)
            config_cls = getattr(sinks, "SnowflakeSinkConfig", None)
            if sink_cls is None or config_cls is None:
                raise AirflowException(
                    "Snowflake sink is not available in the installed sinks plugin. "
                    "Please install the required sink dependencies: pip install 'imednet-plugins-sinks[snowflake]'"
                )
            snowflake_config = config_cls(
                study_key=config.study_key,
                batch_size=config.batch_size,
                max_retries=config.max_retries,
                idempotent=config.idempotent,
                extra=config.extra,
                account=self.export_kwargs.get("account", ""),
                user=self.export_kwargs.get("user", ""),
                password=self.export_kwargs.get("password", ""),
                database=self.export_kwargs.get("database", ""),
                schema=self.export_kwargs.get("schema", "PUBLIC"),
                warehouse=self.export_kwargs.get("warehouse", ""),
                stage=self.export_kwargs.get("stage", ""),
                table=self.export_kwargs.get("table", ""),
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
                idempotent=config.idempotent,
                extra=config.extra,
                uri=self.export_kwargs.get("uri", ""),
                auth=self.export_kwargs.get("auth", ("", "")),
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
            mongo_config = config_cls(
                study_key=config.study_key,
                batch_size=config.batch_size,
                max_retries=config.max_retries,
                idempotent=config.idempotent,
                extra=config.extra,
                uri=self.export_kwargs.get("uri", ""),
                database=self.export_kwargs.get("database", ""),
                collection=self.export_kwargs.get("collection", ""),
            )
            return sink_cls(config=mongo_config)

        return None

    def execute(self, context: Context) -> str | None:
        """Execute the export task."""
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

        config = sink_base.SinkConfig(
            study_key=self.study_key,
            batch_size=self.batch_size,
            max_retries=self.max_retries,
            idempotent=self.idempotent,
            extra=self._get_runtime_export_kwargs(),
        )

        sink = None
        if not export_callable:
            sink = self._resolve_sink(config, sdk=sdk)
            if not sink:
                raise AirflowException(
                    f"Unsupported export_func '{self.export_func or dest}'. Expected a valid destination or tabular function."
                )

        if sink:
            # Single execution path for Sink-based destinations
            raw_records = sdk.records.list(study_key=self.study_key, record_data_filter=None)
            records_list = list(
                sink_base.apply_quality_gate(sdk, self.study_key, raw_records, config)
            )
            with sink:
                for i, batch in enumerate(sink_base.iter_batches(records_list, config.batch_size)):
                    sink.write_batch(batch, batch_id=f"{self.study_key}/batch/{i}")
        else:
            # Execution path for legacy tabular functions
            # We dispatch to getattr so mocks in tests are preserved
            if not export_callable:
                raise AirflowException(f"Unsupported destination or export_func '{dest}'")

            export_callable(
                sdk, self.study_key, self.output_path, **self._get_runtime_export_kwargs()
            )
        return self.output_path


__all__ = ["ImednetExportOperator"]
