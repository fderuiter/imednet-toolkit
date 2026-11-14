"""Unit tests for duckdb export."""

import sys
from builtins import __import__ as builtin_import
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

import imednet.integrations.export as export_mod


def test_export_to_duckdb_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that export to duckdb happy path."""
    duckdb = pytest.importorskip("duckdb")

    sdk = MagicMock()
    df = pd.DataFrame({"record_id": [1, 2], "answer": ["yes", "no"]})
    monkeypatch.setattr(export_mod, "_prepare_export_df", MagicMock(return_value=df))

    db_path = tmp_path / "study.duckdb"
    export_mod.export_to_duckdb(sdk, "STUDY", str(db_path), "records")

    conn = duckdb.connect(str(db_path))
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info('records')").fetchall()]
        row_count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    finally:
        conn.close()

    assert columns == ["record_id", "answer"]
    assert row_count == 2


def test_export_to_duckdb_wide_dataframe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that export to duckdb wide dataframe."""
    duckdb = pytest.importorskip("duckdb")

    sdk = MagicMock()
    wide_columns = [f"c{i}" for i in range(2101)]
    wide_df = pd.DataFrame([range(2101)], columns=wide_columns)
    monkeypatch.setattr(export_mod, "_prepare_export_df", MagicMock(return_value=wide_df))

    db_path = tmp_path / "wide.duckdb"
    export_mod.export_to_duckdb(sdk, "STUDY", str(db_path), "wide_records")

    conn = duckdb.connect(str(db_path))
    try:
        row_count = conn.execute("SELECT COUNT(*) FROM wide_records").fetchone()[0]
        column_count = len(conn.execute("PRAGMA table_info('wide_records')").fetchall())
    finally:
        conn.close()

    assert row_count == 1
    assert column_count == len(wide_columns)


def test_export_to_duckdb_by_form_creates_per_form_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export to duckdb by form creates per form tables."""
    duckdb = pytest.importorskip("duckdb")

    sdk = MagicMock()
    sdk.forms.list.return_value = [
        SimpleNamespace(form_id=1, form_key="FORM_A"),
        SimpleNamespace(form_id=2, form_key="FORM_B"),
    ]

    monkeypatch.setattr(
        export_mod,
        "_records_df",
        MagicMock(
            side_effect=[
                pd.DataFrame({"record_id": [1], "value": ["A"]}),
                pd.DataFrame({"record_id": [2], "value": ["B"]}),
            ]
        ),
    )

    db_path = tmp_path / "forms.duckdb"
    export_mod.export_to_duckdb_by_form(sdk, "STUDY", str(db_path))

    conn = duckdb.connect(str(db_path))
    try:
        table_a = conn.execute("SELECT record_id, value FROM FORM_A").fetchall()
        table_b = conn.execute("SELECT record_id, value FROM FORM_B").fetchall()
    finally:
        conn.close()

    assert table_a == [(1, "A")]
    assert table_b == [(2, "B")]


def test_export_to_duckdb_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that export to duckdb import error."""

    def fake_import(
        name: str,
        globals_arg: Any = None,
        locals_arg: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        """Helper function to fake import."""
        if name == "duckdb":
            raise ImportError("No module named duckdb")
        return builtin_import(name, globals_arg, locals_arg, fromlist, level)

    monkeypatch.delitem(sys.modules, "duckdb", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(ImportError, match=r"pip install"):
        export_mod.export_to_duckdb(MagicMock(), "STUDY", "out.duckdb", "records")

    with pytest.raises(ImportError, match=r"pip install"):
        export_mod.export_to_duckdb_by_form(MagicMock(), "STUDY", "out.duckdb")


def test_export_to_duckdb_type_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that export to duckdb type handling."""
    duckdb = pytest.importorskip("duckdb")

    sdk = MagicMock()
    df = pd.DataFrame(
        {
            "event_date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "note": ["first", "second"],
        }
    )
    monkeypatch.setattr(export_mod, "_prepare_export_df", MagicMock(return_value=df))

    db_path = tmp_path / "typed.duckdb"
    export_mod.export_to_duckdb(sdk, "STUDY", str(db_path), "typed_records")

    conn = duckdb.connect(str(db_path))
    try:
        schema = {
            row[1]: row[2].upper()
            for row in conn.execute("PRAGMA table_info('typed_records')").fetchall()
        }
        rows = conn.execute("SELECT event_date, note FROM typed_records ORDER BY note").fetchall()
    finally:
        conn.close()

    assert schema["event_date"] in {
        "DATE",
        "TIMESTAMP",
        "TIMESTAMP_NS",
        "TIMESTAMP_MS",
        "TIMESTAMP_S",
    }
    assert "VARCHAR" in schema["note"]
    assert rows[0][1] == "first"
    assert rows[1][1] == "second"


def test_export_to_duckdb_connection_closed_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export to duckdb connection closed on error."""
    sdk = MagicMock()
    df = pd.DataFrame({"a": [1]})
    monkeypatch.setattr(export_mod, "_prepare_export_df", MagicMock(return_value=df))

    conn = MagicMock()
    conn.execute.side_effect = RuntimeError("boom")
    duckdb_module = ModuleType("duckdb")
    duckdb_module.connect = MagicMock(return_value=conn)
    monkeypatch.setitem(sys.modules, "duckdb", duckdb_module)

    with pytest.raises(RuntimeError, match="boom"):
        export_mod.export_to_duckdb(sdk, "STUDY", str(tmp_path / "out.duckdb"), "records")

    conn.close.assert_called_once_with()


@pytest.mark.parametrize(
    "valid_name",
    [
        "records",
        "my_table",
        "Form123",
        "TABLE_NAME",
        "_hidden",
        "v1_2_3",
    ],
)
def test_quote_duckdb_identifier_valid(valid_name: str) -> None:
    """Test that valid identifiers are properly quoted."""
    quoted = export_mod._quote_duckdb_identifier(valid_name)
    assert quoted == f'"{valid_name}"'


@pytest.mark.parametrize(
    "invalid_name",
    [
        "my table",
        "table; DROP TABLE users; --",
        'bad"name',
        "table-name",
        "table.name",
        "table$name",
        "table\nname",
        "table\x00name",
        "",
        " ",
    ],
)
def test_quote_duckdb_identifier_invalid(invalid_name: str) -> None:
    """Test that invalid identifiers raise ValueError."""
    with pytest.raises(ValueError, match="Invalid identifier"):
        export_mod._quote_duckdb_identifier(invalid_name)


@pytest.mark.parametrize(
    "payload",
    [
        "my table",
        "records; DROP TABLE records; --",
        'records" OR 1=1 --',
        "bad-name",
    ],
)
def test_export_to_duckdb_rejects_invalid_table_name(payload: str, tmp_path: Path) -> None:
    """Test that export_to_duckdb rejects invalid identifiers before DDL execution."""
    sdk = MagicMock()
    with pytest.raises(ValueError, match="Invalid identifier"):
        export_mod.export_to_duckdb(sdk, "STUDY", str(tmp_path / "out.duckdb"), payload)


def test_export_to_duckdb_by_form_rejects_invalid_form_key(tmp_path: Path) -> None:
    """Test that export_to_duckdb_by_form rejects invalid form_key before DDL execution."""
    pytest.importorskip("duckdb")
    sdk = MagicMock()
    sdk.forms.list.return_value = [
        SimpleNamespace(form_id=1, form_key="FORM; DROP TABLE users; --")
    ]

    with pytest.raises(ValueError, match="Invalid identifier"):
        export_mod.export_to_duckdb_by_form(sdk, "STUDY", str(tmp_path / "out.duckdb"))
