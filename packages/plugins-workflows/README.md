# imednet-workflows

Workflow plugin package for the iMednet Python SDK.

## Install

Install the package via standard pip using the public package distribution name:

```bash
pip install imednet-workflows
```

Or using the workspace-relative path install command:

```bash
pip install ./packages/plugins-workflows
```

**Required Dependencies:**
Requires Python 3.11 or newer, `imednet`, `pandas`, `cryptography`, and
`filelock>=4.2.1` for cross-platform file locking. Recreate Python 3.10 virtual
environments before upgrading. UAT commands are included; there is no separate
`uat` extra.

## Launch via CLI

The package provides CLI commands mapped to registered `imednet.cli_plugins` subcommands.

```bash
# Extract clinical records
imednet workflows extract-records --study-key <STUDY_KEY>

# Continuous synchronization loop
imednet workflows sync-worker --study-key <STUDY_KEY> --interval 900

# Administrative state checkpoint management
imednet workflows state show --study-key <STUDY_KEY>
imednet workflows state set --study-key <STUDY_KEY> --stream records --timestamp "2023-10-27T12:00:00Z"

# Single subject profile data
imednet subject-data --study-key <STUDY_KEY> --subject-key <SUBJECT_KEY>
```

**Registered Entry Points:**
The following components are exposed via Python entry points:
- **Workflows (`imednet.workflows`)**: `data_extraction`, `query_management`, `record_mapper`, `record_update`, `subject_data`, `uat`, `uat_inspector`.
- **CLI Plugins (`imednet.cli_plugins`)**: `workflows`, `subject_data`.
- **Custom Pollers (`imednet.pollers`)**: `JobPoller`, `AsyncJobPoller`.
- **Custom Mappers (`imednet.mappers`)**: `RecordMapper`.
- **Data Loaders (`imednet.loaders`)**: `CachedRecordsLoader`.
- **Config Stores (`imednet.stores`)**: `ConfigVersionStore`.

## Features

- **State Ledger Management**: High-water mark state tracking via File ledger (local JSON with atomic file updates and cooperative file-locking) or Airflow (XCom databases).
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
| `UatOrchestrator` | Test generation engine that generates valid and invalid mock payloads. |
