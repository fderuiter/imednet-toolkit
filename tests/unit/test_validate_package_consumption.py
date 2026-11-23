"""Unit tests for isolated package and extras consumption validation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from scripts import validate_package_consumption as vpc


def _write_fake_workspace(root: Path) -> tuple[Path, Path, dict[str, Path]]:
    packages_dir = root / "packages"
    specs = [
        (
            "core",
            "imednet",
            "1.1.0",
            ["pydantic>=2.0"],
            {"cli": ["python-dotenv"], "tui": ["imednet-tui"]},
        ),
        (
            "plugins-sinks",
            "imednet-plugins-sinks",
            "1.0.1",
            ["imednet>=1.0.0,<2.0.0"],
            {"mongodb": ["pymongo"]},
        ),
        ("plugins-tui", "imednet-tui", "1.0.0", ["imednet>=1.0.0,<2.0.0"], {}),
        ("plugins-workflows", "imednet-workflows", "1.1.0", ["imednet>=1.0.0,<2.0.0"], {}),
        (
            "providers-airflow",
            "apache-airflow-providers-imednet",
            "1.1.0",
            ["imednet>=1.0.0,<2.0.0", "imednet-workflows>=1.0.0,<2.0.0"],
            {"sinks": ["imednet-plugins-sinks"]},
        ),
    ]
    manifest_data: dict[str, str] = {}
    pyproject_paths: dict[str, Path] = {}

    for folder, pkg_name, version, deps, extras in specs:
        pkg_dir = packages_dir / folder
        pkg_dir.mkdir(parents=True)
        manifest_data[f"packages/{folder}"] = version
        lines = [
            "[project]",
            f'name = "{pkg_name}"',
            f'version = "{version}"',
            f"dependencies = {json.dumps(deps)}",
        ]
        if extras:
            lines.append("[project.optional-dependencies]")
            for extra_name, extra_deps in extras.items():
                lines.append(f"{extra_name} = {json.dumps(extra_deps)}")
        pyproject = pkg_dir / "pyproject.toml"
        pyproject.write_text("\n".join(lines) + "\n", encoding="utf-8")
        pyproject_paths[folder] = pyproject

    manifest_path = root / ".release-please-manifest.json"
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    return packages_dir, manifest_path, pyproject_paths


def test_checked_in_manifests_have_complete_isolated_scenarios() -> None:
    extras = vpc.collect_manifest_extras()
    assert len(extras["imednet"]) == 7
    assert len(extras["imednet-plugins-sinks"]) == 4
    assert len(extras["apache-airflow-providers-imednet"]) == 10
    assert sum(len(v) for v in extras.values()) == 21

    scenarios = vpc.build_scenarios()
    vpc.verify_manifest_extra_coverage(scenarios)

    by_install = {tuple(s["install"]): s for s in scenarios}
    for pkg_name, pkg_extras in extras.items():
        for extra_name in pkg_extras:
            key = (f"{pkg_name}[{extra_name}]",)
            assert key in by_install, f"Missing isolated scenario for {key[0]}"
            smoke = by_install[key]["smoke"]
            assert any(chk.get("expect_fail") for chk in smoke), (
                f"Scenario for {key[0]} should include negative import assertions"
            )


def test_core_duckdb_and_cli_scenarios_assert_positive_and_negative_imports() -> None:
    by_name = {s["name"]: s for s in vpc.build_scenarios()}

    duckdb_cmds = {(c["cmd"], c.get("expect_fail", False)) for c in by_name["core-duckdb"]["smoke"]}
    assert ("import duckdb", False) in duckdb_cmds
    assert ("import imednet_workflows", True) in duckdb_cmds
    assert ("import imednet_sinks", True) in duckdb_cmds
    assert ("import imednet_tui", True) in duckdb_cmds

    cli_cmds = {(c["cmd"], c.get("expect_fail", False)) for c in by_name["core-cli"]["smoke"]}
    assert ("import dotenv", False) in cli_cmds
    assert ("import pythonjsonlogger", False) in cli_cmds
    assert ("import pygments", False) in cli_cmds
    assert ("import imednet_workflows", True) in cli_cmds
    assert ("import imednet_sinks", True) in cli_cmds
    assert ("import imednet_tui", True) in cli_cmds


def test_verify_manifest_extra_coverage_rejects_missing_or_weak_scenarios(tmp_path: Path) -> None:
    packages_dir, _, _ = _write_fake_workspace(tmp_path)
    full_scenarios = [
        {"name": "cli", "install": ["imednet[cli]"], "smoke": [{"cmd": "import dotenv"}]},
        {"name": "tui", "install": ["imednet[tui]"], "smoke": [{"cmd": "import imednet_tui"}]},
        {
            "name": "mongo",
            "install": ["imednet-plugins-sinks[mongodb]"],
            "smoke": [{"cmd": "import pymongo"}],
        },
        {
            "name": "sinks",
            "install": ["apache-airflow-providers-imednet[sinks]"],
            "smoke": [{"cmd": "import imednet_sinks"}],
        },
    ]
    vpc.verify_manifest_extra_coverage(full_scenarios, packages_dir)

    # Missing an extra fails
    with pytest.raises(vpc.ConsumptionValidationError, match="imednet\\[tui\\]"):
        vpc.verify_manifest_extra_coverage(full_scenarios[:1] + full_scenarios[2:], packages_dir)

    # Preinstalling extra workspace packages fails
    preinstalled = [
        {
            "name": "cli",
            "install": ["imednet-workflows", "imednet[cli]"],
            "smoke": [{"cmd": "import dotenv"}],
        },
        *full_scenarios[1:],
    ]
    with pytest.raises(vpc.ConsumptionValidationError, match="in isolation"):
        vpc.verify_manifest_extra_coverage(preinstalled, packages_dir)

    # Help-only smoke check fails
    help_only = [
        {"name": "cli", "install": ["imednet[cli]"], "smoke": [{"cmd": "imednet --help"}]},
        *full_scenarios[1:],
    ]
    with pytest.raises(vpc.ConsumptionValidationError, match="non-help"):
        vpc.verify_manifest_extra_coverage(help_only, packages_dir)

    # Empty smoke check fails
    empty_smoke = [
        {"name": "cli", "install": ["imednet[cli]"], "smoke": []},
        *full_scenarios[1:],
    ]
    with pytest.raises(vpc.ConsumptionValidationError, match="no smoke checks"):
        vpc.verify_manifest_extra_coverage(empty_smoke, packages_dir)

    # Undeclared extra fails
    undeclared = [
        *full_scenarios,
        {"name": "bogus", "install": ["imednet[bogus]"], "smoke": [{"cmd": "import imednet"}]},
    ]
    with pytest.raises(vpc.ConsumptionValidationError, match="undeclared extra"):
        vpc.verify_manifest_extra_coverage(undeclared, packages_dir)


def test_prepare_wheels_skips_builds_when_all_wheels_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages_dir, manifest_path, pyprojects = _write_fake_workspace(tmp_path)
    original_contents = {k: p.read_text(encoding="utf-8") for k, p in pyprojects.items()}

    wheel_dir = tmp_path / "prebuilt"
    wheel_dir.mkdir()
    for prefix in (
        "imednet-1.1.0",
        "imednet_plugins_sinks-1.0.1",
        "imednet_tui-1.0.0",
        "imednet_workflows-1.1.0",
        "apache_airflow_providers_imednet-1.1.0",
    ):
        (wheel_dir / f"{prefix}-py3-none-any.whl").write_bytes(b"wheel")

    build_calls: list[str] = []
    monkeypatch.setattr(
        vpc,
        "_build_single_wheel",
        lambda uv_bin, pkg_path, dist_dir: build_calls.append(pkg_path.name),
    )

    staging_dir = tmp_path / "staging"
    built = vpc.prepare_wheels(
        staging_dir,
        wheel_dir,
        packages_dir=packages_dir,
        manifest_path=manifest_path,
    )

    assert built == []
    assert build_calls == []
    assert len(list(staging_dir.glob("*.whl"))) == 5
    for k, p in pyprojects.items():
        assert p.read_text(encoding="utf-8") == original_contents[k]
        assert not p.with_suffix(".toml.bak").exists()


def test_prepare_wheels_partial_wheel_dir_builds_only_missing_and_restores_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages_dir, manifest_path, pyprojects = _write_fake_workspace(tmp_path)
    original_contents = {k: p.read_text(encoding="utf-8") for k, p in pyprojects.items()}

    wheel_dir = tmp_path / "partial_prebuilt"
    wheel_dir.mkdir()
    (wheel_dir / "imednet-1.1.0-py3-none-any.whl").write_bytes(b"core-wheel")

    tightened_during_build: dict[str, str] = {}

    def fake_build(uv_bin: str, pkg_path: Path, dist_dir: Path) -> None:
        tightened_during_build[pkg_path.name] = (pkg_path / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        norm = vpc._normalize_wheel_prefix(pkg_path.name)
        (dist_dir / f"{norm}-1.0.0-py3-none-any.whl").write_bytes(b"built")

    monkeypatch.setattr(vpc, "_resolve_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(vpc, "_build_single_wheel", fake_build)

    staging_dir = tmp_path / "staging"
    built = vpc.prepare_wheels(
        staging_dir,
        wheel_dir,
        packages_dir=packages_dir,
        manifest_path=manifest_path,
    )

    assert built == ["plugins-sinks", "plugins-tui", "plugins-workflows", "providers-airflow"]
    assert "core" not in tightened_during_build
    # Internal bounds were tightened before building missing packages
    assert "<1.2.0" in tightened_during_build["plugins-sinks"]
    assert "<1.1.0" in tightened_during_build["providers-airflow"]

    # All manifests restored cleanly afterward
    for k, p in pyprojects.items():
        assert p.read_text(encoding="utf-8") == original_contents[k]
        assert not p.with_suffix(".toml.bak").exists()


def test_prepare_wheels_restores_manifests_on_build_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages_dir, manifest_path, pyprojects = _write_fake_workspace(tmp_path)
    original_contents = {k: p.read_text(encoding="utf-8") for k, p in pyprojects.items()}

    def failing_build(uv_bin: str, pkg_path: Path, dist_dir: Path) -> None:
        raise subprocess.CalledProcessError(1, ["uv", "build"])

    monkeypatch.setattr(vpc, "_resolve_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(vpc, "_build_single_wheel", failing_build)

    with pytest.raises(subprocess.CalledProcessError):
        vpc.prepare_wheels(
            tmp_path / "staging",
            tmp_path / "nonexistent-wheel-dir",
            packages_dir=packages_dir,
            manifest_path=manifest_path,
        )

    for k, p in pyprojects.items():
        assert p.read_text(encoding="utf-8") == original_contents[k]
        assert not p.with_suffix(".toml.bak").exists()


def test_main_exits_nonzero_when_manifest_coverage_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vpc, "build_scenarios", list)
    with pytest.raises(SystemExit) as exc_info:
        vpc.main([])
    assert exc_info.value.code == 1
