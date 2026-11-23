# imednet-plugins-sinks

Unified database sinks plugin for the iMednet Python SDK.

## Install

### Published PyPI Releases

Install the package and optional destination drivers from PyPI:

```bash
pip install imednet-plugins-sinks
pip install "imednet-plugins-sinks[mongodb]"
pip install "imednet-plugins-sinks[neo4j]"
pip install "imednet-plugins-sinks[snowflake]"
pip install "imednet-plugins-sinks[all]"
```

### Source Checkout

From a local repository checkout:

```bash
git clone https://github.com/fderuiter/imednet-toolkit.git
cd imednet-toolkit
pip install -e ./packages/plugins-sinks
```

## Programmatic Usage

The package provides a unified SDK namespace integration (`SinksNamespace`) directly registered with the core SDK client via entry points (`imednet.plugins`).

Outside simulation mode, pass both `api_key` and `security_key` when initializing `ImednetSDK`:

```python
import os

from imednet import ImednetSDK
from imednet_sinks import (
    MongoDbSinkConfig,
    Neo4jSinkConfig,
    SnowflakeSinkConfig,
    export_to_mongodb,
    export_to_neo4j,
    export_to_snowflake,
)

sdk = ImednetSDK(
    api_key=os.environ["IMEDNET_API_KEY"],
    security_key=os.environ["IMEDNET_SECURITY_KEY"],
)

config = MongoDbSinkConfig(
    study_key="STUDY",
    uri="mongodb://localhost:27017",
    database="imednet",
    collection="records",
)
export_to_mongodb(sdk, "STUDY", config=config)
sdk.close()
```

For offline testing and dry-run pipeline verification without API credentials, enable `simulation_mode=True`:

```python
from imednet import ImednetSDK
from imednet_sinks import MongoDbSinkConfig, export_to_mongodb

with ImednetSDK(simulation_mode=True) as sdk:
    config = MongoDbSinkConfig(
        study_key="STUDY",
        uri="mongodb://localhost:27017",
        database="imednet",
        collection="records",
    )
    loaded = export_to_mongodb(sdk, "STUDY", config=config)
    print(f"Exported {loaded} simulated Record items.")
```

Alternatively, commands can be launched via standard CLI structures mapped to these functions when installed alongside `imednet[cli]`.

## Features

- **Multi-backend Support**: Tailored exporters mapping Study Records to Document Store (MongoDB), Property Graph (Neo4j), or Data Warehouse (Snowflake) systems.
- **Dynamic Lazy Imports**: Keeps package initialization light by lazy-loading heavy drivers (e.g. PyMongo, Neo4j, Snowflake Connector, PyArrow) at execution time.
- **Idempotency & Resiliency**:
  - **MongoDB**: Uses bulk upserts mapping unique compound keys (`<study_key>/<record_id>`) using PyMongo `bulk_write` update/upsert actions.
  - **Neo4j**: Merges hierarchies (`Study -> Subject -> Visit -> Record`) idempotently using query templates.
  - **Snowflake**: Two-phase loading converting Study Records to Apache Arrow tables, writing staged Parquet batches, and running staged `COPY INTO` + `MERGE INTO` keyed on `(study_key, record_id)`.
- **Unified SDK Namespace Integration**: Seamless integration through the `SinksNamespace` registered directly with the core SDK client.

## Component Description Table

| Component | Description |
|-----------|-------------|
| `MongoDbExportSink` | Document store backend exporter using PyMongo `bulk_write` and upserts. |
| `Neo4jExportSink` | Property graph backend exporter merging hierarchical models idempotently. |
| `SnowflakeExportSink` | Data warehouse backend exporter using Apache Arrow and atomic `COPY INTO`. |
| `SinksNamespace` | Unified SDK namespace integration registered with the core SDK client. |

## Links

- Export Destinations Guide: <https://fderuiter.github.io/imednet-toolkit/how-to/export_destinations.html>
- Examples: <https://fderuiter.github.io/imednet-toolkit/tutorials/examples/index.html>
- Repository: <https://github.com/fderuiter/imednet-toolkit>
- License: [MIT](https://github.com/fderuiter/imednet-toolkit/blob/main/LICENSE)
