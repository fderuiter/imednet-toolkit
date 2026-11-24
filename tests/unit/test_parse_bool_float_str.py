"""Unit tests for parse bool float str."""

import pytest

from imednet.utils.validators import parse_bool


@pytest.mark.parametrize(
    ("input_val", "expected"),
    [
        ("1.0", True),
        ("0.0", False),
        ("1.000", True),
        ("-1.0", True),
        ("0.1", True),
        ("1e1", True),
        ("0e1", False),
        ("inf", False),
        ("-inf", False),
        ("nan", False),
        ("NaN", False),
        ("123.456", True),
        ("0.000", False),
    ],
)
def test_parse_bool_float_strings(input_val, expected):
    """Test that parse_bool handles float strings while rejecting non-finite values."""
    assert parse_bool(input_val) is expected


@pytest.mark.parametrize("non_finite", ["inf", "-inf", "nan", "NaN", float("nan"), float("inf")])
def test_parse_bool_non_finite_strict_raises(monkeypatch, non_finite):
    """Verify parse_bool raises ValueError on NaN and infinity in strict mode."""
    with pytest.raises(ValueError, match="non-finite"):
        parse_bool(non_finite, strict=True)
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "1")
    with pytest.raises(ValueError, match="non-finite"):
        parse_bool(non_finite)
