# apache-airflow-providers-imednet

Apache Airflow provider for iMednet data workflows.

## Install

Install the package via standard pip using the public package distribution name:

```bash
pip install apache-airflow-providers-imednet
```

Install optional destination extras for tabular/DuckDB exports or database sinks:

```bash
pip install "apache-airflow-providers-imednet[export]"
pip install "apache-airflow-providers-imednet[duckdb]"
pip install "apache-airflow-providers-imednet[snowflake,mongodb,neo4j]"
```

Or using the workspace-relative path install command:

```bash
pip install ./packages/providers-airflow
```

**Required Dependencies:**
This package requires `imednet`, `imednet-workflows` (for incremental Airflow Variable checkpointing), and `apache-airflow>=3.2.0,<4.0.0`.

**Build Targets (Hatchling):**
This package uses Hatchling as its build backend, targeting the `src/apache_airflow_providers_imednet` package directory and registering the `apache_airflow_provider` discovery entry point in `pyproject.toml`.

## Usage Guide

You can integrate iMednet hooks, operators, and sensors directly within your Airflow 3 DAGs (see `examples/airflow/airflow_multi_study_pipeline.py` for a full dynamic-mapping reference):

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
    )
    sensor_task = ImednetJobSensor(
        task_id="wait_for_job",
        study_key="{{ params.study_key }}",
        batch_id="{{ params.batch_id }}",
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

- **Connection Hook Management**: Secures connection variables via `ImednetHook`, resolving authorization keys/secrets and auto-masking sensitive values (`api_key`, `security_key`, `token`) as `***` during serialization.
- **Dynamic Task Mapping & Templating**: Supports Airflow standard task mapping via Jinja-templated fields (`study_key`, `output_path`, `export_kwargs`).
- **Lazy Database Ingestion**: Uses dynamic resolution to lazily import database drivers (such as `imednet_sinks`) during execution.
- **State Polling Sensor**: `ImednetJobSensor` monitors asynchronous platform processes with robust exception handling and state polling.
- **Flexible Tabular Exporters**: Exposes tabular serialization adapters for CSV, Parquet, JSON, and SQL formats.

## Component Description Table

| Component | Description |
|-----------|-------------|
| `ImednetHook` | Secures connection variables and handles auto-masking of sensitive values. |
| `ImednetExportOperator` | Airflow operator for executing data export tasks via the iMedNet API. |
| `ImednetJobSensor` | Monitors asynchronous platform processes, handling state polling and exception management. |
| `_airflow_compat` | Compatibility layer for different versions of standard Airflow environments. |
