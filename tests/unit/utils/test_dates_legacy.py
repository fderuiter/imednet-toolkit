"""Unit tests for dates legacy."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from imednet.utils import dates


@pytest.fixture
def force_legacy_dates():
    """Forces the legacy date parsing path (simulating Python < 3.11)."""
    with patch("imednet.utils.dates._IS_PY311_OR_GREATER", False):
        yield


@pytest.mark.unit
class TestLegacyDateParsing:
    """Tests for `parse_iso_datetime` running in legacy mode (Python < 3.11 simulation).

    This ensures the manual padding, truncation, and 'Z' handling logic is correct.
    """

    def test_legacy_z_handling(self, force_legacy_dates):
        """Test replacement of 'Z' with '+00:00'."""
        dt = dates.parse_iso_datetime("2024-01-01T12:00:00Z")
        assert dt == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_legacy_padding_1_digit(self, force_legacy_dates):
        """Test padding of 1 fractional digit to 6."""
        # .1 -> .100000
        dt = dates.parse_iso_datetime("2024-01-01T12:00:00.1")
        assert dt.microsecond == 100000
        assert dt.tzinfo is None

    def test_legacy_padding_2_digits_with_offset(self, force_legacy_dates):
        """Test padding of 2 fractional digits to 6 with timezone offset."""
        # .12 -> .120000
        dt = dates.parse_iso_datetime("2024-01-01T12:00:00.12+02:00")
        assert dt.microsecond == 120000
        assert dt.tzinfo is not None
        assert dt.tzinfo.utcoffset(dt) == timedelta(hours=2)

    def test_legacy_truncation_7_digits(self, force_legacy_dates):
        """Test truncation of 7 fractional digits to 6."""
        # .1234567 -> .123456
        dt = dates.parse_iso_datetime("2024-01-01T12:00:00.1234567")
        assert dt.microsecond == 123456

    def test_legacy_truncation_9_digits_with_z(self, force_legacy_dates):
        """Test truncation of 9 fractional digits to 6 with Z suffix."""
        # Z is replaced first, then regex finds .123456789 before +00:00
        dt = dates.parse_iso_datetime("2024-01-01T12:00:00.123456789Z")
        assert dt.microsecond == 123456
        assert dt.tzinfo == timezone.utc

    def test_legacy_exact_6_digits(self, force_legacy_dates):
        """Test that exactly 6 digits are untouched."""
        dt = dates.parse_iso_datetime("2024-01-01T12:00:00.123456")
        assert dt.microsecond == 123456

    def test_legacy_no_fraction(self, force_legacy_dates):
        """Test parsing without fractional seconds."""
        dt = dates.parse_iso_datetime("2024-01-01T12:00:00")
        assert dt.microsecond == 0

    def test_legacy_invalid_format(self, force_legacy_dates):
        """Test that invalid formats still raise ValueError."""
        with pytest.raises(ValueError):
            dates.parse_iso_datetime("not-a-date")

    def test_legacy_none(self, force_legacy_dates):
        """Test that None raises TypeError/AttributeError (same as native)."""
        with pytest.raises((TypeError, AttributeError)):
            dates.parse_iso_datetime(None)  # type: ignore[arg-type]
