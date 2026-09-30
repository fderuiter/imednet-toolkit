import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
PACKAGES_DIR = ROOT / "packages"


def run(cmd, **kwargs):
    print(f"Executing: {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def build_wheels(dist_dir: Path):
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir()

    # Pre-install build in the current environment to ensure we can build
    subprocess.run([sys.executable, "-m", "pip", "install", "build"], check=True)

    for pkg_path in PACKAGES_DIR.iterdir():
        if pkg_path.is_dir() and (pkg_path / "pyproject.toml").exists():
            print(f"Building wheel for {pkg_path.name}...")
            run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir), str(pkg_path)]
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
    print(f"\n>>> Validating Scenario: {name}")
    with tempfile.TemporaryDirectory() as tmpdir:
        venv_dir = Path(tmpdir) / "venv"
        print(f"Creating venv at {venv_dir}")
        venv.create(venv_dir, with_pip=True)

        # Get venv paths
        if os.name == "nt":
            python_bin = venv_dir / "Scripts" / "python.exe"
            pip_bin = venv_dir / "Scripts" / "pip.exe"
            imednet_bin = venv_dir / "Scripts" / "imednet.exe"
        else:
            python_bin = venv_dir / "bin" / "python"
            pip_bin = venv_dir / "bin" / "pip"
            imednet_bin = venv_dir / "bin" / "imednet"

        # Find wheels in dist_dir
        all_wheels = list(dist_dir.glob("*.whl"))

        # Prepare install command
        install_args = [str(pip_bin), "install", "--find-links", str(dist_dir)]

        for item in install_items:
            base_pkg = item.split("[")[0]
            extra = item[len(base_pkg) :] if "[" in item else ""
            normalized_base = base_pkg.replace("-", "_")
            matching_wheels = [w for w in all_wheels if w.name.startswith(normalized_base + "-")]

            if matching_wheels:
                wheel_path = max(matching_wheels)
                install_args.append(f"{wheel_path.name}{extra}")
            else:
                install_args.append(item)

        print(f"Installing packages: {install_items}")
        try:
            run(install_args, cwd=str(dist_dir))
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

        scenarios = [
            {
                "name": "core-minimal",
                "install": ["imednet"],
                "smoke": [
                    {"cmd": "import imednet"},
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
                "name": "core-export-extra",
                "install": ["imednet[export]"],
                "smoke": [
                    {"cmd": "import pandas"},
                    {"cmd": "import imednet_workflows"},
                    {"cmd": "imednet export csv --help"},
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
