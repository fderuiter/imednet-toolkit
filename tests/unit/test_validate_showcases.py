"""Unit tests for automated showcase verification gate."""

from __future__ import annotations

from pathlib import Path

from scripts.validate_showcases import (
    main,
    validate_documentation_literal_includes,
    validate_showcase_ast_and_imports,
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
