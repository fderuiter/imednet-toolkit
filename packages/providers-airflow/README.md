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

### Getting the SDK Client

To retrieve a configured SDK client to use in your custom operators or tasks, use `get_conn()` (adhering to the standard Airflow `BaseHook` interface) or its alias `get_sdk_client()`:

```python
from apache_airflow_providers_imednet.hooks import ImednetHook


def my_task_logic():
    hook = ImednetHook(imednet_conn_id="imednet_default")
    # Retrieve the active ImednetSDK client using the standard BaseHook entry point:
    client = hook.get_conn()
    # Alternatively, hook.get_sdk_client() can also be used:
    # client = hook.get_sdk_client()

    # Use the client to interact with iMednet
    studies = client.studies.list()
```

## Incremental Checkpointing (Airflow 3)

`ImednetExportOperator(incremental=True)` standardizes on the file-backed `ExtractionStateLedger` (`FileStateProvider`) resolved via `ledger_path` or `IMEDNET_WORKFLOW_LEDGER_PATH` rather than mutating Airflow XCom tables across runs.

In Airflow 3, pass Study checkpoints across runs using:

- **Operator outputs / TaskFlow return values**: pass explicit ISO-8601 watermarks between tasks and set `modified_since` on `ImednetExportOperator`.
- **`airflow.sdk.Variable`**: read and update per-Study watermarks with `Variable.get(...)` / `Variable.set(...)` inside TaskFlow tasks rather than querying historical XCom rows.
- **Shared storage or Airflow ObjectStorage (`airflow.sdk.ObjectStoragePath`)**: point `ledger_path` at a shared filesystem mount or stage the JSON ledger file to and from `ObjectStoragePath` around `ImednetExportOperator(incremental=True, ledger_path=...)`.

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
