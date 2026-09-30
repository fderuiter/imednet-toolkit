"""Unit tests for architecture."""

import ast
from pathlib import Path

import pytest
from httpx import AsyncClient, Client

import imednet
from imednet.sdk import AsyncImednetSDK, ImednetSDK

try:
    import imednet_workflows
except ImportError:
    imednet_workflows = None


def get_all_python_files(package_path: Path) -> list[Path]:
    """Helper function to get all python files."""
    return list(package_path.rglob("*.py"))


def get_imports_from_file(file_path: Path) -> set[str]:
    """Helper function to get imports from file."""
    with open(file_path, encoding="utf-8") as f:
        # We want SyntaxError to be raised if a file cannot be parsed, rather than silently ignored.
        tree = ast.parse(f.read(), filename=str(file_path))

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                imports.add(name.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
            for name in node.names:
                imports.add(f"{node.module}.{name.name}")
    return imports


def test_core_does_not_import_cli():
    """Ensure no file outside of imednet/cli/ imports the cli module."""
    core_dir = Path(imednet.__file__).parent
    cli_dir = core_dir / "cli"
    spi_dir = core_dir / "spi"

    for file in get_all_python_files(core_dir):
        # Skip CLI directory itself and its subdirectories
        if cli_dir in file.parents or file.parent == cli_dir:
            continue
        # Skip SPI directory because it intentionally re-exports the CLI module for extensions
        if spi_dir in file.parents or file.parent == spi_dir:
            continue

        imports = get_imports_from_file(file)
        for imp in imports:
            assert not imp.startswith("imednet.cli"), (
                f"Architectural violation: {file} imports '{imp}' from the CLI layer"
            )


def test_core_does_not_import_workflows():
    """Test that core does not import workflows."""
    core_dir = Path(imednet.__file__).parent
    files = get_all_python_files(core_dir)
    assert len(files) > 0, f"No files found to test in {core_dir}"

    for file in files:
        imports = get_imports_from_file(file)
        for imp in imports:
            assert not imp.startswith("imednet_workflows"), f"File {file} has hard import of {imp}"
            assert not imp.startswith("apache_airflow_providers_imednet"), (
                f"File {file} has hard import of {imp}"
            )


def test_workflows_does_not_import_providers():
    """Test that workflows does not import providers."""
    if imednet_workflows is None:
        pytest.skip("imednet_workflows not installed")

    workflows_dir = Path(imednet_workflows.__file__).parent
    files = get_all_python_files(workflows_dir)
    assert len(files) > 0, f"No files found to test in {workflows_dir}"

    for file in files:
        imports = get_imports_from_file(file)
        for imp in imports:
            assert not imp.startswith("apache_airflow_providers_imednet"), (
                f"File {file} has hard import of {imp}"
            )


def test_extensions_use_spi():
    """Ensure extension packages use the SPI or top-level public API and not core internals."""
    # We will check packages/plugins-workflows and packages/providers-airflow
    app_dir = Path(imednet.__file__).parent.parent.parent.parent
    packages_dir = app_dir

    extension_dirs = [
        packages_dir / "plugins-workflows" / "src",
        packages_dir / "providers-airflow" / "src",
    ]

    for ext_dir in extension_dirs:
        if not ext_dir.exists():
            continue

        for file in get_all_python_files(ext_dir):
            imports = get_imports_from_file(file)
            for imp in imports:
                # If they import imednet, it must be the top level (e.g. `imednet` itself)
                # or through the SPI `imednet.spi`
                allowed_prefixes = (
                    "imednet.spi",
                    "imednet.sdk",
                    "imednet.config",
                    "imednet.errors",
                    "imednet.ImednetSDK",
                    "imednet.AsyncImednetSDK",
                    "imednet.Config",
                    "imednet.PluginLoadError",
                    "imednet.FilterConflictError",
                    "imednet.OrchestratorError",
                    "imednet.MultiStudyOrchestrator",
                    "imednet.OrchestratorResult",
                    "imednet.StudyWorkerCallable",
                    "imednet.PluginProtocol",
                    "imednet.WorkflowsNamespaceProtocol",
                    "imednet.BaseClient",
                    "imednet.DefaultRetryPolicy",
                    "imednet.FilterScalar",
                    "imednet.FilterValue",
                    "imednet.ImednetClient",
                    "imednet.ItemId",
                    "imednet.JsonDict",
                    "imednet.RetryPolicy",
                    "imednet.RetryState",
                    "imednet.load_config",
                )
                if imp.startswith("imednet.") and not imp.startswith(allowed_prefixes):
                    # The only allowed exceptions are imednet.sdk, imednet.config or something imported via top-level.
                    # Usually, they should just `from imednet import ImednetSDK` which is `imp == "imednet"`
                    # If imp == "imednet", we don't catch it here.
                    pytest.fail(
                        f"Architectural violation: {file} imports '{imp}'. "
                        "Extensions must use the SPI ('imednet.spi') instead of core internals."
                    )


def test_endpoint_no_shared_mutable_state():
    """Test that endpoint no shared mutable state."""
    sdk = ImednetSDK(api_key="1", security_key="2", base_url="http://x")

    assert sdk.records is not sdk.forms

    if hasattr(sdk.records, "_cache"):
        assert sdk.records._cache is not getattr(sdk.forms, "_cache", None)

    # Let's instantiate another SDK and ensure endpoint instances are distinct
    sdk2 = ImednetSDK(api_key="1", security_key="2", base_url="http://x")
    assert sdk.records is not sdk2.records
    assert sdk._client is not sdk2._client


def test_sync_sdk_no_async_client(monkeypatch):
    """Test that sync sdk no async client."""
    instantiated = False
    original_init = AsyncClient.__init__

    def mock_init(self, *args, **kwargs):
        """Helper function to mock init."""
        nonlocal instantiated
        instantiated = True
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(AsyncClient, "__init__", mock_init)

    with ImednetSDK(api_key="1", security_key="2", base_url="http://x"):
        pass
    assert not instantiated, "ImednetSDK should not instantiate AsyncClient"


def test_async_sdk_no_sync_client(monkeypatch):
    """Test that async sdk no sync client."""
    instantiated = False
    original_init = Client.__init__

    def mock_init(self, *args, **kwargs):
        """Helper function to mock init."""
        nonlocal instantiated
        instantiated = True
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(Client, "__init__", mock_init)

    AsyncImednetSDK(api_key="1", security_key="2", base_url="http://x")
    assert not instantiated, "AsyncImednetSDK should not instantiate Client"


def test_plugin_discovery_failure(monkeypatch):
    """Test that plugin discovery failure."""
    monkeypatch.setattr("imednet.sdk.entry_points", lambda *, group, name=None: [])

    sdk = ImednetSDK(api_key="1", security_key="2", base_url="http://x")
    with pytest.raises(
        ImportError,
        match="Workflow 'some_workflow' not found. Please install the required package.",
    ):
        sdk.workflows.some_workflow
