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

Optional workflow commands live in the separate `imednet-workflows` package, and
the Airflow integration lives in `apache-airflow-providers-imednet`.

Full project documentation is available at
<https://fderuiter.github.io/imednet-toolkit/>.
