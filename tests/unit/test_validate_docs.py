"""Unit tests for scripts/validate_docs.py API reference and backlink verification."""

from pathlib import Path

from scripts.build_docs import SCOPED_INHERITED_MEMBERS, scope_apidoc_inherited_members
from scripts.validate_docs import (
    check_cli_snippets,
    check_documented_extras,
    check_no_duplicate_suppression,
    check_rendered_api_reference,
    check_todos_in_init,
)


def test_apidoc_options_sorted_for_sphinx_6_and_8_parity(tmp_path: Path) -> None:
    """SPHINX_APIDOC_OPTIONS is alphabetically sorted so Sphinx 6 and 8 emit identical RST."""
    build_docs_src = (Path(__file__).resolve().parents[2] / "scripts" / "build_docs.py").read_text(
        encoding="utf-8"
    )
    assert 'env["SPHINX_APIDOC_OPTIONS"] = "inherited-members,members,show-inheritance"' in (
        build_docs_src
    )

    sample_rst = tmp_path / "imednet.sample.rst"
    sample_rst.write_text(
        "imednet.sample module\n"
        "=====================\n\n"
        ".. automodule:: imednet.sample\n"
        "   :inherited-members:\n"
        "   :members:\n"
        "   :show-inheritance:\n",
        encoding="utf-8",
    )
    scope_apidoc_inherited_members(tmp_path)
    assert sample_rst.read_text(encoding="utf-8") == (
        "imednet.sample module\n"
        "=====================\n\n"
        ".. automodule:: imednet.sample\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n"
        "   :show-inheritance:\n"
    )


def test_check_no_duplicate_suppression_detects_forbidden_patterns(tmp_path: Path) -> None:
    """check_no_duplicate_suppression flags _SuppressDuplicateDescriptions and duplicate filters."""
    clean_conf = tmp_path / "conf.py"
    clean_conf.write_text('project = "imednet"\n', encoding="utf-8")
    assert check_no_duplicate_suppression(clean_conf) is False

    suppressed_conf = tmp_path / "bad_conf.py"
    suppressed_conf.write_text("class _SuppressDuplicateDescriptions: pass\n", encoding="utf-8")
    assert check_no_duplicate_suppression(suppressed_conf) is True

    filtered_conf = tmp_path / "filtered_conf.py"
    filtered_conf.write_text(
        'warnings.filterwarnings("ignore", message="duplicate object description*")\n',
        encoding="utf-8",
    )
    assert check_no_duplicate_suppression(filtered_conf) is True


def test_check_rendered_api_reference_flags_builtins_and_broken_backlinks(tmp_path: Path) -> None:
    """check_rendered_api_reference fails when pages are missing or contain broken [docs] anchors."""
    assert check_rendered_api_reference(tmp_path) is True


def test_check_todos_in_init_scans_module_class_and_function_docstrings(tmp_path: Path) -> None:
    """check_todos_in_init catches TODO placeholders across examples and package sources."""
    clean_dir = tmp_path / "clean"
    clean_dir.mkdir(parents=True)
    (clean_dir / "ok.py").write_text(
        '"""Clean module docstring."""\n\n'
        "def run() -> None:\n"
        '    """Clean function docstring."""\n'
        "    pass\n",
        encoding="utf-8",
    )
    assert check_todos_in_init([clean_dir]) is False

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "bad.py").write_text(
        '"""Module with TODO placeholder."""\n',
        encoding="utf-8",
    )
    assert check_todos_in_init([bad_dir]) is True


def test_check_documented_extras_validates_against_pyproject_toml(tmp_path: Path) -> None:
    """check_documented_extras passes declared extras and rejects undeclared extras."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "imednet-workspace"\n[project.optional-dependencies]\ndocs = []\n',
        encoding="utf-8",
    )
    core_dir = tmp_path / "packages" / "core"
    core_dir.mkdir(parents=True)
    (core_dir / "pyproject.toml").write_text(
        '[project]\nname = "imednet"\n[project.optional-dependencies]\nexport = []\nsnowflake = []\n',
        encoding="utf-8",
    )
    airflow_dir = tmp_path / "packages" / "providers-airflow"
    airflow_dir.mkdir(parents=True)
    (airflow_dir / "pyproject.toml").write_text(
        '[project]\nname = "apache-airflow-providers-imednet"\n',
        encoding="utf-8",
    )

    readme = tmp_path / "README.md"
    readme.write_text(
        'Install with `pip install "imednet[export,snowflake]"`.\n',
        encoding="utf-8",
    )
    assert check_documented_extras(tmp_path) is False

    readme.write_text(
        'Install with `pip install "imednet[pandas]"` or `apache-airflow-providers-imednet[amazon]`.\n',
        encoding="utf-8",
    )
    assert check_documented_extras(tmp_path) is True


def test_check_cli_snippets_validates_flags_against_parser(tmp_path: Path) -> None:
    """check_cli_snippets accepts valid imednet CLI commands and rejects invalid flags."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    cli_rst = docs_dir / "cli.rst"
    cli_rst.write_text(
        "CLI Guide\n"
        "=========\n\n"
        ".. code-block:: bash\n\n"
        "   imednet workflows extract-records <STUDY_KEY> \\\n"
        "       --record-filter \"site_id=1\" \\\n"
        "       --subject-filter \"status=Active\"\n",
        encoding="utf-8",
    )
    assert check_cli_snippets(tmp_path) is False

    cli_rst.write_text(
        "CLI Guide\n"
        "=========\n\n"
        ".. code-block:: bash\n\n"
        "   imednet workflows extract-records <STUDY_KEY> --nonexistent-flag value\n",
        encoding="utf-8",
    )
    assert check_cli_snippets(tmp_path) is True
