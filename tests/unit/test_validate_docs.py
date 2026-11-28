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
    write_wheel_manifest,
)
from scripts.validate_docs import (
    RETIRED_GITHUB_LABELS,
    WORKSPACE_PACKAGE_IDS,
    _find_stale_schema_cache_claims,
    _verify_api_reference_parity,
    _verify_core_html,
    _verify_pagination_html,
    _verify_sdk_html,
    check_adr_records,
    check_cli_snippets,
    check_clinical_guides_parity,
    check_compliance_html_links,
    check_diataxis_sidebar_isolation,
    check_documented_extras,
    check_duckdb_relations_parity,
    check_env_vars_documented,
    check_examples_index_parity,
    check_governance_and_issue_templates,
    check_no_autodoc_typehints_dependency,
    check_no_duplicate_suppression,
    check_package_docs,
    check_packages_registered,
    check_pypi_install_tags,
    check_readme_github_pages_routes,
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


def _load_api_reference_ext():
    """Load docs/_ext/api_reference.py directly for isolated helper testing."""
    ext_path = Path(__file__).resolve().parents[2] / "docs" / "_ext" / "api_reference.py"
    spec = importlib.util.spec_from_file_location("_test_api_reference_ext", ext_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_test_api_reference_ext"] = module
    spec.loader.exec_module(module)
    return module


def test_no_autodoc_typehints_dependency_check(tmp_path: Path) -> None:
    """sphinx-autodoc-typehints is uninstalled and flagged if re-added to pyproject.toml or conf.py."""
    repo_root = Path(__file__).resolve().parents[2]
    assert importlib.util.find_spec("sphinx_autodoc_typehints") is None
    assert check_no_autodoc_typehints_dependency(repo_root) is False

    bad_root = tmp_path / "bad_pyproject"
    (bad_root / "docs").mkdir(parents=True)
    (bad_root / "pyproject.toml").write_text(
        '[dependency-groups]\ndocs = ["sphinx>=8.0", "sphinx-autodoc-typehints>=2.0"]\n',
        encoding="utf-8",
    )
    (bad_root / "docs" / "conf.py").write_text(
        'extensions = ["sphinx.ext.autodoc"]\n', encoding="utf-8"
    )
    assert check_no_autodoc_typehints_dependency(bad_root) is True

    bad_conf_root = tmp_path / "bad_conf"
    (bad_conf_root / "docs").mkdir(parents=True)
    (bad_conf_root / "pyproject.toml").write_text(
        '[dependency-groups]\ndocs = ["sphinx>=8.0"]\n',
        encoding="utf-8",
    )
    (bad_conf_root / "docs" / "conf.py").write_text(
        'extensions = ["sphinx.ext.autodoc", "sphinx_autodoc_typehints"]\n',
        encoding="utf-8",
    )
    assert check_no_autodoc_typehints_dependency(bad_conf_root) is True


def test_api_reference_ast_type_checking_and_formatting_helpers() -> None:
    """AST TYPE_CHECKING resolution, PEP 604 unions, TypeVar/ParamSpec, factories, and BaseModel.__init__ skipping."""
    import imednet.plugins as plugins_mod
    from imednet.cli.decorators import P
    from imednet.utils.validators import T
    from imednet_sinks.warehouse import SnowflakeSinkConfig

    api_ext = _load_api_reference_ext()
    conf = _load_docs_conf()

    before_keys = set(plugins_mod.__dict__.keys())
    ns = api_ext.resolve_module_type_checking_ns("imednet.plugins")
    assert set(plugins_mod.__dict__.keys()) == before_keys
    assert "Record" in ns

    rendered_union = conf._format_rst_annotation(str | int | None)
    assert rendered_union == "str | int | None"
    assert "UnionType" not in rendered_union

    assert conf._format_rst_annotation(P) == "P"
    assert conf._format_rst_annotation(T) == "T"
    assert conf._format_rst_annotation(list[T]) == "list[T]"

    assert api_ext.format_factory_call(dict) == "dict()"
    sample_lambda = lambda: ["default"]  # noqa: E731
    assert api_ext.format_factory_call(sample_lambda) == "['default']"

    import inspect

    api_ext.prepare_dataclass_init_signature(SnowflakeSinkConfig)
    sig = inspect.signature(SnowflakeSinkConfig.__init__)
    sig_str = api_ext.format_clean_signature(sig)
    assert "<factory>" not in sig_str
    assert "extra: dict[str, Any] = dict()" in sig_str
    assert "password: str = ..." in sig_str
    assert "password: str = ''" not in sig_str

    fake_app = SimpleNamespace(
        env=SimpleNamespace(
            temp_data={"autodoc:module": "imednet.models.records", "autodoc:class": "Record"}
        )
    )
    assert (
        conf._skip_non_canonical_or_internal(
            fake_app, "class", "__init__", Record.__init__, False, SimpleNamespace()
        )
        is True
    )


def test_sphinx_html_renders_overloads_enums_dataclasses_typeddicts_protocols_and_models(
    tmp_path: Path,
) -> None:
    """Strict Sphinx HTML build renders overloads, Enums, dataclasses, TypedDicts, Protocols, and BaseModels."""
    repo_root = Path(__file__).resolve().parents[2]
    src_dir = tmp_path / "docs"
    api_dir = src_dir / "reference" / "api"
    api_dir.mkdir(parents=True)
    html_dir = tmp_path / "_build" / "html"

    (src_dir / "index.rst").write_text(
        "Test Docs\n=========\n\n"
        ".. meta::\n   :description: Test docs\n\n"
        ".. toctree::\n   :maxdepth: 2\n\n"
        "   reference/api/sample\n",
        encoding="utf-8",
    )
    (src_dir / "404.rst").write_text(":orphan:\n\nNot Found\n=========\n", encoding="utf-8")
    target_modules = (
        "imednet.models.jobs",
        "imednet.orchestration.types",
        "imednet.plugins",
        "imednet.validation.cache",
        "imednet.cli.decorators",
        "imednet.utils.validators",
        "imednet_sinks.warehouse",
        "imednet_sinks.plugin",
        "imednet_workflows.uat.inspector",
    )
    rst_blocks = ["Sample API\n==========\n"]
    for mod_name in target_modules:
        rst_blocks.append(
            f".. automodule:: {mod_name}\n"
            f"   :inherited-members: {SCOPED_INHERITED_MEMBERS}\n"
            "   :members:\n"
            "   :show-inheritance:\n"
        )
    (api_dir / "sample.rst").write_text("\n".join(rst_blocks), encoding="utf-8")

    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "sphinx",
            "-b",
            "html",
            "-W",
            "--keep-going",
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
    assert res.returncode == 0, f"Strict Sphinx failed:\n{res.stdout}\n{res.stderr}"

    built_api = html_dir / "reference" / "api"
    assert _verify_api_reference_parity(built_api, repo_root) is False


@pytest.mark.parametrize(
    ("case_name", "html_body"),
    [
        (
            "empty_class_shell",
            '<dl class="py class"><dt id="pkg.EmptyShell">class pkg.EmptyShell</dt>'
            "<dd><p>Bases: <code>object</code></p></dd></dl>",
        ),
        (
            "missing_enum_member",
            '<dl class="py class"><dt id="imednet.models.jobs.JobState">class JobState</dt>'
            '<dd><dl class="py attribute"><dt id="imednet.models.jobs.JobState.OPEN">'
            "OPEN: str = 'OPEN'</dt><dd></dd></dl></dd></dl>",
        ),
        (
            "leaked_repr_false_password_default",
            '<dl class="py class">'
            '<dt id="imednet_sinks.warehouse.SnowflakeSinkConfig">'
            "class SnowflakeSinkConfig(study_key, password='')</dt>"
            '<dd><dl class="py attribute">'
            '<dt id="imednet_sinks.warehouse.SnowflakeSinkConfig.password">'
            "password: str = ''</dt><dd></dd></dl></dd></dl>",
        ),
        (
            "missing_typeddict_key",
            '<dl class="py class">'
            '<dt id="imednet.orchestration.types.OrchestratorResult">class OrchestratorResult</dt>'
            '<dd><dl class="py attribute">'
            '<dt id="imednet.orchestration.types.OrchestratorResult.study_key">'
            "study_key: str</dt><dd><p>Optional.</p></dd></dl></dd></dl>",
        ),
        (
            "missing_protocol_attribute",
            '<dl class="py class">'
            '<dt id="imednet.plugins.MongoDbSinkConfigProtocol">class MongoDbSinkConfigProtocol</dt>'
            "<dd><p>Protocol docs.</p></dd></dl>",
        ),
        (
            "missing_callable_protocol_call",
            '<dl class="py class">'
            '<dt id="imednet.plugins.PluginProtocol">class PluginProtocol</dt>'
            "<dd><p>Callable protocol without __call__.</p></dd></dl>",
        ),
        (
            "collapsed_single_dt_overload",
            '<dl class="py method">'
            '<dt id="imednet_sinks.plugin.SinksNamespace.export_to_mongodb">'
            "export_to_mongodb(*args, **kwargs)</dt><dd><p>Export.</p></dd></dl>",
        ),
        (
            "inherited_basemodel_init_leak",
            '<dl class="py class"><dt id="imednet.models.records.Record">class Record</dt>'
            '<dd><dl class="py method"><dt id="imednet.models.records.Record.__init__">'
            "__init__(**data)</dt><dd></dd></dl></dd></dl>",
        ),
        (
            "forbidden_pydantic_validation_error",
            "<p>Raises pydantic_core.ValidationError when invalid.</p>",
        ),
        (
            "forbidden_union_type_token",
            "<p>Returns UnionType[str, int, None].</p>",
        ),
        (
            "forbidden_factory_token",
            "<p>extra=&lt;factory&gt;</p>",
        ),
        (
            "forbidden_tilde_paramspec",
            '<dl class="py function"><dt id="pkg.fn">fn(*args: ~P.args)</dt><dd><p>Doc.</p></dd></dl>',
        ),
        (
            "forbidden_tilde_typevar",
            '<dl class="py function"><dt id="pkg.fn2">fn2(items: list[~T])</dt><dd><p>Doc.</p></dd></dl>',
        ),
    ],
)
def test_verify_api_reference_parity_rejects_regressions(
    tmp_path: Path, case_name: str, html_body: str
) -> None:
    """_verify_api_reference_parity and check_rendered_api_reference fail on every regression pattern."""
    repo_root = Path(__file__).resolve().parents[2]
    api_dir = tmp_path / case_name / "reference" / "api"
    api_dir.mkdir(parents=True)
    (api_dir / "imednet.html").write_text(
        f"<html><body>{html_body}</body></html>\n",
        encoding="utf-8",
    )
    assert _verify_api_reference_parity(api_dir, repo_root) is True
    assert check_rendered_api_reference(tmp_path / case_name) is True


def test_repo_package_docs_and_governance_validators_pass() -> None:
    """Repository manifests, docs, and issue templates pass check_package_docs and governance checks."""
    repo_root = Path(__file__).resolve().parents[2]
    assert check_package_docs(repo_root) is False
    assert check_governance_and_issue_templates(repo_root) is False


def test_check_package_docs_detects_manifest_and_changelog_drift(tmp_path: Path) -> None:
    """check_package_docs flags missing readme/license/classifiers and obsolete CHANGELOG tokens."""
    pkg_dir = tmp_path / "packages" / "sample"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "README.md").write_text("# Sample\n", encoding="utf-8")
    (pkg_dir / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    valid_manifest = (
        '[project]\nname = "imednet-sample"\nversion = "0.1.0"\n'
        'readme = "README.md"\nlicense = "MIT"\n'
        "classifiers = [\n"
        '  "Programming Language :: Python :: 3.11",\n'
        '  "Programming Language :: Python :: 3.12",\n'
        '  "Programming Language :: Python :: 3.13",\n'
        "]\n"
    )
    (pkg_dir / "pyproject.toml").write_text(valid_manifest, encoding="utf-8")
    changelogs_dir = tmp_path / "docs" / "reference" / "changelogs"
    changelogs_dir.mkdir(parents=True)
    (changelogs_dir / "index.rst").write_text(
        "Changelogs\n==========\n\n.. toctree::\n\n   sample\n",
        encoding="utf-8",
    )
    (changelogs_dir / "sample.md").write_text(
        "```{include} ../../../packages/sample/CHANGELOG.md\n```\n",
        encoding="utf-8",
    )
    ref_dir = tmp_path / "docs" / "reference"
    (ref_dir / "compatibility.rst").write_text(
        "See :doc:`changelogs/index`.\n",
        encoding="utf-8",
    )
    (ref_dir / "contributing.rst").write_text(
        "See :doc:`changelogs/index`.\n",
        encoding="utf-8",
    )
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\nSee packages/sample/CHANGELOG.md.\n", encoding="utf-8")

    assert check_package_docs(tmp_path) is False

    # Obsolete token in root CHANGELOG.md
    changelog.write_text(
        "# Changelog\n\nSee packages/sample/CHANGELOG.md and docs/contributing.rst.\n",
        encoding="utf-8",
    )
    assert check_package_docs(tmp_path) is True
    changelog.write_text("# Changelog\n\nSee packages/sample/CHANGELOG.md.\n", encoding="utf-8")

    # Missing readme / license / classifiers in pyproject.toml
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "imednet-sample"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    assert check_package_docs(tmp_path) is True


def test_check_package_docs_enforces_wheel_metadata_and_absolute_links(tmp_path: Path) -> None:
    """check_package_docs requires readme/license/classifiers in pyproject.toml and rejects relative links in package READMEs."""
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n- [demo](packages/demo-pkg/CHANGELOG.md)\n",
        encoding="utf-8",
    )
    changelogs_dir = tmp_path / "docs" / "reference" / "changelogs"
    changelogs_dir.mkdir(parents=True)
    (changelogs_dir / "index.rst").write_text(
        "Changelogs\n==========\n\n.. toctree::\n\n   demo-pkg\n",
        encoding="utf-8",
    )
    (changelogs_dir / "demo-pkg.md").write_text(
        "# demo-pkg\n\n```{include} ../../../packages/demo-pkg/CHANGELOG.md\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "reference" / "compatibility.rst").write_text(
        "Compatibility\n=============\n\nSee :doc:`changelogs/index`.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "reference" / "contributing.rst").write_text(
        "Contributing\n============\n\nSee :doc:`changelogs/index`.\n",
        encoding="utf-8",
    )
    pkg_dir = tmp_path / "packages" / "demo-pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "CHANGELOG.md").write_text("# Demo Changelog\n", encoding="utf-8")
    (pkg_dir / "README.md").write_text(
        "# demo-pkg\n\nSee [Docs](https://fderuiter.github.io/imednet-toolkit/).\n",
        encoding="utf-8",
    )
    valid_manifest = (
        '[project]\nname = "demo"\nreadme = "README.md"\nlicense = "MIT"\n'
        "classifiers = [\n"
        '  "Programming Language :: Python :: 3.11",\n'
        '  "Programming Language :: Python :: 3.12",\n'
        '  "Programming Language :: Python :: 3.13",\n'
        "]\n"
    )
    (pkg_dir / "pyproject.toml").write_text(valid_manifest, encoding="utf-8")
    assert check_package_docs(tmp_path) is False

    # Relative link in package README fails
    (pkg_dir / "README.md").write_text(
        "# demo-pkg\n\nSee [Example](../../examples/quick_start.py).\n",
        encoding="utf-8",
    )
    assert check_package_docs(tmp_path) is True

    # Restore valid README, remove license -> fails
    (pkg_dir / "README.md").write_text(
        "# demo-pkg\n\nSee [Docs](https://fderuiter.github.io/imednet-toolkit/).\n",
        encoding="utf-8",
    )
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nreadme = "README.md"\n',
        encoding="utf-8",
    )
    assert check_package_docs(tmp_path) is True


def test_check_governance_and_issue_templates_detects_retired_urls_labels_and_dropdowns(
    tmp_path: Path,
) -> None:
    """check_governance_and_issue_templates rejects retired URLs, nonexistent labels, and stale dropdowns."""
    templates_dir = tmp_path / ".github" / "ISSUE_TEMPLATE"
    templates_dir.mkdir(parents=True)
    docs_ref = tmp_path / "docs" / "reference"
    docs_ref.mkdir(parents=True)
    docs_agents = tmp_path / "docs" / "agents"
    docs_agents.mkdir(parents=True)

    # Excluded docs/agents/ directory may mention legacy labels in translation tables
    (docs_agents / "triage-labels.md").write_text(
        "Legacy status:needs-info maps to question.\n",
        encoding="utf-8",
    )

    pkg_opts = "\n".join(f"        - {pkg}" for pkg in WORKSPACE_PACKAGE_IDS)
    valid_form = (
        "name: Bug Report\n"
        "description: Report a bug\n"
        "body:\n"
        "  - type: dropdown\n"
        "    id: area\n"
        "    attributes:\n"
        "      label: Area\n"
        "      options:\n"
        "        - core\n"
        "        - workflows\n"
        "  - type: dropdown\n"
        "    id: package\n"
        "    attributes:\n"
        "      label: Package\n"
        "      options:\n"
        f"{pkg_opts}\n"
    )
    form_path = templates_dir / "bug_report.yml"
    form_path.write_text(valid_form, encoding="utf-8")
    (templates_dir / "config.yml").write_text(
        "blank_issues_enabled: false\n",
        encoding="utf-8",
    )
    issue_doc = docs_ref / "issue_management.rst"
    issue_doc.write_text("Use ``question`` and ``area:workflows``.\n", encoding="utf-8")
    coc = tmp_path / "CODE_OF_CONDUCT.md"
    coc.write_text("https://github.com/fderuiter/imednet-toolkit/issues\n", encoding="utf-8")

    assert check_governance_and_issue_templates(tmp_path) is False

    # Retired repo URL
    coc.write_text("https://github.com/fderuiter/imednet-python-sdk/issues\n", encoding="utf-8")
    assert check_governance_and_issue_templates(tmp_path) is True
    coc.write_text("https://github.com/fderuiter/imednet-toolkit/issues\n", encoding="utf-8")

    # Every retired GitHub label is blocked in docs/reference/
    for retired_label in RETIRED_GITHUB_LABELS:
        issue_doc.write_text(f"Do not apply ``{retired_label}``.\n", encoding="utf-8")
        assert check_governance_and_issue_templates(tmp_path) is True
    issue_doc.write_text("Use ``question`` and ``area:workflows``.\n", encoding="utf-8")

    # Invalid area option in issue template
    form_path.write_text(valid_form.replace("- workflows", "- workflows-plugin"), encoding="utf-8")
    assert check_governance_and_issue_templates(tmp_path) is True

    # Missing workspace package in issue template
    form_path.write_text(valid_form.replace("        - plugins-tui\n", ""), encoding="utf-8")
    assert check_governance_and_issue_templates(tmp_path) is True


def test_check_readme_github_pages_routes_resolves_pages_and_fragments(tmp_path: Path) -> None:
    """check_readme_github_pages_routes rejects raw .rst links and verifies HTML files and #fragments."""
    repo = tmp_path / "repo"
    pkg_dir = repo / "packages" / "core"
    html_dir = tmp_path / "html"
    (html_dir / "tutorials").mkdir(parents=True)
    pkg_dir.mkdir(parents=True)

    (html_dir / "index.html").write_text(
        '<html><body><section id="interactive-showcase"></section>'
        '<div id="documentation-overview"></div></body></html>',
        encoding="utf-8",
    )
    (html_dir / "tutorials" / "quick_start.html").write_text(
        '<html><body><h1 id="quick-start">Quick Start</h1></body></html>',
        encoding="utf-8",
    )

    (repo / "README.md").write_text(
        "# Root\n\n"
        "- [Showcase](https://fderuiter.github.io/imednet-toolkit/#interactive-showcase)\n"
        "- [Tutorials](https://fderuiter.github.io/imednet-toolkit/#documentation-overview)\n"
        "- [Quick Start](https://fderuiter.github.io/imednet-toolkit/tutorials/quick_start.html#quick-start)\n",
        encoding="utf-8",
    )
    (pkg_dir / "README.md").write_text(
        "# Core\n\nFull docs: <https://fderuiter.github.io/imednet-toolkit/>\n",
        encoding="utf-8",
    )

    assert check_readme_github_pages_routes(html_dir, require_exists=True, repo_root=repo) is False

    # Broken #fragment fails
    (pkg_dir / "README.md").write_text(
        "# Core\n\n[Missing Anchor](https://fderuiter.github.io/imednet-toolkit/#nonexistent)\n",
        encoding="utf-8",
    )
    assert check_readme_github_pages_routes(html_dir, require_exists=True, repo_root=repo) is True

    # Raw .rst link fails even without HTML dir
    (pkg_dir / "README.md").write_text(
        "# Core\n\n[Raw RST](docs/tutorials/quick_start.rst)\n",
        encoding="utf-8",
    )
    assert check_readme_github_pages_routes(None, repo_root=repo) is True


def test_check_pypi_install_tags_enforces_git_release_tags(tmp_path: Path) -> None:
    """check_pypi_install_tags allows published packages/extras in PyPI blocks and requires source checkout for untagged ones."""
    repo = tmp_path / "git_repo"
    core_dir = repo / "packages" / "core"
    tui_dir = repo / "packages" / "plugins-tui"
    core_dir.mkdir(parents=True)
    tui_dir.mkdir(parents=True)

    (core_dir / "pyproject.toml").write_text(
        '[project]\nname = "imednet"\nversion = "1.1.0"\n'
        "[project.optional-dependencies]\nexport = []\n",
        encoding="utf-8",
    )
    (tui_dir / "pyproject.toml").write_text(
        '[project]\nname = "imednet-tui"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "tag", "imednet-v1.1.0"], cwd=repo, check=True, capture_output=True)

    # Add unreleased extra 'tui' on HEAD after imednet-v1.1.0 tag
    (core_dir / "pyproject.toml").write_text(
        '[project]\nname = "imednet"\nversion = "1.1.0"\n'
        "[project.optional-dependencies]\nexport = []\ntui = []\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "## Installation\n\n### Published PyPI Releases\n\n"
        '```bash\npip install imednet\npip install "imednet[export]"\n```\n\n'
        "### Source Checkouts\n\n"
        '```bash\npip install -e ./packages/plugins-tui -e "./packages/core[tui]"\n```\n',
        encoding="utf-8",
    )
    assert check_pypi_install_tags(repo) is False

    # Advertising unreleased imednet[tui] in PyPI block fails
    (repo / "README.md").write_text(
        '## Installation\n\n```bash\npip install "imednet[tui]"\n```\n',
        encoding="utf-8",
    )
    assert check_pypi_install_tags(repo) is True

    # Advertising untagged imednet-tui in PyPI block fails
    (repo / "README.md").write_text(
        "## Installation\n\n```bash\npip install imednet-tui\n```\n",
        encoding="utf-8",
    )
    assert check_pypi_install_tags(repo) is True


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
    assert check_governance_and_issue_templates() is False
    assert check_pypi_install_tags() is False
    assert check_readme_github_pages_routes(None) is False
    assert check_release_please_parity() is False
    assert check_examples_index_parity() is False
    assert check_diataxis_sidebar_isolation() is False
    assert check_adr_records() is False
    assert check_env_vars_documented() is False
    assert check_sdtm_and_phi_parity() is False
    assert check_clinical_guides_parity() is False
    assert check_duckdb_relations_parity() is False

    empty_root = tmp_path / "empty_workspace"
    empty_root.mkdir()
    assert check_todos_in_init(repo_root=empty_root) is True
    assert check_documented_extras(empty_root) is True
    assert check_cli_snippets(empty_root) is True
    assert check_packages_registered(empty_root) is True
    assert check_package_docs(empty_root) is True
    assert check_governance_and_issue_templates(empty_root) is True
    assert check_pypi_install_tags(empty_root) is True
    assert check_readme_github_pages_routes(None, repo_root=empty_root) is True
    assert check_release_please_parity(empty_root) is True
    assert check_examples_index_parity(empty_root) is True
    assert check_diataxis_sidebar_isolation(empty_root) is True
    assert check_adr_records(empty_root) is True
    assert check_env_vars_documented(empty_root) is True
    assert check_sdtm_and_phi_parity(empty_root) is True
    assert check_clinical_guides_parity(empty_root) is True
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
    """check_release_please_parity enforces dynamic wheels, manifest.json, and extra-files parity."""
    import json

    for pkg, ver in (
        ("packages/core", "1.2.0"),
        ("packages/plugins-workflows", "1.2.0"),
        ("packages/plugins-sinks", "1.1.0"),
        ("packages/plugins-tui", "1.0.0"),
    ):
        pkg_dir = tmp_path / pkg
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "pyproject.toml").write_text(
            f'[project]\nname = "demo"\nversion = "{ver}"\n',
            encoding="utf-8",
        )

    init_py = tmp_path / "packages" / "core" / "src" / "imednet" / "__init__.py"
    init_py.parent.mkdir(parents=True)
    init_py.write_text('__version__ = "1.2.0"  # x-release-please-version\n', encoding="utf-8")

    thebe_js = tmp_path / "docs" / "_static" / "thebe_pyodide.js"
    thebe_js.parent.mkdir(parents=True)
    clean_thebe_js = (
        "const docRootUrl = new URL('./', 'http://localhost/').href;\n"
        "const wheelManifestUrl = new URL('_static/wheels/manifest.json', docRootUrl).href;\n"
    )
    thebe_js.write_text(clean_thebe_js, encoding="utf-8")

    conf_py = tmp_path / "docs" / "conf.py"
    valid_conf_py = (
        "_PYODIDE_WHEEL_SPECS = (\n"
        "    ('imednet', 'core'),\n"
        "    ('imednet_workflows', 'plugins-workflows'),\n"
        "    ('imednet_plugins_sinks', 'plugins-sinks'),\n"
        ")\n"
        "def _resolve_pyodide_wheels() -> list[str]:\n"
        "    resolved: list[str] = []\n"
        "    for dist_prefix, pkg_dir_name in _PYODIDE_WHEEL_SPECS:\n"
        "        pyproject_file = Path(_REPO_ROOT) / 'packages' / pkg_dir_name / 'pyproject.toml'\n"
        "        with pyproject_file.open('rb') as fh:\n"
        "            ver = str(tomllib.load(fh)['project']['version'])\n"
        "        resolved.append(f'_static/wheels/{dist_prefix}-{ver}-py3-none-any.whl')\n"
        "    return resolved\n"
        "pyodide_config = {'wheels': _resolve_pyodide_wheels()}\n"
    )
    conf_py.write_text(valid_conf_py, encoding="utf-8")

    manifest = {
        "packages/core": "1.2.0",
        "packages/plugins-workflows": "1.2.0",
        "packages/plugins-sinks": "1.1.0",
        "packages/plugins-tui": "1.0.0",
    }
    (tmp_path / ".release-please-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    valid_cfg = {
        "packages": {
            "packages/core": {
                "extra-files": [
                    {"type": "generic", "path": "/packages/core/src/imednet/__init__.py"},
                ]
            },
            "packages/plugins-workflows": {},
            "packages/plugins-sinks": {},
            "packages/plugins-tui": {"initial-version": "1.0.0"},
        }
    }
    (tmp_path / "release-please-config.json").write_text(json.dumps(valid_cfg), encoding="utf-8")
    assert check_release_please_parity(tmp_path) is False

    # Drift in __init__.py version fails parity
    init_py.write_text('__version__ = "1.0.0"  # x-release-please-version\n', encoding="utf-8")
    assert check_release_please_parity(tmp_path) is True
    init_py.write_text('__version__ = "1.2.0"  # x-release-please-version\n', encoding="utf-8")

    # Generic updater pointing to .js path in release-please-config.json fails parity
    bad_js_cfg = {
        "packages": {
            **valid_cfg["packages"],
            "packages/core": {
                "extra-files": [
                    {"type": "generic", "path": "/packages/core/src/imednet/__init__.py"},
                    {"type": "generic", "path": "/docs/_static/thebe_pyodide.js"},
                ]
            },
        }
    }
    (tmp_path / "release-please-config.json").write_text(json.dumps(bad_js_cfg), encoding="utf-8")
    assert check_release_please_parity(tmp_path) is True
    (tmp_path / "release-please-config.json").write_text(json.dumps(valid_cfg), encoding="utf-8")

    # Hardcoded .whl version or x-release-please-version in thebe_pyodide.js fails parity
    thebe_js.write_text(
        clean_thebe_js + '"_static/wheels/imednet-1.2.0-py3-none-any.whl"\n',
        encoding="utf-8",
    )
    assert check_release_please_parity(tmp_path) is True
    thebe_js.write_text(
        clean_thebe_js + "// x-release-please-version\n",
        encoding="utf-8",
    )
    assert check_release_please_parity(tmp_path) is True
    thebe_js.write_text(clean_thebe_js, encoding="utf-8")

    # Drift in docs/conf.py _resolve_pyodide_wheels() fails parity
    conf_py.write_text(
        "def _resolve_pyodide_wheels() -> list[str]:\n"
        "    return ['_static/wheels/imednet-1.0.0-py3-none-any.whl']\n"
        "pyodide_config = {'wheels': _resolve_pyodide_wheels()}\n",
        encoding="utf-8",
    )
    assert check_release_please_parity(tmp_path) is True
    conf_py.write_text(valid_conf_py, encoding="utf-8")

    # --require-html validates built _static/wheels/manifest.json and staged wheel files
    built_html_dir = tmp_path / "docs" / "_build" / "html"
    assert check_release_please_parity(tmp_path, html_dir=built_html_dir, require_html=True) is True

    built_wheels_dir = built_html_dir / "_static" / "wheels"
    built_wheels_dir.mkdir(parents=True)
    for whl_name in (
        "imednet-1.2.0-py3-none-any.whl",
        "imednet_workflows-1.2.0-py3-none-any.whl",
        "imednet_plugins_sinks-1.1.0-py3-none-any.whl",
        "imednet_tui-1.0.0-py3-none-any.whl",
    ):
        (built_wheels_dir / whl_name).write_bytes(b"PK")

    manifest_file = write_wheel_manifest(built_wheels_dir, repo_root=tmp_path)
    manifest_payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest_payload == {
        "wheels": [
            "_static/wheels/imednet-1.2.0-py3-none-any.whl",
            "_static/wheels/imednet_workflows-1.2.0-py3-none-any.whl",
            "_static/wheels/imednet_plugins_sinks-1.1.0-py3-none-any.whl",
        ]
    }
    assert not (tmp_path / "docs" / "_static" / "wheels").exists()
    assert (
        check_release_please_parity(tmp_path, html_dir=built_html_dir, require_html=True) is False
    )

    # Non-browser wheel in _static/wheels/manifest.json fails --require-html check
    manifest_file.write_text(
        json.dumps(
            {
                "wheels": [
                    *manifest_payload["wheels"],
                    "_static/wheels/imednet_tui-1.0.0-py3-none-any.whl",
                ]
            }
        ),
        encoding="utf-8",
    )
    assert check_release_please_parity(tmp_path, html_dir=built_html_dir, require_html=True) is True


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
    """Sphinx renders RecordRevision.validate_chain and resolves imednet.spi.* and imednet.datadict.* links."""
    repo_root = Path(__file__).resolve().parents[2]
    src_dir = tmp_path / "docs_src"
    html_dir = tmp_path / "html_out"
    for sub in ("how-to", "explanation", "reference/api"):
        (src_dir / sub).mkdir(parents=True)

    (src_dir / "index.rst").write_text(
        "Index\n=====\n\n.. toctree::\n\n"
        "   how-to/audit_compliance\n"
        "   how-to/record_mapping\n"
        "   how-to/data_dictionary\n"
        "   explanation/architecture\n"
        "   explanation/api_overview\n"
        "   explanation/schema_validation\n"
        "   reference/governance\n"
        "   reference/api/imednet.models\n"
        "   reference/api/imednet.datadict\n"
        "   reference/api/imednet.validation\n"
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
        "Call :func:`~imednet.spi.models.transform_records_to_sdtm_rows`, "
        ":func:`~imednet.spi.sink_base.evaluate_standards_readiness`, and "
        ":class:`~imednet.integrations.enrichment.EnrichmentPipeline`.\n",
        encoding="utf-8",
    )
    (src_dir / "how-to" / "data_dictionary.rst").write_text(
        "Data Dictionary\n===============\n\n"
        "Use :class:`~imednet.datadict.RangeCheckConstraint` and "
        ":func:`~imednet.datadict.validate_regex_pattern`.\n",
        encoding="utf-8",
    )
    (src_dir / "explanation" / "schema_validation.rst").write_text(
        "Schema Validation\n=================\n\n"
        "Use :class:`~imednet.validation.cache.BaseSchemaCache` and "
        ":func:`~imednet.validation.cache.validate_record_data`.\n",
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
    (src_dir / "reference" / "api" / "imednet.datadict.rst").write_text(
        "imednet.datadict\n================\n\n"
        ".. automodule:: imednet.datadict\n"
        "   :members:\n\n"
        ".. automodule:: imednet.datadict.logic\n"
        "   :members:\n",
        encoding="utf-8",
    )
    (src_dir / "reference" / "api" / "imednet.validation.rst").write_text(
        "imednet.validation\n==================\n\n"
        ".. automodule:: imednet.validation.cache\n"
        "   :members:\n",
        encoding="utf-8",
    )
    (src_dir / "reference" / "api" / "imednet.integrations.rst").write_text(
        "imednet.integrations\n====================\n\n"
        ".. automodule:: imednet.integrations.sink_base\n"
        "   :members:\n\n"
        ".. automodule:: imednet.integrations.enrichment\n"
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
    orig_arch = arch_html.read_text(encoding="utf-8")
    arch_html.write_text(
        orig_arch.replace("#imednet.models.sdtm.transform_records_to_sdtm_rows", "#broken.anchor"),
        encoding="utf-8",
    )
    assert check_compliance_html_links(html_dir, require_exists=True) is True
    arch_html.write_text(orig_arch, encoding="utf-8")

    # Corrupting a datadict re-export anchor must also fail check_compliance_html_links
    dd_html = html_dir / "how-to" / "data_dictionary.html"
    dd_html.write_text(
        dd_html.read_text(encoding="utf-8").replace(
            "#imednet.datadict.logic.RangeCheckConstraint", "#broken.range_check"
        ),
        encoding="utf-8",
    )
    assert check_compliance_html_links(html_dir, require_exists=True) is True


def test_parse_html_file_extracts_headerlink_source_and_backlink_aria_labels(
    tmp_path: Path,
) -> None:
    """_parse_html_file extracts target-specific aria-labels and flags unlabeled links."""
    from scripts.validate_docs import _parse_html_file

    api_sample = tmp_path / "sample.html"
    api_sample.write_text(
        "<html><body>"
        '<h1>Auth<a class="headerlink" href="#auth" aria-label="Permalink to Auth">¶</a></h1>'
        '<dl class="py class"><dt id="imednet.auth.api_key.ApiKeyAuth">'
        "class ApiKeyAuth"
        '<a class="reference internal viewcode-source-link" '
        'href="../../_modules/imednet/auth/api_key.html#ApiKeyAuth" '
        'aria-label="[source] for imednet.auth.api_key.ApiKeyAuth">'
        '<span class="viewcode-link"><span class="pre">[source]</span></span></a>'
        '<a class="headerlink" href="#imednet.auth.api_key.ApiKeyAuth" '
        'aria-label="Permalink to imednet.auth.api_key.ApiKeyAuth">¶</a>'
        "</dt><dd><p>Auth strategy.</p></dd></dl>"
        '<a class="reference internal" href="../../_modules/imednet/sdk.html#ImednetSDK">'
        '<span class="viewcode-link"><span class="pre">[source]</span></span></a>'
        "</body></html>\n",
        encoding="utf-8",
    )
    parsed = _parse_html_file(api_sample)
    assert parsed.headerlinks == [
        ("#auth", "Permalink to Auth"),
        ("#imednet.auth.api_key.ApiKeyAuth", "Permalink to imednet.auth.api_key.ApiKeyAuth"),
    ]
    assert parsed.source_links == [
        (
            "../../_modules/imednet/auth/api_key.html#ApiKeyAuth",
            "[source] for imednet.auth.api_key.ApiKeyAuth",
            True,
        ),
        ("../../_modules/imednet/sdk.html#ImednetSDK", None, False),
    ]

    mod_sample = tmp_path / "mod.html"
    mod_sample.write_text(
        "<html><body>"
        '<div class="viewcode-block" id="ApiKeyAuth">'
        '<a class="viewcode-back" '
        'href="../../../../reference/api/imednet.auth.html#imednet.auth.api_key.ApiKeyAuth" '
        'aria-label="[docs] for imednet.auth.api_key.ApiKeyAuth">[docs]</a>'
        "</div>"
        '<a class="viewcode-back" href="../../../../reference/api/imednet.auth.html#Raw">[docs]</a>'
        "</body></html>\n",
        encoding="utf-8",
    )
    mod_parsed = _parse_html_file(mod_sample)
    assert mod_parsed.backlink_labels == [
        (
            "../../../../reference/api/imednet.auth.html#imednet.auth.api_key.ApiKeyAuth",
            "[docs] for imednet.auth.api_key.ApiKeyAuth",
        ),
        ("../../../../reference/api/imednet.auth.html#Raw", None),
    ]


def test_sphinx_mini_build_emits_accessible_link_labels_and_rejects_regressions(
    tmp_path: Path,
) -> None:
    """Mini Sphinx HTML build emits aria-labels on ¶, [source], and [docs] links, and validate_docs catches regressions."""
    import shutil

    from scripts.validate_docs import _parse_html_file

    repo_root = Path(__file__).resolve().parents[2]
    built_html = repo_root / "docs" / "_build" / "html"
    if not (built_html / "reference" / "api" / "imednet.models.html").exists():
        pytest.skip("docs/_build/html not populated in this isolated test run")

    assert check_rendered_api_reference(built_html) is False

    # Copy minimal subset needed by check_rendered_api_reference into tmp_path to test regression detection
    subset_dir = tmp_path / "html_subset"
    (subset_dir / "reference" / "api").mkdir(parents=True)
    (subset_dir / "_modules" / "imednet" / "auth").mkdir(parents=True)
    for api_name in (
        "imednet.models.html",
        "imednet.endpoints.html",
        "imednet.html",
        "imednet.pagination.html",
        "imednet.core.html",
        "imednet.auth.html",
    ):
        shutil.copy2(
            built_html / "reference" / "api" / api_name,
            subset_dir / "reference" / "api" / api_name,
        )
    shutil.copy2(built_html / "_modules" / "index.html", subset_dir / "_modules" / "index.html")
    shutil.copy2(
        built_html / "_modules" / "imednet" / "auth" / "api_key.html",
        subset_dir / "_modules" / "imednet" / "auth" / "api_key.html",
    )
    # Verify parsed subset has all 3 link categories populated before mutating
    auth_parsed = _parse_html_file(subset_dir / "reference" / "api" / "imednet.auth.html")
    mod_parsed = _parse_html_file(subset_dir / "_modules" / "imednet" / "auth" / "api_key.html")
    assert auth_parsed.headerlinks and auth_parsed.source_links and mod_parsed.backlink_labels

    # 1. Generic headerlink aria-label fails check_rendered_api_reference
    auth_page = subset_dir / "reference" / "api" / "imednet.auth.html"
    orig_auth = auth_page.read_text(encoding="utf-8")
    auth_page.write_text(
        orig_auth.replace(
            'aria-label="Permalink to imednet.auth.api_key.ApiKeyAuth"',
            'aria-label="Permalink to this definition"',
            1,
        ),
        encoding="utf-8",
    )
    assert check_rendered_api_reference(subset_dir) is True
    auth_page.write_text(orig_auth, encoding="utf-8")

    # 2. Missing [source] symbol aria-label fails check_rendered_api_reference
    auth_page.write_text(
        orig_auth.replace(
            'aria-label="[source] for imednet.auth.api_key.ApiKeyAuth"',
            'aria-label="[source]"',
            1,
        ),
        encoding="utf-8",
    )
    assert check_rendered_api_reference(subset_dir) is True
    auth_page.write_text(orig_auth, encoding="utf-8")

    # 3. Missing [docs] backlink aria-label fails check_rendered_api_reference
    mod_page = subset_dir / "_modules" / "imednet" / "auth" / "api_key.html"
    orig_mod = mod_page.read_text(encoding="utf-8")
    mod_page.write_text(
        orig_mod.replace(
            'aria-label="[docs] for imednet.auth.api_key.ApiKeyAuth"',
            'aria-label="[docs]"',
            1,
        ),
        encoding="utf-8",
    )
    assert check_rendered_api_reference(subset_dir) is True
    mod_page.write_text(orig_mod, encoding="utf-8")


def test_check_clinical_guides_parity_passes_repo_and_rejects_drift(tmp_path: Path) -> None:
    """check_clinical_guides_parity fails when validation policies, regex caps, exports, or scoping rules drift."""
    repo_root = Path(__file__).resolve().parents[2]
    assert check_clinical_guides_parity(repo_root) is False

    rel_files = (
        "docs/explanation/schema_validation.rst",
        "docs/how-to/data_dictionary.rst",
        "docs/how-to/caching.rst",
        "docs/how-to/record_mapping.rst",
        "packages/core/src/imednet/datadict/__init__.py",
    )
    originals: dict[str, str] = {}
    for rel in rel_files:
        src_text = (repo_root / rel).read_text(encoding="utf-8")
        originals[rel] = src_text
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src_text, encoding="utf-8")

    assert check_clinical_guides_parity(tmp_path) is False

    def _assert_mutation_fails(rel: str, mutated_text: str) -> None:
        target = tmp_path / rel
        target.write_text(mutated_text, encoding="utf-8")
        try:
            assert check_clinical_guides_parity(tmp_path) is True, (
                f"Expected check_clinical_guides_parity to fail on mutation to {rel}"
            )
        finally:
            target.write_text(originals[rel], encoding="utf-8")

    # 1. Stale BaseSchemaCache skip claims fail parity across all three schema guides
    _assert_mutation_fails(
        "docs/explanation/schema_validation.rst",
        originals["docs/explanation/schema_validation.rst"]
        + "\nBaseSchemaCache returns early—skipping required policies.\n",
    )
    _assert_mutation_fails(
        "docs/explanation/schema_validation.rst",
        originals["docs/explanation/schema_validation.rst"].replace(
            "   * - Required Variable policies (``Yes`` raises error; ``Auto Query`` logs warning) on active Variables\n     - Enforced",
            "   * - Required Variable policies (``Yes`` raises error; ``Auto Query`` logs warning) on active Variables\n     - Skipped",
        ),
    )
    _assert_mutation_fails(
        "docs/how-to/caching.rst",
        originals["docs/how-to/caching.rst"]
        + "\n- **Skipped by** ``SchemaCache``: Required Variable policies (``Yes`` and ``Auto Query``).\n",
    )
    _assert_mutation_fails(
        "docs/how-to/data_dictionary.rst",
        originals["docs/how-to/data_dictionary.rst"]
        + "\nUnlike SchemaCache (which only checks Form existence, unknown Variables, and basic scalar types).\n",
    )

    # 2. Removing masked_phi / ***MASKED*** or _retired_form_variables fails parity
    _assert_mutation_fails(
        "docs/explanation/schema_validation.rst",
        originals["docs/explanation/schema_validation.rst"].replace(
            "``***MASKED***``", "``REDACTED``"
        ),
    )
    _assert_mutation_fails(
        "docs/explanation/schema_validation.rst",
        originals["docs/explanation/schema_validation.rst"].replace(
            "``_retired_form_variables``", "``_legacy_vars``"
        ),
    )

    # 3. Mutating regex caps (512, 256, 1024), unsafe_regex, or ODM comparators fails parity
    for token, replacement in (
        ("``512``", "``999``"),
        ("``256``", "``128``"),
        ("``1024``", "``4096``"),
        ("unsafe_regex", "bad_pattern"),
        ("``NOTIN``", "``EXCL``"),
        ("range_check_soft", "soft_warn"),
    ):
        _assert_mutation_fails(
            "docs/how-to/data_dictionary.rst",
            originals["docs/how-to/data_dictionary.rst"].replace(token, replacement),
        )

    # 4. Omitting RangeCheckConstraint or validate_regex_pattern from imednet.datadict.__all__ fails parity
    _assert_mutation_fails(
        "packages/core/src/imednet/datadict/__init__.py",
        originals["packages/core/src/imednet/datadict/__init__.py"].replace(
            '"RangeCheckConstraint",\n', ""
        ),
    )
    _assert_mutation_fails(
        "packages/core/src/imednet/datadict/__init__.py",
        originals["packages/core/src/imednet/datadict/__init__.py"].replace(
            '"validate_regex_pattern",\n', ""
        ),
    )

    # 5. Mutating EnrichmentPipeline scoping, fallback_value, or domain.variable terminology fails parity
    for token, replacement in (
        ("``(domain, source_form_key, source_variable_name)``", "``(source_variable_name,)``"),
        ("``fallback_value``", "``default_val``"),
        ("``domain.variable``", "``raw_var``"),
        ('domain="AE"', 'study="STUDY"'),
        ('form_key="F_AE"', 'site="SITE"'),
    ):
        _assert_mutation_fails(
            "docs/how-to/record_mapping.rst",
            originals["docs/how-to/record_mapping.rst"].replace(token, replacement),
        )


def test_find_stale_schema_cache_claims_handles_multiline_and_adversarial_inputs_linearly() -> None:
    """_find_stale_schema_cache_claims detects multiline RST bullets without regex backtracking."""
    import time

    assert _find_stale_schema_cache_claims(
        "- **Skipped by** ``BaseSchemaCache``:\n"
        "  Choice codelist membership, and Required\n"
        "  Variable policies.\n"
    ) == ["Skipped by bullet listing Required Variable policies"]

    assert (
        _find_stale_schema_cache_claims(
            "- **Skipped by** ``BaseSchemaCache``:\n"
            "  Choice codelist membership and XML rules.\n\n"
            "Active Required Variable policies are enforced.\n"
        )
        == []
    )

    # Adversarial CodeQL ReDoS inputs with thousands of repeated '  \n' / continuation lines run in O(N)
    adversarial_blank_lines = "- **skipped by**\n" + ("  \n" * 5000) + "Required Variable\n"
    adversarial_continuations = "- **skipped by**\n" + ("  item\n" * 5000) + "end\n"

    start = time.perf_counter()
    assert _find_stale_schema_cache_claims(adversarial_blank_lines) == []
    assert _find_stale_schema_cache_claims(adversarial_continuations) == []
    elapsed = time.perf_counter() - start
    assert elapsed < 0.25, f"Expected linear-time scan (<0.25s), took {elapsed:.3f}s"
