"""Exercise interpreter compatibility and vulnerable dependency exclusion."""

import tomllib
from pathlib import Path

import httpx
import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]


def test_published_packages_keep_supported_python_lines() -> None:
    """Workspace tooling must not retire a published package's Python support."""
    for path in ROOT.glob("packages/*/pyproject.toml"):
        project = tomllib.loads(path.read_text())["project"]
        supported = SpecifierSet(project["requires-python"])
        assert "3.10.99" not in supported, path
        for version in ("3.11", "3.12", "3.13"):
            assert version in supported, path


def test_workspace_rejects_python_without_patched_selector_support() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    supported = SpecifierSet(project["requires-python"])
    assert "3.11.4" not in supported
    for version in ("3.11.5", "3.12", "3.13"):
        assert version in supported


@pytest.mark.parametrize(
    ("name", "affected", "patched", "later_patch"),
    [
        ("soupsieve", ("2.9.2", "2.10", "3.0.4", "4.0.1", "4.1"), "4.1.1", "4.1.2"),
        ("mako", ("1.3.12", "1.4.1"), "1.4.2", "1.4.3"),
        ("opentelemetry-api", ("1.37.0", "1.43.0", "1.45.1"), "1.45.2", "1.45.3"),
    ],
)
def test_workspace_resolution_excludes_reported_vulnerabilities(
    name, affected, patched, later_patch
) -> None:
    """All locked copies and fresh uv resolutions must exclude vulnerable releases."""
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = [p["version"] for p in locked["package"] if p["name"] == name]
    assert versions
    assert all(Version(version) >= Version(patched) for version in versions)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    constraints = [
        Requirement(value) for value in project["tool"]["uv"].get("constraint-dependencies", [])
    ]
    constraint = next(requirement for requirement in constraints if requirement.name == name)
    assert all(version not in constraint.specifier for version in affected)
    assert patched in constraint.specifier
    assert later_patch in constraint.specifier


def test_workflow_dependency_and_lockfile_exclude_vulnerable_filelock() -> None:
    """Fresh installs and locked installs must both reject affected releases."""
    project = tomllib.loads((ROOT / "packages/plugins-workflows/pyproject.toml").read_text())
    requirements = [Requirement(value) for value in project["project"]["dependencies"]]
    filelock = next(requirement for requirement in requirements if requirement.name == "filelock")
    for version in ("3.20.3", "4.1.0", "4.2.0"):
        assert version not in filelock.specifier
    assert "4.2.1" in filelock.specifier
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = [
        package["version"] for package in locked["package"] if package["name"] == "filelock"
    ]
    assert versions
    assert all(Version(version) >= Version("4.2.1") for version in versions)


def test_duckdb_dependencies_and_lockfile_exclude_unstable_v2_major() -> None:
    """Unlocked Hatch/pip and locked uv resolutions must exclude duckdb 2.x crashes."""
    root_project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    core_project = tomllib.loads((ROOT / "packages/core/pyproject.toml").read_text())["project"]

    dev_reqs = [Requirement(v) for v in root_project["optional-dependencies"]["dev"]]
    duckdb_extra_reqs = [Requirement(v) for v in core_project["optional-dependencies"]["duckdb"]]
    export_extra_reqs = [Requirement(v) for v in core_project["optional-dependencies"]["export"]]

    for reqs in (dev_reqs, duckdb_extra_reqs, export_extra_reqs):
        duckdb_req = next(r for r in reqs if r.name == "duckdb")
        assert "1.5.4" in duckdb_req.specifier
        assert "2.0.0" not in duckdb_req.specifier

    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = [p["version"] for p in locked["package"] if p["name"] == "duckdb"]
    assert versions
    assert all(Version("1.1.0") <= Version(v) < Version("2.0.0") for v in versions)


@pytest.mark.parametrize("proxy", ["socks5://127.0.0.1:1", "socks5h://127.0.0.1:1"])
def test_development_clients_support_socks_proxy_configuration(monkeypatch, proxy) -> None:
    """Construct transports without contacting a proxy or EDC server."""
    monkeypatch.setenv("ALL_PROXY", proxy)
    for name in ("all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    with httpx.Client():
        pass


@pytest.mark.asyncio
async def test_async_development_client_supports_socks_proxy_configuration(monkeypatch) -> None:
    """Async transports must also construct with inherited SOCKS settings."""
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:1")
    monkeypatch.delenv("all_proxy", raising=False)
    async with httpx.AsyncClient():
        pass
