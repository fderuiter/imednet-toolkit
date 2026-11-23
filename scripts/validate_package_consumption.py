from __future__ import annotations

import argparse
import email
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

try:
    from scripts import tighten_dependencies
except ImportError:
    import tighten_dependencies  # type: ignore[no-redef]

ROOT = Path(__file__).parent.parent.resolve()
PACKAGES_DIR = ROOT / "packages"
MANIFEST_PATH = ROOT / ".release-please-manifest.json"
UV_LOCK_PATH = ROOT / "uv.lock"
REQUIRED_PYTHON_CLASSIFIERS = (
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
)


class ConsumptionValidationError(RuntimeError):
    """Raised when a package consumption scenario or manifest check fails."""


def run(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    print(f"Executing: {' '.join(cmd)}")
    return subprocess.run(list(cmd), check=True, capture_output=True, text=True, **kwargs)


def _resolve_uv() -> str:
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        raise FileNotFoundError("Required tool 'uv' was not found on PATH.")
    return uv_bin


def _normalize_wheel_prefix(pkg_name: str) -> str:
    return re.sub(r"[-_.]+", "_", pkg_name).lower()


def verify_wheel_metadata(dist_dir: Path, packages_dir: Path = PACKAGES_DIR) -> None:
    """Verify built workspace wheels expose Markdown descriptions, MIT license, and Python 3.11-3.13 classifiers."""
    for _, pkg_name in discover_workspace_packages(packages_dir):
        wheel_path = _find_wheel_for_package(dist_dir, pkg_name)
        if wheel_path is None:
            raise ValueError(
                f"Missing built wheel for workspace package '{pkg_name}' in {dist_dir}"
            )

        with zipfile.ZipFile(wheel_path) as zf:
            meta_entries = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
            if not meta_entries:
                raise ValueError(f"Wheel '{wheel_path.name}' is missing .dist-info/METADATA")
            msg = email.message_from_bytes(zf.read(meta_entries[0]))

        content_type = (msg.get("Description-Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "text/markdown":
            raise ValueError(
                f"Wheel '{wheel_path.name}' expected Description-Content-Type 'text/markdown', "
                f"got {msg.get('Description-Content-Type')!r}"
            )

        payload = msg.get_payload()
        if not isinstance(payload, str) or not payload.strip():
            raise ValueError(f"Wheel '{wheel_path.name}' has an empty METADATA description body")

        license_expr = (msg.get("License-Expression") or "").strip()
        if license_expr != "MIT":
            raise ValueError(
                f"Wheel '{wheel_path.name}' expected License-Expression 'MIT', got {license_expr!r}"
            )

        classifiers = set(msg.get_all("Classifier", []))
        missing_classifiers = [c for c in REQUIRED_PYTHON_CLASSIFIERS if c not in classifiers]
        if missing_classifiers:
            raise ValueError(
                f"Wheel '{wheel_path.name}' is missing classifiers: {missing_classifiers}"
            )


def build_wheels(dist_dir: Path, packages_dir: Path = PACKAGES_DIR) -> None:
    uv_bin = _resolve_uv()
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True)

    for pkg_path, _ in discover_workspace_packages(packages_dir):
        _build_single_wheel(uv_bin, pkg_path, dist_dir)

    verify_wheel_metadata(dist_dir, packages_dir=packages_dir)


def discover_workspace_packages(packages_dir: Path = PACKAGES_DIR) -> list[tuple[Path, str]]:
    """Return sorted ``(package_dir, project_name)`` pairs for all workspace packages."""
    discovered: list[tuple[Path, str]] = []
    for pkg_path in sorted(packages_dir.iterdir()):
        manifest = pkg_path / "pyproject.toml"
        if pkg_path.is_dir() and manifest.exists():
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            pkg_name = data["project"]["name"]
            discovered.append((pkg_path, pkg_name))
    return discovered


def collect_manifest_extras(packages_dir: Path = PACKAGES_DIR) -> dict[str, set[str]]:
    """Collect ``[project.optional-dependencies]`` group names keyed by package name."""
    extras_by_pkg: dict[str, set[str]] = {}
    for pkg_path, pkg_name in discover_workspace_packages(packages_dir):
        data = tomllib.loads((pkg_path / "pyproject.toml").read_text(encoding="utf-8"))
        opt_deps = data.get("project", {}).get("optional-dependencies", {})
        if opt_deps:
            extras_by_pkg[pkg_name] = set(opt_deps.keys())
    return extras_by_pkg


def _parse_single_extra_spec(install_items: Sequence[str]) -> tuple[str, str] | None:
    """Parse ``['pkg[extra]']`` into ``(pkg, extra)`` when installing a single extra."""
    if len(install_items) != 1:
        return None
    item = install_items[0].strip()
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\[([A-Za-z0-9._-]+)\]", item)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _is_help_only_cmd(cmd: str) -> bool:
    tokens = cmd.strip().split()
    return bool(tokens) and tokens[0] == "imednet" and ("--help" in tokens or "-h" in tokens)


def verify_manifest_extra_coverage(
    scenarios: Sequence[dict[str, Any]],
    packages_dir: Path = PACKAGES_DIR,
) -> None:
    """Ensure every manifest optional-dependency group has an isolated scenario and smoke check."""
    manifest_extras = collect_manifest_extras(packages_dir)
    expected_pairs = {
        (pkg_name, extra_name)
        for pkg_name, extras in manifest_extras.items()
        for extra_name in extras
    }

    covered_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for scenario in scenarios:
        install_items = scenario.get("install", [])
        for item in install_items:
            if "[" in item and len(install_items) != 1:
                raise ConsumptionValidationError(
                    f"Scenario '{scenario['name']}' installs extra '{item}' alongside extra "
                    f"packages {list(install_items)}; extras must be validated in isolation."
                )

        parsed = _parse_single_extra_spec(install_items)
        if parsed is None:
            continue

        if parsed not in expected_pairs:
            raise ConsumptionValidationError(
                f"Scenario '{scenario['name']}' targets undeclared extra "
                f"'{parsed[0]}[{parsed[1]}]'."
            )

        smoke_checks = scenario.get("smoke", [])
        if not smoke_checks:
            raise ConsumptionValidationError(
                f"Scenario '{scenario['name']}' for '{parsed[0]}[{parsed[1]}]' has no smoke checks."
            )

        positive_non_help = [
            chk
            for chk in smoke_checks
            if not chk.get("expect_fail", False) and not _is_help_only_cmd(chk.get("cmd", ""))
        ]
        if not positive_non_help:
            raise ConsumptionValidationError(
                f"Scenario '{scenario['name']}' for '{parsed[0]}[{parsed[1]}]' "
                "must include at least one non-help module or entry-point import assertion."
            )

        covered_pairs[parsed] = scenario

    missing = sorted(expected_pairs - set(covered_pairs.keys()))
    if missing:
        formatted = ", ".join(f"{pkg}[{extra}]" for pkg, extra in missing)
        raise ConsumptionValidationError(
            f"Optional dependency groups missing isolated validation scenarios: {formatted}"
        )


def _find_wheel_for_package(wheel_dir: Path, pkg_name: str) -> Path | None:
    prefix = _normalize_wheel_prefix(pkg_name) + "-"
    matching = sorted(w for w in wheel_dir.glob("*.whl") if w.name.lower().startswith(prefix))
    return max(matching) if matching else None


def _build_single_wheel(uv_bin: str, pkg_path: Path, dist_dir: Path) -> None:
    print(f"Building wheel for {pkg_path.name}...")
    build_cmd = [
        uv_bin,
        "build",
        "--wheel",
        "--no-create-gitignore",
        "--out-dir",
        str(dist_dir),
        str(pkg_path),
    ]
    try:
        run([*build_cmd, "--offline"])
    except subprocess.CalledProcessError:
        run(build_cmd)


def prepare_wheels(
    dist_dir: Path,
    wheel_dir: Path | None = None,
    *,
    packages_dir: Path = PACKAGES_DIR,
    manifest_path: Path = MANIFEST_PATH,
) -> list[str]:
    """Populate *dist_dir* from *wheel_dir* and build only missing workspace wheels.

    Returns the list of workspace package directory names that were built.
    """
    dist_dir.mkdir(parents=True, exist_ok=True)
    workspace_packages = discover_workspace_packages(packages_dir)

    missing_packages: list[tuple[Path, str]] = []
    for pkg_path, pkg_name in workspace_packages:
        prebuilt = (
            _find_wheel_for_package(wheel_dir, pkg_name)
            if wheel_dir is not None and wheel_dir.is_dir()
            else None
        )
        if prebuilt is not None:
            print(f"Reusing prebuilt wheel for {pkg_path.name}: {prebuilt.name}")
            shutil.copy2(prebuilt, dist_dir / prebuilt.name)
        else:
            missing_packages.append((pkg_path, pkg_name))

    if not missing_packages:
        print("All workspace wheels found in --wheel-dir; skipping wheel builds.")
        return []

    uv_bin = _resolve_uv()
    internal_packages = (
        tighten_dependencies.get_internal_packages(manifest_path) if manifest_path.exists() else {}
    )

    built_names: list[str] = []
    try:
        if internal_packages:
            for pkg_path, _ in missing_packages:
                tighten_dependencies.tighten_file(pkg_path / "pyproject.toml", internal_packages)

        for pkg_path, _ in missing_packages:
            _build_single_wheel(uv_bin, pkg_path, dist_dir)
            built_names.append(pkg_path.name)
    finally:
        if internal_packages:
            tighten_dependencies.restore_files(internal_packages)

    return built_names


def _write_locked_registry_constraints(
    target_file: Path,
    workspace_pkg_names: set[str],
    uv_lock_path: Path = UV_LOCK_PATH,
) -> Path | None:
    """Write third-party version pins from ``uv.lock`` so ``uv`` resolves cached wheels."""
    if not uv_lock_path.is_file():
        return None

    lock_data = tomllib.loads(uv_lock_path.read_text(encoding="utf-8"))
    packages = lock_data.get("package", [])
    name_counts = Counter(pkg.get("name", "") for pkg in packages if pkg.get("name"))
    normalized_workspace = {_normalize_wheel_prefix(name) for name in workspace_pkg_names}

    lines: list[str] = []
    for pkg in packages:
        name = pkg.get("name")
        version = pkg.get("version")
        source = pkg.get("source", {})
        if (
            not name
            or not version
            or name_counts[name] != 1
            or "registry" not in source
            or _normalize_wheel_prefix(name) in normalized_workspace
        ):
            continue
        lines.append(f"{name}=={version}")

    if not lines:
        return None

    target_file.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
    return target_file


def _resolve_workspace_wheels(
    dist_dir: Path,
    packages_dir: Path = PACKAGES_DIR,
) -> dict[str, Path]:
    workspace_wheels: dict[str, Path] = {}
    for _, pkg_name in discover_workspace_packages(packages_dir):
        matched = _find_wheel_for_package(dist_dir, pkg_name)
        if matched is not None:
            workspace_wheels[pkg_name] = matched
    return workspace_wheels


def _install_into_venv(
    uv_bin: str,
    python_bin: Path,
    dist_dir: Path,
    constraints_file: Path,
    locked_constraints_file: Path | None,
    install_specs: Sequence[str],
    tmpdir: str,
) -> None:
    base_cmd = [
        uv_bin,
        "pip",
        "install",
        "--python",
        str(python_bin),
        "--find-links",
        str(dist_dir),
        "-c",
        str(constraints_file),
    ]
    if locked_constraints_file is not None:
        locked_cmd = [*base_cmd, "-c", str(locked_constraints_file), *install_specs]
        try:
            run([*locked_cmd, "--offline"], cwd=tmpdir)
            return
        except subprocess.CalledProcessError:
            try:
                run(locked_cmd, cwd=tmpdir)
                return
            except subprocess.CalledProcessError:
                pass

    unlocked_cmd = [*base_cmd, *install_specs]
    try:
        run([*unlocked_cmd, "--offline"], cwd=tmpdir)
    except subprocess.CalledProcessError:
        run(unlocked_cmd, cwd=tmpdir)


def validate_scenario(
    name: str,
    install_items: Sequence[str],
    smoke_checks: Sequence[dict[str, Any]],
    dist_dir: Path,
    *,
    packages_dir: Path = PACKAGES_DIR,
    uv_lock_path: Path = UV_LOCK_PATH,
) -> None:
    """Validate a single clean-venv package consumption scenario."""
    uv_bin = _resolve_uv()
    print(f"\n>>> Validating Scenario: {name}")
    with tempfile.TemporaryDirectory() as tmpdir:
        venv_dir = Path(tmpdir) / "venv"
        print(f"Creating venv at {venv_dir}")
        run([uv_bin, "venv", "--python", sys.executable, str(venv_dir)], cwd=tmpdir)

        if os.name == "nt":
            python_bin = venv_dir / "Scripts" / "python.exe"
            imednet_bin = venv_dir / "Scripts" / "imednet.exe"
        else:
            python_bin = venv_dir / "bin" / "python"
            imednet_bin = venv_dir / "bin" / "imednet"

        workspace_wheels = _resolve_workspace_wheels(dist_dir, packages_dir)
        constraints_file = Path(tmpdir) / "workspace-constraints.txt"
        constraints_file.write_text(
            "\n".join(
                f"{pkg_name} @ {wheel_path.resolve().as_uri()}"
                for pkg_name, wheel_path in sorted(workspace_wheels.items())
            )
            + "\n",
            encoding="utf-8",
        )
        locked_constraints_file = _write_locked_registry_constraints(
            Path(tmpdir) / "locked-constraints.txt",
            set(workspace_wheels.keys()),
            uv_lock_path=uv_lock_path,
        )

        install_specs: list[str] = []
        for item in install_items:
            base_pkg = item.split("[")[0]
            extra = item[len(base_pkg) :] if "[" in item else ""
            wheel_path = workspace_wheels.get(base_pkg)
            if wheel_path is not None:
                install_specs.append(f"{base_pkg}{extra} @ {wheel_path.resolve().as_uri()}")
            else:
                install_specs.append(item)

        print(f"Installing packages: {list(install_items)}")
        try:
            _install_into_venv(
                uv_bin,
                python_bin,
                dist_dir,
                constraints_file,
                locked_constraints_file,
                install_specs,
                tmpdir,
            )
        except subprocess.CalledProcessError as e:
            print(f"Installation failed: {e.stdout}\n{e.stderr}")
            raise

        for check in smoke_checks:
            cmd = check["cmd"]
            expect_fail = check.get("expect_fail", False)
            contains = check.get("contains")
            not_contains = check.get("not_contains")

            try:
                if cmd.startswith("import "):
                    print(f"Checking import: {cmd} (expect_fail={expect_fail})")
                    res = run([str(python_bin), "-c", cmd])
                elif cmd.startswith("imednet "):
                    print(f"Checking command: {cmd} (expect_fail={expect_fail})")
                    full_cmd = [str(imednet_bin), *cmd.split()[1:]]
                    res = run(full_cmd)
                else:
                    print(f"Running custom command: {cmd} (expect_fail={expect_fail})")
                    res = run([str(python_bin), "-c", cmd])

                output = res.stdout + res.stderr
                if expect_fail:
                    print(f"Command '{cmd}' succeeded but was expected to fail.")
                    print(output)
                    raise ConsumptionValidationError(
                        f"Command '{cmd}' succeeded in scenario '{name}' but was expected to fail."
                    )

                if contains and contains not in output:
                    print(f"Output of '{cmd}' did not contain expected string: {contains}")
                    print(f"Output: {output}")
                    raise ConsumptionValidationError(
                        f"Output of '{cmd}' in scenario '{name}' did not contain '{contains}'."
                    )

                if not_contains and not_contains in output:
                    print(f"Output of '{cmd}' contained unexpected string: {not_contains}")
                    print(f"Output: {output}")
                    raise ConsumptionValidationError(
                        f"Output of '{cmd}' in scenario '{name}' contained '{not_contains}'."
                    )

            except subprocess.CalledProcessError as e:
                output = e.stdout + e.stderr
                if not expect_fail:
                    print(f"Command '{cmd}' failed unexpectedly:")
                    print(output)
                    raise
                print(f"Command '{cmd}' failed as expected.")
                if contains and contains not in output:
                    print(f"Error output of '{cmd}' did not contain expected string: {contains}")
                    print(f"Output: {output}")
                    raise ConsumptionValidationError(
                        f"Error output of '{cmd}' in scenario '{name}' "
                        f"did not contain '{contains}'."
                    ) from e


_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK = (
    "import importlib.metadata as m; "
    "eps = [ep for ep in m.entry_points(group='apache_airflow_provider') "
    "if ep.value == 'apache_airflow_providers_imednet:get_provider_info']; "
    "assert len(eps) == 1 and eps[0].load()()['package-name'] == 'apache-airflow-providers-imednet'"
)


def _extra_requires_check(dist_name: str, extra_name: str, expected_pkgs: Sequence[str]) -> str:
    expected_repr = repr(sorted(_normalize_wheel_prefix(p) for p in expected_pkgs))
    marker_expr = f"extra == '{extra_name}'"
    return (
        "import importlib.metadata as m, re; "
        f"reqs = m.requires({dist_name!r}) or []; "
        f"marker = {marker_expr!r}; "
        "active = {re.sub(r'[-_.]+', '_', r.split(';')[0].strip().split()[0].split('[')[0]"
        ".split('>')[0].split('<')[0].split('=')[0].split('!')[0].split('~')[0]).lower() "
        "for r in reqs if ';' in r and marker in r.split(';', 1)[1].replace('\"', \"'\")}; "
        f"expected = set({expected_repr}); "
        "assert expected <= active, f'Missing extra metadata {expected - active} in {active}'"
    )


def build_scenarios() -> list[dict[str, Any]]:
    """Build isolated validation scenarios covering base packages and all 21 extras."""
    return [
        # ------------------------------------------------------------------
        # Base workspace package scenarios
        # ------------------------------------------------------------------
        {
            "name": "core-minimal",
            "install": ["imednet"],
            "smoke": [
                {"cmd": "import imednet"},
                {"cmd": "import snowflake.connector", "expect_fail": True},
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_sinks", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
                {
                    "cmd": (
                        "from imednet.models.subjects import Subject; "
                        "assert 'subject_id' in Subject.model_fields "
                        "and 'subject_key' in Subject.model_fields"
                    ),
                },
                {
                    "cmd": (
                        "from imednet.models.records import Record; "
                        "assert 'record_id' in Record.model_fields"
                    ),
                },
                {
                    "cmd": "imednet --help",
                    "expect_fail": False,
                    "contains": "pip install 'imednet[cli]'",
                },
            ],
        },
        {
            "name": "workflows-isolated",
            "install": ["imednet-workflows"],
            "smoke": [
                {"cmd": "import imednet_workflows"},
                {"cmd": "import imednet"},
                {
                    "cmd": (
                        "from imednet.sdk import ImednetSDK; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.workflows.data_extraction is not None"
                    ),
                },
                {"cmd": "imednet workflows extract-records --help", "contains": "usage:"},
                {"cmd": "import imednet_sinks", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "sinks-minimal",
            "install": ["imednet-plugins-sinks"],
            "smoke": [
                {"cmd": "import imednet_sinks"},
                {
                    "cmd": (
                        "import importlib.metadata as m; "
                        "eps = {ep.name: ep.load() for ep in m.entry_points(group='imednet.plugins')}; "
                        "assert callable(eps['sinks'])"
                    ),
                },
                {"cmd": "import pymongo", "expect_fail": True},
                {"cmd": "import neo4j", "expect_fail": True},
                {"cmd": "import snowflake.connector", "expect_fail": True},
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "airflow-provider",
            "install": ["apache-airflow-providers-imednet"],
            "smoke": [
                {
                    "cmd": (
                        "import apache_airflow_providers_imednet; "
                        "from apache_airflow_providers_imednet.hooks import ImednetHook; "
                        "from apache_airflow_providers_imednet.operators.export import ImednetExportOperator; "
                        f"{_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK}"
                    ),
                },
                {"cmd": "import imednet_sinks", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        # ------------------------------------------------------------------
        # imednet optional dependency groups (7 of 7)
        # ------------------------------------------------------------------
        {
            "name": "core-cli",
            "install": ["imednet[cli]"],
            "smoke": [
                {"cmd": "import dotenv"},
                {"cmd": "import pythonjsonlogger"},
                {"cmd": "import pygments"},
                {
                    "cmd": (
                        "from imednet.utils.json_logging import configure_json_logging; "
                        "configure_json_logging()"
                    ),
                    "not_contains": "python-json-logger not installed",
                },
                {"cmd": "imednet --help", "contains": "workflows"},
                {
                    "cmd": "imednet workflows extract-records",
                    "expect_fail": True,
                    "contains": "The workflows plugin is not installed.",
                },
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_sinks", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "core-duckdb",
            "install": ["imednet[duckdb]"],
            "smoke": [
                {"cmd": "import duckdb"},
                {
                    "cmd": (
                        "from imednet.cli import export_to_duckdb; "
                        "assert callable(export_to_duckdb)"
                    ),
                },
                {"cmd": "imednet export duckdb --help", "contains": "usage:"},
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_sinks", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "core-export",
            "install": ["imednet[export]"],
            "smoke": [
                {"cmd": "import pandas, sqlalchemy, pyarrow, openpyxl, duckdb, imednet_workflows"},
                {
                    "cmd": (
                        "from imednet.sdk import ImednetSDK; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.workflows.data_extraction is not None"
                    ),
                },
                {"cmd": "imednet export csv --help", "contains": "usage:"},
                {"cmd": "import imednet_sinks", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "core-mongodb",
            "install": ["imednet[mongodb]"],
            "smoke": [
                {"cmd": "import pymongo"},
                {"cmd": "import imednet_sinks.document"},
                {
                    "cmd": (
                        "from imednet.spi.sink_base import _require_optional_dep; "
                        "from imednet.sdk import ImednetSDK; "
                        "assert _require_optional_dep('pymongo', 'mongodb') is not None; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.sinks.MongoDbExportSink is not None"
                    ),
                },
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "core-neo4j",
            "install": ["imednet[neo4j]"],
            "smoke": [
                {"cmd": "import neo4j"},
                {"cmd": "import imednet_sinks.graph"},
                {
                    "cmd": (
                        "from imednet.spi.sink_base import _require_optional_dep; "
                        "from imednet.sdk import ImednetSDK; "
                        "assert _require_optional_dep('neo4j', 'neo4j') is not None; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.sinks.Neo4jExportSink is not None"
                    ),
                },
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "core-snowflake",
            "install": ["imednet[snowflake]"],
            "smoke": [
                {"cmd": "import snowflake.connector"},
                {"cmd": "import pyarrow"},
                {"cmd": "import imednet_sinks.warehouse"},
                {
                    "cmd": (
                        "import pyarrow; "
                        "from imednet.spi.sink_base import _require_optional_dep; "
                        "from imednet.sdk import ImednetSDK; "
                        "assert pyarrow.__version__ and "
                        "_require_optional_dep('snowflake.connector', 'snowflake').__version__; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.sinks.SnowflakeExportSink is not None"
                    ),
                },
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "core-tui",
            "install": ["imednet[tui]"],
            "smoke": [
                {"cmd": "import imednet_tui"},
                {"cmd": "import textual"},
                {
                    "cmd": (
                        "import importlib.metadata as m; "
                        "eps = {ep.name: ep.load() for ep in m.entry_points(group='imednet.cli_plugins')}; "
                        "assert callable(eps['tui'])"
                    ),
                },
                {
                    "cmd": "imednet tui --help",
                    "contains": "usage:",
                    "not_contains": "not installed",
                },
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_sinks", "expect_fail": True},
            ],
        },
        # ------------------------------------------------------------------
        # imednet-plugins-sinks optional dependency groups (4 of 4)
        # ------------------------------------------------------------------
        {
            "name": "sinks-neo4j",
            "install": ["imednet-plugins-sinks[neo4j]"],
            "smoke": [
                {"cmd": "import neo4j"},
                {"cmd": "import imednet_sinks.graph"},
                {
                    "cmd": (
                        "import importlib.metadata as m; "
                        "from imednet.spi.sink_base import _require_optional_dep; "
                        "assert _require_optional_dep('neo4j', 'neo4j') is not None; "
                        "sinks = {ep.name: ep.load() for ep in m.entry_points(group='imednet.sinks')}; "
                        "cfgs = {ep.name: ep.load() for ep in m.entry_points(group='imednet.sink_configs')}; "
                        "assert sinks['neo4j'] is not None and cfgs['neo4j'] is not None"
                    ),
                },
                {"cmd": "import pymongo", "expect_fail": True},
                {"cmd": "import snowflake.connector", "expect_fail": True},
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "sinks-mongodb",
            "install": ["imednet-plugins-sinks[mongodb]"],
            "smoke": [
                {"cmd": "import pymongo"},
                {"cmd": "import imednet_sinks.document"},
                {
                    "cmd": (
                        "import importlib.metadata as m; "
                        "from imednet.spi.sink_base import _require_optional_dep; "
                        "assert _require_optional_dep('pymongo', 'mongodb') is not None; "
                        "sinks = {ep.name: ep.load() for ep in m.entry_points(group='imednet.sinks')}; "
                        "cfgs = {ep.name: ep.load() for ep in m.entry_points(group='imednet.sink_configs')}; "
                        "assert sinks['mongodb'] is not None and cfgs['mongodb'] is not None"
                    ),
                },
                {"cmd": "import neo4j", "expect_fail": True},
                {"cmd": "import snowflake.connector", "expect_fail": True},
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "sinks-snowflake",
            "install": ["imednet-plugins-sinks[snowflake]"],
            "smoke": [
                {"cmd": "import snowflake.connector"},
                {"cmd": "import pyarrow"},
                {"cmd": "import imednet_sinks.warehouse"},
                {
                    "cmd": (
                        "import importlib.metadata as m; "
                        "from imednet.spi.sink_base import _require_optional_dep; "
                        "assert _require_optional_dep('snowflake.connector', 'snowflake').__version__; "
                        "sinks = {ep.name: ep.load() for ep in m.entry_points(group='imednet.sinks')}; "
                        "cfgs = {ep.name: ep.load() for ep in m.entry_points(group='imednet.sink_configs')}; "
                        "assert sinks['snowflake'] is not None and cfgs['snowflake'] is not None"
                    ),
                },
                {"cmd": "import pymongo", "expect_fail": True},
                {"cmd": "import neo4j", "expect_fail": True},
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "sinks-all",
            "install": ["imednet-plugins-sinks[all]"],
            "smoke": [
                {"cmd": "import snowflake.connector"},
                {"cmd": "import pyarrow"},
                {"cmd": "import pymongo"},
                {"cmd": "import neo4j"},
                {
                    "cmd": (
                        "from imednet.sdk import ImednetSDK; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.sinks.MongoDbExportSink and sdk.sinks.Neo4jExportSink "
                        "and sdk.sinks.SnowflakeExportSink"
                    ),
                },
                {"cmd": "import imednet_workflows", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        # ------------------------------------------------------------------
        # apache-airflow-providers-imednet optional dependency groups (10 of 10)
        # ------------------------------------------------------------------
        {
            "name": "airflow-amazon",
            "install": ["apache-airflow-providers-imednet[amazon]"],
            "smoke": [
                {
                    "cmd": (
                        "import airflow.providers.amazon; "
                        "from airflow.providers.amazon.aws.hooks.s3 import S3Hook; "
                        "assert S3Hook is not None; "
                        f"{_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK}"
                    ),
                },
                {"cmd": "import imednet_sinks", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "airflow-duckdb",
            "install": ["apache-airflow-providers-imednet[duckdb]"],
            "smoke": [
                {
                    "cmd": (
                        "import duckdb; "
                        "from apache_airflow_providers_imednet.export import export_to_duckdb; "
                        "assert callable(export_to_duckdb); "
                        f"{_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK}"
                    ),
                },
                {"cmd": "import imednet_sinks", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "airflow-export",
            "install": ["apache-airflow-providers-imednet[export]"],
            "smoke": [
                {
                    "cmd": (
                        "import pandas, sqlalchemy, pyarrow, openpyxl, duckdb, imednet_workflows; "
                        "from apache_airflow_providers_imednet.operators.export import ImednetExportOperator; "
                        "assert ImednetExportOperator is not None; "
                        f"{_extra_requires_check('apache-airflow-providers-imednet', 'export', ['pandas', 'sqlalchemy', 'pyarrow', 'openpyxl', 'duckdb', 'imednet-workflows'])}; "
                        f"{_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK}"
                    ),
                },
                {"cmd": "import imednet_sinks", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "airflow-mongodb",
            "install": ["apache-airflow-providers-imednet[mongodb]"],
            "smoke": [
                {
                    "cmd": (
                        "import pymongo, imednet_sinks.document; "
                        "from imednet.sdk import ImednetSDK; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.sinks.MongoDbExportSink is not None; "
                        f"{_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK}"
                    ),
                },
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "airflow-neo4j",
            "install": ["apache-airflow-providers-imednet[neo4j]"],
            "smoke": [
                {
                    "cmd": (
                        "import neo4j, imednet_sinks.graph; "
                        "from imednet.sdk import ImednetSDK; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.sinks.Neo4jExportSink is not None; "
                        f"{_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK}"
                    ),
                },
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "airflow-openlineage",
            "install": ["apache-airflow-providers-imednet[openlineage]"],
            "smoke": [
                {
                    "cmd": (
                        "import airflow.providers.openlineage; "
                        "from apache_airflow_providers_imednet.operators.export import _load_openlineage_types; "
                        "assert _load_openlineage_types() is not None; "
                        f"{_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK}"
                    ),
                },
                {"cmd": "import imednet_sinks", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "airflow-sinks",
            "install": ["apache-airflow-providers-imednet[sinks]"],
            "smoke": [
                {
                    "cmd": (
                        "import imednet_sinks; "
                        "from imednet.sdk import ImednetSDK; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.sinks.MongoDbExportSink and sdk.sinks.Neo4jExportSink "
                        "and sdk.sinks.SnowflakeExportSink; "
                        f"{_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK}"
                    ),
                },
                {"cmd": "import pymongo", "expect_fail": True},
                {"cmd": "import neo4j", "expect_fail": True},
                {"cmd": "import snowflake.connector", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "airflow-snowflake",
            "install": ["apache-airflow-providers-imednet[snowflake]"],
            "smoke": [
                {
                    "cmd": (
                        "import snowflake.connector, pyarrow, imednet_sinks.warehouse; "
                        "from imednet.sdk import ImednetSDK; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.sinks.SnowflakeExportSink is not None; "
                        f"{_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK}"
                    ),
                },
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "airflow-workflows",
            "install": ["apache-airflow-providers-imednet[workflows]"],
            "smoke": [
                {
                    "cmd": (
                        "import imednet_workflows; "
                        "from imednet.sdk import ImednetSDK; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.workflows.data_extraction is not None; "
                        f"{_extra_requires_check('apache-airflow-providers-imednet', 'workflows', ['imednet-workflows'])}; "
                        f"{_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK}"
                    ),
                },
                {"cmd": "import imednet_sinks", "expect_fail": True},
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
        {
            "name": "airflow-all",
            "install": ["apache-airflow-providers-imednet[all]"],
            "smoke": [
                {
                    "cmd": (
                        "import airflow.providers.amazon, airflow.providers.openlineage, "
                        "duckdb, imednet_sinks, imednet_workflows, neo4j, openpyxl, pandas, "
                        "pyarrow, pymongo, snowflake.connector, sqlalchemy; "
                        "from apache_airflow_providers_imednet.operators.export import _load_openlineage_types; "
                        "from imednet.sdk import ImednetSDK; "
                        "assert _load_openlineage_types() is not None; "
                        "sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); "
                        "assert sdk.sinks.SnowflakeExportSink and sdk.workflows.data_extraction; "
                        f"{_extra_requires_check('apache-airflow-providers-imednet', 'all', ['apache-airflow-providers-amazon', 'apache-airflow-providers-openlineage', 'duckdb', 'imednet-plugins-sinks', 'imednet-workflows', 'neo4j', 'openpyxl', 'pandas', 'pyarrow', 'pymongo', 'snowflake-connector-python', 'sqlalchemy'])}; "
                        f"{_AIRFLOW_PROVIDER_ENTRY_POINT_CHECK}"
                    ),
                },
                {"cmd": "import imednet_tui", "expect_fail": True},
            ],
        },
    ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate clean-environment package and extras consumption."
    )
    parser.add_argument(
        "--wheel-dir",
        type=Path,
        default=None,
        help="Optional directory containing prebuilt workspace wheels to reuse.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    scenarios = build_scenarios()
    try:
        verify_manifest_extra_coverage(scenarios, PACKAGES_DIR)
        with tempfile.TemporaryDirectory(prefix="imednet-consumption-wheels-") as tmp_wheels:
            dist_dir = Path(tmp_wheels)
            prepare_wheels(dist_dir, args.wheel_dir)
            verify_wheel_metadata(dist_dir, PACKAGES_DIR)

            for scenario in scenarios:
                try:
                    validate_scenario(
                        scenario["name"],
                        scenario["install"],
                        scenario["smoke"],
                        dist_dir,
                    )
                except Exception as e:
                    print(f"FAILED scenario {scenario['name']}: {e}")
                    sys.exit(1)
    except Exception as e:
        print(f"FAILED consumption validation: {e}")
        sys.exit(1)

    print("\nALL SCENARIOS PASSED")


if __name__ == "__main__":
    main()
