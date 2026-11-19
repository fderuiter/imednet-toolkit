"""Prove the architecture guard rejects real syntax, including equivalent imports."""

import pytest
from import_rules import (
    EXTENSION_LABELS,
    NON_AIRFLOW_PACKAGES,
    import_edges,
    scan_package,
    violations,
)

CROSS_EXTENSION_PAIRS = [
    (src_pkg, src_label, dst_pkg, dst_label)
    for src_pkg, src_label in EXTENSION_LABELS.items()
    for dst_pkg, dst_label in EXTENSION_LABELS.items()
    if src_pkg != dst_pkg
]


@pytest.mark.parametrize(
    "statement",
    [
        "from imednet.cli import app",
        "from ..cli import app",
        "from .. import cli",
        "if TYPE_CHECKING:\n    from ..cli import app",
    ],
)
def test_absolute_relative_and_type_only_cli_edges_fail(tmp_path, statement):
    path = tmp_path / "imednet" / "core" / "example.py"
    path.parent.mkdir(parents=True)
    path.write_text(statement)
    errors = violations(path, tmp_path)
    assert any("core cannot import CLI" in error and str(path) in error for error in errors)
    assert "imednet.cli" in import_edges(path, tmp_path)


@pytest.mark.parametrize(
    ("module", "statement"),
    [
        ("imednet/spi/example.py", "from ..cli import app"),
        ("imednet_workflows/example.py", "from imednet.spi import models"),
        ("imednet_sinks/example.py", "from imednet.spi.sinks import ExportSink"),
        ("imednet_sinks/example.py", "from imednet.spi.sink_base import ExportSink"),
        ("imednet_sinks/example.py", "from imednet import ImednetSDK"),
    ],
)
def test_approved_composition_and_spi_pass(tmp_path, module, statement):
    path = tmp_path / module
    path.parent.mkdir(parents=True)
    path.write_text(statement)
    assert violations(path, tmp_path) == []


@pytest.mark.parametrize(
    "statement",
    [
        "from imednet.core import client",
        "from imednet.spurious import x",
        "from imednet.spi_private import x",
    ],
)
def test_extension_private_and_prefix_lookalikes_fail(tmp_path, statement):
    path = tmp_path / "imednet_sinks" / "sink.py"
    path.parent.mkdir()
    path.write_text(statement)
    assert any("extensions must use" in e for e in violations(path, tmp_path))


def test_missing_package_is_an_error(tmp_path):
    with pytest.raises(AssertionError, match="Missing expected source package"):
        scan_package(tmp_path, "imednet_sinks")


@pytest.mark.parametrize(
    "statement",
    [
        "import imednet_sinks",
        "from imednet_sinks import SnowflakeExportSink",
        "from imednet_sinks.warehouse import SnowflakeSinkConfig",
    ],
)
def test_airflow_provider_cannot_import_sinks(tmp_path, statement):
    path = tmp_path / "apache_airflow_providers_imednet" / "operators" / "export.py"
    path.parent.mkdir(parents=True)
    path.write_text(statement)
    errors = violations(path, tmp_path)
    assert any("Airflow provider cannot import sinks" in error for error in errors)


def test_cross_extension_pair_matrix_has_twelve_pairs():
    assert len(CROSS_EXTENSION_PAIRS) == 12


@pytest.mark.parametrize(
    ("src_pkg", "src_label", "dst_pkg", "dst_label"),
    CROSS_EXTENSION_PAIRS,
)
@pytest.mark.parametrize(
    "template",
    [
        "import {dst_pkg}",
        "from {dst_pkg} import something",
        "from {dst_pkg}.submodule import helper",
        "if TYPE_CHECKING:\n    import {dst_pkg}",
    ],
)
def test_all_twelve_cross_extension_pairs_fail_scan_package(
    tmp_path, src_pkg, src_label, dst_pkg, dst_label, template
):
    path = tmp_path / src_pkg / "sub" / "mod.py"
    path.parent.mkdir(parents=True)
    path.write_text(template.format(dst_pkg=dst_pkg))
    errors = scan_package(tmp_path, src_pkg)
    expected = f"{src_label} cannot import {dst_label}"
    assert any(expected in error and str(path) in error for error in errors)


@pytest.mark.parametrize("package", NON_AIRFLOW_PACKAGES)
@pytest.mark.parametrize(
    "statement",
    [
        "import airflow",
        "import airflow.sdk",
        "from airflow.models.xcom import XCom",
        "if TYPE_CHECKING:\n    from airflow.sdk import Variable",
    ],
)
def test_non_airflow_packages_cannot_import_airflow(tmp_path, package, statement):
    path = tmp_path / package / "feature.py"
    path.parent.mkdir(parents=True)
    path.write_text(statement)
    errors = violations(path, tmp_path)
    assert any("only Airflow provider may import airflow" in error for error in errors)


def test_airflow_provider_may_import_airflow(tmp_path):
    path = tmp_path / "apache_airflow_providers_imednet" / "operators" / "export.py"
    path.parent.mkdir(parents=True)
    path.write_text("import airflow\nfrom airflow.sdk import BaseOperator\n")
    assert violations(path, tmp_path) == []
