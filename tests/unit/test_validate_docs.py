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

from scripts.build_docs import (
    SCOPED_INHERITED_MEMBERS,
    emit_curated_core_reference,
    scope_apidoc_inherited_members,
)
from scripts.validate_docs import (
    _verify_core_html,
    _verify_pagination_html,
    _verify_sdk_html,
    check_no_duplicate_suppression,
    check_rendered_api_reference,
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
    assert conf._resolve_mermaid_version() == "10.9.1"
