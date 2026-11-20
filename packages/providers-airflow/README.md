# apache-airflow-providers-imednet

Apache Airflow provider for iMednet data workflows.

## Install

Install the package via standard pip using the public package distribution name:

```bash
pip install apache-airflow-providers-imednet
```

Install optional extras for destination sinks, tabular/DuckDB exports, OpenLineage dataset facets, S3/AWS object-store targets, or all integrations together:

```bash
pip install "apache-airflow-providers-imednet[export]"
pip install "apache-airflow-providers-imednet[duckdb]"
pip install "apache-airflow-providers-imednet[snowflake,mongodb,neo4j]"
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
This package requires `imednet`, `imednet-workflows` (for incremental XCom checkpointing), and `apache-airflow>=3.2.0,<4.0.0`.

**Build Targets (Hatchling):**
This package uses Hatchling as its build backend, targeting `src/apache_airflow_providers_imednet` (with PEP 420 namespace bridge `src/airflow`) and registering the `apache_airflow_provider` discovery entry point in `pyproject.toml`.

## Usage Guide

You can integrate iMednet hooks, operators, deferrable sensors, and triggers directly within your Airflow 3.x DAGs (see `examples/airflow/airflow_multi_study_pipeline.py` for a full dynamic-mapping reference):

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
        modified_since="{{ prev_data_interval_start_success or '' }}",
        record_filter="recordStatus==Record Complete",
        stream_name="records_{{ params.study_key }}",
        incremental=True,
    )
    sensor_task = ImednetJobSensor(
        task_id="wait_for_job",
        study_key="{{ params.study_key }}",
        batch_id="{{ params.batch_id }}",
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
