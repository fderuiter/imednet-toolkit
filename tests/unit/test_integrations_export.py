"""Unit tests for integrations export."""

import sys
from builtins import __import__ as builtin_import
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, call

import pandas as pd
import pytest

import imednet.integrations.export as export_mod
from imednet.models.records import Record
from imednet.models.variables import Variable


def _make_quality_sdk() -> MagicMock:
    """Create a mock SDK configured with schema variables for quality gate validation."""
    sdk = MagicMock()
    sdk.get_variables.return_value = [
        Variable(variable_name="A", variable_type="int", form_id=1, form_key="F1")
    ]
    return sdk


def _setup_mapper(monkeypatch):
    """Helper function to setup mapper without monkeypatching apply_quality_gate."""
    import pandas as pd

    df = pd.DataFrame([{"A": 1}])
    # We mock the class methods so that slices don't lose the mock
    monkeypatch.setattr(pd.DataFrame, "to_excel", MagicMock())
    monkeypatch.setattr(pd.DataFrame, "to_parquet", MagicMock())
    # For JSON we need to mock df.where().to_dict
    df.where = MagicMock(return_value=MagicMock(to_dict=MagicMock(return_value=[{"A": 1}])))
    mapper_inst = MagicMock()
    mapper_inst.dataframe.return_value = df

    mapper_inst._fetch_variable_metadata.return_value = (["A"], {"A": "A"})
    mapper_inst._build_record_model.return_value = MagicMock()
    mapper_inst._iter_records.return_value = [
        Record(record_id=1, form_id=1, form_key="F1", record_data={"A": 1})
    ]
    mapper_inst._parse_records.return_value = ([], 0)
    mapper_inst._build_dataframe.return_value = df

    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)
    return df, mapper_cls, mapper_inst


def _setup_real_mapper(monkeypatch):
    """Helper function to  setup real mapper."""
    df = pd.DataFrame({"A": [1]})
    mapper_inst = MagicMock()
    mapper_inst.dataframe.return_value = df
    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)
    return df, mapper_cls, mapper_inst


def test_export_to_csv(monkeypatch):
    """Test that export to csv."""
    _df, mapper_cls, mapper_inst = _setup_mapper(monkeypatch)
    sdk = _make_quality_sdk()

    m_open = MagicMock()
    monkeypatch.setattr("builtins.open", m_open)

    export_mod.export_to_csv(sdk, "STUDY", "out.csv", sep=";")

    mapper_cls.assert_called_once_with(sdk)
    mapper_inst._fetch_variable_metadata.assert_called_once_with(
        "STUDY",
        variable_whitelist=None,
        form_whitelist=None,
    )
    m_open.assert_any_call("out.csv", mode="w", encoding="utf-8")


def test_export_to_excel(monkeypatch):
    """Test that export to excel."""
    _df, mapper_cls, mapper_inst = _setup_mapper(monkeypatch)
    sdk = MagicMock()

    export_mod.export_to_excel(sdk, "STUDY", "out.xlsx")

    mapper_cls.assert_called_once_with(sdk)
    mapper_inst.dataframe.assert_called_once_with(
        "STUDY",
        use_labels_as_columns=False,
        variable_whitelist=None,
        form_whitelist=None,
    )
    pd.DataFrame.to_excel.assert_called_once_with("out.xlsx", index=False)


def test_export_to_json(monkeypatch, tmp_path):
    """Test that export to json."""
    df, mapper_cls, mapper_inst = _setup_mapper(monkeypatch)
    df.where.return_value.to_dict.return_value = [{"A": 1}]
    sdk = MagicMock()
    out_file = tmp_path / "out.json"

    export_mod.export_to_json(sdk, "STUDY", str(out_file))

    mapper_cls.assert_called_once_with(sdk)
    mapper_inst.dataframe.assert_called_once_with(
        "STUDY",
        use_labels_as_columns=False,
        variable_whitelist=None,
        form_whitelist=None,
    )


def test_export_to_parquet(monkeypatch):
    """Test that export to parquet."""
    _df, mapper_cls, mapper_inst = _setup_mapper(monkeypatch)
    sdk = MagicMock()

    export_mod.export_to_parquet(sdk, "STUDY", "out.parquet", compression="snappy")

    mapper_cls.assert_called_once_with(sdk)
    mapper_inst.dataframe.assert_called_once_with(
        "STUDY",
        use_labels_as_columns=False,
        variable_whitelist=None,
        form_whitelist=None,
    )
    pd.DataFrame.to_parquet.assert_called_once_with(
        "out.parquet", index=False, compression="snappy"
    )


def test_export_to_sql(monkeypatch):
    """Test that export to sql."""
    df, mapper_cls, mapper_inst = _setup_mapper(monkeypatch)
    sdk = _make_quality_sdk()

    sa_module = ModuleType("sqlalchemy")
    engine = MagicMock()
    engine.dialect.name = "sqlite"
    create_engine = MagicMock(return_value=engine)
    sa_module.create_engine = create_engine
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa_module)

    m_chunking = MagicMock()
    monkeypatch.setattr(export_mod, "_to_sql_with_chunking", m_chunking)

    export_mod.export_to_sql(sdk, "STUDY", "table", "sqlite://", if_exists="append")

    mapper_cls.assert_called_once_with(sdk)
    mapper_inst._fetch_variable_metadata.assert_called_once_with(
        "STUDY",
        variable_whitelist=None,
        form_whitelist=None,
    )
    create_engine.assert_called_once_with("sqlite://")
    assert m_chunking.call_count == 1
    args, kwargs = m_chunking.call_args
    assert args[0].equals(df)
    assert args[1] == "table"
    assert args[2] == engine
    assert kwargs["if_exists"] == "append"
    assert not Path("export_table_manifest.jsonl").exists()


def test_export_to_sql_manifest_custom_path(tmp_path, monkeypatch):
    """Test that export to sql writes to manifest_path when provided."""
    df, mapper_cls, mapper_inst = _setup_mapper(monkeypatch)
    sdk = _make_quality_sdk()

    sa_module = ModuleType("sqlalchemy")
    engine = MagicMock()
    engine.dialect.name = "sqlite"
    create_engine = MagicMock(return_value=engine)
    sa_module.create_engine = create_engine
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa_module)

    m_chunking = MagicMock()
    monkeypatch.setattr(export_mod, "_to_sql_with_chunking", m_chunking)

    manifest_file = tmp_path / "custom_manifest.jsonl"
    export_mod.export_to_sql(
        sdk,
        "STUDY",
        "table",
        "sqlite://",
        manifest_path=str(manifest_file),
    )

    assert manifest_file.exists()


def test_export_to_duckdb(monkeypatch):
    """Test that export to duckdb."""
    sdk = MagicMock()
    df = pd.DataFrame({"A": [1]})
    monkeypatch.setattr(export_mod, "_prepare_export_df", MagicMock(return_value=df))

    conn = MagicMock()
    duckdb_module = ModuleType("duckdb")
    duckdb_module.connect = MagicMock(return_value=conn)
    monkeypatch.setitem(sys.modules, "duckdb", duckdb_module)

    export_mod.export_to_duckdb(sdk, "STUDY", "out.duckdb", "my_table")

    duckdb_module.connect.assert_called_once_with("out.duckdb")
    conn.register.assert_called_once_with("df", df)
    conn.execute.assert_called_once_with('CREATE OR REPLACE TABLE "my_table" AS SELECT * FROM "df"')
    conn.unregister.assert_called_once_with("df")
    conn.close.assert_called_once_with()


def test_export_to_duckdb_handles_wide_dataframe(monkeypatch):
    """Test that export to duckdb handles wide dataframe."""
    sdk = MagicMock()
    wide_df = pd.DataFrame([range(export_mod.MAX_SQLITE_COLUMNS + 50)])
    monkeypatch.setattr(export_mod, "_prepare_export_df", MagicMock(return_value=wide_df))

    conn = MagicMock()
    duckdb_module = ModuleType("duckdb")
    duckdb_module.connect = MagicMock(return_value=conn)
    monkeypatch.setitem(sys.modules, "duckdb", duckdb_module)

    export_mod.export_to_duckdb(sdk, "STUDY", "wide.duckdb", "wide_table")

    conn.register.assert_called_once_with("df", wide_df)
    conn.execute.assert_called_once_with(
        'CREATE OR REPLACE TABLE "wide_table" AS SELECT * FROM "df"'
    )


def test_export_to_duckdb_import_error(monkeypatch):
    """Test that export to duckdb import error."""

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        """Helper function to fake import."""
        if name == "duckdb":
            raise ImportError("No module named duckdb")
        return builtin_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(
        ImportError,
        match=(
            r"DuckDB export requires the optional 'duckdb' dependency\. "
            r"Install with `pip install 'imednet\[duckdb\]'`\."
        ),
    ):
        export_mod.export_to_duckdb(MagicMock(), "STUDY", "out.duckdb", "records")


def test_export_functions_handle_duplicate_columns(tmp_path, monkeypatch):
    """Test that export functions handle duplicate columns."""
    df = pd.DataFrame([[1, 2]], columns=["A", "A"])
    monkeypatch.setattr(
        pd.DataFrame,
        "to_parquet",
        lambda self, path, index=False, **kwargs: open(path, "wb").close(),
    )
    mapper_inst = MagicMock(dataframe=MagicMock(return_value=df))
    mapper_inst._fetch_variable_metadata.return_value = (
        list(df.columns),
        {c: c for c in df.columns},
    )
    mapper_inst._build_record_model.return_value = MagicMock()
    mapper_inst._iter_records.return_value = [
        Record(record_id=1, form_id=1, form_key="F1", record_data={"A": 1})
    ]
    mapper_inst._parse_records.return_value = ([], 0)
    mapper_inst._build_dataframe.return_value = df
    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)
    sdk = _make_quality_sdk()

    out_json = tmp_path / "d.json"
    export_mod.export_to_json(sdk, "S", str(out_json))
    assert out_json.exists()

    out_db = tmp_path / "d.db"
    export_mod.export_to_sql(sdk, "S", "t", f"sqlite:///{out_db}")
    assert out_db.exists()

    out_parquet = tmp_path / "d.parquet"
    export_mod.export_to_parquet(sdk, "S", str(out_parquet))
    assert out_parquet.exists()


def test_export_functions_handle_case_insensitive_duplicates(tmp_path, monkeypatch):
    """Test that export functions handle case insensitive duplicates."""
    df = pd.DataFrame([[1, 2]], columns=["A", "a"])
    monkeypatch.setattr(
        pd.DataFrame,
        "to_parquet",
        lambda self, path, index=False, **kwargs: open(path, "wb").close(),
    )
    mapper_inst = MagicMock(dataframe=MagicMock(return_value=df))
    mapper_inst._fetch_variable_metadata.return_value = (
        list(df.columns),
        {c: c for c in df.columns},
    )
    mapper_inst._build_record_model.return_value = MagicMock()
    mapper_inst._iter_records.return_value = [
        Record(record_id=1, form_id=1, form_key="F1", record_data={"A": 1})
    ]
    mapper_inst._parse_records.return_value = ([], 0)
    mapper_inst._build_dataframe.return_value = df
    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)
    sdk = _make_quality_sdk()

    out_json = tmp_path / "case.json"
    export_mod.export_to_json(sdk, "S", str(out_json))
    assert out_json.exists()

    out_db = tmp_path / "case.db"
    export_mod.export_to_sql(sdk, "S", "t", f"sqlite:///{out_db}")
    assert out_db.exists()

    out_parquet = tmp_path / "case.parquet"
    export_mod.export_to_parquet(sdk, "S", str(out_parquet))
    assert out_parquet.exists()


def test_export_sql_too_many_columns(monkeypatch):
    """Test that export sql too many columns."""
    columns = [f"c{i}" for i in range(export_mod.MAX_SQLITE_COLUMNS + 1)]
    df = pd.DataFrame(
        [range(export_mod.MAX_SQLITE_COLUMNS + 1)],
        columns=columns,
    )
    mock_to_sql = MagicMock()
    monkeypatch.setattr(pd.DataFrame, "to_sql", mock_to_sql)
    mapper_inst = MagicMock(dataframe=MagicMock(return_value=df))
    mapper_inst._fetch_variable_metadata.return_value = (
        list(df.columns),
        {c: c for c in df.columns},
    )
    mapper_inst._build_record_model.return_value = MagicMock()
    mapper_inst._iter_records.return_value = [
        Record(record_id=1, form_id=1, form_key="F1", record_data={"A": 1})
    ]
    mapper_inst._parse_records.return_value = ([], 0)
    mapper_inst._build_dataframe.return_value = df
    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)

    engine = MagicMock()
    engine.dialect.name = "sqlite"
    sa_module = ModuleType("sqlalchemy")
    sa_module.create_engine = MagicMock(return_value=engine)
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa_module)

    export_mod.export_to_sql(_make_quality_sdk(), "STUDY", "table", "sqlite://")
    assert mock_to_sql.call_count == 2
    calls = mock_to_sql.call_args_list
    assert calls[0].args[0] == "table_part1"
    assert calls[1].args[0] == "table_part2"


def test_export_to_sql_by_form(monkeypatch):
    """Test that export to sql by form."""
    sdk = MagicMock()
    form1 = MagicMock(form_id=1, form_key="F1")
    form2 = MagicMock(form_id=2, form_key="F2")
    sdk.forms.list.return_value = [form1, form2]
    sdk.variables.list.return_value = [
        MagicMock(variable_name="A", label="A", form_id=1),
        MagicMock(variable_name="B", label="B", form_id=2),
    ]

    mapper_inst = MagicMock()
    mapper_inst._build_record_model.return_value = object()
    mapper_inst._fetch_records.side_effect = [MagicMock(), MagicMock()]
    mapper_inst._parse_records.side_effect = [([], 0), ([], 0)]
    df1 = MagicMock()
    df2 = MagicMock()
    mapper_inst._build_dataframe.side_effect = [df1, df2]
    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)

    sa_module = ModuleType("sqlalchemy")
    engine = MagicMock()
    engine.dialect.name = "sqlite"
    create_engine = MagicMock(return_value=engine)
    sa_module.create_engine = create_engine
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa_module)

    export_mod.export_to_sql_by_form(sdk, "STUDY", "sqlite://")

    mapper_cls.assert_called_once_with(sdk)
    assert sdk.variables.list.call_count == 1
    df1.to_sql.assert_called_once_with("F1", engine, if_exists="replace", index=False)
    df2.to_sql.assert_called_once_with("F2", engine, if_exists="replace", index=False)


def test_export_to_duckdb_by_form(monkeypatch):
    """Test that export to duckdb by form."""
    sdk = MagicMock()
    form1 = MagicMock(form_id=1, form_key="F1")
    form2 = MagicMock(form_id=2, form_key="F2")
    sdk.forms.list.return_value = [form1, form2]
    sdk.variables.list.return_value = [
        MagicMock(variable_name="A", label="A", form_id=1),
        MagicMock(variable_name="B", label="B", form_id=2),
    ]

    mapper_inst = MagicMock()
    mapper_inst._build_record_model.return_value = object()
    mapper_inst._fetch_records.side_effect = [MagicMock(), MagicMock()]
    mapper_inst._parse_records.side_effect = [([], 0), ([], 0)]
    df1 = pd.DataFrame({"A": [1]})
    df2 = pd.DataFrame({"B": [2]})
    mapper_inst._build_dataframe.side_effect = [df1, df2]
    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)

    conn = MagicMock()
    duckdb_module = ModuleType("duckdb")
    duckdb_module.connect = MagicMock(return_value=conn)
    monkeypatch.setitem(sys.modules, "duckdb", duckdb_module)

    export_mod.export_to_duckdb_by_form(
        sdk,
        "STUDY",
        "forms.duckdb",
        use_labels_as_columns=True,
        variable_whitelist=["A", "B"],
        form_whitelist=[1, 2],
    )

    mapper_cls.assert_called_once_with(sdk)
    assert sdk.variables.list.call_count == 1
    sdk.variables.list.assert_called_once_with(study_key="STUDY", formId=[1, 2])
    assert conn.register.call_count == 2
    assert conn.register.call_args_list[0][0][0] == "df"
    pd.testing.assert_frame_equal(conn.register.call_args_list[0][0][1], df1)
    assert conn.register.call_args_list[1][0][0] == "df"
    pd.testing.assert_frame_equal(conn.register.call_args_list[1][0][1], df2)
    assert conn.execute.call_args_list == [
        call('CREATE OR REPLACE TABLE "F1" AS SELECT * FROM "df"'),
        call('CREATE OR REPLACE TABLE "F2" AS SELECT * FROM "df"'),
    ]
    assert conn.unregister.call_args_list == [call("df"), call("df")]
    conn.close.assert_called_once_with()


def test_export_to_duckdb_by_form_import_error(monkeypatch):
    """Test that export to duckdb by form import error."""

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        """Helper function to fake import."""
        if name == "duckdb":
            raise ImportError("No module named duckdb")
        return builtin_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(
        ImportError,
        match=(
            r"DuckDB export requires the optional 'duckdb' dependency\. "
            r"Install with `pip install 'imednet\[duckdb\]'`\."
        ),
    ):
        export_mod.export_to_duckdb_by_form(MagicMock(), "STUDY", "out.duckdb")


def test_export_to_long_sql(monkeypatch):
    """Test that export to long sql."""
    sdk = MagicMock()
    dt1 = datetime(2023, 1, 1)
    dt2 = datetime(2023, 1, 2)
    dt3 = datetime(2023, 1, 3)
    records = [
        MagicMock(record_id=1, form_id=10, record_data={"A": 1, "B": 2}, date_modified=dt1),
        MagicMock(record_id=2, form_id=11, record_data={"C": 3}, date_modified=dt2),
        MagicMock(record_id=3, form_id=12, record_data={"D": 4}, date_modified=dt3),
    ]
    mapper_inst = MagicMock(_fetch_records=MagicMock(return_value=records))
    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)

    sa_module = ModuleType("sqlalchemy")
    engine = MagicMock()
    sa_module.create_engine = MagicMock(return_value=engine)
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa_module)

    captured = []

    def fake_to_sql(self, table, engine_arg, if_exists, index=False):
        """Helper function to fake to sql."""
        captured.append((self.copy(), if_exists))

    monkeypatch.setattr(pd.DataFrame, "to_sql", fake_to_sql)

    export_mod.export_to_long_sql(sdk, "STUDY", "tbl", "sqlite://", chunk_size=2)

    assert len(captured) == 2
    assert list(captured[0][0].to_dict("records")) == [
        {"record_id": 1, "form_id": 10, "variable_name": "A", "value": 1, "timestamp": dt1},
        {"record_id": 1, "form_id": 10, "variable_name": "B", "value": 2, "timestamp": dt1},
    ]
    assert captured[0][1] == "replace"
    assert list(captured[1][0].to_dict("records")) == [
        {"record_id": 2, "form_id": 11, "variable_name": "C", "value": 3, "timestamp": dt2},
        {"record_id": 3, "form_id": 12, "variable_name": "D", "value": 4, "timestamp": dt3},
    ]
    assert captured[1][1] == "append"


def test_records_df_missing_pandas(monkeypatch):
    """Test that records df missing pandas."""
    monkeypatch.setattr(export_mod, "pd", None)
    with pytest.raises(
        ImportError,
        match=r"pandas is required for _records_df\. Install with \"pip install 'imednet\[export\]'\"\.",
    ):
        export_mod._records_df(MagicMock(), "STUDY")


def test_export_to_long_sql_missing_pandas(monkeypatch):
    """Test that export to long sql missing pandas."""
    monkeypatch.setattr(export_mod, "pd", None)
    with pytest.raises(
        ImportError,
        match=r"pandas is required for export_to_long_sql\. Install with \"pip install 'imednet\[export\]'\"\.",
    ):
        export_mod.export_to_long_sql(MagicMock(), "STUDY", "table", "sqlite://")


def test_exports_with_multi_item_whitelists_and_quality_gate_end_to_end(tmp_path: Path) -> None:
    """Verify CSV, SQL, DuckDB-by-form, and SQL-by-form retain Record models and emit singular filters."""
    import sqlite3

    import httpx
    import respx

    from imednet.sdk import ImednetSDK

    seen_filters: list[str] = []

    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.test/api/v1/edc/studies/STUDY/forms").respond(
            200,
            json={
                "data": [
                    {"formId": 10, "formKey": "DEMO"},
                    {"formId": 20, "formKey": "VITALS"},
                ]
            },
        )

        def var_handler(request: httpx.Request) -> httpx.Response:
            flt = request.url.params.get("filter", "")
            seen_filters.append(flt)
            assert "variableNames" not in flt
            assert "formIds" not in flt
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "variableName": "AGE",
                            "variableType": "int",
                            "label": "Age",
                            "formId": 10,
                            "formKey": "DEMO",
                        },
                        {
                            "variableName": "HR",
                            "variableType": "int",
                            "label": "Heart Rate",
                            "formId": 20,
                            "formKey": "VITALS",
                        },
                    ]
                },
            )

        def rec_handler(request: httpx.Request) -> httpx.Response:
            flt = request.url.params.get("filter", "")
            seen_filters.append(flt)
            assert "variableNames" not in flt
            assert "formIds" not in flt
            if "formId==10" in flt:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "recordId": 101,
                                "subjectKey": "SUBJ-1",
                                "visitId": 1,
                                "formId": 10,
                                "formKey": "DEMO",
                                "recordStatus": "Complete",
                                "recordData": {"AGE": 42},
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "recordId": 102,
                            "subjectKey": "SUBJ-1",
                            "visitId": 1,
                            "formId": 20,
                            "formKey": "VITALS",
                            "recordStatus": "Complete",
                            "recordData": {"HR": 70},
                        }
                    ]
                },
            )

        router.get("https://api.test/api/v1/edc/studies/STUDY/variables").mock(
            side_effect=var_handler
        )
        router.get("https://api.test/api/v1/edc/studies/STUDY/records").mock(
            side_effect=rec_handler
        )

        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")

        csv_path = tmp_path / "study.csv"
        export_mod.export_to_csv(
            sdk,
            "STUDY",
            str(csv_path),
            quality_gate_enabled=True,
            variable_whitelist=["AGE", "HR"],
            form_whitelist=[10, 20],
        )
        csv_df = pd.read_csv(csv_path)
        assert len(csv_df) == 2

        sql_path = tmp_path / "study.sqlite"
        export_mod.export_to_sql(
            sdk,
            "STUDY",
            "records_tbl",
            f"sqlite:///{sql_path}",
            quality_gate_enabled=True,
            variable_whitelist=["AGE", "HR"],
            form_whitelist=[10, 20],
        )
        with sqlite3.connect(sql_path) as conn:
            row_count = conn.execute("SELECT COUNT(*) FROM records_tbl").fetchone()[0]
        assert row_count == 2

        by_form_sql = tmp_path / "by_form.sqlite"
        export_mod.export_to_sql_by_form(
            sdk,
            "STUDY",
            f"sqlite:///{by_form_sql}",
            variable_whitelist=["AGE", "HR"],
            form_whitelist=[10, 20],
        )
        with sqlite3.connect(by_form_sql) as conn:
            assert conn.execute("SELECT COUNT(*) FROM DEMO").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM VITALS").fetchone()[0] == 1

        duckdb_mod = pytest.importorskip("duckdb")
        by_form_duck = tmp_path / "by_form.duckdb"
        export_mod.export_to_duckdb_by_form(
            sdk,
            "STUDY",
            str(by_form_duck),
            variable_whitelist=["AGE", "HR"],
            form_whitelist=[10, 20],
        )
        duck_conn = duckdb_mod.connect(str(by_form_duck))
        try:
            assert duck_conn.execute("SELECT COUNT(*) FROM DEMO").fetchone()[0] == 1
            assert duck_conn.execute("SELECT COUNT(*) FROM VITALS").fetchone()[0] == 1
        finally:
            duck_conn.close()


def test_tabular_sink_config_receives_operational_options_without_pandas_leak(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify batch_size, max_retries, idempotent, and quality gate options populate TabularSinkConfig without leaking into pandas/json kwargs."""
    df, _mapper_cls, _mapper_inst = _setup_mapper(monkeypatch)
    sdk = _make_quality_sdk()

    captured_configs: list[export_mod.TabularSinkConfig] = []
    orig_build = export_mod._build_tabular_sink_config

    def spy_build(*args: object, **kwargs: object) -> export_mod.TabularSinkConfig:
        cfg = orig_build(*args, **kwargs)  # type: ignore[arg-type]
        captured_configs.append(cfg)
        return cfg

    monkeypatch.setattr(export_mod, "_build_tabular_sink_config", spy_build)

    # 1. Parquet: compression reaches DataFrame.to_parquet, operational options do not
    parquet_file = tmp_path / "records.parquet"
    export_mod.export_to_parquet(
        sdk,
        "STUDY",
        str(parquet_file),
        batch_size=125,
        max_retries=4,
        idempotent=False,
        quality_gate_enabled=True,
        min_schema_readiness_score=91.5,
        compression="gzip",
    )
    pd.DataFrame.to_parquet.assert_called_once_with(
        str(parquet_file), index=False, compression="gzip"
    )
    assert captured_configs[-1].batch_size == 125
    assert captured_configs[-1].max_retries == 4
    assert captured_configs[-1].idempotent is False
    assert captured_configs[-1].quality_gate_enabled is True
    assert captured_configs[-1].min_schema_readiness_score == 91.5

    # 2. Excel: operational options do not leak into DataFrame.to_excel
    excel_file = tmp_path / "records.xlsx"
    export_mod.export_to_excel(
        sdk,
        "STUDY",
        str(excel_file),
        batch_size=75,
        max_retries=2,
        idempotent=True,
        quality_gate_enabled=True,
        min_schema_readiness_score=85.0,
    )
    pd.DataFrame.to_excel.assert_called_once_with(str(excel_file), index=False)
    assert captured_configs[-1].batch_size == 75
    assert captured_configs[-1].max_retries == 2
    assert captured_configs[-1].quality_gate_enabled is True
    assert captured_configs[-1].min_schema_readiness_score == 85.0

    # 3. JSON: operational options do not leak into json.dump
    json_file = tmp_path / "records.json"
    export_mod.export_to_json(
        sdk,
        "STUDY",
        str(json_file),
        batch_size=60,
        max_retries=5,
        idempotent=True,
        quality_gate_enabled=True,
        min_schema_readiness_score=90.0,
        indent=2,
    )
    assert json_file.exists()
    assert captured_configs[-1].batch_size == 60
    assert captured_configs[-1].max_retries == 5

    # 4. CSV: non-idempotent runs append without rewriting headers or leaking sink options
    csv_file = tmp_path / "append_records.csv"
    export_mod.export_to_csv(
        sdk,
        "STUDY",
        str(csv_file),
        batch_size=50,
        max_retries=2,
        idempotent=True,
        sep=",",
    )
    export_mod.export_to_csv(
        sdk,
        "STUDY",
        str(csv_file),
        batch_size=50,
        max_retries=2,
        idempotent=False,
        sep=",",
    )
    csv_result = pd.read_csv(csv_file)
    assert len(csv_result) == 2
    assert captured_configs[-1].idempotent is False

    # 5. SQL: idempotent=False defaults if_exists to 'append' and retries transient errors via max_retries
    sql_file = tmp_path / "append_records.sqlite"
    export_mod.export_to_sql(
        sdk,
        "STUDY",
        "clinical_tbl",
        f"sqlite:///{sql_file}",
        batch_size=40,
        max_retries=2,
        idempotent=True,
    )
    export_mod.export_to_sql(
        sdk,
        "STUDY",
        "clinical_tbl",
        f"sqlite:///{sql_file}",
        batch_size=40,
        max_retries=2,
        idempotent=False,
    )
    import sqlite3

    with sqlite3.connect(sql_file) as conn:
        total_rows = conn.execute("SELECT COUNT(*) FROM clinical_tbl").fetchone()[0]
    assert total_rows == 2
    assert captured_configs[-1].batch_size == 40
    assert captured_configs[-1].max_retries == 2
    assert captured_configs[-1].idempotent is False

    # 6. DuckDB: idempotent=False appends into an existing table via INSERT INTO
    duckdb_mod = pytest.importorskip("duckdb")
    duck_file = tmp_path / "append_records.duckdb"
    export_mod.export_to_duckdb(
        sdk,
        "STUDY",
        str(duck_file),
        "duck_tbl",
        batch_size=30,
        max_retries=2,
        idempotent=True,
    )
    export_mod.export_to_duckdb(
        sdk,
        "STUDY",
        str(duck_file),
        "duck_tbl",
        batch_size=30,
        max_retries=2,
        idempotent=False,
    )
    duck_conn = duckdb_mod.connect(str(duck_file))
    try:
        assert duck_conn.execute("SELECT COUNT(*) FROM duck_tbl").fetchone()[0] == 2
    finally:
        duck_conn.close()
    assert captured_configs[-1].batch_size == 30
    assert captured_configs[-1].idempotent is False
