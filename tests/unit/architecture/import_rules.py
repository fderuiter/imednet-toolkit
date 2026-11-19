"""Static package boundary checks, including relative and type-only imports."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

PUBLIC_CORE = (
    "imednet.spi",
    "imednet.sdk",
    "imednet.config",
    "imednet.errors",
)
EXTENSION_LABELS = {
    "imednet_workflows": "workflows",
    "imednet_sinks": "sinks",
    "imednet_tui": "TUI",
    "apache_airflow_providers_imednet": "Airflow provider",
}
NON_AIRFLOW_PACKAGES = (
    "imednet",
    "imednet_workflows",
    "imednet_sinks",
    "imednet_tui",
)
PUBLIC_SYMBOLS = {
    "ImednetSDK",
    "AsyncImednetSDK",
    "Config",
    "PluginLoadError",
    "FilterConflictError",
    "OrchestratorError",
    "MultiStudyOrchestrator",
    "OrchestratorResult",
    "StudyWorkerCallable",
    "PluginProtocol",
    "WorkflowsNamespaceProtocol",
    "BaseClient",
    "DefaultRetryPolicy",
    "FilterScalar",
    "FilterValue",
    "ImednetClient",
    "ItemId",
    "JsonDict",
    "RetryPolicy",
    "RetryState",
    "load_config",
}


def within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def import_edges(path: Path, source_root: Path) -> set[str]:
    package = ".".join(path.relative_to(source_root).parts[:-1])
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    edges: set[str] = set()
    # Type-only imports follow the same layering rules, but are never imported/executed.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            edges.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            if module:
                edges.add(module)
                edges.update(f"{module}.{alias.name}" for alias in node.names)
    return edges


def violations(path: Path, source_root: Path) -> list[str]:
    module = ".".join(path.relative_to(source_root).with_suffix("").parts)
    errors = []
    for edge in sorted(import_edges(path, source_root)):
        rules: list[str] = []
        if within(module, "imednet"):
            if within(edge, "imednet.cli") and not any(
                within(module, prefix) for prefix in ("imednet.cli", "imednet.spi")
            ):
                rules.append("core cannot import CLI")
            elif any(within(edge, prefix) for prefix in EXTENSION_LABELS):
                rules.append("core cannot statically import extensions")
        elif within(edge, "imednet") and edge != "imednet":
            public_symbol = edge.removeprefix("imednet.") in PUBLIC_SYMBOLS
            if not public_symbol and not any(within(edge, prefix) for prefix in PUBLIC_CORE):
                rules.append("extensions must use public API or SPI")
        for src_pkg, src_label in EXTENSION_LABELS.items():
            if not within(module, src_pkg):
                continue
            for dst_pkg, dst_label in EXTENSION_LABELS.items():
                if src_pkg != dst_pkg and within(edge, dst_pkg):
                    rules.append(f"{src_label} cannot import {dst_label}")
        if any(within(module, pkg) for pkg in NON_AIRFLOW_PACKAGES) and within(edge, "airflow"):
            rules.append("only Airflow provider may import airflow")
        for rule in rules:
            errors.append(f"{path}: {module} -> {edge}: {rule}")
    return errors


def scan_package(source_root: Path, package: str) -> list[str]:
    directory = source_root / package
    files = sorted(directory.rglob("*.py"))
    assert directory.is_dir() and files, f"Missing expected source package: {directory}"
    return [error for path in files for error in violations(path, source_root)]
