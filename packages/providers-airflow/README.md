# apache-airflow-providers-imednet

Apache Airflow provider for iMednet data workflows.

## Install

Install the package via standard pip using the public package distribution name:

```bash
pip install apache-airflow-providers-imednet
```

Or using the workspace-relative path install command:

```bash
pip install ./packages/providers-airflow
```

**Required Dependencies:**
This package requires `imednet` and a standard `apache-airflow` core environment as parent dependencies.
Optionally, install the `amazon` extra for AWS integrations:
```bash
pip install apache-airflow-providers-imednet[amazon]
```

**Build Targets (Hatchling):**
This package uses Hatchling as its build backend, targeting the `src/apache_airflow_providers_imednet` package directory as defined in `pyproject.toml`.

## Usage Guide

You can integrate iMedNet operators and sensors directly within your declarative Airflow DAGs:

```python
from apache_airflow_providers_imednet.hooks import ImednetHook
from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
from apache_airflow_providers_imednet.sensors import ImednetJobSensor

# Example declarative Airflow DAG configuration:
# hook = ImednetHook(imednet_conn_id='my_imednet_conn')
# export_task = ImednetExportOperator(task_id='export', study_key='{{ params.study_key }}')
# sensor_task = ImednetJobSensor(task_id='wait_for_job', job_id='{{ ti.xcom_pull(task_ids="export") }}')
```

## Using the ImednetHook

The `ImednetHook` allows you to interact with the iMednet system securely by resolving configurations from Airflow connections. It provides access to the underlying `ImednetSDK` client.

### Getting the SDK Client (Modern Approach)

To retrieve a configured SDK client to use in your custom operators or tasks, use the `get_sdk_client()` method:

```python
from apache_airflow_providers_imednet.hooks import ImednetHook


def my_task_logic():
    hook = ImednetHook(imednet_conn_id="imednet_default")
    sdk_client = hook.get_sdk_client()

    # Use the sdk_client to interact with iMednet
    studies = sdk_client.studies.list()
```

### Migration Notice: `get_conn()` Removed

The legacy connection helper `ImednetHook.get_conn()` has been removed. Use `get_sdk_client()` instead.

#### Transitioning to the Modern Client

If you have legacy DAGs or custom operators using `get_conn()`, update them to use `get_sdk_client()`. Both return the same `ImednetSDK` client, making the migration straightforward:

**Before (Removed):**
```python
client = hook.get_conn()
```

**After (Recommended):**
```python
client = hook.get_sdk_client()
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
