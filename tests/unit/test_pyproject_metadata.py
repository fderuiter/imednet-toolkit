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


def test_all_workspace_packages_declare_readme_and_mit_license() -> None:
    """Verify all five workspace packages declare readme = 'README.md' and license = 'MIT'."""
    import tomllib

    packages_dir = Path(__file__).resolve().parents[2] / "packages"
    pkg_dirs = sorted(
        d for d in packages_dir.iterdir() if d.is_dir() and (d / "pyproject.toml").is_file()
    )
    assert len(pkg_dirs) == 5

    for pkg_dir in pkg_dirs:
        data = tomllib.loads((pkg_dir / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]
        assert project.get("readme") == "README.md", f"{pkg_dir.name} missing readme = 'README.md'"
        assert (pkg_dir / "README.md").is_file(), f"{pkg_dir.name}/README.md missing on disk"
        assert project.get("license") == "MIT", f"{pkg_dir.name} missing license = 'MIT'"


def test_validate_wheel_metadata_enforces_license_markdown_and_body(tmp_path: Path) -> None:
    """Verify validate_wheel_metadata checks License, Description-Content-Type, and non-empty body."""
    import zipfile

    from scripts.validate_package_consumption import validate_wheel_metadata

    pkgs_dir = tmp_path / "packages" / "demo"
    pkgs_dir.mkdir(parents=True)
    (pkgs_dir / "pyproject.toml").write_text('[project]\nname = "demo-pkg"\n', encoding="utf-8")

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir(parents=True)
    wheel_path = dist_dir / "demo_pkg-1.0.0-py3-none-any.whl"

    def _write_wheel(metadata_text: str) -> None:
        with zipfile.ZipFile(wheel_path, "w") as zf:
            zf.writestr("demo_pkg-1.0.0.dist-info/METADATA", metadata_text)

    _write_wheel(
        "Metadata-Version: 2.4\n"
        "Name: demo-pkg\n"
        "Version: 1.0.0\n"
        "License-Expression: MIT\n"
        "Description-Content-Type: text/markdown\n\n"
        "# demo-pkg\n\nRendered package documentation.\n"
    )
    validate_wheel_metadata(dist_dir, packages_dir=tmp_path / "packages")

    # Missing License
    _write_wheel(
        "Metadata-Version: 2.4\n"
        "Name: demo-pkg\n"
        "Version: 1.0.0\n"
        "Description-Content-Type: text/markdown\n\n"
        "# demo-pkg\n"
    )
    with pytest.raises(ValueError, match="License"):
        validate_wheel_metadata(dist_dir, packages_dir=tmp_path / "packages")

    # Missing Description-Content-Type: text/markdown
    _write_wheel(
        "Metadata-Version: 2.4\nName: demo-pkg\nVersion: 1.0.0\nLicense: MIT\n\n# demo-pkg\n"
    )
    with pytest.raises(ValueError, match="Description-Content-Type"):
        validate_wheel_metadata(dist_dir, packages_dir=tmp_path / "packages")

    # Empty METADATA body
    _write_wheel(
        "Metadata-Version: 2.4\n"
        "Name: demo-pkg\n"
        "Version: 1.0.0\n"
        "License: MIT\n"
        "Description-Content-Type: text/markdown\n\n"
        "   \n"
    )
    with pytest.raises(ValueError, match="empty"):
        validate_wheel_metadata(dist_dir, packages_dir=tmp_path / "packages")
