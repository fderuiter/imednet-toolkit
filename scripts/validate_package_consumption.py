import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
PACKAGES_DIR = ROOT / "packages"


def run(cmd, **kwargs):
    print(f"Executing: {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _resolve_uv() -> str:
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        raise FileNotFoundError("Required tool 'uv' was not found on PATH.")
    return uv_bin


def build_wheels(dist_dir: Path):
    uv_bin = _resolve_uv()
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True)

    for pkg_path in sorted(PACKAGES_DIR.iterdir()):
        if pkg_path.is_dir() and (pkg_path / "pyproject.toml").exists():
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


def validate_wheel_metadata(dist_dir: Path, packages_dir: Path = PACKAGES_DIR) -> None:
    """Verify every built wheel in dist_dir declares License, text/markdown, and a non-empty METADATA body."""
    all_wheels = sorted(dist_dir.glob("*.whl"))
    if not all_wheels:
        raise ValueError(f"No built wheels (.whl) found in {dist_dir}")

    if packages_dir.is_dir():
        for pkg_path in sorted(packages_dir.iterdir()):
            manifest = pkg_path / "pyproject.toml"
            if pkg_path.is_dir() and manifest.is_file():
                pkg_name = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]["name"]
                normalized_pkg = pkg_name.replace("-", "_")
                if not any(w.name.startswith(normalized_pkg + "-") for w in all_wheels):
                    raise ValueError(
                        f"Missing built wheel for workspace package '{pkg_name}' in {dist_dir}"
                    )

    parser = Parser()
    for wheel_path in all_wheels:
        with zipfile.ZipFile(wheel_path, "r") as zf:
            metadata_members = [
                member for member in zf.namelist() if member.endswith(".dist-info/METADATA")
            ]
            if len(metadata_members) != 1:
                raise ValueError(
                    f"{wheel_path.name}: Expected exactly 1 .dist-info/METADATA file, found {metadata_members}"
                )
            raw_metadata = zf.read(metadata_members[0]).decode("utf-8")

        msg = parser.parsestr(raw_metadata)
        license_val = (msg.get("License") or msg.get("License-Expression") or "").strip()
        if license_val != "MIT":
            raise ValueError(
                f"{wheel_path.name}: Expected License or License-Expression 'MIT' in METADATA, got {license_val!r}"
            )

        content_type = (msg.get("Description-Content-Type") or "").strip()
        if content_type.lower().split(";", 1)[0].strip() != "text/markdown":
            raise ValueError(
                f"{wheel_path.name}: Expected Description-Content-Type 'text/markdown', got {content_type!r}"
            )

        payload = msg.get_payload()
        body = payload.strip() if isinstance(payload, str) else ""
        if not body:
            raise ValueError(f"{wheel_path.name}: Wheel METADATA long description body is empty")
        print(
            f"Verified wheel METADATA for {wheel_path.name} "
            f"(license={license_val}, content_type={content_type}, body_chars={len(body)})."
        )


def validate_scenario(name, install_items, smoke_checks, dist_dir: Path):
    """smoke_checks is a list of dicts:
    {
        "cmd": "command to run",
        "expect_fail": False,
        "contains": "expected output substring",
        "not_contains": "unexpected output substring"
    }
    """
    uv_bin = _resolve_uv()
    print(f"\n>>> Validating Scenario: {name}")
    with tempfile.TemporaryDirectory() as tmpdir:
        venv_dir = Path(tmpdir) / "venv"
        print(f"Creating venv at {venv_dir}")
        run([uv_bin, "venv", "--python", sys.executable, str(venv_dir)], cwd=tmpdir)

        # Get venv paths
        if os.name == "nt":
            python_bin = venv_dir / "Scripts" / "python.exe"
            imednet_bin = venv_dir / "Scripts" / "imednet.exe"
        else:
            python_bin = venv_dir / "bin" / "python"
            imednet_bin = venv_dir / "bin" / "imednet"

        # Find wheels in dist_dir and constrain all workspace packages to freshly built wheels
        all_wheels = sorted(dist_dir.glob("*.whl"))
        workspace_wheels: dict[str, Path] = {}
        for pkg_path in sorted(PACKAGES_DIR.iterdir()):
            manifest = pkg_path / "pyproject.toml"
            if pkg_path.is_dir() and manifest.exists():
                pkg_name = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]["name"]
                normalized_pkg = pkg_name.replace("-", "_")
                matching = [w for w in all_wheels if w.name.startswith(normalized_pkg + "-")]
                if matching:
                    workspace_wheels[pkg_name] = max(matching)

        constraints_file = Path(tmpdir) / "workspace-constraints.txt"
        constraints_file.write_text(
            "\n".join(
                f"{pkg_name} @ {wheel_path.resolve().as_uri()}"
                for pkg_name, wheel_path in sorted(workspace_wheels.items())
            )
            + "\n",
            encoding="utf-8",
        )

        # Prepare install command using uv pip install (no pip required in venv)
        install_args = [
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

        for item in install_items:
            base_pkg = item.split("[")[0]
            extra = item[len(base_pkg) :] if "[" in item else ""
            wheel_path = workspace_wheels.get(base_pkg)
            if wheel_path is not None:
                install_args.append(f"{base_pkg}{extra} @ {wheel_path.resolve().as_uri()}")
            else:
                install_args.append(item)

        print(f"Installing packages: {install_items}")
        try:
            try:
                run([*install_args, "--offline"], cwd=tmpdir)
            except subprocess.CalledProcessError:
                run(install_args, cwd=tmpdir)
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
                    full_cmd = [str(imednet_bin), *cmd.split(" ")[1:]]
                    res = run(full_cmd)
                else:
                    print(f"Running custom command: {cmd} (expect_fail={expect_fail})")
                    res = run([str(python_bin), "-c", cmd])

                output = res.stdout + res.stderr
                if expect_fail:
                    print(f"Command '{cmd}' succeeded but was expected to fail.")
                    print(output)
                    sys.exit(1)

                if contains and contains not in output:
                    print(f"Output of '{cmd}' did not contain expected string: {contains}")
                    print(f"Output: {output}")
                    sys.exit(1)

                if not_contains and not_contains in output:
                    print(f"Output of '{cmd}' contained unexpected string: {not_contains}")
                    print(f"Output: {output}")
                    sys.exit(1)

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
                    sys.exit(1)


def main():
    dist_dir = ROOT / "dist_validation"
    try:
        build_wheels(dist_dir)
        validate_wheel_metadata(dist_dir)

        scenarios = [
            {
                "name": "core-minimal",
                "install": ["imednet"],
                "smoke": [
                    {"cmd": "import imednet"},
                    {"cmd": "import snowflake.connector", "expect_fail": True},
                    {
                        "cmd": "from imednet.models.subjects import Subject; assert 'subject_id' in Subject.model_fields and 'subject_key' in Subject.model_fields",
                    },
                    {
                        "cmd": "from imednet.models.records import Record; assert 'record_id' in Record.model_fields",
                    },
                    {
                        "cmd": "imednet --help",
                        "expect_fail": False,
                        "contains": "pip install 'imednet[cli]'",
                    },
                ],
            },
            {
                "name": "core-cli-no-plugins",
                "install": ["imednet[cli]"],
                "smoke": [
                    {"cmd": "imednet --help", "contains": "workflows"},
                    {
                        "cmd": "imednet workflows extract-records",
                        "expect_fail": True,
                        "contains": "The workflows plugin is not installed.",
                    },
                ],
            },
            {
                "name": "workflows-isolated",
                "install": ["imednet-workflows"],
                "smoke": [
                    {"cmd": "import imednet_workflows"},
                    {"cmd": "import imednet"},
                    {"cmd": "imednet workflows extract-records --help", "contains": "usage:"},
                ],
            },
            {
                "name": "airflow-provider",
                "install": ["apache-airflow", "apache-airflow-providers-imednet"],
                "smoke": [
                    {"cmd": "import apache_airflow_providers_imednet"},
                    {"cmd": "from apache_airflow_providers_imednet.hooks import ImednetHook"},
                    {"cmd": "import apache_airflow_providers_imednet.operators.export"},
                ],
            },
            {
                "name": "sinks-mongodb",
                "install": ["imednet-plugins-sinks[mongodb]"],
                "smoke": [{"cmd": "import pymongo"}],
            },
            {
                "name": "sinks-snowflake",
                "install": ["imednet-plugins-sinks[snowflake]"],
                "smoke": [
                    {"cmd": "import snowflake.connector"},
                    {"cmd": "import pyarrow"},
                    {
                        "cmd": "from imednet.spi.sink_base import _require_optional_dep; assert _require_optional_dep('snowflake.connector', 'snowflake').__version__",
                    },
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
                        "cmd": "from imednet.sdk import ImednetSDK; sdk = ImednetSDK(api_key='k', security_key='s', simulation_mode=True); assert sdk.sinks.SnowflakeExportSink is not None",
                    },
                ],
            },
            {
                "name": "core-export-extra",
                "install": ["imednet[export]"],
                "smoke": [
                    {"cmd": "import pandas"},
                    {"cmd": "import imednet_workflows"},
                    {"cmd": "imednet export csv --help"},
                ],
            },
            {
                "name": "core-tui",
                "install": ["imednet[tui]"],
                "smoke": [
                    {"cmd": "import imednet_tui"},
                    {"cmd": "imednet tui --help", "contains": "usage:"},
                ],
            },
        ]

        for s in scenarios:
            try:
                validate_scenario(s["name"], s["install"], s["smoke"], dist_dir)
            except Exception as e:
                print(f"FAILED scenario {s['name']}: {e}")
                sys.exit(1)

        print("\nALL SCENARIOS PASSED")

    finally:
        if dist_dir.exists():
            shutil.rmtree(dist_dir)


if __name__ == "__main__":
    main()
