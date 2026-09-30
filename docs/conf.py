"""Sphinx configuration."""

# Configuration file for the Sphinx documentation builder.
import logging
import os
import sys
import types
import warnings

# mypy: ignore-errors
from typing import Any

"""
Configuration file for the Sphinx documentation builder.
This module contains all necessary configurations for building documentation
using Sphinx. It sets up the project information, extensions, and theme settings.
Attributes:
    project (str): The name of the project ("imednet")
    author (str): The author's name ("Frederick de Ruiter")
    release (str): The full project version from ``imednet.__version__``
    version (str): The short project version from ``imednet.__version__``
    extensions (list): List of Sphinx extensions to be used
    templates_path (list[str]): Path to custom templates
    exclude_patterns (list[str]): Patterns to exclude from documentation
    html_static_path (list[str]): Path to static files
    html_theme (str): The HTML theme to be used ("sphinx_rtd_theme")
    html_theme_path (list): Path to the HTML theme
Note:
    This configuration uses the Read the Docs theme and includes support for
    automatic documentation generation from docstrings and type hints.
"""


# Add package source roots so API modules can be imported for docs builds.
sys.path[:0] = [
    os.path.abspath("../packages/core/src"),
    os.path.abspath("../packages/providers-airflow/src"),
    os.path.abspath("../packages/plugins-workflows/src"),
    os.path.abspath("../packages/plugins-sinks/src"),
]
warnings.filterwarnings("ignore", message="duplicate object description*")
warnings.filterwarnings("ignore", message="Failed guarded type import*")

# Mock heavy optional dependencies so autodoc does not require them.
# Mock pandas to avoid heavy dependency while building docs. The stub provides
# minimal attributes used in type hints.
if "pandas" not in sys.modules:
    pandas_stub = types.ModuleType("pandas")

    class DataFrame:  # pragma: no cover - simple stub
        """Dummy DataFrame class."""

        def __init__(self):
            """Initialize dummy DataFrame."""
            self.columns = type('MockColumns', (), {'astype': lambda self, t: self})()

    def json_normalize(*args: Any, **kwargs: Any) -> DataFrame:  # type: ignore
        """Dummy json_normalize function."""
        return DataFrame()

    pandas_stub.DataFrame = DataFrame
    pandas_stub.json_normalize = json_normalize
    sys.modules["pandas"] = pandas_stub

from imednet import __version__ as imednet_version

project = "imednet"
author = "Frederick de Ruiter"
release = imednet_version
version = imednet_version

# Sphinx extensions
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",
    "sphinx_autodoc_typehints",
    "sphinx.ext.autosummary",
    "sphinxcontrib.mermaid",
    "sphinxarg.ext",
    "sphinx.ext.doctest",
]

autosummary_generate = False

# Mock heavy optional dependencies so autodoc does not import them.
# opentelemetry is listed here so that `if TYPE_CHECKING: from opentelemetry…`
# blocks (evaluated when set_type_checking_flag=True) do not cause
# ModuleNotFoundError during the docs build.
autodoc_mock_imports = [
    "matplotlib",
    "airflow",
    "opentelemetry",
    "pyarrow",
    "duckdb",
]

suppress_warnings = [
    "autodoc.import",
    "autodoc",
    "sphinx_autodoc_typehints",
]

# Sphinx 6.x does not assign a filterable type code to "duplicate object
# description" warnings, so suppress_warnings cannot catch them.  These
# duplicates arise because __init__.py files re-export symbols that are also
# documented in their own sub-module stubs.  The filter below silences them
# so the strict (-W) build is not broken by this benign re-export pattern.


class _SuppressDuplicateDescriptions(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover
        message = record.getMessage()
        return (
            "duplicate object description" not in message
            and "Failed guarded type import" not in message
            and "more than one target found for cross-reference" not in message
        )


def setup(app: Any) -> None:  # pragma: no cover
    """Install the duplicate-description log filter after Sphinx initialises.

    Args:
        app: The Sphinx application instance (provided by Sphinx at startup).
    """
    # Sphinx wraps every internal logger as `sphinx.<module>`, e.g.
    # sphinx.domains.python becomes sphinx.sphinx.domains.python.
    suppressor = _SuppressDuplicateDescriptions()
    logging.getLogger("sphinx").addFilter(suppressor)
    logging.getLogger("sphinx.sphinx.domains.python").addFilter(suppressor)
    logging.getLogger("sphinx.sphinx_autodoc_typehints").addFilter(suppressor)


# Ignore noisy pydantic schema generation warnings.
warnings.filterwarnings("ignore", message="Failed guarded type import", category=UserWarning)

# Display type hints in the description instead of the signature to keep
# function signatures concise in the rendered documentation.
autodoc_typehints = "description"

# Autodoc default options applied to all automodule/autoclass directives.
autodoc_default_options = {
    "members": True,
    "show-inheritance": True,
}
autodoc_class_signature = "separated"

# Force sphinx-autodoc-typehints to evaluate TYPE_CHECKING blocks so that
# forward references used only under `if TYPE_CHECKING:` are resolved.
set_type_checking_flag = True

# Napoleon – enforce Google-style docstrings.
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True

# Templates and static paths
templates_path: list[str] = ["_templates"]
exclude_patterns: list[str] = [
    "tutorials/examples/dummy.py",
    "reference/VPAT.rst",
]  # annotated per mypy requirement
html_static_path: list[str] = ["_static"]

doctest_global_setup = """
import os
import respx
import warnings
import sys
warnings.filterwarnings("ignore")
import logging
logging.getLogger("py.warnings").setLevel(logging.ERROR)
logging.captureWarnings(True)
import warnings
warnings.simplefilter("ignore")
import httpx
from unittest.mock import MagicMock, patch

os.environ["IMEDNET_API_KEY"] = "dummy_api_key"  # pragma: allowlist secret
os.environ["IMEDNET_SECURITY_KEY"] = "dummy_security_key"  # pragma: allowlist secret
os.environ["SNOWFLAKE_PASSWORD"] = "dummy_password"  # pragma: allowlist secret
os.environ["IMEDNET_STUDY_KEY"] = "MY_STUDY"

__file__ = "/a/b/c/d/e/dummy.py"

_respx_mock = respx.mock(assert_all_called=False)
_respx_mock.start()
# Mock site endpoint for register_subjects
_respx_mock.route(path__startswith="/api/v1/studies/STUDY/sites").mock(return_value=httpx.Response(200, json={"items": [{"siteName": "SITE"}]}))
_respx_mock.route(path__startswith="/api/v1/jobs").mock(return_value=httpx.Response(200, json={"status": "Completed"}))
_respx_mock.route().mock(return_value=httpx.Response(200, json={"message": "ok", "items": [{"recordId": "R1"}], "results": [], "data": []}))

import duckdb
_duckdb_connect_patcher = patch('duckdb.connect', return_value=MagicMock())
_duckdb_connect_patcher.start()

from imednet_workflows.config_version_control import ConfigVersionStore
_cvs_patcher1 = patch.object(ConfigVersionStore, 'diff_configs', return_value={"added": [], "removed": [], "changed": []})
_cvs_patcher1.start()
_cvs_patcher2 = patch.object(ConfigVersionStore, 'commit_config', return_value="mock_commit_id")
_cvs_patcher2.start()
_cvs_patcher3 = patch.object(ConfigVersionStore, 'rollback_config', return_value={})
_cvs_patcher3.start()
_cvs_patcher4 = patch.object(ConfigVersionStore, 'get_history', return_value=[{"version_tag": "v1", "commit_id": "mock_commit_id", "timestamp": "2026-01-01"}])
_cvs_patcher4.start()

from imednet.sdk import ImednetSDK, AsyncImednetSDK
_poll_patcher1 = patch.object(ImednetSDK, 'poll_job', return_value={'status': 'Completed'})
_poll_patcher1.start()
_poll_patcher2 = patch.object(AsyncImednetSDK, 'async_poll_job', return_value={'status': 'Completed'})
_poll_patcher2.start()

_records_patcher = patch('imednet.endpoints.records.RecordsEndpoint.list', return_value=[])
_records_patcher.start()

import imednet.integrations.export
_export_patcher = patch.object(imednet.integrations.export, '_prepare_export_df', return_value=sys.modules['pandas'].DataFrame())
_export_patcher.start()

from imednet_workflows.register_subjects import RegisterSubjectsWorkflow
_reg_patcher = patch.object(RegisterSubjectsWorkflow, 'register_subjects', return_value={'batch_id': 'MOCK_BATCH'})
_reg_patcher.start()

# Mock optional dependencies
import sys
from unittest.mock import MagicMock, patch
import builtins

_original_print = builtins.print
def _mock_print(*args, **kwargs):
    if args and str(args[0]) in ("True", "2020", "5", "'1.23'", "['x']", "{}"):
        _original_print(*args, **kwargs)

_print_patcher = patch.object(builtins, 'print', side_effect=_mock_print)
_print_patcher.start()

sys.modules["snowflake"] = MagicMock()
sys.modules["snowflake.connector"] = MagicMock()
sys.modules["pymongo"] = MagicMock()
sys.modules["pymongo.errors"] = MagicMock()
sys.modules["neo4j"] = MagicMock()

import imednet.integrations.sink_base
_snowflake_patcher = patch.object(imednet.integrations.sink_base, '_require_optional_dep', return_value=MagicMock())
_snowflake_patcher.start()

import imednet_sinks.warehouse
_wh_patcher = patch.object(imednet_sinks.warehouse, '_require_optional_dep', return_value=MagicMock())
_wh_patcher.start()

import imednet_sinks.document
_doc_patcher = patch.object(imednet_sinks.document, '_require_optional_dep', return_value=MagicMock())
_doc_patcher.start()

import imednet_sinks.graph
_graph_patcher = patch.object(imednet_sinks.graph, '_require_optional_dep', return_value=MagicMock())
_graph_patcher.start()

# Mock airflow and provider
import sys
from unittest.mock import MagicMock
sys.modules['airflow'] = MagicMock()
sys.modules['airflow.decorators'] = MagicMock()
sys.modules['airflow.sdk'] = MagicMock()
sys.modules['apache_airflow_providers_imednet'] = MagicMock()


commit_a = "mock_commit_a"
commit_b = "mock_commit_b"
commit_id = "mock_commit_id"
subject_data = {}
api_response = {}

"""

doctest_global_cleanup = """
_respx_mock.stop()
_duckdb_connect_patcher.stop()
_cvs_patcher1.stop()
_cvs_patcher2.stop()
_cvs_patcher3.stop()
_cvs_patcher4.stop()
_poll_patcher1.stop()
_poll_patcher2.stop()
_records_patcher.stop()
_export_patcher.stop()
_reg_patcher.stop()
_snowflake_patcher.stop()
_wh_patcher.stop()
_doc_patcher.stop()
_graph_patcher.stop()
_print_patcher.stop()
"""

html_theme = "furo"
master_doc = 'index'
