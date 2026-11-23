# imednet

Unofficial Python SDK for the iMednet clinical trials API.

Requires Python 3.11 or newer. Recreate Python 3.10 virtual environments with a
supported interpreter before upgrading; the minimum enables patched workflow
file locking dependencies.

The `imednet` package contains the core SDK: typed models, HTTP client
integration, endpoint wrappers, Data Dictionary utilities, and the base CLI commands.

## Interactive Documentation & Browser Showcases

Full project documentation: <https://fderuiter.github.io/imednet-toolkit/>

- **Live Executable Pyodide Tutorials** *(edit and execute Python cells in WebAssembly via `Activate Interactive Runtime`)*:
  - [Quick Start](https://fderuiter.github.io/imednet-toolkit/tutorials/quick_start.html) — Synchronous `ImednetSDK` exploration
  - [Async Quick Start](https://fderuiter.github.io/imednet-toolkit/tutorials/async_quick_start.html) — Concurrent `AsyncImednetSDK` exploration
- **Read-Only Offline Fixture Explorer** *(instant precomputed Study, Form, Record, Variable, and Query previews without starting a Python runtime)*:
  - [Simulation Scenario Explorer](https://fderuiter.github.io/imednet-toolkit/#interactive-showcase) ([Integration Engineer Live Console](https://fderuiter.github.io/imednet-toolkit/#panel-integration), [Clinical Data Manager Form Validator](https://fderuiter.github.io/imednet-toolkit/#panel-datamanager), [Regulatory Compliance Auditor Verification](https://fderuiter.github.io/imednet-toolkit/#panel-auditor))

## Installation

### Published PyPI Releases

```bash
pip install imednet
pip install "imednet[cli]"
pip install "imednet[export]"
pip install "imednet[duckdb]"
pip install "imednet[mongodb]"
pip install "imednet[neo4j]"
pip install "imednet[snowflake]"
```

Published companion packages on PyPI:

```bash
pip install imednet-plugins-sinks
pip install imednet-workflows
pip install apache-airflow-providers-imednet
```

### Source Checkout (Pre-Release TUI Plugin)

`imednet-tui` and the `imednet[tui]` extra are available from a source checkout until an `imednet-tui-v*` release tag is published:

```bash
git clone https://github.com/fderuiter/imednet-toolkit.git
cd imednet-toolkit
pip install -e ./packages/plugins-tui -e "./packages/core[tui]"
```

## Quick Start

### Offline Simulation Mode

Explore typed `Study`, `Form`, `Variable`, `Record`, and `Query` endpoints without API credentials:

```python
from imednet import ImednetSDK

with ImednetSDK(simulation_mode=True) as sdk:
    studies = list(sdk.studies.list())
    records = list(sdk.records.list(study_key="STUDY"))
    print(f"Loaded {len(studies)} Study and {len(records)} Record items.")
```

### Authenticated API Usage

Outside simulation mode, pass both `api_key` and `security_key` (for example from environment variables):

```python
import os

from imednet import ImednetSDK

with ImednetSDK(
    api_key=os.environ["IMEDNET_API_KEY"],
    security_key=os.environ["IMEDNET_SECURITY_KEY"],
) as sdk:
    studies = list(sdk.studies.list())
    print(f"Connected to {len(studies)} Study resources.")
```

## Links

- Documentation: <https://fderuiter.github.io/imednet-toolkit/>
- API Reference: <https://fderuiter.github.io/imednet-toolkit/reference/api/core.html>
- Examples: <https://fderuiter.github.io/imednet-toolkit/tutorials/examples/index.html>
- Repository: <https://github.com/fderuiter/imednet-toolkit>
- License: [MIT](https://github.com/fderuiter/imednet-toolkit/blob/main/LICENSE)
