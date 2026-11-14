"""Unit tests for integrations export."""

import sys
from builtins import __import__ as builtin_import
from datetime import datetime
from types import ModuleType
from unittest.mock import MagicMock, call

import pandas as pd
import pytest

import imednet.integrations.export as export_mod


def _setup_mapper(monkeypatch):
    """Helper function to setup mapper."""
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
    mapper_inst._iter_records.return_value = [MagicMock()]
    mapper_inst._parse_records.return_value = ([], 0)
    mapper_inst._build_dataframe.return_value = df

    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)
    import imednet.integrations.export as export_mod_module

    monkeypatch.setattr(
        export_mod_module, "apply_quality_gate", lambda sdk, study, recs, conf: recs
    )
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
    sdk = MagicMock()

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


def test_export_to_json(monkeypatch):
    """Test that export to json."""
    df, mapper_cls, mapper_inst = _setup_mapper(monkeypatch)
    df.where.return_value.to_dict.return_value = [{"A": 1}]
    sdk = MagicMock()

    export_mod.export_to_json(sdk, "STUDY", "out.json")

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
    sdk = MagicMock()

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
    mapper_inst._iter_records.return_value = [MagicMock()]
    mapper_inst._parse_records.return_value = ([], 0)
    mapper_inst._build_dataframe.return_value = df
    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)
    import imednet.integrations.export as export_mod_module

    monkeypatch.setattr(
        export_mod_module, "apply_quality_gate", lambda sdk, study, recs, conf: recs
    )
    sdk = MagicMock()

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
    mapper_inst._iter_records.return_value = [MagicMock()]
    mapper_inst._parse_records.return_value = ([], 0)
    mapper_inst._build_dataframe.return_value = df
    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)
    import imednet.integrations.export as export_mod_module

    monkeypatch.setattr(
        export_mod_module, "apply_quality_gate", lambda sdk, study, recs, conf: recs
    )
    sdk = MagicMock()

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
    mapper_inst._iter_records.return_value = [MagicMock()]
    mapper_inst._parse_records.return_value = ([], 0)
    mapper_inst._build_dataframe.return_value = df
    mapper_cls = MagicMock(return_value=mapper_inst)
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)
    import imednet.integrations.export as export_mod_module

    monkeypatch.setattr(
        export_mod_module, "apply_quality_gate", lambda sdk, study, recs, conf: recs
    )

    engine = MagicMock()
    engine.dialect.name = "sqlite"
    sa_module = ModuleType("sqlalchemy")
    sa_module.create_engine = MagicMock(return_value=engine)
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa_module)

    export_mod.export_to_sql(MagicMock(), "STUDY", "table", "sqlite://")
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
    df1 = pd.DataFrame({"A": [1]})
    df2 = pd.DataFrame({"B": [2]})
    records_df_mock = MagicMock(side_effect=[df1, df2])
    monkeypatch.setattr(export_mod, "_records_df", records_df_mock)

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

    assert records_df_mock.call_args_list == [
        call(
            sdk,
            "STUDY",
            use_labels_as_columns=True,
            variable_whitelist=["A", "B"],
            form_whitelist=[1],
        ),
        call(
            sdk,
            "STUDY",
            use_labels_as_columns=True,
            variable_whitelist=["A", "B"],
            form_whitelist=[2],
        ),
    ]
    assert conn.register.call_args_list == [call("df", df1), call("df", df2)]
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
