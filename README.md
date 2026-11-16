# imednet

**Unofficial Python SDK for the iMednet clinical trials API.**

Requires Python 3.11 or newer. Recreate Python 3.10 virtual environments with a
supported interpreter before upgrading; the minimum enables patched workflow
file locking dependencies.

Development and documentation checkouts require Python **3.11.5+** to install
patched selector dependencies. Published packages retain Python 3.11+ support.

Full documentation: <https://fderuiter.github.io/imednet-python-sdk/> | [Interactive Live Explorer](https://fderuiter.github.io/imednet-python-sdk/#interactive-showcase)

<div align="center">

[![PyPI](https://img.shields.io/pypi/v/imednet.svg)](https://pypi.org/project/imednet/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/imednet.svg)](https://pypi.org/project/imednet/)
[![PyPI - Wheel](https://img.shields.io/pypi/wheel/imednet.svg)](https://pypi.org/project/imednet/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/imednet.svg)](https://pypi.org/project/imednet/)
[![License](https://img.shields.io/pypi/l/imednet.svg)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/fderuiter/imednet-python-sdk/ci.yml?branch=main)](https://github.com/fderuiter/imednet-python-sdk/actions/workflows/ci.yml)

</div>

This package simplifies integration with the iMednet REST API for study
management. It provides typed endpoint wrappers, helper workflows and a CLI so
researchers and developers can automate data extraction and record operations without
reimplementing HTTP logic.

## Documentation

Our documentation is organized according to the **Diátaxis framework**, which categorizes information based on user intent.

### ⚡ [Interactive Live Explorer](https://fderuiter.github.io/imednet-python-sdk/#interactive-showcase)
*Run live queries, test Python SDK and REST calls, inspect clinical form schemas, and verify audit trails directly in your browser without credentials or backend servers.*
- [Integration Engineer Live Console](https://fderuiter.github.io/imednet-python-sdk/#panel-integration)
- [Clinical Data Manager Form Validator](https://fderuiter.github.io/imednet-python-sdk/#panel-datamanager)
- [Regulatory Compliance Auditor Verification](https://fderuiter.github.io/imednet-python-sdk/#panel-auditor)

### 📚 [Tutorials](docs/tutorials/)
*Learning-oriented guides for getting started.*
- [Quick Start](docs/tutorials/quick_start.rst) (and [Interactive Showcase](https://fderuiter.github.io/imednet-python-sdk/#interactive-showcase))
- [Async Quick Start](docs/tutorials/async_quick_start.rst)
- [Examples](docs/tutorials/examples/)

### 🛠️ [How-To Guides](docs/how-to/)
*Problem-oriented guides for completing specific tasks.*
- [CLI Usage](docs/how-to/cli.rst)
- [UAT Workflow](docs/how-to/workflows/uat_workflow.rst)
- [Bulk Record Creation](docs/how-to/workflows/bulk_submission.rst)
- [Export Destinations](docs/how-to/export_destinations.rst)

### 💡 [Explanation](docs/explanation/)
*Understanding-oriented concepts and architectural overviews.*
- [Architecture](docs/explanation/architecture.rst)
- [API Overview](docs/explanation/api_overview.rst)
- [Plugins System](docs/explanation/plugins.rst)

### 📖 [Reference](docs/reference/)
*Information-oriented material like API documentation and contributor rules.*
- [REST API Reference](docs/reference/rest_api_reference.rst)
- [Configuration](docs/reference/configuration.rst)
- [Contributor Rules](docs/reference/agent_rules.rst)
- [Automated API Reference](docs/reference/api/)

## Installation

```bash
# PyPI release
pip install imednet
```

For extended integrations, you can install optional dependencies:
```bash
pip install "imednet[export]"
pip install imednet-workflows
pip install apache-airflow-providers-imednet
```

## Developer Quickstart & Environment Setup

Contributors and developers can initialize their local environment, credentials, and dependencies using the interactive setup wizard:

```bash
bash scripts/wizards/setup-imednet-env.sh
```

The wizard guides you through:
1. **Tooling Verification**: Ensuring Python (>= 3.11.5) and `uv` are available in your `PATH`.
2. **UTF-8 Encoding**: Configuring `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` to prevent Windows console `charmap` encoding errors.
3. **Workspace Synchronization**: Running `uv sync --extra dev --extra docs` to link workspace packages in editable mode.
4. **EDC Credentials**: Safely entering EDC API credentials into `.env` (with optional GitHub Actions secrets sync via `gh`).
5. **Self-Test**: Validating that the SDK and CLI (`imednet --help`) are fully operational.

To set up manually:
```bash
# Set UTF-8 encoding in PowerShell (Windows) or bash (macOS/Linux)
# PowerShell: $env:PYTHONUTF8 = "1"
# bash: export PYTHONUTF8=1

# Synchronize monorepo packages with dev and docs tooling
uv sync --extra dev --extra docs
```

## Contributing

Contributions are welcome! Please see our [Contributor Guide](AGENTS.md) and [Verification Loop](docs/how-to/verification.rst) to get started.

## License

This project is licensed under the MIT license. See [LICENSE](LICENSE) for details.
