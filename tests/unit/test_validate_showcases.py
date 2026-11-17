"""Unit tests for automated showcase verification gate."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest
from scripts.validate_showcases import (
    extract_literalinclude_slice,
    main,
    stage_wheels,
    validate_documentation_literal_includes,
    validate_python_slice,
    validate_showcase_ast_and_imports,
    validate_staged_wheels,
)


def test_repo_showcases_and_docs_pass() -> None:
    """Verify that current repository showcases and documentation literal includes pass validation."""
    repo_root = Path(__file__).resolve().parents[2]
    examples_dir = repo_root / "examples"
    docs_dir = repo_root / "docs"

    showcase_errors = validate_showcase_ast_and_imports(examples_dir, repo_root)
    assert not showcase_errors, f"Expected no showcase errors, got: {showcase_errors}"

    doc_errors = validate_documentation_literal_includes(docs_dir)
    assert not doc_errors, f"Expected no documentation literal include errors, got: {doc_errors}"


def test_syntax_error_detection(tmp_path: Path) -> None:
    """Verify that syntax errors in showcase files are detected."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)
    broken_file = examples_dir / "broken_syntax.py"
    broken_file.write_text("def invalid_syntax(\n    pass\n", encoding="utf-8")

    errors = validate_showcase_ast_and_imports(examples_dir)
    assert len(errors) == 1
    assert "Syntax error" in errors[0]
    assert "broken_syntax.py" in errors[0]


def test_missing_module_reference_detection(tmp_path: Path) -> None:
    """Verify that missing required module imports are detected."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)
    bad_import_file = examples_dir / "missing_module.py"
    bad_import_file.write_text("import nonexistent_package_alpha_12345\n", encoding="utf-8")

    errors = validate_showcase_ast_and_imports(examples_dir)
    assert len(errors) == 1
    assert "Missing required module reference 'nonexistent_package_alpha_12345'" in errors[0]


def test_optional_integration_dependencies_handled_gracefully(tmp_path: Path) -> None:
    """Verify that optional integration dependencies do not trigger validation errors."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)
    optional_showcase = examples_dir / "optional_demo.py"
    optional_showcase.write_text(
        "import airflow\n"
        "from airflow import DAG\n"
        "from dotenv import load_dotenv\n"
        "import snowflake\n",
        encoding="utf-8",
    )

    errors = validate_showcase_ast_and_imports(examples_dir)
    assert not errors, f"Expected optional dependencies to be handled gracefully, got: {errors}"


def test_missing_documentation_literal_include_detection(tmp_path: Path) -> None:
    """Verify that missing files referenced in literalinclude directives are detected."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    doc_file = docs_dir / "index.rst"
    doc_file.write_text(
        "Showcase Section\n"
        "----------------\n"
        ".. literalinclude:: /../examples/missing_example.py\n"
        "   :language: python\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert len(errors) == 1
    assert "Referenced literalinclude target not found on disk" in errors[0]
    assert "missing_example.py" in errors[0]


def test_valid_documentation_literal_includes(tmp_path: Path) -> None:
    """Verify that valid literalinclude references pass without errors."""
    repo_root = tmp_path / "repo"
    docs_dir = repo_root / "docs"
    examples_dir = repo_root / "examples"
    docs_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    example_target = examples_dir / "demo.py"
    example_target.write_text("print('hello')\n", encoding="utf-8")

    doc_file = docs_dir / "tutorial.rst"
    doc_file.write_text(
        ".. literalinclude:: /../examples/demo.py\n   :language: python\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert not errors, f"Expected no errors for existing target, got: {errors}"


def test_main_exit_code_zero_on_success(tmp_path: Path) -> None:
    """Verify main returns 0 when showcase files and docs pass."""
    repo_root = tmp_path / "repo"
    examples_dir = repo_root / "examples"
    docs_dir = repo_root / "docs"
    examples_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)

    sample_py = examples_dir / "sample.py"
    sample_py.write_text("import sys\nprint(sys.version)\n", encoding="utf-8")

    sample_rst = docs_dir / "index.rst"
    sample_rst.write_text(
        ".. literalinclude:: /../examples/sample.py\n",
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


def test_main_exit_code_nonzero_on_failure(tmp_path: Path, capsys) -> None:
    """Verify main returns 1 and outputs error details when validation fails."""
    repo_root = tmp_path / "repo"
    examples_dir = repo_root / "examples"
    docs_dir = repo_root / "docs"
    examples_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)

    broken_py = examples_dir / "broken.py"
    broken_py.write_text("def broken(\n", encoding="utf-8")

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
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "Showcase verification failed" in captured.err
    assert "broken.py" in captured.err


def test_validate_staged_wheels_missing_directory(tmp_path: Path) -> None:
    """Verify that missing wheels directory returns error."""
    missing_dir = tmp_path / "nonexistent_wheels"
    errors = validate_staged_wheels(missing_dir)
    assert len(errors) == 1
    assert "Staged wheels directory not found" in errors[0]


def test_validate_staged_wheels_empty_directory(tmp_path: Path) -> None:
    """Verify that empty wheels directory returns error."""
    empty_dir = tmp_path / "wheels"
    empty_dir.mkdir(parents=True)
    errors = validate_staged_wheels(empty_dir)
    assert len(errors) == 1
    assert "No staged wheel artifacts" in errors[0]


def test_validate_staged_wheels_missing_expected_package(tmp_path: Path) -> None:
    """Verify error when expected package wheel is missing."""
    wheels_dir = tmp_path / "wheels"
    wheels_dir.mkdir(parents=True)
    other_wheel = wheels_dir / "other_package-1.0.0-py3-none-any.whl"
    other_wheel.write_text("dummy", encoding="utf-8")

    errors = validate_staged_wheels(wheels_dir, expected_packages=("imednet",))
    assert any("Missing expected staged wheel artifact for package 'imednet'" in e for e in errors)


def test_validate_staged_wheels_missing_required_module(tmp_path: Path) -> None:
    """Verify error when wheel archive does not contain required module files."""
    wheels_dir = tmp_path / "wheels"
    wheels_dir.mkdir(parents=True)
    incomplete_wheel = wheels_dir / "imednet-1.1.0-py3-none-any.whl"

    with zipfile.ZipFile(incomplete_wheel, "w") as zf:
        zf.writestr("imednet/__init__.py", "print('hello')\n")
        # Missing imednet/sdk.py and imednet/config.py

    errors = validate_staged_wheels(wheels_dir, expected_packages=("imednet",))
    assert any("missing required module entry 'imednet/sdk.py'" in e for e in errors)


def test_validate_staged_wheels_workspace_integration(tmp_path: Path) -> None:
    """Verify that staged wheels in repo pass validation and simulated Pyodide import."""
    repo_root = Path(__file__).resolve().parents[2]
    wheels_dir = repo_root / "docs" / "_static" / "wheels"

    # Stage wheels if not already staged
    if not list(wheels_dir.glob("*.whl")):
        stage_wheels(repo_root, wheels_dir)

    errors = validate_staged_wheels(wheels_dir, expected_packages=("imednet",))
    assert not errors, f"Expected staged wheels to pass validation, got: {errors}"


def test_literalinclude_slice_missing_start_marker_detection(tmp_path: Path) -> None:
    """Verify that missing start-after pattern is detected."""
    docs_dir = tmp_path / "docs"
    examples_dir = tmp_path / "examples"
    docs_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    example_target = examples_dir / "quick_start.py"
    example_target.write_text("def main():\n    print('hello')\n", encoding="utf-8")

    doc_file = docs_dir / "quick_start.rst"
    doc_file.write_text(
        ".. literalinclude:: /../examples/quick_start.py\n"
        "   :language: python\n"
        "   :start-after: # [missing_start]\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert len(errors) == 1
    assert "start-after pattern '# [missing_start]' not found" in errors[0]


def test_literalinclude_slice_missing_end_marker_detection(tmp_path: Path) -> None:
    """Verify that missing end-before pattern is detected."""
    docs_dir = tmp_path / "docs"
    examples_dir = tmp_path / "examples"
    docs_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    example_target = examples_dir / "quick_start.py"
    example_target.write_text(
        "# [start_marker]\ndef main():\n    print('hello')\n",
        encoding="utf-8",
    )

    doc_file = docs_dir / "quick_start.rst"
    doc_file.write_text(
        ".. literalinclude:: /../examples/quick_start.py\n"
        "   :language: python\n"
        "   :start-after: # [start_marker]\n"
        "   :end-before: # [missing_end]\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert len(errors) == 1
    assert "end-before pattern '# [missing_end]' not found" in errors[0]


def test_literalinclude_slice_syntax_error_detection(tmp_path: Path) -> None:
    """Verify that syntax errors within extracted slices are detected."""
    docs_dir = tmp_path / "docs"
    examples_dir = tmp_path / "examples"
    docs_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    example_target = examples_dir / "broken_slice.py"
    example_target.write_text(
        "# [start_code]\ndef broken_function(\n# [end_code]\n",
        encoding="utf-8",
    )

    doc_file = docs_dir / "index.rst"
    doc_file.write_text(
        ".. literalinclude:: /../examples/broken_slice.py\n"
        "   :language: python\n"
        "   :start-after: # [start_code]\n"
        "   :end-before: # [end_code]\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert len(errors) == 1
    assert "has syntax error" in errors[0]


def test_literalinclude_slice_zero_call_nodes_detection(tmp_path: Path) -> None:
    """Verify that extracted slices without executable call nodes are flagged."""
    docs_dir = tmp_path / "docs"
    examples_dir = tmp_path / "examples"
    docs_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    example_target = examples_dir / "no_calls.py"
    example_target.write_text(
        "# [start_code]\nfrom __future__ import annotations\nimport sys\n# [end_code]\n",
        encoding="utf-8",
    )

    doc_file = docs_dir / "index.rst"
    doc_file.write_text(
        ".. literalinclude:: /../examples/no_calls.py\n"
        "   :language: python\n"
        "   :start-after: # [start_code]\n"
        "   :end-before: # [end_code]\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert any("contains zero executable call nodes" in e for e in errors)


def test_literalinclude_slice_missing_function_definition_detection(tmp_path: Path) -> None:
    """Verify that sliced showcases missing executable function definitions are flagged."""
    docs_dir = tmp_path / "docs"
    examples_dir = tmp_path / "examples"
    docs_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    example_target = examples_dir / "no_func.py"
    example_target.write_text(
        "# [start_code]\nprint('only a call, no function def')\n# [end_code]\n",
        encoding="utf-8",
    )

    doc_file = docs_dir / "index.rst"
    doc_file.write_text(
        ".. literalinclude:: /../examples/no_func.py\n"
        "   :language: python\n"
        "   :start-after: # [start_code]\n"
        "   :end-before: # [end_code]\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert any("contains zero executable function definitions" in e for e in errors)


def test_literalinclude_valid_slice_passes(tmp_path: Path) -> None:
    """Verify that a properly delimited slice containing function defs and calls passes."""
    docs_dir = tmp_path / "docs"
    examples_dir = tmp_path / "examples"
    docs_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    example_target = examples_dir / "valid_showcase.py"
    example_target.write_text(
        '"""Showcase docstring."""\n'
        "\n"
        "# [start_demo]\n"
        "import os\n"
        "\n"
        "def main() -> None:\n"
        "    print(os.getcwd())\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
        "# [end_demo]\n",
        encoding="utf-8",
    )

    doc_file = docs_dir / "showcase.rst"
    doc_file.write_text(
        ".. literalinclude:: /../examples/valid_showcase.py\n"
        "   :language: python\n"
        "   :start-after: # [start_demo]\n"
        "   :end-before: # [end_demo]\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert not errors, f"Expected no errors for valid slice, got: {errors}"


def test_extract_literalinclude_slice_options() -> None:
    """Verify slicing with start-at, end-at, lines, and empty slice detection."""
    content = "line1\n# start\nline3\nline4\n# end\nline6\n"

    # start-at and end-at
    sliced, is_sliced, errs = extract_literalinclude_slice(
        content, {"start-at": "# start", "end-at": "# end"}
    )
    assert not errs
    assert is_sliced is True
    assert sliced == "# start\nline3\nline4\n# end\n"

    # lines spec
    sliced_lines, is_sliced_lines, lines_errs = extract_literalinclude_slice(
        content, {"lines": "1, 3-4"}
    )
    assert not lines_errs
    assert is_sliced_lines is True
    assert sliced_lines == "line1\nline3\nline4\n"

    # empty slice detection
    empty_content = "line1\n# start\n\n# end\nline6\n"
    _, _, empty_errs = extract_literalinclude_slice(
        empty_content, {"start-after": "# start", "end-before": "# end"}
    )
    assert any("Extracted literalinclude slice is empty" in e for e in empty_errs)


def test_validate_python_slice_helper() -> None:
    """Verify validate_python_slice returns expected errors for invalid snippets."""
    # Syntax error
    errs = validate_python_slice("def broken(", "target.py", is_sliced=True)
    assert any("has syntax error" in e for e in errs)

    # Missing call nodes
    errs = validate_python_slice("x = 1\ny = 2\n", "target.py", is_sliced=False)
    assert any("contains zero executable call nodes" in e for e in errs)

    # Missing function definition when sliced
    errs = validate_python_slice("print('call only')\n", "target.py", is_sliced=True)
    assert any("contains zero executable function definitions" in e for e in errs)


def test_validate_staged_wheels_scrubs_coverage_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that coverage environment variables are excluded from subprocess execution."""
    wheels_dir = tmp_path / "wheels"
    wheels_dir.mkdir(parents=True)
    wheel = wheels_dir / "imednet-1.1.0-py3-none-any.whl"

    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("imednet/__init__.py", "class ImednetSDK:\n    pass\n")
        zf.writestr("imednet/sdk.py", "")
        zf.writestr("imednet/config.py", "")

    monkeypatch.setenv("COV_CORE_SOURCE", "/some/source")
    monkeypatch.setenv("COV_CORE_CONFIG", "/some/config")
    monkeypatch.setenv("COVERAGE_PROCESS_START", "/some/.coveragerc")
    monkeypatch.setenv("cov_core_datafile", "/some/file")
    monkeypatch.setenv("coverage_custom_token", "123")
    monkeypatch.setenv("SAFE_WORKSPACE_VAR", "preserved_value")

    captured_env: dict[str, str] | None = None
    original_run = subprocess.run

    def mock_subprocess_run(cmd, *args, **kwargs):
        nonlocal captured_env
        captured_env = kwargs.get("env")
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

    errors = validate_staged_wheels(wheels_dir, expected_packages=("imednet",))
    assert not errors, f"Unexpected validation errors: {errors}"
    assert captured_env is not None
    assert captured_env.get("SAFE_WORKSPACE_VAR") == "preserved_value"

    leakage = [
        k
        for k in captured_env
        if k.upper().startswith(("COV_", "COVERAGE_"))
        or "cov_core" in k.lower()
        or "coverage" in k.lower()
    ]
    assert not leakage, f"Found leaked coverage environment variables in subprocess env: {leakage}"


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
