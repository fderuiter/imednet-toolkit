"""Integration tests for Sphinx HTML SEO hook output, sitemap.xml, robots.txt, and 404 rewriting."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from defusedxml import ElementTree as DefusedET
from scripts.build_docs import SCOPED_INHERITED_MEMBERS
from scripts.validate_docs import (
    _verify_core_html,
    _verify_pagination_html,
    _verify_sdk_html,
    check_built_html_seo,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sphinx_html_renders_curated_core_pagination_sdk_and_endpoints(tmp_path: Path) -> None:
    """Mini Sphinx HTML build with docs/conf.py renders core, pagination, SDK, and endpoint pages."""
    src_dir = tmp_path / "docs"
    api_dir = src_dir / "reference" / "api"
    api_dir.mkdir(parents=True)
    html_dir = tmp_path / "_build" / "html"

    (src_dir / "index.rst").write_text(
        "Test Docs\n=========\n\n"
        ".. meta::\n   :description: Test docs\n\n"
        ".. toctree::\n   :maxdepth: 2\n\n"
        "   reference/api/imednet\n"
        "   reference/api/imednet.core\n"
        "   reference/api/imednet.pagination\n"
        "   reference/api/imednet.endpoints\n",
        encoding="utf-8",
    )
    (src_dir / "404.rst").write_text(":orphan:\n\nNot Found\n=========\n", encoding="utf-8")
    (api_dir / "imednet.rst").write_text(
        "imednet\n=======\n\n"
        ".. automodule:: imednet.sdk\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n\n"
        ".. automodule:: imednet.sdk_convenience\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n",
        encoding="utf-8",
    )
    (api_dir / "imednet.core.rst").write_text(
        "imednet.core\n============\n\n"
        ".. automodule:: imednet.core\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n",
        encoding="utf-8",
    )
    (api_dir / "imednet.pagination.rst").write_text(
        "imednet.pagination\n==================\n\n"
        ".. automodule:: imednet.pagination\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n",
        encoding="utf-8",
    )
    (api_dir / "imednet.endpoints.rst").write_text(
        "imednet.endpoints\n=================\n\n"
        ".. automodule:: imednet.endpoints.records\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n\n"
        ".. automodule:: imednet.models.records\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n\n"
        ".. automodule:: imednet.models.jobs\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n\n"
        ".. automodule:: imednet.models.base\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n",
        encoding="utf-8",
    )

    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "sphinx",
            "-b",
            "html",
            "-c",
            str(REPO_ROOT / "docs"),
            str(src_dir),
            str(html_dir),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Sphinx failed:\n{res.stdout}\n{res.stderr}"

    built_api = html_dir / "reference" / "api"
    assert _verify_core_html(built_api / "imednet.core.html") is False
    assert _verify_pagination_html(built_api / "imednet.pagination.html") is False
    assert _verify_sdk_html(built_api / "imednet.html") is False

    ep_text = (built_api / "imednet.endpoints.html").read_text(encoding="utf-8")
    assert "ListOperation" not in ep_text
    assert "FilterGetOperation" not in ep_text
    assert "model_validate" not in ep_text
    assert "RecordsEndpoint.to_dataframe" in ep_text
    assert "AsyncRecordsEndpoint.to_dataframe" in ep_text


def test_sphinx_seo_hooks_emit_meta_tags_sitemap_robots_and_404(tmp_path: Path) -> None:
    """Verify native Sphinx hooks generate social metadata, sitemap.xml, robots.txt, and 404.html."""
    src_dir = tmp_path / "docs"
    out_dir = tmp_path / "_build" / "html"
    doctree_dir = tmp_path / "_build" / "doctrees"
    static_dir = src_dir / "_static"
    static_dir.mkdir(parents=True)

    shutil.copy2(REPO_ROOT / "docs" / "_static" / "logo.png", static_dir / "logo.png")
    (static_dir / "custom.css").write_text("body {}\n", encoding="utf-8")
    (static_dir / "showcase.js").write_text("", encoding="utf-8")
    (static_dir / "thebe_pyodide.js").write_text("", encoding="utf-8")

    # Reuse the real docs/conf.py, docs/_ext, and docs/404.rst with a compact doc set for fast isolated build
    shutil.copytree(REPO_ROOT / "docs" / "_ext", src_dir / "_ext")
    shutil.copy2(REPO_ROOT / "docs" / "conf.py", src_dir / "conf.py")
    (src_dir / "index.rst").write_text(
        ".. meta::\n"
        "   :description: Portal landing description for clinical trial integrations.\n\n"
        "Welcome to imednet's documentation!\n"
        "===================================\n\n"
        ".. toctree::\n"
        "   :maxdepth: 1\n\n"
        "   guide\n",
        encoding="utf-8",
    )
    (src_dir / "guide.rst").write_text(
        "Quick Start Guide\n"
        "=================\n\n"
        ".. note::\n"
        "   Disclaimer note skipped for summary extraction.\n\n"
        "Install the SDK and query clinical study records in three lines of Python.\n",
        encoding="utf-8",
    )
    (src_dir / "404.rst").write_text(
        ":orphan:\n\n"
        ".. meta::\n"
        "   :description: Custom 404 page description.\n\n"
        "Page Not Found (404)\n"
        "====================\n\n"
        "Return to :doc:`index` or :doc:`guide`.\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sphinx",
            "-b",
            "html",
            "-W",
            "--keep-going",
            "-d",
            str(doctree_dir),
            str(src_dir),
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"Sphinx HTML build failed:\n{result.stdout}\n{result.stderr}"

    assert check_built_html_seo(out_dir, require_exists=True) is False

    # Verify index.html uses explicit .. meta:: :description: without duplicate description tags
    index_html = (out_dir / "index.html").read_text(encoding="utf-8")
    assert (
        '<meta name="description" content="Portal landing description for clinical trial integrations." />'
        in index_html
    )
    assert index_html.count('name="description"') == 1
    assert (
        '<meta property="og:url" content="https://fderuiter.github.io/imednet-toolkit/" />'
        in index_html
    )
    assert (
        '<meta property="og:image" content="https://fderuiter.github.io/imednet-toolkit/_static/logo.png" />'
        in index_html
    )
    assert '<meta name="twitter:card" content="summary" />' in index_html

    # Verify guide.html extracts body paragraph description (skipping .. note:: admonition)
    guide_html = (out_dir / "guide.html").read_text(encoding="utf-8")
    assert (
        '<meta name="description" content="Install the SDK and query clinical study records in three lines of Python." />'
        in guide_html
    )
    assert (
        '<meta property="og:url" content="https://fderuiter.github.io/imednet-toolkit/guide.html" />'
        in guide_html
    )

    # Verify 404.html rewrites _static/ and internal page links without corrupting https:// og:image
    not_found_html = (out_dir / "404.html").read_text(encoding="utf-8")
    assert "/imednet-toolkit/_static/" in not_found_html
    assert 'href="_static/' not in not_found_html
    assert 'src="_static/' not in not_found_html
    assert 'href="/imednet-toolkit/index.html"' in not_found_html
    assert 'href="/imednet-toolkit/guide.html"' in not_found_html
    assert (
        'content="https://fderuiter.github.io/imednet-toolkit/_static/logo.png"' in not_found_html
    )

    # Verify sitemap.xml includes index and guide, and excludes 404.html
    sitemap_root = DefusedET.fromstring((out_dir / "sitemap.xml").read_text(encoding="utf-8"))
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    locs = [el.text for el in sitemap_root.findall(f"{ns}url/{ns}loc")]
    assert "https://fderuiter.github.io/imednet-toolkit/" in locs
    assert "https://fderuiter.github.io/imednet-toolkit/guide.html" in locs
    assert all(loc is not None and not loc.endswith("/404.html") for loc in locs)

    # Verify robots.txt references canonical sitemap
    robots_txt = (out_dir / "robots.txt").read_text(encoding="utf-8")
    assert "User-agent: *" in robots_txt
    assert "Sitemap: https://fderuiter.github.io/imednet-toolkit/sitemap.xml" in robots_txt
