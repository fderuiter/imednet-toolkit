"""Tests for contract validation across namespaces and plugins."""

import ast
import dataclasses
import importlib.util
import inspect
import re
import sys
import textwrap
import tomllib
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from importlib.metadata import entry_points
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import imednet.integrations as core_integrations
import imednet.utils.evaluator as evaluator_mod
from imednet.core.operations.circuit_breaker import CircuitBreaker, CircuitState, ProbeLease
from imednet.errors import (
    ApiError,
    CircuitBreakerError,
    DiagnosticContext,
    EvaluationError,
    ExportBatchError,
    PathTraversalValidationError,
    RequestError,
    RestrictedEvaluationError,
)
from imednet.integrations import dispatcher as export_dispatcher
from imednet.integrations.sink_base import BoundedFormBufferRouter, SinkConfig
from imednet.models.study_config import MappingRule
from imednet.plugins import WorkflowsNamespaceProtocol
from imednet.sdk import ImednetSDK
from imednet.utils.url import build_safe_path, validate_path_segment
from imednet_sinks.document import (
    _attach_sdtm_document_id,
    _encode_bson_scalars,
    _make_document_id,
)
from imednet_sinks.graph import _build_sdtm_cypher, _normalize_visit_id
from imednet_sinks.plugin import SinksNamespace
from imednet_sinks.warehouse import _resolve_merge_keys
from imednet_workflows.duckdb_centralizer import DuckDBIngestionWorkflow
from imednet_workflows.namespace import Workflows
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_DIR = REPO_ROOT / "docs"
SINKS_README = REPO_ROOT / "packages" / "plugins-sinks" / "README.md"

STREAMING_CHECKPOINT_OPTIONS: tuple[str, ...] = (
    "modified_since",
    "record_filter",
    "record_data_filter",
    "include_deleted",
    "incremental",
    "stream_name",
    "ledger_path",
    "state_provider",
)

RESILIENCE_SYMBOLS: tuple[str, ...] = (
    CircuitBreaker.__name__,
    CircuitState.__name__,
    ProbeLease.__name__,
    CircuitBreaker.record_rate_limit.__name__,
    ExportBatchError.__name__,
)

DIAGNOSTIC_ERROR_SYMBOLS: tuple[str, ...] = (
    DiagnosticContext.__name__,
    RequestError.__name__,
    CircuitBreakerError.__name__,
    ApiError.__name__,
    "errors",
    "field_attribute",
    "field_value",
)

PATH_VALIDATION_SYMBOLS: tuple[str, ...] = (
    validate_path_segment.__name__,
    build_safe_path.__name__,
    PathTraversalValidationError.__name__,
)

AST_SANDBOX_SYMBOLS: tuple[str, ...] = (
    *tuple(evaluator_mod.__all__),
    RestrictedEvaluationError.__name__,
    EvaluationError.__name__,
    MappingRule.__name__,
)


def test_sinks_namespace_contract():
    """Verify that MongoDbSinkConfig is accessible dynamically on SinksNamespace."""
    mock_sdk = MagicMock()
    sinks_ns = SinksNamespace(mock_sdk)

    # Should not raise an AttributeError
    try:
        mongo_config = sinks_ns.MongoDbSinkConfig
    except AttributeError as e:
        pytest.fail(f"Failed to access MongoDbSinkConfig: {e}")

    assert mongo_config is not None


def test_workflows_namespace_contract():
    """Verify that duckdb_centralizer and register_subjects are instantiated and accessible on Workflows."""
    mock_sdk = MagicMock()
    workflows_ns = Workflows(mock_sdk)

    # Should not raise an AttributeError
    try:
        duckdb_workflow = workflows_ns.duckdb_centralizer
    except AttributeError as e:
        pytest.fail(f"Failed to access duckdb_centralizer: {e}")

    assert duckdb_workflow is not None
    assert isinstance(duckdb_workflow, DuckDBIngestionWorkflow)
    assert workflows_ns.duckdb_centralizer is duckdb_workflow

    try:
        reg_workflow = workflows_ns.register_subjects
    except AttributeError as e:
        pytest.fail(f"Failed to access register_subjects: {e}")

    assert reg_workflow is not None
    assert isinstance(reg_workflow, RegisterSubjectsWorkflow)


def test_workflows_contract_parity():
    """Assert 100% parity between Workflows namespace attributes and registered entry points,
    and assert that every registered entry point resolves and instantiates through sdk.workflows.
    """
    mock_sdk = MagicMock()
    workflows_ns = Workflows(mock_sdk)

    # Public attributes exposed on the Workflows namespace
    namespace_attrs = {attr for attr in dir(workflows_ns) if not attr.startswith("_")}

    # Entry points registered in the 'imednet.workflows' entrypoint group
    eps = {ep.name: ep for ep in entry_points(group="imednet.workflows")}

    # Assert 100% parity between Workflows namespace attributes and registered entry points
    assert namespace_attrs == set(eps.keys()), (
        f"Parity mismatch between Workflows namespace attributes and registered entry points: "
        f"in namespace but not registered: {namespace_attrs - set(eps.keys())}, "
        f"registered but not in namespace: {set(eps.keys()) - namespace_attrs}"
    )

    # Protocol attributes exposed on WorkflowsNamespaceProtocol
    protocol_attrs = set(WorkflowsNamespaceProtocol.__annotations__.keys())

    # Assert 100% parity between Workflows namespace attributes and WorkflowsNamespaceProtocol attributes
    assert namespace_attrs == protocol_attrs, (
        f"Parity mismatch between Workflows namespace attributes and WorkflowsNamespaceProtocol: "
        f"in namespace but not protocol: {namespace_attrs - protocol_attrs}, "
        f"in protocol but not namespace: {protocol_attrs - namespace_attrs}"
    )

    # Assert 100% parity between registered entry points and WorkflowsNamespaceProtocol attributes
    assert set(eps.keys()) == protocol_attrs, (
        f"Parity mismatch between registered entry points and WorkflowsNamespaceProtocol: "
        f"registered but not in protocol: {set(eps.keys()) - protocol_attrs}, "
        f"in protocol but not registered: {protocol_attrs - set(eps.keys())}"
    )

    # Assert parity against packages/plugins-workflows/pyproject.toml if present
    pyproject_path = (
        Path(__file__).resolve().parents[3] / "packages" / "plugins-workflows" / "pyproject.toml"
    )
    if pyproject_path.exists():
        with open(pyproject_path, "rb") as f:
            pyproject_data = tomllib.load(f)
        toml_eps = (
            pyproject_data.get("project", {}).get("entry-points", {}).get("imednet.workflows", {})
        )
        assert namespace_attrs == set(toml_eps.keys()), (
            f"Parity mismatch between Workflows namespace and pyproject.toml entry points: "
            f"in namespace but not pyproject: {namespace_attrs - set(toml_eps.keys())}, "
            f"in pyproject but not namespace: {set(toml_eps.keys()) - namespace_attrs}"
        )
        assert protocol_attrs == set(toml_eps.keys()), (
            f"Parity mismatch between WorkflowsNamespaceProtocol and pyproject.toml entry points: "
            f"in protocol but not pyproject: {protocol_attrs - set(toml_eps.keys())}, "
            f"in pyproject but not protocol: {set(toml_eps.keys()) - protocol_attrs}"
        )

    # Assert that Workflows instance satisfies WorkflowsNamespaceProtocol
    assert isinstance(workflows_ns, WorkflowsNamespaceProtocol)

    # Assert that every registered entry point resolves and instantiates through sdk.workflows
    sdk = ImednetSDK(api_key="key", security_key="secret", base_url="https://example.com")
    for name, ep in eps.items():
        workflow_instance = getattr(sdk.workflows, name)
        assert workflow_instance is not None
        target_cls = ep.load()
        assert isinstance(workflow_instance, target_cls)
        assert sdk.workflows[name] is workflow_instance

    # Acceptance criteria verification
    assert isinstance(sdk.workflows.duckdb_centralizer, DuckDBIngestionWorkflow)
    assert isinstance(sdk.workflows.register_subjects, RegisterSubjectsWorkflow)


def test_workflows_namespace_lazy_instantiation_without_duckdb(monkeypatch):
    """Verify that Workflows instantiates cleanly without duckdb and defers ImportError."""
    builtin_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "duckdb":
            raise ImportError("No module named duckdb")
        return builtin_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    mock_sdk = MagicMock()
    workflows_ns = Workflows(mock_sdk)

    # Protocol runtime check passes without importing duckdb
    assert isinstance(workflows_ns, WorkflowsNamespaceProtocol)

    assert workflows_ns.data_extraction is not None

    with pytest.raises(
        ImportError,
        match=(
            r"DuckDB ingestion requires the optional 'duckdb' dependency\. "
            r"Install with `pip install 'imednet\[duckdb\]'`\."
        ),
    ):
        _ = workflows_ns.duckdb_centralizer


def test_workflow_constructors_contract():
    """Verify that all workflow constructors accept at most one mandatory argument (the SDK client)."""
    mock_sdk = MagicMock()
    workflows_ns = Workflows(mock_sdk)
    _ = workflows_ns.duckdb_centralizer

    for attr_name, attr_val in vars(workflows_ns).items():
        if attr_name.startswith("_"):
            continue

        cls = attr_val.__class__
        sig = inspect.signature(cls.__init__)

        mandatory_params = []
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if param.default is inspect.Parameter.empty:
                mandatory_params.append(param_name)

        assert len(mandatory_params) <= 1, (
            f"Workflow class {cls.__name__} constructor requires more than one mandatory argument: "
            f"{mandatory_params}. It must accept only the SDK client as a mandatory argument."
        )


def _read_narrative_guides(
    docs_dir: Path,
    subdirs: tuple[str, ...] = ("explanation", "how-to"),
) -> str:
    """Concatenate hand-written narrative .rst guides under docs/explanation and docs/how-to."""
    chunks: list[str] = []
    for subdir in subdirs:
        root = docs_dir / subdir
        if not root.exists():
            continue
        for rst_file in sorted(root.rglob("*.rst")):
            chunks.append(rst_file.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def validate_sink_config_narrative_coverage(
    docs_dir: Path,
    sink_fields: Iterable[str],
    checkpoint_options: Iterable[str] = STREAMING_CHECKPOINT_OPTIONS,
) -> list[str]:
    """Verify architecture.rst documents SinkConfig fields and how-to guides cover checkpoint options."""
    errors: list[str] = []
    arch_rst = docs_dir / "explanation" / "architecture.rst"
    if not arch_rst.exists():
        return [f"Missing architecture guide: {arch_rst}"]

    arch_text = arch_rst.read_text(encoding="utf-8")
    for core_symbol in ("StateProviderProtocol", "get_state_provider", "run_streaming_export"):
        if not re.search(rf"\b{re.escape(core_symbol)}\b", arch_text):
            errors.append(
                f"{arch_rst}: Missing narrative documentation for streaming architecture symbol '{core_symbol}'"
            )

    for field_name in sink_fields:
        if not re.search(rf"\b{re.escape(field_name)}\b", arch_text):
            errors.append(
                f"{arch_rst}: SinkConfig field '{field_name}' lacks narrative documentation"
            )

    howto_text = _read_narrative_guides(docs_dir, subdirs=("how-to",))
    for option_name in checkpoint_options:
        if not re.search(rf"\b{re.escape(option_name)}\b", howto_text):
            errors.append(
                f"{docs_dir / 'how-to'}: Streaming checkpoint option '{option_name}' lacks how-to guide coverage"
            )

    return errors


def _extract_airflow_connection_targets(func: object) -> set[str]:
    """Extract destination target literals handled by ImednetHook.resolve_destination_connection."""
    source = textwrap.dedent(inspect.getsource(func))  # type: ignore[arg-type]
    tree = ast.parse(source)
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "dest"):
            continue
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                if isinstance(comparator.value, str):
                    targets.add(comparator.value)
            elif isinstance(op, ast.In) and isinstance(comparator, (ast.Set, ast.Tuple, ast.List)):
                for elt in comparator.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        targets.add(elt.value)
    return targets


def validate_tabular_exporters_narrative_coverage(
    docs_dir: Path,
    tabular_exporters: Iterable[str],
) -> list[str]:
    """Verify export_destinations.rst documents all tabular exporters, upserts, and watermark capping."""
    errors: list[str] = []
    export_rst = docs_dir / "how-to" / "export_destinations.rst"
    arch_rst = docs_dir / "explanation" / "architecture.rst"
    if not export_rst.exists():
        return [f"Missing export destinations guide: {export_rst}"]

    export_text = export_rst.read_text(encoding="utf-8")
    for exporter_name in tabular_exporters:
        if not re.search(rf"\b{re.escape(exporter_name)}\b", export_text):
            errors.append(
                f"{export_rst}: Tabular exporter '{exporter_name}' lacks narrative documentation"
            )

    for required_token in (
        "recordId",
        "reconcile_deleted",
        ".tmp",
        "quality_gate_enabled",
        "min_rejected_ts",
    ):
        if required_token not in export_text:
            errors.append(
                f"{export_rst}: Missing incremental reconciliation or watermark token '{required_token}'"
            )

    if arch_rst.exists():
        arch_text = arch_rst.read_text(encoding="utf-8")
        for arch_token in ("ExportRegistry", "min_rejected_ts"):
            if arch_token not in arch_text:
                errors.append(
                    f"{arch_rst}: Missing streaming export architecture token '{arch_token}'"
                )

    return errors


def validate_duckdb_streaming_router_narrative_coverage(
    docs_dir: Path,
    *,
    router_cls_name: str = "BoundedFormBufferRouter",
    router_params: Iterable[str] = ("batch_size", "max_buffered_records"),
) -> list[str]:
    """Verify duckdb_integration.rst documents BoundedFormBufferRouter and INSERT INTO ... BY NAME without CTAS claims."""
    duckdb_rst = docs_dir / "how-to" / "duckdb_integration.rst"
    if not duckdb_rst.exists():
        return [f"Missing DuckDB integration guide: {duckdb_rst}"]

    errors: list[str] = []
    duckdb_text = duckdb_rst.read_text(encoding="utf-8")
    if not re.search(rf"\b{re.escape(router_cls_name)}\b", duckdb_text):
        errors.append(f"{duckdb_rst}: Missing narrative documentation for '{router_cls_name}'")
    for param_name in router_params:
        if not re.search(rf"\b{re.escape(param_name)}\b", duckdb_text):
            errors.append(
                f"{duckdb_rst}: Router parameter '{param_name}' lacks narrative documentation"
            )
    if "INSERT INTO ... BY NAME" not in duckdb_text:
        errors.append(f"{duckdb_rst}: Missing 'INSERT INTO ... BY NAME' batch append documentation")
    if "CREATE TABLE AS SELECT" in duckdb_text:
        errors.append(f"{duckdb_rst}: Contains stale whole-table 'CREATE TABLE AS SELECT' claim")
    return errors


def validate_sink_schemas_and_keys_narrative_coverage(
    docs_dir: Path,
    sinks_readme: Path,
    *,
    extra_required_tokens: Mapping[Path, tuple[str, ...]] | None = None,
) -> list[str]:
    """Verify MongoDB, Neo4j, and Snowflake schema/key contracts match narrative guides and sink README."""
    required_by_file: dict[Path, tuple[str, ...]] = {
        docs_dir / "how-to" / "export_destinations.rst": (
            "Decimal128",
            "ISO-8601",
            "<STUDYID>/<DOMAIN>/<USUBJID>/<*SEQ>",
            "visit_id <= 0",
            "(:Subject)-[:HAS_RECORD]->(:Record)",
            "(STUDYID, DOMAIN, USUBJID, *SEQ)",
        ),
        docs_dir / "explanation" / "architecture.rst": (
            "Decimal128",
            "ISO-8601",
            "<STUDYID>/<DOMAIN>/<USUBJID>/<*SEQ>",
            "visit_id <= 0",
            "(:Subject)-[:HAS_RECORD]->(:Record)",
            "(STUDYID, DOMAIN, USUBJID, *SEQ)",
            "(studyKey, recordId)",
            "(STUDYID, RECORD_ID)",
        ),
        docs_dir / "how-to" / "snowflake_export.rst": (
            "(study_key, record_id)",
            "(studyKey, recordId)",
            "(STUDYID, RECORD_ID)",
            "(STUDYID, DOMAIN, USUBJID, *SEQ)",
            "(STUDYID, DOMAIN, USUBJID)",
        ),
        docs_dir / "tutorials" / "examples" / "neo4j_export.rst": (
            "visit_id <= 0",
            "(:Subject)-[:HAS_RECORD]->(:Record)",
            "(STUDYID, DOMAIN, USUBJID, *SEQ)",
        ),
        docs_dir / "tutorials" / "examples" / "mongodb_export.rst": (
            "Decimal128",
            "ISO-8601",
            "<STUDYID>/<DOMAIN>/<USUBJID>/<*SEQ>",
        ),
        sinks_readme: (
            "Decimal128",
            "ISO-8601",
            "<STUDYID>/<DOMAIN>/<USUBJID>/<*SEQ>",
            "visit_id <= 0",
            "(:Subject)-[:HAS_RECORD]->(:Record)",
            "(STUDYID, DOMAIN, USUBJID, *SEQ)",
            "(studyKey, recordId)",
            "(STUDYID, RECORD_ID)",
        ),
    }
    if extra_required_tokens:
        for path, tokens in extra_required_tokens.items():
            required_by_file[path] = required_by_file.get(path, ()) + tuple(tokens)

    errors: list[str] = []
    for doc_path, tokens in required_by_file.items():
        if not doc_path.exists():
            errors.append(f"Missing sink documentation file: {doc_path}")
            continue
        content = doc_path.read_text(encoding="utf-8")
        for token in tokens:
            if token not in content:
                errors.append(f"{doc_path}: Missing sink schema/key documentation token '{token}'")
    return errors


def validate_airflow_export_narrative_coverage(
    docs_dir: Path,
    airflow_helpers: Iterable[str],
    connection_targets: Iterable[str],
) -> list[str]:
    """Verify architecture.rst documents all Airflow export helpers and destination connection targets."""
    arch_rst = docs_dir / "explanation" / "architecture.rst"
    if not arch_rst.exists():
        return [f"Missing architecture guide: {arch_rst}"]

    arch_text = arch_rst.read_text(encoding="utf-8")
    anchor = "apache_airflow_providers_imednet.export"
    anchor_idx = arch_text.find(anchor)
    airflow_section = arch_text[anchor_idx:] if anchor_idx != -1 else arch_text

    errors: list[str] = []
    if anchor_idx == -1:
        errors.append(f"{arch_rst}: Missing '{anchor}' boundary documentation")
    if "resolve_destination_connection" not in airflow_section:
        errors.append(f"{arch_rst}: Missing 'resolve_destination_connection' documentation")

    for helper_name in airflow_helpers:
        if not re.search(rf"\b{re.escape(helper_name)}\b", airflow_section):
            errors.append(
                f"{arch_rst}: Airflow export helper '{helper_name}' lacks narrative documentation"
            )
    for target in connection_targets:
        token_pattern = (
            rf"(?:``{re.escape(target)}``|\"{re.escape(target)}\"|'{re.escape(target)}')"
        )
        if not re.search(token_pattern, airflow_section):
            errors.append(
                f"{arch_rst}: Airflow destination connection target '{target}' lacks narrative documentation"
            )
    return errors


def validate_workflow_entrypoints_narrative_coverage(
    docs_dir: Path,
    workflow_entrypoints: Iterable[str],
) -> list[str]:
    """Verify every imednet.workflows entry point is documented in hand-written how-to guides."""
    errors: list[str] = []
    howto_text = _read_narrative_guides(docs_dir, subdirs=("how-to",))
    for ep_name in workflow_entrypoints:
        if not re.search(rf"\b{re.escape(ep_name)}\b", howto_text):
            errors.append(
                f"{docs_dir / 'how-to'}: Workflow entry point '{ep_name}' lacks narrative how-to documentation"
            )
    return errors


def validate_state_provider_entrypoints_narrative_coverage(
    docs_dir: Path,
    state_provider_entrypoints: Mapping[str, str],
) -> list[str]:
    """Verify every imednet.state_providers entry point and target class has narrative documentation."""
    errors: list[str] = []
    narrative_text = _read_narrative_guides(docs_dir, subdirs=("explanation", "how-to"))
    for ep_name, target_class in state_provider_entrypoints.items():
        token_pattern = (
            rf"(?:``{re.escape(ep_name)}``|\"{re.escape(ep_name)}\"|'{re.escape(ep_name)}')"
        )
        if not re.search(token_pattern, narrative_text):
            errors.append(
                f"{docs_dir}: State provider entry point '{ep_name}' lacks narrative documentation"
            )
        if target_class and not re.search(rf"\b{re.escape(target_class)}\b", narrative_text):
            errors.append(
                f"{docs_dir}: State provider class '{target_class}' (entry point '{ep_name}') lacks narrative documentation"
            )
    return errors


def validate_workflow_interactions_sections(workflow_interactions_rst: Path) -> list[str]:
    """Verify every level-2 section in workflow_interactions.rst has prose, Mermaid, and testcode."""
    if not workflow_interactions_rst.exists():
        return [f"Missing workflow interactions guide: {workflow_interactions_rst}"]

    lines = workflow_interactions_rst.read_text(encoding="utf-8").splitlines()
    section_starts: list[tuple[str, int]] = []
    for idx in range(1, len(lines)):
        underline = lines[idx].strip()
        title = lines[idx - 1].strip()
        if title and underline and set(underline) == {"-"} and len(underline) >= len(title):
            section_starts.append((title, idx + 1))

    if not section_starts:
        return [f"{workflow_interactions_rst}: No level-2 ('---') workflow sections found"]

    errors: list[str] = []
    for sec_idx, (title, start_line) in enumerate(section_starts):
        end_line = (
            section_starts[sec_idx + 1][1] - 2 if sec_idx + 1 < len(section_starts) else len(lines)
        )
        sec_lines = lines[start_line:end_line]
        sec_body = "\n".join(sec_lines)

        has_mermaid = any(line.strip().startswith(".. mermaid::") for line in sec_lines)
        has_testcode = any(line.strip().startswith(".. testcode::") for line in sec_lines)

        prose_lines: list[str] = []
        in_indented_block = False
        for raw_line in sec_lines:
            stripped = raw_line.strip()
            if not stripped:
                continue
            if raw_line.startswith(".. "):
                in_indented_block = True
                continue
            if in_indented_block:
                if raw_line.startswith((" ", "\t")):
                    continue
                in_indented_block = False
            if not raw_line.startswith((" ", "\t")):
                prose_lines.append(stripped)

        if not prose_lines:
            errors.append(f"{workflow_interactions_rst}: Section '{title}' lacks explanatory prose")
        if not has_mermaid:
            errors.append(
                f"{workflow_interactions_rst}: Section '{title}' lacks a '.. mermaid::' diagram"
            )
        if not has_testcode or ".. testcode::" not in sec_body:
            errors.append(
                f"{workflow_interactions_rst}: Section '{title}' lacks a '.. testcode::' example"
            )

    return errors


def validate_resilience_and_error_narrative_coverage(
    docs_dir: Path,
    resilience_symbols: Iterable[str] = RESILIENCE_SYMBOLS,
    error_symbols: Iterable[str] = DIAGNOSTIC_ERROR_SYMBOLS,
) -> list[str]:
    """Verify retry_policy.rst, api_overview.rst, and architecture.rst document resilience and error contracts."""
    errors: list[str] = []
    retry_rst = docs_dir / "how-to" / "retry_policy.rst"
    api_rst = docs_dir / "explanation" / "api_overview.rst"
    arch_rst = docs_dir / "explanation" / "architecture.rst"

    for rst_path in (retry_rst, api_rst, arch_rst):
        if not rst_path.exists():
            errors.append(f"Missing narrative guide: {rst_path}")
    if errors:
        return errors

    retry_text = retry_rst.read_text(encoding="utf-8")
    api_text = api_rst.read_text(encoding="utf-8")
    arch_text = arch_rst.read_text(encoding="utf-8")

    for rst_path, text in ((retry_rst, retry_text), (api_rst, api_text)):
        if ".. testcode::" not in text:
            errors.append(f"{rst_path}: Missing runnable '.. testcode::' coverage")

    for sym in resilience_symbols:
        for rst_path, text in ((retry_rst, retry_text), (arch_rst, arch_text)):
            if not re.search(rf"\b{re.escape(sym)}\b", text):
                errors.append(
                    f"{rst_path}: Missing narrative documentation for resilience symbol '{sym}'"
                )

    for sym in error_symbols:
        for rst_path, text in ((retry_rst, retry_text), (api_rst, api_text)):
            if not re.search(rf"\b{re.escape(sym)}\b", text):
                errors.append(
                    f"{rst_path}: Missing narrative documentation for diagnostic/error symbol '{sym}'"
                )

    return errors


def validate_path_validation_narrative_coverage(
    docs_dir: Path,
    path_symbols: Iterable[str] = PATH_VALIDATION_SYMBOLS,
) -> list[str]:
    """Verify SDK, CLI, workflow, and Airflow guides document dynamic URL path validation."""
    errors: list[str] = []
    target_guides = (
        docs_dir / "explanation" / "api_overview.rst",
        docs_dir / "how-to" / "cli.rst",
        docs_dir / "how-to" / "workflows" / "uat_workflow.rst",
        docs_dir / "how-to" / "airflow.rst",
    )

    for rst_path in target_guides:
        if not rst_path.exists():
            errors.append(f"Missing path-validation narrative guide: {rst_path}")
            continue
        text = rst_path.read_text(encoding="utf-8")
        if ".. testcode::" not in text:
            errors.append(f"{rst_path}: Missing runnable '.. testcode::' coverage")
        for sym in path_symbols:
            if not re.search(rf"\b{re.escape(sym)}\b", text):
                errors.append(
                    f"{rst_path}: Missing narrative documentation for path-validation symbol '{sym}'"
                )

    return errors


def validate_ast_evaluator_narrative_coverage(
    docs_dir: Path,
    evaluator_symbols: Iterable[str] = AST_SANDBOX_SYMBOLS,
) -> list[str]:
    """Verify record_mapping.rst, data_extraction.rst, and architecture.rst document AST sandbox symbols."""
    errors: list[str] = []
    target_guides = (
        docs_dir / "how-to" / "record_mapping.rst",
        docs_dir / "how-to" / "workflows" / "data_extraction.rst",
        docs_dir / "explanation" / "architecture.rst",
    )

    for rst_path in target_guides:
        if not rst_path.exists():
            errors.append(f"Missing AST sandbox narrative guide: {rst_path}")
            continue
        text = rst_path.read_text(encoding="utf-8")
        if ".. testcode::" not in text:
            errors.append(f"{rst_path}: Missing runnable '.. testcode::' coverage")
        for sym in evaluator_symbols:
            if not re.search(rf"\b{re.escape(sym)}\b", text):
                errors.append(
                    f"{rst_path}: Missing narrative documentation for AST sandbox symbol '{sym}'"
                )

    return errors


def _load_docs_conf_module():
    """Load docs/conf.py as an isolated module for Sphinx xref unit testing."""
    conf_path = DOCS_DIR / "conf.py"
    spec = importlib.util.spec_from_file_location("_test_docs_conf", conf_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    docs_ext = str(DOCS_DIR / "_ext")
    added_ext = False
    if docs_ext not in sys.path:
        sys.path.insert(0, docs_ext)
        added_ext = True
    try:
        spec.loader.exec_module(module)
    finally:
        if added_ext and docs_ext in sys.path:
            sys.path.remove(docs_ext)
    return module


def test_resilience_and_error_narrative_parity() -> None:
    """Assert circuit breaker, probe lease, rate-limit, and diagnostic/API error contracts are documented."""
    assert hasattr(ApiError, "field_attribute")
    assert hasattr(ApiError, "field_value")
    errors = validate_resilience_and_error_narrative_coverage(DOCS_DIR)
    assert not errors, f"Resilience/error narrative parity failed: {errors}"


def test_path_validation_narrative_parity() -> None:
    """Assert validate_path_segment, build_safe_path, and PathTraversalValidationError are documented across guides."""
    errors = validate_path_validation_narrative_coverage(DOCS_DIR)
    assert not errors, f"Path validation narrative parity failed: {errors}"


def test_ast_evaluator_narrative_parity() -> None:
    """Assert RestrictedAstEvaluator allowlists, errors, and MappingRule fallbacks are documented across guides."""
    errors = validate_ast_evaluator_narrative_coverage(DOCS_DIR)
    assert not errors, f"AST evaluator narrative parity failed: {errors}"


def test_sphinx_reexported_py_reference_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert _resolve_reexported_py_reference resolves imednet, errors, utils, and spi re-exports."""
    conf = _load_docs_conf_module()
    resolved_targets: list[tuple[str, str, str]] = []

    def fake_make_refnode(
        _builder: object,
        _fromdocname: str,
        todocname: str,
        targetid: str,
        _child: object,
        title: str,
    ) -> tuple[str, str, str]:
        hit = (todocname, targetid, title)
        resolved_targets.append(hit)
        return hit

    monkeypatch.setattr(conf, "make_refnode", fake_make_refnode)

    canonical_inventory = {
        "imednet.errors.base.ImednetError": SimpleNamespace(
            docname="api/generated/imednet.errors.base",
            node_id="imednet.errors.base.ImednetError",
        ),
        "imednet.errors.base.DiagnosticContext": SimpleNamespace(
            docname="api/generated/imednet.errors.base",
            node_id="imednet.errors.base.DiagnosticContext",
        ),
        "imednet.errors.network.RequestError": SimpleNamespace(
            docname="api/generated/imednet.errors.network",
            node_id="imednet.errors.network.RequestError",
        ),
        "imednet.errors.network.CircuitBreakerError": SimpleNamespace(
            docname="api/generated/imednet.errors.network",
            node_id="imednet.errors.network.CircuitBreakerError",
        ),
        "imednet.errors.validation.ConfigurationError": SimpleNamespace(
            docname="api/generated/imednet.errors.validation",
            node_id="imednet.errors.validation.ConfigurationError",
        ),
        "imednet.errors.api.ApiError": SimpleNamespace(
            docname="api/generated/imednet.errors.api",
            node_id="imednet.errors.api.ApiError",
        ),
        "imednet.errors.api.ApiError.errors": SimpleNamespace(
            docname="api/generated/imednet.errors.api",
            node_id="imednet.errors.api.ApiError.errors",
        ),
        "imednet.errors.api.ApiError.field_attribute": SimpleNamespace(
            docname="api/generated/imednet.errors.api",
            node_id="imednet.errors.api.ApiError.field_attribute",
        ),
        "imednet.errors.api.ApiError.field_value": SimpleNamespace(
            docname="api/generated/imednet.errors.api",
            node_id="imednet.errors.api.ApiError.field_value",
        ),
        "imednet.errors.validation.BadRequestError": SimpleNamespace(
            docname="api/generated/imednet.errors.validation",
            node_id="imednet.errors.validation.BadRequestError",
        ),
        "imednet.errors.registry.get_error_class": SimpleNamespace(
            docname="api/generated/imednet.errors.registry",
            node_id="imednet.errors.registry.get_error_class",
        ),
        "imednet.errors.validation.PathTraversalValidationError": SimpleNamespace(
            docname="api/generated/imednet.errors.validation",
            node_id="imednet.errors.validation.PathTraversalValidationError",
        ),
        "imednet.errors.validation.RestrictedEvaluationError": SimpleNamespace(
            docname="api/generated/imednet.errors.validation",
            node_id="imednet.errors.validation.RestrictedEvaluationError",
        ),
        "imednet.sdk.ImednetSDK": SimpleNamespace(
            docname="api/generated/imednet.sdk",
            node_id="imednet.sdk.ImednetSDK",
        ),
        "imednet.utils.url.validate_path_segment": SimpleNamespace(
            docname="api/generated/imednet.utils.url",
            node_id="imednet.utils.url.validate_path_segment",
        ),
        "imednet.utils.url.build_safe_path": SimpleNamespace(
            docname="api/generated/imednet.utils.url",
            node_id="imednet.utils.url.build_safe_path",
        ),
        "imednet.utils.evaluator.RestrictedAstEvaluator": SimpleNamespace(
            docname="api/generated/imednet.utils.evaluator",
            node_id="imednet.utils.evaluator.RestrictedAstEvaluator",
        ),
        "imednet.integrations.sink_base.SinkConfig": SimpleNamespace(
            docname="api/generated/imednet.integrations.sink_base",
            node_id="imednet.integrations.sink_base.SinkConfig",
        ),
        "imednet.integrations.sink_base.StateProviderProtocol": SimpleNamespace(
            docname="api/generated/imednet.integrations.sink_base",
            node_id="imednet.integrations.sink_base.StateProviderProtocol",
        ),
    }

    fake_app = SimpleNamespace(builder=SimpleNamespace(get_relative_uri=lambda _a, _b: ""))
    fake_env = SimpleNamespace(domaindata={"py": {"objects": canonical_inventory}})

    expected_resolutions = {
        "imednet.errors.ImednetError": "imednet.errors.base.ImednetError",
        "imednet.errors.DiagnosticContext": "imednet.errors.base.DiagnosticContext",
        "imednet.errors.RequestError": "imednet.errors.network.RequestError",
        "imednet.errors.CircuitBreakerError": "imednet.errors.network.CircuitBreakerError",
        "imednet.errors.ConfigurationError": "imednet.errors.validation.ConfigurationError",
        "imednet.errors.BadRequestError": "imednet.errors.validation.BadRequestError",
        "imednet.errors.ApiError.errors": "imednet.errors.api.ApiError.errors",
        "imednet.errors.ApiError.field_attribute": "imednet.errors.api.ApiError.field_attribute",
        "imednet.errors.ApiError.field_value": "imednet.errors.api.ApiError.field_value",
        "imednet.errors.get_error_class": "imednet.errors.registry.get_error_class",
        "imednet.errors.PathTraversalValidationError": (
            "imednet.errors.validation.PathTraversalValidationError"
        ),
        "imednet.errors.RestrictedEvaluationError": (
            "imednet.errors.validation.RestrictedEvaluationError"
        ),
        "imednet.ImednetSDK": "imednet.sdk.ImednetSDK",
        "imednet.utils.validate_path_segment": "imednet.utils.url.validate_path_segment",
        "imednet.utils.build_safe_path": "imednet.utils.url.build_safe_path",
        "imednet.utils.RestrictedAstEvaluator": "imednet.utils.evaluator.RestrictedAstEvaluator",
        "imednet.spi.sink_base.SinkConfig": "imednet.integrations.sink_base.SinkConfig",
        "imednet.spi.sink_base.StateProviderProtocol": (
            "imednet.integrations.sink_base.StateProviderProtocol"
        ),
    }

    for reftarget, expected_canonical in expected_resolutions.items():
        node = {"refdomain": "py", "reftarget": reftarget, "refdoc": "explanation/architecture"}
        res = conf._resolve_reexported_py_reference(fake_app, fake_env, node, object())
        assert res is not None, f"Failed to resolve re-exported xref '{reftarget}'"
        assert res[2] == expected_canonical


def test_sink_config_and_streaming_checkpoints_narrative_parity() -> None:
    """Assert all SinkConfig fields and streaming checkpoint options are documented in narrative guides."""
    sink_field_names = [f.name for f in dataclasses.fields(SinkConfig)]
    assert set(STREAMING_CHECKPOINT_OPTIONS).issubset(set(sink_field_names))

    errors = validate_sink_config_narrative_coverage(DOCS_DIR, sink_field_names)
    assert not errors, f"SinkConfig narrative documentation parity failed: {errors}"


def test_workflow_and_state_provider_entrypoints_narrative_parity() -> None:
    """Assert all 9 imednet.workflows and all 3 imednet.state_providers entry points have narrative guides."""
    workflow_eps = {ep.name for ep in entry_points(group="imednet.workflows")}
    state_provider_eps = {
        ep.name: ep.value.split(":")[-1] for ep in entry_points(group="imednet.state_providers")
    }

    pyproject_path = REPO_ROOT / "packages" / "plugins-workflows" / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        pyproject_data = tomllib.load(f)
    toml_entry_points = pyproject_data.get("project", {}).get("entry-points", {})
    toml_state_eps = {
        name: target.split(":")[-1]
        for name, target in toml_entry_points.get("imednet.state_providers", {}).items()
    }
    assert state_provider_eps == toml_state_eps
    assert len(workflow_eps) >= 9
    assert len(state_provider_eps) >= 3

    workflow_errors = validate_workflow_entrypoints_narrative_coverage(DOCS_DIR, workflow_eps)
    assert not workflow_errors, f"Workflow narrative guide parity failed: {workflow_errors}"

    provider_errors = validate_state_provider_entrypoints_narrative_coverage(
        DOCS_DIR, state_provider_eps
    )
    assert not provider_errors, f"State provider narrative guide parity failed: {provider_errors}"


def test_workflow_interactions_sections_have_prose_mermaid_and_testcode() -> None:
    """Assert every section in docs/explanation/workflow_interactions.rst has prose, Mermaid, and testcode."""
    interactions_rst = DOCS_DIR / "explanation" / "workflow_interactions.rst"
    errors = validate_workflow_interactions_sections(interactions_rst)
    assert not errors, f"workflow_interactions.rst parity failed: {errors}"


def test_tabular_exporters_and_streaming_router_narrative_parity() -> None:
    """Assert all 10 ExportRegistry tabular exporters and BoundedFormBufferRouter are documented."""
    registry = export_dispatcher._registry
    assert isinstance(registry, export_dispatcher.ExportRegistry)
    registered_tabular_funcs = {func.__name__ for func in registry._tabular_targets.values()}
    public_export_funcs = {
        name for name in core_integrations.__all__ if name.startswith("export_to_")
    }
    assert registered_tabular_funcs == public_export_funcs
    assert len(registered_tabular_funcs) == 10

    router_sig = inspect.signature(BoundedFormBufferRouter.__init__)
    router_params = [
        name for name in ("batch_size", "max_buffered_records") if name in router_sig.parameters
    ]
    assert router_params == ["batch_size", "max_buffered_records"]

    tabular_errors = validate_tabular_exporters_narrative_coverage(
        DOCS_DIR, sorted(registered_tabular_funcs)
    )
    assert not tabular_errors, f"Tabular exporters narrative parity failed: {tabular_errors}"

    duckdb_errors = validate_duckdb_streaming_router_narrative_coverage(
        DOCS_DIR,
        router_cls_name=BoundedFormBufferRouter.__name__,
        router_params=router_params,
    )
    assert not duckdb_errors, f"DuckDB streaming router narrative parity failed: {duckdb_errors}"


def test_sink_schemas_and_keys_narrative_parity() -> None:
    """Assert sink key helpers and scalar encoders match narrative docs and sink README."""
    assert _make_document_id("STUDY_A", 42) == "STUDY_A/42"
    sdtm_doc = _attach_sdtm_document_id(
        {"STUDYID": "STUDY_A", "DOMAIN": "AE", "USUBJID": "SUBJ-1", "AESEQ": 3},
        {},
        "STUDY_A",
    )
    assert sdtm_doc["_id"] == "STUDY_A/AE/SUBJ-1/3"
    assert _encode_bson_scalars(date(2026, 5, 1)) == "2026-05-01"
    encoded_decimal = _encode_bson_scalars(Decimal("12.50"))
    assert type(encoded_decimal).__name__ == "Decimal128"

    assert _normalize_visit_id(0) is None
    assert _normalize_visit_id(-5) is None
    sdtm_merge_cypher, _ = _build_sdtm_cypher("AESEQ", has_visit=False)
    assert "STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, USUBJID: row.USUBJID, AESEQ: row.AESEQ" in (
        sdtm_merge_cypher
    )
    assert "MERGE (su)-[:HAS_RECORD]->(r)" in sdtm_merge_cypher

    assert _resolve_merge_keys(["STUDYID", "DOMAIN", "USUBJID", "AESEQ"], as_sdtm=True) == (
        "STUDYID",
        "DOMAIN",
        "USUBJID",
        "AESEQ",
    )
    assert _resolve_merge_keys(["STUDYID", "DOMAIN", "USUBJID"], as_sdtm=True) == (
        "STUDYID",
        "DOMAIN",
        "USUBJID",
    )
    assert _resolve_merge_keys(["studyKey", "recordId"], naming_convention="camelCase") == (
        "studyKey",
        "recordId",
    )
    assert _resolve_merge_keys(["STUDYID", "RECORD_ID"], naming_convention="sdtm") == (
        "STUDYID",
        "RECORD_ID",
    )

    sink_errors = validate_sink_schemas_and_keys_narrative_coverage(DOCS_DIR, SINKS_README)
    assert not sink_errors, f"Sink schema and key narrative parity failed: {sink_errors}"


def test_airflow_export_and_connection_targets_narrative_parity() -> None:
    """Assert Airflow 10-helper export surface and connection targets match narrative docs."""
    from apache_airflow_providers_imednet import export as airflow_export
    from apache_airflow_providers_imednet.hooks import ImednetHook
    from apache_airflow_providers_imednet.operators.export import _TABULAR_EXPORTS

    airflow_helpers = list(airflow_export.__all__)
    assert len(airflow_helpers) == 10
    assert {fn.__name__ for fn in _TABULAR_EXPORTS.values()} == set(airflow_helpers)

    conn_targets = _extract_airflow_connection_targets(ImednetHook.resolve_destination_connection)
    assert conn_targets == {
        "snowflake",
        "neo4j",
        "mongodb",
        "duckdb",
        "duckdb_by_form",
        "hive_parquet",
        "sql",
        "sql_by_form",
        "long_sql",
    }

    airflow_errors = validate_airflow_export_narrative_coverage(
        DOCS_DIR, sorted(airflow_helpers), sorted(conn_targets)
    )
    assert not airflow_errors, f"Airflow export narrative parity failed: {airflow_errors}"


def test_narrative_parity_gates_fail_on_undocumented_additions(tmp_path: Path) -> None:
    """Verify parity validators fail when new SinkConfig fields, exporters, Airflow targets, or Mermaid sections lack docs."""
    sink_fields_with_new = [f.name for f in dataclasses.fields(SinkConfig)] + [
        "undocumented_sink_field_xyz"
    ]
    sink_errs = validate_sink_config_narrative_coverage(DOCS_DIR, sink_fields_with_new)
    assert any("undocumented_sink_field_xyz" in err for err in sink_errs)

    exporter_errs = validate_tabular_exporters_narrative_coverage(
        DOCS_DIR, ["export_to_csv", "export_to_undocumented_xyz"]
    )
    assert any("export_to_undocumented_xyz" in err for err in exporter_errs)

    stale_docs = tmp_path / "stale_docs"
    (stale_docs / "how-to").mkdir(parents=True)
    (stale_docs / "how-to" / "duckdb_integration.rst").write_text(
        "DuckDB Integration\n==================\n\n"
        "Registers a DataFrame before a ``CREATE TABLE AS SELECT`` operation.\n",
        encoding="utf-8",
    )
    duckdb_errs = validate_duckdb_streaming_router_narrative_coverage(stale_docs)
    assert any("BoundedFormBufferRouter" in err for err in duckdb_errs)
    assert any("INSERT INTO ... BY NAME" in err for err in duckdb_errs)
    assert any("CREATE TABLE AS SELECT" in err for err in duckdb_errs)

    sink_key_errs = validate_sink_schemas_and_keys_narrative_coverage(
        DOCS_DIR,
        SINKS_README,
        extra_required_tokens={SINKS_README: ("undocumented_sink_key_xyz",)},
    )
    assert any("undocumented_sink_key_xyz" in err for err in sink_key_errs)

    airflow_errs = validate_airflow_export_narrative_coverage(
        DOCS_DIR,
        ["export_to_csv", "export_to_undocumented_airflow_xyz"],
        ["duckdb", "undocumented_airflow_target_xyz"],
    )
    assert any("export_to_undocumented_airflow_xyz" in err for err in airflow_errs)
    assert any("undocumented_airflow_target_xyz" in err for err in airflow_errs)

    workflow_errs = validate_workflow_entrypoints_narrative_coverage(
        DOCS_DIR, ["data_extraction", "undocumented_workflow_plugin_xyz"]
    )
    assert any("undocumented_workflow_plugin_xyz" in err for err in workflow_errs)

    provider_errs = validate_state_provider_entrypoints_narrative_coverage(
        DOCS_DIR, {"undocumented_provider_xyz": "UndocumentedStateProviderXYZ"}
    )
    assert any("undocumented_provider_xyz" in err for err in provider_errs)
    assert any("UndocumentedStateProviderXYZ" in err for err in provider_errs)

    bare_rst = tmp_path / "workflow_interactions.rst"
    bare_rst.write_text(
        "Workflow Interactions\n"
        "=====================\n\n"
        "Bare Diagram Section\n"
        "--------------------\n\n"
        ".. mermaid:: /diagrams/example.mmd\n",
        encoding="utf-8",
    )
    sec_errs = validate_workflow_interactions_sections(bare_rst)
    assert any("lacks explanatory prose" in err for err in sec_errs)
    assert any("lacks a '.. testcode::' example" in err for err in sec_errs)

    resilience_errs = validate_resilience_and_error_narrative_coverage(
        DOCS_DIR,
        resilience_symbols=(*RESILIENCE_SYMBOLS, "UndocumentedBreakerSymbolXYZ"),
        error_symbols=(*DIAGNOSTIC_ERROR_SYMBOLS, "UndocumentedDiagnosticSymbolXYZ"),
    )
    assert any("UndocumentedBreakerSymbolXYZ" in err for err in resilience_errs)
    assert any("UndocumentedDiagnosticSymbolXYZ" in err for err in resilience_errs)

    path_errs = validate_path_validation_narrative_coverage(
        DOCS_DIR,
        path_symbols=(*PATH_VALIDATION_SYMBOLS, "undocumented_path_guard_xyz"),
    )
    assert len([err for err in path_errs if "undocumented_path_guard_xyz" in err]) == 4

    ast_errs = validate_ast_evaluator_narrative_coverage(
        DOCS_DIR,
        evaluator_symbols=(*AST_SANDBOX_SYMBOLS, "UndocumentedAstEvaluatorSymbolXYZ"),
    )
    assert len([err for err in ast_errs if "UndocumentedAstEvaluatorSymbolXYZ" in err]) == 3

    empty_docs = tmp_path / "empty_docs"
    (empty_docs / "explanation").mkdir(parents=True)
    (empty_docs / "how-to" / "workflows").mkdir(parents=True)
    for rel in (
        "explanation/api_overview.rst",
        "explanation/architecture.rst",
        "how-to/retry_policy.rst",
        "how-to/cli.rst",
        "how-to/airflow.rst",
        "how-to/record_mapping.rst",
        "how-to/workflows/uat_workflow.rst",
        "how-to/workflows/data_extraction.rst",
    ):
        (empty_docs / rel).write_text("Title\n=====\n\nProse without testcode.\n", encoding="utf-8")

    assert any(
        "Missing runnable '.. testcode::'" in err
        for err in validate_resilience_and_error_narrative_coverage(empty_docs)
    )
    assert any(
        "Missing runnable '.. testcode::'" in err
        for err in validate_path_validation_narrative_coverage(empty_docs)
    )
    assert any(
        "Missing runnable '.. testcode::'" in err
        for err in validate_ast_evaluator_narrative_coverage(empty_docs)
    )
