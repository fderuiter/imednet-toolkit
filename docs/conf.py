"""Sphinx configuration."""

# Configuration file for the Sphinx documentation builder.
import importlib.util
import json
import os
import sys
import types

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
    html_theme (str): The HTML theme to be used ("furo")
    html_theme_path (list): Path to the HTML theme
Note:
    This configuration uses the Furo theme and includes support for
    automatic documentation generation from docstrings and type hints.
"""


# Add package source roots so API modules can be imported for docs builds.
sys.path[:0] = [
    os.path.abspath("../packages/core/src"),
    os.path.abspath("../packages/providers-airflow/src"),
    os.path.abspath("../packages/plugins-workflows/src"),
    os.path.abspath("../packages/plugins-sinks/src"),
    os.path.abspath("../packages/plugins-tui/src"),
]

# Mock heavy optional dependencies so autodoc does not require them.
# Mock pandas to avoid heavy dependency while building docs if not installed.
if not importlib.util.find_spec("pandas"):
    pandas_stub = types.ModuleType("pandas")

    class DataFrame:  # pragma: no cover - simple stub
        """Dummy DataFrame class."""

        def __init__(self, *args: Any, **kwargs: Any):
            """Initialize dummy DataFrame."""
            self.columns = type(
                "MockColumns",
                (),
                {
                    "astype": lambda self, t: self,
                    "str": type(
                        "MockStr",
                        (),
                        {
                            "lower": lambda self: type(
                                "MockLower", (), {"duplicated": lambda self: []}
                            )()
                        },
                    )(),
                },
            )()
            self.loc = type("MockLoc", (), {"__getitem__": lambda self, idx: DataFrame()})()
            self.empty = True

    def json_normalize(*args: Any, **kwargs: Any) -> DataFrame:  # type: ignore
        """Dummy json_normalize function."""
        return DataFrame()

    pandas_stub.DataFrame = DataFrame
    pandas_stub.json_normalize = json_normalize
    sys.modules["pandas"] = pandas_stub
else:
    import pandas  # noqa: F401

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
    "sphinx.ext.autosummary",
    "sphinxcontrib.mermaid",
    "sphinxarg.ext",
    "sphinx.ext.doctest",
    "myst_parser",
    "sphinx.ext.githubpages",
    "sphinx_thebe",
]

# Configure sphinx-thebe options for interactive in-browser execution with Pyodide
thebe_config = {
    "always_load": False,
    "selector": "div.highlight-python",
    "selector_input": "pre",
    "selector_output": ".output, .cell_output",
    "kernel_name": "pyodide",
    "wheels": [
        f"_static/wheels/imednet-{imednet_version}-py3-none-any.whl",
    ],
    "wheel_paths": [
        f"_static/wheels/imednet-{imednet_version}-py3-none-any.whl",
    ],
    "requirements": [
        "micropip",
        "pydantic>=2.11.3,<3.0.0",
        "httpx>=0.28.1,<0.29.0",
        "tenacity>=9.1.2,<10.0.0",
        "defusedxml>=0.7.1",
        "python-dotenv",
    ],
    "micropip_requirements": [
        "micropip",
        "pydantic>=2.11.3,<3.0.0",
        "httpx>=0.28.1,<0.29.0",
        "tenacity>=9.1.2,<10.0.0",
        "defusedxml>=0.7.1",
        "python-dotenv",
    ],
}

autosummary_generate = False

# Only mock optional third-party imports that are not installed in the active environment.
autodoc_mock_imports = [
    _mod
    for _mod in ("matplotlib", "airflow", "opentelemetry", "pyarrow", "duckdb")
    if importlib.util.find_spec(_mod) is None
]


def setup(app: Any) -> None:  # pragma: no cover
    """Register Sphinx client assets at startup.

    Args:
        app: The Sphinx application instance (provided by Sphinx at startup).
    """
    from pydantic import BaseModel
    from sphinx.ext.autodoc import AttributeDocumenter

    class _PydanticSafeAttributeDocumenter(AttributeDocumenter):
        """Avoid mutating Pydantic BaseModel subclass __annotations__ with internal dunder hints."""

        def update_annotations(self, parent: Any) -> None:
            if isinstance(parent, type) and issubclass(parent, BaseModel):
                return
            super().update_annotations(parent)

    app.add_autodocumenter(_PydanticSafeAttributeDocumenter, override=True)
    # Expose thebe_config containing wheel artifacts and requirements to client JS runtime
    app.add_js_file(None, body=f"window.thebe_config = {json.dumps(thebe_config)};")


# Display type hints in the description instead of the signature to keep
# function signatures concise in the rendered documentation.
autodoc_typehints = "description"

# Autodoc default options applied to all automodule/autoclass directives.
autodoc_default_options = {
    "members": True,
    "show-inheritance": True,
}
autodoc_class_signature = "separated"

# Napoleon – enforce Google-style docstrings.
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True

# Templates and static paths
templates_path: list[str] = ["_templates"]
exclude_patterns: list[str] = [
    "tutorials/examples/dummy.py",
    "agents/**",
    "superpowers/**",
    "reference/rest_tables/**",
]  # annotated per mypy requirement
html_static_path: list[str] = ["_static"]
html_css_files: list[str] = ["custom.css"]
html_js_files: list[str] = ["showcase.js", "thebe_pyodide.js"]

doctest_global_setup = """
import contextlib
import os
import tempfile
from pathlib import Path
from unittest.mock import create_autospec, patch

import neo4j
import pymongo
import pymongo.collection
import pymongo.database
import snowflake.connector
from snowflake.connector.connection import SnowflakeConnection
from snowflake.connector.cursor import SnowflakeCursor

import imednet_workflows.cached_loader as _cached_loader_mod
from imednet.simulation.transport import get_default_simulation_state

os.environ["IMEDNET_SIMULATION_MODE"] = "true"
os.environ["IMEDNET_API_KEY"] = "dummy_api_key"  # pragma: allowlist secret
os.environ["IMEDNET_SECURITY_KEY"] = "dummy_security_key"  # pragma: allowlist secret
os.environ["SNOWFLAKE_PASSWORD"] = "dummy_password"  # pragma: allowlist secret
os.environ["IMEDNET_STUDY_KEY"] = "MY_STUDY"

_doctest_orig_cwd = os.getcwd()
_doctest_tmpdir = tempfile.TemporaryDirectory()
os.chdir(_doctest_tmpdir.name)

_orig_cache_dir = _cached_loader_mod.DEFAULT_CACHE_DIR
_cached_loader_mod.DEFAULT_CACHE_DIR = Path(_doctest_tmpdir.name) / ".imednet_cache"

_sim_state = get_default_simulation_state()
_sim_state.reset()
_sim_state.forms.extend(
    [
        {
            "formId": 1301,
            "formKey": "F_DM",
            "formName": "Demographics",
            "formType": "Enrollment",
            "studyKey": "MY_STUDY",
            "revision": 1,
            "disabled": False,
            "eproForm": False,
            "allowCopy": False,
            "dateCreated": "2026-01-01T00:00:00Z",
            "dateModified": "2026-01-01T00:00:00Z",
            "lastUpdated": "2026-01-01T00:00:00Z",
        },
        {
            "formId": 1302,
            "formKey": "F_VS",
            "formName": "Vital Signs",
            "formType": "Standard",
            "studyKey": "MY_STUDY",
            "revision": 1,
            "disabled": False,
            "eproForm": False,
            "allowCopy": False,
            "dateCreated": "2026-01-01T00:00:00Z",
            "dateModified": "2026-01-01T00:00:00Z",
            "lastUpdated": "2026-01-01T00:00:00Z",
        },
    ]
)
_sim_state.records.extend(
    [
        {
            "recordId": 1601,
            "studyKey": "MY_STUDY",
            "subjectKey": "SUB-001",
            "subjectId": 1201,
            "formId": 1301,
            "formKey": "F_DM",
            "visitId": 1401,
            "intervalId": 1501,
            "siteId": 103,
            "recordType": "Standard",
            "recordStatus": "Complete",
            "recordData": {"V_AGE": 45, "V_GENDER": "Female"},
            "deleted": False,
            "dateCreated": "2026-01-10T09:30:00Z",
            "dateModified": "2026-01-10T09:30:00Z",
            "lastUpdated": "2026-01-10T09:30:00Z",
        },
        {
            "recordId": 1602,
            "studyKey": "MY_STUDY",
            "subjectKey": "SUB-001",
            "subjectId": 1201,
            "formId": 1302,
            "formKey": "F_VS",
            "visitId": 1401,
            "intervalId": 1501,
            "siteId": 103,
            "recordType": "Standard",
            "recordStatus": "Complete",
            "recordData": {"V_WEIGHT": 68.5},
            "deleted": False,
            "dateCreated": "2026-01-10T09:35:00Z",
            "dateModified": "2026-01-10T09:35:00Z",
            "lastUpdated": "2026-01-10T09:35:00Z",
        },
    ]
)

_sf_conn = create_autospec(SnowflakeConnection, instance=True)
_sf_cursor = create_autospec(SnowflakeCursor, instance=True)
_sf_conn.cursor.return_value = _sf_cursor
_sf_patcher = patch("snowflake.connector.connect", autospec=True, return_value=_sf_conn)
_sf_patcher.start()

_mongo_client = create_autospec(pymongo.MongoClient, instance=True)
_mongo_admin = create_autospec(pymongo.database.Database, instance=True)
_mongo_admin.command.return_value = {"ok": 1}
_mongo_client.admin = _mongo_admin
_mongo_db = create_autospec(pymongo.database.Database, instance=True)
_mongo_coll = create_autospec(pymongo.collection.Collection, instance=True)
_mongo_db.__getitem__.return_value = _mongo_coll
_mongo_client.__getitem__.return_value = _mongo_db
_mongo_patcher = patch("pymongo.MongoClient", autospec=True, return_value=_mongo_client)
_mongo_patcher.start()

_neo4j_driver = create_autospec(neo4j.Driver, instance=True)
_neo4j_session = create_autospec(neo4j.Session, instance=True)
_neo4j_session.__enter__.return_value = _neo4j_session
_neo4j_driver.session.return_value = _neo4j_session
_neo4j_patcher = patch("neo4j.GraphDatabase.driver", autospec=True, return_value=_neo4j_driver)
_neo4j_patcher.start()
"""

doctest_global_cleanup = """
_neo4j_patcher.stop()
_mongo_patcher.stop()
_sf_patcher.stop()
_cached_loader_mod.DEFAULT_CACHE_DIR = _orig_cache_dir
_sim_state.reset()
os.chdir(_doctest_orig_cwd)
_doctest_tmpdir.cleanup()
with contextlib.suppress(OSError):
    Path("/tmp/imednet_manifest.jsonl").unlink()
"""

html_theme = "furo"
html_title = f"{project} Documentation"
html_baseurl = "https://fderuiter.github.io/imednet-toolkit/"
html_logo = "_static/logo.png"
html_favicon = "_static/logo.png"

html_theme_options: dict[str, Any] = {
    "source_repository": "https://github.com/fderuiter/imednet-toolkit",
    "source_branch": "main",
    "source_directory": "docs/",
    "light_css_variables": {
        "color-brand-primary": "#005fb8",
        "color-brand-content": "#005fb8",
        "color-brand-visited": "#5c3d99",
        "color-brand-btn-text": "#ffffff",
        "color-brand-button-text": "#ffffff",
        "color-status-success": "#065f46",
        "color-status-warning": "#92400e",
        "color-status-danger": "#b91c1c",
        "color-badge-success": "#065f46",
        "color-badge-warning": "#92400e",
        "color-badge-danger": "#b91c1c",
        "color-badge-required": "#b91c1c",
    },
    "dark_css_variables": {
        "color-brand-primary": "#58a6ff",
        "color-brand-content": "#79c0ff",
        "color-brand-visited": "#c4a0ff",
        "color-brand-btn-text": "#131416",
        "color-brand-button-text": "#131416",
        "color-status-success": "#34d399",
        "color-status-warning": "#fbbf24",
        "color-status-danger": "#f87171",
        "color-badge-success": "#34d399",
        "color-badge-warning": "#fbbf24",
        "color-badge-danger": "#f87171",
        "color-badge-required": "#f87171",
    },
}

master_doc = 'index'

# Linkcheck configuration
linkcheck_ignore = [
    r"https://.*\.imednet\.com.*",
    r"https://cdn\.jsdelivr\.net/.*",
    r"https://unpkg\.com/.*",
]
linkcheck_timeout = 30
linkcheck_retries = 3
