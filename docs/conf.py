"""Sphinx configuration."""

# Configuration file for the Sphinx documentation builder.
import html
import importlib.util
import json
import logging
import os
import re
import sys
import types
import urllib.parse
import warnings
from pathlib import Path

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
warnings.filterwarnings("ignore", message="duplicate object description*")
warnings.filterwarnings("ignore", message="Failed guarded type import*")

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
    "sphinx_autodoc_typehints",
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


_DEFAULT_SITE_DESCRIPTION = (
    "Unofficial Python SDK, CLI, workflow plugins, TUI, and Apache Airflow provider "
    "for the iMednet clinical trials EDC API."
)
_SPECIAL_PAGE_TITLES = {
    "genindex": "Index",
    "py-modindex": "Python Module Index",
    "search": "Search",
    "_modules/index": "Module Code",
}
_META_DESC_RE = re.compile(
    r"<meta\s+(?:name=[\"']description[\"']\s+content=[\"'](?P<c1>[^\"']*)[\"']"
    r"|content=[\"'](?P<c2>[^\"']*)[\"']\s+name=[\"']description[\"'])\s*/?>",
    re.IGNORECASE,
)


def _strip_html(raw: str) -> str:
    """Convert an HTML fragment into normalized plain text."""
    text = re.sub(r"<[^>]+>", "", raw)
    return " ".join(html.unescape(text).split())


def _extract_doctree_description(doctree: Any) -> str:
    """Extract a concise plain-text summary from the first body paragraph in doctree."""
    if doctree is None:
        return ""
    for node in doctree.findall():
        if node.__class__.__name__ != "paragraph":
            continue
        parent_name = getattr(getattr(node, "parent", None), "__class__", type(None)).__name__
        if parent_name in {"note", "warning", "admonition", "tip", "important", "caution"}:
            continue
        text = " ".join(node.astext().split())
        if text:
            if len(text) > 200:
                return text[:197].rstrip() + "..."
            return text
    return ""


def _resolve_page_title(pagename: str, context: dict[str, Any], site_title: str) -> str:
    """Resolve a clean plain-text page title for social metadata tags."""
    raw_title = context.get("title")
    if isinstance(raw_title, str) and raw_title.strip():
        cleaned = _strip_html(raw_title)
        if cleaned:
            return site_title if pagename == master_doc else f"{cleaned} — {site_title}"
    if pagename in _SPECIAL_PAGE_TITLES:
        return f"{_SPECIAL_PAGE_TITLES[pagename]} — {site_title}"
    if pagename.startswith("_modules/"):
        mod_name = pagename.removeprefix("_modules/").replace("/", ".")
        return f"Source code for {mod_name} — {site_title}"
    fallback_slug = pagename.rsplit("/", maxsplit=1)[-1].replace("_", " ").replace("-", " ").title()
    return f"{fallback_slug} — {site_title}"


def _canonical_page_url(base_url: str, pagename: str) -> str:
    """Build the canonical public URL for a given Sphinx page name."""
    normalized_base = base_url.rstrip("/") + "/"
    if pagename == master_doc:
        return normalized_base
    return f"{normalized_base}{pagename}.html"


def _inject_seo_metadata(
    app: Any,
    pagename: str,
    templatename: str,  # noqa: ARG001
    context: dict[str, Any],
    doctree: Any,
) -> None:
    """Inject description, OpenGraph, and Twitter Card metadata into HTML pages."""
    base_url = getattr(app.config, "html_baseurl", "") or html_baseurl
    normalized_base = base_url.rstrip("/") + "/"
    site_title = getattr(app.config, "html_title", None) or f"{project} Documentation"

    existing_metatags = context.get("metatags") or ""
    match = _META_DESC_RE.search(existing_metatags)
    if match:
        raw_desc = html.unescape(match.group("c1") or match.group("c2") or "").strip()
        description = raw_desc or _DEFAULT_SITE_DESCRIPTION
        escaped_desc = html.escape(description, quote=True)
        existing_metatags = _META_DESC_RE.sub(
            f'<meta name="description" content="{escaped_desc}" />',
            existing_metatags,
            count=1,
        )
        has_meta_desc = True
    else:
        doctree_desc = _extract_doctree_description(doctree)
        page_label = _resolve_page_title(pagename, context, site_title)
        description = doctree_desc or f"{page_label}. {_DEFAULT_SITE_DESCRIPTION}"
        has_meta_desc = False

    full_title = _resolve_page_title(pagename, context, site_title)
    canonical_url = _canonical_page_url(normalized_base, pagename)
    logo_rel = (getattr(app.config, "html_logo", None) or "_static/logo.png").lstrip("/")
    image_url = f"{normalized_base}{logo_rel}"

    esc_title = html.escape(full_title, quote=True)
    esc_desc = html.escape(description, quote=True)
    esc_url = html.escape(canonical_url, quote=True)
    esc_image = html.escape(image_url, quote=True)
    esc_site = html.escape(site_title, quote=True)

    injected_lines: list[str] = []
    if not has_meta_desc:
        injected_lines.append(f'<meta name="description" content="{esc_desc}" />')
    injected_lines.extend(
        [
            f'<meta property="og:title" content="{esc_title}" />',
            f'<meta property="og:description" content="{esc_desc}" />',
            '<meta property="og:type" content="website" />',
            f'<meta property="og:url" content="{esc_url}" />',
            f'<meta property="og:image" content="{esc_image}" />',
            f'<meta property="og:site_name" content="{esc_site}" />',
            '<meta name="twitter:card" content="summary" />',
            f'<meta name="twitter:title" content="{esc_title}" />',
            f'<meta name="twitter:description" content="{esc_desc}" />',
            f'<meta name="twitter:image" content="{esc_image}" />',
        ]
    )
    prefix = (
        existing_metatags
        if existing_metatags.endswith("\n") or not existing_metatags
        else f"{existing_metatags}\n"
    )
    context["metatags"] = prefix + "\n".join(injected_lines) + "\n"


def _rewrite_not_found_assets(not_found_path: Path, base_path: str) -> None:
    """Rewrite relative asset and navigation URLs on 404.html so nested paths render cleanly."""
    if not not_found_path.exists():
        return
    prefix = "/" + base_path.strip("/") + "/" if base_path.strip("/") else "/"
    content = not_found_path.read_text(encoding="utf-8")

    # Anchor Sphinx content_root and DOCUMENTATION_OPTIONS.URL_ROOT for scripts/search
    content = re.sub(
        r'data-content_root="[^"]*"',
        f'data-content_root="{prefix}"',
        content,
    )
    content = re.sub(
        r"(URL_ROOT\s*:\s*)(['\"])(?:\./|)\2",
        rf"\g<1>\g<2>{prefix}\g<2>",
        content,
    )

    # Rewrite relative _static/ asset paths (including JSON config strings and tag attributes)
    content = re.sub(
        r'(?P<q>["\'])(?:\./)?_static/',
        rf"\g<q>{prefix}_static/",
        content,
    )

    # Rewrite remaining root-relative href/action links so sidebar and search work from nested 404 URLs
    def _anchor_attr(match: re.Match[str]) -> str:
        attr = match.group("attr")
        quote = match.group("q")
        target = match.group("target")
        if target.startswith(("/", "#", "http://", "https://", "mailto:", "javascript:", "data:")):
            return match.group(0)
        cleaned = target.removeprefix("./")
        return f"{attr}={quote}{prefix}{cleaned}{quote}"

    content = re.sub(
        r'(?P<attr>\b(?:href|action))=(?P<q>["\'])(?P<target>[^"\']+)(?P=q)',
        _anchor_attr,
        content,
    )
    not_found_path.write_text(content, encoding="utf-8")


def _on_build_finished(app: Any, exception: Exception | None) -> None:
    """Finalize HTML build by rewriting 404.html asset paths and emitting sitemap.xml and robots.txt."""
    if exception is not None or getattr(app.builder, "format", "") != "html":
        return

    outdir = Path(app.outdir)
    base_url = (getattr(app.config, "html_baseurl", "") or html_baseurl).rstrip("/") + "/"
    base_path = urllib.parse.urlsplit(base_url).path or "/"

    _rewrite_not_found_assets(outdir / "404.html", base_path)

    found_docs = sorted(doc for doc in getattr(app.env, "found_docs", set()) if doc != "404")
    sitemap_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for docname in found_docs:
        loc = html.escape(_canonical_page_url(base_url, docname), quote=False)
        sitemap_lines.append(f"  <url><loc>{loc}</loc></url>")
    sitemap_lines.append("</urlset>\n")
    (outdir / "sitemap.xml").write_text("\n".join(sitemap_lines), encoding="utf-8")

    robots_txt = f"User-agent: *\nAllow: /\n\nSitemap: {base_url}sitemap.xml\n"
    (outdir / "robots.txt").write_text(robots_txt, encoding="utf-8")


def setup(app: Any) -> None:  # pragma: no cover
    """Install log filters and native SEO / GitHub Pages hooks after Sphinx initialises.

    Args:
        app: The Sphinx application instance (provided by Sphinx at startup).
    """
    # Sphinx wraps every internal logger as `sphinx.<module>`, e.g.
    # sphinx.domains.python becomes sphinx.sphinx.domains.python.
    suppressor = _SuppressDuplicateDescriptions()
    logging.getLogger("sphinx").addFilter(suppressor)
    logging.getLogger("sphinx.sphinx.domains.python").addFilter(suppressor)
    logging.getLogger("sphinx.sphinx_autodoc_typehints").addFilter(suppressor)

    # Expose thebe_config containing wheel artifacts and requirements to client JS runtime
    app.add_js_file(None, body=f"window.thebe_config = {json.dumps(thebe_config)};")

    app.connect("html-page-context", _inject_seo_metadata)
    app.connect("build-finished", _on_build_finished)


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
templates_path: list[str] = []
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

os.environ["IMEDNET_SIMULATION_MODE"] = "true"
os.environ["IMEDNET_API_KEY"] = "dummy_api_key"  # pragma: allowlist secret
os.environ["IMEDNET_SECURITY_KEY"] = "dummy_security_key"  # pragma: allowlist secret
os.environ["SNOWFLAKE_PASSWORD"] = "dummy_password"  # pragma: allowlist secret
os.environ["IMEDNET_STUDY_KEY"] = "MY_STUDY"

__file__ = "/a/b/c/d/e/dummy.py"

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

import imednet.integrations.export
_export_patcher = patch.object(imednet.integrations.export, '_prepare_export_df', return_value=sys.modules['pandas'].DataFrame())
_export_patcher.start()

from imednet_workflows.register_subjects import RegisterSubjectsWorkflow
_reg_patcher = patch.object(RegisterSubjectsWorkflow, 'register_subjects', return_value={'batch_id': 'MOCK_BATCH'})
_reg_patcher.start()

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
_duckdb_connect_patcher.stop()
_cvs_patcher1.stop()
_cvs_patcher2.stop()
_cvs_patcher3.stop()
_cvs_patcher4.stop()
_export_patcher.stop()
_reg_patcher.stop()
_snowflake_patcher.stop()
_wh_patcher.stop()
_doc_patcher.stop()
_graph_patcher.stop()
_print_patcher.stop()
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
