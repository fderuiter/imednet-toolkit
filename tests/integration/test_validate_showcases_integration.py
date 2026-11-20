"""Integration tests for staged browser wheel builds and showcase simulation subprocesses."""

from __future__ import annotations

from pathlib import Path

from scripts.validate_showcases import (
    execute_showcases_in_simulation,
    main,
    stage_wheels,
    validate_staged_wheels,
)


def test_validate_staged_wheels_workspace_integration(tmp_path: Path) -> None:
    """Verify that staged browser wheels pass validation and simulated Pyodide import."""
    repo_root = Path(__file__).resolve().parents[2]
    wheels_dir = tmp_path / "wheels"

    staged = stage_wheels(repo_root, wheels_dir)
    staged_names = [w.name for w in staged]
    assert any(n.startswith("imednet-") for n in staged_names)
    assert any(n.startswith("imednet_workflows-") for n in staged_names)
    assert any(n.startswith("imednet_plugins_sinks-") for n in staged_names)
    assert not any(n.startswith("apache_airflow_providers_imednet-") for n in staged_names)
    assert not any(n.startswith("imednet_tui-") for n in staged_names)

    errors = validate_staged_wheels(wheels_dir)
    assert not errors, f"Expected all three staged browser wheels to pass validation, got: {errors}"


def test_execute_showcases_in_simulation_passes_and_isolates_cwd(tmp_path: Path) -> None:
    """Verify simulation execution sets IMEDNET_SIMULATION_MODE=true and isolates generated files."""
    repo_root = tmp_path / "repo"
    examples_dir = repo_root / "examples"
    docs_dir = repo_root / "docs"
    examples_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)

    showcase = examples_dir / "sim_ok.py"
    showcase.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from imednet import ImednetSDK\n\n"
        "assert os.environ.get('IMEDNET_SIMULATION_MODE') == 'true'\n"
        "Path('generated_output.txt').write_text('ok', encoding='utf-8')\n"
        "with ImednetSDK() as sdk:\n"
        "    studies = list(sdk.studies.list())\n"
        "    assert len(studies) >= 1\n",
        encoding="utf-8",
    )
    (docs_dir / "index.rst").write_text(
        ".. literalinclude:: /../examples/sim_ok.py\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--examples-dir",
            str(examples_dir),
            "--docs-dir",
            str(docs_dir),
            "--root-dir",
            str(repo_root),
        ]
    )
    assert exit_code == 0
    assert not (examples_dir / "generated_output.txt").exists()


def test_execute_showcases_in_simulation_catches_nonzero_and_error_output(
    tmp_path: Path,
) -> None:
    """Verify simulation execution catches non-zero exit codes, error lines, and JSON error logs."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)

    (examples_dir / "fail_exit.py").write_text(
        "import sys\nsys.exit(2)\n",
        encoding="utf-8",
    )
    (examples_dir / "fail_printed_error.py").write_text(
        "print('Error: simulated failure without non-zero exit')\n",
        encoding="utf-8",
    )
    (examples_dir / "fail_json_log.py").write_text(
        'import sys\nprint(\'{"level": "ERROR", "message": "boom"}\', file=sys.stderr)\n',
        encoding="utf-8",
    )

    errors = execute_showcases_in_simulation(examples_dir)
    assert len(errors) == 3
    assert any("fail_exit.py" in e and "non-zero code 2" in e for e in errors)
    assert any("fail_printed_error.py" in e and "Error message emitted" in e for e in errors)
    assert any("fail_json_log.py" in e and "Error log emitted" in e for e in errors)


def test_build_form_payload_no_args_shows_help_and_tui_guidance() -> None:
    """Verify examples/build_form_payload.py prints Form Builder usage and directs browsing to imednet tui."""
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "examples" / "build_form_payload.py"
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    combined_output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "TUI mode has been removed" not in combined_output
    assert "Form Builder" in combined_output
    assert "imednet tui" in combined_output
    assert "--preset" in combined_output
    assert "--form-id" in combined_output
