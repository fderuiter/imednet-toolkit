"""Unit tests for integrations export."""

import sys
from builtins import __import__ as builtin_import
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any
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


def _make_phi_export_sdk() -> MagicMock:
    """Build a mock SDK returning PHI variables with non-PHI human-readable labels."""
    from imednet.models.forms import Form
    from imednet.models.intervals import FormSummary, Interval

    sdk = MagicMock()
    variables = [
        Variable(
            variable_name="patientName",
            variable_type="text",
            label="Participant Display Alias",
            form_id=10,
            form_key="DEMO",
        ),
        Variable(
            variable_name="DOB",
            variable_type="text",
            label="Enrollment Milestone Calendar",
            form_id=10,
            form_key="DEMO",
        ),
        Variable(
            variable_name="SSN",
            variable_type="text",
            label="Registry Reference Code",
            form_id=10,
            form_key="DEMO",
        ),
        Variable(
            variable_name="email",
            variable_type="text",
            label="Notification Channel",
            form_id=10,
            form_key="DEMO",
        ),
        Variable(
            variable_name="systolicBp",
            variable_type="int",
            label="Systolic BP",
            form_id=10,
            form_key="DEMO",
        ),
    ]
    demo_form = Form(
        form_id=10,
        form_key="DEMO",
        form_name="Demographics",
        subject_record_report=True,
        unscheduled_visit=False,
        disabled=False,
    )
    baseline_interval = Interval(
        interval_id=1,
        interval_name="Baseline",
        forms=[FormSummary(form_id=10, form_key="DEMO", form_name="Demographics")],
    )
    sdk.forms.list.return_value = [demo_form]
    sdk.get_forms.return_value = [demo_form]
    sdk.get_intervals.return_value = [baseline_interval]
    sdk.variables.list.return_value = variables
    sdk.get_variables.return_value = variables

    base_record = Record(
        record_id=101,
        study_key="STUDY1",
        subject_key="SUBJ-001",
        site_id=5,
        visit_id=1,
        form_id=10,
        form_key="DEMO",
        record_status="Complete",
        date_modified=datetime(2025, 1, 15, 12, 0, 0),
        record_data={
            "patientName": "Jane Doe",
            "DOB": "1980-01-01",
            "SSN": "123-45-6789",
            "email": "jane@example.org",
            "systolicBp": 118,
        },
    )
    sdk.records.list.side_effect = lambda **_kw: [base_record.model_copy(deep=True)]
    sdk.records.iter.side_effect = lambda **_kw: iter([base_record.model_copy(deep=True)])
    sdk.get_records.side_effect = lambda **_kw: [base_record.model_copy(deep=True)]
    return sdk


def test_tabular_and_hierarchical_exports_mask_phi_with_labels_and_opt_out(
    tmp_path: Path,
) -> None:
    """Verify CSV, Parquet, Excel, DuckDB, JSON (flat & hierarchical), and SQL mask PHI by default."""
    import json
    import sqlite3

    pytest.importorskip("pyarrow")
    pytest.importorskip("openpyxl")
    duckdb_mod = pytest.importorskip("duckdb")
    from imednet.utils.security import global_sensitivity_registry

    sdk = _make_phi_export_sdk()
    snapshot_keywords = set(global_sensitivity_registry._sensitive_keys)
    sensitive_labels = (
        "Participant Display Alias",
        "Enrollment Milestone Calendar",
        "Registry Reference Code",
        "Notification Channel",
    )

    # 1. CSV with use_labels_as_columns=True
    csv_masked = tmp_path / "masked.csv"
    export_mod.export_to_csv(sdk, "STUDY1", str(csv_masked), use_labels_as_columns=True)
    csv_row = pd.read_csv(csv_masked).to_dict("records")[0]
    for label in sensitive_labels:
        assert csv_row[label] == "***MASKED***"
    assert csv_row["Systolic BP"] == 118
    assert csv_row["subjectKey"] == "SUBJ-001"
    assert csv_row["recordId"] == 101

    # 2. Parquet with use_labels_as_columns=True
    pq_masked = tmp_path / "masked.parquet"
    export_mod.export_to_parquet(sdk, "STUDY1", str(pq_masked), use_labels_as_columns=True)
    pq_row = pd.read_parquet(pq_masked).to_dict("records")[0]
    for label in sensitive_labels:
        assert pq_row[label] == "***MASKED***"
    assert pq_row["Systolic BP"] == 118

    # 3. Excel with use_labels_as_columns=True
    xlsx_masked = tmp_path / "masked.xlsx"
    export_mod.export_to_excel(sdk, "STUDY1", str(xlsx_masked), use_labels_as_columns=True)
    xlsx_row = pd.read_excel(xlsx_masked).to_dict("records")[0]
    for label in sensitive_labels:
        assert xlsx_row[label] == "***MASKED***"
    assert xlsx_row["Systolic BP"] == 118

    # 4. DuckDB with use_labels_as_columns=True
    duck_masked = tmp_path / "masked.duckdb"
    export_mod.export_to_duckdb(
        sdk, "STUDY1", str(duck_masked), "records", use_labels_as_columns=True
    )
    conn = duckdb_mod.connect(str(duck_masked))
    try:
        duck_row = conn.execute("SELECT * FROM records").fetchdf().to_dict("records")[0]
    finally:
        conn.close()
    for label in sensitive_labels:
        assert duck_row[label] == "***MASKED***"
    assert duck_row["Systolic BP"] == 118

    # 5. Flat JSON with use_labels_as_columns=True
    json_masked = tmp_path / "masked_flat.json"
    export_mod.export_to_json(sdk, "STUDY1", str(json_masked), use_labels_as_columns=True)
    json_row = json.loads(json_masked.read_text(encoding="utf-8"))[0]
    for label in sensitive_labels:
        assert json_row[label] == "***MASKED***"
    assert json_row["Systolic BP"] == 118

    # 6. Hierarchical JSON (default + use_labels_as_columns=True)
    hier_json = tmp_path / "masked_hier.json"
    export_mod.export_to_json(sdk, "STUDY1", str(hier_json), hierarchical=True)
    hier_payload = json.loads(hier_json.read_text(encoding="utf-8"))
    assert hier_payload[0]["subject_key"] == "SUBJ-001"
    rec_node = hier_payload[0]["visits"][0]["forms"][0]["records"][0]
    assert rec_node["patientName"] == "***MASKED***"
    assert rec_node["DOB"] == "***MASKED***"
    assert rec_node["SSN"] == "***MASKED***"
    assert rec_node["email"] == "***MASKED***"
    assert rec_node["systolicBp"] == 118
    assert rec_node["recordId"] == 101
    assert rec_node["subjectKey"] == "SUBJ-001"

    hier_labeled = tmp_path / "masked_hier_labeled.json"
    export_mod.export_to_json(
        sdk,
        "STUDY1",
        str(hier_labeled),
        hierarchical=True,
        use_labels_as_columns=True,
    )
    labeled_node = json.loads(hier_labeled.read_text(encoding="utf-8"))[0]["visits"][0]["forms"][0][
        "records"
    ][0]
    for label in sensitive_labels:
        assert labeled_node[label] == "***MASKED***"
    assert labeled_node["Systolic BP"] == 118

    # 7. SQL, SQL-by-form, DuckDB-by-form, and long-SQL mask PHI by default
    sql_db = tmp_path / "masked_sql.sqlite"
    export_mod.export_to_sql(
        sdk, "STUDY1", "wide_tbl", f"sqlite:///{sql_db}", use_labels_as_columns=True
    )
    export_mod.export_to_sql_by_form(
        sdk, "STUDY1", f"sqlite:///{sql_db}", use_labels_as_columns=True
    )
    export_mod.export_to_long_sql(sdk, "STUDY1", "long_tbl", f"sqlite:///{sql_db}")
    with sqlite3.connect(sql_db) as sconn:
        wide_df = pd.read_sql_query("SELECT * FROM wide_tbl", sconn)
        demo_df = pd.read_sql_query("SELECT * FROM DEMO", sconn)
        long_df = pd.read_sql_query("SELECT * FROM long_tbl", sconn)
    for label in sensitive_labels:
        assert wide_df.loc[0, label] == "***MASKED***"
        assert demo_df.loc[0, label] == "***MASKED***"
    long_map = dict(zip(long_df["variable_name"], long_df["value"]))
    assert long_map["patientName"] == "***MASKED***"
    assert long_map["DOB"] == "***MASKED***"
    assert long_map["SSN"] == "***MASKED***"
    assert long_map["email"] == "***MASKED***"
    assert str(long_map["systolicBp"]) == "118"

    # 8. Explicit mask_phi=False opt-out returns unmasked values without mutating global registry
    csv_unmasked = tmp_path / "unmasked.csv"
    export_mod.export_to_csv(
        sdk, "STUDY1", str(csv_unmasked), use_labels_as_columns=True, mask_phi=False
    )
    unmasked_csv_row = pd.read_csv(csv_unmasked).to_dict("records")[0]
    assert unmasked_csv_row["Participant Display Alias"] == "Jane Doe"
    assert unmasked_csv_row["Enrollment Milestone Calendar"] == "1980-01-01"
    assert unmasked_csv_row["Registry Reference Code"] == "123-45-6789"
    assert unmasked_csv_row["Notification Channel"] == "jane@example.org"

    hier_unmasked = tmp_path / "unmasked_hier.json"
    export_mod.export_to_json(
        sdk,
        "STUDY1",
        str(hier_unmasked),
        hierarchical=True,
        mask_phi=False,
    )
    unmasked_node = json.loads(hier_unmasked.read_text(encoding="utf-8"))[0]["visits"][0]["forms"][
        0
    ]["records"][0]
    assert unmasked_node["patientName"] == "Jane Doe"
    assert unmasked_node["SSN"] == "123-45-6789"

    sql_unmasked = tmp_path / "unmasked_sql.sqlite"
    export_mod.export_to_long_sql(
        sdk, "STUDY1", "long_tbl", f"sqlite:///{sql_unmasked}", mask_phi=False
    )
    with sqlite3.connect(sql_unmasked) as sconn:
        long_unmasked = pd.read_sql_query("SELECT * FROM long_tbl", sconn)
    unmasked_long_map = dict(zip(long_unmasked["variable_name"], long_unmasked["value"]))
    assert unmasked_long_map["patientName"] == "Jane Doe"
    assert unmasked_long_map["SSN"] == "123-45-6789"
    assert global_sensitivity_registry._sensitive_keys == snapshot_keywords


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


def test_configured_study_exports_mask_six_default_and_custom_phi_before_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify CSV, Parquet, DuckDB, SQL, and hierarchical JSON honor StudyConfiguration PHI rules before applying labels."""
    import json
    import sqlite3
    from types import SimpleNamespace

    pytest.importorskip("pyarrow")
    duckdb_mod = pytest.importorskip("duckdb")

    from imednet.integrations import enrichment as enrichment_mod
    from imednet.models.forms import Form
    from imednet.models.intervals import FormSummary, Interval
    from imednet.models.records import Record
    from imednet.models.study_config import StudyConfiguration
    from imednet.models.variables import Variable
    from imednet.utils.security import global_sensitivity_registry

    study_cfg = StudyConfiguration(
        study_key="STUDY-CFG",
        phi_fields=["customChartId"],
        terminology_lookups={"gender": {"1": "Female"}},
    )
    monkeypatch.setattr(
        enrichment_mod,
        "resolve_study_configuration",
        lambda sk: study_cfg if sk == "STUDY-CFG" else None,
    )

    sdk = MagicMock()
    demo_form = Form(
        form_id=1,
        form_key="DEMO",
        form_name="Demo",
        subject_record_report=True,
        unscheduled_visit=False,
        disabled=False,
    )
    screening_interval = Interval(
        interval_id=10,
        interval_name="Screening",
        forms=[FormSummary(form_id=1, form_key="DEMO", form_name="Demo")],
    )
    sdk.forms.list.return_value = [demo_form]
    sdk.get_forms.return_value = [demo_form]
    sdk.get_intervals.return_value = [screening_interval]
    sdk.visits.list.return_value = [
        SimpleNamespace(visit_id=10, interval_name="Screening", subject_key="SUBJ-1")
    ]
    sdk.subjects.list.return_value = [SimpleNamespace(subject_key="SUBJ-1", site_name="Site A")]
    vars_list = [
        Variable(
            form_id=1,
            form_key="DEMO",
            variable_name="SSN",
            label="National Registry Code",
            variable_type="text",
        ),
        Variable(
            form_id=1,
            form_key="DEMO",
            variable_name="DOB",
            label="Participant Birth Calendar",
            variable_type="text",
        ),
        Variable(
            form_id=1,
            form_key="DEMO",
            variable_name="patientName",
            label="Display Alias",
            variable_type="text",
        ),
        Variable(
            form_id=1,
            form_key="DEMO",
            variable_name="email",
            label="Electronic Reach",
            variable_type="text",
        ),
        Variable(
            form_id=1,
            form_key="DEMO",
            variable_name="phone",
            label="Voice Line",
            variable_type="text",
        ),
        Variable(
            form_id=1,
            form_key="DEMO",
            variable_name="address",
            label="Mailing Street",
            variable_type="text",
        ),
        Variable(
            form_id=1,
            form_key="DEMO",
            variable_name="customChartId",
            label="Internal Chart Ref",
            variable_type="int",
        ),
        Variable(
            form_id=1,
            form_key="DEMO",
            variable_name="gender",
            label="Coded Sex",
            variable_type="int",
        ),
        Variable(
            form_id=1,
            form_key="DEMO",
            variable_name="note",
            label="Investigator Note",
            variable_type="text",
        ),
    ]
    sdk.variables.list.return_value = vars_list
    sdk.get_variables.return_value = vars_list
    sdk.variables.iter.side_effect = lambda **_kw: iter(vars_list)

    rec = Record(
        record_id=55,
        study_key="STUDY-CFG",
        subject_key="SUBJ-1",
        visit_id=10,
        form_id=1,
        form_key="DEMO",
        record_data={
            "SSN": "123-45-6789",
            "DOB": "1979-03-11",
            "patientName": "Jane Doe",
            "email": "jane@clinical.org",
            "phone": "555-0111",
            "address": "742 Evergreen",
            "customChartId": 9988,
            "gender": 1,
            "note": "=SUM(1,2)",
        },
    )
    sdk.records.list.side_effect = lambda **_kw: [rec.model_copy(deep=True)]
    sdk.records.iter.side_effect = lambda **_kw: iter([rec.model_copy(deep=True)])
    sdk.get_records.side_effect = lambda **_kw: [rec.model_copy(deep=True)]

    expected_masked_labels = (
        "National Registry Code",
        "Participant Birth Calendar",
        "Display Alias",
        "Electronic Reach",
        "Voice Line",
        "Mailing Street",
        "Internal Chart Ref",
    )
    registry_snapshot = set(global_sensitivity_registry._sensitive_keys)

    # 1. CSV with labels + formula sanitization preserved
    csv_out = tmp_path / "cfg.csv"
    export_mod.export_to_csv(sdk, "STUDY-CFG", str(csv_out), use_labels_as_columns=True)
    csv_row = pd.read_csv(csv_out).to_dict("records")[0]
    for lbl in expected_masked_labels:
        assert csv_row[lbl] == "***MASKED***"
    assert csv_row["Coded Sex"] == "Female"
    assert str(csv_row["Investigator Note"]).startswith("'=")

    # 2. Parquet with labels (custom int PHI field safely widened to string)
    pq_out = tmp_path / "cfg.parquet"
    export_mod.export_to_parquet(sdk, "STUDY-CFG", str(pq_out), use_labels_as_columns=True)
    pq_row = pd.read_parquet(pq_out).to_dict("records")[0]
    for lbl in expected_masked_labels:
        assert pq_row[lbl] == "***MASKED***"
    assert pq_row["Coded Sex"] == "Female"

    # 3. DuckDB with labels
    ddb_out = tmp_path / "cfg.duckdb"
    export_mod.export_to_duckdb(
        sdk, "STUDY-CFG", str(ddb_out), "demo_tbl", use_labels_as_columns=True
    )
    with duckdb_mod.connect(str(ddb_out)) as dconn:
        ddb_row = dconn.execute("SELECT * FROM demo_tbl").fetchdf().to_dict("records")[0]
    for lbl in expected_masked_labels:
        assert ddb_row[lbl] == "***MASKED***"
    assert ddb_row["Coded Sex"] == "Female"

    # 4. SQL with labels
    sql_out = tmp_path / "cfg.sqlite"
    export_mod.export_to_sql(
        sdk, "STUDY-CFG", "demo_sql", f"sqlite:///{sql_out}", use_labels_as_columns=True
    )
    with sqlite3.connect(sql_out) as sconn:
        sql_row = pd.read_sql_query("SELECT * FROM demo_sql", sconn).to_dict("records")[0]
    for lbl in expected_masked_labels:
        assert sql_row[lbl] == "***MASKED***"
    assert sql_row["Coded Sex"] == "Female"

    # 5. Hierarchical JSON with labels + opt-out
    hier_out = tmp_path / "cfg_hier.json"
    export_mod.export_to_json(
        sdk,
        "STUDY-CFG",
        str(hier_out),
        hierarchical=True,
        use_labels_as_columns=True,
    )
    hier_rec = json.loads(hier_out.read_text(encoding="utf-8"))[0]["visits"][0]["forms"][0][
        "records"
    ][0]
    for lbl in expected_masked_labels:
        assert hier_rec[lbl] == "***MASKED***"
    assert hier_rec["Coded Sex"] == "Female"

    hier_unmasked = tmp_path / "cfg_hier_unmasked.json"
    export_mod.export_to_json(
        sdk,
        "STUDY-CFG",
        str(hier_unmasked),
        hierarchical=True,
        use_labels_as_columns=True,
        mask_phi=False,
    )
    unmasked_rec = json.loads(hier_unmasked.read_text(encoding="utf-8"))[0]["visits"][0]["forms"][
        0
    ]["records"][0]
    assert unmasked_rec["National Registry Code"] == "123-45-6789"
    assert unmasked_rec["Internal Chart Ref"] == 9988
    assert unmasked_rec["Coded Sex"] == "Female"
    assert global_sensitivity_registry._sensitive_keys == registry_snapshot


def test_export_to_json_hierarchical_masks_phi_under_both_label_modes(tmp_path, monkeypatch):
    """Hierarchical export_to_json masks DOB, SSN, and patientName under both use_labels_as_columns settings."""
    import json
    from types import SimpleNamespace

    from imednet.models.variables import Variable

    sdk = MagicMock()
    form_sched = SimpleNamespace(
        form_id=10,
        form_key="DEMOGRAPHICS",
        variables=[
            Variable(variable_name="DOB", label="Enrollment Birth Date", form_id=10),
            Variable(variable_name="SSN", label="Tax Identifier", form_id=10),
            Variable(variable_name="patientName", label="Legal Participant Name", form_id=10),
            Variable(variable_name="heartRate", label="Resting Heart Rate", form_id=10),
        ],
    )
    form_unsched = SimpleNamespace(
        form_id=20,
        form_key="CONTACT",
        variables=[
            Variable(variable_name="patientName", label="Emergency Contact Person", form_id=20),
            Variable(variable_name="SSN", label="Unpopulated National ID", form_id=20),
        ],
    )
    study_struct = SimpleNamespace(intervals=[SimpleNamespace(forms=[form_sched, form_unsched])])
    monkeypatch.setattr(
        "imednet_workflows.study_structure.get_study_structure",
        lambda _sdk, _study_key: study_struct,
    )

    records = [
        Record(
            record_id=501,
            study_key="STUDY_HIER",
            subject_key="SUBJ-501",
            visit_id=1,
            form_id=10,
            form_key="DEMOGRAPHICS",
            record_status="Complete",
            record_data={
                "DOB": "1978-05-14",
                "SSN": "123-45-6789",
                "patientName": "Jane Doe",
                "heartRate": 66,
            },
        ),
        Record(
            record_id=502,
            study_key="STUDY_HIER",
            subject_key="SUBJ-501",
            visit_id=None,
            form_id=20,
            form_key="CONTACT",
            record_status="Complete",
            record_data={"patientName": "John Doe"},
        ),
    ]
    sdk.records.list.side_effect = lambda **_kw: iter([r.model_copy(deep=True) for r in records])

    # 1. use_labels_as_columns=False
    raw_keys_json = tmp_path / "hier_keys.json"
    export_mod.export_to_json(
        sdk,
        "STUDY_HIER",
        str(raw_keys_json),
        hierarchical=True,
        use_labels_as_columns=False,
    )
    payload_keys = json.loads(raw_keys_json.read_text(encoding="utf-8"))
    subj_keys = payload_keys[0]
    assert subj_keys["subject_key"] == "SUBJ-501"
    visit_node = subj_keys["visits"][0]
    assert visit_node["visit_id"] == 1
    form_node = visit_node["forms"][0]
    assert form_node["form_id"] == 10
    rec_keys = form_node["records"][0]
    assert rec_keys["recordId"] == 501
    assert rec_keys["subjectKey"] == "SUBJ-501"
    assert rec_keys["DOB"] == "***MASKED***"
    assert rec_keys["SSN"] == "***MASKED***"
    assert rec_keys["patientName"] == "***MASKED***"
    assert rec_keys["heartRate"] == 66

    unsched_keys = subj_keys["forms"][0]["records"][0]
    assert subj_keys["forms"][0]["form_id"] == 20
    assert unsched_keys["recordId"] == 502
    assert unsched_keys["patientName"] == "***MASKED***"
    assert unsched_keys["SSN"] == "***MASKED***"

    # 2. use_labels_as_columns=True
    labeled_json = tmp_path / "hier_labels.json"
    export_mod.export_to_json(
        sdk,
        "STUDY_HIER",
        str(labeled_json),
        hierarchical=True,
        use_labels_as_columns=True,
    )
    payload_labels = json.loads(labeled_json.read_text(encoding="utf-8"))
    subj_labels = payload_labels[0]
    assert subj_labels["subject_key"] == "SUBJ-501"
    assert subj_labels["visits"][0]["visit_id"] == 1
    assert subj_labels["visits"][0]["forms"][0]["form_id"] == 10
    rec_labels = subj_labels["visits"][0]["forms"][0]["records"][0]
    assert rec_labels["recordId"] == 501
    assert rec_labels["subjectKey"] == "SUBJ-501"
    assert rec_labels["Enrollment Birth Date"] == "***MASKED***"
    assert rec_labels["Tax Identifier"] == "***MASKED***"
    assert rec_labels["Legal Participant Name"] == "***MASKED***"
    assert rec_labels["Resting Heart Rate"] == 66

    unsched_labels = subj_labels["forms"][0]["records"][0]
    assert unsched_labels["Emergency Contact Person"] == "***MASKED***"
    assert unsched_labels["Unpopulated National ID"] == "***MASKED***"


def test_custom_global_sensitivity_registry_key_redacts_across_all_outputs(tmp_path, monkeypatch):
    """Registering a custom variable on global_sensitivity_registry redacts it across tabular, JSON, Hive Parquet, and sink plugins."""
    import json
    from types import SimpleNamespace

    import imednet.integrations.parquet as parquet_mod
    from imednet.integrations.enrichment import CentralizedMapper
    from imednet.models.variables import Variable
    from imednet.utils.security import _normalize_key, global_sensitivity_registry

    custom_var = "biobankSpecimenCode"
    normalized_custom = _normalize_key(custom_var)
    snapshot = set(global_sensitivity_registry._sensitive_keys)
    global_sensitivity_registry.add_sensitive_key(custom_var)

    try:
        assert normalized_custom in global_sensitivity_registry._sensitive_keys

        sdk = MagicMock()
        vars_list = [
            Variable(
                variable_name="biobankSpecimenCode",
                label="Biobank Barcode",
                variable_type="text",
                form_id=10,
                form_key="BIOSPECIMEN",
            ),
            Variable(
                variable_name="volumeMl",
                label="Sample Volume",
                variable_type="int",
                form_id=10,
                form_key="BIOSPECIMEN",
            ),
        ]
        sdk.get_variables.return_value = vars_list
        sdk.variables.list.return_value = vars_list
        sdk.forms.list.return_value = [SimpleNamespace(form_id=10, form_key="BIOSPECIMEN")]

        study_struct = SimpleNamespace(
            intervals=[
                SimpleNamespace(
                    forms=[SimpleNamespace(form_id=10, form_key="BIOSPECIMEN", variables=vars_list)]
                )
            ]
        )
        monkeypatch.setattr(
            "imednet_workflows.study_structure.get_study_structure",
            lambda _sdk, _sk: study_struct,
        )

        rec = Record(
            record_id=777,
            study_key="STUDY_CUSTOM",
            subject_key="SUBJ-777",
            visit_id=1,
            form_id=10,
            form_key="BIOSPECIMEN",
            record_status="Complete",
            record_data={"biobankSpecimenCode": "BIO-RAW-999", "volumeMl": 5},
        )
        sdk.records.list.side_effect = lambda **_kw: iter([rec.model_copy(deep=True)])
        sdk.records.iter.side_effect = lambda **_kw: iter([rec.model_copy(deep=True)])
        sdk.get_records.side_effect = lambda **_kw: [rec.model_copy(deep=True)]

        # 1. Tabular CSV
        csv_out = tmp_path / "custom.csv"
        export_mod.export_to_csv(sdk, "STUDY_CUSTOM", str(csv_out), quality_gate_enabled=False)
        csv_df = pd.read_csv(csv_out)
        assert csv_df["biobankSpecimenCode"].iloc[0] == "***MASKED***"
        assert csv_df["subjectKey"].iloc[0] == "SUBJ-777"
        assert csv_df["volumeMl"].iloc[0] == 5

        # 2. Hierarchical JSON with labels
        hier_out = tmp_path / "custom_hier.json"
        export_mod.export_to_json(
            sdk,
            "STUDY_CUSTOM",
            str(hier_out),
            hierarchical=True,
            use_labels_as_columns=True,
        )
        hier_data = json.loads(hier_out.read_text(encoding="utf-8"))
        hier_rec = hier_data[0]["visits"][0]["forms"][0]["records"][0]
        assert hier_rec["Biobank Barcode"] == "***MASKED***"
        assert hier_rec["Sample Volume"] == 5
        assert hier_data[0]["subject_key"] == "SUBJ-777"

        # 3. Hive Parquet
        pytest.importorskip("pyarrow")
        hive_dir = tmp_path / "hive_custom"
        parquet_mod.export_to_hive_parquet(sdk, "STUDY_CUSTOM", str(hive_dir))
        hive_files = list(
            (hive_dir / "study_key=STUDY_CUSTOM" / "form_key=BIOSPECIMEN").glob("**/*.parquet")
        )
        hive_df = pd.read_parquet(hive_files[0])
        assert hive_df["biobankSpecimenCode"].iloc[0] == "***MASKED***"
        assert hive_df["volumeMl"].iloc[0] == 5
        assert hive_df["subjectKey"].iloc[0] == "SUBJ-777"

        # 4. CentralizedMapper (used by MongoDB, Neo4j, Snowflake sinks) across case variants
        c_mapper = CentralizedMapper(mode="document")
        mapped_doc = c_mapper.map_record(
            {
                "record_id": 777,
                "subject_key": "SUBJ-777",
                "record_data": {
                    "biobankSpecimenCode": "BIO-1",
                    "BIOBANK_SPECIMEN_CODE": "BIO-2",
                    "volume_ml": 5,
                },
            },
            study_key="STUDY_CUSTOM",
        )
        assert mapped_doc["subject_key"] == "SUBJ-777"
        assert mapped_doc["record_data"]["biobank_specimen_code"] == "***MASKED***"
        assert mapped_doc["record_data"]["volume_ml"] == 5
    finally:
        global_sensitivity_registry._sensitive_keys.clear()
        global_sensitivity_registry._sensitive_keys.update(snapshot)


def test_by_form_exports_50_forms_5000_records_single_pass_http_counts(tmp_path: Path) -> None:
    """Verify 50-Form, 5,000-Record DuckDB and SQL per-Form exports issue 52 HTTP calls total."""
    import sqlite3

    import httpx
    import respx

    from imednet.sdk import ImednetSDK

    duckdb_mod = pytest.importorskip("duckdb")

    forms_payload = [{"formId": i, "formKey": f"FORM_{i:02d}"} for i in range(1, 51)]
    variables_payload = [
        {
            "variableName": f"VAL_{i:02d}",
            "variableType": "int",
            "label": f"Value {i:02d}",
            "formId": i,
            "formKey": f"FORM_{i:02d}",
        }
        for i in range(1, 51)
    ]

    # Populate 30 forms across 5,000 records; leave FORM_31..FORM_50 empty (0 records)
    all_records: list[dict[str, Any]] = []
    for rec_idx in range(1, 5001):
        fid = ((rec_idx - 1) % 30) + 1
        all_records.append(
            {
                "recordId": rec_idx,
                "studyKey": "BENCH50",
                "subjectKey": f"SUBJ-{(rec_idx % 100) + 1:03d}",
                "visitId": 1,
                "formId": fid,
                "formKey": f"FORM_{fid:02d}",
                "recordStatus": "Complete",
                "deleted": False,
                "dateModified": "2026-06-01T00:00:00Z",
                "recordData": {f"VAL_{fid:02d}": rec_idx},
            }
        )

    counts = {"forms": 0, "variables": 0, "records": 0}
    record_filters: list[str] = []

    with respx.mock(assert_all_called=True) as router:

        def forms_handler(_req: httpx.Request) -> httpx.Response:
            counts["forms"] += 1
            return httpx.Response(200, json={"data": forms_payload})

        def vars_handler(_req: httpx.Request) -> httpx.Response:
            counts["variables"] += 1
            return httpx.Response(200, json={"data": variables_payload})

        def records_handler(req: httpx.Request) -> httpx.Response:
            counts["records"] += 1
            flt = req.url.params.get("filter", "")
            record_filters.append(flt)
            page = int(req.url.params.get("page", "0"))
            size = int(req.url.params.get("size", "100"))
            start = page * size
            page_slice = all_records[start : start + size]
            total_pages = (len(all_records) + size - 1) // size
            return httpx.Response(
                200,
                json={
                    "data": page_slice,
                    "pagination": {
                        "currentPage": page,
                        "size": size,
                        "totalPages": total_pages,
                        "totalElements": len(all_records),
                    },
                },
            )

        router.get("https://api.test/api/v1/edc/studies/BENCH50/forms").mock(
            side_effect=forms_handler
        )
        router.get("https://api.test/api/v1/edc/studies/BENCH50/variables").mock(
            side_effect=vars_handler
        )
        router.get("https://api.test/api/v1/edc/studies/BENCH50/records").mock(
            side_effect=records_handler
        )

        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")

        # 1. DuckDB by form: 1 /forms + 1 /variables + 50 /records pages = 52 HTTP calls
        ddb_path = tmp_path / "bench50.duckdb"
        export_mod.export_to_duckdb_by_form(sdk, "BENCH50", str(ddb_path), batch_size=200)
        assert counts == {"forms": 1, "variables": 1, "records": 50}
        assert sum(counts.values()) == 52
        assert all("formId" not in flt for flt in record_filters)

        with duckdb_mod.connect(str(ddb_path)) as dconn:
            ddb_tables = {r[0] for r in dconn.execute("SHOW TABLES").fetchall()}
            assert len(ddb_tables) == 50
            populated_total = sum(
                dconn.execute(f'SELECT COUNT(*) FROM "FORM_{i:02d}"').fetchone()[0]
                for i in range(1, 31)
            )
            assert populated_total == 5000
            for empty_i in range(31, 51):
                tbl = f"FORM_{empty_i:02d}"
                assert dconn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0] == 0
                col_types = {
                    r[1]: r[2].upper()
                    for r in dconn.execute(f"PRAGMA table_info('{tbl}')").fetchall()
                }
                assert col_types[f"VAL_{empty_i:02d}"] == "BIGINT"

        # 2. SQL by form with quality_gate_enabled=True: still 52 HTTP calls (0 extra /variables calls!)
        counts.update({"forms": 0, "variables": 0, "records": 0})
        record_filters.clear()
        sql_path = tmp_path / "bench50.sqlite"
        export_mod.export_to_sql_by_form(
            sdk,
            "BENCH50",
            f"sqlite:///{sql_path}",
            quality_gate_enabled=True,
            batch_size=200,
        )
        assert counts == {"forms": 1, "variables": 1, "records": 50}
        assert sum(counts.values()) == 52
        assert all("formId" not in flt for flt in record_filters)

        with sqlite3.connect(sql_path) as sconn:
            sql_tables = {
                r[0]
                for r in sconn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert len(sql_tables) == 50
            sql_total = sum(
                sconn.execute(f'SELECT COUNT(*) FROM "FORM_{i:02d}"').fetchone()[0]
                for i in range(1, 31)
            )
            assert sql_total == 5000
            for empty_i in range(31, 51):
                tbl = f"FORM_{empty_i:02d}"
                assert sconn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0] == 0
                cols = {r[1] for r in sconn.execute(f"PRAGMA table_info('{tbl}')").fetchall()}
                assert f"VAL_{empty_i:02d}" in cols


def test_sql_by_form_quality_gate_drops_low_readiness_with_single_variables_fetch(
    tmp_path: Path,
) -> None:
    """Verify export_to_sql_by_form with quality_gate_enabled=True fetches /variables once and filters."""
    import sqlite3

    import httpx
    import respx

    from imednet.sdk import ImednetSDK

    var_calls = 0
    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.test/api/v1/edc/studies/QG_STUDY/forms").respond(
            200,
            json={
                "data": [
                    {"formId": 1, "formKey": "DEMO"},
                    {"formId": 2, "formKey": "LABS"},
                ]
            },
        )

        def var_handler(_req: httpx.Request) -> httpx.Response:
            nonlocal var_calls
            var_calls += 1
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "variableName": "AGE",
                            "variableType": "int",
                            "label": "Age",
                            "formId": 1,
                            "formKey": "DEMO",
                        },
                        {
                            "variableName": "GLUCOSE",
                            "variableType": "float",
                            "label": "Glucose",
                            "formId": 2,
                            "formKey": "LABS",
                        },
                    ]
                },
            )

        router.get("https://api.test/api/v1/edc/studies/QG_STUDY/variables").mock(
            side_effect=var_handler
        )
        router.get("https://api.test/api/v1/edc/studies/QG_STUDY/records").respond(
            200,
            json={
                "data": [
                    {
                        "recordId": 10,
                        "formId": 1,
                        "formKey": "DEMO",
                        "subjectKey": "S1",
                        "recordData": {"AGE": 45},
                    },
                    {
                        "recordId": 11,
                        "formId": 1,
                        "formKey": "DEMO",
                        "subjectKey": "S2",
                        "recordData": {},
                    },
                    {
                        "recordId": 20,
                        "formId": 2,
                        "formKey": "LABS",
                        "subjectKey": "S1",
                        "recordData": {"GLUCOSE": 92.5},
                    },
                    {
                        "recordId": 21,
                        "formId": 2,
                        "formKey": "LABS",
                        "subjectKey": "S2",
                        "recordData": {"GLUCOSE": "not-a-float"},
                    },
                ]
            },
        )

        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
        sql_path = tmp_path / "qg_by_form.sqlite"
        export_mod.export_to_sql_by_form(
            sdk,
            "QG_STUDY",
            f"sqlite:///{sql_path}",
            quality_gate_enabled=True,
            min_schema_readiness_score=100.0,
        )

        assert var_calls == 1
        with sqlite3.connect(sql_path) as conn:
            demo_ids = [r[0] for r in conn.execute('SELECT "recordId" FROM "DEMO"').fetchall()]
            labs_ids = [r[0] for r in conn.execute('SELECT "recordId" FROM "LABS"').fetchall()]
        assert demo_ids == [10]
        assert labs_ids == [20]


def test_incremental_sql_by_form_watermarks_reconcile_deleted_and_post_flush_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify per-Form watermarks, deleted reconciliation, and atomic post-flush checkpoints."""
    import contextlib
    import sqlite3
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from imednet.models.records import Record
    from imednet.models.variables import Variable
    from imednet_workflows.state_ledger import FileStateProvider

    sdk = MagicMock()
    sdk.forms.list.return_value = [
        SimpleNamespace(form_id=1, form_key="DEMO"),
        SimpleNamespace(form_id=2, form_key="VITALS"),
        SimpleNamespace(form_id=3, form_key="UNCHANGED"),
    ]
    vars_list = [
        Variable(variable_name="AGE", label="Age", variable_type="int", form_id=1, form_key="DEMO"),
        Variable(variable_name="HR", label="HR", variable_type="int", form_id=2, form_key="VITALS"),
        Variable(
            variable_name="NOTE",
            label="Note",
            variable_type="text",
            form_id=3,
            form_key="UNCHANGED",
        ),
    ]
    sdk.variables.list.return_value = vars_list
    sdk.get_variables.return_value = vars_list

    initial_records = [
        Record(
            record_id=101,
            subject_key="S1",
            visit_id=1,
            form_id=1,
            form_key="DEMO",
            date_modified="2026-06-01T00:00:00Z",
            record_data={"AGE": 40},
        ),
        Record(
            record_id=201,
            subject_key="S1",
            visit_id=1,
            form_id=2,
            form_key="VITALS",
            date_modified="2026-06-01T00:00:00Z",
            record_data={"HR": 70},
        ),
        Record(
            record_id=202,
            subject_key="S2",
            visit_id=1,
            form_id=2,
            form_key="VITALS",
            date_modified="2026-06-01T00:00:00Z",
            record_data={"HR": 75},
        ),
        Record(
            record_id=301,
            subject_key="S1",
            visit_id=1,
            form_id=3,
            form_key="UNCHANGED",
            date_modified="2026-06-01T00:00:00Z",
            record_data={"NOTE": "keep-me"},
        ),
    ]
    sdk.records.list.side_effect = lambda **_kw: iter(initial_records)

    ledger_path = tmp_path / "ledger.json"
    provider = FileStateProvider(str(ledger_path))
    sql_path = tmp_path / "inc_by_form.sqlite"

    # Seed initial full load
    export_mod.export_to_sql_by_form(sdk, "INC_STUDY", f"sqlite:///{sql_path}")

    # Seed uneven per-form watermarks: DEMO is at June 5, VITALS is at June 2, UNCHANGED is at June 5
    ts_june_5 = datetime(2026, 6, 5, 0, 0, tzinfo=UTC)
    ts_june_2 = datetime(2026, 6, 2, 0, 0, tzinfo=UTC)
    provider.set_last_timestamp("INC_STUDY", "sql_DEMO", ts_june_5, records_processed=1)
    provider.set_last_timestamp("INC_STUDY", "sql_VITALS", ts_june_2, records_processed=2)
    provider.set_last_timestamp("INC_STUDY", "sql_UNCHANGED", ts_june_5, records_processed=1)

    delta_stream = [
        # Stale for DEMO (June 3 < June 5 watermark) -> must NOT overwrite DEMO record 101!
        Record(
            record_id=101,
            subject_key="S1",
            visit_id=1,
            form_id=1,
            form_key="DEMO",
            date_modified="2026-06-03T00:00:00Z",
            record_data={"AGE": 999},
        ),
        # Fresh update for DEMO (June 7 >= June 5)
        Record(
            record_id=102,
            subject_key="S2",
            visit_id=1,
            form_id=1,
            form_key="DEMO",
            date_modified="2026-06-07T00:00:00Z",
            record_data={"AGE": 51},
        ),
        # Newly voided VITALS record 201 (June 4 >= June 2) -> must be deleted from VITALS!
        Record(
            record_id=201,
            subject_key="S1",
            visit_id=1,
            form_id=2,
            form_key="VITALS",
            deleted=True,
            date_modified="2026-06-04T00:00:00Z",
            record_data={},
        ),
    ]

    # 1. Simulate a failure during the final Form sink's flush -> NO checkpoints may advance!
    sdk.records.list.side_effect = lambda **_kw: iter(delta_stream)
    orig_flush = export_mod.TabularSQLSink.flush

    def _failing_last_flush(self_sink: export_mod.TabularSQLSink) -> None:
        if self_sink.table == "DEMO":
            raise RuntimeError("disk full during final sink flush")
        orig_flush(self_sink)

    monkeypatch.setattr(export_mod.TabularSQLSink, "flush", _failing_last_flush)
    with pytest.raises(RuntimeError, match="disk full during final sink flush"):
        export_mod.export_to_sql_by_form(
            sdk,
            "INC_STUDY",
            f"sqlite:///{sql_path}",
            incremental=True,
            state_provider=provider,
        )

    assert provider.get_last_timestamp("INC_STUDY", "sql_DEMO") == ts_june_5
    assert provider.get_last_timestamp("INC_STUDY", "sql_VITALS") == ts_june_2
    assert provider.get_last_timestamp("INC_STUDY", "sql_UNCHANGED") == ts_june_5
    for s_name in ("sql_DEMO", "sql_VITALS", "sql_UNCHANGED"):
        assert str(provider.get_last_status("INC_STUDY", s_name)).lower().endswith("failed")

    # 2. Restore flush and re-run -> single /records call, deleted row reconciled, checkpoints committed post-flush
    monkeypatch.setattr(export_mod.TabularSQLSink, "flush", orig_flush)
    flush_order: list[str] = []
    orig_tx = provider.transaction

    def _spy_flush(self_sink: export_mod.TabularSQLSink) -> None:
        flush_order.append(f"flush:{self_sink.table}")
        orig_flush(self_sink)

    @contextlib.contextmanager
    def _spy_tx(study_key: str, stream_name: str, fallback_timestamp: Any = None):
        flush_order.append(f"enter:{stream_name}")
        with orig_tx(study_key, stream_name, fallback_timestamp=fallback_timestamp) as tx:
            yield tx
        flush_order.append(f"commit:{stream_name}")

    monkeypatch.setattr(export_mod.TabularSQLSink, "flush", _spy_flush)
    monkeypatch.setattr(provider, "transaction", _spy_tx)
    sdk.records.list.reset_mock()
    sdk.records.list.side_effect = lambda **_kw: iter(delta_stream)

    export_mod.export_to_sql_by_form(
        sdk,
        "INC_STUDY",
        f"sqlite:///{sql_path}",
        incremental=True,
        state_provider=provider,
    )

    assert sdk.records.list.call_count == 1
    assert flush_order[:3] == ["enter:sql_DEMO", "enter:sql_VITALS", "enter:sql_UNCHANGED"]
    assert set(flush_order[3:6]) == {"flush:DEMO", "flush:VITALS", "flush:UNCHANGED"}
    assert set(flush_order[6:]) == {"commit:sql_DEMO", "commit:sql_VITALS", "commit:sql_UNCHANGED"}

    with sqlite3.connect(sql_path) as conn:
        demo_rows = conn.execute(
            'SELECT "recordId", "AGE" FROM "DEMO" ORDER BY "recordId"'
        ).fetchall()
        vitals_rows = conn.execute(
            'SELECT "recordId", "HR" FROM "VITALS" ORDER BY "recordId"'
        ).fetchall()
        unchanged_rows = conn.execute('SELECT "recordId", "NOTE" FROM "UNCHANGED"').fetchall()

    assert demo_rows == [(101, 40), (102, 51)]
    assert vitals_rows == [(202, 75)]
    assert unchanged_rows == [(301, "keep-me")]
    assert provider.get_last_timestamp("INC_STUDY", "sql_DEMO") == datetime(
        2026, 6, 7, 0, 0, tzinfo=UTC
    )
    assert provider.get_last_timestamp("INC_STUDY", "sql_VITALS") == datetime(
        2026, 6, 4, 0, 0, tzinfo=UTC
    )

    # 3. Verify DuckDB incremental per-form watermarks, upserts, and voided delete reconciliation
    duckdb_mod = pytest.importorskip("duckdb")
    ddb_path = tmp_path / "inc_by_form.duckdb"
    ddb_provider = FileStateProvider(str(tmp_path / "ddb_ledger.json"))
    sdk.records.list.side_effect = lambda **_kw: iter(initial_records)
    export_mod.export_to_duckdb_by_form(sdk, "INC_STUDY", str(ddb_path))

    ddb_provider.set_last_timestamp("INC_STUDY", "duckdb_DEMO", ts_june_5, records_processed=1)
    ddb_provider.set_last_timestamp("INC_STUDY", "duckdb_VITALS", ts_june_2, records_processed=2)
    ddb_provider.set_last_timestamp("INC_STUDY", "duckdb_UNCHANGED", ts_june_5, records_processed=1)

    sdk.records.list.reset_mock()
    sdk.records.list.side_effect = lambda **_kw: iter(delta_stream)
    export_mod.export_to_duckdb_by_form(
        sdk,
        "INC_STUDY",
        str(ddb_path),
        incremental=True,
        state_provider=ddb_provider,
    )
    assert sdk.records.list.call_count == 1

    with duckdb_mod.connect(str(ddb_path)) as dconn:
        ddb_demo = dconn.execute(
            'SELECT "recordId", "AGE" FROM "DEMO" ORDER BY "recordId"'
        ).fetchall()
        ddb_vitals = dconn.execute(
            'SELECT "recordId", "HR" FROM "VITALS" ORDER BY "recordId"'
        ).fetchall()
        ddb_unchanged = dconn.execute('SELECT "recordId", "NOTE" FROM "UNCHANGED"').fetchall()

    assert ddb_demo == [(101, 40), (102, 51)]
    assert ddb_vitals == [(202, 75)]
    assert ddb_unchanged == [(301, "keep-me")]


def test_by_form_exports_bound_memory_and_preserve_phi_masking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify max_buffered_records triggers mid-stream flushes and PHI masking/opt-out holds."""
    import sqlite3
    from types import SimpleNamespace

    from imednet.models.records import Record
    from imednet.models.variables import Variable

    duckdb_mod = pytest.importorskip("duckdb")

    sdk = MagicMock()
    sdk.forms.list.return_value = [
        SimpleNamespace(form_id=i, form_key=f"F{i}") for i in range(1, 6)
    ]
    vars_list = [
        Variable(
            variable_name="patientName",
            label="Participant Alias",
            variable_type="text",
            form_id=i,
            form_key=f"F{i}",
        )
        for i in range(1, 6)
    ]
    sdk.variables.list.return_value = vars_list
    sdk.get_variables.return_value = vars_list

    # 15 records interleaved across 5 forms (3 per form); batch_size=50, max_buffered_records=5
    records = [
        Record(
            record_id=idx,
            subject_key=f"S{idx}",
            visit_id=1,
            form_id=((idx - 1) % 5) + 1,
            form_key=f"F{((idx - 1) % 5) + 1}",
            record_data={"patientName": f"Alice {idx}"},
        )
        for idx in range(1, 16)
    ]

    events: list[str] = []

    def _stream_with_events(**_kw: Any):
        for r in records:
            events.append("read")
            yield r.model_copy(deep=True)

    sdk.records.list.side_effect = _stream_with_events
    orig_write_batch = export_mod.TabularSQLSink.write_batch

    def _spy_write_batch(self_sink: export_mod.TabularSQLSink, recs: Any, *, batch_id: str) -> int:
        events.append(f"write:{self_sink.table}")
        return orig_write_batch(self_sink, recs, batch_id=batch_id)

    monkeypatch.setattr(export_mod.TabularSQLSink, "write_batch", _spy_write_batch)

    sql_masked = tmp_path / "ceil_masked.sqlite"
    export_mod.export_to_sql_by_form(
        sdk,
        "PHI_CEIL",
        f"sqlite:///{sql_masked}",
        use_labels_as_columns=True,
        batch_size=50,
        max_buffered_records=5,
    )

    # First write MUST happen right after the 5th read (mid-stream ceiling eviction, well before 15th read!)
    first_write_pos = next(i for i, e in enumerate(events) if e.startswith("write:"))
    reads_before_first_write = sum(1 for e in events[:first_write_pos] if e == "read")
    assert reads_before_first_write == 5

    with sqlite3.connect(sql_masked) as sconn:
        for i in range(1, 6):
            vals = [
                r[0] for r in sconn.execute(f'SELECT "Participant Alias" FROM "F{i}"').fetchall()
            ]
            assert vals == ["***MASKED***", "***MASKED***", "***MASKED***"]

    # DuckDB masked vs mask_phi=False opt-out with labels
    ddb_masked = tmp_path / "ddb_masked.duckdb"
    ddb_unmasked = tmp_path / "ddb_unmasked.duckdb"
    sdk.records.list.side_effect = lambda **_kw: iter([r.model_copy(deep=True) for r in records])
    export_mod.export_to_duckdb_by_form(
        sdk,
        "PHI_CEIL",
        str(ddb_masked),
        use_labels_as_columns=True,
        batch_size=50,
        max_buffered_records=5,
    )
    sdk.records.list.side_effect = lambda **_kw: iter([r.model_copy(deep=True) for r in records])
    export_mod.export_to_duckdb_by_form(
        sdk,
        "PHI_CEIL",
        str(ddb_unmasked),
        use_labels_as_columns=True,
        mask_phi=False,
        batch_size=50,
        max_buffered_records=5,
    )

    with duckdb_mod.connect(str(ddb_masked)) as mconn:
        masked_vals = [
            r[0] for r in mconn.execute('SELECT "Participant Alias" FROM "F1"').fetchall()
        ]
    with duckdb_mod.connect(str(ddb_unmasked)) as uconn:
        unmasked_vals = sorted(
            r[0] for r in uconn.execute('SELECT "Participant Alias" FROM "F1"').fetchall()
        )
    assert masked_vals == ["***MASKED***", "***MASKED***", "***MASKED***"]
    assert unmasked_vals == ["Alice 1", "Alice 11", "Alice 6"]


def test_by_form_exports_recover_stale_running_lease_without_premature_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify stale RUNNING leases are recovered inside transaction without premature SUCCESS on failure."""
    import json
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from imednet.models.records import Record
    from imednet.models.variables import Variable
    from imednet_workflows.state_ledger import FileStateProvider

    sdk = MagicMock()
    sdk.forms.list.return_value = [SimpleNamespace(form_id=1, form_key="DEMO")]
    vars_list = [
        Variable(
            variable_name="AGE", label="Age", variable_type="integer", form_id=1, form_key="DEMO"
        )
    ]
    sdk.variables.list.return_value = vars_list
    sdk.get_variables.return_value = vars_list

    ledger_file = tmp_path / "stale_lease_ledger.json"
    ledger_file.write_text(
        json.dumps(
            {
                "version": 1,
                "studies": {
                    "STALE_STUDY": {
                        "streams": {
                            "sql_DEMO": {
                                "last_timestamp": "2026-06-01T00:00:00+00:00",
                                "status": "RUNNING",
                                "updated_at": "2020-01-01T00:00:00+00:00",
                                "records_processed": 5,
                                "owner_pid": 99999999,
                                "owner_host": "crashed-worker",
                                "lease_expires_at": "2020-01-01T00:05:00+00:00",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    provider = FileStateProvider(str(ledger_file))

    delta_records = [
        Record(
            record_id=10,
            subject_key="S1",
            visit_id=1,
            form_id=1,
            form_key="DEMO",
            date_modified="2026-06-05T00:00:00Z",
            record_data={"AGE": 33},
        )
    ]
    sdk.records.list.side_effect = lambda **_kw: iter(delta_records)

    orig_flush = export_mod.TabularSQLSink.flush

    def _boom_flush(_self_sink: export_mod.TabularSQLSink) -> None:
        raise RuntimeError("mid-flush failure after stale lease recovery")

    monkeypatch.setattr(export_mod.TabularSQLSink, "flush", _boom_flush)
    sql_path = tmp_path / "stale.sqlite"
    with pytest.raises(RuntimeError, match="mid-flush failure after stale lease recovery"):
        export_mod.export_to_sql_by_form(
            sdk,
            "STALE_STUDY",
            f"sqlite:///{sql_path}",
            incremental=True,
            state_provider=provider,
            mask_phi=False,
        )

    assert str(provider.get_last_status("STALE_STUDY", "sql_DEMO")).lower().endswith("failed")
    assert provider.get_last_timestamp("STALE_STUDY", "sql_DEMO") == datetime(
        2026, 6, 1, 0, 0, tzinfo=UTC
    )

    monkeypatch.setattr(export_mod.TabularSQLSink, "flush", orig_flush)
    sdk.records.list.side_effect = lambda **_kw: iter(delta_records)
    export_mod.export_to_sql_by_form(
        sdk,
        "STALE_STUDY",
        f"sqlite:///{sql_path}",
        incremental=True,
        state_provider=provider,
        mask_phi=False,
    )
    assert str(provider.get_last_status("STALE_STUDY", "sql_DEMO")).lower().endswith("success")
    assert provider.get_last_timestamp("STALE_STUDY", "sql_DEMO") == datetime(
        2026, 6, 5, 0, 0, tzinfo=UTC
    )


def test_by_form_exports_enforce_required_variables_and_tabular_import_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify by-form exports enforce Variable(required=True), #1979 CDISC/collapsed PHI keys, and dataframe_to_records."""
    import sqlite3
    from types import SimpleNamespace

    from imednet.errors import ValidationError
    from imednet.integrations import enrichment as enrichment_mod
    from imednet.models.records import Record
    from imednet.models.study_config import StudyConfiguration
    from imednet.models.variables import Variable
    from imednet.utils.pandas import MASKED_SENTINEL, dataframe_to_records
    from imednet.validation.cache import SchemaCache

    duckdb_mod = pytest.importorskip("duckdb")

    study_cfg = StudyConfiguration(
        study_key="RT_STUDY",
        phi_fields=["patient_chart_id"],
    )
    monkeypatch.setattr(
        enrichment_mod,
        "resolve_study_configuration",
        lambda sk: study_cfg if sk == "RT_STUDY" else None,
    )

    sdk = MagicMock()
    forms = [
        SimpleNamespace(form_id=10, form_key="DEMO"),
        SimpleNamespace(form_id=20, form_key="VITALS"),
    ]
    sdk.forms.list.return_value = forms
    vars_list = [
        Variable(
            variable_name="patientName",
            label="Patient Name",
            variable_type="text",
            form_id=10,
            form_key="DEMO",
            required=True,
        ),
        Variable(
            variable_name="BRTHDTC",
            label="Birth Date",
            variable_type="text",
            form_id=10,
            form_key="DEMO",
        ),
        Variable(
            variable_name="PATIENTCHARTID",
            label="Chart ID",
            variable_type="text",
            form_id=10,
            form_key="DEMO",
        ),
        Variable(
            variable_name="SITE_CODE",
            label="Site Code",
            variable_type="text",
            form_id=10,
            form_key="DEMO",
        ),
        Variable(
            variable_name="HR",
            label="Heart Rate",
            variable_type="int",
            form_id=20,
            form_key="VITALS",
            required=True,
        ),
    ]
    sdk.variables.list.return_value = vars_list
    sdk.get_variables.side_effect = AssertionError("must reuse prefetched variables")

    records = [
        Record(
            record_id=101,
            study_key="RT_STUDY",
            subject_key="SUBJ-1",
            visit_id=1,
            form_id=10,
            form_key="DEMO",
            record_data={
                "patientName": "Jane Doe",
                "BRTHDTC": "1985-04-12",
                "PATIENTCHARTID": "CH-900",
                "SITE_CODE": "0042",
            },
        ),
        Record(
            record_id=102,
            study_key="RT_STUDY",
            subject_key="SUBJ-2",
            visit_id=1,
            form_id=10,
            form_key="DEMO",
            record_data={
                "patientName": "",
                "BRTHDTC": "1990-01-01",
                "PATIENTCHARTID": "CH-901",
                "SITE_CODE": "0099",
            },
        ),
        Record(
            record_id=201,
            study_key="RT_STUDY",
            subject_key="SUBJ-1",
            visit_id=1,
            form_id=20,
            form_key="VITALS",
            record_data={"HR": 72},
        ),
    ]

    sql_masked = tmp_path / "rt_masked.sqlite"
    sdk.records.list.side_effect = lambda **_kw: iter([r.model_copy(deep=True) for r in records])
    export_mod.export_to_sql_by_form(
        sdk,
        "RT_STUDY",
        f"sqlite:///{sql_masked}",
        quality_gate_enabled=True,
        min_schema_readiness_score=100.0,
    )
    assert sdk.variables.list.call_count == 1

    with sqlite3.connect(sql_masked) as sconn:
        demo_masked_df = pd.read_sql_query('SELECT * FROM "DEMO"', sconn)
    assert len(demo_masked_df) == 1
    assert demo_masked_df["patientName"].iloc[0] == MASKED_SENTINEL
    assert demo_masked_df["BRTHDTC"].iloc[0] == MASKED_SENTINEL
    assert demo_masked_df["PATIENTCHARTID"].iloc[0] == MASKED_SENTINEL
    assert demo_masked_df["SITE_CODE"].iloc[0] == "0042"
    with pytest.raises(ValidationError, match="contains masked PHI sentinel"):
        dataframe_to_records(demo_masked_df)

    ddb_unmasked = tmp_path / "rt_unmasked.duckdb"
    sdk.records.list.side_effect = lambda **_kw: iter([r.model_copy(deep=True) for r in records])
    export_mod.export_to_duckdb_by_form(
        sdk,
        "RT_STUDY",
        str(ddb_unmasked),
        quality_gate_enabled=True,
        min_schema_readiness_score=100.0,
        mask_phi=False,
    )

    schema = SchemaCache()
    schema.populate(vars_list)
    with duckdb_mod.connect(str(ddb_unmasked)) as uconn:
        demo_unmasked_df = uconn.execute('SELECT * FROM "DEMO"').df()
        vitals_unmasked_df = uconn.execute('SELECT * FROM "VITALS"').df()

    demo_payloads = dataframe_to_records(demo_unmasked_df, schema=schema)
    vitals_payloads = dataframe_to_records(vitals_unmasked_df, schema=schema)
    assert demo_payloads == [
        {
            "formId": 10,
            "subjectKey": "SUBJ-1",
            "data": {
                "patientName": "Jane Doe",
                "BRTHDTC": "1985-04-12",
                "PATIENTCHARTID": "CH-900",
                "SITE_CODE": "0042",
            },
        }
    ]
    assert vitals_payloads == [
        {
            "formId": 20,
            "subjectKey": "SUBJ-1",
            "data": {"HR": 72},
        }
    ]


def test_export_to_csv_and_tabular_sinks_as_sdtm_domain_ae_without_batch_error(
    tmp_path: Path,
) -> None:
    """export_to_csv(sdk, study_key, path, as_sdtm=True, domain='AE') succeeds without ExportBatchError and masks PHI."""
    from imednet.models.forms import Form
    from imednet.models.records import Record
    from imednet.models.variables import Variable

    sdk = MagicMock()
    sdk.forms.list.return_value = [Form(form_id=10, form_key="AE", form_name="Adverse Events")]
    sdk.variables.list.return_value = [
        Variable(variable_id=1, form_id=10, form_key="AE", variable_name="aeTerm", label="AE Term"),
        Variable(
            variable_id=2,
            form_id=10,
            form_key="AE",
            variable_name="aeStartDate",
            label="Start Date",
        ),
        Variable(
            variable_id=3,
            form_id=10,
            form_key="AE",
            variable_name="aeSerious",
            label="Serious",
        ),
        Variable(
            variable_id=4,
            form_id=10,
            form_key="AE",
            variable_name="patientName",
            label="Patient Name",
        ),
    ]
    ae_records = [
        Record(
            record_id=101,
            study_key="TRIAL_AE",
            form_id=10,
            form_key="AE",
            subject_key="SUBJ-01",
            record_data={
                "aeTerm": "Headache",
                "aeDecod": "HEADACHE",
                "aeStartDate": "2026-03-01 09:15:00",
                "aeSerious": True,
                "aeSeverity": "1",
                "aeRelationship": "POSSIBLE",
                "aeActionTaken": "NONE",
                "aeOutcome": "RECOVERED",
                "patientName": "John Secret",
            },
        ),
        Record(
            record_id=102,
            study_key="TRIAL_AE",
            form_id=10,
            form_key="AE",
            subject_key="SUBJ-01",
            record_data={
                "ae_term": "Dizziness",
                "AEDECOD": "DIZZINESS",
                "ae_start_date": "2026-03-02",
                "ae_serious": "no",
                "ae_severity": "2",
                "ae_relationship": "PROBABLE",
                "ae_action_taken": "NONE",
                "ae_outcome": "RECOVERED",
                "patientName": "John Secret",
            },
        ),
    ]
    sdk.records.list.side_effect = lambda **_kw: iter(ae_records)
    sdk.visits.list.return_value = []

    csv_path = tmp_path / "ae_sdtm.csv"
    export_mod.export_to_csv(
        sdk,
        "TRIAL_AE",
        str(csv_path),
        as_sdtm=True,
        domain="AE",
        batch_size=1,
    )
    df = pd.read_csv(csv_path)
    assert list(df["STUDYID"]) == ["TRIAL_AE", "TRIAL_AE"]
    assert list(df["DOMAIN"]) == ["AE", "AE"]
    assert list(df["USUBJID"]) == ["SUBJ-01", "SUBJ-01"]
    assert list(df["AESEQ"]) == [1, 2]
    assert list(df["AETERM"]) == ["Headache", "Dizziness"]
    assert list(df["AESTDTC"]) == ["2026-03-01T09:15:00", "2026-03-02"]
    assert list(df["AESER"]) == ["Y", "N"]
    assert list(df["patientName"]) == ["***MASKED***", "***MASKED***"]

    # Parquet and JSON SDTM exports also succeed with identical domain semantics
    pq_path = tmp_path / "ae_sdtm.parquet"
    export_mod.export_to_parquet(
        sdk,
        "TRIAL_AE",
        str(pq_path),
        as_sdtm=True,
        sdtm_domain="AE",
        standards_profile="drug",
        min_standards_score=95.0,
    )
    pq_df = pd.read_parquet(pq_path)
    assert list(pq_df["AESEQ"]) == [1, 2]

    import json

    json_path = tmp_path / "ae_sdtm.json"
    export_mod.export_to_json(sdk, "TRIAL_AE", str(json_path), as_sdtm=True, domain="AE")
    json_rows = json.loads(json_path.read_text(encoding="utf-8"))
    assert [r["AESEQ"] for r in json_rows] == [1, 2]


def test_standards_profile_and_min_standards_score_gate_non_conforming_export(
    tmp_path: Path,
) -> None:
    """standards_profile='drug' with min_standards_score gates non-conforming exports before writing."""
    from imednet.errors import ExportError
    from imednet.models.forms import Form
    from imednet.models.records import Record

    sdk = MagicMock()
    sdk.forms.list.return_value = [Form(form_id=10, form_key="AE", form_name="Adverse Events")]
    sdk.variables.list.return_value = []
    # Missing aeDecod and aeRelationship, invalid non-CTCAE severity -> fails DrugSafetyProfile
    bad_records = [
        Record(
            record_id=1,
            study_key="BAD_DRUG_STUDY",
            form_id=10,
            form_key="AE",
            subject_key="S01",
            record_data={"aeTerm": "Rash", "aeSeverity": "SEVERE"},
        )
    ]
    sdk.records.list.side_effect = lambda **_kw: iter(bad_records)

    out_csv = tmp_path / "gated_ae.csv"
    with pytest.raises(ExportError, match="Standards readiness check failed"):
        export_mod.export_to_csv(
            sdk,
            "BAD_DRUG_STUDY",
            str(out_csv),
            as_sdtm=True,
            domain="AE",
            standards_profile="drug",
            min_standards_score=90.0,
        )
    assert not out_csv.exists()


@pytest.mark.parametrize(
    ("convention", "expected_cols"),
    [
        ("snake_case", {"study_key", "record_id", "subject_key", "ae_term", "sys_bp"}),
        ("camelCase", {"studyKey", "recordId", "subjectKey", "aeTerm", "sysBp"}),
        ("sdtm", {"STUDYID", "RECORD_ID", "USUBJID", "AETERM", "SYSBP"}),
    ],
)
def test_export_to_parquet_naming_conventions(
    tmp_path: Path, convention: str, expected_cols: set[str]
) -> None:
    """export_to_parquet honors naming_convention across snake_case, camelCase, and sdtm."""
    from imednet.models.forms import Form
    from imednet.models.records import Record
    from imednet.models.variables import Variable

    sdk = MagicMock()
    sdk.forms.list.return_value = [Form(form_id=1, form_key="VITALS", form_name="Vitals")]
    sdk.variables.list.return_value = [
        Variable(variable_id=1, form_id=1, form_key="VITALS", variable_name="aeTerm"),
        Variable(variable_id=2, form_id=1, form_key="VITALS", variable_name="sys_bp"),
    ]
    recs = [
        Record(
            record_id=7,
            study_key="CONV_STUDY",
            form_id=1,
            form_key="VITALS",
            subject_key="S7",
            record_data={"aeTerm": "Mild", "sys_bp": 118},
        )
    ]
    sdk.records.list.side_effect = lambda **_kw: iter(recs)

    pq_file = tmp_path / f"out_{convention}.parquet"
    export_mod.export_to_parquet(
        sdk,
        "CONV_STUDY",
        str(pq_file),
        naming_convention=convention,
    )
    result_df = pd.read_parquet(pq_file)
    assert expected_cols.issubset(set(result_df.columns))


def test_incremental_reconciliation_with_sdtm_naming_convention(tmp_path: Path) -> None:
    """Incremental Parquet, DuckDB, and SQL exports deduplicate updates and reconcile deletes under naming_convention='sdtm'."""
    import duckdb
    from sqlalchemy import create_engine

    from imednet.models.forms import Form
    from imednet.models.records import Record
    from imednet.models.variables import Variable

    sdk = MagicMock()
    sdk.forms.list.return_value = [Form(form_id=1, form_key="VITALS", form_name="Vitals")]
    sdk.variables.list.return_value = [
        Variable(variable_id=1, form_id=1, form_key="VITALS", variable_name="sys_bp"),
    ]

    initial_recs = [
        Record(
            record_id=1,
            study_key="INC_SDTM",
            form_id=1,
            form_key="VITALS",
            subject_key="S1",
            date_modified="2026-01-01T00:00:00Z",
            record_data={"sys_bp": 110},
        ),
        Record(
            record_id=2,
            study_key="INC_SDTM",
            form_id=1,
            form_key="VITALS",
            subject_key="S2",
            date_modified="2026-01-01T00:00:00Z",
            record_data={"sys_bp": 120},
        ),
    ]
    delta_recs = [
        Record(
            record_id=1,
            study_key="INC_SDTM",
            form_id=1,
            form_key="VITALS",
            subject_key="S1",
            date_modified="2026-01-02T00:00:00Z",
            record_data={"sys_bp": 135},
        ),
        Record(
            record_id=2,
            study_key="INC_SDTM",
            form_id=1,
            form_key="VITALS",
            subject_key="S2",
            deleted=True,
            date_modified="2026-01-02T00:00:00Z",
            record_data={"sys_bp": 120},
        ),
    ]

    pq_file = tmp_path / "inc_sdtm.parquet"
    duck_file = tmp_path / "inc_sdtm.duckdb"
    sqlite_url = f"sqlite:///{tmp_path / 'inc_sdtm.db'}"

    sdk.records.list.side_effect = lambda **_kw: iter(initial_recs)
    export_mod.export_to_parquet(sdk, "INC_SDTM", str(pq_file), naming_convention="sdtm")
    export_mod.export_to_duckdb(sdk, "INC_SDTM", str(duck_file), "vitals", naming_convention="sdtm")
    export_mod.export_to_sql(
        sdk,
        "INC_SDTM",
        "vitals",
        sqlite_url,
        quality_gate_enabled=False,
        naming_convention="sdtm",
    )

    sdk.records.list.side_effect = lambda **_kw: iter(delta_recs)
    export_mod.export_to_parquet(
        sdk,
        "INC_SDTM",
        str(pq_file),
        naming_convention="sdtm",
        modified_since="2026-01-02T00:00:00Z",
    )
    export_mod.export_to_duckdb(
        sdk,
        "INC_SDTM",
        str(duck_file),
        "vitals",
        naming_convention="sdtm",
        modified_since="2026-01-02T00:00:00Z",
    )
    export_mod.export_to_sql(
        sdk,
        "INC_SDTM",
        "vitals",
        sqlite_url,
        quality_gate_enabled=False,
        naming_convention="sdtm",
        modified_since="2026-01-02T00:00:00Z",
    )

    pq_df = pd.read_parquet(pq_file)
    assert pq_df["RECORD_ID"].tolist() == [1]
    assert pq_df["SYSBP"].tolist() == [135]

    with duckdb.connect(str(duck_file)) as conn:
        duck_df = conn.execute('SELECT "RECORD_ID", "SYSBP" FROM vitals').df()
    assert duck_df["RECORD_ID"].tolist() == [1]
    assert duck_df["SYSBP"].tolist() == [135]

    engine = create_engine(sqlite_url)
    try:
        sql_df = pd.read_sql_table("vitals", engine)
    finally:
        engine.dispose()
    assert sql_df["RECORD_ID"].tolist() == [1]
    assert sql_df["SYSBP"].tolist() == [135]
