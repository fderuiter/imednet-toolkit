"""Unit tests for validators."""

from datetime import datetime, timezone

import pytest

from imednet.utils.validators import (
    parse_bool,
    parse_datetime,
    parse_dict_or_default,
    parse_int_or_default,
    parse_list_or_default,
    parse_str_or_default,
)


def test_parse_datetime_handles_empty_values():
    """Test that parse datetime handles empty values."""
    sentinel = datetime(1969, 4, 20, 16, 20)
    assert parse_datetime("") == sentinel
    assert parse_datetime(None) == sentinel


def test_parse_datetime_handles_numeric_timestamps():
    """Test that parse datetime handles numeric timestamps."""
    dt = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    ts = dt.timestamp()
    assert parse_datetime(ts) == dt
    assert parse_datetime(int(ts)) == dt


def test_parse_datetime_handles_zero_timestamps():
    """Test that parse datetime handles zero timestamps."""
    dt = datetime(1970, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert parse_datetime(0) == dt
    assert parse_datetime(0.0) == dt


def test_parse_bool_handles_various_representations():
    """Test that parse bool handles various representations."""
    assert parse_bool(True) is True
    assert parse_bool("true") is True
    assert parse_bool("1") is True
    assert parse_bool(1) is True
    assert parse_bool("yes") is True
    assert parse_bool("y") is True
    assert parse_bool("t") is True

    assert parse_bool(False) is False
    assert parse_bool("false") is False
    assert parse_bool("0") is False
    assert parse_bool(0) is False
    assert parse_bool("no") is False
    assert parse_bool("n") is False
    assert parse_bool("f") is False


def test_parse_bool_handles_floats():
    """Test that parse bool handles floats."""
    assert parse_bool("1.0") is True
    assert parse_bool(1.0) is True
    assert parse_bool("0.0") is False
    assert parse_bool(0.0) is False


def test_parse_bool_handles_irregular_casing():
    """Test that parse bool handles irregular casing."""
    assert parse_bool(" TrUe ") is True
    assert parse_bool(" fAlSe ") is False


def test_parse_bool_returns_false_for_invalid_strings():
    """Test that parse bool returns false for invalid strings."""
    assert parse_bool("invalid") is False
    assert parse_bool("apple") is False
    assert parse_bool("") is False


def test_parse_int_or_default_handles_valid_ints():
    """Test that parse int or default handles valid ints."""
    assert parse_int_or_default(42) == 42
    assert parse_int_or_default("42") == 42


def test_parse_int_or_default_handles_floats():
    """Test that parse int or default handles floats."""
    assert parse_int_or_default(42.0) == 42
    assert parse_int_or_default("42.0") == 42


def test_parse_int_or_default_uses_default_for_empty():
    """Test that parse int or default uses default for empty."""
    assert parse_int_or_default(None, default=10) == 10
    assert parse_int_or_default("", default=10) == 10


def test_parse_int_or_default_strict_raises_on_invalid():
    """Test that parse int or default strict raises on invalid."""
    with pytest.raises(ValueError):
        parse_int_or_default("invalid", strict=True)


def test_parse_int_or_default_returns_default_on_invalid_when_not_strict():
    """Test that parse int or default returns default on invalid when not strict."""
    assert parse_int_or_default("invalid", default=10) == 10


def test_parse_str_or_default():
    """Test that parse str or default."""
    assert parse_str_or_default("hello") == "hello"
    assert parse_str_or_default(42) == "42"
    assert parse_str_or_default(None, default="default") == "default"


def test_parse_list_or_default():
    """Test that parse list or default."""
    assert parse_list_or_default([1, 2]) == [1, 2]
    assert parse_list_or_default(42) == [42]
    assert parse_list_or_default(None) == []


def test_parse_dict_or_default():
    """Test that parse dict or default."""
    assert parse_dict_or_default({"a": 1}) == {"a": 1}
    assert parse_dict_or_default(None) == {}
    assert parse_dict_or_default("invalid") == {}


def test_parse_datetime_handles_datetime_objects():
    """Test that parse datetime handles datetime objects."""
    dt = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert parse_datetime(dt) == dt


def test_parse_datetime_handles_string_timestamps():
    """Test that parse datetime handles string timestamps."""
    dt = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert parse_datetime("2024-01-01T12:00:00Z") == dt


def test_parse_bool_handles_unusual_strings():
    """Test that parse bool handles unusual strings."""
    # Non-finite float strings must be rejected (False in permissive mode)
    assert parse_bool("inf") is False
    assert parse_bool("nan") is False
    assert parse_bool("1.5") is True
    assert parse_bool("100") is True
    assert parse_bool("-1") is True
    # Test edge cases for float fallback that should return False
    assert parse_bool(".") is False
    assert parse_bool("+") is False
    assert parse_bool("-") is False
    assert parse_bool("n") is False  # 'n' is in _FALSE_LOWER, explicitly False
    assert parse_bool("i") is False  # 'i' not in TRUE/FALSE sets, but triggers float fallback check


def test_parse_bool_handles_non_string_types():
    """Test that parse bool handles non string types."""
    # Non-truthy values according to rationale should return False
    assert parse_bool(None) is False
    assert parse_bool([]) is False
    assert parse_bool({}) is False
    assert parse_bool(object()) is False


def test_parse_bool_handles_extra_numeric_values():
    """Test that parse bool handles extra numeric values."""
    # Non-finite floats return False in permissive mode; finite non-zero numbers are True
    assert parse_bool(float("inf")) is False
    assert parse_bool(float("nan")) is False
    assert parse_bool(100) is True
    assert parse_bool(-1) is True
