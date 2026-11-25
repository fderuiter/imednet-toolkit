"""Unit tests for pyproject metadata."""

import ast
import email
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml
from scripts.validate_docs import REQUIRED_PYTHON_CLASSIFIERS, check_sphinx_html_baseurl
from scripts.validate_package_consumption import (
    build_distributions,
    validate_wheel_metadata,
    verify_wheel_metadata,
)

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


def assert_workspace_license_parity(repo_root: Path) -> None:
    """Assert all publishable package LICENSE files exist as regular files matching root LICENSE."""
    root_license = repo_root / "LICENSE"
    assert root_license.is_file() and not root_license.is_symlink()
    expected_bytes = root_license.read_bytes()
    assert expected_bytes.strip() != b""

    pkg_manifests = sorted((repo_root / "packages").glob("*/pyproject.toml"))
    assert len(pkg_manifests) == 5
    for manifest_path in pkg_manifests:
        pkg_license = manifest_path.parent / "LICENSE"
        assert not pkg_license.is_symlink(), f"{pkg_license} must not be a symlink"
        assert pkg_license.is_file(), f"Missing synced LICENSE file at {pkg_license}"
        assert pkg_license.read_bytes() == expected_bytes, (
            f"{pkg_license} drifted from root {root_license}"
        )


def test_all_workspace_packages_declare_readme_license_and_python_classifiers() -> None:
    """Every publishable workspace package declares readme, PEP 639 MIT license, sdist includes, and Python 3.11-3.13 classifiers."""
    repo_root = Path(__file__).resolve().parents[2]
    packages_dir = repo_root / "packages"
    expected_dirs = {
        "core": "imednet",
        "plugins-workflows": "imednet_workflows",
        "plugins-sinks": "imednet_sinks",
        "plugins-tui": "imednet_tui",
        "providers-airflow": "apache_airflow_providers_imednet",
    }
    discovered_dirs = {
        p.name for p in packages_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    }
    assert discovered_dirs == set(expected_dirs)

    assert_workspace_license_parity(repo_root)

    for pkg_name, import_pkg in sorted(expected_dirs.items()):
        manifest_path = packages_dir / pkg_name / "pyproject.toml"
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        project = data["project"]

        readme_rel = project.get("readme")
        assert isinstance(readme_rel, str) and readme_rel == "README.md"
        readme_file = manifest_path.parent / readme_rel
        assert readme_file.is_file()
        assert readme_file.read_text(encoding="utf-8").strip() != ""

        assert project.get("license") == "MIT"
        license_files = project.get("license-files")
        assert license_files == ["LICENSE"]
        for lf in license_files:
            assert not lf.startswith("/") and ".." not in Path(lf).parts

        sdist_includes = set(
            data.get("tool", {})
            .get("hatch", {})
            .get("build", {})
            .get("targets", {})
            .get("sdist", {})
            .get("include", [])
        )
        assert {
            "/CHANGELOG.md",
            "/LICENSE",
            "/README.md",
            f"/src/{import_pkg}",
        } <= sdist_includes

        classifiers = set(project.get("classifiers", []))
        for py_classifier in REQUIRED_PYTHON_CLASSIFIERS:
            assert py_classifier in classifiers, (
                f"{manifest_path} missing classifier {py_classifier!r}"
            )


def test_built_workspace_wheels_and_sdists_emit_license_files_and_changelogs(
    tmp_path: Path,
) -> None:
    """Building all five workspace packages populates PEP 639 License-File metadata and sdist docs."""
    repo_root = Path(__file__).resolve().parents[2]
    root_license_bytes = (repo_root / "LICENSE").read_bytes()

    dist_dir = tmp_path / "dists"
    build_distributions(dist_dir)

    built_wheels = sorted(dist_dir.glob("*.whl"))
    built_sdists = sorted(dist_dir.glob("*.tar.gz"))
    assert len(built_wheels) == 5
    assert len(built_sdists) == 5

    expected_names = {
        "imednet",
        "imednet-workflows",
        "imednet-plugins-sinks",
        "imednet-tui",
        "apache-airflow-providers-imednet",
    }
    found_wheel_names: set[str] = set()
    for wheel_path in built_wheels:
        with zipfile.ZipFile(wheel_path) as zf:
            meta_name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
            dist_info_dir = meta_name.rsplit("/", 1)[0]
            license_entry = f"{dist_info_dir}/licenses/LICENSE"
            assert license_entry in zf.namelist()
            assert zf.read(license_entry) == root_license_bytes
            msg = email.message_from_bytes(zf.read(meta_name))

        found_wheel_names.add(msg["Name"])
        assert msg["Description-Content-Type"] == "text/markdown"
        assert msg["License-Expression"] == "MIT"
        assert "LICENSE" in msg.get_all("License-File", [])
        payload = msg.get_payload()
        assert isinstance(payload, str) and len(payload.strip()) > 50
        classifiers = set(msg.get_all("Classifier", []))
        for py_classifier in REQUIRED_PYTHON_CLASSIFIERS:
            assert py_classifier in classifiers

    assert found_wheel_names == expected_names

    expected_import_by_dist = {
        "imednet": "imednet",
        "imednet-workflows": "imednet_workflows",
        "imednet-plugins-sinks": "imednet_sinks",
        "imednet-tui": "imednet_tui",
        "apache-airflow-providers-imednet": "apache_airflow_providers_imednet",
    }
    found_sdist_names: set[str] = set()
    for sdist_path in built_sdists:
        with tarfile.open(sdist_path, "r:gz") as tf:
            by_rel = {
                "/".join(m.name.split("/")[1:]): tf.extractfile(m).read()  # type: ignore[union-attr]
                for m in tf.getmembers()
                if m.isfile() and "/" in m.name
            }
        for req_file in ("PKG-INFO", "LICENSE", "CHANGELOG.md", "README.md", "pyproject.toml"):
            assert req_file in by_rel, f"{sdist_path.name} missing {req_file}"
            assert by_rel[req_file].strip() != b""
        assert by_rel["LICENSE"] == root_license_bytes

        pkg_info = email.message_from_bytes(by_rel["PKG-INFO"])
        dist_name = pkg_info["Name"]
        found_sdist_names.add(dist_name)
        assert pkg_info["License-Expression"] == "MIT"
        assert "LICENSE" in pkg_info.get_all("License-File", [])
        assert f"src/{expected_import_by_dist[dist_name]}/py.typed" in by_rel

    assert found_sdist_names == expected_names


def test_package_license_drift_fails_pytest_and_pre_commit_hook(tmp_path: Path) -> None:
    """Modifying or removing any packages/*/LICENSE without updating root LICENSE fails parity gates."""
    repo_root = Path(__file__).resolve().parents[2]
    hooks_cfg = yaml.safe_load((repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    local_repo = next(r for r in hooks_cfg["repos"] if r["repo"] == "local")
    parity_hook = next(h for h in local_repo["hooks"] if h["id"] == "check-package-license-parity")
    hook_argv = [sys.executable, *shlex.split(parity_hook["entry"])[1:]]

    shutil.copy2(repo_root / "LICENSE", tmp_path / "LICENSE")
    for manifest in sorted((repo_root / "packages").glob("*/pyproject.toml")):
        pkg_dst = tmp_path / "packages" / manifest.parent.name
        pkg_dst.mkdir(parents=True)
        shutil.copy2(manifest, pkg_dst / "pyproject.toml")
        shutil.copy2(manifest.parent / "LICENSE", pkg_dst / "LICENSE")

    assert_workspace_license_parity(tmp_path)
    ok_res = subprocess.run(hook_argv, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert ok_res.returncode == 0, ok_res.stderr

    # Modify one package LICENSE without updating root LICENSE
    tui_license = tmp_path / "packages" / "plugins-tui" / "LICENSE"
    tui_license.write_text("MIT License (modified)\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="drifted"):
        assert_workspace_license_parity(tmp_path)
    drift_res = subprocess.run(hook_argv, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert drift_res.returncode != 0
    assert "plugins-tui/LICENSE" in drift_res.stderr

    # Missing package LICENSE also fails both checks
    tui_license.unlink()
    with pytest.raises(AssertionError, match="Missing synced LICENSE"):
        assert_workspace_license_parity(tmp_path)
    missing_res = subprocess.run(
        hook_argv, cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert missing_res.returncode != 0


def test_verify_wheel_metadata_rejects_missing_description_or_license(tmp_path: Path) -> None:
    """verify_wheel_metadata fails when a wheel omits Markdown description, MIT license, or classifiers."""
    pkgs_dir = tmp_path / "packages"
    sample_pkg = pkgs_dir / "sample"
    sample_pkg.mkdir(parents=True)
    (sample_pkg / "pyproject.toml").write_text(
        '[project]\nname = "imednet-sample"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    # Missing wheel
    with pytest.raises(ValueError, match="Missing built wheel"):
        verify_wheel_metadata(dist_dir, packages_dir=pkgs_dir)

    wheel_path = dist_dir / "imednet_sample-0.1.0-py3-none-any.whl"

    def _write_wheel(
        headers: str,
        body: str,
        *,
        license_bytes: bytes | None = b"MIT License\n",
    ) -> None:
        with zipfile.ZipFile(wheel_path, "w") as zf:
            zf.writestr(
                "imednet_sample-0.1.0.dist-info/METADATA",
                f"Metadata-Version: 2.4\nName: imednet-sample\nVersion: 0.1.0\n{headers}\n{body}",
            )
            if license_bytes is not None:
                zf.writestr("imednet_sample-0.1.0.dist-info/licenses/LICENSE", license_bytes)

    # Missing License-File header
    _write_wheel("Description-Content-Type: text/markdown\nLicense-Expression: MIT\n", "# Sample\n")
    with pytest.raises(ValueError, match="License-File: LICENSE"):
        verify_wheel_metadata(dist_dir, packages_dir=pkgs_dir)

    # Missing .dist-info/licenses/LICENSE entry
    _write_wheel(
        "Description-Content-Type: text/markdown\nLicense-Expression: MIT\nLicense-File: LICENSE\n",
        "# Sample\n",
        license_bytes=None,
    )
    with pytest.raises(ValueError, match="missing bundled license file"):
        verify_wheel_metadata(dist_dir, packages_dir=pkgs_dir)

    # Empty .dist-info/licenses/LICENSE entry
    _write_wheel(
        "Description-Content-Type: text/markdown\nLicense-Expression: MIT\nLicense-File: LICENSE\n",
        "# Sample\n",
        license_bytes=b"   \n",
    )
    with pytest.raises(ValueError, match="empty bundled license file"):
        verify_wheel_metadata(dist_dir, packages_dir=pkgs_dir)

    # Missing Description-Content-Type
    _write_wheel(
        "License-Expression: MIT\nLicense-File: LICENSE\n",
        "# Sample\n",
    )
    with pytest.raises(ValueError, match="Description-Content-Type"):
        verify_wheel_metadata(dist_dir, packages_dir=pkgs_dir)

    # Empty description body
    _write_wheel(
        "Description-Content-Type: text/markdown\nLicense-Expression: MIT\nLicense-File: LICENSE\n",
        "   \n",
    )
    with pytest.raises(ValueError, match="empty METADATA description body"):
        verify_wheel_metadata(dist_dir, packages_dir=pkgs_dir)

    # Missing License-Expression
    _write_wheel(
        "Description-Content-Type: text/markdown\nLicense-File: LICENSE\n",
        "# Sample\n",
    )
    with pytest.raises(ValueError, match="License-Expression"):
        verify_wheel_metadata(dist_dir, packages_dir=pkgs_dir)

    # Missing Python classifiers
    _write_wheel(
        "Description-Content-Type: text/markdown\nLicense-Expression: MIT\nLicense-File: LICENSE\n",
        "# Sample\n",
    )
    with pytest.raises(ValueError, match="missing classifiers"):
        verify_wheel_metadata(dist_dir, packages_dir=pkgs_dir)

    # Valid wheel passes
    classifier_headers = "".join(f"Classifier: {c}\n" for c in REQUIRED_PYTHON_CLASSIFIERS)
    _write_wheel(
        f"Description-Content-Type: text/markdown\nLicense-Expression: MIT\nLicense-File: LICENSE\n{classifier_headers}",
        "# Sample README\n",
    )
    verify_wheel_metadata(dist_dir, packages_dir=pkgs_dir)


def test_validate_wheel_metadata_enforces_license_markdown_and_body(tmp_path: Path) -> None:
    """Verify validate_wheel_metadata checks License/License-Expression, Description-Content-Type, and non-empty body."""
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
        "License: MIT\n"
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
