"""Unit tests for native Sphinx SEO hooks, custom 404 asset rewriting, and docs validation."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from defusedxml import ElementTree as DefusedET
from docs import conf as docs_conf
from scripts.validate_docs import check_built_html_seo, check_sphinx_seo_configuration

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_conf_sets_empty_templates_path_and_seo_hooks() -> None:
    """Verify docs/conf.py sets templates_path = [] and passes static SEO config check."""
    assert docs_conf.templates_path == []
    assert check_sphinx_seo_configuration(REPO_ROOT) is False


@pytest.mark.unit
def test_check_sphinx_seo_configuration_detects_invalid_configs(tmp_path: Path) -> None:
    """Verify check_sphinx_seo_configuration flags missing hooks, meta tags, or non-empty templates_path."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "conf.py").write_text('templates_path = ["_templates"]\n', encoding="utf-8")
    (docs_dir / "index.rst").write_text("Welcome\n=======\n", encoding="utf-8")
    (docs_dir / "404.rst").write_text("Not orphan\n==========\n", encoding="utf-8")

    assert check_sphinx_seo_configuration(tmp_path) is True


@pytest.mark.unit
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

    # Reuse the real docs/conf.py and docs/404.rst with a compact doc set for fast isolated build
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


@pytest.mark.unit
def test_check_built_html_seo_detects_missing_artifacts(tmp_path: Path) -> None:
    """Verify check_built_html_seo fails when required files or metadata are missing."""
    missing_dir = tmp_path / "does_not_exist"
    assert check_built_html_seo(missing_dir, require_exists=False) is False
    assert check_built_html_seo(missing_dir, require_exists=True) is True

    incomplete_dir = tmp_path / "incomplete_html"
    incomplete_dir.mkdir()
    (incomplete_dir / "index.html").write_text(
        "<html><head></head><body></body></html>", encoding="utf-8"
    )
    assert check_built_html_seo(incomplete_dir, require_exists=True) is True
