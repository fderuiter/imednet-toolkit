"""Unit tests for models validators."""

import datetime

import pytest

from imednet.utils import validators


@pytest.mark.parametrize(
    ("input_val", "expected"),
    [
        # Boolean inputs
        (True, True),
        (False, False),
        # String inputs - True variants (optimized)
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("y", True),
        ("t", True),
        # String inputs - False variants (optimized)
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("n", False),
        ("f", False),
        # String inputs - Whitespace/Case (fallback path)
        (" true ", True),
        (" Yes ", True),
        ("TrUe", True),
        (" FALSE ", False),
        (" No ", False),
        ("FaLsE", False),
        # Numeric inputs
        (1, True),
        (0, False),
        (1.0, True),
        (0.0, False),
        (-1, True),  # Non-zero number is True
        # Edge cases / Invalid
        (None, False),
        ("maybe", False),
        ("", False),
        ([], False),
        ({}, False),
    ],
)
def test_parse_bool_comprehensive(input_val, expected):
    """Test that parse bool comprehensive."""
    assert validators.parse_bool(input_val) is expected


def test_parse_int_or_default_and_str_default():
    """Test that parse int or default and str default."""
    assert validators.parse_int_or_default("5") == 5
    assert validators.parse_int_or_default(None, default=2) == 2
    assert validators.parse_int_or_default("bad") == 0
    with pytest.raises(ValueError):
        validators.parse_int_or_default("bad", strict=True)

    # Float string robustness
    assert validators.parse_int_or_default("5.0") == 5
    assert validators.parse_int_or_default("5.9") == 5
    assert validators.parse_int_or_default(5.9) == 5
    assert validators.parse_int_or_default("inf") == 0
    with pytest.raises(OverflowError):
        validators.parse_int_or_default("inf", strict=True)

    assert validators.parse_str_or_default(None) == ""
    assert validators.parse_str_or_default(123) == "123"


def test_parse_list_and_dict_helpers():
    """Test that parse list and dict helpers."""
    assert validators.parse_list_or_default(None) == []
    assert validators.parse_list_or_default("a") == ["a"]
    assert validators.parse_list_or_default([1, 2]) == [1, 2]
    assert validators.parse_dict_or_default(None) == {}
    assert validators.parse_dict_or_default({"a": 1}) == {"a": 1}
    assert validators.parse_dict_or_default("bad") == {}


def test_parse_datetime_wrapper():
    """Test that parse datetime wrapper."""
    dt = datetime.datetime(2024, 1, 1)
    assert validators.parse_datetime(dt) == dt
    iso = "2024-01-01T00:00:00Z"
    parsed = validators.parse_datetime(iso)
    assert isinstance(parsed, datetime.datetime)
    assert validators.parse_datetime("") == datetime.datetime(1969, 4, 20, 16, 20)


def test_parse_datetime_default_date() -> None:
    """Return a sentinel date when given an empty or ``None`` value."""
    sentinel = datetime.datetime(1969, 4, 20, 16, 20)
    assert validators.parse_datetime("") == sentinel
    assert validators.parse_datetime(None) == sentinel  # type: ignore[arg-type]


def test_parse_datetime_parses_strings() -> None:
    """Parse ISO formatted strings into ``datetime`` objects."""
    iso = "2024-01-01T00:00:00Z"
    parsed = validators.parse_datetime(iso)
    assert parsed == datetime.datetime(2024, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)


def test_parse_datetime_numeric():
    """Test that parse datetime numeric."""
    timestamp = 1704067200  # 2024-01-01 00:00:00 UTC
    dt = validators.parse_datetime(timestamp)
    assert dt == datetime.datetime(2024, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)

    dt_float = validators.parse_datetime(float(timestamp))
    assert dt_float == datetime.datetime(2024, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)


def test_parse_bool_string_float():
    """Test that parse bool string float."""
    assert validators.parse_bool("1.0") is True
    assert validators.parse_bool("0.0") is False
    assert validators.parse_bool("-1") is True
    assert validators.parse_bool("+1") is True
    assert validators.parse_bool(".5") is True
    assert validators.parse_bool("nan") is True  # bool(float("nan")) is True
    assert validators.parse_bool("inf") is True
    assert validators.parse_bool("-inf") is True
    assert validators.parse_bool("nope") is False
    assert validators.parse_bool("invalid") is False


def test_parse_dict_or_default_structural_shift(caplog):
    """Test that parse dict or default structural shift."""
    import logging

    with caplog.at_level(logging.WARNING):
        result = validators.parse_dict_or_default([{"a": 1}, {"b": 2}])
        assert result == {"a": 1}
        assert (
            "Structural shift detected: API returned a list where an object was expected. Coercing by extracting the first item."
            in caplog.text
        )

    result_empty = validators.parse_dict_or_default([])
    assert result_empty == {}


def test_parse_list_or_default_structural_shift(caplog):
    """Test that parse list or default structural shift."""
    import logging

    with caplog.at_level(logging.WARNING):
        result = validators.parse_list_or_default({"a": 1})
        assert result == [{"a": 1}]
        assert (
            "Structural shift detected: API returned an object where a list was expected. Coercing by wrapping in a list."
            in caplog.text
        )


def test_parse_bool_invalid_float():
    """Test that parse bool invalid float."""
    # To cover the 'except (ValueError, TypeError)' block in parse_bool float fallback
    assert validators.parse_bool("1invalid") is False


@pytest.mark.parametrize("container", [["a", "b"], {"k": "v"}])
def test_str_normalizers_guard_against_containers(caplog, monkeypatch, container):
    """Verify parse_str_or_default and _optional_str emit drift warnings and fail in strict mode instead of calling str(v)."""
    import logging

    from pydantic import Field, ValidationError

    from imednet.models.base import ImednetBaseModel, _optional_str

    class SampleStrModel(ImednetBaseModel):
        opt_text: str | None = Field(default=None, alias="optText")
        req_text: str = Field(default="fallback", alias="reqText")

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "false")
    with caplog.at_level(logging.WARNING, logger="imednet.drift"):
        assert validators.parse_str_or_default(container, default="def") == "def"
        assert _optional_str(container) is None
        parsed = SampleStrModel.from_json({"optText": container, "reqText": container})
        assert parsed.opt_text is None
        assert parsed.req_text == ""
    assert "Expected str, got" in caplog.text

    with pytest.raises(ValueError, match="Expected str, got"):
        validators.parse_str_or_default(container, strict=True)

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "true")
    with pytest.raises(ValueError, match="Expected str, got"):
        _optional_str(container)
    with pytest.raises(ValidationError, match="Expected str, got"):
        SampleStrModel.from_json({"optText": container})
