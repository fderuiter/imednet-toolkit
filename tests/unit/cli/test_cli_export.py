"""Unit tests for cli export."""

import importlib.util
from unittest.mock import MagicMock

import pytest
from imednet_sinks import MongoDbSinkConfig, Neo4jSinkConfig, SnowflakeSinkConfig


class Result:
    def __init__(self, exit_code, stdout, stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.output = stdout + stderr


class CliRunner:
    def invoke(self, app, args):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out = io.StringIO()
        err = io.StringIO()
        exit_code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                if hasattr(app, "parse_args"):
                    pass
                app(args)
        except SystemExit as e:
            exit_code = e.code or 0
        except Exception:
            import traceback

            err.write(traceback.format_exc())
            exit_code = 1

        # We also need to catch argparse sys.exit(2)
        return Result(exit_code, out.getvalue(), err.getvalue())


from imednet import cli
from imednet.integrations import export as export_mod


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Helper function to env."""
    monkeypatch.setenv("IMEDNET_API_KEY", "key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "secret")


@pytest.fixture
def runner() -> CliRunner:
    """Helper function to runner."""
    return CliRunner()


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Helper function to sdk."""
    mock_sdk = MagicMock()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))
    return mock_sdk


def test_cli_export_duckdb_happy_path(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export duckdb happy path."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_duckdb", func)
    monkeypatch.setattr(cli, "export_to_duckdb", export_mod.export_to_duckdb)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    result = runner.invoke(
        cli.app,
        ["export", "duckdb", "STUDY", "study_records", "out.duckdb"],
    )

    assert result.exit_code == 0
    func.assert_called_once_with(
        sdk,
        "STUDY",
        "out.duckdb",
        "study_records",
        use_labels_as_columns=False,
        variable_whitelist=None,
        form_whitelist=None,
    )


def test_cli_export_duckdb_missing_dependency(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export duckdb missing dependency."""
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str) -> object | None:
        """Helper function to fake find spec."""
        if name == "duckdb":
            return None
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    result = runner.invoke(
        cli.app,
        ["export", "duckdb", "STUDY", "table", "out.duckdb"],
    )

    assert result.exit_code == 1
    assert "imednet[duckdb]" in result.stderr
    assert result.stdout == ""


def test_cli_export_duckdb_vars_forms_passthrough(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export duckdb vars forms passthrough."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_duckdb", func)
    monkeypatch.setattr(cli, "export_to_duckdb", export_mod.export_to_duckdb)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    result = runner.invoke(
        cli.app,
        [
            "export",
            "duckdb",
            "STUDY",
            "study_records",
            "out.duckdb",
            "--vars",
            "A,B",
            "--forms",
            "1,2",
        ],
    )

    assert result.exit_code == 0
    func.assert_called_once_with(
        sdk,
        "STUDY",
        "out.duckdb",
        "study_records",
        use_labels_as_columns=False,
        variable_whitelist=["A", "B"],
        form_whitelist=[1, 2],
    )


@pytest.mark.parametrize(
    "invalid_table",
    [
        "my table",
        "records; DROP TABLE records; --",
        'records" OR 1=1',
        "bad-table",
        "table.name",
    ],
)
def test_cli_export_duckdb_rejects_invalid_table_name(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch, invalid_table: str
) -> None:
    """Test that CLI export duckdb rejects invalid table name before db operations."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_duckdb", func)
    monkeypatch.setattr(cli, "export_to_duckdb", export_mod.export_to_duckdb)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    result = runner.invoke(
        cli.app,
        ["export", "duckdb", "STUDY", invalid_table, "out.duckdb"],
    )

    assert result.exit_code == 1
    assert "Invalid identifier" in result.stderr or "Error" in result.stderr
    assert result.stdout == ""
    func.assert_not_called()


def test_cli_export_mongodb_happy_path(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export mongodb happy path."""
    sdk.sinks.MongoDbSinkConfig = MongoDbSinkConfig
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    result = runner.invoke(
        cli.app,
        [
            "export",
            "mongodb",
            "STUDY",
            "mongodb://localhost:27017",
            "imednet",
            "records",
            "--batch-size",
            "100",
            "--insert-only",
        ],
    )

    assert result.exit_code == 0
    sdk.sinks.export_to_mongodb.assert_called_once_with(
        sdk,
        "STUDY",
        config=MongoDbSinkConfig(
            study_key="STUDY",
            uri="mongodb://localhost:27017",
            database="imednet",
            collection="records",
            batch_size=100,
            idempotent=False,
        ),
    )


def test_cli_export_mongodb_missing_dependency(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export mongodb missing dependency."""
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str) -> object | None:
        """Helper function to fake find spec."""
        if name == "pymongo":
            return None
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    result = runner.invoke(
        cli.app,
        ["export", "mongodb", "STUDY", "mongodb://localhost:27017", "db", "records"],
    )

    assert result.exit_code == 1
    assert "imednet[mongodb]" in result.stderr
    assert result.stdout == ""


def test_cli_export_neo4j_happy_path(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export neo4j happy path."""
    sdk.sinks.Neo4jSinkConfig = Neo4jSinkConfig
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    result = runner.invoke(
        cli.app,
        [
            "export",
            "neo4j",
            "STUDY",
            "bolt://localhost:7687",
            "neo4j",
            "password",
            "--database",
            "analytics",
            "--batch-size",
            "250",
            "--create-only",
        ],
    )

    assert result.exit_code == 0
    sdk.sinks.export_to_neo4j.assert_called_once_with(
        sdk,
        "STUDY",
        config=Neo4jSinkConfig(
            study_key="STUDY",
            uri="bolt://localhost:7687",
            auth=("neo4j", "password"),
            batch_size=250,
            idempotent=False,
            database="analytics",
        ),
    )


def test_cli_export_neo4j_missing_dependency(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export neo4j missing dependency."""
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str) -> object | None:
        """Helper function to fake find spec."""
        if name == "neo4j":
            return None
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    result = runner.invoke(
        cli.app,
        ["export", "neo4j", "STUDY", "bolt://localhost:7687", "neo4j", "password"],
    )

    assert result.exit_code == 1
    assert "imednet[neo4j]" in result.stderr
    assert result.stdout == ""


def test_cli_export_snowflake_happy_path(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export snowflake happy path."""
    sdk.sinks.SnowflakeSinkConfig = SnowflakeSinkConfig
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    result = runner.invoke(
        cli.app,
        [
            "export",
            "snowflake",
            "STUDY",
            "acct",
            "user",
            "secret",
            "DB",
            "PUBLIC",
            "WH",
            "STAGE",
            "TBL",
            "--stage-prefix",
            "imednet/dev",
            "--batch-size",
            "200",
            "--force-reload",
        ],
    )

    assert result.exit_code == 0
    sdk.sinks.export_to_snowflake.assert_called_once_with(
        sdk,
        "STUDY",
        config=SnowflakeSinkConfig(
            study_key="STUDY",
            account="acct",
            user="user",
            password="secret",
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STAGE",
            table="TBL",
            stage_prefix="imednet/dev",
            batch_size=200,
            idempotent=False,
        ),
    )


def test_cli_export_snowflake_missing_dependency(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export snowflake missing dependency."""
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str) -> object | None:
        """Helper function to fake find spec."""
        if name == "snowflake.connector":
            return None
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    result = runner.invoke(
        cli.app,
        ["export", "snowflake", "STUDY", "acct", "user", "secret", "DB", "PUBLIC", "WH", "S", "T"],
    )

    assert result.exit_code == 1
    assert "imednet[snowflake]" in result.stderr
    assert result.stdout == ""
