"""Unit tests for cli."""

import importlib
import importlib.util
import io
import json
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


class _TTYStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class CliRunner:
    def invoke(self, app, args, tty: bool = False):
        from contextlib import redirect_stderr, redirect_stdout

        out = _TTYStringIO() if tty else io.StringIO()
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
from imednet.models.jobs import JobStatus


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
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)


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
    assert "IMEDNET_API_KEY" in result.stderr
    assert result.stdout == ""


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
    assert "API Error" in result.stderr
    assert "API Error" not in result.stdout


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
    assert "API Error" in result.stderr
    assert "API Error" not in result.stdout


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
    assert "Invalid filter format" in result.stderr
    assert result.stdout == ""
    sdk.subjects.list.assert_not_called()


def test_subjects_list_api_error(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that subjects list api error."""
    sdk.subjects.list.side_effect = ApiError("boom")
    result = runner.invoke(cli.app, ["subjects", "list", "STUDY"])
    assert result.exit_code == 1
    assert "API Error" in result.stderr
    assert "API Error" not in result.stdout


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
    assert "API Error" in result.stderr
    assert "API Error" not in result.stdout


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
    assert "Invalid output format" in result.stderr
    assert result.stdout == ""
    sdk.records.list.assert_not_called()


def test_records_list_api_error(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that records list api error."""
    sdk.records.list.side_effect = ApiError("oops")
    result = runner.invoke(cli.app, ["records", "list", "STUDY"])
    assert result.exit_code == 1
    assert "API Error" in result.stderr
    assert "API Error" not in result.stdout


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
    func.assert_called_once_with(sdk, "STUDY", "out.parquet", use_labels_as_columns=False)


def test_export_csv_calls_helper(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export csv calls helper."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_csv", func)
    monkeypatch.setattr(cli, "export_to_csv", export_mod.export_to_csv)
    result = runner.invoke(cli.app, ["export", "csv", "STUDY", "out.csv"])
    assert result.exit_code == 0
    func.assert_called_once_with(sdk, "STUDY", "out.csv", use_labels_as_columns=False)


def test_export_excel_calls_helper(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export excel calls helper."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_excel", func)
    monkeypatch.setattr(cli, "export_to_excel", export_mod.export_to_excel)
    result = runner.invoke(cli.app, ["export", "excel", "STUDY", "out.xlsx"])
    assert result.exit_code == 0
    func.assert_called_once_with(sdk, "STUDY", "out.xlsx", use_labels_as_columns=False)


def test_export_json_calls_helper(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export json calls helper."""
    func = MagicMock()
    monkeypatch.setattr(export_mod, "export_to_json", func)
    monkeypatch.setattr(cli, "export_to_json", export_mod.export_to_json)
    result = runner.invoke(cli.app, ["export", "json", "STUDY", "out.json"])
    assert result.exit_code == 0
    func.assert_called_once_with(
        sdk, "STUDY", "out.json", use_labels_as_columns=False, hierarchical=False
    )


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
    assert "pyarrow is required" in result.stderr
    assert result.stdout == ""


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
    assert "SQLAlchemy is required" in result.stderr
    assert result.stdout == ""


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
    assert "imednet[duckdb]" in result.stderr
    assert result.stdout == ""


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
    """Test that jobs status outputs formatted table headers and fields."""
    job = JobStatus(
        batchId="BATCH123",
        jobId="JOB456",
        state="RUNNING",
        progress=45,
        dateCreated="2026-11-17 10:00",
        dateFinished=None,
    )
    sdk.get_job.return_value = job
    result = runner.invoke(cli.app, ["jobs", "status", "STUDY", "BATCH123"])
    assert result.exit_code == 0
    sdk.get_job.assert_called_once_with("STUDY", "BATCH123")
    assert "Found 1 jobs:" in result.stdout
    assert "Batch Id" in result.stdout
    assert "Job Id" in result.stdout
    assert "State" in result.stdout
    assert "Progress" in result.stdout
    assert "Date Created" in result.stdout
    assert "Date Finished" in result.stdout
    assert "BATCH123" in result.stdout
    assert "JOB456" in result.stdout
    assert "RUNNING" in result.stdout
    assert "45" in result.stdout
    assert "2026-11-17 10:00" in result.stdout
    assert "-" in result.stdout
    # ANSI escape sequences stripped on non-tty
    assert "\x1b[" not in result.stdout


def test_jobs_wait_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that jobs wait outputs formatted table headers and fields."""
    job = JobStatus(
        batchId="BATCH123",
        jobId="JOB456",
        state="COMPLETE",
        progress=100,
        dateCreated="2026-11-17 10:00",
        dateFinished="2026-11-17 10:05",
    )
    sdk.poll_job.return_value = job
    result = runner.invoke(cli.app, ["jobs", "wait", "STUDY", "BATCH123"])
    assert result.exit_code == 0
    sdk.poll_job.assert_called_once_with("STUDY", "BATCH123", interval=5, timeout=300)
    assert "Found 1 jobs:" in result.stdout
    assert "Batch Id" in result.stdout
    assert "Job Id" in result.stdout
    assert "State" in result.stdout
    assert "Progress" in result.stdout
    assert "Date Created" in result.stdout
    assert "Date Finished" in result.stdout
    assert "BATCH123" in result.stdout
    assert "COMPLETE" in result.stdout
    assert "100" in result.stdout
    assert "\x1b[" not in result.stdout


def test_jobs_missing_timestamps_hyphens(runner: CliRunner, sdk: MagicMock) -> None:
    """Handle terminal jobs with missing timestamp fields by printing hyphens in table cells."""
    job = JobStatus(
        batchId="BATCH-TERM",
        jobId="JOB-TERM",
        state="CANCELLED",
        progress=10,
        dateCreated="",
        dateFinished=None,
    )
    sdk.get_job.return_value = job
    result = runner.invoke(cli.app, ["jobs", "status", "STUDY", "BATCH-TERM"])
    assert result.exit_code == 0
    # Both date_created ("") and date_finished (None) should render as "-"
    assert "BATCH-TERM" in result.stdout
    assert "CANCELLED" in result.stdout
    row_line = [line for line in result.stdout.splitlines() if "BATCH-TERM" in line][0]
    cells = [c.strip() for c in row_line.split("|")]
    # Cells: Batch Id, Job Id, State, Progress, Date Created, Date Finished
    assert cells[4] == "-"
    assert cells[5] == "-"


def test_jobs_status_json_format(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that jobs status supports --format json preserving envelope structure."""
    job = JobStatus(batchId="BATCH_J", jobId="JOB_J", state="RUNNING", progress=50)
    sdk.get_job.return_value = job
    result = runner.invoke(cli.app, ["jobs", "status", "STUDY", "BATCH_J", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == "jobs"
    assert payload["status"] == "complete"
    assert payload["count"] == 1
    assert payload["items"][0]["batchId"] == "BATCH_J"
    assert payload["items"][0]["state"] == "RUNNING"


def test_jobs_wait_json_format(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that jobs wait supports --format json preserving envelope structure."""
    job = JobStatus(batchId="BATCH_W", jobId="JOB_W", state="COMPLETE", progress=100)
    sdk.poll_job.return_value = job
    result = runner.invoke(cli.app, ["jobs", "wait", "STUDY", "BATCH_W", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == "jobs"
    assert payload["status"] == "complete"
    assert payload["count"] == 1
    assert payload["items"][0]["batchId"] == "BATCH_W"
    assert payload["items"][0]["state"] == "COMPLETE"


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        ("RUNNING", "\x1b[33mRUNNING\x1b[0m"),
        ("QUEUED", "\x1b[33mQUEUED\x1b[0m"),
        ("COMPLETE", "\x1b[32mCOMPLETE\x1b[0m"),
        ("COMPLETED", "\x1b[32mCOMPLETED\x1b[0m"),
        ("FAILED", "\x1b[31mFAILED\x1b[0m"),
        ("CANCELLED", "\x1b[31mCANCELLED\x1b[0m"),
        ("TIMEOUT", "\x1b[31mTIMEOUT\x1b[0m"),
    ],
)
def test_jobs_status_colors_on_tty(
    runner: CliRunner,
    sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    expected_code: str,
) -> None:
    """Test that job states display correct ANSI colors when stdout is interactive (TTY)."""
    job = JobStatus(batchId="BATCH_C", jobId="JOB_C", state=state, progress=50)
    sdk.get_job.return_value = job
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    result = runner.invoke(cli.app, ["jobs", "status", "STUDY", "BATCH_C"], tty=True)
    assert result.exit_code == 0
    assert expected_code in result.stdout


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        ("COMPLETE", "\x1b[34mCOMPLETE\x1b[0m"),
        ("CANCELLED", "\x1b[35mCANCELLED\x1b[0m"),
        ("TIMEOUT", "\x1b[35mTIMEOUT\x1b[0m"),
    ],
)
def test_jobs_status_colors_high_contrast(
    runner: CliRunner,
    sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    expected_code: str,
) -> None:
    """Test that job states honor high contrast color mapping on TTY."""
    job = JobStatus(batchId="BATCH_HC", jobId="JOB_HC", state=state, progress=50)
    sdk.get_job.return_value = job
    monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", "1")
    result = runner.invoke(cli.app, ["jobs", "status", "STUDY", "BATCH_HC"], tty=True)
    assert result.exit_code == 0
    assert expected_code in result.stdout


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


def test_visits_list_success(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that visits list success."""
    obj = MagicMock()
    obj.visit_id = 100
    obj.interval_name = "Baseline"
    obj.subject_key = "SUB-001"
    obj.visit_date = "2023-01-15"
    sdk.visits.list.return_value = [obj]
    result = runner.invoke(cli.app, ["visits", "list", "STUDY"])
    assert result.exit_code == 0
    sdk.visits.list.assert_called_once_with("STUDY")
    assert "Found 1 visits:" in result.stdout
    assert "Baseline" in result.stdout
    assert "SUB-001" in result.stdout


def test_visits_list_empty(runner: CliRunner, sdk: MagicMock) -> None:
    """Test that visits list empty."""
    sdk.visits.list.return_value = []
    result = runner.invoke(cli.app, ["visits", "list", "STUDY"])
    assert result.exit_code == 0
    assert "No visits found." in result.stdout


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


def test_cli_subcommand_isolation(runner: CliRunner) -> None:
    """Verify that internal development scripts are not registered as CLI subcommands."""
    import argparse

    from imednet.cli import get_parser

    parser = get_parser()
    subparser_actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(subparser_actions) == 1
    registered_subcommands = set(subparser_actions[0].choices.keys())

    # Core clinical commands should be registered
    expected_core_commands = {
        "studies",
        "records",
        "export",
        "datadict",
        "sites",
        "subjects",
        "queries",
        "variables",
        "record-revisions",
        "intervals",
        "jobs",
        "visits",
    }
    assert expected_core_commands.issubset(registered_subcommands)

    # Repository maintenance script names should not be registered as subcommands
    internal_script_commands = [
        "aggregate-verification",
        "build-docs",
        "check-root-files",
        "ci-plan",
        "post-smoke-record",
        "tighten-dependencies",
        "typecheck-docs",
        "validate-diagrams",
        "validate-docs",
        "validate-package-consumption",
    ]
    for script_cmd in internal_script_commands:
        assert script_cmd not in registered_subcommands

    # Dynamic check against all python scripts in root scripts/ directory if present
    repo_root = Path(__file__).resolve().parents[3]
    scripts_dir = repo_root / "scripts"
    if scripts_dir.is_dir():
        for script_file in scripts_dir.glob("*.py"):
            if script_file.name == "__init__.py":
                continue
            script_cmd = script_file.stem.replace("_", "-")
            assert script_cmd not in registered_subcommands

    # Invoking an obsolete script command should fail with an invalid choice error
    result = runner.invoke(cli.app, ["ci-plan"])
    assert result.exit_code != 0
    assert "invalid choice" in result.stderr or "unrecognized" in result.stderr


def test_root_help_without_arguments(runner: CliRunner) -> None:
    """Invoking imednet without arguments displays the main root help banner."""
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0
    assert "iMednet SDK Command Line Interface" in result.stdout
    assert "options:" in result.stdout or "optional arguments:" in result.stdout


@pytest.mark.parametrize(
    "cmd",
    [
        "datadict",
        "studies",
        "queries",
        "variables",
        "record-revisions",
        "sites",
        "export",
        "subjects",
        "intervals",
        "jobs",
        "records",
        "visits",
    ],
)
def test_noun_commands_show_contextual_help(runner: CliRunner, cmd: str) -> None:
    """Invoking any of the core noun commands without sub-actions displays contextual help."""
    result = runner.invoke(cli.app, [cmd])
    assert result.exit_code == 0
    assert "usage:" in result.stdout
    assert f" {cmd} " in result.stdout
    if cmd != "datadict":
        assert "iMednet SDK Command Line Interface" not in result.stdout


def test_high_contrast_with_subcommand(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoking imednet --high-contrast records displays records help and sets the env var."""
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    result = runner.invoke(cli.app, ["--high-contrast", "records"])
    assert result.exit_code == 0
    assert "usage:" in result.stdout
    assert " records " in result.stdout
    assert "iMednet SDK Command Line Interface" not in result.stdout
    assert os.environ.get("IMEDNET_HIGH_CONTRAST") == "1"


def test_plugin_subparser_help(runner: CliRunner) -> None:
    """Invoking a plugin command such as workflows displays its contextual help."""
    result = runner.invoke(cli.app, ["workflows"])
    assert result.exit_code == 0
    assert "usage:" in result.stdout
    assert " workflows " in result.stdout
    assert "extract-records" in result.stdout
    assert "sync-worker" in result.stdout
    assert "state" in result.stdout
    assert "iMednet SDK Command Line Interface" not in result.stdout


def test_nested_plugin_subparser_help(runner: CliRunner) -> None:
    """Invoking nested subparser workflows state displays state subparser help."""
    result = runner.invoke(cli.app, ["workflows", "state"])
    assert result.exit_code == 0
    assert "usage:" in result.stdout
    assert " workflows state " in result.stdout
    assert "show" in result.stdout
    assert "set" in result.stdout
    assert "reset" in result.stdout
    assert "iMednet SDK Command Line Interface" not in result.stdout


def test_module_reload_without_dotenv_emits_no_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reloading imednet.cli when python-dotenv is not installed produces no stderr noise."""
    monkeypatch.setitem(sys.modules, "dotenv", None)
    capsys.readouterr()  # Flush any previous buffers
    importlib.reload(cli)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_cli_help_without_dotenv_emits_no_stderr(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Displaying root CLI help when python-dotenv is not installed produces no stderr noise."""
    monkeypatch.setitem(sys.modules, "dotenv", None)
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert result.stderr == ""
    assert "iMednet SDK Command Line Interface" in result.stdout


def test_cli_subparser_help_without_dotenv_emits_no_stderr(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Displaying subcommand help when python-dotenv is not installed produces no stderr noise."""
    monkeypatch.setitem(sys.modules, "dotenv", None)
    result = runner.invoke(cli.app, ["studies", "--help"])
    assert result.exit_code == 0
    assert result.stderr == ""


def test_cli_command_with_env_credentials_without_dotenv_emits_no_stderr(
    runner: CliRunner, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running an SDK command when credentials exist in env and dotenv is absent produces no stderr noise."""
    monkeypatch.setitem(sys.modules, "dotenv", None)
    sdk.studies.list.return_value = []
    result = runner.invoke(cli.app, ["studies", "list"])
    assert result.exit_code == 0
    assert result.stderr == ""


def test_get_sdk_without_dotenv_reports_missing_credentials(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing credentials when dotenv is not installed prints standard error without dotenv warning."""
    monkeypatch.setitem(sys.modules, "dotenv", None)
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)
    result = runner.invoke(cli.app, ["studies", "list"])
    assert result.exit_code == 1
    assert "Warning: python-dotenv not installed" not in result.stderr
    assert (
        "Error: IMEDNET_API_KEY and IMEDNET_SECURITY_KEY environment variables must be set."
        in result.stderr
    )
    assert result.stdout == ""


def test_get_sdk_with_env_file_but_no_python_dotenv(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When a user provides a .env file without python-dotenv, the CLI displays missing credentials error."""
    monkeypatch.setitem(sys.modules, "dotenv", None)
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text("IMEDNET_API_KEY=file_key\nIMEDNET_SECURITY_KEY=file_secret\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["studies", "list"])
    assert result.exit_code == 1
    assert "Warning: python-dotenv not installed" not in result.stderr
    assert (
        "Error: IMEDNET_API_KEY and IMEDNET_SECURITY_KEY environment variables must be set."
        in result.stderr
    )
    assert result.stdout == ""


def test_get_sdk_lazily_loads_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_sdk lazily loads credentials via dotenv when absent from the environment."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)

    fake_dotenv = ModuleType("dotenv")
    called = False

    def fake_load_dotenv() -> None:
        nonlocal called
        called = True
        os.environ["IMEDNET_API_KEY"] = "lazy_key_from_dotenv"
        os.environ["IMEDNET_SECURITY_KEY"] = "lazy_sec_from_dotenv"

    fake_dotenv.load_dotenv = fake_load_dotenv  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    from imednet.cli.utils.context import get_sdk

    sdk_instance = get_sdk()
    try:
        assert called is True
        assert sdk_instance.config.api_key == "lazy_key_from_dotenv"
        assert sdk_instance.config.security_key == "lazy_sec_from_dotenv"
    finally:
        sdk_instance.close()


def test_get_sdk_skips_dotenv_when_credentials_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_sdk does not attempt to load dotenv when credentials already exist in environment."""
    monkeypatch.setenv("IMEDNET_API_KEY", "env_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "env_secret")

    fake_dotenv = ModuleType("dotenv")
    called = False

    def fake_load_dotenv() -> None:
        nonlocal called
        called = True

    fake_dotenv.load_dotenv = fake_load_dotenv  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    from imednet.cli.utils.context import get_sdk

    sdk_instance = get_sdk()
    try:
        assert called is False
        assert sdk_instance.config.api_key == "env_key"
        assert sdk_instance.config.security_key == "env_secret"
    finally:
        sdk_instance.close()


def test_get_sdk_handles_dotenv_parse_error_gracefully(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_sdk ignores dotenv parse or loading errors gracefully and reports missing credentials."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)

    fake_dotenv = ModuleType("dotenv")

    def broken_load() -> None:
        raise ValueError("Malformed .env file syntax error")

    fake_dotenv.load_dotenv = broken_load  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    result = runner.invoke(cli.app, ["studies", "list"])
    assert result.exit_code == 1
    assert "Malformed .env file syntax error" not in result.stderr
    assert (
        "Error: IMEDNET_API_KEY and IMEDNET_SECURITY_KEY environment variables must be set."
        in result.stderr
    )


def test_jobs_wait_failed_terminal_state_exit(runner: CliRunner, sdk: MagicMock) -> None:
    """When a job ends in FAILED state, jobs wait exits with code 1, clean stdout, and diagnostic stderr."""
    from imednet.utils.job_poller import JobFailedError

    job = JobStatus(
        batchId="BATCH-FAIL",
        jobId="JOB-FAIL",
        state="FAILED",
        progress=80,
        results={"error": "Database constraint violation", "failed": 1},
    )
    sdk.poll_job.side_effect = JobFailedError("Job BATCH-FAIL ended in state FAILED", job)

    result = runner.invoke(cli.app, ["jobs", "wait", "STUDY", "BATCH-FAIL"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Unexpected error: Job BATCH-FAIL ended in state FAILED" in result.stderr


def test_jobs_wait_cancelled_terminal_state_exit(runner: CliRunner, sdk: MagicMock) -> None:
    """When a job ends in CANCELLED state, jobs wait exits with code 1, clean stdout, and diagnostic stderr."""
    from imednet.utils.job_poller import JobFailedError

    job = JobStatus(
        batchId="BATCH-CANCEL",
        jobId="JOB-CANCEL",
        state="CANCELLED",
        progress=30,
    )
    sdk.poll_job.side_effect = JobFailedError("Job BATCH-CANCEL ended in state CANCELLED", job)

    result = runner.invoke(cli.app, ["jobs", "wait", "STUDY", "BATCH-CANCEL"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Unexpected error: Job BATCH-CANCEL ended in state CANCELLED" in result.stderr


def test_jobs_wait_timeout_error_exit(runner: CliRunner, sdk: MagicMock) -> None:
    """When job polling times out, jobs wait exits with code 1, clean stdout, and diagnostic stderr."""
    from imednet.utils.job_poller import JobTimeoutError

    sdk.poll_job.side_effect = JobTimeoutError("Job BATCH-TIMEOUT timed out after 300 seconds")
    result = runner.invoke(cli.app, ["jobs", "wait", "STUDY", "BATCH-TIMEOUT"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Unexpected error: Job BATCH-TIMEOUT timed out after 300 seconds" in result.stderr

    sdk.poll_job.side_effect = TimeoutError("Polling exceeded timeout of 300s")
    result2 = runner.invoke(cli.app, ["jobs", "wait", "STUDY", "BATCH-TIMEOUT2"])
    assert result2.exit_code == 1
    assert result2.stdout == ""
    assert "Unexpected error: Polling exceeded timeout of 300s" in result2.stderr


def test_jobs_wait_failure_json_mode_atomic_no_stdout(runner: CliRunner, sdk: MagicMock) -> None:
    """In JSON mode, poller failures exit 1 with clean stdout and do not emit partial JSON."""
    from imednet.utils.job_poller import JobFailedError

    job = JobStatus(batchId="BATCH-JFAIL", state="FAILED", progress=90)
    sdk.poll_job.side_effect = JobFailedError("Job BATCH-JFAIL ended in state FAILED", job)

    result = runner.invoke(cli.app, ["jobs", "wait", "STUDY", "BATCH-JFAIL", "--format", "json"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Job BATCH-JFAIL ended in state FAILED" in result.stderr


@pytest.mark.parametrize("command", ["status", "wait"])
@pytest.mark.parametrize("tty", [False, True])
def test_jobs_json_mode_interactive_and_redirected(
    runner: CliRunner,
    sdk: MagicMock,
    command: str,
    tty: bool,
) -> None:
    """JSON output on both interactive and redirected stdout remains a clean single envelope without ANSI or progress."""
    job = JobStatus(
        batchId="BATCH-STREAM",
        jobId="JOB-STREAM",
        state="COMPLETE",
        progress=100,
        dateCreated="2026-11-17T09:00:00Z",
        dateStarted="2026-11-17T09:01:00Z",
        dateFinished="2026-11-17T09:04:00Z",
        resultUrl="https://example.com/api/v1/jobs/BATCH-STREAM/result",
        results={
            "total": 2,
            "success": 2,
            "failed": 0,
            "details": [{"id": 1, "status": "PASS"}, {"id": 2, "status": "PASS"}],
        },
    )
    sdk.get_job.return_value = job
    sdk.poll_job.return_value = job

    result = runner.invoke(
        cli.app, ["jobs", command, "STUDY", "BATCH-STREAM", "--format", "json"], tty=tty
    )
    assert result.exit_code == 0
    # No ANSI escape sequences anywhere in stdout
    assert "\x1b[" not in result.stdout
    # Progress text must never appear on stdout
    assert "Fetching" not in result.stdout
    # Must parse cleanly as exactly one JSON object
    payload = json.loads(result.stdout)
    assert payload["schemaVersion"] == "1.0"
    assert payload["resource"] == "jobs"
    assert payload["status"] == "complete"
    assert payload["count"] == 1
    expected_item = job.model_dump(mode="json", by_alias=True)
    assert payload["items"] == [expected_item]
    assert payload["items"][0] == expected_item
    assert payload["items"][0]["results"] == job.results
    assert payload["items"][0]["resultUrl"] == job.result_url
    assert payload["items"][0]["dateStarted"] == job.date_started
