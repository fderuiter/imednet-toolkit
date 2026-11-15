"""Exercise interpreter compatibility and vulnerable dependency exclusion."""

import tomllib
from pathlib import Path

import httpx
import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]


def test_all_packages_share_the_supported_python_floor() -> None:
    """Published packages must reject the interpreter unable to install patched locks."""
    for path in [ROOT / "pyproject.toml", *ROOT.glob("packages/*/pyproject.toml")]:
        project = tomllib.loads(path.read_text())["project"]
        supported = SpecifierSet(project["requires-python"])
        assert "3.10.99" not in supported, path
        for version in ("3.11", "3.12", "3.13"):
            assert version in supported, path


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
