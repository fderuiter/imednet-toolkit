"""Unit tests for pyproject metadata."""

import ast
import re
from pathlib import Path

import pytest
from scripts.validate_docs import check_sphinx_html_baseurl

import imednet


def test_project_version_is_single_source_of_truth() -> None:
    """Test that project version is single source of truth."""
    pyproject_path = Path(__file__).resolve().parents[2] / "packages" / "core" / "pyproject.toml"
    content = pyproject_path.read_text(encoding="utf-8")

    project_section_match = re.search(
        r"(?ms)^\[project\]\n(.*?)(?=^\[|\Z)",
        content,
    )
    assert project_section_match is not None
    project_section = project_section_match.group(1)

    version_match = re.search(r'^\s*version\s*=\s*"([^"]+)"', project_section, flags=re.MULTILINE)
    assert version_match is not None
    assert version_match.group(1) == imednet.__version__

    readme_match = re.search(r'^\s*readme\s*=\s*"([^"]+)"', project_section, flags=re.MULTILINE)
    assert readme_match is not None
    readme_path = pyproject_path.parent / readme_match.group(1)
    assert readme_path.exists()
    assert readme_path.parent == pyproject_path.parent

    for required_key in ("name", "description", "readme", "authors", "license"):
        assert (
            re.search(rf"^\s*{re.escape(required_key)}\s*=", project_section, flags=re.MULTILINE)
            is not None
        )


def test_hatch_scripts_for_maintenance_tasks() -> None:
    """Test that root pyproject.toml registers Hatch scripts for developer maintenance tasks."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    root_pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with root_pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    hatch_scripts = (
        data.get("tool", {}).get("hatch", {}).get("envs", {}).get("default", {}).get("scripts", {})
    )

    expected_tasks = [
        "check-root-files",
        "ci-plan",
        "aggregate-verification",
        "tighten-dependencies",
        "typecheck-docs",
        "validate-diagrams",
        "validate-docs",
        "validate-package-consumption",
    ]

    for task in expected_tasks:
        assert task in hatch_scripts, (
            f"Hatch script '{task}' should be registered in root pyproject.toml"
        )
        script_file = task.replace("-", "_") + ".py"
        assert f"scripts/{script_file}" in hatch_scripts[task]

    for retired_task in ("post-smoke-record", "post_smoke_record"):
        assert retired_task not in hatch_scripts


def test_sphinx_conf_defines_canonical_html_baseurl() -> None:
    """Test that docs/conf.py defines the canonical html_baseurl with trailing slash."""
    docs_conf_path = Path(__file__).resolve().parents[2] / "docs" / "conf.py"
    assert docs_conf_path.exists()
    content = docs_conf_path.read_text(encoding="utf-8")

    tree = ast.parse(content, filename=str(docs_conf_path))
    baseurl = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "html_baseurl"
                    and isinstance(node.value, ast.Constant)
                ):
                    baseurl = node.value.value
                    break
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "html_baseurl"
            and node.value
            and isinstance(node.value, ast.Constant)
        ):
            baseurl = node.value.value
            break

    assert baseurl is not None, "html_baseurl must be defined in docs/conf.py"
    assert baseurl == "https://fderuiter.github.io/imednet-toolkit/"
    assert baseurl.startswith("https://")
    assert baseurl.endswith("/")


def test_validate_docs_sphinx_html_baseurl_check() -> None:
    """Test that check_sphinx_html_baseurl succeeds on the repository docs/conf.py."""
    assert check_sphinx_html_baseurl() is False


def test_validate_docs_sphinx_html_baseurl_error_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that check_sphinx_html_baseurl detects missing, wrong scheme, or missing trailing slash."""
    # Missing html_baseurl
    monkeypatch.setattr(Path, "read_text", lambda self, *args, **kwargs: "html_theme = 'furo'")
    assert check_sphinx_html_baseurl() is True

    # Missing trailing slash
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *args, **kwargs: (
            'html_baseurl = "https://fderuiter.github.io/imednet-toolkit"'
        ),
    )
    assert check_sphinx_html_baseurl() is True

    # Invalid scheme
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *args, **kwargs: (
            'html_baseurl = "http://fderuiter.github.io/imednet-python-sdk/"'
        ),
    )
    assert check_sphinx_html_baseurl() is True

    # Wrong baseurl
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *args, **kwargs: 'html_baseurl = "https://example.com/docs/"',
    )
    assert check_sphinx_html_baseurl() is True


def test_baseurl_validation_is_independent_of_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert check_sphinx_html_baseurl() is False
