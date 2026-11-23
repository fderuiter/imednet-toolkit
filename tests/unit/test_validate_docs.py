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
    _verify_api_reference_parity,
    _verify_core_html,
    _verify_pagination_html,
    _verify_sdk_html,
    check_cli_snippets,
    check_documented_extras,
    check_no_autodoc_typehints_dependency,
    check_no_duplicate_suppression,
    check_rendered_api_reference,
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
