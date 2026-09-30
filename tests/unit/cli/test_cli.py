"""Unit tests for cli."""

import importlib
import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest


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
from imednet.errors import ApiError
from imednet.integrations import export as export_mod


@pytest.fixture(autouse=True)
def reload_cli():
    """Ensure cli module is fresh and has all original commands bound to app."""
    importlib.reload(cli)
    yield
    importlib.reload(cli)


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set required environment variables for each test."""
    monkeypatch.setenv("IMEDNET_API_KEY", "key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "secret")


@pytest.fixture
def runner() -> CliRunner:
    """Helper function to runner."""
    return CliRunner()


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Provide a mocked SDK and patch get_sdk."""
    mock_sdk = MagicMock()
    # Patch the source of get_sdk used by decorators
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))
    return mock_sdk


def test_missing_env_vars(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that missing env vars."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    result = runner.invoke(cli.app, ["studies", "list"])
    assert result.exit_code == 1
    assert "IMEDNET_API_KEY" in result.stdout


def test_studies_list_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that studies list success."""
    obj = MagicMock()
    obj.study_key = "study1"
    obj.study_name = "Study One"
    obj.study_type = "Type"
    obj.sponsor_key = "Sponsor"
    sdk.studies.list.return_value = [obj]
    result = runner.invoke(cli.app, ["studies", "list"])
    assert result.exit_code == 0
    sdk.studies.list.assert_called_once_with()
    assert "study1" in result.stdout


def test_studies_list_api_error(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that studies list api error."""
    sdk.studies.list.side_effect = ApiError("boom")
    result = runner.invoke(cli.app, ["studies", "list"])
    assert result.exit_code == 1
    assert "API Error" in result.stdout


def test_sdk_closed_after_command(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that sdk closed after command."""
    sdk.studies.list.return_value = []
    result = runner.invoke(cli.app, ["studies", "list"])
    assert result.exit_code == 0
    sdk.close.assert_called_once()


def test_multiple_invocations_close_sdk(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that multiple invocations close sdk."""
    sdk.studies.list.return_value = []
    first = runner.invoke(cli.app, ["studies", "list"])
    second = runner.invoke(cli.app, ["studies", "list"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert sdk.close.call_count == 2


def test_sites_list_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that sites list success."""
    obj = MagicMock()
    obj.site_id = "site1"
    obj.site_name = "Site One"
    obj.site_enrollment_status = "Active"
    sdk.sites.list.return_value = [obj]
    result = runner.invoke(cli.app, ["sites", "list", "STUDY"])
    assert result.exit_code == 0
    sdk.sites.list.assert_called_once_with("STUDY")
    assert "site1" in result.stdout


def test_sites_list_missing_argument(runner: CliRunner) -> None:
    """Test that sites list missing argument."""
    result = runner.invoke(cli.app, ["sites", "list"])
    assert result.exit_code != 0


def test_sites_list_api_error(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that sites list api error."""
    sdk.sites.list.side_effect = ApiError("fail")
    result = runner.invoke(cli.app, ["sites", "list", "STUDY"])
    assert result.exit_code == 1
    assert "API Error" in result.stdout


def test_subjects_list_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that subjects list success."""
    mock_subject = MagicMock()
    mock_subject.subject_key = "S1"
    mock_subject.subject_status = "Screened"
    mock_subject.site_name = "Site 1"
    mock_subject.enrollment_start_date = datetime(2023, 1, 1)
    mock_subject.keywords = [MagicMock(keyword_name="Diabetes")]

    sdk.subjects.list.return_value = [mock_subject]
    result = runner.invoke(
        cli.app,
        ["subjects", "list", "STUDY", "--filter", "subject_status=Screened"],
    )
    assert result.exit_code == 0
    sdk.subjects.list.assert_called_once_with("STUDY", subject_status="Screened")
    assert "S1" in result.stdout
    assert "Diabetes" in result.stdout


def test_subjects_list_invalid_filter(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that subjects list invalid filter."""
    result = runner.invoke(cli.app, ["subjects", "list", "STUDY", "--filter", "badfilter"])
    assert result.exit_code == 1
    assert "Invalid filter format" in result.stdout
    sdk.subjects.list.assert_not_called()


def test_subjects_list_api_error(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that subjects list api error."""
    sdk.subjects.list.side_effect = ApiError("boom")
    result = runner.invoke(cli.app, ["subjects", "list", "STUDY"])
    assert result.exit_code == 1
    assert "API Error" in result.stdout


def test_extract_records_calls_workflow(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that extract records calls workflow."""
    workflow = MagicMock()
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow", MagicMock(return_value=workflow)
    )
    workflow.extract_records_by_criteria.return_value = [1]
    result = runner.invoke(
        cli.app,
        ["workflows", "extract-records", "STUDY", "--record-filter", "form_key=DEMOG"],
    )
    assert result.exit_code == 0
    workflow.extract_records_by_criteria.assert_called_once_with(
        study_key="STUDY",
        record_filter={"form_key": "DEMOG"},
        subject_filter=None,
        visit_filter=None,
    )


def test_extract_records_api_error(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI surfaces workflow errors."""
    workflow = MagicMock()
    workflow.extract_records_by_criteria.side_effect = ApiError("fail")
    monkeypatch.setattr(
        "imednet_workflows.cli.DataExtractionWorkflow", MagicMock(return_value=workflow)
    )
    result = runner.invoke(cli.app, ["workflows", "extract-records", "STUDY"])
    assert result.exit_code == 1
    assert "API Error" in result.stdout


def test_records_list_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that records list success."""
    rec = MagicMock()
    rec.record_id = 1
    rec.subject_key = "S1"
    rec.form_key = "F1"
    rec.record_status = "Active"
    rec.date_created = "2023-01-01"
    sdk.records.list.return_value = [rec]
    result = runner.invoke(cli.app, ["records", "list", "STUDY"])
    assert result.exit_code == 0
    sdk.records.list.assert_called_once_with("STUDY")
    assert "S1" in result.stdout


def test_records_list_output_csv(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test that records list output csv."""
    rec = MagicMock()
    rec.model_dump.return_value = {"recordId": 1}
    sdk.records.list.return_value = [rec]
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["records", "list", "STUDY", "--output", "csv"])
    assert result.exit_code == 0
    assert os.path.exists("records.csv")
    sdk.records.list.assert_called_once_with("STUDY")


def test_records_list_output_json(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test that records list output json."""
    rec = MagicMock()
    rec.model_dump.return_value = {"recordId": 1}
    sdk.records.list.return_value = [rec]
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["records", "list", "STUDY", "--output", "json"])
    assert result.exit_code == 0
    assert os.path.exists("records.json")
    sdk.records.list.assert_called_once_with("STUDY")


def test_records_list_no_records(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that records list no records."""
    sdk.records.list.return_value = []
    result = runner.invoke(cli.app, ["records", "list", "STUDY"])
    assert result.exit_code == 0
    assert "No records found." in result.stdout
    sdk.records.list.assert_called_once_with("STUDY")


def test_records_list_invalid_output(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that records list invalid output."""
    result = runner.invoke(cli.app, ["records", "list", "STUDY", "--output", "txt"])
    assert result.exit_code == 1
    assert "Invalid output format" in result.stdout
    sdk.records.list.assert_not_called()


def test_records_list_api_error(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that records list api error."""
    sdk.records.list.side_effect = ApiError("oops")
    result = runner.invoke(cli.app, ["records", "list", "STUDY"])
    assert result.exit_code == 1
    assert "API Error" in result.stdout


def test_export_parquet_calls_helper(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export parquet calls helper."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_parquet", func)
    monkeypatch.setattr(cli, "export_to_parquet", export_mod.export_to_parquet)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    result = runner.invoke(cli.app, ["export", "parquet", "STUDY", "out.parquet"])
    assert result.exit_code == 0
    func.assert_called_once_with(sdk, "STUDY", "out.parquet")


def test_export_csv_calls_helper(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export csv calls helper."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_csv", func)
    monkeypatch.setattr(cli, "export_to_csv", export_mod.export_to_csv)
    result = runner.invoke(cli.app, ["export", "csv", "STUDY", "out.csv"])
    assert result.exit_code == 0
    func.assert_called_once_with(sdk, "STUDY", "out.csv")


def test_export_excel_calls_helper(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export excel calls helper."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_excel", func)
    monkeypatch.setattr(cli, "export_to_excel", export_mod.export_to_excel)
    result = runner.invoke(cli.app, ["export", "excel", "STUDY", "out.xlsx"])
    assert result.exit_code == 0
    func.assert_called_once_with(sdk, "STUDY", "out.xlsx")


def test_export_json_calls_helper(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export json calls helper."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_json", func)
    monkeypatch.setattr(cli, "export_to_json", export_mod.export_to_json)
    result = runner.invoke(cli.app, ["export", "json", "STUDY", "out.json"])
    assert result.exit_code == 0
    func.assert_called_once_with(sdk, "STUDY", "out.json")


def test_export_duckdb_calls_helper(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export duckdb calls helper."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_duckdb", func)
    monkeypatch.setattr(cli, "export_to_duckdb", export_mod.export_to_duckdb)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
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
            "--use-labels",
        ],
    )
    assert result.exit_code == 0
    func.assert_called_once_with(
        sdk,
        "STUDY",
        "out.duckdb",
        "study_records",
        use_labels_as_columns=True,
        variable_whitelist=["A", "B"],
        form_whitelist=[1, 2],
    )


def test_export_duckdb_help(runner: CliRunner) -> None:
    """Test that export duckdb help."""
    result = runner.invoke(cli.app, ["export", "duckdb", "--help"])
    assert result.exit_code == 0
    assert "The key identifying the study" in result.stdout
    assert "table_name" in result.stdout
    assert "db_path" in result.stdout
    assert "vars" in result.stdout
    assert "forms" in result.stdout
    assert "labels" in result.stdout


def test_export_sql_calls_helper_non_sqlite(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export sql calls helper non sqlite."""
    func = MagicMock()
    form_func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_sql", func)
    monkeypatch.setattr(export_mod, "export_to_sql_by_form", form_func)
    monkeypatch.setattr(cli, "export_to_sql", export_mod.export_to_sql)
    monkeypatch.setattr(cli, "export_to_sql_by_form", export_mod.export_to_sql_by_form)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    engine = MagicMock()
    engine.dialect.name = "postgres"
    sa_module = ModuleType("sqlalchemy")
    sa_module.create_engine = MagicMock(return_value=engine)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa_module)
    result = runner.invoke(
        cli.app,
        [
            "export",
            "sql",
            "STUDY",
            "table",
            "postgresql://",
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
        "table",
        "postgresql://",
        variable_whitelist=["A", "B"],
        form_whitelist=[1, 2],
    )


def test_export_sql_sqlite_uses_by_form(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export sql sqlite uses by form."""
    form_func = MagicMock()
    sql_func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_sql_by_form", form_func)
    monkeypatch.setattr(export_mod, "export_to_sql", sql_func)
    monkeypatch.setattr(cli, "export_to_sql_by_form", export_mod.export_to_sql_by_form)
    monkeypatch.setattr(cli, "export_to_sql", export_mod.export_to_sql)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    engine = MagicMock()
    engine.dialect.name = "sqlite"
    sa_module = ModuleType("sqlalchemy")
    sa_module.create_engine = MagicMock(return_value=engine)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa_module)
    result = runner.invoke(
        cli.app,
        [
            "export",
            "sql",
            "STUDY",
            "table",
            "sqlite://",
            "--vars",
            "A",
            "--forms",
            "10",
        ],
    )
    assert result.exit_code == 0
    form_func.assert_called_once_with(
        sdk,
        "STUDY",
        "sqlite://",
        variable_whitelist=["A"],
        form_whitelist=[10],
    )


def test_export_sql_sqlite_single_table(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export sql sqlite single table."""
    form_func = MagicMock()
    single = ["--single-table"]
    sql_func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_sql_by_form", form_func)
    monkeypatch.setattr(export_mod, "export_to_sql", sql_func)
    monkeypatch.setattr(cli, "export_to_sql_by_form", export_mod.export_to_sql_by_form)
    monkeypatch.setattr(cli, "export_to_sql", export_mod.export_to_sql)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    engine = MagicMock()
    engine.dialect.name = "sqlite"
    sa_module = ModuleType("sqlalchemy")
    sa_module.create_engine = MagicMock(return_value=engine)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa_module)
    result = runner.invoke(
        cli.app,
        ["export", "sql", "STUDY", "table", "sqlite://", "--vars", "V1", "--forms", "5", *single],
    )
    assert result.exit_code == 0
    sql_func.assert_called_once_with(
        sdk,
        "STUDY",
        "table",
        "sqlite://",
        variable_whitelist=["V1"],
        form_whitelist=[5],
    )
    form_func.assert_not_called()


def test_export_sql_long_format(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export sql long format."""
    long_func = MagicMock()
    form_func = MagicMock()
    sql_func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_long_sql", long_func)
    monkeypatch.setattr(export_mod, "export_to_sql_by_form", form_func)
    monkeypatch.setattr(export_mod, "export_to_sql", sql_func)
    monkeypatch.setattr(cli, "export_to_long_sql", export_mod.export_to_long_sql)
    monkeypatch.setattr(cli, "export_to_sql_by_form", export_mod.export_to_sql_by_form)
    monkeypatch.setattr(cli, "export_to_sql", export_mod.export_to_sql)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    engine = MagicMock()
    engine.dialect.name = "sqlite"
    sa_module = ModuleType("sqlalchemy")
    sa_module.create_engine = MagicMock(return_value=engine)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa_module)
    result = runner.invoke(
        cli.app,
        ["export", "sql", "STUDY", "table", "sqlite://", "--long-format"],
    )
    assert result.exit_code == 0
    long_func.assert_called_once_with(sdk, "STUDY", "table", "sqlite://")
    sql_func.assert_not_called()
    form_func.assert_not_called()


def test_export_sql_long_format_overrides_single(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export sql long format overrides single."""
    long_func = MagicMock()
    form_func = MagicMock()
    sql_func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_long_sql", long_func)
    monkeypatch.setattr(export_mod, "export_to_sql_by_form", form_func)
    monkeypatch.setattr(export_mod, "export_to_sql", sql_func)
    monkeypatch.setattr(cli, "export_to_long_sql", export_mod.export_to_long_sql)
    monkeypatch.setattr(cli, "export_to_sql_by_form", export_mod.export_to_sql_by_form)
    monkeypatch.setattr(cli, "export_to_sql", export_mod.export_to_sql)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    engine = MagicMock()
    engine.dialect.name = "postgres"
    sa_module = ModuleType("sqlalchemy")
    sa_module.create_engine = MagicMock(return_value=engine)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa_module)
    result = runner.invoke(
        cli.app,
        [
            "export",
            "sql",
            "STUDY",
            "tbl",
            "postgresql://",
            "--single-table",
            "--long-format",
        ],
    )
    assert result.exit_code == 0
    long_func.assert_called_once_with(sdk, "STUDY", "tbl", "postgresql://")
    sql_func.assert_not_called()
    form_func.assert_not_called()


def test_export_parquet_missing_pyarrow(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that export parquet missing pyarrow."""
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str) -> object | None:
        """Helper function to fake find spec."""
        if name == "pyarrow":
            return None
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    result = runner.invoke(cli.app, ["export", "parquet", "STUDY", "out.parquet"])
    assert result.exit_code == 1
    assert "pyarrow is required" in result.stdout


def test_export_sql_missing_sqlalchemy(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that export sql missing sqlalchemy."""
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str) -> object | None:
        """Helper function to fake find spec."""
        if name == "sqlalchemy":
            return None
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    result = runner.invoke(
        cli.app,
        ["export", "sql", "STUDY", "table", "sqlite://"],
    )
    assert result.exit_code == 1
    assert "SQLAlchemy is required" in result.stdout


def test_export_duckdb_missing_dependency(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export duckdb missing dependency."""
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
    assert "imednet[duckdb]" in result.stdout


def test_subject_data_calls_workflow(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that subject data calls workflow."""
    workflow = MagicMock()
    monkeypatch.setattr(
        "imednet_workflows.cli.SubjectDataWorkflow", MagicMock(return_value=workflow)
    )
    workflow.get_all_subject_data.return_value = MagicMock()
    result = runner.invoke(cli.app, ["subject-data", "STUDY", "SUBJ"])
    assert result.exit_code == 0
    workflow.get_all_subject_data.assert_called_once_with("STUDY", "SUBJ")


def test_sync_worker_once_command(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that sync worker once command."""
    worker = MagicMock()
    worker.run_once.return_value = 42
    loader_cls = MagicMock()
    monkeypatch.setattr("imednet_workflows.cached_loader.CachedRecordsLoader", loader_cls)
    monkeypatch.setattr("imednet_workflows.cli.SyncWorker", MagicMock(return_value=worker))

    result = runner.invoke(
        cli.app, ["workflows", "sync-worker", "STUDY", "--interval", "5", "--once"]
    )

    assert result.exit_code == 0
    loader_cls.assert_called_once_with(sdk)
    worker.run_once.assert_called_once_with()
    assert "Synced 42 cached records" in result.stdout


def test_sync_worker_command_handles_keyboard_interrupt(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that sync worker command handles keyboard interrupt."""
    worker = MagicMock()
    worker.run_forever.side_effect = KeyboardInterrupt()
    loader_cls = MagicMock()
    monkeypatch.setattr("imednet_workflows.cached_loader.CachedRecordsLoader", loader_cls)
    monkeypatch.setattr("imednet_workflows.cli.SyncWorker", MagicMock(return_value=worker))

    result = runner.invoke(cli.app, ["workflows", "sync-worker", "STUDY", "--interval", "1"])

    assert result.exit_code == 0
    loader_cls.assert_called_once_with(sdk)
    worker.stop.assert_called_once_with()
    assert "Sync worker termination requested. Exiting cleanly." in result.stdout


def test_queries_list_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that queries list success."""
    obj = MagicMock()
    obj.description = "Q1"
    obj.annotation_type = "Query"
    obj.subject_key = "S1"
    obj.variable = "V1"
    obj.date_created = "2023"
    sdk.queries.list.return_value = [obj]
    result = runner.invoke(cli.app, ["queries", "list", "STUDY"])
    assert result.exit_code == 0
    sdk.queries.list.assert_called_once_with("STUDY")
    assert "Found 1 queries:" in result.stdout
    assert "Q1" in result.stdout


def test_queries_list_empty(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that queries list empty."""
    sdk.queries.list.return_value = []
    result = runner.invoke(cli.app, ["queries", "list", "STUDY"])
    assert result.exit_code == 0
    assert "No queries found." in result.stdout


def test_variables_list_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that variables list success."""
    obj = MagicMock()
    obj.variable_name = "V1"
    obj.label = "Var 1"
    obj.variable_type = "Text"
    obj.form_name = "Form"
    obj.disabled = False
    sdk.variables.list.return_value = [obj]
    result = runner.invoke(cli.app, ["variables", "list", "STUDY"])
    assert result.exit_code == 0
    sdk.variables.list.assert_called_once_with("STUDY")
    assert "Found 1 variables:" in result.stdout
    assert "V1" in result.stdout


def test_variables_list_empty(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that variables list empty."""
    sdk.variables.list.return_value = []
    result = runner.invoke(cli.app, ["variables", "list", "STUDY"])
    assert result.exit_code == 0
    assert "No variables found." in result.stdout


def test_record_revisions_list_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that record revisions list success."""
    sdk.record_revisions.list.return_value = ["R1"]
    result = runner.invoke(cli.app, ["record-revisions", "list", "STUDY"])
    assert result.exit_code == 0
    sdk.record_revisions.list.assert_called_once_with("STUDY")
    assert "Found 1 record revisions:" in result.stdout
    assert "R1" in result.stdout


def test_record_revisions_list_empty(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that record revisions list empty."""
    sdk.record_revisions.list.return_value = []
    result = runner.invoke(cli.app, ["record-revisions", "list", "STUDY"])
    assert result.exit_code == 0
    assert "No record revisions found." in result.stdout


def test_jobs_status_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that jobs status success."""
    result = runner.invoke(cli.app, ["jobs", "status", "STUDY", "BATCH"])
    assert result.exit_code == 0
    sdk.get_job.assert_called_once_with("STUDY", "BATCH")


def test_jobs_wait_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that jobs wait success."""
    result = runner.invoke(cli.app, ["jobs", "wait", "STUDY", "BATCH"])
    assert result.exit_code == 0
    sdk.poll_job.assert_called_once_with("STUDY", "BATCH", interval=5, timeout=300)


def test_intervals_list_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that intervals list success."""
    obj = MagicMock()
    obj.interval_id = 1
    obj.interval_name = "Baseline"
    obj.interval_sequence = 10
    sdk.intervals.list.return_value = [obj]
    result = runner.invoke(cli.app, ["intervals", "list", "STUDY"])
    assert result.exit_code == 0
    sdk.intervals.list.assert_called_once_with("STUDY")
    assert "Found 1 intervals:" in result.stdout
    assert "Baseline" in result.stdout


def test_intervals_list_empty(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that intervals list empty."""
    sdk.intervals.list.return_value = []
    result = runner.invoke(cli.app, ["intervals", "list", "STUDY"])
    assert result.exit_code == 0
    assert "No intervals found." in result.stdout


def test_workflows_stub_uninstalled_mocked(monkeypatch, capsys):
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [])
    from imednet.cli import get_parser

    parser = get_parser()

    # We can parse args
    args = parser.parse_args(["workflows"])

    import pytest

    with pytest.raises(SystemExit) as e:
        args.func(args)
    assert e.value.code == 1
    _out, err = capsys.readouterr()
    assert "The workflows plugin is not installed" in err


def test_plugin_load_exception(monkeypatch, capsys):
    class BrokenEntryPoint:
        name = "broken"

        def load(self):
            raise ValueError("Test error loading plugin")

    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [BrokenEntryPoint()])
    from imednet.cli import get_parser

    get_parser()

    _out, err = capsys.readouterr()
    assert "Warning: Failed to load CLI plugin 'broken': Test error loading plugin" in err
