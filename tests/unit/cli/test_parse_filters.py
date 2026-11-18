"""Unit tests for parse filters."""

import pytest

from imednet.cli import parse_filter_args


def test_parse_filter_args_none():
    """Test that parse filter args none."""
    assert parse_filter_args(None) is None


def test_parse_filter_args_types():
    """Test that parse filter args types."""
    result = parse_filter_args(["a=1", "b=true", "c=no", "d=text"])
    assert result == {"a": 1, "b": True, "c": "no", "d": "text"}


def test_parse_filter_args_invalid():
    """Test that parse filter args invalid."""
    with pytest.raises(SystemExit) as exc_info:
        parse_filter_args(["bad"])
    assert exc_info.value.code == 1


def test_parse_filter_args_invalid_escaped(capfd: pytest.CaptureFixture[str]):
    """Test that parse filter args invalid escaped."""
    with pytest.raises(SystemExit) as exc_info:
        parse_filter_args(["[red]bad[/red]"])
    assert exc_info.value.code == 1

    captured = capfd.readouterr()
    assert "[red]bad[/red]" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("op", ["<", "<=", ">", ">=", "==", "!=", "=~"])
def test_parse_filter_args_comparison_operators(op: str) -> None:
    """All seven iMednet comparison operators parse into (operator, value) tuples."""
    result = parse_filter_args([f"age{op}30"])
    assert result == {"age": (op, 30)}


def test_parse_filter_args_semicolon_and_repeatable() -> None:
    """Semicolon-joined and repeated --filter arguments combine as AND conditions."""
    result = parse_filter_args(["formId>10;formType==SUBJECT", "active=false"])
    assert result == {
        "formId": (">", 10),
        "formType": ("==", "SUBJECT"),
        "active": False,
    }


def test_parse_filter_args_duplicate_key_range_preserves_both_clauses() -> None:
    """Range filters on the same attribute preserve both clauses via raw filter string."""
    result = parse_filter_args(["age>=18;age<=65"])
    assert result == {"filter": "age>=18;age<=65"}


def test_parse_filter_args_comma_or_connector() -> None:
    """Comma-separated OR filters build a validated server filter string."""
    result = parse_filter_args(["form_key==DM,form_key==VS"])
    assert result == {"filter": "formKey==DM,formKey==VS"}


def test_parse_filter_args_quoted_spaces_and_connectors() -> None:
    """Quoted values preserve spaces and connectors without triggering exclusivity errors."""
    result = parse_filter_args(['site_name=="Smith, John";status=="In Progress"'])
    assert result == {
        "site_name": ("==", "Smith, John"),
        "status": ("==", "In Progress"),
    }

    or_result = parse_filter_args(['siteName=="A;B",siteName=="C;D"'])
    assert or_result == {"filter": 'siteName=="A;B",siteName=="C;D"'}


@pytest.mark.parametrize(
    "bad_args",
    [
        ["a==1;b==2,c==3"],
        ["a==1,b==2", "c==3"],
        ["age > 10"],
        ['siteName=="Unclosed'],
        ["a===1"],
        ["a~=1"],
        ["=val"],
        ["key="],
        ["a==1;"],
        [""],
        ['key=="mid"quote"'],
        ['key==unquoted"quote'],
        ["123bad==1"],
    ],
)
def test_parse_filter_args_rejects_invalid_expressions_on_stderr(
    bad_args: list[str], capfd: pytest.CaptureFixture[str]
) -> None:
    """Invalid syntax or mixed ';' and ',' connectors exit 1 and write to stderr only."""
    with pytest.raises(SystemExit) as exc_info:
        parse_filter_args(bad_args)
    assert exc_info.value.code == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "Invalid filter" in captured.err
