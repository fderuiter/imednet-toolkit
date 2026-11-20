"""Unit tests for native Sphinx SEO hooks, custom 404 asset rewriting, and docs validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from docs import conf as docs_conf
from scripts.validate_docs import check_built_html_seo, check_sphinx_seo_configuration

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_conf_sets_empty_templates_path_and_seo_hooks() -> None:
    """Verify docs/conf.py sets templates_path = [] and passes static SEO config check."""
    assert docs_conf.templates_path == []
    assert check_sphinx_seo_configuration(REPO_ROOT) is False


@pytest.mark.unit
def test_conf_resolves_compatible_mermaid_version() -> None:
    """Verify docs/conf.py selects Mermaid 10.2.0 for 0.x and >=10.3.0 for sphinxcontrib-mermaid >=1.0."""
    assert docs_conf._resolve_mermaid_version("0.9.2") == "10.2.0"
    assert docs_conf._resolve_mermaid_version("2.1.1") == "11.12.1"
    assert docs_conf._resolve_mermaid_version("invalid") == "10.2.0"


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
