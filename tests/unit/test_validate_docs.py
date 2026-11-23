"""Unit tests for scripts/validate_docs.py, scripts/build_docs.py, and docs/conf.py hooks."""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import types
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar

import pytest
from scripts.build_docs import (
    SCOPED_INHERITED_MEMBERS,
    emit_curated_core_reference,
    generate_api_reference,
    require_sphinx_tools,
    scope_apidoc_inherited_members,
)
from scripts.validate_docs import (
    _verify_core_html,
    _verify_pagination_html,
    _verify_sdk_html,
    check_cli_snippets,
    check_cli_subcommand_parity,
    check_documented_extras,
    check_entry_points_parity,
    check_env_var_parity,
    check_no_duplicate_suppression,
    check_package_extras_parity,
    check_rendered_api_reference,
    check_todos_in_init,
    check_tui_bindings_parity,
)

import imednet.core as core_pkg
from imednet.core.endpoint.operations import (
    FilterGetOperation,
    ListOperation,
    RecordCreateOperation,
)
from imednet.endpoints.records import AsyncRecordsEndpoint, RecordsEndpoint
from imednet.models.jobs import Job
from imednet.models.records import Record


def _load_docs_conf():
    """Load docs/conf.py as an isolated module for direct hook testing."""
    conf_path = Path(__file__).resolve().parents[2] / "docs" / "conf.py"
    spec = importlib.util.spec_from_file_location("_test_docs_conf", conf_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_apidoc_options_sorted_for_sphinx_6_and_8_parity(tmp_path: Path) -> None:
    """SPHINX_APIDOC_OPTIONS is alphabetically sorted and includes ImednetBaseModel scoping."""
    build_docs_src = (Path(__file__).resolve().parents[2] / "scripts" / "build_docs.py").read_text(
        encoding="utf-8"
    )
    assert 'env["SPHINX_APIDOC_OPTIONS"] = "inherited-members,members,show-inheritance"' in (
        build_docs_src
    )
    assert "ImednetBaseModel" in SCOPED_INHERITED_MEMBERS

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


def test_emit_curated_core_reference_and_core_exports(tmp_path: Path) -> None:
    """emit_curated_core_reference writes imednet.core.rst and registers it in imednet.rst."""
    expected_exports = {
        "HTTPClientBase",
        "BaseClient",
        "Client",
        "AsyncClient",
        "RetryConfig",
        "RetryPolicy",
        "RetryState",
        "DefaultRetryPolicy",
        "BasePaginator",
        "EdcSyncListEndpoint",
        "EdcAsyncListEndpoint",
        "EdcSyncGetEndpoint",
        "EdcAsyncGetEndpoint",
        "EdcSyncListGetEndpoint",
        "EdcAsyncListGetEndpoint",
    }
    assert expected_exports.issubset(set(core_pkg.__all__))

    imednet_rst = tmp_path / "imednet.rst"
    imednet_rst.write_text(
        "imednet package\n"
        "===============\n\n"
        "Subpackages\n"
        "-----------\n\n"
        ".. toctree::\n"
        "   :maxdepth: 4\n\n"
        "   imednet.auth\n"
        "   imednet.endpoints\n\n"
        "Submodules\n"
        "----------\n",
        encoding="utf-8",
    )
    emit_curated_core_reference(tmp_path)
    emit_curated_core_reference(tmp_path)  # idempotent
    scope_apidoc_inherited_members(tmp_path)

    core_rst = tmp_path / "imednet.core.rst"
    assert core_rst.exists()
    assert ".. automodule:: imednet.core" in core_rst.read_text(encoding="utf-8")
    assert f":inherited-members: {SCOPED_INHERITED_MEMBERS}" in core_rst.read_text(encoding="utf-8")
    imednet_lines = imednet_rst.read_text(encoding="utf-8").splitlines()
    assert imednet_lines.count("   imednet.core") == 1


def test_conf_autodoc_operation_signature_hooks() -> None:
    """docs/conf.py hooks map Operation[T] return annotations to Iterator[T], AsyncIterator[T], or T."""
    conf = _load_docs_conf()
    T_var = TypeVar("T_var")

    assert (
        conf._normalize_operation_return_type(ListOperation[T_var], RecordsEndpoint)
        == Iterator[Record]
    )
    assert (
        conf._normalize_operation_return_type(ListOperation[T_var], AsyncRecordsEndpoint)
        == AsyncIterator[Record]
    )
    assert (
        conf._normalize_operation_return_type(FilterGetOperation[T_var], RecordsEndpoint) is Record
    )
    assert (
        conf._normalize_operation_return_type(RecordCreateOperation[Job], AsyncRecordsEndpoint)
        is Job
    )
    assert (
        conf._normalize_operation_return_type(ListOperation[T_var], core_pkg.EdcSyncListEndpoint)
        == Iterator[T_var]
    )
    assert (
        conf._normalize_operation_return_type(ListOperation[T_var], core_pkg.EdcAsyncListEndpoint)
        == AsyncIterator[T_var]
    )

    from imednet.core.endpoint.base import _ListEndpointBase

    def sample_op(study_key: str | None = None) -> ListOperation[T_var]:  # type: ignore[empty-body]
        ...

    fake_app = SimpleNamespace(
        env=SimpleNamespace(
            temp_data={
                "autodoc:module": "imednet.endpoints.records",
                "autodoc:class": "AsyncRecordsEndpoint",
            }
        )
    )
    conf._before_process_endpoint_signature(fake_app, sample_op, False)
    assert sample_op.__annotations__["return"] == AsyncIterator[Record]

    raw_base_list = _ListEndpointBase.__dict__["list"].__wrapped__
    base_list_copy = types.FunctionType(
        raw_base_list.__code__,
        raw_base_list.__globals__,
        "list",
    )
    base_list_copy.__annotations__ = dict(raw_base_list.__annotations__)
    conf._before_process_endpoint_signature(fake_app, base_list_copy, False)
    assert base_list_copy.__annotations__["return"] == AsyncIterator[Record]

    def raw_op(study_key: str | None = None) -> FilterGetOperation[Record]:  # type: ignore[empty-body]
        ...

    rewritten = conf._process_endpoint_signature(
        fake_app,
        "method",
        "imednet.endpoints.records.RecordsEndpoint.get",
        raw_op,
        {},
        "(study_key=None)",
        "FilterGetOperation[Record]",
    )
    assert rewritten is not None
    assert "FilterGetOperation" not in (rewritten[1] or "")
    assert "Record" in (rewritten[1] or "")


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
        '       --record-filter "site_id=1" \\\n'
        '       --subject-filter "status=Active"\n',
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


def test_sphinx_html_renders_curated_core_pagination_sdk_and_endpoints(tmp_path: Path) -> None:
    """Mini Sphinx HTML build with docs/conf.py renders core, pagination, SDK, and endpoint pages."""
    repo_root = Path(__file__).resolve().parents[2]
    src_dir = tmp_path / "docs"
    api_dir = src_dir / "reference" / "api"
    api_dir.mkdir(parents=True)
    html_dir = tmp_path / "_build" / "html"

    (src_dir / "index.rst").write_text(
        "Test Docs\n=========\n\n"
        ".. meta::\n   :description: Test docs\n\n"
        ".. toctree::\n   :maxdepth: 2\n\n"
        "   reference/api/imednet\n"
        "   reference/api/imednet.core\n"
        "   reference/api/imednet.pagination\n"
        "   reference/api/imednet.endpoints\n",
        encoding="utf-8",
    )
    (src_dir / "404.rst").write_text(":orphan:\n\nNot Found\n=========\n", encoding="utf-8")
    (api_dir / "imednet.rst").write_text(
        "imednet\n=======\n\n"
        ".. automodule:: imednet.sdk\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n\n"
        ".. automodule:: imednet.sdk_convenience\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n",
        encoding="utf-8",
    )
    (api_dir / "imednet.core.rst").write_text(
        "imednet.core\n============\n\n"
        ".. automodule:: imednet.core\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n",
        encoding="utf-8",
    )
    (api_dir / "imednet.pagination.rst").write_text(
        "imednet.pagination\n==================\n\n"
        ".. automodule:: imednet.pagination\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n",
        encoding="utf-8",
    )
    (api_dir / "imednet.endpoints.rst").write_text(
        "imednet.endpoints\n=================\n\n"
        ".. automodule:: imednet.endpoints.records\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n\n"
        ".. automodule:: imednet.models.records\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n\n"
        ".. automodule:: imednet.models.jobs\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n\n"
        ".. automodule:: imednet.models.base\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n",
        encoding="utf-8",
    )

    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "sphinx",
            "-b",
            "html",
            "-c",
            str(repo_root / "docs"),
            str(src_dir),
            str(html_dir),
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Sphinx failed:\n{res.stdout}\n{res.stderr}"

    built_api = html_dir / "reference" / "api"
    assert _verify_core_html(built_api / "imednet.core.html") is False
    assert _verify_pagination_html(built_api / "imednet.pagination.html") is False
    assert _verify_sdk_html(built_api / "imednet.html") is False

    ep_text = (built_api / "imednet.endpoints.html").read_text(encoding="utf-8")
    assert "ListOperation" not in ep_text
    assert "FilterGetOperation" not in ep_text
    assert "model_validate" not in ep_text
    assert "RecordsEndpoint.to_dataframe" in ep_text
    assert "AsyncRecordsEndpoint.to_dataframe" in ep_text


def test_conf_resolves_mermaid_version_across_extension_majors(monkeypatch) -> None:
    """_resolve_mermaid_version returns 10.2.0 for <1.0 and >=10.3.0-compatible version for >=1.0."""
    import importlib.metadata

    conf = _load_docs_conf()
    monkeypatch.setattr(importlib.metadata, "version", lambda _pkg: "0.9.2")
    assert conf._resolve_mermaid_version() == "10.2.0"

    monkeypatch.setattr(importlib.metadata, "version", lambda _pkg: "2.0.0")
    assert conf._resolve_mermaid_version() == "11.12.1"


def test_generate_api_reference_fails_fast_without_sphinx_apidoc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing sphinx-apidoc fails fast and keeps all existing .rst files in api_dir."""
    import shutil

    api_dir = tmp_path / "docs" / "reference" / "api"
    api_dir.mkdir(parents=True)
    tracked_files = {}
    for idx in range(51):
        rst = api_dir / f"module_{idx:02d}.rst"
        content = f"Module {idx}\n=========\n"
        rst.write_text(content, encoding="utf-8")
        tracked_files[rst.name] = content

    monkeypatch.setattr(shutil, "which", lambda _tool: None)

    with pytest.raises(FileNotFoundError, match="sphinx-apidoc"):
        generate_api_reference(api_dir)

    remaining = {p.name: p.read_text(encoding="utf-8") for p in sorted(api_dir.glob("*.rst"))}
    assert len(remaining) == 51
    assert remaining == tracked_files


def test_generate_api_reference_preserves_existing_docs_on_staging_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subprocess failure during apidoc staging leaves existing api_dir intact with no staging leftovers."""
    import scripts.build_docs as build_docs_mod

    api_dir = tmp_path / "docs" / "reference" / "api"
    api_dir.mkdir(parents=True)
    original = api_dir / "imednet.rst"
    original.write_text("Original\n========\n", encoding="utf-8")

    monkeypatch.setattr(
        build_docs_mod, "require_sphinx_tools", lambda *tools: {t: f"/bin/{t}" for t in tools}
    )

    def _fail_run_command(cmd, env=None, check=True):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(build_docs_mod, "run_command", _fail_run_command)

    with pytest.raises(subprocess.CalledProcessError):
        generate_api_reference(api_dir)

    assert original.read_text(encoding="utf-8") == "Original\n========\n"
    assert list(api_dir.parent.glob(".api_staging_*")) == []
    with pytest.raises(FileNotFoundError):
        monkeypatch.setattr(build_docs_mod.shutil, "which", lambda _t: None)
        require_sphinx_tools("sphinx-apidoc", "sphinx-build")


def test_reverse_parity_gates_pass_on_clean_repository() -> None:
    """All reverse parity gates pass against the synchronized monorepo documentation."""
    repo_root = Path(__file__).resolve().parents[2]
    assert check_package_extras_parity(repo_root) is False
    assert check_entry_points_parity(repo_root) is False
    assert check_env_var_parity(repo_root) is False
    assert check_cli_subcommand_parity(repo_root) is False
    assert check_tui_bindings_parity(repo_root) is False


def test_check_package_extras_parity_detects_missing_extra(tmp_path: Path) -> None:
    """check_package_extras_parity fails when declared extras are omitted from install docs."""
    core_dir = tmp_path / "packages" / "core"
    core_dir.mkdir(parents=True)
    (core_dir / "pyproject.toml").write_text(
        '[project]\nname = "imednet"\n'
        "[project.optional-dependencies]\ncli = []\nexport = []\ntui = []\n",
        encoding="utf-8",
    )
    inc_dir = tmp_path / "docs" / "_includes"
    inc_dir.mkdir(parents=True)
    install_rst = inc_dir / "install_extensions.rst"
    readme_md = tmp_path / "README.md"

    install_rst.write_text(
        'pip install "imednet[cli]"\npip install "imednet[export]"\npip install "imednet[tui]"\n',
        encoding="utf-8",
    )
    readme_md.write_text(
        'pip install "imednet[cli]"\npip install "imednet[export]"\npip install "imednet[tui]"\n',
        encoding="utf-8",
    )
    assert check_package_extras_parity(tmp_path) is False

    # Drift: omit imednet[cli] from install_extensions.rst
    install_rst.write_text(
        'pip install "imednet[export]"\npip install "imednet[tui]"\n',
        encoding="utf-8",
    )
    assert check_package_extras_parity(tmp_path) is True

    # Restore install_extensions.rst, omit imednet[cli] from root README.md
    install_rst.write_text(
        'pip install "imednet[cli]"\npip install "imednet[export]"\npip install "imednet[tui]"\n',
        encoding="utf-8",
    )
    readme_md.write_text(
        'pip install "imednet[export]"\npip install "imednet[tui]"\n',
        encoding="utf-8",
    )
    assert check_package_extras_parity(tmp_path) is True


def test_check_entry_points_parity_detects_missing_group_or_entry(tmp_path: Path) -> None:
    """check_entry_points_parity fails when pyproject entry points are undocumented."""
    wf_dir = tmp_path / "packages" / "plugins-workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "pyproject.toml").write_text(
        '[project]\nname = "imednet-workflows"\n'
        '[project.entry-points."imednet.state_providers"]\n'
        'default = "imednet_workflows.state_ledger:get_state_provider"\n'
        'file = "imednet_workflows.state_ledger:FileStateProvider"\n'
        'airflow = "imednet_workflows.state_ledger:AirflowStateProvider"\n',
        encoding="utf-8",
    )
    wf_readme = wf_dir / "README.md"
    wf_readme.write_text(
        "Registers `imednet.state_providers` (`default`, `file`, `airflow`).\n",
        encoding="utf-8",
    )
    assert check_entry_points_parity(tmp_path) is False

    # Drift: omit 'airflow' entry point name
    wf_readme.write_text(
        "Registers `imednet.state_providers` (`default`, `file`).\n",
        encoding="utf-8",
    )
    assert check_entry_points_parity(tmp_path) is True

    # Drift: omit group name
    wf_readme.write_text(
        "Providers: `default`, `file`, `airflow`.\n",
        encoding="utf-8",
    )
    assert check_entry_points_parity(tmp_path) is True


def test_check_env_var_parity_detects_missing_runtime_var(tmp_path: Path) -> None:
    """check_env_var_parity extracts runtime IMEDNET_* AST constants and flags drift."""
    src_dir = tmp_path / "packages" / "core" / "src" / "imednet"
    src_dir.mkdir(parents=True)
    (src_dir / "cfg.py").write_text(
        '"""Docstring mentioning IMEDNET_DOCSTRING_ONLY."""\n'
        "import os\n"
        'API_KEY = os.getenv("IMEDNET_API_KEY")\n'
        'STRICT = os.environ.get("IMEDNET_STRICT_MODE", "0")\n',
        encoding="utf-8",
    )
    ref_dir = tmp_path / "docs" / "reference"
    ref_dir.mkdir(parents=True)
    config_rst = ref_dir / "configuration.rst"
    env_example = tmp_path / ".env.example"

    config_rst.write_text(
        ".. list-table::\n\n"
        "   * - IMEDNET_API_KEY\n"
        "     - None\n"
        "     - Key\n"
        "   * - IMEDNET_STRICT_MODE\n"
        "     - 0\n"
        "     - Strict\n",
        encoding="utf-8",
    )
    env_example.write_text(
        "IMEDNET_API_KEY=placeholder\n# IMEDNET_STRICT_MODE=0\n",
        encoding="utf-8",
    )
    assert check_env_var_parity(tmp_path) is False

    # Drift in configuration.rst
    config_rst.write_text(
        ".. list-table::\n\n   * - IMEDNET_API_KEY\n     - None\n     - Key\n",
        encoding="utf-8",
    )
    assert check_env_var_parity(tmp_path) is True

    # Restore configuration.rst, introduce drift in .env.example
    config_rst.write_text(
        ".. list-table::\n\n"
        "   * - IMEDNET_API_KEY\n"
        "     - None\n"
        "     - Key\n"
        "   * - IMEDNET_STRICT_MODE\n"
        "     - 0\n"
        "     - Strict\n",
        encoding="utf-8",
    )
    env_example.write_text("IMEDNET_API_KEY=placeholder\n", encoding="utf-8")
    assert check_env_var_parity(tmp_path) is True


def test_check_cli_subcommand_parity_detects_missing_subcommands_and_export_flags(
    tmp_path: Path,
) -> None:
    """check_cli_subcommand_parity enforces shell examples and shared export flags."""
    parser = argparse.ArgumentParser(prog="imednet")
    top = parser.add_subparsers(dest="command")

    studies = top.add_parser("studies")
    studies_sub = studies.add_subparsers(dest="sub")
    studies_sub.add_parser("list")

    records = top.add_parser("records")
    records_sub = records.add_subparsers(dest="sub")
    rec_create = records_sub.add_parser("create")
    rec_create.add_argument("--schema")
    rec_create.add_argument("--dry-run", action="store_true")
    rec_create.add_argument("--wait", action="store_true")

    export = top.add_parser("export")
    export_sub = export.add_subparsers(dest="dest")
    for dest in ("csv", "parquet"):
        p = export_sub.add_parser(dest)
        p.add_argument("--since")
        p.add_argument("--no-quality-gate", action="store_true")

    workflows = top.add_parser("workflows")
    wf_sub = workflows.add_subparsers(dest="wf_cmd")
    wf_sub.add_parser("validate-standards")

    howto_dir = tmp_path / "docs" / "how-to"
    howto_dir.mkdir(parents=True)
    cli_rst = howto_dir / "cli.rst"
    wf_dir = tmp_path / "packages" / "plugins-workflows"
    wf_dir.mkdir(parents=True)
    wf_readme = wf_dir / "README.md"

    valid_cli_rst = (
        "CLI Guide\n=========\n\n"
        "Resources: ``studies``. Exporters: ``csv``, ``parquet``.\n"
        "Flags: ``--since``, ``--no-quality-gate``, ``--schema``, ``--dry-run``, ``--wait``.\n\n"
        ".. code-block:: bash\n\n"
        "   imednet records create <STUDY_KEY> --schema s.json --dry-run --wait\n"
        "   imednet workflows validate-standards <STUDY_KEY>\n"
    )
    cli_rst.write_text(valid_cli_rst, encoding="utf-8")
    wf_readme.write_text(
        "```bash\nimednet workflows validate-standards <STUDY_KEY>\n```\n",
        encoding="utf-8",
    )
    assert check_cli_subcommand_parity(tmp_path, parser=parser) is False

    # Drift 1: prose mention of 'records create' without a shell snippet fails
    cli_rst.write_text(
        "CLI Guide\n=========\n\n"
        "Resources: ``studies``. Exporters: ``csv``, ``parquet``.\n"
        "Write command ``records create`` supports ``--schema``, ``--dry-run``, ``--wait``.\n"
        "Export flags: ``--since``, ``--no-quality-gate``.\n\n"
        ".. code-block:: bash\n\n"
        "   imednet workflows validate-standards <STUDY_KEY>\n",
        encoding="utf-8",
    )
    assert check_cli_subcommand_parity(tmp_path, parser=parser) is True

    # Drift 2: missing shared export flag (--no-quality-gate) fails
    cli_rst.write_text(
        valid_cli_rst.replace("``--no-quality-gate``, ", ""),
        encoding="utf-8",
    )
    assert check_cli_subcommand_parity(tmp_path, parser=parser) is True

    # Drift 3: missing workflow subcommand from plugins-workflows/README.md fails
    cli_rst.write_text(valid_cli_rst, encoding="utf-8")
    wf_readme.write_text("# Workflows\n", encoding="utf-8")
    assert check_cli_subcommand_parity(tmp_path, parser=parser) is True


def test_check_tui_bindings_parity_detects_missing_bindings_flags_and_false_space_claim(
    tmp_path: Path,
) -> None:
    """check_tui_bindings_parity flags missing bindings/flags and false Space selection claims."""
    parser = argparse.ArgumentParser(prog="imednet")
    sub = parser.add_subparsers(dest="command")
    tui_p = sub.add_parser("tui")
    tui_p.add_argument("--study-key")
    tui_p.add_argument("--force", action="store_true")
    tui_p.add_argument("--simulation", action="store_true")

    app_dir = tmp_path / "packages" / "plugins-tui" / "src" / "imednet_tui"
    app_dir.mkdir(parents=True)
    (app_dir / "app.py").write_text(
        "class StudyList:\n    pass\n\n"
        "class JobTable:\n"
        '    BINDINGS = [("delete,backspace", "untrack_selected_job", "Untrack Job")]\n\n'
        "class JobMonitor:\n"
        "    MAX_CONCURRENT_POLLS = 5\n\n"
        "class ImednetTuiApp:\n"
        '    BINDINGS = [("q", "quit", "Quit"), ("escape", "handle_escape", "Clear Filter")]\n',
        encoding="utf-8",
    )
    tui_readme = tmp_path / "packages" / "plugins-tui" / "README.md"
    howto_dir = tmp_path / "docs" / "how-to"
    howto_dir.mkdir(parents=True)
    cli_rst = howto_dir / "cli.rst"

    valid_readme = (
        "| `Enter` | Select item in `StudyList` |\n"
        "| `Delete` / `Backspace` | Untrack Job |\n"
        "| `Esc` | Clear Filter |\n"
        "| `q` | Quit |\n"
        "Polls up to 5 concurrent jobs, shows `Logs`, and switches to `compact` layout.\n"
        "Flags: `--study-key`, `--force`, `--simulation`.\n"
    )
    tui_readme.write_text(valid_readme, encoding="utf-8")
    cli_rst.write_text(
        "Keys: ``Delete``, ``Backspace``, ``Esc``, ``q``.\n",
        encoding="utf-8",
    )
    assert check_tui_bindings_parity(tmp_path, parser=parser) is False

    # Drift 1: false Enter / Space selection claim when Space is not bound
    tui_readme.write_text(
        valid_readme.replace("| `Enter` |", "| `Enter` / `Space` |"),
        encoding="utf-8",
    )
    assert check_tui_bindings_parity(tmp_path, parser=parser) is True

    # Drift 2: omit Delete keybinding
    tui_readme.write_text(
        valid_readme.replace("`Delete` / ", ""),
        encoding="utf-8",
    )
    assert check_tui_bindings_parity(tmp_path, parser=parser) is True

    # Drift 3: omit --force CLI flag
    tui_readme.write_text(
        valid_readme.replace("`--force`, ", ""),
        encoding="utf-8",
    )
    assert check_tui_bindings_parity(tmp_path, parser=parser) is True
