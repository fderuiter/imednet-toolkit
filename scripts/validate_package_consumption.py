from __future__ import annotations

import argparse
import email
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
import zlib
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
TYPED_CLASSIFIER = "Typing :: Typed"
_CONSUMER_TYPING_CHECK = """from __future__ import annotations

import apache_airflow_providers_imednet
import imednet
import imednet_sinks
import imednet_tui
import imednet_workflows
from apache_airflow_providers_imednet import get_provider_info
from apache_airflow_providers_imednet.hooks import ImednetHook
from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
from imednet import AsyncImednetSDK, Config, ImednetSDK
from imednet.models.records import Record
from imednet.models.studies import Study
from imednet_sinks.plugin import SinksNamespace, create_sinks
from imednet_tui.cli import setup_parser as setup_tui_parser
from imednet_workflows import DataExtractionWorkflow, StudySchemaInspector, UATWorkflow


def verify_typed_consumer(
    sdk: ImednetSDK,
    async_sdk: AsyncImednetSDK,
    cfg: Config,
) -> tuple[type[Study], type[Record], dict[str, object]]:
    info = get_provider_info()
    _ = (
        apache_airflow_providers_imednet,
        imednet,
        imednet_sinks,
        imednet_tui,
        imednet_workflows,
        ImednetHook,
        ImednetExportOperator,
        SinksNamespace,
        create_sinks,
        setup_tui_parser,
        DataExtractionWorkflow,
        StudySchemaInspector,
        UATWorkflow,
        sdk,
        async_sdk,
        cfg,
    )
    return Study, Record, info
"""


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


def verify_wheel_metadata(
    dist_dir: Path,
    packages_dir: Path = PACKAGES_DIR,
    *,
    require_classifiers: bool = True,
    require_license_file: bool = True,
) -> None:
    """Verify built workspace wheels expose Markdown descriptions, MIT license, License-File, and Python 3.11-3.13 classifiers."""
    for pkg_path, pkg_name in discover_workspace_packages(packages_dir):
        wheel_path = _find_wheel_for_package(dist_dir, pkg_name)
        if wheel_path is None:
            raise ValueError(
                f"Missing built wheel for workspace package '{pkg_name}' in {dist_dir}"
            )

        with zipfile.ZipFile(wheel_path) as zf:
            names = set(zf.namelist())
            meta_entries = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
            if len(meta_entries) != 1:
                raise ValueError(
                    f"Wheel '{wheel_path.name}' is missing .dist-info/METADATA (found {meta_entries})"
                )
            dist_info_dir = meta_entries[0].rsplit("/", 1)[0]
            msg = email.message_from_bytes(zf.read(meta_entries[0]))

            if require_license_file:
                license_files = [v.strip() for v in msg.get_all("License-File", []) if v.strip()]
                if "LICENSE" not in license_files:
                    raise ValueError(
                        f"Wheel '{wheel_path.name}' expected 'License-File: LICENSE' in METADATA, "
                        f"got {license_files!r}"
                    )
                license_entry = f"{dist_info_dir}/licenses/LICENSE"
                if license_entry not in names:
                    raise ValueError(
                        f"Wheel '{wheel_path.name}' is missing bundled license file '{license_entry}'"
                    )
                bundled_license = zf.read(license_entry)
                if not bundled_license.strip():
                    raise ValueError(
                        f"Wheel '{wheel_path.name}' has an empty bundled license file '{license_entry}'"
                    )
                source_license = pkg_path / "LICENSE"
                if source_license.is_file() and bundled_license != source_license.read_bytes():
                    raise ValueError(
                        f"Wheel '{wheel_path.name}' bundled '{license_entry}' does not match {source_license}"
                    )

        content_type = (msg.get("Description-Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "text/markdown":
            raise ValueError(
                f"Wheel '{wheel_path.name}' expected Description-Content-Type 'text/markdown', "
                f"got {msg.get('Description-Content-Type')!r}"
            )

        payload = msg.get_payload()
        if not isinstance(payload, str) or not payload.strip():
            raise ValueError(f"Wheel '{wheel_path.name}' has an empty METADATA description body")

        license_val = (msg.get("License-Expression") or msg.get("License") or "").strip()
        if license_val != "MIT":
            raise ValueError(
                f"Wheel '{wheel_path.name}' expected License-Expression 'MIT', got {license_val!r}"
            )

        if require_classifiers:
            classifiers = set(msg.get_all("Classifier", []))
            missing_classifiers = [c for c in REQUIRED_PYTHON_CLASSIFIERS if c not in classifiers]
            if missing_classifiers:
                raise ValueError(
                    f"Wheel '{wheel_path.name}' is missing classifiers: {missing_classifiers}"
                )
        print(
            f"Verified wheel METADATA for {wheel_path.name} "
            f"(license={license_val}, content_type={content_type}, body_chars={len(payload.strip())})."
        )


def validate_wheel_metadata(dist_dir: Path, packages_dir: Path = PACKAGES_DIR) -> None:
    """Verify every built wheel in dist_dir declares License/License-Expression, text/markdown, and a non-empty body."""
    if not any(dist_dir.glob("*.whl")):
        raise ValueError(f"No built wheels (.whl) found in {dist_dir}")
    verify_wheel_metadata(
        dist_dir,
        packages_dir=packages_dir,
        require_classifiers=False,
        require_license_file=False,
    )


def build_wheels(dist_dir: Path, packages_dir: Path = PACKAGES_DIR) -> None:
    uv_bin = _resolve_uv()
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True)

    for pkg_path, _ in discover_workspace_packages(packages_dir):
        _build_single_wheel(uv_bin, pkg_path, dist_dir)

    verify_wheel_metadata(dist_dir, packages_dir=packages_dir)
    verify_wheel_py_typed(dist_dir, packages_dir=packages_dir)


def build_distributions(dist_dir: Path, packages_dir: Path = PACKAGES_DIR) -> None:
    """Build both wheels and sdists for all workspace packages and verify archive metadata."""
    uv_bin = _resolve_uv()
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True)

    for pkg_path, _ in discover_workspace_packages(packages_dir):
        _build_single_wheel(uv_bin, pkg_path, dist_dir)
        _build_single_sdist(uv_bin, pkg_path, dist_dir)

    verify_wheel_metadata(dist_dir, packages_dir=packages_dir)
    verify_wheel_py_typed(dist_dir, packages_dir=packages_dir)
    verify_sdist_archives(dist_dir, packages_dir=packages_dir)


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


def discover_workspace_import_packages(
    packages_dir: Path = PACKAGES_DIR,
) -> list[tuple[Path, str, str]]:
    """Return sorted ``(package_dir, project_name, import_package)`` tuples for workspace packages."""
    discovered: list[tuple[Path, str, str]] = []
    for pkg_path, pkg_name in discover_workspace_packages(packages_dir):
        data = tomllib.loads((pkg_path / "pyproject.toml").read_text(encoding="utf-8"))
        wheel_pkgs = (
            data.get("tool", {})
            .get("hatch", {})
            .get("build", {})
            .get("targets", {})
            .get("wheel", {})
            .get("packages", [])
        )
        if wheel_pkgs:
            import_pkg = Path(wheel_pkgs[0]).name
        else:
            src_dir = pkg_path / "src"
            src_pkgs = (
                sorted(
                    p.name
                    for p in src_dir.iterdir()
                    if p.is_dir() and (p / "__init__.py").is_file()
                )
                if src_dir.is_dir()
                else []
            )
            import_pkg = src_pkgs[0] if src_pkgs else _normalize_wheel_prefix(pkg_name)
        discovered.append((pkg_path, pkg_name, import_pkg))
    return discovered


def verify_wheel_py_typed(
    dist_dir: Path,
    packages_dir: Path = PACKAGES_DIR,
) -> None:
    """Verify every built workspace wheel embeds PEP 561 ``py.typed`` and ``Typing :: Typed``."""
    for _, pkg_name, import_pkg in discover_workspace_import_packages(packages_dir):
        wheel_path = _find_wheel_for_package(dist_dir, pkg_name)
        if wheel_path is None:
            raise ConsumptionValidationError(
                f"Missing built wheel for workspace package '{pkg_name}' in {dist_dir}"
            )

        expected_marker = f"{import_pkg}/py.typed"
        with zipfile.ZipFile(wheel_path) as zf:
            names = set(zf.namelist())
            if expected_marker not in names:
                raise ConsumptionValidationError(
                    f"Wheel '{wheel_path.name}' is missing PEP 561 marker '{expected_marker}'"
                )
            meta_entries = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
            if len(meta_entries) != 1:
                raise ConsumptionValidationError(
                    f"Wheel '{wheel_path.name}' is missing .dist-info/METADATA (found {meta_entries})"
                )
            msg = email.message_from_bytes(zf.read(meta_entries[0]))

        classifiers = set(msg.get_all("Classifier", []))
        if TYPED_CLASSIFIER not in classifiers:
            raise ConsumptionValidationError(
                f"Wheel '{wheel_path.name}' is missing classifier {TYPED_CLASSIFIER!r}"
            )
        print(f"Verified PEP 561 py.typed marker in {wheel_path.name} ({expected_marker}).")


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


def _find_sdist_for_package(dist_dir: Path, pkg_name: str) -> Path | None:
    expected_norm = _normalize_wheel_prefix(pkg_name)
    matching: list[Path] = []
    for candidate in sorted(dist_dir.glob("*.tar.gz")):
        stem = candidate.name[: -len(".tar.gz")]
        match = re.match(r"^(.+)-(\d.*)$", stem)
        if match and _normalize_wheel_prefix(match.group(1)) == expected_norm:
            matching.append(candidate)
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


def _build_single_sdist(uv_bin: str, pkg_path: Path, dist_dir: Path) -> None:
    print(f"Building sdist for {pkg_path.name}...")
    build_cmd = [
        uv_bin,
        "build",
        "--sdist",
        "--no-create-gitignore",
        "--out-dir",
        str(dist_dir),
        str(pkg_path),
    ]
    try:
        run([*build_cmd, "--offline"])
    except subprocess.CalledProcessError:
        run(build_cmd)


def _tighten_and_run_builds(
    packages_to_build: Sequence[Path],
    manifest_path: Path,
    build_fn: Any,
) -> None:
    internal_packages = (
        tighten_dependencies.get_internal_packages(manifest_path) if manifest_path.exists() else {}
    )
    try:
        if internal_packages:
            for pkg_path in packages_to_build:
                tighten_dependencies.tighten_file(pkg_path / "pyproject.toml", internal_packages)
        for pkg_path in packages_to_build:
            build_fn(pkg_path)
    finally:
        if internal_packages:
            tighten_dependencies.restore_files(internal_packages)
        for pkg_path in packages_to_build:
            pyproject_path = pkg_path / "pyproject.toml"
            backup_path = pyproject_path.with_suffix(".toml.bak")
            if backup_path.exists():
                shutil.copy2(backup_path, pyproject_path)
                backup_path.unlink()
                print(f"Restored {pyproject_path} from backup")


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
    built_names: list[str] = []

    def _build_pkg_wheel(pkg_path: Path) -> None:
        _build_single_wheel(uv_bin, pkg_path, dist_dir)
        built_names.append(pkg_path.name)

    _tighten_and_run_builds([p for p, _ in missing_packages], manifest_path, _build_pkg_wheel)
    return built_names


def prepare_distributions(
    dist_dir: Path,
    source_dir: Path | None = None,
    *,
    allow_build_missing: bool = True,
    packages_dir: Path = PACKAGES_DIR,
    manifest_path: Path = MANIFEST_PATH,
) -> list[str]:
    """Populate *dist_dir* with both ``.whl`` and ``.tar.gz`` distributions.

    When *allow_build_missing* is ``False`` (used for ``--dist-dir``), *source_dir*
    must exist and already contain both ``.whl`` and ``.tar.gz`` archives for every
    workspace package, raising :class:`ConsumptionValidationError` otherwise.
    """
    if not allow_build_missing and (source_dir is None or not source_dir.is_dir()):
        raise ConsumptionValidationError(f"Distribution directory does not exist: {source_dir}")

    dist_dir.mkdir(parents=True, exist_ok=True)
    workspace_packages = discover_workspace_packages(packages_dir)

    missing_wheels: set[Path] = set()
    missing_sdists: set[Path] = set()
    packages_needing_build: list[Path] = []

    for pkg_path, pkg_name in workspace_packages:
        prebuilt_wheel = (
            _find_wheel_for_package(source_dir, pkg_name)
            if source_dir is not None and source_dir.is_dir()
            else None
        )
        prebuilt_sdist = (
            _find_sdist_for_package(source_dir, pkg_name)
            if source_dir is not None and source_dir.is_dir()
            else None
        )

        if prebuilt_wheel is not None:
            print(f"Reusing prebuilt wheel for {pkg_path.name}: {prebuilt_wheel.name}")
            shutil.copy2(prebuilt_wheel, dist_dir / prebuilt_wheel.name)
        elif not allow_build_missing:
            raise ConsumptionValidationError(
                f"Missing built wheel (.whl) for workspace package '{pkg_name}' in {source_dir}"
            )
        else:
            missing_wheels.add(pkg_path)

        if prebuilt_sdist is not None:
            print(f"Reusing prebuilt sdist for {pkg_path.name}: {prebuilt_sdist.name}")
            shutil.copy2(prebuilt_sdist, dist_dir / prebuilt_sdist.name)
        elif not allow_build_missing:
            raise ConsumptionValidationError(
                f"Missing built sdist (.tar.gz) for workspace package '{pkg_name}' in {source_dir}"
            )
        else:
            missing_sdists.add(pkg_path)

        if pkg_path in missing_wheels or pkg_path in missing_sdists:
            packages_needing_build.append(pkg_path)

    if not packages_needing_build:
        print("All workspace wheels and sdists found in prebuilt directory; skipping builds.")
        return []

    uv_bin = _resolve_uv()
    built_names: list[str] = []

    def _build_pkg_artifacts(pkg_path: Path) -> None:
        if pkg_path in missing_wheels:
            _build_single_wheel(uv_bin, pkg_path, dist_dir)
        if pkg_path in missing_sdists:
            _build_single_sdist(uv_bin, pkg_path, dist_dir)
        built_names.append(pkg_path.name)

    _tighten_and_run_builds(packages_needing_build, manifest_path, _build_pkg_artifacts)
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


def _resolve_workspace_sdists(
    dist_dir: Path,
    packages_dir: Path = PACKAGES_DIR,
) -> dict[str, Path]:
    workspace_sdists: dict[str, Path] = {}
    for _, pkg_name in discover_workspace_packages(packages_dir):
        matched = _find_sdist_for_package(dist_dir, pkg_name)
        if matched is not None:
            workspace_sdists[pkg_name] = matched
    return workspace_sdists


def verify_sdist_archives(
    dist_dir: Path,
    packages_dir: Path = PACKAGES_DIR,
    *,
    require_classifiers: bool = True,
) -> None:
    """Verify every built workspace sdist contains compliant metadata, docs, license, and py.typed."""
    for pkg_path, pkg_name, import_pkg in discover_workspace_import_packages(packages_dir):
        sdist_path = _find_sdist_for_package(dist_dir, pkg_name)
        if sdist_path is None:
            raise ConsumptionValidationError(
                f"Missing built sdist (.tar.gz) for workspace package '{pkg_name}' in {dist_dir}"
            )

        try:
            with tarfile.open(sdist_path, "r:gz") as tf:
                file_bytes_by_rel: dict[str, bytes] = {}
                top_dirs: set[str] = set()
                for member in tf.getmembers():
                    norm_name = member.name.replace("\\", "/").lstrip("./")
                    parts = [p for p in norm_name.split("/") if p]
                    if (
                        not parts
                        or member.name.startswith("/")
                        or ".." in parts
                        or member.issym()
                        or member.islnk()
                    ):
                        raise ConsumptionValidationError(
                            f"Sdist '{sdist_path.name}' contains unsafe archive member '{member.name}'"
                        )
                    top_dirs.add(parts[0])
                    if member.isfile():
                        if len(parts) < 2:
                            raise ConsumptionValidationError(
                                f"Sdist '{sdist_path.name}' places file '{member.name}' outside top-level directory"
                            )
                        extracted = tf.extractfile(member)
                        if extracted is None:
                            raise ConsumptionValidationError(
                                f"Sdist '{sdist_path.name}' member '{member.name}' could not be extracted"
                            )
                        rel_path = "/".join(parts[1:])
                        file_bytes_by_rel[rel_path] = extracted.read()
        except ConsumptionValidationError:
            raise
        except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
            raise ConsumptionValidationError(
                f"Corrupt or unreadable sdist archive '{sdist_path.name}': {exc}"
            ) from exc

        if len(top_dirs) != 1 or not file_bytes_by_rel:
            raise ConsumptionValidationError(
                f"Sdist '{sdist_path.name}' must contain a single non-empty top-level directory (found {sorted(top_dirs)})"
            )

        required_non_empty = (
            "PKG-INFO",
            "LICENSE",
            "CHANGELOG.md",
            "README.md",
            "pyproject.toml",
        )
        for req_rel in required_non_empty:
            if req_rel not in file_bytes_by_rel:
                raise ConsumptionValidationError(
                    f"Sdist '{sdist_path.name}' is missing required file '{req_rel}'"
                )
            if not file_bytes_by_rel[req_rel].strip():
                raise ConsumptionValidationError(
                    f"Sdist '{sdist_path.name}' contains an empty '{req_rel}' file"
                )

        py_typed_rel = f"src/{import_pkg}/py.typed"
        if py_typed_rel not in file_bytes_by_rel:
            raise ConsumptionValidationError(
                f"Sdist '{sdist_path.name}' is missing PEP 561 marker '{py_typed_rel}'"
            )

        for synced_doc in ("LICENSE", "CHANGELOG.md", "README.md"):
            disk_file = pkg_path / synced_doc
            if disk_file.is_file() and file_bytes_by_rel[synced_doc] != disk_file.read_bytes():
                raise ConsumptionValidationError(
                    f"Sdist '{sdist_path.name}' file '{synced_doc}' does not match {disk_file}"
                )

        msg = email.message_from_bytes(file_bytes_by_rel["PKG-INFO"])
        content_type = (msg.get("Description-Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "text/markdown":
            raise ConsumptionValidationError(
                f"Sdist '{sdist_path.name}' expected Description-Content-Type 'text/markdown', "
                f"got {msg.get('Description-Content-Type')!r}"
            )
        payload = msg.get_payload()
        if not isinstance(payload, str) or not payload.strip():
            raise ConsumptionValidationError(
                f"Sdist '{sdist_path.name}' has an empty PKG-INFO description body"
            )

        license_val = (msg.get("License-Expression") or msg.get("License") or "").strip()
        if license_val != "MIT":
            raise ConsumptionValidationError(
                f"Sdist '{sdist_path.name}' expected License-Expression 'MIT', got {license_val!r}"
            )

        license_files = [v.strip() for v in msg.get_all("License-File", []) if v.strip()]
        if "LICENSE" not in license_files:
            raise ConsumptionValidationError(
                f"Sdist '{sdist_path.name}' expected 'License-File: LICENSE' in PKG-INFO, "
                f"got {license_files!r}"
            )

        if require_classifiers:
            classifiers = set(msg.get_all("Classifier", []))
            missing_classifiers = [
                c for c in (*REQUIRED_PYTHON_CLASSIFIERS, TYPED_CLASSIFIER) if c not in classifiers
            ]
            if missing_classifiers:
                raise ConsumptionValidationError(
                    f"Sdist '{sdist_path.name}' PKG-INFO is missing classifiers: {missing_classifiers}"
                )

        print(f"Verified sdist archive contents and PKG-INFO for {sdist_path.name}.")


def verify_sdist_offline_wheel_rebuilds(
    dist_dir: Path,
    packages_dir: Path = PACKAGES_DIR,
    *,
    require_classifiers: bool = True,
) -> None:
    """Rebuild wheels offline via PEP 517 from all workspace sdists inside an OS temp dir."""
    uv_bin = _resolve_uv()
    discovered = discover_workspace_packages(packages_dir)
    print("\n>>> Validating Offline PEP 517 Wheel Rebuilds from Sdist Archives")

    clean_env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "MYPYPATH"}}

    with tempfile.TemporaryDirectory(prefix="imednet-sdist-rebuilds-") as tmpdir:
        tmp_root = Path(tmpdir)
        staged_sdists_dir = tmp_root / "sdists"
        rebuilt_wheels_dir = tmp_root / "rebuilt_wheels"
        staged_sdists_dir.mkdir(parents=True, exist_ok=True)
        rebuilt_wheels_dir.mkdir(parents=True, exist_ok=True)

        for _, pkg_name in discovered:
            sdist_path = _find_sdist_for_package(dist_dir, pkg_name)
            if sdist_path is None:
                raise ConsumptionValidationError(
                    f"Missing built sdist (.tar.gz) for offline PEP 517 rebuild of '{pkg_name}' in {dist_dir}"
                )

            staged_sdist = staged_sdists_dir / sdist_path.name
            shutil.copy2(sdist_path, staged_sdist)
            try:
                run(
                    [
                        uv_bin,
                        "build",
                        "--wheel",
                        "--offline",
                        "--no-create-gitignore",
                        "--out-dir",
                        str(rebuilt_wheels_dir),
                        str(staged_sdist),
                    ],
                    cwd=tmpdir,
                    env=clean_env,
                )
            except subprocess.CalledProcessError as exc:
                output = (exc.stdout or "") + (exc.stderr or "")
                raise ConsumptionValidationError(
                    f"Offline PEP 517 wheel rebuild failed for '{sdist_path.name}':\n{output}"
                ) from exc

        try:
            verify_wheel_metadata(
                rebuilt_wheels_dir,
                packages_dir=packages_dir,
                require_classifiers=require_classifiers,
                require_license_file=True,
            )
        except ValueError as exc:
            raise ConsumptionValidationError(
                f"Rebuilt wheel metadata validation failed: {exc}"
            ) from exc

        if require_classifiers:
            verify_wheel_py_typed(rebuilt_wheels_dir, packages_dir=packages_dir)


def _install_into_venv(
    uv_bin: str,
    python_bin: Path,
    dist_dir: Path,
    constraints_file: Path,
    locked_constraints_file: Path | None,
    install_specs: Sequence[str],
    tmpdir: str,
    *,
    extra_pip_args: Sequence[str] = (),
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
        *extra_pip_args,
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


def verify_sdist_clean_venv_install(
    dist_dir: Path,
    *,
    packages_dir: Path = PACKAGES_DIR,
    uv_lock_path: Path = UV_LOCK_PATH,
) -> None:
    """Install all workspace ``.tar.gz`` archives with ``--no-binary`` into a clean venv and smoke-test."""
    uv_bin = _resolve_uv()
    discovered = discover_workspace_import_packages(packages_dir)
    print("\n>>> Validating Clean-Venv Sdist Installation (--no-binary for workspace packages)")

    with tempfile.TemporaryDirectory(prefix="imednet-sdist-venv-") as tmpdir:
        tmp_root = Path(tmpdir)
        venv_dir = tmp_root / "venv"
        sdist_only_dir = tmp_root / "sdists"
        sdist_only_dir.mkdir(parents=True, exist_ok=True)

        run([uv_bin, "venv", "--python", sys.executable, str(venv_dir)], cwd=tmpdir)

        if os.name == "nt":
            python_bin = venv_dir / "Scripts" / "python.exe"
            imednet_bin = venv_dir / "Scripts" / "imednet.exe"
        else:
            python_bin = venv_dir / "bin" / "python"
            imednet_bin = venv_dir / "bin" / "imednet"

        workspace_sdists = _resolve_workspace_sdists(dist_dir, packages_dir)
        missing_sdists = [
            pkg_name for _, pkg_name, _ in discovered if pkg_name not in workspace_sdists
        ]
        if missing_sdists:
            raise ConsumptionValidationError(
                f"Missing built sdists (.tar.gz) for clean-venv sdist install: {missing_sdists}"
            )

        staged_sdists: dict[str, Path] = {}
        has_imednet_script = False
        expected_entry_points: list[tuple[str, str, str]] = []
        for pkg_path, pkg_name, _ in discovered:
            src_sdist = workspace_sdists[pkg_name]
            dst_sdist = sdist_only_dir / src_sdist.name
            shutil.copy2(src_sdist, dst_sdist)
            staged_sdists[pkg_name] = dst_sdist

            manifest_data = tomllib.loads((pkg_path / "pyproject.toml").read_text(encoding="utf-8"))
            project_tbl = manifest_data.get("project", {})
            if "imednet" in project_tbl.get("scripts", {}):
                has_imednet_script = True
            for group_name, group_entries in project_tbl.get("entry-points", {}).items():
                if isinstance(group_entries, dict):
                    for ep_name, ep_value in group_entries.items():
                        expected_entry_points.append((group_name, ep_name, str(ep_value)))

        constraints_file = tmp_root / "workspace-sdist-constraints.txt"
        constraints_file.write_text(
            "\n".join(
                f"{pkg_name} @ {sdist_path.resolve().as_uri()}"
                for pkg_name, sdist_path in sorted(staged_sdists.items())
            )
            + "\n",
            encoding="utf-8",
        )
        locked_constraints_file = _write_locked_registry_constraints(
            tmp_root / "locked-constraints.txt",
            set(staged_sdists.keys()),
            uv_lock_path=uv_lock_path,
        )

        no_binary_args: list[str] = []
        install_specs: list[str] = []
        for pkg_name, sdist_path in sorted(staged_sdists.items()):
            no_binary_args.extend(["--no-binary", pkg_name])
            install_specs.append(f"{pkg_name} @ {sdist_path.resolve().as_uri()}")

        try:
            _install_into_venv(
                uv_bin,
                python_bin,
                sdist_only_dir,
                constraints_file,
                locked_constraints_file,
                install_specs,
                tmpdir,
                extra_pip_args=no_binary_args,
            )
        except subprocess.CalledProcessError as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            raise ConsumptionValidationError(
                f"Clean-venv sdist installation failed:\n{output}"
            ) from exc

        clean_env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "MYPYPATH"}}
        dist_import_specs = [(pkg_name, import_pkg) for _, pkg_name, import_pkg in discovered]
        probe_payload = json.dumps(
            {
                "venv_root": str(venv_dir.resolve()),
                "packages": dist_import_specs,
                "entry_points": expected_entry_points,
            }
        )
        probe_file = tmp_root / "sdist_smoke_probe.py"
        probe_file.write_text(
            "import importlib\n"
            "import importlib.metadata as md\n"
            "import json\n"
            "import pathlib\n\n"
            f"cfg = json.loads({probe_payload!r})\n"
            "venv_root = pathlib.Path(cfg['venv_root']).resolve()\n"
            "bad_origins = []\n"
            "missing_typed = []\n"
            "bad_meta = []\n"
            "bad_direct = []\n"
            "for dist_name, import_pkg in cfg['packages']:\n"
            "    mod = importlib.import_module(import_pkg)\n"
            "    mod_file = pathlib.Path(mod.__file__).resolve()\n"
            "    if not mod_file.is_relative_to(venv_root):\n"
            "        bad_origins.append(import_pkg)\n"
            "    if not (mod_file.parent / 'py.typed').is_file():\n"
            "        missing_typed.append(import_pkg)\n"
            "    dist = md.distribution(dist_name)\n"
            "    lic = (dist.metadata.get('License-Expression') or dist.metadata.get('License') or '').strip()\n"
            "    lic_files = [v.strip() for v in (dist.metadata.get_all('License-File') or []) if v.strip()]\n"
            "    if lic != 'MIT' or 'LICENSE' not in lic_files:\n"
            "        bad_meta.append((dist_name, lic, lic_files))\n"
            "    direct_text = dist.read_text('direct_url.json')\n"
            "    if direct_text:\n"
            "        url = json.loads(direct_text).get('url', '')\n"
            "        if not url.endswith('.tar.gz'):\n"
            "            bad_direct.append((dist_name, url))\n"
            "assert not bad_origins, f'Sdist modules resolved outside clean venv: {bad_origins}'\n"
            "assert not missing_typed, f'Installed sdists missing py.typed: {missing_typed}'\n"
            "assert not bad_meta, f'Installed sdists missing MIT/License-File metadata: {bad_meta}'\n"
            "assert not bad_direct, f'Installed workspace packages did not originate from .tar.gz: {bad_direct}'\n"
            "for group_name, ep_name, ep_value in cfg['entry_points']:\n"
            "    matches = [ep for ep in md.entry_points(group=group_name) if ep.name == ep_name and ep.value == ep_value]\n"
            "    assert len(matches) == 1 and matches[0].load() is not None, f'Failed loading entry point {group_name}:{ep_name}'\n",
            encoding="utf-8",
        )
        try:
            run([str(python_bin), str(probe_file)], cwd=tmpdir, env=clean_env)
            if has_imednet_script:
                run([str(imednet_bin), "--help"], cwd=tmpdir, env=clean_env)
        except subprocess.CalledProcessError as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            raise ConsumptionValidationError(
                f"Clean-venv sdist smoke check failed:\n{output}"
            ) from exc


def _resolve_host_mypy() -> Path | None:
    exe_name = "mypy.exe" if os.name == "nt" else "mypy"
    sibling = Path(sys.executable).with_name(exe_name)
    if sibling.is_file():
        return sibling
    which_mypy = shutil.which("mypy")
    return Path(which_mypy) if which_mypy is not None else None


def verify_wheel_mypy_strict(
    dist_dir: Path,
    *,
    packages_dir: Path = PACKAGES_DIR,
    uv_lock_path: Path = UV_LOCK_PATH,
    consumer_source: str = _CONSUMER_TYPING_CHECK,
) -> None:
    """Install all workspace wheels into an isolated venv and run ``mypy --strict``."""
    uv_bin = _resolve_uv()
    discovered = discover_workspace_import_packages(packages_dir)
    print("\n>>> Validating PEP 561 Consumer Typing (mypy --strict)")

    with tempfile.TemporaryDirectory() as tmpdir:
        venv_dir = Path(tmpdir) / "venv"
        run([uv_bin, "venv", "--python", sys.executable, str(venv_dir)], cwd=tmpdir)

        if os.name == "nt":
            python_bin = venv_dir / "Scripts" / "python.exe"
            venv_mypy = venv_dir / "Scripts" / "mypy.exe"
        else:
            python_bin = venv_dir / "bin" / "python"
            venv_mypy = venv_dir / "bin" / "mypy"

        workspace_wheels = _resolve_workspace_wheels(dist_dir, packages_dir)
        missing_wheels = [
            pkg_name for _, pkg_name, _ in discovered if pkg_name not in workspace_wheels
        ]
        if missing_wheels:
            raise ConsumptionValidationError(
                f"Missing built wheels for mypy strict validation: {missing_wheels}"
            )

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

        install_specs = [
            f"{pkg_name} @ {wheel_path.resolve().as_uri()}"
            for pkg_name, wheel_path in sorted(workspace_wheels.items())
        ]
        mypy_bin = _resolve_host_mypy()
        if mypy_bin is None:
            install_specs.append("mypy")
            mypy_bin = venv_mypy

        _install_into_venv(
            uv_bin,
            python_bin,
            dist_dir,
            constraints_file,
            locked_constraints_file,
            install_specs,
            tmpdir,
        )

        clean_env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "MYPYPATH"}}

        import_names = [import_pkg for _, _, import_pkg in discovered]
        origin_probe = (
            "import importlib, pathlib; "
            f"venv_root = pathlib.Path({str(venv_dir)!r}).resolve(); "
            f"pkgs = {import_names!r}; "
            "mods = [importlib.import_module(p) for p in pkgs]; "
            "bad = [m.__name__ for m in mods if not pathlib.Path(m.__file__).resolve().is_relative_to(venv_root)]; "
            "assert not bad, f'Packages did not resolve from installed wheels: {bad}'; "
            "missing_typed = [m.__name__ for m in mods if not (pathlib.Path(m.__file__).parent / 'py.typed').is_file()]; "
            "assert not missing_typed, f'Installed wheels missing py.typed on disk: {missing_typed}'"
        )
        run([str(python_bin), "-c", origin_probe], cwd=tmpdir, env=clean_env)

        consumer_file = Path(tmpdir) / "consumer_typing.py"
        consumer_file.write_text(consumer_source, encoding="utf-8")

        try:
            res = run(
                [
                    str(mypy_bin),
                    "--strict",
                    "--python-executable",
                    str(python_bin),
                    str(consumer_file),
                ],
                cwd=tmpdir,
                env=clean_env,
            )
        except subprocess.CalledProcessError as e:
            output = (e.stdout or "") + (e.stderr or "")
            raise ConsumptionValidationError(
                f"mypy --strict failed against installed workspace wheels:\n{output}"
            ) from e

        print(res.stdout.strip())


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
        description="Validate clean-environment wheel, sdist, and extras consumption."
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="Optional directory containing prebuilt workspace wheels (.whl) and sdists (.tar.gz) to validate.",
    )
    parser.add_argument(
        "--wheel-dir",
        type=Path,
        default=None,
        help="Compatible alias for a directory containing prebuilt workspace wheels (missing sdists are built).",
    )
    args = parser.parse_args(argv)
    if (
        args.dist_dir is not None
        and args.wheel_dir is not None
        and args.dist_dir.resolve() != args.wheel_dir.resolve()
    ):
        parser.error("Cannot specify different paths for --dist-dir and --wheel-dir.")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    scenarios = build_scenarios()
    source_dir = args.dist_dir if args.dist_dir is not None else args.wheel_dir
    allow_build_missing = args.dist_dir is None
    try:
        verify_manifest_extra_coverage(scenarios, PACKAGES_DIR)
        with tempfile.TemporaryDirectory(prefix="imednet-consumption-dists-") as tmp_dists:
            dist_dir = Path(tmp_dists)
            prepare_distributions(
                dist_dir,
                source_dir,
                allow_build_missing=allow_build_missing,
            )
            verify_wheel_metadata(dist_dir, PACKAGES_DIR)
            verify_wheel_py_typed(dist_dir, PACKAGES_DIR)
            verify_sdist_archives(dist_dir, PACKAGES_DIR)
            verify_sdist_offline_wheel_rebuilds(dist_dir, PACKAGES_DIR)
            verify_sdist_clean_venv_install(
                dist_dir,
                packages_dir=PACKAGES_DIR,
                uv_lock_path=UV_LOCK_PATH,
            )
            verify_wheel_mypy_strict(
                dist_dir,
                packages_dir=PACKAGES_DIR,
                uv_lock_path=UV_LOCK_PATH,
            )

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
