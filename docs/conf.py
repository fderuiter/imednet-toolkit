"""Sphinx configuration."""

# Configuration file for the Sphinx documentation builder.
import html
import importlib.util
import os
import re
import sys
import tomllib
import types
import urllib.parse
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


# Add package source roots and local Sphinx extensions so API modules and directives resolve.
_DOCS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_DOCS_DIR, ".."))
sys.path[:0] = [
    os.path.join(_DOCS_DIR, "_ext"),
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
    "pyodide_cells",
]

_PYODIDE_WHEEL_SPECS: tuple[tuple[str, str], ...] = (
    ("imednet", "core"),
    ("imednet_workflows", "plugins-workflows"),
    ("imednet_plugins_sinks", "plugins-sinks"),
)


def _resolve_pyodide_wheels() -> list[str]:
    """Resolve dynamic browser wheel paths in installation order."""
    candidate_dirs = (
        Path(_DOCS_DIR) / "_build" / "wheels",
        Path(_DOCS_DIR) / "_static" / "wheels",
    )
    resolved: list[str] = []
    for dist_prefix, pkg_dir_name in _PYODIDE_WHEEL_SPECS:
        staged_matches: list[Path] = []
        for wheels_dir in candidate_dirs:
            if wheels_dir.is_dir():
                staged_matches = sorted(wheels_dir.glob(f"{dist_prefix}-*.whl"))
                if staged_matches:
                    break
        if staged_matches:
            resolved.append(f"_static/wheels/{staged_matches[-1].name}")
            continue

        pyproject_file = Path(_REPO_ROOT) / "packages" / pkg_dir_name / "pyproject.toml"
        pkg_version = imednet_version
        if pyproject_file.is_file():
            with pyproject_file.open("rb") as fh:
                pkg_version = str(
                    tomllib.load(fh).get("project", {}).get("version", imednet_version)
                )
        resolved.append(f"_static/wheels/{dist_prefix}-{pkg_version}-py3-none-any.whl")
    return resolved


# Configure client-side Pyodide WebAssembly execution for interactive tutorials
pyodide_config = {
    "selector": "div.highlight-python",
    "pyodide_packages": [
        "micropip",
        "pydantic",
        "cryptography",
        "pandas",
        "ssl",
        "sqlite3",
    ],
    "requirements": [
        "httpx",
        "tenacity",
        "defusedxml",
        "python-dotenv",
        "filelock",
        "python-json-logger",
    ],
    "wheels": _resolve_pyodide_wheels(),
    "env": {
        "IMEDNET_SIMULATION_MODE": "true",
        "IMEDNET_API_KEY": "dummy_api_key",  # pragma: allowlist secret
        "IMEDNET_SECURITY_KEY": "dummy_security_key",  # pragma: allowlist secret
        "SNOWFLAKE_PASSWORD": "dummy_password",  # pragma: allowlist secret
        "IMEDNET_STUDY_KEY": "MY_STUDY",
    },
}

autosummary_generate = False

# Only mock optional third-party imports that are not installed in the active environment.
autodoc_mock_imports = [
    _mod
    for _mod in ("matplotlib", "airflow", "opentelemetry", "pyarrow", "duckdb")
    if importlib.util.find_spec(_mod) is None
]


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
    """Register autodoc safeguards and native SEO / GitHub Pages hooks at startup.

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
    app.connect("html-page-context", _inject_seo_metadata)
    app.connect("build-finished", _on_build_finished)


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
