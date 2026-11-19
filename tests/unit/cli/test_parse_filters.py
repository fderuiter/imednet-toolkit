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
