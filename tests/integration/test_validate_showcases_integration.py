"""Integration tests for staged browser wheel builds and showcase simulation subprocesses."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from scripts.validate_showcases import (
    execute_showcases_in_simulation,
    main,
    stage_wheels,
    validate_airflow_dags_in_simulation,
    validate_provider_readme_blocks,
    validate_showcase_ast_and_imports,
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


def test_execute_showcases_in_simulation_rejects_silent_noop_and_name_error_workflows(
    tmp_path: Path,
) -> None:
    """Verify workflow showcases fail simulation checks on empty stdout, skipped guards, or NameError."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)

    (examples_dir / "workflow_silent_noop.py").write_text(
        "from imednet import ImednetSDK\nsdk = ImednetSDK(simulation_mode=True)\nsdk.close()\n",
        encoding="utf-8",
    )
    (examples_dir / "workflow_skipped.py").write_text(
        "print('Skipping execution: set IMEDNET_STUDY_KEY')\n",
        encoding="utf-8",
    )
    (examples_dir / "workflow_name_error.py").write_text(
        "print(undefined_workflow_binding)\n",
        encoding="utf-8",
    )

    errors = execute_showcases_in_simulation(examples_dir)
    assert len(errors) == 3
    assert any("workflow_silent_noop.py" in e and "empty stdout" in e for e in errors)
    assert any("workflow_skipped.py" in e and "skipped execution" in e for e in errors)
    assert any("workflow_name_error.py" in e and "NameError" in e for e in errors)


def test_repo_workflow_bulk_submission_uat_and_data_dictionary_execute_in_simulation(
    tmp_path: Path,
) -> None:
    """Verify workflow_bulk_submission.py, workflow_uat.py, and data_dictionary.py run actively in simulation."""
    repo_root = Path(__file__).resolve().parents[2]
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)
    for script_name in ("workflow_bulk_submission.py", "workflow_uat.py", "data_dictionary.py"):
        shutil.copy2(repo_root / "examples" / script_name, examples_dir / script_name)

    ast_errors = validate_showcase_ast_and_imports(examples_dir, repo_root)
    assert not ast_errors, f"Expected zero AST errors, got: {ast_errors}"

    exec_errors = execute_showcases_in_simulation(examples_dir, repo_root)
    assert not exec_errors, f"Expected zero simulation execution errors, got: {exec_errors}"


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


def test_validate_staged_wheels_child_process_environment_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify in actual child process execution that coverage environment variables are absent."""
    wheels_dir = tmp_path / "wheels"
    wheels_dir.mkdir(parents=True)
    wheel = wheels_dir / "imednet-1.1.0-py3-none-any.whl"

    wheel_init = (
        "import os\n"
        "leaked = [\n"
        "    k for k in os.environ\n"
        "    if k.upper().startswith(('COV_', 'COVERAGE_'))\n"
        "    or 'cov_core' in k.lower()\n"
        "    or 'coverage' in k.lower()\n"
        "]\n"
        "if leaked:\n"
        "    raise RuntimeError(f'Child process inherited coverage variables: {leaked}')\n"
        "class ImednetSDK:\n"
        "    pass\n"
    )

    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("imednet/__init__.py", wheel_init)
        zf.writestr("imednet/sdk.py", "")
        zf.writestr("imednet/config.py", "")

    monkeypatch.setenv("COV_CORE_SOURCE", "/some/source")
    monkeypatch.setenv("COV_CORE_DATAFILE", "/tmp/data")
    monkeypatch.setenv("COVERAGE_FILE", "/tmp/.coverage")
    monkeypatch.setenv("COVERAGE_PROCESS_START", "/tmp/.coveragerc")
    monkeypatch.setenv("cov_core_test", "1")
    monkeypatch.setenv("coverage_test", "1")

    errors = validate_staged_wheels(wheels_dir, expected_packages=("imednet",))
    assert not errors, f"Subprocess validation failed due to leaked variables: {errors}"


def test_execute_showcases_in_simulation_rejects_empty_output(tmp_path: Path) -> None:
    """Verify non-Airflow showcase scripts that produce zero output fail simulation execution."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)
    (examples_dir / "silent_noop.py").write_text("x = 1 + 1\n", encoding="utf-8")

    errors = execute_showcases_in_simulation(examples_dir)
    assert len(errors) == 1
    assert "Produced empty output in simulation mode" in errors[0]


def test_get_study_structure_populates_variable_label_in_csv(tmp_path: Path) -> None:
    """Verify examples/get_study_structure.py reads Variable.label into study_structure.csv."""
    import csv
    import os
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "examples" / "get_study_structure.py"
    env = os.environ.copy()
    env["IMEDNET_SIMULATION_MODE"] = "true"
    env["IMEDNET_OUTPUT_DIR"] = str(tmp_path)

    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Saved study structure to:" in proc.stdout
    csv_path = tmp_path / "study_structure.csv"
    assert csv_path.is_file()
    with csv_path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    weight_rows = [r for r in rows if r["variableName"] == "V_WEIGHT"]
    assert weight_rows and weight_rows[0]["variableLabel"] == "Weight"


def test_validate_airflow_dags_in_simulation_passes_and_catches_regressions(
    tmp_path: Path,
) -> None:
    """Verify Airflow 3.x DAG import and CSV export checks pass and detect colliding index kwargs."""
    if importlib.util.find_spec("airflow") is None:
        pytest.skip("airflow is not installed")
    repo_root = Path(__file__).resolve().parents[2]

    errors = validate_airflow_dags_in_simulation(repo_root / "examples" / "airflow", repo_root)
    assert not errors, f"Expected repository Airflow DAG to pass validation, got: {errors}"

    bad_airflow_dir = tmp_path / "airflow"
    bad_airflow_dir.mkdir(parents=True)
    ref_dag = (repo_root / "examples" / "airflow" / "airflow_multi_study_pipeline.py").read_text(
        encoding="utf-8"
    )
    bad_dag = ref_dag.replace('"export_kwargs": {}', '"export_kwargs": {"index": False}')
    (bad_airflow_dir / "bad_multi_study.py").write_text(bad_dag, encoding="utf-8")

    bad_errors = validate_airflow_dags_in_simulation(bad_airflow_dir, repo_root)
    assert any("ExportBatchError" in e or "Mapped CSV export" in e for e in bad_errors), (
        f"Expected ExportBatchError on colliding index=False, got: {bad_errors}"
    )


def test_validate_provider_readme_blocks_passes_and_catches_regressions(tmp_path: Path) -> None:
    """Verify provider README Python blocks run without MagicMock and catch contract drift."""
    if importlib.util.find_spec("airflow") is None:
        pytest.skip("airflow is not installed")
    repo_root = Path(__file__).resolve().parents[2]
    readme_path = repo_root / "packages" / "providers-airflow" / "README.md"

    errors = validate_provider_readme_blocks(readme_path, repo_root)
    assert not errors, f"Expected provider README blocks to pass, got: {errors}"

    bad_readme = tmp_path / "README.md"
    bad_readme.write_text(
        "Install `apache-airflow-providers-imednet[amazon]`\n```python\nprint('hi')\n```\n",
        encoding="utf-8",
    )
    assert any("[amazon]" in e for e in validate_provider_readme_blocks(bad_readme, repo_root))

    bad_sensor_readme = tmp_path / "README_sensor.md"
    bad_sensor_readme.write_text(
        "```python\n"
        "from apache_airflow_providers_imednet.sensors import ImednetJobSensor\n"
        "ImednetJobSensor(task_id='w', job_id='123')\n"
        "```\n",
        encoding="utf-8",
    )
    sensor_errs = validate_provider_readme_blocks(bad_sensor_readme, repo_root)
    assert any("Execution failed" in e for e in sensor_errs)
