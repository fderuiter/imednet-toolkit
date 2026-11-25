"""Unit tests for isolated package and extras consumption validation."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

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


def test_prepare_distributions_stages_wheels_and_sdists_and_enforces_strict_dist_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages_dir, manifest_path, pyprojects = _write_fake_workspace(tmp_path)
    original_contents = {k: p.read_text(encoding="utf-8") for k, p in pyprojects.items()}

    dist_dir = tmp_path / "prebuilt_all"
    dist_dir.mkdir()
    prefixes = (
        "imednet-1.1.0",
        "imednet_plugins_sinks-1.0.1",
        "imednet_tui-1.0.0",
        "imednet_workflows-1.1.0",
        "apache_airflow_providers_imednet-1.1.0",
    )
    for prefix in prefixes:
        (dist_dir / f"{prefix}-py3-none-any.whl").write_bytes(b"wheel")
        (dist_dir / f"{prefix}.tar.gz").write_bytes(b"sdist")

    wheel_builds: list[str] = []
    sdist_builds: list[str] = []
    monkeypatch.setattr(
        vpc,
        "_build_single_wheel",
        lambda uv_bin, pkg_path, out_dir: wheel_builds.append(pkg_path.name),
    )
    monkeypatch.setattr(
        vpc,
        "_build_single_sdist",
        lambda uv_bin, pkg_path, out_dir: sdist_builds.append(pkg_path.name),
    )

    staging_dir = tmp_path / "staging"
    built = vpc.prepare_distributions(
        staging_dir,
        dist_dir,
        allow_build_missing=False,
        packages_dir=packages_dir,
        manifest_path=manifest_path,
    )
    assert built == []
    assert wheel_builds == []
    assert sdist_builds == []
    assert len(list(staging_dir.glob("*.whl"))) == 5
    assert len(list(staging_dir.glob("*.tar.gz"))) == 5

    # Missing a single .tar.gz fails under --dist-dir (allow_build_missing=False)
    (dist_dir / "imednet_tui-1.0.0.tar.gz").unlink()
    with pytest.raises(vpc.ConsumptionValidationError, match="Missing built sdist"):
        vpc.prepare_distributions(
            tmp_path / "staging_strict_fail",
            dist_dir,
            allow_build_missing=False,
            packages_dir=packages_dir,
            manifest_path=manifest_path,
        )

    # Nonexistent --dist-dir also fails under allow_build_missing=False
    with pytest.raises(vpc.ConsumptionValidationError, match="does not exist"):
        vpc.prepare_distributions(
            tmp_path / "staging_missing_dir",
            tmp_path / "does_not_exist",
            allow_build_missing=False,
            packages_dir=packages_dir,
            manifest_path=manifest_path,
        )

    # Under --wheel-dir compatibility (allow_build_missing=True), only the missing sdist is built
    monkeypatch.setattr(vpc, "_resolve_uv", lambda: "/usr/bin/uv")
    built_compat = vpc.prepare_distributions(
        tmp_path / "staging_compat",
        dist_dir,
        allow_build_missing=True,
        packages_dir=packages_dir,
        manifest_path=manifest_path,
    )
    assert built_compat == ["plugins-tui"]
    assert wheel_builds == []
    assert sdist_builds == ["plugins-tui"]
    for k, p in pyprojects.items():
        assert p.read_text(encoding="utf-8") == original_contents[k]
        assert not p.with_suffix(".toml.bak").exists()


def test_main_exits_nonzero_when_manifest_coverage_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vpc, "build_scenarios", list)
    with pytest.raises(SystemExit) as exc_info:
        vpc.main([])
    assert exc_info.value.code == 1


def _write_typed_wheel(
    wheel_path: Path,
    *,
    dist_name: str,
    import_pkg: str,
    include_py_typed: bool = True,
    include_typed_classifier: bool = True,
) -> None:
    dist_info = f"{vpc._normalize_wheel_prefix(dist_name)}-1.0.0.dist-info"
    classifiers = list(vpc.REQUIRED_PYTHON_CLASSIFIERS)
    if include_typed_classifier:
        classifiers.append(vpc.TYPED_CLASSIFIER)
    cls_block = "".join(f"Classifier: {c}\n" for c in classifiers)
    metadata = (
        f"Metadata-Version: 2.4\n"
        f"Name: {dist_name}\n"
        f"Version: 1.0.0\n"
        f"License-Expression: MIT\n"
        f"Description-Content-Type: text/markdown\n"
        f"{cls_block}\n"
        f"# {dist_name}\n"
    )
    with zipfile.ZipFile(wheel_path, "w") as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{import_pkg}/__init__.py", "VALUE: int = 1\n")
        if include_py_typed:
            zf.writestr(f"{import_pkg}/py.typed", "")


def test_verify_wheel_py_typed_enforces_marker_and_classifier_across_all_five_packages(
    tmp_path: Path,
) -> None:
    packages_dir, _, pyprojects = _write_fake_workspace(tmp_path)
    import_map = {
        "core": ("imednet", "imednet"),
        "plugins-sinks": ("imednet-plugins-sinks", "imednet_sinks"),
        "plugins-tui": ("imednet-tui", "imednet_tui"),
        "plugins-workflows": ("imednet-workflows", "imednet_workflows"),
        "providers-airflow": (
            "apache-airflow-providers-imednet",
            "apache_airflow_providers_imednet",
        ),
    }
    for folder, (_, import_pkg) in import_map.items():
        with pyprojects[folder].open("a", encoding="utf-8") as fh:
            fh.write(
                f'\n[tool.hatch.build.targets.wheel]\npackages = ["src/{import_pkg}"]\n'
                f'artifacts = ["src/{import_pkg}/py.typed"]\n'
            )

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    for dist_name, import_pkg in import_map.values():
        wheel_name = f"{vpc._normalize_wheel_prefix(dist_name)}-1.0.0-py3-none-any.whl"
        _write_typed_wheel(dist_dir / wheel_name, dist_name=dist_name, import_pkg=import_pkg)

    vpc.verify_wheel_py_typed(dist_dir, packages_dir=packages_dir)

    # Omit imednet_sinks/py.typed -> fails with ConsumptionValidationError
    sinks_wheel = dist_dir / "imednet_plugins_sinks-1.0.0-py3-none-any.whl"
    _write_typed_wheel(
        sinks_wheel,
        dist_name="imednet-plugins-sinks",
        import_pkg="imednet_sinks",
        include_py_typed=False,
    )
    with pytest.raises(vpc.ConsumptionValidationError, match="imednet_sinks/py.typed"):
        vpc.verify_wheel_py_typed(dist_dir, packages_dir=packages_dir)

    # Restore py.typed, omit Typing :: Typed classifier -> fails
    _write_typed_wheel(
        sinks_wheel,
        dist_name="imednet-plugins-sinks",
        import_pkg="imednet_sinks",
        include_py_typed=True,
        include_typed_classifier=False,
    )
    with pytest.raises(vpc.ConsumptionValidationError, match="Typing :: Typed"):
        vpc.verify_wheel_py_typed(dist_dir, packages_dir=packages_dir)


def test_verify_wheel_mypy_strict_validates_typed_wheels_and_catches_type_errors(
    tmp_path: Path,
) -> None:
    pkgs_dir = tmp_path / "packages" / "sample"
    pkgs_dir.mkdir(parents=True)
    (pkgs_dir / "pyproject.toml").write_text(
        '[project]\nname = "imednet-sample"\nversion = "1.0.0"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/imednet_sample"]\n',
        encoding="utf-8",
    )
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    wheel_path = dist_dir / "imednet_sample-1.0.0-py3-none-any.whl"
    _write_typed_wheel(wheel_path, dist_name="imednet-sample", import_pkg="imednet_sample")
    with zipfile.ZipFile(wheel_path, "a") as zf:
        zf.writestr(
            "imednet_sample/WHEEL_TEST_DUMMY",
            "",
        )
        zf.writestr(
            "imednet_sample-1.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        zf.writestr("imednet_sample-1.0.0.dist-info/RECORD", "")

    valid_consumer = "from imednet_sample import VALUE\n\ndef use_val() -> int:\n    return VALUE\n"
    vpc.verify_wheel_mypy_strict(
        dist_dir,
        packages_dir=tmp_path / "packages",
        uv_lock_path=tmp_path / "nonexistent.lock",
        consumer_source=valid_consumer,
    )

    invalid_consumer = (
        "from imednet_sample import VALUE\n\ndef bad_val() -> str:\n    return VALUE\n"
    )
    with pytest.raises(vpc.ConsumptionValidationError, match="mypy --strict failed"):
        vpc.verify_wheel_mypy_strict(
            dist_dir,
            packages_dir=tmp_path / "packages",
            uv_lock_path=tmp_path / "nonexistent.lock",
            consumer_source=invalid_consumer,
        )


def _write_synthetic_sdist(
    sdist_path: Path,
    *,
    top_dir: str,
    dist_name: str,
    import_pkg: str,
    include_license_file_header: bool = True,
    files_override: dict[str, bytes | None] | None = None,
) -> None:
    cls_lines = "".join(
        f"Classifier: {c}\n" for c in (*vpc.REQUIRED_PYTHON_CLASSIFIERS, vpc.TYPED_CLASSIFIER)
    )
    lf_line = "License-File: LICENSE\n" if include_license_file_header else ""
    pkg_info = (
        f"Metadata-Version: 2.4\n"
        f"Name: {dist_name}\n"
        f"Version: 1.0.0\n"
        f"License-Expression: MIT\n"
        f"{lf_line}"
        f"Description-Content-Type: text/markdown\n"
        f"{cls_lines}\n"
        f"# {dist_name}\n\nPackage docs.\n"
    ).encode("utf-8")

    members: dict[str, bytes | None] = {
        "PKG-INFO": pkg_info,
        "LICENSE": b"MIT License\n",
        "CHANGELOG.md": b"# Changelog\n",
        "README.md": b"# Readme\n",
        "pyproject.toml": f'[project]\nname = "{dist_name}"\nversion = "1.0.0"\n'.encode("utf-8"),
        f"src/{import_pkg}/__init__.py": b"VALUE: int = 1\n",
        f"src/{import_pkg}/py.typed": b"",
    }
    if files_override:
        members.update(files_override)

    with tarfile.open(sdist_path, "w:gz") as tf:
        for rel_path, payload in members.items():
            if payload is None:
                continue
            info = tarfile.TarInfo(name=f"{top_dir}/{rel_path}")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))


def test_verify_sdist_archives_enforces_contents_metadata_and_rejects_corruption(
    tmp_path: Path,
) -> None:
    pkgs_dir = tmp_path / "packages"
    core_dir = pkgs_dir / "core"
    tui_dir = pkgs_dir / "plugins-tui"
    for pkg_dir, dist_name, import_pkg in (
        (core_dir, "imednet", "imednet"),
        (tui_dir, "imednet-tui", "imednet_tui"),
    ):
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "LICENSE").write_bytes(b"MIT License\n")
        (pkg_dir / "CHANGELOG.md").write_bytes(b"# Changelog\n")
        (pkg_dir / "README.md").write_bytes(b"# Readme\n")
        (pkg_dir / "pyproject.toml").write_text(
            f'[project]\nname = "{dist_name}"\nversion = "1.0.0"\n'
            f'[tool.hatch.build.targets.wheel]\npackages = ["src/{import_pkg}"]\n',
            encoding="utf-8",
        )

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    # Hyphenated imednet-tui-1.0.0.tar.gz must NOT falsely match imednet!
    _write_synthetic_sdist(
        dist_dir / "imednet-tui-1.0.0.tar.gz",
        top_dir="imednet_tui-1.0.0",
        dist_name="imednet-tui",
        import_pkg="imednet_tui",
    )
    with pytest.raises(vpc.ConsumptionValidationError, match="Missing built sdist.*'imednet'"):
        vpc.verify_sdist_archives(dist_dir, packages_dir=pkgs_dir)

    core_sdist = dist_dir / "imednet-1.0.0.tar.gz"
    _write_synthetic_sdist(
        core_sdist,
        top_dir="imednet-1.0.0",
        dist_name="imednet",
        import_pkg="imednet",
    )
    vpc.verify_sdist_archives(dist_dir, packages_dir=pkgs_dir)

    # Missing License-File header in PKG-INFO fails
    _write_synthetic_sdist(
        core_sdist,
        top_dir="imednet-1.0.0",
        dist_name="imednet",
        import_pkg="imednet",
        include_license_file_header=False,
    )
    with pytest.raises(vpc.ConsumptionValidationError, match="License-File: LICENSE"):
        vpc.verify_sdist_archives(dist_dir, packages_dir=pkgs_dir)

    # Missing CHANGELOG.md fails
    _write_synthetic_sdist(
        core_sdist,
        top_dir="imednet-1.0.0",
        dist_name="imednet",
        import_pkg="imednet",
        files_override={"CHANGELOG.md": None},
    )
    with pytest.raises(vpc.ConsumptionValidationError, match="CHANGELOG.md"):
        vpc.verify_sdist_archives(dist_dir, packages_dir=pkgs_dir)

    # Missing py.typed fails
    _write_synthetic_sdist(
        core_sdist,
        top_dir="imednet-1.0.0",
        dist_name="imednet",
        import_pkg="imednet",
        files_override={"src/imednet/py.typed": None},
    )
    with pytest.raises(vpc.ConsumptionValidationError, match="src/imednet/py.typed"):
        vpc.verify_sdist_archives(dist_dir, packages_dir=pkgs_dir)

    # Corrupt .tar.gz fails
    core_sdist.write_bytes(b"not-a-valid-gzip-stream")
    with pytest.raises(vpc.ConsumptionValidationError, match="Corrupt or unreadable sdist"):
        vpc.verify_sdist_archives(dist_dir, packages_dir=pkgs_dir)


def test_offline_pep517_sdist_rebuild_and_clean_venv_sdist_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkgs_dir = tmp_path / "packages"
    sample_dir = pkgs_dir / "sample"
    src_pkg = sample_dir / "src" / "imednet_sample"
    src_pkg.mkdir(parents=True)
    (src_pkg / "__init__.py").write_text(
        "VALUE: int = 42\n\ndef load_plugin() -> int:\n    return VALUE\n",
        encoding="utf-8",
    )
    (src_pkg / "py.typed").write_text("", encoding="utf-8")
    (sample_dir / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (sample_dir / "CHANGELOG.md").write_text(
        "# Changelog\n\n- Initial release.\n", encoding="utf-8"
    )
    (sample_dir / "README.md").write_text("# imednet-sample\n\nSample docs.\n", encoding="utf-8")
    cls_toml = ", ".join(f'"{c}"' for c in (*vpc.REQUIRED_PYTHON_CLASSIFIERS, vpc.TYPED_CLASSIFIER))
    (sample_dir / "pyproject.toml").write_text(
        f'[project]\nname = "imednet-sample"\nversion = "0.1.0"\n'
        f'readme = "README.md"\nlicense = "MIT"\nlicense-files = ["LICENSE"]\n'
        f'requires-python = ">=3.11"\nclassifiers = [{cls_toml}]\n'
        f'[project.entry-points."imednet.sample"]\nplugin = "imednet_sample:load_plugin"\n'
        f'[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        f'[tool.hatch.build.targets.wheel]\npackages = ["src/imednet_sample"]\n'
        f'artifacts = ["src/imednet_sample/py.typed"]\n'
        f'[tool.hatch.build.targets.sdist]\n'
        f'include = ["/CHANGELOG.md", "/LICENSE", "/README.md", "/src/imednet_sample", "/src/imednet_sample/py.typed"]\n',
        encoding="utf-8",
    )

    dist_dir = tmp_path / "dist"
    vpc.build_distributions(dist_dir, packages_dir=pkgs_dir)
    vpc.verify_sdist_offline_wheel_rebuilds(dist_dir, packages_dir=pkgs_dir)
    vpc.verify_sdist_clean_venv_install(
        dist_dir,
        packages_dir=pkgs_dir,
        uv_lock_path=vpc.UV_LOCK_PATH,
    )

    # Broken sdist fails offline PEP 517 rebuild AND clean-venv sdist install
    sdist_file = next(dist_dir.glob("*.tar.gz"))
    _write_synthetic_sdist(
        sdist_file,
        top_dir="imednet_sample-0.1.0",
        dist_name="imednet-sample",
        import_pkg="imednet_sample",
        files_override={
            "pyproject.toml": (
                b'[project]\nname = "imednet-sample"\nversion = "0.1.0"\n'
                b'[build-system]\nrequires = ["nonexistent-pep517-backend==99.99.99"]\n'
                b'build-backend = "nonexistent_backend"\n'
            )
        },
    )
    with pytest.raises(
        vpc.ConsumptionValidationError, match="Offline PEP 517 wheel rebuild failed"
    ):
        vpc.verify_sdist_offline_wheel_rebuilds(dist_dir, packages_dir=pkgs_dir)

    with pytest.raises(
        vpc.ConsumptionValidationError, match="Clean-venv sdist installation failed"
    ):
        vpc.verify_sdist_clean_venv_install(
            dist_dir,
            packages_dir=pkgs_dir,
            uv_lock_path=tmp_path / "nonexistent.lock",
        )

    # CLI --dist-dir exits non-zero when passed missing or corrupt .tar.gz archives
    monkeypatch.setattr(vpc, "PACKAGES_DIR", pkgs_dir)
    monkeypatch.setattr(vpc, "build_scenarios", list)
    monkeypatch.setattr(vpc, "verify_manifest_extra_coverage", lambda s, p: None)

    corrupt_dir = tmp_path / "corrupt_dist"
    corrupt_dir.mkdir()
    for whl in dist_dir.glob("*.whl"):
        (corrupt_dir / whl.name).write_bytes(whl.read_bytes())
    # Missing .tar.gz -> exit 1
    with pytest.raises(SystemExit) as missing_exc:
        vpc.main(["--dist-dir", str(corrupt_dir)])
    assert missing_exc.value.code == 1

    # Corrupt .tar.gz -> exit 1
    (corrupt_dir / "imednet_sample-0.1.0.tar.gz").write_bytes(b"corrupt-gzip")
    with pytest.raises(SystemExit) as corrupt_exc:
        vpc.main(["--dist-dir", str(corrupt_dir)])
    assert corrupt_exc.value.code == 1


def test_conftest_wheel_mode_gates_sys_path_and_architecture_resolves_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    from tests.unit.architecture import test_architecture

    conftest_path = vpc.ROOT / "tests" / "conftest.py"
    local_core_src = str(vpc.ROOT / "packages" / "core" / "src")

    # Wheel mode (IMEDNET_TEST_WHEELS=1) does NOT inject local packages/*/src
    clean_sys_path = [p for p in sys.path if not p.startswith(str(vpc.ROOT / "packages"))]
    monkeypatch.setattr(sys, "path", list(clean_sys_path))
    monkeypatch.setenv("IMEDNET_TEST_WHEELS", "1")
    spec = importlib.util.spec_from_file_location("_conftest_wheel_probe", conftest_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert local_core_src not in sys.path

    # Unset IMEDNET_TEST_WHEELS preserves local source injection
    with monkeypatch.context() as m:
        m.setattr(sys, "path", list(clean_sys_path))
        m.delenv("IMEDNET_TEST_WHEELS", raising=False)
        spec_default = importlib.util.spec_from_file_location(
            "_conftest_default_probe", conftest_path
        )
        assert spec_default is not None and spec_default.loader is not None
        mod_default = importlib.util.module_from_spec(spec_default)
        spec_default.loader.exec_module(mod_default)
        assert local_core_src in sys.path

    # test_extensions_use_spi still scans workspace packages when imednet loads from site-packages
    fake_site_pkg = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages" / "imednet"
    fake_site_pkg.mkdir(parents=True)
    fake_init = fake_site_pkg / "__init__.py"
    fake_init.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        test_architecture,
        "imednet",
        SimpleNamespace(__file__=str(fake_init)),
    )
    test_architecture.test_extensions_use_spi()
