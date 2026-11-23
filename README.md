# imednet

**Unofficial Python SDK for the iMednet clinical trials API.**

Requires Python 3.11 or newer. Recreate Python 3.10 virtual environments with a
supported interpreter before upgrading; the minimum enables patched workflow
file locking dependencies.

Development and documentation checkouts require Python **3.11.5+** to install
patched selector dependencies. Published packages retain Python 3.11+ support.

Full documentation: <https://fderuiter.github.io/imednet-toolkit/> | [Quick Start (Live Pyodide)](https://fderuiter.github.io/imednet-toolkit/tutorials/quick_start.html) | [Async Quick Start (Live Pyodide)](https://fderuiter.github.io/imednet-toolkit/tutorials/async_quick_start.html) | [Simulation Scenario Explorer](https://fderuiter.github.io/imednet-toolkit/#interactive-showcase)

<div align="center">

[![PyPI](https://img.shields.io/pypi/v/imednet.svg)](https://pypi.org/project/imednet/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/imednet.svg)](https://pypi.org/project/imednet/)
[![PyPI - Wheel](https://img.shields.io/pypi/wheel/imednet.svg)](https://pypi.org/project/imednet/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/imednet.svg)](https://pypi.org/project/imednet/)
[![License](https://img.shields.io/pypi/l/imednet.svg)](https://github.com/fderuiter/imednet-toolkit/blob/main/LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/fderuiter/imednet-toolkit/main.yml?branch=main)](https://github.com/fderuiter/imednet-toolkit/actions/workflows/main.yml)

</div>

This package simplifies integration with the iMednet REST API for study
management. It provides typed endpoint wrappers, helper workflows and a CLI so
researchers and developers can automate data extraction and record operations without
reimplementing HTTP logic.

## Documentation

Our documentation is organized according to the **Diátaxis framework**, which categorizes information based on user intent, and offers two distinct browser-based interactive experiences:

### ⚡ [Simulation Scenario Explorer](https://fderuiter.github.io/imednet-toolkit/#interactive-showcase) *(Read-Only Offline Fixture Explorer)*
*Browse verified Python SDK and REST snippets, inspect instant precomputed simulation fixture previews for Study, Form, Record, and Query resources, validate Form Variable schemas, and review audit trails directly in your browser without credentials or backend servers.*
- [Integration Engineer Live Console](https://fderuiter.github.io/imednet-toolkit/#panel-integration)
- [Clinical Data Manager Form Validator](https://fderuiter.github.io/imednet-toolkit/#panel-datamanager)
- [Regulatory Compliance Auditor Verification](https://fderuiter.github.io/imednet-toolkit/#panel-auditor)

### 🧪 Live Executable Pyodide Tutorials
*Unlike the read-only fixture explorer on the landing page, these tutorial pages embed live WebAssembly Python cells (**Activate Interactive Runtime**) where you can edit and run `ImednetSDK` and `AsyncImednetSDK` code directly in your browser.*
- [Quick Start](https://fderuiter.github.io/imednet-toolkit/tutorials/quick_start.html) — Edit and execute synchronous `ImednetSDK(simulation_mode=True)` cells live in Pyodide
- [Async Quick Start](https://fderuiter.github.io/imednet-toolkit/tutorials/async_quick_start.html) — Edit and execute concurrent `AsyncImednetSDK(simulation_mode=True)` cells live in Pyodide

### 📚 [Tutorials](https://fderuiter.github.io/imednet-toolkit/#documentation-overview)
*Learning-oriented guides for getting started.*
- [Quick Start](https://fderuiter.github.io/imednet-toolkit/tutorials/quick_start.html) (live Pyodide runtime; compare with the read-only [Simulation Scenario Explorer](https://fderuiter.github.io/imednet-toolkit/#interactive-showcase))
- [Async Quick Start](https://fderuiter.github.io/imednet-toolkit/tutorials/async_quick_start.html) (live Pyodide runtime)
- [Examples](https://fderuiter.github.io/imednet-toolkit/tutorials/examples/index.html)

### 🛠️ [How-To Guides](https://fderuiter.github.io/imednet-toolkit/#documentation-overview)
*Problem-oriented guides for completing specific tasks.*
- [Data Dictionary & Schema Validation](https://fderuiter.github.io/imednet-toolkit/how-to/data_dictionary.html) — Compile, convert, validate, and diff Study schemas with `imednet datadict` (`from-csv`, `from-odm`, `to-odm`, `from-redcap`, `from-api`, `validate`, `diff`, and `schema`)
- [CLI Usage](https://fderuiter.github.io/imednet-toolkit/how-to/cli.html)
- [UAT Workflow](https://fderuiter.github.io/imednet-toolkit/how-to/workflows/uat_workflow.html)
- [Registering Subjects](https://fderuiter.github.io/imednet-toolkit/how-to/register_subjects.html)
- [Bulk Record Creation](https://fderuiter.github.io/imednet-toolkit/how-to/workflows/bulk_submission.html)
- [Audit & 21 CFR Part 11 Compliance](https://fderuiter.github.io/imednet-toolkit/how-to/audit_compliance.html)
- [Export Destinations](https://fderuiter.github.io/imednet-toolkit/how-to/export_destinations.html)

### 💡 [Explanation](https://fderuiter.github.io/imednet-toolkit/#documentation-overview)
*Understanding-oriented concepts and architectural overviews.*
- [Architecture](https://fderuiter.github.io/imednet-toolkit/explanation/architecture.html)
- [API Overview](https://fderuiter.github.io/imednet-toolkit/explanation/api_overview.html)
- [Plugins System](https://fderuiter.github.io/imednet-toolkit/explanation/plugins.html)

### 📖 [Reference](https://fderuiter.github.io/imednet-toolkit/#documentation-overview)
*Information-oriented material like API documentation and contributor rules.*
- [REST API Reference](https://fderuiter.github.io/imednet-toolkit/reference/rest_api_reference.html)
- [Configuration](https://fderuiter.github.io/imednet-toolkit/reference/configuration.html)
- [Contributor Rules](https://fderuiter.github.io/imednet-toolkit/reference/agent_rules.html)
- [Automated API Reference](https://fderuiter.github.io/imednet-toolkit/reference/api/core.html)

## Installation

### Published PyPI Releases

Install the core SDK or CLI from PyPI:

```bash
pip install imednet
pip install "imednet[cli]"
```

Install published export and database destination extras or companion packages:

```bash
pip install "imednet[export]"
pip install "imednet[duckdb]"
pip install "imednet[mongodb]"
pip install "imednet[neo4j]"
pip install "imednet[snowflake]"
pip install imednet-plugins-sinks
pip install imednet-workflows
pip install apache-airflow-providers-imednet
```

### Source Checkouts (Pre-Release Packages & Extras)

`imednet-tui` and the `imednet[tui]` extra are not yet tagged on PyPI. Install them from a repository source checkout until an `imednet-tui-v*` release tag is published:

```bash
git clone https://github.com/fderuiter/imednet-toolkit.git
cd imednet-toolkit
pip install -e ./packages/plugins-tui -e "./packages/core[tui]"
```

## Developer Quickstart & Environment Setup

Contributors and developers can initialize their local environment, credentials, and dependencies using the interactive setup wizard:

```bash
bash scripts/wizards/setup-imednet-env.sh
```

The wizard guides you through:
1. **Tooling Verification**: Ensuring Python (>= 3.11.5) and `uv` are available in your `PATH`.
2. **UTF-8 Encoding**: Configuring `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` to prevent Windows console `charmap` encoding errors.
3. **Workspace Synchronization**: Running `uv sync --locked --all-packages --all-extras` to link all five workspace packages in editable mode along with member extras and default `dev` and `docs` groups.
4. **EDC Credentials**: Safely entering EDC API credentials into `.env` (with optional GitHub Actions secrets sync via `gh`).
5. **Self-Test**: Validating that the SDK and CLI (`imednet --help`) are fully operational.

To set up manually:
```bash
# Set UTF-8 encoding in PowerShell (Windows) or bash (macOS/Linux)
# PowerShell: $env:PYTHONUTF8 = "1"
# bash: export PYTHONUTF8=1

# Synchronize monorepo packages, extras, and default dev/docs dependency groups
uv sync --locked --all-packages --all-extras
```

## Contributing

Contributions are welcome! Please see our [Contributor Guide](https://github.com/fderuiter/imednet-toolkit/blob/main/AGENTS.md) and [Verification Loop](https://fderuiter.github.io/imednet-toolkit/how-to/verification.html) to get started.

## License

This project is licensed under the MIT license. See [LICENSE](https://github.com/fderuiter/imednet-toolkit/blob/main/LICENSE) for details.
