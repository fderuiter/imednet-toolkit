"""Unit tests for lazy attrs."""

from imednet.utils import (
    intervals_to_dataframe,
    records_to_dataframe,
    sites_to_dataframe,
    visits_to_dataframe,
)
from imednet.validation.cache import SchemaCache


def test_lazy_attrs_available() -> None:
    """Test that lazy attrs available."""
    assert callable(records_to_dataframe)
    assert callable(visits_to_dataframe)
    assert callable(intervals_to_dataframe)
    assert callable(sites_to_dataframe)
    assert isinstance(SchemaCache, type)
