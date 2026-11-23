"""Unit tests for scripts/validate_docs.py, scripts/build_docs.py, and docs/conf.py hooks."""

from __future__ import annotations

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
    check_adr_records,
    check_cli_snippets,
    check_compliance_html_links,
    check_diataxis_sidebar_isolation,
    check_documented_extras,
    check_duckdb_relations_parity,
    check_env_vars_documented,
    check_examples_index_parity,
    check_no_duplicate_suppression,
    check_package_docs,
    check_packages_registered,
    check_release_please_parity,
    check_rendered_api_reference,
    check_sdtm_and_phi_parity,
    check_todos_in_init,
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


def test_validate_docs_checks_pass_outside_repo_root_and_fail_fast_on_missing_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All default validate_docs checks anchor to REPO_ROOT from any CWD and fail fast on missing dirs."""
    monkeypatch.chdir(tmp_path)
    assert check_todos_in_init() is False
    assert check_documented_extras() is False
    assert check_cli_snippets() is False
    assert check_packages_registered() is False
    assert check_package_docs() is False
    assert check_release_please_parity() is False
    assert check_examples_index_parity() is False
    assert check_diataxis_sidebar_isolation() is False
    assert check_adr_records() is False
    assert check_env_vars_documented() is False
    assert check_sdtm_and_phi_parity() is False
    assert check_duckdb_relations_parity() is False

    empty_root = tmp_path / "empty_workspace"
    empty_root.mkdir()
    assert check_todos_in_init(repo_root=empty_root) is True
    assert check_documented_extras(empty_root) is True
    assert check_cli_snippets(empty_root) is True
    assert check_packages_registered(empty_root) is True
    assert check_package_docs(empty_root) is True
    assert check_release_please_parity(empty_root) is True
    assert check_examples_index_parity(empty_root) is True
    assert check_diataxis_sidebar_isolation(empty_root) is True
    assert check_adr_records(empty_root) is True
    assert check_env_vars_documented(empty_root) is True
    assert check_sdtm_and_phi_parity(empty_root) is True
    assert check_duckdb_relations_parity(empty_root) is True


def test_check_examples_index_parity_detects_unindexed_script(tmp_path: Path) -> None:
    """check_examples_index_parity fails when an examples/*.py script lacks an index entry."""
    ex_dir = tmp_path / "examples"
    wrappers_dir = tmp_path / "docs" / "tutorials" / "examples"
    ex_dir.mkdir(parents=True)
    wrappers_dir.mkdir(parents=True)

    (ex_dir / "indexed_script.py").write_text('"""Indexed."""\n', encoding="utf-8")
    (wrappers_dir / "indexed_script.rst").write_text(
        "Indexed\n=======\n\n.. literalinclude:: /../examples/indexed_script.py\n   :language: python\n",
        encoding="utf-8",
    )
    (wrappers_dir / "index.rst").write_text(
        "Examples\n========\n\n.. toctree::\n   :maxdepth: 1\n\n   indexed_script\n",
        encoding="utf-8",
    )
    assert check_examples_index_parity(tmp_path) is False

    (ex_dir / "orphan_script.py").write_text('"""Unindexed."""\n', encoding="utf-8")
    assert check_examples_index_parity(tmp_path) is True


def test_check_release_please_parity_detects_version_drift_and_missing_extra_files(
    tmp_path: Path,
) -> None:
    """check_release_please_parity enforces extra-files and version parity with manifest."""
    import json

    for pkg, ver in (
        ("packages/core", "1.1.0"),
        ("packages/plugins-workflows", "1.1.0"),
        ("packages/plugins-sinks", "1.0.1"),
    ):
        pkg_dir = tmp_path / pkg
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "pyproject.toml").write_text(
            f'[project]\nname = "demo"\nversion = "{ver}"\n',
            encoding="utf-8",
        )

    init_py = tmp_path / "packages" / "core" / "src" / "imednet" / "__init__.py"
    init_py.parent.mkdir(parents=True)
    init_py.write_text('__version__ = "1.1.0"  # x-release-please-version\n', encoding="utf-8")

    thebe_js = tmp_path / "docs" / "_static" / "thebe_pyodide.js"
    thebe_js.parent.mkdir(parents=True)
    thebe_js.write_text(
        '"_static/wheels/imednet-1.1.0-py3-none-any.whl", // x-release-please-version\n'
        '"_static/wheels/imednet_workflows-1.1.0-py3-none-any.whl",\n'
        '"_static/wheels/imednet_plugins_sinks-1.0.1-py3-none-any.whl",\n',
        encoding="utf-8",
    )

    manifest = {
        "packages/core": "1.1.0",
        "packages/plugins-workflows": "1.1.0",
        "packages/plugins-sinks": "1.0.1",
    }
    (tmp_path / ".release-please-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    cfg = {
        "packages": {
            "packages/core": {
                "extra-files": [
                    {"type": "generic", "path": "/packages/core/src/imednet/__init__.py"},
                    {"type": "generic", "path": "/docs/_static/thebe_pyodide.js"},
                ]
            },
            "packages/plugins-workflows": {},
            "packages/plugins-sinks": {},
        }
    }
    (tmp_path / "release-please-config.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert check_release_please_parity(tmp_path) is False

    # Drift in __init__.py version fails parity
    init_py.write_text('__version__ = "1.0.0"  # x-release-please-version\n', encoding="utf-8")
    assert check_release_please_parity(tmp_path) is True


def test_check_diataxis_sidebar_isolation_rejects_internal_leak_and_duplicate_toctree(
    tmp_path: Path,
) -> None:
    """check_diataxis_sidebar_isolation fails when maintainer files leak into Reference or Explanation."""
    docs_dir = tmp_path / "docs" / "reference"
    docs_dir.mkdir(parents=True)
    (tmp_path / "docs" / "index.rst").write_text(
        "Docs\n====\n\n"
        ".. toctree::\n   :caption: Reference:\n\n   reference/changelogs/index\n   reference/agent_rules\n",
        encoding="utf-8",
    )
    (docs_dir / "contributing.rst").write_text("Contributing\n============\n", encoding="utf-8")
    assert check_diataxis_sidebar_isolation(tmp_path) is True


def test_check_env_vars_documented_passes_repo_and_detects_missing_var(tmp_path: Path) -> None:
    """check_env_vars_documented passes on repo and fails fast when a runtime IMEDNET_* var is undocumented."""
    repo_root = Path(__file__).resolve().parents[2]
    assert check_env_vars_documented(repo_root) is False

    pkg_src = tmp_path / "packages" / "core" / "src" / "imednet"
    pkg_src.mkdir(parents=True)
    (pkg_src / "sample.py").write_text(
        'import os\nEXTRA_ENV = "IMEDNET_SYNTHETIC_EXTRA"\n'
        'val_a = os.getenv("IMEDNET_OIDC_ISSUER")\n'
        "val_b = os.environ.get(EXTRA_ENV)\n",
        encoding="utf-8",
    )
    conf_rst = tmp_path / "docs" / "reference" / "configuration.rst"
    conf_rst.parent.mkdir(parents=True)
    conf_rst.write_text("Configuration\n=============\n\nIMEDNET_OIDC_ISSUER\n", encoding="utf-8")

    assert check_env_vars_documented(tmp_path) is True

    conf_rst.write_text(
        "Configuration\n=============\n\nIMEDNET_OIDC_ISSUER\nIMEDNET_SYNTHETIC_EXTRA\n",
        encoding="utf-8",
    )
    assert check_env_vars_documented(tmp_path) is False


def test_check_sdtm_and_phi_parity_passes_repo_and_rejects_omissions(tmp_path: Path) -> None:
    """check_sdtm_and_phi_parity passes on repo and rejects missing SDTM domains or PHI keys."""
    repo_root = Path(__file__).resolve().parents[2]
    assert check_sdtm_and_phi_parity(repo_root) is False

    real_text = (repo_root / "docs" / "how-to" / "record_mapping.rst").read_text(encoding="utf-8")
    target_rst = tmp_path / "docs" / "how-to" / "record_mapping.rst"
    target_rst.parent.mkdir(parents=True)

    # Removing an SDTM domain fails parity
    target_rst.write_text(real_text.replace("``MH``", "``XX``"), encoding="utf-8")
    assert check_sdtm_and_phi_parity(tmp_path) is True

    # Removing a SensitivityRegistry PHI key fails parity
    target_rst.write_text(real_text.replace("``subjinit``", "``other_field``"), encoding="utf-8")
    assert check_sdtm_and_phi_parity(tmp_path) is True

    # Removing an exempt structural key fails parity
    target_rst.write_text(real_text.replace("``usubjid``", "``other_id``"), encoding="utf-8")
    assert check_sdtm_and_phi_parity(tmp_path) is True


def test_check_duckdb_relations_parity_passes_repo_and_rejects_stale_names(tmp_path: Path) -> None:
    """check_duckdb_relations_parity enforces bronze_revisions/silver_current_state and rejects stale names."""
    repo_root = Path(__file__).resolve().parents[2]
    assert check_duckdb_relations_parity(repo_root) is False

    centralizer_dst = (
        tmp_path
        / "packages"
        / "plugins-workflows"
        / "src"
        / "imednet_workflows"
        / "duckdb_centralizer.py"
    )
    centralizer_dst.parent.mkdir(parents=True)
    centralizer_dst.write_text(
        (
            repo_root
            / "packages"
            / "plugins-workflows"
            / "src"
            / "imednet_workflows"
            / "duckdb_centralizer.py"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    audit_dst = tmp_path / "docs" / "how-to" / "audit_compliance.rst"
    audit_dst.parent.mkdir(parents=True)
    security_dst = tmp_path / "docs" / "reference" / "security_exceptions.rst"
    security_dst.parent.mkdir(parents=True)

    real_audit = (repo_root / "docs" / "how-to" / "audit_compliance.rst").read_text(
        encoding="utf-8"
    )
    real_security = (repo_root / "docs" / "reference" / "security_exceptions.rst").read_text(
        encoding="utf-8"
    )

    security_dst.write_text(real_security, encoding="utf-8")
    audit_dst.write_text(
        real_audit.replace("``silver_records_history``", "``legacy_history``"),
        encoding="utf-8",
    )
    assert check_duckdb_relations_parity(tmp_path) is True

    audit_dst.write_text(
        real_audit + "\nStale ``record_revisions`` table wording.\n",
        encoding="utf-8",
    )
    assert check_duckdb_relations_parity(tmp_path) is True

    audit_dst.write_text(real_audit, encoding="utf-8")
    security_dst.write_text(real_security + "\nRun uvx hatch run lint:audit\n", encoding="utf-8")
    assert check_duckdb_relations_parity(tmp_path) is True


def test_sphinx_shadowed_methods_and_spi_compliance_html_links(tmp_path: Path) -> None:
    """Sphinx renders RecordRevision.validate_chain and resolves imednet.spi.* links verified by check_compliance_html_links."""
    repo_root = Path(__file__).resolve().parents[2]
    src_dir = tmp_path / "docs_src"
    html_dir = tmp_path / "html_out"
    for sub in ("how-to", "explanation", "reference/api"):
        (src_dir / sub).mkdir(parents=True)

    (src_dir / "index.rst").write_text(
        "Index\n=====\n\n.. toctree::\n\n"
        "   how-to/audit_compliance\n"
        "   how-to/record_mapping\n"
        "   explanation/architecture\n"
        "   explanation/api_overview\n"
        "   reference/governance\n"
        "   reference/api/imednet.models\n"
        "   reference/api/imednet.integrations\n"
        "   reference/api/imednet.auth\n"
        "   reference/api/imednet_workflows\n",
        encoding="utf-8",
    )
    (src_dir / "404.rst").write_text(":orphan:\n\nNot Found\n=========\n", encoding="utf-8")

    (src_dir / "how-to" / "audit_compliance.rst").write_text(
        "Audit\n=====\n\n"
        "See :meth:`RecordRevision.validate_chain <imednet.models.record_revisions.RecordRevision.validate_chain>`.\n",
        encoding="utf-8",
    )
    (src_dir / "explanation" / "architecture.rst").write_text(
        "Architecture\n============\n\n"
        "Use :func:`~imednet.spi.models.transform_records_to_sdtm_rows` and "
        ":func:`~imednet.spi.sink_base.evaluate_standards_readiness`.\n",
        encoding="utf-8",
    )
    (src_dir / "how-to" / "record_mapping.rst").write_text(
        "Record Mapping\n==============\n\n"
        "Call :func:`~imednet.spi.models.transform_records_to_sdtm_rows` and "
        ":func:`~imednet.spi.sink_base.evaluate_standards_readiness`.\n",
        encoding="utf-8",
    )
    (src_dir / "reference" / "governance.rst").write_text(
        "Governance\n==========\n\n"
        "See :meth:`~imednet_workflows.config_version_control.ConfigVersionStore.rollback_config`.\n",
        encoding="utf-8",
    )
    (src_dir / "explanation" / "api_overview.rst").write_text(
        "API Overview\n============\n\n"
        "Use :class:`~imednet.auth.strategy.AuthStrategy`, "
        ":class:`~imednet.auth.oidc.OIDCAuth`, and "
        ":class:`~imednet.auth.credentials.MemoryCredentialStore`.\n",
        encoding="utf-8",
    )
    (src_dir / "reference" / "api" / "imednet.models.rst").write_text(
        "imednet.models\n==============\n\n"
        ".. automodule:: imednet.models.record_revisions\n"
        f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
        "   :members:\n   :show-inheritance:\n\n"
        ".. automodule:: imednet.models.sdtm\n"
        "   :members:\n",
        encoding="utf-8",
    )
    (src_dir / "reference" / "api" / "imednet.integrations.rst").write_text(
        "imednet.integrations\n====================\n\n"
        ".. automodule:: imednet.integrations.sink_base\n"
        "   :members:\n",
        encoding="utf-8",
    )
    (src_dir / "reference" / "api" / "imednet.auth.rst").write_text(
        "imednet.auth\n============\n\n"
        ".. automodule:: imednet.auth.strategy\n   :members:\n\n"
        ".. automodule:: imednet.auth.oidc\n   :members:\n\n"
        ".. automodule:: imednet.auth.credentials\n   :members:\n",
        encoding="utf-8",
    )
    (src_dir / "reference" / "api" / "imednet_workflows.rst").write_text(
        "imednet_workflows\n=================\n\n"
        ".. automodule:: imednet_workflows.config_version_control\n"
        "   :members:\n",
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
    assert check_compliance_html_links(html_dir, require_exists=True) is False

    # Corrupting an anchor must fail check_compliance_html_links
    arch_html = html_dir / "explanation" / "architecture.html"
    arch_html.write_text(
        arch_html.read_text(encoding="utf-8").replace(
            "#imednet.models.sdtm.transform_records_to_sdtm_rows", "#broken.anchor"
        ),
        encoding="utf-8",
    )
    assert check_compliance_html_links(html_dir, require_exists=True) is True
