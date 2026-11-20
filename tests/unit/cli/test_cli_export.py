"""Unit tests for cli export."""

import importlib.util
from pathlib import Path
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


def test_cli_export_parquet_with_use_labels(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export parquet passes use_labels_as_columns=True."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_parquet", func)
    monkeypatch.setattr(cli, "export_to_parquet", export_mod.export_to_parquet)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    result = runner.invoke(cli.app, ["export", "parquet", "STUDY", "out.parquet", "--use-labels"])
    assert result.exit_code == 0
    func.assert_called_once_with(sdk, "STUDY", "out.parquet", use_labels_as_columns=True)


def test_cli_export_csv_with_use_labels(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export csv passes use_labels_as_columns=True."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_csv", func)
    monkeypatch.setattr(cli, "export_to_csv", export_mod.export_to_csv)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    result = runner.invoke(cli.app, ["export", "csv", "STUDY", "out.csv", "--use-labels"])
    assert result.exit_code == 0
    func.assert_called_once_with(sdk, "STUDY", "out.csv", use_labels_as_columns=True)


def test_cli_export_excel_with_use_labels(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export excel passes use_labels_as_columns=True."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_excel", func)
    monkeypatch.setattr(cli, "export_to_excel", export_mod.export_to_excel)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    result = runner.invoke(cli.app, ["export", "excel", "STUDY", "out.xlsx", "--use-labels"])
    assert result.exit_code == 0
    func.assert_called_once_with(sdk, "STUDY", "out.xlsx", use_labels_as_columns=True)


def test_cli_export_json_with_use_labels(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export json passes use_labels_as_columns=True."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_json", func)
    monkeypatch.setattr(cli, "export_to_json", export_mod.export_to_json)

    result = runner.invoke(cli.app, ["export", "json", "STUDY", "out.json", "--use-labels"])
    assert result.exit_code == 0
    func.assert_called_once_with(
        sdk, "STUDY", "out.json", use_labels_as_columns=True, hierarchical=False
    )


def test_cli_export_json_with_hierarchical(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export json passes hierarchical=True."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_json", func)
    monkeypatch.setattr(cli, "export_to_json", export_mod.export_to_json)

    result = runner.invoke(cli.app, ["export", "json", "STUDY", "out.json", "--hierarchical"])
    assert result.exit_code == 0
    func.assert_called_once_with(
        sdk, "STUDY", "out.json", use_labels_as_columns=False, hierarchical=True
    )


def test_cli_export_json_with_both_flags(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that cli export json passes both use_labels_as_columns=True and hierarchical=True."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_json", func)
    monkeypatch.setattr(cli, "export_to_json", export_mod.export_to_json)

    result = runner.invoke(
        cli.app, ["export", "json", "STUDY", "out.json", "--use-labels", "--hierarchical"]
    )
    assert result.exit_code == 0
    func.assert_called_once_with(
        sdk, "STUDY", "out.json", use_labels_as_columns=True, hierarchical=True
    )


@pytest.mark.parametrize("subcommand", ["parquet", "csv", "excel", "json"])
def test_cli_export_subcommand_help_includes_use_labels(runner: CliRunner, subcommand: str) -> None:
    """Test that tabular and json export subcommands include --use-labels in help."""
    result = runner.invoke(cli.app, ["export", subcommand, "--help"])
    assert result.exit_code == 0
    assert "--use-labels" in result.stdout


def test_cli_export_json_help_includes_hierarchical(runner: CliRunner) -> None:
    """Test that json export subcommand includes --hierarchical in help."""
    result = runner.invoke(cli.app, ["export", "json", "--help"])
    assert result.exit_code == 0
    assert "--hierarchical" in result.stdout


@pytest.mark.parametrize(
    "subcommand",
    ["parquet", "csv", "excel", "json", "duckdb", "sql", "mongodb", "neo4j", "snowflake"],
)
def test_cli_export_subcommands_expose_mask_phi_and_quality_gate_flags(
    runner: CliRunner, subcommand: str
) -> None:
    """Verify every export subcommand exposes --no-mask-phi and --quality-gate in --help."""
    result = runner.invoke(cli.app, ["export", subcommand, "--help"])
    assert result.exit_code == 0
    assert "--no-mask-phi" in result.stdout
    assert "--quality-gate" in result.stdout
    assert "--min-readiness-score" in result.stdout


def test_cli_export_no_mask_phi_and_quality_gate_passthrough(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify --no-mask-phi and --quality-gate propagate to SDK helpers and SinkConfig classes."""
    sdk.sinks.Neo4jSinkConfig = Neo4jSinkConfig
    sdk.sinks.MongoDbSinkConfig = MongoDbSinkConfig
    sdk.sinks.SnowflakeSinkConfig = SnowflakeSinkConfig
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    json_func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_json", json_func)
    monkeypatch.setattr(cli, "export_to_json", export_mod.export_to_json)

    res_json = runner.invoke(
        cli.app,
        [
            "export",
            "json",
            "STUDY",
            "out.json",
            "--hierarchical",
            "--no-mask-phi",
            "--quality-gate",
            "--min-readiness-score",
            "92.5",
        ],
    )
    assert res_json.exit_code == 0
    json_func.assert_called_once_with(
        sdk,
        "STUDY",
        "out.json",
        use_labels_as_columns=False,
        hierarchical=True,
        mask_phi=False,
        quality_gate_enabled=True,
        min_schema_readiness_score=92.5,
    )

    res_neo = runner.invoke(
        cli.app,
        [
            "export",
            "neo4j",
            "STUDY",
            "bolt://localhost:7687",
            "neo4j",
            "pass",
            "--no-mask-phi",
            "--quality-gate",
            "--min-readiness-score",
            "88.0",
        ],
    )
    assert res_neo.exit_code == 0
    sdk.sinks.export_to_neo4j.assert_called_once_with(
        sdk,
        "STUDY",
        config=Neo4jSinkConfig(
            study_key="STUDY",
            uri="bolt://localhost:7687",
            auth=("neo4j", "pass"),
            mask_phi=False,
            quality_gate_enabled=True,
            min_schema_readiness_score=88.0,
        ),
    )


@pytest.mark.parametrize(
    "subcmd",
    [
        "parquet",
        "csv",
        "excel",
        "json",
        "duckdb",
        "sql",
        "mongodb",
        "neo4j",
        "snowflake",
    ],
)
def test_cli_export_subcommands_expose_sdtm_and_standards_flags(
    runner: CliRunner, subcmd: str
) -> None:
    """All 9 export subcommands expose --as-sdtm, --sdtm-domain, --standards-profile, --min-standards-score, and --naming-convention."""
    result = runner.invoke(cli.app, ["export", subcmd, "--help"])
    assert result.exit_code == 0
    for flag in (
        "--as-sdtm",
        "--sdtm-domain",
        "--standards-profile",
        "--min-standards-score",
        "--naming-convention",
    ):
        assert flag in result.stdout


def test_cli_export_sdtm_passthrough_and_non_conforming_study_gate(
    runner: CliRunner,
    sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CLI passes SDTM/standards flags to tabular and plugin sinks and exits 1 when standards score fails."""
    from imednet.integrations import export as real_export
    from imednet.models.forms import Form
    from imednet.models.records import Record

    saved_export_to_csv = real_export.export_to_csv
    csv_mock = MagicMock()
    monkeypatch.setattr(real_export, "export_to_csv", csv_mock)
    monkeypatch.setattr(cli, "export_to_csv", csv_mock)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())

    res_csv = runner.invoke(
        cli.app,
        [
            "export",
            "csv",
            "STUDY_SDTM",
            "ae.csv",
            "--as-sdtm",
            "--sdtm-domain",
            "AE",
            "--standards-profile",
            "drug",
            "--min-standards-score",
            "95",
            "--naming-convention",
            "sdtm",
        ],
    )
    assert res_csv.exit_code == 0
    csv_mock.assert_called_once_with(
        sdk,
        "STUDY_SDTM",
        "ae.csv",
        use_labels_as_columns=False,
        as_sdtm=True,
        sdtm_domain="AE",
        standards_profile="drug",
        min_standards_score=95.0,
        naming_convention="sdtm",
    )

    sdk.sinks = MagicMock()
    sdk.sinks.SnowflakeSinkConfig = SnowflakeSinkConfig
    res_sf = runner.invoke(
        cli.app,
        [
            "export",
            "snowflake",
            "STUDY_SDTM",
            "acct",
            "usr",
            "pwd",
            "DB",
            "PUBLIC",
            "WH",
            "STG",
            "AE_TBL",
            "--domain",
            "AE",
            "--standards-profile",
            "drug",
            "--min-standards-score",
            "90",
            "--naming-convention",
            "sdtm",
        ],
    )
    assert res_sf.exit_code == 0
    sdk.sinks.export_to_snowflake.assert_called_once_with(
        sdk,
        "STUDY_SDTM",
        config=SnowflakeSinkConfig(
            study_key="STUDY_SDTM",
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="AE_TBL",
            as_sdtm=True,
            sdtm_domain="AE",
            standards_profile="drug",
            min_standards_score=90.0,
            naming_convention="sdtm",
        ),
    )

    # Real end-to-end gating check via CLI (unpatched export_to_csv): exits 1 before writing
    monkeypatch.setattr(real_export, "export_to_csv", saved_export_to_csv)
    monkeypatch.setattr(cli, "export_to_csv", saved_export_to_csv)
    sdk.forms.list.return_value = [Form(form_id=1, form_key="AE", form_name="AE")]
    sdk.variables.list.return_value = []
    sdk.records.list.side_effect = lambda **_kw: iter(
        [
            Record(
                record_id=1,
                study_key="NON_CONFORMING",
                form_id=1,
                form_key="AE",
                subject_key="S1",
                record_data={"aeTerm": "Nausea", "aeSeverity": "SEVERE"},
            )
        ]
    )
    gated_path = tmp_path / "blocked.csv"
    res_blocked = runner.invoke(
        cli.app,
        [
            "export",
            "csv",
            "NON_CONFORMING",
            str(gated_path),
            "--as-sdtm",
            "--sdtm-domain",
            "AE",
            "--standards-profile",
            "drug",
            "--min-standards-score",
            "95",
        ],
    )
    assert res_blocked.exit_code == 1
    assert not gated_path.exists()


def test_cli_export_json_hierarchical_honors_incremental_filters_ledger_and_quality_gate(
    runner: CliRunner,
    sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify `imednet export json --hierarchical` applies streaming filters, ledger checkpoints, and quality gate."""
    import json
    from datetime import UTC, datetime

    from imednet.models.forms import Form
    from imednet.models.intervals import FormSummary, Interval
    from imednet.models.records import Record
    from imednet.models.variables import Variable
    from imednet_workflows.state_ledger import FileStateProvider

    vars_list = [
        Variable(
            variable_name="SCORE", label="Score", variable_type="int", form_id=10, form_key="F_VS"
        )
    ]
    forms_list = [
        Form(
            form_id=10,
            form_key="F_VS",
            form_name="Vital Signs",
            form_type="Standard",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
        )
    ]
    intervals_list = [
        Interval(
            interval_id=1,
            interval_name="V1",
            interval_sequence=1,
            interval_description="V1",
            interval_group_name="G1",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
            forms=[FormSummary(form_id=10, form_key="F_VS", form_name="Vital Signs")],
        )
    ]
    sdk.get_variables.return_value = vars_list
    sdk.variables.list.return_value = vars_list
    sdk.get_forms.return_value = forms_list
    sdk.forms.list.return_value = forms_list
    sdk.get_intervals.return_value = intervals_list

    r1 = Record(
        record_id=1,
        study_key="S_CLI",
        subject_key="SUBJ-1",
        visit_id=1,
        form_id=10,
        form_key="F_VS",
        record_status="Complete",
        date_created=datetime(2026, 6, 1, tzinfo=UTC),
        date_modified=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        record_data={"SCORE": 85},
    )
    r2_filtered_out = Record(
        record_id=2,
        study_key="S_CLI",
        subject_key="SUBJ-2",
        visit_id=1,
        form_id=10,
        form_key="F_VS",
        record_status="Complete",
        date_created=datetime(2026, 6, 1, tzinfo=UTC),
        date_modified=datetime(2026, 6, 1, 10, 5, tzinfo=UTC),
        record_data={"SCORE": 20},
    )
    seen_calls: list[dict[str, object]] = []

    def _matches_subj1(call_kw: dict[str, object]) -> bool:
        for key in ("subject_key", "subjectKey"):
            val = call_kw.get(key)
            if val in {"SUBJ-1", ("==", "SUBJ-1"), ("=", "SUBJ-1")}:
                return True
        return "subjectKey==SUBJ-1" in str(call_kw.get("filter", ""))

    def _list_records(**kw: object) -> object:
        seen_calls.append(dict(kw))
        rdf = str(kw.get("record_data_filter", "") or "")
        rows = [r1, r2_filtered_out]
        if _matches_subj1(dict(kw)):
            rows = [r for r in rows if r.subject_key == "SUBJ-1"]
        if "SCORE>=50" in rdf:
            rows = [r for r in rows if int((r.record_data or {}).get("SCORE", 0)) >= 50]
        return iter([r.model_copy(deep=True) for r in rows])

    sdk.records.list.side_effect = _list_records
    monkeypatch.setattr(cli, "export_to_json", export_mod.export_to_json)

    out_file = tmp_path / "cli_hier.json"
    ledger_file = tmp_path / "ledger.json"

    res1 = runner.invoke(
        cli.app,
        [
            "export",
            "json",
            "S_CLI",
            str(out_file),
            "--hierarchical",
            "--incremental",
            "--ledger-path",
            str(ledger_file),
            "--since",
            "2026-06-01T00:00:00Z",
            "--filter",
            "subjectKey==SUBJ-1",
            "--record-data-filter",
            "SCORE>=50",
            "--quality-gate",
            "--min-readiness-score",
            "0.0",
        ],
    )
    assert res1.exit_code == 0, res1.output
    data1 = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(data1) == 1
    assert data1[0]["subject_key"] == "SUBJ-1"
    assert data1[0]["visits"][0]["forms"][0]["records"][0]["recordId"] == 1
    assert any(
        _matches_subj1(c)
        and (
            isinstance(c.get("dateModified"), tuple) or "dateModified>=" in str(c.get("filter", ""))
        )
        and c.get("record_data_filter") == "SCORE>=50"
        for c in seen_calls
    ), f"Unexpected SDK list calls: {seen_calls}"

    provider = FileStateProvider(str(ledger_file))
    assert provider.get_last_timestamp("S_CLI", "json_hierarchical") == datetime(
        2026, 6, 1, 10, 0, tzinfo=UTC
    )

    # Zero-delta incremental CLI rerun preserves existing hierarchical archive
    sdk.records.list.side_effect = lambda **_kw: iter([])
    res2 = runner.invoke(
        cli.app,
        [
            "export",
            "json",
            "S_CLI",
            str(out_file),
            "--hierarchical",
            "--incremental",
            "--ledger-path",
            str(ledger_file),
        ],
    )
    assert res2.exit_code == 0, res2.output
    data2 = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(data2) == 1
    assert data2[0]["visits"][0]["forms"][0]["records"][0]["recordId"] == 1
