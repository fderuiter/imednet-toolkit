# imednet

Unofficial Python SDK for the iMednet clinical trials API.

Requires Python 3.11 or newer. Recreate Python 3.10 virtual environments with a
supported interpreter before upgrading; the minimum enables patched workflow
file locking dependencies.

The `imednet` package contains the core SDK: typed models, HTTP client
integration, endpoint wrappers, and the base CLI commands.

## Installation

```bash
pip install imednet
```

Optional core extras can be installed for CLI enhancements, analytical exports,
warehouse sinks, and interactive views (`imednet[cli]`, `imednet[duckdb]`,
`imednet[export]`, `imednet[mongodb]`, `imednet[neo4j]`, `imednet[snowflake]`,
and `imednet[tui]`).

Companion workspace packages provide extended Study and Record integrations:

- `imednet-workflows` for higher-level Study and Record workflows and CLI subcommands
- `imednet-plugins-sinks` for MongoDB, Neo4j, and Snowflake export sinks
- `imednet-tui` for the interactive terminal user interface
- `apache-airflow-providers-imednet` for Apache Airflow hooks, operators, and sensors

Full project documentation is available at
<https://fderuiter.github.io/imednet-toolkit/>.
