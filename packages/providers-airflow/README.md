# apache-airflow-providers-imednet

Apache Airflow provider for iMednet data workflows.

## Install

Install the package via standard pip using the public package distribution name:

```bash
pip install apache-airflow-providers-imednet
```

Install optional destination, lineage, or cloud extras as needed:

```bash
pip install "apache-airflow-providers-imednet[export]"
pip install "apache-airflow-providers-imednet[duckdb]"
pip install "apache-airflow-providers-imednet[sinks]"
pip install "apache-airflow-providers-imednet[workflows]"
pip install "apache-airflow-providers-imednet[snowflake,mongodb,neo4j]"
pip install "apache-airflow-providers-imednet[openlineage,amazon]"
pip install "apache-airflow-providers-imednet[all]"
```

Or using the workspace-relative path install command:

```bash
pip install ./packages/providers-airflow
```

**Required Dependencies:**
This package requires `imednet`, `imednet-workflows` (for incremental Airflow `Variable` checkpointing), and `apache-airflow>=3.2.0,<4.0.0`.

**Build Targets & Namespace Compatibility (Hatchling):**
This package uses Hatchling as its build backend, targeting `src/apache_airflow_providers_imednet`, registering the `apache_airflow_provider` discovery entry point (`apache_airflow_providers_imednet:get_provider_info`), and dynamically aliasing `airflow.providers.imednet.*` to the public provider modules.

## Usage Guide

You can integrate iMednet hooks, operators, deferrable sensors, and triggers directly within your Airflow 3 DAGs (see `examples/airflow/airflow_multi_study_pipeline.py` for a full dynamic-mapping reference):

```python
from datetime import datetime

from airflow.sdk import DAG
from apache_airflow_providers_imednet import (
    ImednetExportOperator,
    ImednetHook,
    ImednetJobSensor,
)

with DAG(
    dag_id="imednet_pipeline",
    schedule=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
):
    hook = ImednetHook(imednet_conn_id="imednet_default")
    export_task = ImednetExportOperator(
        task_id="export_records",
        study_key="{{ params.study_key }}",
        destination="duckdb",
        output_path="/tmp/study.duckdb",
        export_kwargs={"table_name": "clinical_records"},
        incremental=True,
        modified_since="{{ data_interval_start.isoformat() }}",
    )
    sensor_task = ImednetJobSensor(
        task_id="wait_for_job",
        study_key="{{ params.study_key }}",
        batch_id="{{ params.batch_id }}",
        deferrable=True,
    )
```

## Using the ImednetHook

The `ImednetHook` resolves credentials from Airflow connections (`imednet` connection type, including custom Airflow UI form widgets and `extra__imednet__*` fields), provides `test_connection()` with automatic secret scrubbing, and exposes both synchronous (`ImednetSDK`) and asynchronous (`AsyncImednetSDK`) clients.

### Getting the SDK Client

To retrieve a configured SDK client to use in your custom operators, tasks, or triggers, use `get_conn()` (or `get_sdk_client()`) for synchronous tasks and `await get_async_conn()` for async triggers:

```python
from apache_airflow_providers_imednet.hooks import ImednetHook


def my_task_logic() -> None:
    hook = ImednetHook(imednet_conn_id="imednet_default")
    # Retrieve the active ImednetSDK client using the standard BaseHook entry point:
    client = hook.get_conn()
    # Alternatively, hook.get_sdk_client() can also be used:
    # client = hook.get_sdk_client()

    # Use the client to interact with iMednet
    studies = list(client.studies.list())
    print(len(studies))
```

## Features

- **Airflow 3 Provider Metadata & UI Form**: Registers the `imednet` connection type with typed UI form widgets (`base_url`, password-masked `oidc_token`, `timeout`, `strict_mode`, `simulation_mode`), `get_ui_field_behaviour()`, and redacted `test_connection()`.
- **Connection Hook Management**: Resolves sync (`get_conn()`) and async (`get_async_conn()`) SDK clients via `ImednetHook`, masking sensitive values (`api_key`, `security_key`, `oidc_token`, `token`) as `***`.
- **Dynamic Task Mapping & Watermark Templating**: Supports Jinja-templated runtime fields (`study_key`, `output_path`, `export_kwargs`, `modified_since`, `record_filter`, `record_data_filter`, `stream_name`) while rejecting inline credentials.
- **Deferrable Job Polling**: `ImednetJobSensor(deferrable=True)` yields worker slots to `ImednetJobTrigger`, polling asynchronously via `AsyncImednetSDK` and `AsyncJobPoller` without persisting secrets in trigger serialization.
- **OpenLineage Dataset Facets**: Emits `OperatorLineage` inputs and outputs across `snowflake`, `duckdb`, `sql` / `sql_by_form` / `long_sql`, `mongodb`, `neo4j`, cloud object URIs (`s3://`, `gs://`, `abfss://`), and local files.
- **Lazy Database Ingestion & Tabular Exporters**: Lazily resolves database sinks (`imednet_sinks`) and tabular serialization adapters for CSV, Parquet, JSON, DuckDB, and SQL (`sql`, `sql_by_form`, `long_sql`) formats.

## Component Description Table

| Component | Description |
|-----------|-------------|
| `ImednetHook` | Resolves sync/async SDK clients, UI form behaviour, connection testing, and credential redaction. |
| `ImednetExportOperator` | Airflow operator for streaming/tabular data exports with Jinja templating and OpenLineage facets. |
| `ImednetJobSensor` | Monitors asynchronous iMednet jobs in synchronous poke/reschedule or deferrable triggerer mode. |
| `ImednetJobTrigger` | Async Airflow trigger polling iMednet jobs via `AsyncImednetSDK` and `AsyncJobPoller`. |
| `_airflow_compat` | Compatibility layer for Airflow 3 Task SDK and offline unit testing environments. |
