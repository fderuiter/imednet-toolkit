"""Unit tests for automated showcase verification gate."""

from __future__ import annotations

import importlib.util
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest
from scripts.validate_showcases import (
    execute_showcases_in_simulation,
    extract_literalinclude_slice,
    main,
    stage_wheels,
    validate_airflow_dags_in_simulation,
    validate_canonical_showcase_terminology,
    validate_documentation_literal_includes,
    validate_landing_showcase,
    validate_provider_readme_blocks,
    validate_python_slice,
    validate_python_syntax,
    validate_quick_start_ast_structure,
    validate_showcase_ast_and_imports,
    validate_showcase_mock_data_schemas,
    validate_staged_wheels,
)
from sphinx.config import Config
from sphinx.directives.code import LiteralIncludeReader


def _extract_with_sphinx(
    tmp_path: Path, filename: str, content: str, options: dict[str, Any]
) -> tuple[str, bool, str]:
    """Helper to extract slice using Sphinx's LiteralIncludeReader for differential testing."""
    target_file = tmp_path / filename
    target_file.write_text(content, encoding="utf-8")
    cfg = Config({}, {})
    try:
        reader = LiteralIncludeReader(str(target_file), options, cfg)
        text, _ = reader.read()
        return text, True, ""
    except Exception as exc:
        return "", False, f"{type(exc).__name__}: {exc}"


def test_repo_showcases_and_docs_pass() -> None:
    """Verify that current repository showcases and documentation literal includes pass validation."""
    repo_root = Path(__file__).resolve().parents[2]
    examples_dir = repo_root / "examples"
    docs_dir = repo_root / "docs"

    showcase_errors = validate_showcase_ast_and_imports(examples_dir, repo_root)
    assert not showcase_errors, f"Expected no showcase errors, got: {showcase_errors}"

    doc_errors = validate_documentation_literal_includes(docs_dir)
    assert not doc_errors, f"Expected no documentation literal include errors, got: {doc_errors}"

    landing_errors = validate_landing_showcase(docs_dir, repo_root)
    assert not landing_errors, f"Expected no landing showcase errors, got: {landing_errors}"


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


def test_validate_staged_wheels_detects_missing_plugin_wheels(tmp_path: Path) -> None:
    """Verify default validate_staged_wheels requires imednet, imednet_workflows, and imednet_plugins_sinks."""
    wheels_dir = tmp_path / "wheels"
    wheels_dir.mkdir(parents=True)
    core_only = wheels_dir / "imednet-1.1.0-py3-none-any.whl"
    with zipfile.ZipFile(core_only, "w") as zf:
        zf.writestr("imednet/__init__.py", "class ImednetSDK:\n    pass\n")
        zf.writestr("imednet/sdk.py", "")
        zf.writestr("imednet/config.py", "")

    errors = validate_staged_wheels(wheels_dir)
    assert any("imednet_workflows" in e for e in errors)
    assert any("imednet_plugins_sinks" in e for e in errors)


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
    """Verify that quick-start slices without executable call nodes are flagged."""
    docs_dir = tmp_path / "docs"
    examples_dir = tmp_path / "examples"
    docs_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    example_target = examples_dir / "no_calls.py"
    example_target.write_text(
        "# [start_code]\nfrom __future__ import annotations\nimport sys\n# [end_code]\n",
        encoding="utf-8",
    )

    doc_file = docs_dir / "quick_start.rst"
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
    """Verify that sliced quick-start showcases missing function definitions are flagged."""
    docs_dir = tmp_path / "docs"
    examples_dir = tmp_path / "examples"
    docs_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    example_target = examples_dir / "no_func.py"
    example_target.write_text(
        "# [start_code]\nprint('only a call, no function def')\n# [end_code]\n",
        encoding="utf-8",
    )

    doc_file = docs_dir / "quick_start.rst"
    doc_file.write_text(
        ".. literalinclude:: /../examples/no_func.py\n"
        "   :language: python\n"
        "   :start-after: # [start_code]\n"
        "   :end-before: # [end_code]\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert any("contains zero executable function definitions" in e for e in errors)


def test_literalinclude_general_doc_slice_syntax_only_passes(tmp_path: Path) -> None:
    """Verify that non-quick-start docs pass syntax validation without requiring function defs or calls."""
    docs_dir = tmp_path / "docs"
    examples_dir = tmp_path / "examples"
    docs_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    example_target = examples_dir / "config_sample.py"
    example_target.write_text(
        "# [start_cfg]\nAPI_TIMEOUT = 30\nDEFAULT_RETRY = 3\n# [end_cfg]\n",
        encoding="utf-8",
    )

    doc_file = docs_dir / "config_reference.rst"
    doc_file.write_text(
        ".. literalinclude:: /../examples/config_sample.py\n"
        "   :language: python\n"
        "   :start-after: # [start_cfg]\n"
        "   :end-before: # [end_cfg]\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert not errors, (
        f"Expected non-quick-start doc slice to pass syntax-only check, got: {errors}"
    )


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
    """Verify validate_python_slice distinguishes syntax validity from quick-start expectations."""
    # Syntax error is always flagged
    errs = validate_python_slice("def broken(", "target.py", is_sliced=True)
    assert any("has syntax error" in e for e in errs)

    # Pure variables pass when is_quick_start=False
    general_errs = validate_python_slice(
        "x = 1\ny = 2\n", "target.py", is_sliced=False, is_quick_start=False
    )
    assert not general_errs

    # Pure variables fail call check when is_quick_start=True
    qs_errs = validate_python_slice(
        "x = 1\ny = 2\n", "target.py", is_sliced=False, is_quick_start=True
    )
    assert any("contains zero executable call nodes" in e for e in qs_errs)

    # Sliced calls without function def fail when is_quick_start=True
    func_errs = validate_python_slice(
        "print('call only')\n", "target.py", is_sliced=True, is_quick_start=True
    )
    assert any("contains zero executable function definitions" in e for e in func_errs)

    # Direct validation of syntax and quick-start helpers
    tree, syntax_errs = validate_python_syntax("x = 1\n", "target.py")
    assert not syntax_errs
    assert tree is not None

    qs_struct_errs = validate_quick_start_ast_structure(tree, "target.py", is_sliced=False)
    assert any("contains zero executable call nodes" in e for e in qs_struct_errs)


SAMPLE_MULTI_LINE = (
    '"""Showcase sample file."""\n'
    "import os\n"
    "\n"
    "# [start_demo]\n"
    "def run_sample() -> None:\n"
    "    print('sample executed')\n"
    "# [end_demo]\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    run_sample()\n"
)


def test_sphinx_parity_start_after_end_before(tmp_path: Path) -> None:
    """Compare start-after and end-before extraction with Sphinx LiteralIncludeReader."""
    options = {"start-after": "# [start_demo]", "end-before": "# [end_demo]"}
    sphinx_out, sphinx_ok, sphinx_err = _extract_with_sphinx(
        tmp_path, "sample.py", SAMPLE_MULTI_LINE, options
    )
    assert sphinx_ok, f"Sphinx extraction failed: {sphinx_err}"

    custom_out, is_sliced, custom_errs = extract_literalinclude_slice(SAMPLE_MULTI_LINE, options)
    assert not custom_errs
    assert is_sliced is True
    assert custom_out == sphinx_out


def test_sphinx_parity_start_at_end_at(tmp_path: Path) -> None:
    """Compare start-at and end-at extraction with Sphinx LiteralIncludeReader."""
    options = {"start-at": "# [start_demo]", "end-at": "# [end_demo]"}
    sphinx_out, sphinx_ok, sphinx_err = _extract_with_sphinx(
        tmp_path, "sample.py", SAMPLE_MULTI_LINE, options
    )
    assert sphinx_ok, f"Sphinx extraction failed: {sphinx_err}"

    custom_out, is_sliced, custom_errs = extract_literalinclude_slice(SAMPLE_MULTI_LINE, options)
    assert not custom_errs
    assert is_sliced is True
    assert custom_out == sphinx_out


def test_sphinx_parity_line_ranges(tmp_path: Path) -> None:
    """Compare closed and open line ranges with Sphinx LiteralIncludeReader."""
    test_specs = [
        "2-4",
        "-3",
        "4-",
        "1, 3-4, 6-",
    ]
    for linespec in test_specs:
        options = {"lines": linespec}
        clean_name = f"sample_{linespec.replace(',', '_').replace(' ', '_').replace('-', '_')}.py"
        sphinx_out, sphinx_ok, sphinx_err = _extract_with_sphinx(
            tmp_path, clean_name, SAMPLE_MULTI_LINE, options
        )
        assert sphinx_ok, f"Sphinx extraction failed for '{linespec}': {sphinx_err}"

        custom_out, is_sliced, custom_errs = extract_literalinclude_slice(
            SAMPLE_MULTI_LINE, options
        )
        assert not custom_errs, f"Custom extractor returned errors for '{linespec}': {custom_errs}"
        assert is_sliced is True
        assert custom_out == sphinx_out, f"Mismatch for linespec '{linespec}'"


def test_sphinx_parity_supported_combinations(tmp_path: Path) -> None:
    """Compare combined slice options (markers + lines + dedent) with Sphinx."""
    combo_options = [
        {"start-after": "# [start_demo]", "end-before": "# [end_demo]", "lines": "1-2"},
        {"start-at": "# [start_demo]", "end-at": "# [end_demo]", "lines": "2-3"},
        {"start-after": "# [start_demo]", "end-before": "# [end_demo]", "dedent": "4"},
        {"start-after": "# [start_demo]", "end-before": "# [end_demo]", "dedent": ""},
    ]
    for idx, options in enumerate(combo_options):
        sphinx_opts = dict(options)
        if "dedent" in sphinx_opts:
            val = sphinx_opts["dedent"]
            sphinx_opts["dedent"] = int(val) if val else None
        sphinx_out, sphinx_ok, sphinx_err = _extract_with_sphinx(
            tmp_path, f"combo_{idx}.py", SAMPLE_MULTI_LINE, sphinx_opts
        )
        assert sphinx_ok, f"Sphinx extraction failed for {options}: {sphinx_err}"

        custom_out, is_sliced, custom_errs = extract_literalinclude_slice(
            SAMPLE_MULTI_LINE, options
        )
        assert not custom_errs, f"Custom errors for {options}: {custom_errs}"
        assert is_sliced is True
        assert custom_out == sphinx_out, f"Mismatch for {options}"


def test_sphinx_parity_dedent_handling(tmp_path: Path) -> None:
    """Compare indentation and dedent handling against Sphinx."""
    indented_text = "def outer():\n    def inner():\n        value = 100\n        return value\n"
    # Explicit numeric dedent
    sphinx_out, sphinx_ok, _ = _extract_with_sphinx(
        tmp_path, "indented.py", indented_text, {"lines": "2-4", "dedent": 4}
    )
    assert sphinx_ok
    custom_out, _, errs = extract_literalinclude_slice(
        indented_text, {"lines": "2-4", "dedent": "4"}
    )
    assert not errs
    assert custom_out == sphinx_out

    # Auto common-whitespace dedent
    sphinx_auto, sphinx_auto_ok, _ = _extract_with_sphinx(
        tmp_path, "indented_auto.py", indented_text, {"lines": "2-4", "dedent": None}
    )
    assert sphinx_auto_ok
    custom_auto, _, auto_errs = extract_literalinclude_slice(
        indented_text, {"lines": "2-4", "dedent": ""}
    )
    assert not auto_errs
    assert custom_auto == sphinx_auto


def test_sphinx_parity_repeated_markers(tmp_path: Path) -> None:
    """Compare repeated marker selection against Sphinx (first occurrences selected)."""
    repeated_content = (
        "# [marker]\nblock1 = True\n# [stop]\ngap = 1\n# [marker]\nblock2 = False\n# [stop]\n"
    )
    options = {"start-after": "# [marker]", "end-before": "# [stop]"}
    sphinx_out, sphinx_ok, _ = _extract_with_sphinx(
        tmp_path, "repeated.py", repeated_content, options
    )
    assert sphinx_ok

    custom_out, _, errs = extract_literalinclude_slice(repeated_content, options)
    assert not errs
    assert custom_out == sphinx_out
    assert custom_out == "block1 = True\n"


def test_sphinx_parity_adjacent_markers_error(tmp_path: Path) -> None:
    """Verify that adjacent markers produce actionable error matching Sphinx rejection."""
    adjacent_content = "header = 1\n# [marker]\n# [stop]\ntail = 2\n"
    options = {"start-after": "# [marker]", "end-before": "# [stop]"}
    _, sphinx_ok, sphinx_err = _extract_with_sphinx(
        tmp_path, "adjacent.py", adjacent_content, options
    )
    assert not sphinx_ok
    assert "end-before pattern not found" in sphinx_err

    _, _, errs = extract_literalinclude_slice(adjacent_content, options)
    assert any("end-before pattern '# [stop]' not found" in e for e in errs)


def test_sphinx_parity_missing_markers_diagnostics(tmp_path: Path) -> None:
    """Verify that missing markers produce actionable diagnostics without uncaught exceptions."""
    content = "line1\nline2\n"
    # start-after missing
    _, sphinx_ok1, _ = _extract_with_sphinx(
        tmp_path, "m1.py", content, {"start-after": "# [missing]"}
    )
    assert not sphinx_ok1
    _, _, errs1 = extract_literalinclude_slice(content, {"start-after": "# [missing]"})
    assert any("start-after pattern '# [missing]' not found" in e for e in errs1)

    # start-at missing
    _, sphinx_ok2, _ = _extract_with_sphinx(tmp_path, "m2.py", content, {"start-at": "# [missing]"})
    assert not sphinx_ok2
    _, _, errs2 = extract_literalinclude_slice(content, {"start-at": "# [missing]"})
    assert any("start-at pattern '# [missing]' not found" in e for e in errs2)

    # end-before missing
    content_with_start = "# [start]\nline2\n"
    _, sphinx_ok3, _ = _extract_with_sphinx(
        tmp_path,
        "m3.py",
        content_with_start,
        {"start-after": "# [start]", "end-before": "# [missing]"},
    )
    assert not sphinx_ok3
    _, _, errs3 = extract_literalinclude_slice(
        content_with_start, {"start-after": "# [start]", "end-before": "# [missing]"}
    )
    assert any("end-before pattern '# [missing]' not found" in e for e in errs3)

    # end-at missing
    _, sphinx_ok4, _ = _extract_with_sphinx(
        tmp_path, "m4.py", content_with_start, {"start-after": "# [start]", "end-at": "# [missing]"}
    )
    assert not sphinx_ok4
    _, _, errs4 = extract_literalinclude_slice(
        content_with_start, {"start-after": "# [start]", "end-at": "# [missing]"}
    )
    assert any("end-at pattern '# [missing]' not found" in e for e in errs4)


def test_sphinx_parity_out_of_range_lines_diagnostics(tmp_path: Path) -> None:
    """Verify that out-of-range lines specifications produce actionable diagnostics."""
    content = "line1\nline2\nline3\n"
    options = {"lines": "50-100"}
    _, sphinx_ok, sphinx_err = _extract_with_sphinx(tmp_path, "oor.py", content, options)
    assert not sphinx_ok
    assert "no lines pulled" in sphinx_err.lower()

    _, _, errs = extract_literalinclude_slice(content, options)
    assert any("pulled no lines" in e for e in errs)


def test_actionable_diagnostics_on_invalid_options() -> None:
    """Verify that mutually exclusive options and malformed parameters yield actionable diagnostics."""
    content = "line1\nline2\nline3\n"

    # Conflicting start options
    _, _, errs_start = extract_literalinclude_slice(
        content, {"start-after": "line1", "start-at": "line1"}
    )
    assert any("Cannot use both 'start-after' and 'start-at'" in e for e in errs_start)

    # Conflicting end options
    _, _, errs_end = extract_literalinclude_slice(
        content, {"end-before": "line2", "end-at": "line2"}
    )
    assert any("Cannot use both 'end-before' and 'end-at'" in e for e in errs_end)

    # Inverted line range
    _, _, errs_range = extract_literalinclude_slice(content, {"lines": "5-2"})
    assert any("start exceeds end" in e for e in errs_range)

    # Malformed non-numeric line range
    _, _, errs_malformed = extract_literalinclude_slice(content, {"lines": "abc"})
    assert any("Invalid lines specification 'abc'" in e for e in errs_malformed)

    # Empty range '-'
    _, _, errs_dash = extract_literalinclude_slice(content, {"lines": "-"})
    assert any("empty range '-'" in e for e in errs_dash)

    # Invalid non-numeric dedent
    _, _, errs_dedent = extract_literalinclude_slice(content, {"dedent": "abc"})
    assert any("Invalid dedent specification 'abc'" in e for e in errs_dedent)

    # Negative dedent
    _, _, errs_neg_dedent = extract_literalinclude_slice(content, {"dedent": "-2"})
    assert any("must be non-negative integer" in e for e in errs_neg_dedent)


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


def test_validate_staged_wheels_verifies_wheel_metadata_against_pyodide_config(
    tmp_path: Path,
) -> None:
    """Verify validate_staged_wheels catches Requires-Dist packages missing from pyodide_config even if installed in dev."""
    wheels_dir = tmp_path / "wheels"
    docs_dir = tmp_path / "docs"
    wheels_dir.mkdir(parents=True)
    docs_dir.mkdir(parents=True)

    (docs_dir / "conf.py").write_text(
        "pyodide_config = {\n"
        '    "pyodide_packages": ["pydantic", "jsonschema"],\n'
        '    "requirements": ["httpx", "tenacity", "defusedxml"],\n'
        '    "lazy_pyodide_packages": ["duckdb", "sqlalchemy"],\n'
        '    "lazy_requirements": ["openpyxl"],\n'
        "}\n",
        encoding="utf-8",
    )

    wheel = wheels_dir / "imednet-1.1.0-py3-none-any.whl"
    # 'pytest' is installed in host [dev] Python, but absent from pyodide_config;
    # 'snowflake-connector-python' is guarded by extra == 'snowflake' and must be ignored.
    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: imednet\n"
        "Version: 1.1.0\n"
        "Requires-Dist: pydantic>=2.11.3\n"
        "Requires-Dist: jsonschema>=4.20.0\n"
        "Requires-Dist: pytest>=8.0.0\n"
        "Requires-Dist: snowflake-connector-python>=3.0.0; extra == 'snowflake'\n\n"
    )
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("imednet/__init__.py", "class ImednetSDK:\n    pass\n")
        zf.writestr("imednet/sdk.py", "")
        zf.writestr("imednet/config.py", "")
        zf.writestr("imednet-1.1.0.dist-info/METADATA", metadata)

    errors = validate_staged_wheels(
        wheels_dir,
        expected_packages=("imednet",),
        docs_dir=docs_dir,
    )
    assert len(errors) == 1, f"Expected 1 missing browser dependency error, got: {errors}"
    assert "requires 'pytest'" in errors[0]
    assert "snowflake-connector-python" not in errors[0]


def test_missing_workspace_symbol_in_showcase_detected(tmp_path: Path) -> None:
    """Verify showcase validation fails when an example imports a removed or nonexistent workspace symbol."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)
    bad_symbol_file = examples_dir / "stale_sink_import.py"
    bad_symbol_file.write_text(
        "from imednet.integrations import SnowflakeSinkConfig, export_to_snowflake\n",
        encoding="utf-8",
    )

    errors = validate_showcase_ast_and_imports(examples_dir)
    assert len(errors) == 2
    assert any(
        "Missing symbol 'SnowflakeSinkConfig' in module 'imednet.integrations'" in e for e in errors
    )
    assert any(
        "Missing symbol 'export_to_snowflake' in module 'imednet.integrations'" in e for e in errors
    )


def test_missing_workspace_symbol_in_literalinclude_slice_detected(tmp_path: Path) -> None:
    """Verify documentation validation fails when a literalinclude slice imports a nonexistent workspace symbol."""
    docs_dir = tmp_path / "docs"
    examples_dir = tmp_path / "examples"
    docs_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    snippet_py = examples_dir / "slice_target.py"
    snippet_py.write_text(
        "# [start]\nfrom imednet_sinks import NonExistentSinkSymbol\n# [end]\n",
        encoding="utf-8",
    )
    doc_file = docs_dir / "guide.rst"
    doc_file.write_text(
        ".. literalinclude:: /../examples/slice_target.py\n"
        "   :language: python\n"
        "   :start-after: # [start]\n"
        "   :end-before: # [end]\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert len(errors) == 1
    assert "Missing symbol 'NonExistentSinkSymbol' in module 'imednet_sinks'" in errors[0]


def test_missing_workspace_symbol_in_inline_rst_snippet_detected(tmp_path: Path) -> None:
    """Verify inline .. testcode:: and .. code-block:: python directives catch missing workspace symbols."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)

    doc_file = docs_dir / "howto.rst"
    doc_file.write_text(
        "Export Guide\n"
        "============\n"
        "\n"
        ".. testcode::\n"
        "\n"
        "   from imednet.integrations import export_to_mongodb\n"
        "\n"
        ".. code-block:: python\n"
        "\n"
        "   from imednet_sinks import RemovedGraphHelper\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert len(errors) == 2
    assert any(
        "Missing symbol 'export_to_mongodb' in module 'imednet.integrations'" in e for e in errors
    )
    assert any("Missing symbol 'RemovedGraphHelper' in module 'imednet_sinks'" in e for e in errors)


def test_symbol_validation_skips_uninstalled_optional_drivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify symbol-level import validation skips uninstalled optional third-party drivers in minimal envs."""
    import sys

    blocked_roots = {"snowflake", "pymongo", "neo4j", "airflow", "duckdb", "pyarrow"}

    class _BlockOptionalFinder:
        def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
            if fullname.split(".", maxsplit=1)[0] in blocked_roots:
                raise ModuleNotFoundError(
                    f"Blocked optional {fullname}", name=fullname.split(".", maxsplit=1)[0]
                )

    for mod_name in list(sys.modules):
        if mod_name.split(".", maxsplit=1)[0] in blocked_roots:
            monkeypatch.delitem(sys.modules, mod_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockOptionalFinder(), *sys.meta_path])

    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)
    showcase = examples_dir / "minimal_env_demo.py"
    showcase.write_text(
        "from airflow import DAG\n"
        "from airflow.sdk import task\n"
        "from snowflake.connector import connect\n"
        "from pymongo import MongoClient\n"
        "from neo4j import GraphDatabase\n"
        "from imednet_sinks import MongoDbSinkConfig, Neo4jSinkConfig, SnowflakeSinkConfig\n",
        encoding="utf-8",
    )

    errors = validate_showcase_ast_and_imports(examples_dir)
    assert not errors, f"Expected optional drivers to be skipped cleanly, got: {errors}"


def test_todo_docstring_rejected_in_showcases(tmp_path: Path) -> None:
    """Verify leftover TODO placeholders in module, class, or function docstrings fail validation."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)
    todo_file = examples_dir / "todo_demo.py"
    todo_file.write_text(
        '"""Module summary with TODO placeholder."""\n\n'
        "def run() -> None:\n"
        '    """Execute TODO action."""\n'
        "    pass\n",
        encoding="utf-8",
    )

    errors = validate_showcase_ast_and_imports(examples_dir)
    assert len(errors) == 2
    assert any("<module>" in err and "TODO" in err for err in errors)
    assert any("'run'" in err and "TODO" in err for err in errors)


def test_execute_showcases_in_simulation_passes_and_isolates_cwd(tmp_path: Path) -> None:
    """Verify simulation execution sets IMEDNET_SIMULATION_MODE=true and isolates generated files."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)
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

    errors = execute_showcases_in_simulation(examples_dir)
    assert not errors, f"Expected simulation execution to pass, got: {errors}"
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


def test_validate_landing_showcase_detects_simulation_and_vocabulary_regressions(
    tmp_path: Path,
) -> None:
    """Verify validate_landing_showcase catches broken SDK calls, unmaterialized iterators, Math.random, and synonyms."""
    repo_root = Path(__file__).resolve().parents[2]
    docs_dir = tmp_path / "docs"
    static_dir = docs_dir / "_static"
    static_dir.mkdir(parents=True)

    # Copy valid showcase.js as baseline and inject targeted regressions
    valid_js = (repo_root / "docs" / "_static" / "showcase.js").read_text(encoding="utf-8")
    broken_js = (
        valid_js.replace(
            'studies = list(sdk.studies.list())',
            'studies = sdk.studies.list(status="ACTIVE")',
        ).replace(
            'study_key="STUDY"',
            'study_key="STUDY-101"',
            1,
        )
        + "\n// Math.random()\n// patient discrepancy\n"
    )
    (static_dir / "showcase.js").write_text(broken_js, encoding="utf-8")

    index_rst = docs_dir / "index.rst"
    index_rst.write_text(
        '<div id="interactive-showcase-root">\n'
        '  <textarea id="ie-code-input"></textarea>\n'
        '  <button id="ie-run-btn">Run</button>\n'
        "  Study Variable Query trial eCRF\n"
        "</div>\n",
        encoding="utf-8",
    )

    errors = validate_landing_showcase(docs_dir, repo_root)
    assert any("Forbidden Math.random()" in e for e in errors)
    assert any("Must target seeded 'STUDY' instead of 'STUDY-101'" in e for e in errors)
    assert any("Offline simulation execution failed" in e for e in errors)
    assert any("Legacy editable sandbox markup '<textarea'" in e for e in errors)
    assert any("Missing required read-only showcase element" in e for e in errors)
    assert any("Disallowed CONTEXT.md synonym 'patient'" in e for e in errors)
    assert any("Disallowed CONTEXT.md synonym 'discrepancy'" in e for e in errors)
    assert any("Disallowed CONTEXT.md synonym 'trial'" in e for e in errors)
    assert any("Disallowed CONTEXT.md synonym 'eCRF'" in e for e in errors)


def test_validate_landing_showcase_detects_unmaterialized_generator(tmp_path: Path) -> None:
    """Verify validate_landing_showcase rejects snippets that leave lazy generators unmaterialized."""
    repo_root = Path(__file__).resolve().parents[2]
    docs_dir = tmp_path / "docs"
    static_dir = docs_dir / "_static"
    static_dir.mkdir(parents=True)

    valid_js = (repo_root / "docs" / "_static" / "showcase.js").read_text(encoding="utf-8")
    lazy_js = valid_js.replace(
        "forms = list(sdk.forms.list(study_key=\"STUDY\"))\\n    for f in forms:\\n        print(f\"{f.form_name} ({f.form_key})\")",
        "forms = sdk.forms.list(study_key=\"STUDY\")",
    )
    (static_dir / "showcase.js").write_text(lazy_js, encoding="utf-8")

    errors = validate_landing_showcase(docs_dir, repo_root)
    assert any("unmaterialized lazy iterator" in e for e in errors), f"Got errors: {errors}"


def test_validate_canonical_showcase_terminology_allows_js_window_globals(tmp_path: Path) -> None:
    """Verify window.location.hash and window.history do not false-positive while domain synonyms fail."""
    js_file = tmp_path / "sample.js"
    js_file.write_text(
        "// Study Query Variable\n"
        "const h = window.location.hash;\n"
        "window.history.replaceState(null, '', '#panel-integration');\n"
        "window.addEventListener('hashchange', () => {});\n",
        encoding="utf-8",
    )
    assert not validate_canonical_showcase_terminology(js_file)

    bad_js_file = tmp_path / "bad.js"
    bad_js_file.write_text(
        "// Study Query\nconst label = 'Clinic window and field count';\n",
        encoding="utf-8",
    )
    errs = validate_canonical_showcase_terminology(bad_js_file)
    assert any("Missing required canonical CONTEXT.md term 'Variable'" in e for e in errs)
    assert any("Disallowed CONTEXT.md synonym 'Clinic'" in e for e in errs)
    assert any("Disallowed CONTEXT.md synonym 'window'" in e for e in errs)
    assert any("Disallowed CONTEXT.md synonym 'field'" in e for e in errs)


def test_validate_showcase_mock_data_schemas_rejects_non_contract_keys(tmp_path: Path) -> None:
    """Verify validate_showcase_mock_data_schemas detects non-contract keys across REST and DataDictionary sections."""
    repo_root = Path(__file__).resolve().parents[2]
    postman_path = repo_root / "imednet.postman_collection.json"
    valid_js = (repo_root / "docs" / "_static" / "showcase.js").read_text(encoding="utf-8")

    drifted_js = (
        valid_js.replace(
            'studyKey: "STUDY",\n                studyId: 1003,',
            'studyKey: "STUDY",\n                protocol: "ONC-01",\n                studyId: 1003,',
            1,
        )
        .replace(
            'formKey: "F_VS",\n                formName: "Vital Signs",\n                formType: "CRF",',
            'formKey: "F_VS",\n                variableCount: 7,\n                formName: "Vital Signs",\n                formType: "CRF",',
            1,
        )
        .replace(
            'formKey: "F_VS",\n                formName: "Vital Signs",\n                formType: "Subject",',
            'formKey: "F_VS",\n                domain: "VS",\n                formName: "Vital Signs",\n                formType: "Subject",',
            1,
        )
        .replace(
            "annotationId: 701,",
            'annotationId: 701,\n                status: "OPEN",',
            1,
        )
        .replace(
            "recordRevisionId: 9001,",
            'recordRevisionId: 9001,\n                auditId: "AUD-8801",',
            1,
        )
    )
    bad_showcase = tmp_path / "showcase.js"
    bad_showcase.write_text(drifted_js, encoding="utf-8")

    errors = validate_showcase_mock_data_schemas(bad_showcase, postman_path)
    assert any("MOCK_DATA.studies[0] contains non-contract keys ['protocol']" in e for e in errors)
    assert any(
        "MOCK_DATA.formsCatalog[0] contains non-contract keys ['variableCount']" in e
        for e in errors
    )
    assert any("MOCK_DATA.forms.VS contains non-contract keys ['domain']" in e for e in errors)
    assert any("MOCK_DATA.queries[0] contains non-contract keys ['status']" in e for e in errors)
    assert any("MOCK_DATA.audits[0] contains non-contract keys ['auditId']" in e for e in errors)


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


def test_docs_conf_does_not_mock_duckdb_export_or_airflow() -> None:
    """Verify Sphinx doctest configuration runs real duckdb.connect, _prepare_export_df, and airflow."""
    repo_root = Path(__file__).resolve().parents[2]
    conf_text = (repo_root / "docs" / "conf.py").read_text(encoding="utf-8")

    assert "_duckdb_connect_patcher" not in conf_text
    assert "_export_patcher" not in conf_text
    assert "sys.modules['airflow']" not in conf_text
    assert 'sys.modules["airflow"]' not in conf_text
    assert "sys.modules['apache_airflow_providers_imednet']" not in conf_text


def test_workflow_showcase_rejects_unassigned_constructor_calls(tmp_path: Path) -> None:
    """Verify workflow showcases fail AST validation when workflow or record-set instances are discarded."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)
    noop_uat = examples_dir / "workflow_broken_uat.py"
    noop_uat.write_text(
        "from imednet import ImednetSDK\n"
        "from imednet_workflows.uat import UATWorkflow\n\n"
        "with ImednetSDK(simulation_mode=True) as sdk:\n"
        "    UATWorkflow(sdk)\n",
        encoding="utf-8",
    )

    errors = validate_showcase_ast_and_imports(examples_dir)
    assert len(errors) == 1
    assert "Unassigned 'UATWorkflow(...)' call" in errors[0]


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


def test_repo_workflow_bulk_submission_and_uat_execute_in_simulation(tmp_path: Path) -> None:
    """Verify examples/workflow_bulk_submission.py and examples/workflow_uat.py run actively in simulation."""
    import shutil

    repo_root = Path(__file__).resolve().parents[2]
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)
    for script_name in ("workflow_bulk_submission.py", "workflow_uat.py"):
        shutil.copy2(repo_root / "examples" / script_name, examples_dir / script_name)

    ast_errors = validate_showcase_ast_and_imports(examples_dir, repo_root)
    assert not ast_errors, f"Expected zero AST errors, got: {ast_errors}"

    exec_errors = execute_showcases_in_simulation(examples_dir, repo_root)
    assert not exec_errors, f"Expected zero simulation execution errors, got: {exec_errors}"


def test_inline_rst_code_block_syntax_error_detected(tmp_path: Path) -> None:
    """Verify .. code-block:: python and .. sourcecode:: python syntax errors fail validation."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "broken_guide.rst").write_text(
        "Guide\n"
        "=====\n\n"
        ".. code-block:: python\n\n"
        "   def broken_code_block(\n\n"
        ".. sourcecode:: python\n\n"
        "   if True\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir)
    assert len(errors) == 2
    assert all("broken_guide.rst:" in err and "syntax error" in err.lower() for err in errors)


def test_markdown_syntax_and_workspace_import_errors_detected(tmp_path: Path) -> None:
    """Verify fenced Python blocks in docs/*.md, README.md, packages/*/README.md, and examples/*.md fail on syntax or workspace import errors."""
    repo_root = tmp_path / "repo"
    docs_dir = repo_root / "docs"
    pkg_dir = repo_root / "packages" / "demo"
    examples_dir = repo_root / "examples"
    docs_dir.mkdir(parents=True)
    pkg_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    (docs_dir / "notes.md").write_text(
        "# Notes\n\n```python\ndef unclosed_func(\n```\n",
        encoding="utf-8",
    )
    (repo_root / "README.md").write_text(
        "# Root\n\n```python\nfrom imednet import MissingRootSymbol\n```\n",
        encoding="utf-8",
    )
    (pkg_dir / "README.md").write_text(
        "# Package\n\n```py\nfrom apache_airflow_providers_imednet import MissingOperator\n```\n",
        encoding="utf-8",
    )
    (examples_dir / "README.md").write_text(
        "# Examples\n\n```python\nfrom imednet_workflows import NonExistentWorkflow\n```\n",
        encoding="utf-8",
    )

    errors = validate_documentation_literal_includes(docs_dir, repo_root)
    assert len(errors) == 4
    assert any("notes.md:" in e and "syntax error" in e.lower() for e in errors)
    assert any("README.md:" in e and "MissingRootSymbol" in e for e in errors)
    assert any("MissingOperator" in e for e in errors)
    assert any("NonExistentWorkflow" in e for e in errors)


def test_showcase_js_syntax_and_workspace_import_errors_detected(tmp_path: Path) -> None:
    """Verify validate_landing_showcase fails on syntax or workspace import errors in showcase.js."""
    repo_root = Path(__file__).resolve().parents[2]
    docs_dir = tmp_path / "docs"
    static_dir = docs_dir / "_static"
    static_dir.mkdir(parents=True)

    valid_js = (repo_root / "docs" / "_static" / "showcase.js").read_text(encoding="utf-8")
    broken_js = valid_js.replace(
        'studies: `# List available clinical studies\\nfrom imednet import ImednetSDK',
        'studies: `# List available clinical studies\\nfrom imednet import MissingShowcaseSymbol',
    ).replace(
        'forms: `# List study forms and metadata\\nfrom imednet import ImednetSDK',
        'forms: `# List study forms and metadata\\ndef invalid_syntax(',
    )
    (static_dir / "showcase.js").write_text(broken_js, encoding="utf-8")

    errors = validate_landing_showcase(docs_dir, repo_root)
    assert any("SNIPPETS.sdk.studies" in e and "MissingShowcaseSymbol" in e for e in errors)
    assert any("SNIPPETS.sdk.forms" in e and "Syntax error" in e for e in errors)
