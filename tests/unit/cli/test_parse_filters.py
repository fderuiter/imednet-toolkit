"""Unit tests for parse filters."""

import pytest

from imednet.cli import parse_filter_args
from imednet.models.visits import Visit


def test_parse_filter_args_none():
    """Test that parse filter args none."""
    assert parse_filter_args(None) is None


def test_parse_filter_args_types():
    """Test that parse filter args types."""
    result = parse_filter_args(["a=1", "b=true", "c=no", "d=text"])
    assert result == {"a": 1, "b": True, "c": "no", "d": "text"}


def test_parse_filter_args_comparison_operators():
    """Test that parse_filter_args preserves comparison operators without extra equals signs."""
    assert parse_filter_args(["dictionaryName==MedDRA"]) == {"dictionaryName": ("==", "MedDRA")}
    assert parse_filter_args(["revision>=2"]) == {"revision": (">=", 2)}
    assert parse_filter_args(["subjectStatus!=Enrolled"]) == {"subjectStatus": ("!=", "Enrolled")}
    assert parse_filter_args(["revision>1"]) == {"revision": (">", 1)}

    combined = parse_filter_args(
        [
            "dictionaryName==MedDRA",
            "revision>=2",
            "subjectStatus!=Enrolled",
            "revision>1",
        ]
    )
    assert combined == {
        "dictionaryName": ("==", "MedDRA"),
        "revision": [(">=", 2), (">", 1)],
        "subjectStatus": ("!=", "Enrolled"),
    }


def test_parse_filter_args_rejects_unknown_visit_attribute(capfd: pytest.CaptureFixture[str]):
    """Test that unknown filter keys for Visit fail deterministically."""
    assert parse_filter_args(["interval_name=Screening"], model=Visit) == {
        "interval_name": "Screening"
    }
    with pytest.raises(SystemExit) as exc_info:
        parse_filter_args(["visit_key=SCREENING"], model=Visit)
    assert exc_info.value.code == 1
    captured = capfd.readouterr()
    assert "Unknown filter attribute 'visit_key' for Visit" in captured.err


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
def test_parse_filter_args_all_seven_comparison_operators(op: str) -> None:
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
    """Range filters on the same attribute preserve both clauses as AND operator tuples."""
    from imednet.utils.filters import build_filter_string

    result = parse_filter_args(["age>=18;age<=65"])
    assert result == {"age": [(">=", 18), ("<=", 65)]}
    assert build_filter_string(result) == "age>=18;age<=65"


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


def test_parse_filter_args_preserves_zero_padded_keys_and_superscripts() -> None:
    """Zero-padded identifiers and Unicode superscripts stay strings without crashing."""
    from imednet.models.codings import Coding
    from imednet.models.subjects import Subject

    assert parse_filter_args(["subject_key=001"], model=Subject) == {"subject_key": "001"}
    assert parse_filter_args(["subject_key=123"], model=Subject) == {"subject_key": "123"}
    assert parse_filter_args(["code=0042", "value=²"], model=Coding) == {
        "code": "0042",
        "value": "²",
    }
    assert parse_filter_args(["subject_key=001", "value=²", "count=0", "page=12"]) == {
        "subject_key": "001",
        "value": "²",
        "count": 0,
        "page": 12,
    }


@pytest.mark.parametrize(
    ("expr", "expected_fragment"),
    [
        ("revision=abc", "Invalid integer value 'abc' for attribute 'revision'"),
        ("revision=²", "Invalid integer value '²' for attribute 'revision'"),
    ],
)
def test_parse_filter_args_rejects_invalid_model_integer_values(
    expr: str, expected_fragment: str, capfd: pytest.CaptureFixture[str]
) -> None:
    """Invalid integer values for model attributes exit 1 with diagnostic on stderr."""
    from imednet.models.codings import Coding

    with pytest.raises(SystemExit) as exc_info:
        parse_filter_args([expr], model=Coding)
    assert exc_info.value.code == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert expected_fragment in captured.err


def test_parse_filter_args_rejects_invalid_model_boolean_values(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Invalid boolean values for model attributes exit 1 with diagnostic on stderr."""
    from imednet.models.forms import Form

    with pytest.raises(SystemExit) as exc_info:
        parse_filter_args(["disabled=maybe"], model=Form)
    assert exc_info.value.code == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "Invalid boolean value 'maybe' for attribute 'disabled'" in captured.err
