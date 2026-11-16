"""Prove the architecture guard rejects real syntax, including equivalent imports."""

import pytest
from import_rules import import_edges, scan_package, violations


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
