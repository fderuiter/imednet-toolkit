# apache-airflow-providers-imednet

Apache Airflow provider for iMednet data workflows.

## Install

Install the package via standard pip using the public package distribution name:

```bash
pip install apache-airflow-providers-imednet
```

Optional extras can be installed for OpenLineage dataset facets, warehouse/graph/document sinks, incremental workflow checkpointing, S3/AWS object-store targets, or all integrations together:

```bash
pip install "apache-airflow-providers-imednet[openlineage]"
pip install "apache-airflow-providers-imednet[sinks,workflows]"
pip install "apache-airflow-providers-imednet[amazon]"
pip install "apache-airflow-providers-imednet[all]"
```

Or using the workspace-relative path install command:

```bash
pip install ./packages/providers-airflow
```

**Required Dependencies:**
This package requires `imednet` and an `apache-airflow>=3.2.0,<4.0.0` core environment as parent dependencies.

**Build Targets (Hatchling):**
This package uses Hatchling as its build backend, targeting the `src/apache_airflow_providers_imednet` package directory and registering the `apache_airflow_provider` entry point in `pyproject.toml`.

## Usage Guide

You can integrate iMednet hooks, operators, deferrable sensors, and triggers directly within your Airflow 3.x DAGs:

```python
from apache_airflow_providers_imednet.hooks import ImednetHook
from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
from apache_airflow_providers_imednet.sensors import ImednetJobSensor

hook = ImednetHook(imednet_conn_id="my_imednet_conn")
export_task = ImednetExportOperator(
    task_id="export",
    study_key="{{ params.study_key }}",
    output_path="/tmp/records.csv",
    export_func="export_to_csv",
    modified_since="{{ data_interval_start.isoformat() }}",
    record_filter="recordStatus==Record Complete",
    stream_name="records_{{ params.study_key }}",
)
sensor_task = ImednetJobSensor(
    task_id="wait_for_job",
    study_key="{{ params.study_key }}",
    batch_id="{{ ti.xcom_pull(task_ids='submit_job') }}",
    deferrable=True,
)
```

## Using the ImednetHook

The `ImednetHook` allows you to interact with the iMednet system securely by resolving configurations from Airflow connections. It provides access to the underlying `ImednetSDK` client.

### Getting the SDK Client

To retrieve a configured SDK client to use in your custom operators or tasks, use `get_conn()` (adhering to the standard Airflow `BaseHook` interface) or its alias `get_sdk_client()`:

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

- **Provider Discovery & Connection Testing**: Registers `get_provider_info` via `apache_airflow_provider` with declarative `imednet` UI fields, `ImednetHook.test_connection()`, and `ImednetHook.get_async_conn()`.
- **Connection Hook Management**: Secures connection variables via `ImednetHook`, resolving authorization keys/secrets and auto-masking sensitive values (`api_key`, `security_key`, `oidc_token`, `token`) as `***` during serialization and error reporting.
- **Dynamic Task Mapping & Watermark Templating**: Supports Airflow dynamic task mapping (`study_key`, `output_path`, `export_kwargs`) alongside Jinja-templated incremental watermarks and filters (`modified_since`, `record_filter`, `record_data_filter`, `stream_name`) with automatic secret rejection.
- **Deferrable Job Polling**: `ImednetJobSensor(deferrable=True)` suspends worker execution and polls asynchronously in the Airflow triggerer via `ImednetJobTrigger`, `AsyncImednetSDK`, and `AsyncJobPoller`.
- **OpenLineage Dataset Facets**: `ImednetExportOperator` lazily emits redacted source and destination OpenLineage `Dataset` facets when `apache-airflow-providers-openlineage` is installed.
- **Lazy Database Ingestion & Tabular Exporters**: Dynamically resolves plugin sinks (`snowflake`, `neo4j`, `mongodb`) and tabular serialization adapters (`csv`, `parquet`, `excel`, `json`, `sql`, `duckdb`).

## Component Description Table

| Component | Description |
|-----------|-------------|
| `get_provider_info` | Declares Airflow 3.x provider metadata, UI connection fields, hooks, operators, sensors, and triggers. |
| `ImednetHook` | Resolves sync/async SDK clients, tests API connectivity, and masks sensitive values. |
| `ImednetExportOperator` | Unified export operator with templated watermarks/filters and optional redacted OpenLineage facets. |
| `ImednetJobSensor` | Monitors batch jobs synchronously (`poke`) or asynchronously (`deferrable=True`). |
| `ImednetJobTrigger` | Polls batch jobs in the Airflow triggerer via `AsyncImednetSDK` without persisting secrets. |
| `_airflow_compat` | Compatibility layer for optional Airflow runtime and offline unit test environments. |
