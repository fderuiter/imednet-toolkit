# imednet-workflows

Workflow plugin package for the iMednet Python SDK.

## Install

### Published PyPI Release

Install the package via `pip` using the public package distribution name:

```bash
pip install imednet-workflows
```

### Source Checkout

From a local repository checkout:

```bash
git clone https://github.com/fderuiter/imednet-toolkit.git
cd imednet-toolkit
pip install -e ./packages/plugins-workflows
```

**Required Dependencies:**
Requires Python 3.11 or newer, `imednet`, `pandas`, `cryptography`, and
`filelock>=4.2.1` for cross-platform file locking. Recreate Python 3.10 virtual
environments before upgrading. UAT commands are included; there is no separate
`uat` extra.

## Programmatic Usage

Initialize `ImednetSDK` in offline `simulation_mode=True` or pass both `api_key` and `security_key` for live environments:

```python
import os

from imednet import ImednetSDK
from imednet_workflows import get_study_structure

with ImednetSDK(simulation_mode=True) as sim_sdk:
    structure = get_study_structure(sim_sdk, "STUDY")
    print(f"Study {structure.study_key}: {len(structure.forms)} Form definitions.")

with ImednetSDK(
    api_key=os.environ["IMEDNET_API_KEY"],
    security_key=os.environ["IMEDNET_SECURITY_KEY"],
) as live_sdk:
    live_structure = get_study_structure(live_sdk, "STUDY")
    print(len(live_structure.intervals))
```

## Launch via CLI

The package provides CLI commands mapped to registered `imednet.cli_plugins` subcommands.

```bash
# Extract clinical records (positional STUDY_KEY or -s/--study-key; supports --simulation)
imednet workflows extract-records <STUDY_KEY>
imednet workflows extract-records --study-key <STUDY_KEY> --simulation --format json

# Continuous synchronization loop
imednet workflows sync-worker <STUDY_KEY> --interval 900

# Administrative state checkpoint management (human table or v1 JSON envelope)
imednet workflows state show --study-key <STUDY_KEY>
imednet workflows state show <STUDY_KEY> --format json
imednet workflows state set --study-key <STUDY_KEY> --stream records --timestamp "2023-10-27T12:00:00Z"

# Single subject profile data (supports flags, positionals, hybrids, and --simulation)
imednet subject-data --study-key <STUDY_KEY> --subject-key <SUBJECT_KEY>
imednet subject-data <STUDY_KEY> <SUBJECT_KEY>
imednet subject-data --study-key <STUDY_KEY> <SUBJECT_KEY>
imednet subject-data <STUDY_KEY> --subject-key <SUBJECT_KEY>
```

**Registered Entry Points:**
The following components are exposed via Python entry points:
- **Workflows (`imednet.workflows`)**: `data_extraction`, `duckdb_centralizer`, `query_management`, `record_mapper`, `record_update`, `register_subjects`, `subject_data`, `uat`, `uat_inspector`.
- **CLI Plugins (`imednet.cli_plugins`)**: `workflows`, `subject_data`.
- **Custom Pollers (`imednet.pollers`)**: `JobPoller`, `AsyncJobPoller`.
- **Custom Mappers (`imednet.mappers`)**: `RecordMapper`.
- **Data Loaders (`imednet.loaders`)**: `CachedRecordsLoader`.
- **Config Stores (`imednet.stores`)**: `ConfigVersionStore`, `TriageStore`.

## Features

- **State Ledger Management**: High-water mark state tracking via File ledger (local JSON with atomic file updates and cooperative file-locking) or Airflow (`Variable` checkpoints).
- **Continuous Sync Workers**: Background polling loops (`SyncWorker`) with configurable polling intervals, execution callbacks, and clean shutdown/one-run modes.
- **Local SQLite Caching**: Thread-safe caching mirroring records with WAL (Write-Ahead Logging) mode, custom locks, and tenacity-backed network retry resilience.
- **Bounded Chunk Processing**: Memory-efficient record streaming partitions (`ChunkedRecordPipeline`) to export sequence-numbered Parquet/tabular files.
- **Schema & Validation Suites**: Validates clinical records against standard CDISC definitions, profiles schemas, and normalizes categorical variables.
- **Synthetic UAT Testing**: Complete test generation engine parsing form layouts to generate valid and invalid mock payloads deterministically.

## Component Description Table

| Component | Description |
|-----------|-------------|
| `SyncWorker` | Background polling loops with configurable polling intervals and execution callbacks. |
| `ExtractionStateLedger` | High-water mark state tracking for incremental extraction. |
| `CachedRecordsLoader` | Thread-safe caching mirroring records with SQLite WAL mode. |
| `ChunkedRecordPipeline` | Memory-efficient streaming partitions to export sequence-numbered tabular files. |
| `SchemaProfiler` | Profiles clinical records schemas against expected structures. |
| `StandardsReadinessValidator` | Validates records against standard CDISC definitions. |
| `UATWorkflow` | Test generation engine that generates valid and invalid mock payloads. |

## Links

- UAT Workflow Guide: <https://fderuiter.github.io/imednet-toolkit/how-to/workflows/uat_workflow.html>
- Bulk Record Creation Guide: <https://fderuiter.github.io/imednet-toolkit/how-to/workflows/bulk_submission.html>
- Documentation: <https://fderuiter.github.io/imednet-toolkit/>
- Repository: <https://github.com/fderuiter/imednet-toolkit>
- License: [MIT](https://github.com/fderuiter/imednet-toolkit/blob/main/LICENSE)
